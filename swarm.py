#!/usr/bin/env python3
"""
E-FINDER — OSINT Agent Swarm
==============================
Coordinator + specialist agents for investigating the DOJ Epstein document corpus.

The coordinator takes a research question, routes it to specialist agents,
collects their findings, and synthesizes a report.

Usage:
  export MONGODB_URI="mongodb+srv://user:PASSWORD@your-cluster.mongodb.net/..."
  export ANTHROPIC_API_KEY="sk-ant-..."

  # Interactive mode — ask questions
  python3 _pipeline_output/swarm.py

  # Single question
  python3 _pipeline_output/swarm.py --question "Who are the most connected people in the corpus?"

  # Run a specific agent directly
  python3 _pipeline_output/swarm.py --agent network_mapper
  python3 _pipeline_output/swarm.py --agent document_query --question "Find all financial records mentioning Deutsche Bank"
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

try:
    from pymongo import MongoClient
    from pymongo.database import Database
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient
    from pymongo.database import Database

try:
    import anthropic
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "anthropic", "--break-system-packages", "-q"])
    import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb+srv://user:password@your-cluster.mongodb.net/?retryWrites=true&w=majority",
)
DATABASE_NAME = "doj_investigation"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-5-20250929"


# ═══════════════════════════════════════════
# Agent Interface
# ═══════════════════════════════════════════

@dataclass
class AgentResult:
    agent_name: str
    task: dict
    findings: list = field(default_factory=list)
    evidence: list = field(default_factory=list)  # [{doc_id, excerpt, relevance}]
    confidence: float = 0.0
    open_questions: list = field(default_factory=list)
    duration_seconds: float = 0.0
    api_calls: int = 0
    estimated_cost: float = 0.0
    error: Optional[str] = None


class Agent(ABC):
    """Base class for all swarm agents."""

    name: str = "base_agent"
    description: str = "Base agent"
    requires_claude: bool = False

    def __init__(self, db: Database, claude_client=None):
        self.db = db
        self.claude = claude_client

    @abstractmethod
    def run(self, task: dict) -> AgentResult:
        """Execute the agent's task."""
        ...

    def ask_claude(self, prompt: str, max_tokens: int = 4096) -> str:
        """Helper: send a prompt to Claude and get text back."""
        if not self.claude:
            raise RuntimeError(f"Agent {self.name} requires Claude but no client provided")

        message = self.claude.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()

    def ask_claude_json(self, prompt: str, max_tokens: int = 4096) -> dict:
        """Helper: send a prompt to Claude and parse JSON response."""
        text = self.ask_claude(prompt, max_tokens)
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        return json.loads(text)


# ═══════════════════════════════════════════
# Agent: Network Mapper
# ═══════════════════════════════════════════

class NetworkMapperAgent(Agent):
    name = "network_mapper"
    description = "Build relationship graphs from entity co-occurrence across documents"
    requires_claude = False

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        try:
            import networkx as nx
        except ImportError:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                  "networkx", "--break-system-packages", "-q"])
            import networkx as nx

        target = task.get("target")  # Optional: focus on a specific person
        min_weight = task.get("min_weight", 2)  # Minimum co-occurrence count

        # Build co-occurrence from entities collection
        # Find all person entities, group by document
        pipeline = [
            {"$match": {"entity_type": "person"}},
            {"$group": {
                "_id": "$source_doc_id",
                "people": {"$addToSet": "$name"},
            }},
            {"$match": {"people.1": {"$exists": True}}},  # At least 2 people
        ]

        log.info("Building co-occurrence graph from entities...")
        edges = {}
        doc_count = 0

        for doc in self.db["entities"].aggregate(pipeline, allowDiskUse=True):
            people = sorted(doc["people"])
            doc_id = doc["_id"]
            doc_count += 1

            for i in range(len(people)):
                for j in range(i + 1, len(people)):
                    pair = (people[i], people[j])
                    if pair not in edges:
                        edges[pair] = {"weight": 0, "shared_docs": []}
                    edges[pair]["weight"] += 1
                    if len(edges[pair]["shared_docs"]) < 20:  # Cap stored doc IDs
                        edges[pair]["shared_docs"].append(doc_id)

        log.info("Found %d edges from %d documents", len(edges), doc_count)

        # Filter by minimum weight
        edges = {k: v for k, v in edges.items() if v["weight"] >= min_weight}
        log.info("After filtering (min_weight=%d): %d edges", min_weight, len(edges))

        # Build NetworkX graph
        G = nx.Graph()
        for (p1, p2), data in edges.items():
            G.add_edge(p1, p2, weight=data["weight"], shared_docs=data["shared_docs"])

        if target:
            # Focus on target's neighborhood
            if target in G:
                neighbors = list(G.neighbors(target))
                subgraph = G.subgraph([target] + neighbors)
                log.info("Subgraph for %s: %d nodes, %d edges",
                         target, subgraph.number_of_nodes(), subgraph.number_of_edges())
            else:
                result.error = f"'{target}' not found in network"
                result.duration_seconds = time.time() - start
                return result

            analysis_graph = subgraph
        else:
            analysis_graph = G

        # Compute graph metrics
        log.info("Computing graph metrics...")

        # Degree centrality (most connected)
        degree = nx.degree_centrality(analysis_graph)
        top_degree = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:20]

        # Betweenness centrality (bridges between groups)
        if analysis_graph.number_of_nodes() < 5000:
            betweenness = nx.betweenness_centrality(analysis_graph, weight="weight")
            top_betweenness = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:20]
        else:
            # Approximate for large graphs
            betweenness = nx.betweenness_centrality(analysis_graph, k=500, weight="weight")
            top_betweenness = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:20]

        # Community detection
        try:
            communities = list(nx.community.greedy_modularity_communities(analysis_graph, weight="weight"))
            community_info = []
            for i, comm in enumerate(communities[:10]):
                members = sorted(comm, key=lambda n: degree.get(n, 0), reverse=True)
                community_info.append({
                    "community_id": i,
                    "size": len(comm),
                    "top_members": members[:5],
                })
        except Exception:
            community_info = []

        # Store network in MongoDB
        network_ops = []
        for (p1, p2), data in edges.items():
            network_ops.append({
                "person1": p1,
                "person2": p2,
                "weight": data["weight"],
                "shared_doc_ids": data["shared_docs"],
                "computed_at": datetime.now(timezone.utc),
            })

        if network_ops:
            self.db["network"].drop()
            self.db["network"].insert_many(network_ops)
            self.db["network"].create_index([("person1", 1), ("person2", 1)])
            self.db["network"].create_index("weight", name="idx_weight")
            log.info("Wrote %d edges to network collection", len(network_ops))

        # Build findings
        result.findings = [
            {
                "type": "graph_stats",
                "nodes": analysis_graph.number_of_nodes(),
                "edges": analysis_graph.number_of_edges(),
                "density": round(nx.density(analysis_graph), 4),
            },
            {
                "type": "most_connected",
                "description": "People with the most connections (degree centrality)",
                "entities": [{"name": n, "centrality": round(c, 4)} for n, c in top_degree],
            },
            {
                "type": "bridge_people",
                "description": "People who bridge different groups (betweenness centrality)",
                "entities": [{"name": n, "centrality": round(c, 4)} for n, c in top_betweenness],
            },
        ]

        if community_info:
            result.findings.append({
                "type": "communities",
                "description": "Detected groups of closely connected people",
                "communities": community_info,
            })

        result.confidence = 0.85
        result.duration_seconds = time.time() - start
        return result


# ═══════════════════════════════════════════
# Agent: Document Query
# ═══════════════════════════════════════════

class DocumentQueryAgent(Agent):
    name = "document_query"
    description = "Natural language queries against the document corpus"
    requires_claude = True

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        question = task.get("question", "")
        if not question:
            result.error = "No question provided"
            return result

        # Step 1: Have Claude translate the question into MongoDB queries
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

Return valid JSON only. Use MongoDB query syntax. Be precise with field paths (e.g., "extracted_entities.people.name").
"""

        try:
            query_plan = self.ask_claude_json(translate_prompt)
            result.api_calls += 1
        except Exception as e:
            result.error = f"Failed to translate question: {e}"
            result.duration_seconds = time.time() - start
            return result

        # Step 2: Execute queries
        all_results = []
        for q in query_plan.get("queries", []):
            collection = q.get("collection", "documents")
            coll = self.db[collection]

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

                # Convert ObjectIds to strings
                for d in docs:
                    if "_id" in d:
                        d["_id"] = str(d["_id"])

                all_results.append({
                    "query_description": q.get("description", ""),
                    "collection": collection,
                    "count": len(docs),
                    "results": docs[:20],  # Cap results
                })
            except Exception as e:
                all_results.append({
                    "query_description": q.get("description", ""),
                    "error": str(e),
                })

        # Step 3: Have Claude synthesize the results into an answer
        synthesis_prompt = f"""You are analyzing DOJ Epstein investigation documents.

ORIGINAL QUESTION: {question}

QUERY RESULTS:
{json.dumps(all_results, indent=2, default=str)[:15000]}

Synthesize these results into a clear answer. Include:
1. A direct answer to the question
2. Key findings with specific document references (doc_ids)
3. Confidence level (how well the data answers the question)
4. Open questions (what the data can't answer)

Return JSON:
{{
  "answer": "clear answer to the question",
  "key_findings": [
    {{"finding": "description", "doc_ids": ["doc1", "doc2"], "confidence": 0.9}}
  ],
  "open_questions": ["question 1", "question 2"],
  "overall_confidence": 0.8
}}
"""

        try:
            synthesis = self.ask_claude_json(synthesis_prompt)
            result.api_calls += 1
        except Exception as e:
            # Return raw results if synthesis fails
            result.findings = all_results
            result.error = f"Synthesis failed: {e}"
            result.duration_seconds = time.time() - start
            return result

        result.findings = synthesis.get("key_findings", [])
        result.confidence = synthesis.get("overall_confidence", 0.5)
        result.open_questions = synthesis.get("open_questions", [])

        # Add the answer as the primary finding
        result.findings.insert(0, {
            "type": "answer",
            "content": synthesis.get("answer", ""),
        })

        # Build evidence chain
        for finding in synthesis.get("key_findings", []):
            for doc_id in finding.get("doc_ids", []):
                result.evidence.append({
                    "doc_id": doc_id,
                    "relevance": finding.get("finding", ""),
                })

        result.duration_seconds = time.time() - start
        return result


# ═══════════════════════════════════════════
# Agent: Timeline Builder
# ═══════════════════════════════════════════

class TimelineBuilderAgent(Agent):
    name = "timeline_builder"
    description = "Reconstruct chronological narratives from document dates and events"
    requires_claude = True

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        subject = task.get("subject")  # Person or topic to build timeline for
        if not subject:
            result.error = "No subject provided. Pass {'subject': 'Person Name'}"
            return result

        # Find all documents mentioning this subject
        pipeline = [
            {"$match": {
                "$or": [
                    {"name_lower": subject.lower()},
                    {"name_lower": {"$regex": subject.lower()}},
                ]
            }},
            {"$group": {"_id": "$source_doc_id"}},
        ]
        doc_ids = [r["_id"] for r in self.db["entities"].aggregate(pipeline)]
        log.info("Found %d documents mentioning '%s'", len(doc_ids), subject)

        if not doc_ids:
            result.findings = [{"type": "no_results", "message": f"No documents found for '{subject}'"}]
            result.duration_seconds = time.time() - start
            return result

        # Get document details with dates
        docs = list(self.db["documents"].find(
            {"doc_id": {"$in": doc_ids}},
            {"doc_id": 1, "filename": 1, "section": 1, "document_type": 1,
             "document_summary": 1, "date_range": 1,
             "extracted_entities.dates": 1}
        ))

        # Build date events
        events = []
        for doc in docs:
            # From date_range field
            dr = doc.get("date_range")
            if dr and dr != "null":
                events.append({
                    "date_raw": dr,
                    "doc_id": doc["doc_id"],
                    "doc_type": doc.get("document_type", "unknown"),
                    "summary": doc.get("document_summary", "")[:200],
                    "section": doc.get("section", ""),
                })

            # From extracted dates
            for date_entry in doc.get("extracted_entities", {}).get("dates", []):
                if isinstance(date_entry, dict) and date_entry.get("date"):
                    events.append({
                        "date_raw": date_entry["date"],
                        "event": date_entry.get("event", ""),
                        "doc_id": doc["doc_id"],
                        "doc_type": doc.get("document_type", "unknown"),
                        "section": doc.get("section", ""),
                    })

        log.info("Collected %d date events for '%s'", len(events), subject)

        if not events:
            result.findings = [{"type": "no_dates", "message": f"Documents found but no dates extracted for '{subject}'"}]
            result.duration_seconds = time.time() - start
            return result

        # Have Claude organize the timeline
        timeline_prompt = f"""Build a chronological timeline for "{subject}" from these document references.

EVENTS DATA:
{json.dumps(events[:100], indent=2, default=str)}

Return JSON:
{{
  "subject": "{subject}",
  "timeline": [
    {{"date": "YYYY-MM-DD or partial", "event": "what happened", "doc_ids": ["id1"], "significance": "high/medium/low"}}
  ],
  "date_range": "earliest to latest date",
  "gaps": [
    {{"from": "date", "to": "date", "note": "why this gap matters"}}
  ],
  "patterns": ["any temporal patterns noticed"],
  "total_documents": {len(doc_ids)}
}}

Sort chronologically. Merge duplicate events. Flag significant gaps.
"""

        try:
            timeline = self.ask_claude_json(timeline_prompt)
            result.api_calls += 1
        except Exception as e:
            result.error = f"Timeline synthesis failed: {e}"
            result.findings = [{"type": "raw_events", "events": events[:50]}]
            result.duration_seconds = time.time() - start
            return result

        result.findings = [
            {"type": "timeline", "data": timeline},
        ]
        result.confidence = 0.75
        result.open_questions = timeline.get("gaps", [])
        result.duration_seconds = time.time() - start
        return result


# ═══════════════════════════════════════════
# Agent: Redaction Analyst
# ═══════════════════════════════════════════

class RedactionAnalystAgent(Agent):
    name = "redaction_analyst"
    description = "Analyze redaction patterns across the corpus"
    requires_claude = True

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        focus = task.get("focus")  # Optional: section, person, or doc_type

        # Get redaction statistics
        match_stage = {"redaction_analysis.has_redactions": True}
        if focus:
            match_stage["$or"] = [
                {"section": focus},
                {"document_type": focus},
            ]

        pipeline = [
            {"$match": match_stage},
            {"$group": {
                "_id": "$section",
                "count": {"$sum": 1},
                "avg_density": {"$avg": "$redaction_analysis.redaction_density"},
                "max_density": {"$max": "$redaction_analysis.redaction_density"},
                "doc_types": {"$addToSet": "$document_type"},
            }},
            {"$sort": {"avg_density": -1}},
        ]

        section_stats = list(self.db["documents"].aggregate(pipeline))

        # Get FOIA code distribution
        foia_pipeline = [
            {"$match": {"redaction_analysis.has_redactions": True}},
            {"$project": {"foia_codes": {"$objectToArray": "$redaction_analysis.foia_codes"}}},
            {"$unwind": "$foia_codes"},
            {"$group": {
                "_id": "$foia_codes.k",
                "total_count": {"$sum": "$foia_codes.v"},
                "doc_count": {"$sum": 1},
            }},
            {"$sort": {"total_count": -1}},
        ]
        foia_stats = list(self.db["documents"].aggregate(foia_pipeline))

        # Get most heavily redacted documents
        most_redacted = list(self.db["documents"].find(
            {"redaction_analysis.has_redactions": True},
            {"doc_id": 1, "section": 1, "document_type": 1, "document_summary": 1,
             "redaction_analysis": 1}
        ).sort("redaction_analysis.redaction_density", -1).limit(20))

        # Synthesize with Claude
        synth_data = {
            "section_stats": section_stats,
            "foia_codes": foia_stats,
            "most_redacted_samples": [
                {
                    "doc_id": d["doc_id"],
                    "section": d.get("section"),
                    "type": d.get("document_type"),
                    "density": d.get("redaction_analysis", {}).get("redaction_density"),
                    "summary": d.get("document_summary", "")[:150],
                }
                for d in most_redacted[:10]
            ]
        }

        synth_prompt = f"""Analyze these redaction patterns from DOJ Epstein documents.

DATA:
{json.dumps(synth_data, indent=2, default=str)}

FOIA exemption codes reference:
- (b)(1): Classified national security
- (b)(2): Internal agency rules
- (b)(3): Statutory exemption
- (b)(4): Trade secrets/commercial
- (b)(5): Inter/intra-agency deliberative
- (b)(6): Personal privacy
- (b)(7)(A): Law enforcement - interference with proceedings
- (b)(7)(C): Law enforcement - personal privacy
- (b)(7)(D): Law enforcement - confidential source
- (b)(7)(E): Law enforcement - techniques/procedures

Return JSON:
{{
  "overall_assessment": "summary of redaction patterns",
  "key_patterns": [
    {{"pattern": "description", "significance": "why it matters", "sections_affected": [], "confidence": 0.8}}
  ],
  "foia_analysis": "what the FOIA codes tell us about what's being hidden",
  "anomalies": ["any unusual redaction patterns"],
  "most_significant_redactions": [
    {{"doc_id": "id", "why": "why this redaction matters"}}
  ]
}}
"""

        try:
            synthesis = self.ask_claude_json(synth_prompt)
            result.api_calls += 1
        except Exception as e:
            result.error = f"Synthesis failed: {e}"
            result.findings = [{"type": "raw_stats", "data": synth_data}]
            result.duration_seconds = time.time() - start
            return result

        result.findings = [
            {"type": "redaction_analysis", "data": synthesis},
            {"type": "section_stats", "data": section_stats},
        ]
        result.confidence = 0.8
        result.duration_seconds = time.time() - start
        return result


# ═══════════════════════════════════════════
# Coordinator
# ═══════════════════════════════════════════

# ── IntellYWeave integration: load upgraded agents if available ───────────────
try:
    from document_query_weaviate import DocumentQueryAgentV2
    _DocumentQueryClass = DocumentQueryAgentV2
    log.info("Using DocumentQueryAgentV2 (Weaviate hybrid search)")
except ImportError:
    _DocumentQueryClass = DocumentQueryAgent
    log.info("Using DocumentQueryAgent (MongoDB only)")

try:
    from intelligence_orchestrator import IntelligenceOrchestratorAgent
    _IntelligenceOrchestratorClass = IntelligenceOrchestratorAgent
    log.info("IntelligenceOrchestratorAgent loaded")
except ImportError:
    _IntelligenceOrchestratorClass = None
    log.info("IntelligenceOrchestratorAgent not available")

try:
    from geospatial_agent import GeospatialAnalystAgent
    _GeospatialClass = GeospatialAnalystAgent
    log.info("GeospatialAnalystAgent loaded")
except ImportError:
    _GeospatialClass = None
    log.info("GeospatialAnalystAgent not available")

AGENT_REGISTRY = {
    "network_mapper":    NetworkMapperAgent,
    "document_query":    _DocumentQueryClass,
    "timeline_builder":  TimelineBuilderAgent,
    "redaction_analyst": RedactionAnalystAgent,
}

# Register IntellYWeave agents if available
if _IntelligenceOrchestratorClass:
    AGENT_REGISTRY["intelligence_orchestrator"] = _IntelligenceOrchestratorClass
if _GeospatialClass:
    AGENT_REGISTRY["geospatial_analyst"] = _GeospatialClass

ROUTING_PROMPT = """You are the coordinator of an OSINT investigation swarm analyzing DOJ Epstein documents.

Available agents:
- network_mapper: Build relationship graphs, find most connected people, detect communities. Task: {"target": "optional person name", "min_weight": 2}
- document_query: Search and analyze documents with natural language (Weaviate hybrid + MongoDB). Task: {"question": "the question", "alpha": 0.5}
- timeline_builder: Build chronological timelines for a person/topic. Task: {"subject": "person or topic name"}
- redaction_analyst: Analyze redaction patterns across the corpus. Task: {"focus": "optional section/type"}
- intelligence_orchestrator: Full 6-phase analysis (Extract→Map→Geo→Network→Patterns→Synthesize). Use for complex questions needing comprehensive corpus analysis. Task: {"question": "the question"}
- geospatial_analyst: Analyze location patterns, geocode locations, export GeoJSON for map visualization. Task: {"subject": "optional person", "export_geojson": "path.geojson"}

Given this research question, decide which agents to run and in what order.

Guidelines:
- Use intelligence_orchestrator for broad, complex questions ("full network", "comprehensive analysis", "everything about X")
- Use document_query for targeted questions about specific topics or documents
- Use geospatial_analyst when the question involves locations, travel, or geographic patterns
- Use network_mapper for relationship/connection questions
- Use timeline_builder for chronological questions about a specific person or event
- Use redaction_analyst for questions about what is hidden or classified
- Do NOT use intelligence_orchestrator AND document_query for the same question — they overlap

Return JSON:
{
  "plan": [
    {"agent": "agent_name", "task": {...}, "depends_on": [], "reason": "why this agent"}
  ],
  "synthesis_strategy": "how to combine the results"
}

Keep the plan focused — don't use agents that aren't relevant to the question.

QUESTION: {question}
"""


class Coordinator:
    """Routes questions to specialist agents and synthesizes results."""

    def __init__(self, db: Database, claude_client):
        self.db = db
        self.claude = claude_client

    def investigate(self, question: str) -> dict:
        """Run a full investigation for a research question."""
        start = time.time()

        print(f"\n  {'═'*60}")
        print(f"  INVESTIGATION: {question}")
        print(f"  {'═'*60}\n")

        # Step 1: Plan
        print("  Planning investigation...")
        plan = self._plan(question)

        if not plan or not plan.get("plan"):
            return {"error": "Failed to create investigation plan"}

        steps = plan["plan"]
        print(f"  Plan: {len(steps)} steps")
        for step in steps:
            print(f"    → {step['agent']}: {step.get('reason', '')}")

        # Step 2: Execute agents
        results = {}
        for step in steps:
            agent_name = step["agent"]
            task = step.get("task", {})

            if agent_name not in AGENT_REGISTRY:
                log.warning("Unknown agent: %s", agent_name)
                continue

            # Check dependencies
            deps = step.get("depends_on", [])
            for dep in deps:
                if dep in results and results[dep].error:
                    log.warning("Skipping %s — dependency %s failed", agent_name, dep)
                    continue

            print(f"\n  Running {agent_name}...")
            agent_class = AGENT_REGISTRY[agent_name]
            agent = agent_class(self.db, self.claude)

            try:
                agent_result = agent.run(task)
                results[agent_name] = agent_result
                print(f"    Done ({agent_result.duration_seconds:.1f}s, "
                      f"{agent_result.api_calls} API calls, "
                      f"confidence={agent_result.confidence:.2f})")
            except Exception as e:
                log.error("Agent %s failed: %s", agent_name, e)
                results[agent_name] = AgentResult(
                    agent_name=agent_name, task=task, error=str(e)
                )

        # Step 3: Synthesize
        print(f"\n  Synthesizing findings...")
        report = self._synthesize(question, results, plan.get("synthesis_strategy", ""))

        # Step 4: Courthouse Debate — adversarial validation of key findings
        try:
            from courthouse_debate import CourthouseDebate
            if report.get("key_findings"):
                print(f"\n  Running Courthouse Debate on {len(report['key_findings'])} findings...")
                debate = CourthouseDebate(self.claude)
                # Build evidence map from agent results
                evidence_by_finding = {}
                for f in report.get("key_findings", []):
                    finding_text = f.get("finding", "")
                    evidence = []
                    for agent_name, r in results.items():
                        for ev in (r.evidence or []):
                            evidence.append(ev)
                    evidence_by_finding[finding_text] = evidence[:10]
                report = debate.adjudicate_report(report, evidence_by_finding)
                cs = report.get("courthouse_summary", {})
                print(f"  Courthouse: {cs.get('confirmed',0)} confirmed, "
                      f"{cs.get('contested',0)} contested, "
                      f"{cs.get('insufficient_evidence',0)} insufficient")
        except ImportError:
            pass  # courthouse_debate.py not in path
        except Exception as e:
            log.warning("Courthouse debate failed (non-fatal): %s", e)

        elapsed = time.time() - start
        total_api = sum(r.api_calls for r in results.values()) + 2  # +2 for plan + synthesis
        total_cost = total_api * 0.006

        report["meta"] = {
            "question": question,
            "agents_used": list(results.keys()),
            "total_duration_seconds": round(elapsed, 1),
            "total_api_calls": total_api,
            "estimated_cost": round(total_cost, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Store in reports collection
        self.db["reports"].insert_one(report)

        return report

    def _plan(self, question: str) -> dict:
        prompt = ROUTING_PROMPT.format(question=question)
        try:
            message = self.claude.messages.create(
                model=MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            return json.loads(text)
        except Exception as e:
            log.error("Planning failed: %s", e)
            return None

    def _synthesize(self, question: str, results: dict, strategy: str) -> dict:
        # Prepare results summary for Claude
        results_summary = {}
        for name, r in results.items():
            results_summary[name] = {
                "findings": r.findings[:10],  # Cap for context
                "confidence": r.confidence,
                "open_questions": r.open_questions,
                "error": r.error,
            }

        prompt = f"""Synthesize these agent findings into an investigation report.

ORIGINAL QUESTION: {question}
SYNTHESIS STRATEGY: {strategy}

AGENT RESULTS:
{json.dumps(results_summary, indent=2, default=str)[:20000]}

Return JSON:
{{
  "executive_summary": "2-3 sentence answer to the question",
  "key_findings": [
    {{"finding": "description", "supporting_agents": ["agent1"], "confidence": 0.9, "evidence_doc_ids": []}}
  ],
  "contradictions": ["any contradictory findings between agents"],
  "open_questions": ["what still needs investigation"],
  "recommended_next_steps": ["what to investigate next"],
  "overall_confidence": 0.8
}}
"""

        try:
            message = self.claude.messages.create(
                model=MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            return json.loads(text)
        except Exception as e:
            log.error("Synthesis failed: %s", e)
            return {
                "executive_summary": "Synthesis failed — see raw agent results",
                "raw_results": {k: asdict(v) for k, v in results.items()},
            }


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="E-FINDER OSINT Agent Swarm")
    parser.add_argument("--question", "-q", help="Research question to investigate")
    parser.add_argument("--agent", help="Run a specific agent directly")
    parser.add_argument("--task-json", help="JSON task for direct agent execution")
    parser.add_argument("--interactive", "-i", action="store_true",
                       help="Interactive question mode")
    args = parser.parse_args()

    if "<db_password>" in MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  E-FINDER — OSINT AGENT SWARM")
    print(f"{'='*60}\n")

    client_mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client_mongo.admin.command("ping")
    db = client_mongo[DATABASE_NAME]
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print("  Connected to MongoDB + Claude.\n")

    # Show corpus stats
    doc_count = db["documents"].count_documents({})
    extracted = db["documents"].count_documents({"processing_stage": "entities_extracted"})
    entity_count = db["entities"].count_documents({})
    print(f"  Corpus: {doc_count:,} documents ({extracted:,} extracted)")
    print(f"  Entities: {entity_count:,} raw records\n")

    if args.agent:
        # Direct agent execution
        if args.agent not in AGENT_REGISTRY:
            print(f"Unknown agent: {args.agent}")
            print(f"Available: {', '.join(AGENT_REGISTRY.keys())}")
            sys.exit(1)

        task = json.loads(args.task_json) if args.task_json else {}
        if args.question:
            task["question"] = args.question
            task["subject"] = args.question  # For timeline builder

        agent_class = AGENT_REGISTRY[args.agent]
        agent = agent_class(db, claude)
        result = agent.run(task)

        print(f"\n  Results ({result.duration_seconds:.1f}s, confidence={result.confidence:.2f}):")
        print(json.dumps(result.findings, indent=2, default=str))
        if result.open_questions:
            print(f"\n  Open questions: {result.open_questions}")
        if result.error:
            print(f"\n  Error: {result.error}")
        return

    coordinator = Coordinator(db, claude)

    if args.question:
        report = coordinator.investigate(args.question)
        print(f"\n{'='*60}")
        print(f"  REPORT")
        print(f"{'='*60}")
        print(json.dumps(report, indent=2, default=str)[:5000])
        return

    # Interactive mode
    print("  Enter research questions (type 'quit' to exit):\n")
    while True:
        try:
            question = input("  > ").strip()
            if question.lower() in ("quit", "exit", "q"):
                break
            if not question:
                continue
            report = coordinator.investigate(question)
            print(f"\n  SUMMARY: {report.get('executive_summary', 'No summary')}")
            print(f"  Confidence: {report.get('overall_confidence', 'N/A')}")
            findings = report.get('key_findings', [])
            if findings:
                print(f"\n  KEY FINDINGS:")
                for f in findings[:5]:
                    print(f"    • {f.get('finding', '')}")
            questions = report.get('open_questions', [])
            if questions:
                print(f"\n  OPEN QUESTIONS:")
                for q in questions[:3]:
                    print(f"    ? {q}")
            print()
        except (KeyboardInterrupt, EOFError):
            break

    print("\n  Done.")


if __name__ == "__main__":
    main()
