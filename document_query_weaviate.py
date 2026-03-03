#!/usr/bin/env python3
"""
E-FINDER — Weaviate-Powered Document Query Agent
==================================================
Drop-in upgrade for the DocumentQueryAgent in swarm.py.

Replaces the Claude → MongoDB query translation approach with a two-pass
hybrid search strategy:

  Pass 1 — Weaviate hybrid search (BM25 + vector similarity)
    → Finds semantically relevant chunks even without exact keyword matches
    → Returns ranked chunks with entity metadata

  Pass 2 — MongoDB structured query (optional, for precise filters)
    → Runs the existing MongoDB translation for structured constraints
      (e.g. "documents from section X" or "documents with redaction density > 0.5")

  Synthesis — Claude combines both result sets into a final answer

This gives E-FINDER the best of both worlds:
  - Semantic search: "documents about financial transfers" finds relevant
    content even if the exact phrase never appears
  - Structured search: "find all court filings from 2006" remains precise

Usage (standalone test):
  export MONGODB_URI="mongodb+srv://..."
  export ANTHROPIC_API_KEY="sk-ant-..."
  export OPENAI_API_KEY="sk-..."
  export WEAVIATE_URL="http://localhost:8080"

  python3 document_query_weaviate.py \\
    --question "What financial connections appear between Epstein and Deutsche Bank?"
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = "doj_investigation"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
WEAVIATE_API_KEY = os.environ.get("WEAVIATE_API_KEY", "")
MODEL = "claude-sonnet-4-5-20250929"
COLLECTION_NAME = "EfinderChunks"

WEAVIATE_AVAILABLE = False  # set to True after successful connection


# ── Dependency bootstrap ──────────────────────────────────────────────────────
def _install(pkg):
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          pkg, "--break-system-packages", "-q"])


try:
    from pymongo import MongoClient
    from pymongo.database import Database
except ImportError:
    _install("pymongo")
    from pymongo import MongoClient
    from pymongo.database import Database

try:
    import anthropic
except ImportError:
    _install("anthropic")
    import anthropic

try:
    import weaviate
    import weaviate.classes as wvc
    WEAVIATE_AVAILABLE = True
except ImportError:
    log.warning("weaviate-client not installed. Weaviate search disabled.")


# ── AgentResult (mirrors swarm.py) ───────────────────────────────────────────
@dataclass
class AgentResult:
    agent_name: str
    task: dict
    findings: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    confidence: float = 0.0
    open_questions: list = field(default_factory=list)
    duration_seconds: float = 0.0
    api_calls: int = 0
    estimated_cost: float = 0.0
    error: Optional[str] = None


# ── Weaviate client ───────────────────────────────────────────────────────────
def _get_weaviate_client():
    if not WEAVIATE_AVAILABLE:
        return None
    try:
        if WEAVIATE_API_KEY:
            client = weaviate.connect_to_weaviate_cloud(
                cluster_url=WEAVIATE_URL,
                auth_credentials=wvc.init.Auth.api_key(WEAVIATE_API_KEY),
                headers={"X-OpenAI-Api-Key": OPENAI_API_KEY} if OPENAI_API_KEY else {},
            )
        else:
            host = WEAVIATE_URL.replace("http://", "").replace("https://", "").split(":")[0]
            port = int(WEAVIATE_URL.split(":")[-1]) if ":" in WEAVIATE_URL else 8080
            client = weaviate.connect_to_local(
                host=host, port=port,
                headers={"X-OpenAI-Api-Key": OPENAI_API_KEY} if OPENAI_API_KEY else {},
            )
        client.is_ready()
        return client
    except Exception as e:
        log.warning("Weaviate unavailable (%s) — falling back to MongoDB-only search.", e)
        return None


# ── Weaviate hybrid search ────────────────────────────────────────────────────
def weaviate_hybrid_search(
    weaviate_client,
    question: str,
    limit: int = 20,
    alpha: float = 0.5,
    filters: Optional[dict] = None,
) -> list[dict]:
    """
    Run a hybrid search (BM25 + vector) against the EfinderChunks collection.

    alpha controls the blend:
      0.0 = pure BM25 keyword search
      0.5 = balanced hybrid (recommended default)
      1.0 = pure vector / semantic search

    Returns a list of result dicts with chunk text, metadata, and score.
    """
    if not weaviate_client:
        return []

    try:
        collection = weaviate_client.collections.get(COLLECTION_NAME)

        # Build Weaviate filter if structured constraints were provided
        weaviate_filter = None
        if filters:
            filter_parts = []
            if filters.get("section"):
                filter_parts.append(
                    wvc.query.Filter.by_property("section").equal(filters["section"])
                )
            if filters.get("document_type"):
                filter_parts.append(
                    wvc.query.Filter.by_property("document_type").equal(filters["document_type"])
                )
            if filters.get("has_redactions") is not None:
                filter_parts.append(
                    wvc.query.Filter.by_property("has_redactions").equal(filters["has_redactions"])
                )
            if filters.get("person"):
                filter_parts.append(
                    wvc.query.Filter.by_property("persons").contains_any([filters["person"]])
                )
            if filter_parts:
                weaviate_filter = filter_parts[0]
                for f in filter_parts[1:]:
                    weaviate_filter = weaviate_filter & f

        response = collection.query.hybrid(
            query=question,
            limit=limit,
            alpha=alpha,
            filters=weaviate_filter,
            return_properties=[
                "text", "doc_id", "filename", "section",
                "document_type", "chunk_index", "document_summary",
                "date_range", "persons", "organizations", "locations",
                "dates", "financial_amounts", "cryptonyms", "laws", "events",
                "has_redactions", "redaction_density",
            ],
            return_metadata=wvc.query.MetadataQuery(score=True, explain_score=True),
        )

        results = []
        for obj in response.objects:
            p = obj.properties
            score = obj.metadata.score if obj.metadata else 0.0
            results.append({
                "doc_id":           p.get("doc_id", ""),
                "filename":         p.get("filename", ""),
                "section":          p.get("section", ""),
                "document_type":    p.get("document_type", ""),
                "chunk_index":      p.get("chunk_index", 0),
                "document_summary": p.get("document_summary", ""),
                "date_range":       p.get("date_range", ""),
                "text":             p.get("text", ""),
                "persons":          p.get("persons", []),
                "organizations":    p.get("organizations", []),
                "locations":        p.get("locations", []),
                "dates":            p.get("dates", []),
                "financial_amounts": p.get("financial_amounts", []),
                "cryptonyms":       p.get("cryptonyms", []),
                "laws":             p.get("laws", []),
                "events":           p.get("events", []),
                "has_redactions":   p.get("has_redactions", False),
                "redaction_density": p.get("redaction_density", 0.0),
                "hybrid_score":     round(float(score), 4) if score else 0.0,
                "search_method":    "weaviate_hybrid",
            })

        return results

    except Exception as e:
        log.error("Weaviate hybrid search failed: %s", e)
        return []


# ── MongoDB fallback search ───────────────────────────────────────────────────
def mongodb_query(db: Database, claude_client, question: str) -> list[dict]:
    """
    Original MongoDB-based search from swarm.py DocumentQueryAgent.
    Used as a fallback when Weaviate is unavailable, and as a complementary
    structured query pass when Weaviate is available.
    """
    schema_info = """
    MongoDB collections:
    - documents: {doc_id, filename, section, document_type, document_summary, date_range,
                  extracted_entities: {people: [{name, role, frequency}],
                                      organizations: [{name, type, context}],
                                      locations: [{name, type, context}],
                                      dates: [{date, event}],
                                      financial_amounts: [{amount, context}],
                                      case_numbers: [],
                                      key_relationships: [{person1, person2, relationship}]},
                  redaction_analysis: {has_redactions, redaction_density},
                  text_length, page_count}
    - entities: {name, name_lower, entity_type, source_doc_id, section, context}
    - canonical_entities: {canonical_name, entity_type, variants, total_doc_count}
    - network: {person1, person2, weight, shared_doc_ids}
    - gliner_entities: {doc_id, cryptonyms, laws, events, extracted_at}
    """

    translate_prompt = f"""Translate this research question into MongoDB queries.
Return a JSON object with:
{{
  "strategy": "brief description of query approach",
  "queries": [
    {{
      "collection": "collection_name",
      "operation": "find" or "aggregate",
      "filter": {{...}} or pipeline [...],
      "projection": {{...}},
      "limit": N,
      "description": "what this query finds"
    }}
  ]
}}

{schema_info}

QUESTION: {question}

Return valid JSON only. Use MongoDB query syntax. Be precise with field paths.
Focus on structured filters (section, document_type, date ranges, entity names).
"""

    try:
        message = claude_client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": translate_prompt}],
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        query_plan = json.loads(text)
    except Exception as e:
        log.error("MongoDB query translation failed: %s", e)
        return []

    all_results = []
    for q in query_plan.get("queries", []):
        collection = q.get("collection", "documents")
        coll = db[collection]
        try:
            if q.get("operation") == "aggregate":
                pipeline = q.get("filter", q.get("pipeline", []))
                if not isinstance(pipeline, list):
                    continue
                docs = list(coll.aggregate(pipeline, allowDiskUse=True))
            else:
                filter_q = q.get("filter", {})
                projection = q.get("projection")
                limit = q.get("limit", 20)
                cursor = coll.find(filter_q, projection).limit(limit)
                docs = list(cursor)

            for d in docs:
                if "_id" in d:
                    d["_id"] = str(d["_id"])
                d["search_method"] = "mongodb_structured"
                d["query_description"] = q.get("description", "")

            all_results.extend(docs[:20])
        except Exception as e:
            log.warning("MongoDB query failed: %s", e)

    return all_results


# ── Main agent class ──────────────────────────────────────────────────────────
class DocumentQueryAgentV2:
    """
    Upgraded DocumentQuery agent with Weaviate hybrid search.

    Integrates seamlessly with the existing swarm.py Coordinator —
    the interface (run method, AgentResult) is identical to the original.

    To use in swarm.py, replace:
        from swarm import DocumentQueryAgent
    with:
        from document_query_weaviate import DocumentQueryAgentV2 as DocumentQueryAgent
    """

    name = "document_query"
    description = ("Natural language queries against the document corpus — "
                   "hybrid semantic + structured search via Weaviate + MongoDB")
    requires_claude = True

    def __init__(self, db: Database, claude_client):
        self.db = db
        self.claude = claude_client
        self._weaviate_client = _get_weaviate_client()
        self._weaviate_enabled = self._weaviate_client is not None

        if self._weaviate_enabled:
            log.info("DocumentQueryAgentV2: Weaviate hybrid search ENABLED")
        else:
            log.info("DocumentQueryAgentV2: Weaviate unavailable — MongoDB-only mode")

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        question = task.get("question", "")
        if not question:
            result.error = "No question provided"
            return result

        # Extract optional structured filters from task
        filters = {
            "section":       task.get("section"),
            "document_type": task.get("document_type"),
            "has_redactions": task.get("has_redactions"),
            "person":        task.get("person"),
        }
        filters = {k: v for k, v in filters.items() if v is not None}

        # ── Pass 1: Weaviate hybrid search ────────────────────────────────────
        weaviate_results = []
        if self._weaviate_enabled:
            log.info("Running Weaviate hybrid search for: %s", question)
            weaviate_results = weaviate_hybrid_search(
                self._weaviate_client,
                question=question,
                limit=task.get("weaviate_limit", 20),
                alpha=task.get("alpha", 0.5),
                filters=filters if filters else None,
            )
            log.info("Weaviate returned %d chunks", len(weaviate_results))

        # ── Pass 2: MongoDB structured query (always run as complement) ───────
        log.info("Running MongoDB structured query for: %s", question)
        mongodb_results = mongodb_query(self.db, self.claude, question)
        result.api_calls += 1
        log.info("MongoDB returned %d results", len(mongodb_results))

        # ── Merge and deduplicate by doc_id ───────────────────────────────────
        seen_doc_ids = set()
        merged = []

        # Weaviate results first (ranked by hybrid score)
        for r in weaviate_results:
            doc_id = r.get("doc_id", "")
            if doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                merged.append(r)

        # MongoDB results fill in any gaps
        for r in mongodb_results:
            doc_id = str(r.get("doc_id", r.get("_id", "")))
            if doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                merged.append(r)

        log.info("Merged result set: %d unique documents", len(merged))

        # ── Synthesis: Claude combines both result sets ───────────────────────
        search_mode = ("Weaviate hybrid + MongoDB" if self._weaviate_enabled
                       else "MongoDB only")

        synthesis_prompt = f"""You are analyzing DOJ Epstein investigation documents.

ORIGINAL QUESTION: {question}
SEARCH MODE: {search_mode}

SEARCH RESULTS ({len(merged)} unique documents/chunks):
{json.dumps(merged[:30], indent=2, default=str)[:18000]}

Synthesize these results into a clear, evidence-grounded answer. Include:
1. A direct answer to the question
2. Key findings with specific document references (doc_ids, filenames)
3. Notable entities (people, organizations, locations) that appear in the results
4. Any cryptonyms, laws, or events found (if present in results)
5. Confidence level and what limits it
6. Open questions the data cannot answer

Return JSON:
{{
  "answer": "clear, direct answer to the question",
  "key_findings": [
    {{
      "finding": "specific finding",
      "doc_ids": ["doc1", "doc2"],
      "entities": {{"persons": [], "organizations": [], "locations": []}},
      "confidence": 0.9,
      "search_method": "weaviate_hybrid or mongodb_structured"
    }}
  ],
  "notable_entities": {{
    "persons": [],
    "organizations": [],
    "locations": [],
    "cryptonyms": [],
    "laws": []
  }},
  "open_questions": ["what still needs investigation"],
  "overall_confidence": 0.8,
  "search_coverage": "brief note on how well the search covered the question"
}}
"""

        try:
            message = self.claude.messages.create(
                model=MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": synthesis_prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            synthesis = json.loads(text)
            result.api_calls += 1
        except Exception as e:
            result.findings = merged[:10]
            result.error = f"Synthesis failed: {e}"
            result.duration_seconds = time.time() - start
            return result

        result.findings = [
            {
                "type": "answer",
                "content": synthesis.get("answer", ""),
                "search_mode": search_mode,
            }
        ] + synthesis.get("key_findings", [])

        result.confidence = synthesis.get("overall_confidence", 0.5)
        result.open_questions = synthesis.get("open_questions", [])

        # Build evidence chain
        for finding in synthesis.get("key_findings", []):
            for doc_id in finding.get("doc_ids", []):
                result.evidence.append({
                    "doc_id": doc_id,
                    "relevance": finding.get("finding", ""),
                    "search_method": finding.get("search_method", "unknown"),
                })

        result.duration_seconds = time.time() - start
        return result

    def close(self):
        """Release Weaviate connection."""
        if self._weaviate_client:
            try:
                self._weaviate_client.close()
            except Exception:
                pass


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="E-FINDER Weaviate-powered document query agent"
    )
    parser.add_argument("--question", "-q", required=True,
                        help="Research question to answer")
    parser.add_argument("--alpha", type=float, default=0.5,
                        help="Weaviate hybrid alpha (0=BM25, 1=vector, default 0.5)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Number of Weaviate results to retrieve")
    parser.add_argument("--section", help="Filter by DOJ section")
    parser.add_argument("--doc-type", help="Filter by document type")
    args = parser.parse_args()

    if not MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        sys.exit(1)

    mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    db = mongo[DATABASE_NAME]
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    task = {
        "question": args.question,
        "alpha": args.alpha,
        "weaviate_limit": args.limit,
    }
    if args.section:
        task["section"] = args.section
    if args.doc_type:
        task["document_type"] = args.doc_type

    agent = DocumentQueryAgentV2(db, claude)
    result = agent.run(task)
    agent.close()

    print(f"\n{'='*60}")
    print(f"  DOCUMENT QUERY RESULTS")
    print(f"{'='*60}")
    print(json.dumps(result.findings, indent=2, default=str)[:5000])
    if result.open_questions:
        print(f"\n  Open questions:")
        for q in result.open_questions:
            print(f"    ? {q}")
    print(f"\n  Confidence: {result.confidence:.2f}")
    print(f"  Duration: {result.duration_seconds:.1f}s")
    print(f"  API calls: {result.api_calls}")


if __name__ == "__main__":
    main()
