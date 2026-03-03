#!/usr/bin/env python3
"""
E-FINDER — Intelligence Orchestrator
======================================
Adapted from IntellYWeave's 6-phase Intelligence Orchestrator.

For complex research questions that require full-corpus analysis, this
orchestrator runs a structured 6-phase investigation pipeline:

  Phase 1 — Extract    : Identify all relevant entities and documents
  Phase 2 — Map        : Build relationship maps between entities
  Phase 3 — Geospatial : Analyze location patterns (if applicable)
  Phase 4 — Network    : Run network analysis (centrality, communities)
  Phase 5 — Patterns   : Detect temporal, behavioral, and structural patterns
  Phase 6 — Synthesize : Produce a comprehensive intelligence report

Each phase feeds its output into the next, building a progressively richer
picture of the investigation subject.

The orchestrator integrates with:
  - MongoDB (existing e-finder collections)
  - Weaviate (semantic search — if available)
  - NetworkX (graph analysis)
  - CourthouseDebate (adversarial validation of key findings)

Usage (standalone):
  export MONGODB_URI="mongodb+srv://..."
  export ANTHROPIC_API_KEY="sk-ant-..."

  python3 intelligence_orchestrator.py \\
    --question "What is the full network of Jeffrey Epstein's financial connections?"

Integration with swarm.py:
  from intelligence_orchestrator import IntelligenceOrchestratorAgent
  # Add to AGENT_REGISTRY in swarm.py:
  AGENT_REGISTRY["intelligence_orchestrator"] = IntelligenceOrchestratorAgent
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    import networkx as nx
    NX_AVAILABLE = True
except ImportError:
    NX_AVAILABLE = False


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


# ── Phase context (passed between phases) ────────────────────────────────────
@dataclass
class PhaseContext:
    question: str
    subject: str                     # primary subject of investigation
    relevant_doc_ids: list = field(default_factory=list)
    relevant_entities: dict = field(default_factory=dict)  # {type: [names]}
    relationship_map: dict = field(default_factory=dict)
    location_data: list = field(default_factory=list)
    network_metrics: dict = field(default_factory=dict)
    patterns: list = field(default_factory=list)
    phase_results: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Extractor Agent
# ═══════════════════════════════════════════════════════════════════════════════
class ExtractorAgent:
    """
    Phase 1: Identify all relevant entities and documents for the question.
    Uses Weaviate hybrid search (if available) + MongoDB entity lookup.
    """

    def __init__(self, db: Database, claude_client, weaviate_client=None):
        self.db = db
        self.claude = claude_client
        self.weaviate = weaviate_client

    def run(self, ctx: PhaseContext) -> PhaseContext:
        log.info("[Phase 1 — Extractor] Question: %s", ctx.question)

        # Step 1a: Weaviate semantic search for relevant chunks
        relevant_doc_ids = set()
        if self.weaviate:
            try:
                import weaviate.classes as wvc
                collection = self.weaviate.collections.get(COLLECTION_NAME)
                response = collection.query.hybrid(
                    query=ctx.question,
                    limit=50,
                    alpha=0.5,
                    return_properties=["doc_id", "persons", "organizations",
                                       "locations", "cryptonyms", "laws", "events"],
                    return_metadata=wvc.query.MetadataQuery(score=True),
                )
                for obj in response.objects:
                    p = obj.properties
                    relevant_doc_ids.add(p.get("doc_id", ""))
                    for person in p.get("persons", []):
                        ctx.relevant_entities.setdefault("persons", set()).add(person)
                    for org in p.get("organizations", []):
                        ctx.relevant_entities.setdefault("organizations", set()).add(org)
                    for loc in p.get("locations", []):
                        ctx.relevant_entities.setdefault("locations", set()).add(loc)
                    for crypt in p.get("cryptonyms", []):
                        ctx.relevant_entities.setdefault("cryptonyms", set()).add(crypt)
                log.info("[Phase 1] Weaviate found %d relevant doc_ids", len(relevant_doc_ids))
            except Exception as e:
                log.warning("[Phase 1] Weaviate search failed: %s", e)

        # Step 1b: Ask Claude to identify the primary subject and key entities
        subject_prompt = f"""You are analyzing the DOJ Epstein investigation corpus.

QUESTION: {ctx.question}

Based on this question, identify:
1. The primary subject of investigation (person, organization, or topic)
2. Key entities that should be searched for
3. What types of documents are most relevant

Return JSON:
{{
  "primary_subject": "the main entity being investigated",
  "key_persons": ["person1", "person2"],
  "key_organizations": ["org1", "org2"],
  "key_locations": ["location1"],
  "relevant_doc_types": ["deposition", "financial_record", "email"],
  "search_terms": ["term1", "term2", "term3"]
}}"""

        try:
            message = self.claude.messages.create(
                model=MODEL, max_tokens=1024,
                messages=[{"role": "user", "content": subject_prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            subject_data = json.loads(text)
        except Exception as e:
            log.error("[Phase 1] Subject extraction failed: %s", e)
            subject_data = {"primary_subject": ctx.question, "key_persons": [],
                            "key_organizations": [], "key_locations": [],
                            "relevant_doc_types": [], "search_terms": []}

        ctx.subject = subject_data.get("primary_subject", ctx.question)

        # Step 1c: MongoDB entity lookup for all key persons/orgs
        all_search_terms = (
            subject_data.get("key_persons", []) +
            subject_data.get("key_organizations", []) +
            subject_data.get("search_terms", [])
        )

        for term in all_search_terms[:10]:
            pipeline = [
                {"$match": {"name_lower": {"$regex": term.lower()}}},
                {"$group": {"_id": "$source_doc_id"}},
                {"$limit": 100},
            ]
            for r in self.db["entities"].aggregate(pipeline):
                relevant_doc_ids.add(r["_id"])

        ctx.relevant_doc_ids = list(relevant_doc_ids)[:200]

        # Convert sets to lists for serialization
        ctx.relevant_entities = {
            k: sorted(v) if isinstance(v, set) else v
            for k, v in ctx.relevant_entities.items()
        }
        # Merge Claude's extracted entities
        for person in subject_data.get("key_persons", []):
            ctx.relevant_entities.setdefault("persons", [])
            if person not in ctx.relevant_entities["persons"]:
                ctx.relevant_entities["persons"].append(person)
        for org in subject_data.get("key_organizations", []):
            ctx.relevant_entities.setdefault("organizations", [])
            if org not in ctx.relevant_entities["organizations"]:
                ctx.relevant_entities["organizations"].append(org)

        ctx.phase_results["extraction"] = {
            "subject": ctx.subject,
            "relevant_doc_count": len(ctx.relevant_doc_ids),
            "entities_found": {k: len(v) for k, v in ctx.relevant_entities.items()},
            "subject_data": subject_data,
        }

        log.info("[Phase 1] Complete. Subject: %s, Docs: %d, Entities: %s",
                 ctx.subject, len(ctx.relevant_doc_ids),
                 {k: len(v) for k, v in ctx.relevant_entities.items()})
        return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Mapper Agent
# ═══════════════════════════════════════════════════════════════════════════════
class MapperAgent:
    """
    Phase 2: Build relationship maps between entities found in Phase 1.
    Uses existing MongoDB network collection + co-occurrence analysis.
    """

    def __init__(self, db: Database, claude_client):
        self.db = db
        self.claude = claude_client

    def run(self, ctx: PhaseContext) -> PhaseContext:
        log.info("[Phase 2 — Mapper] Building relationship map for %s", ctx.subject)

        key_persons = ctx.relevant_entities.get("persons", [])[:20]

        # Get existing network edges for these persons
        network_edges = []
        if key_persons:
            edges = list(self.db["network"].find({
                "$or": [
                    {"person1": {"$in": key_persons}},
                    {"person2": {"$in": key_persons}},
                ]
            }, {"person1": 1, "person2": 1, "weight": 1, "shared_doc_ids": 1}).limit(500))

            for edge in edges:
                network_edges.append({
                    "person1": edge["person1"],
                    "person2": edge["person2"],
                    "weight": edge["weight"],
                    "shared_docs": len(edge.get("shared_doc_ids", [])),
                })

        # Get key relationships from documents
        key_relationships = []
        if ctx.relevant_doc_ids:
            docs = list(self.db["documents"].find(
                {"doc_id": {"$in": ctx.relevant_doc_ids[:50]}},
                {"doc_id": 1, "extracted_entities.key_relationships": 1,
                 "document_summary": 1}
            ))
            for doc in docs:
                for rel in doc.get("extracted_entities", {}).get("key_relationships", [])[:5]:
                    key_relationships.append({
                        "doc_id": doc["doc_id"],
                        "person1": rel.get("person1", ""),
                        "person2": rel.get("person2", ""),
                        "relationship": rel.get("relationship", ""),
                    })

        ctx.relationship_map = {
            "network_edges": network_edges[:100],
            "key_relationships": key_relationships[:50],
            "total_edges": len(network_edges),
        }

        ctx.phase_results["mapping"] = {
            "network_edges_found": len(network_edges),
            "key_relationships_found": len(key_relationships),
            "top_connections": [
                f"{e['person1']} ↔ {e['person2']} ({e['weight']} docs)"
                for e in sorted(network_edges, key=lambda x: x["weight"], reverse=True)[:10]
            ],
        }

        log.info("[Phase 2] Complete. Edges: %d, Relationships: %d",
                 len(network_edges), len(key_relationships))
        return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Geospatial Agent
# ═══════════════════════════════════════════════════════════════════════════════
class GeospatialAgent:
    """
    Phase 3: Analyze location patterns in the relevant documents.
    Extracts locations, frequency, and co-occurrence with key entities.
    """

    def __init__(self, db: Database, claude_client):
        self.db = db
        self.claude = claude_client

    def run(self, ctx: PhaseContext) -> PhaseContext:
        log.info("[Phase 3 — Geospatial] Analyzing locations for %s", ctx.subject)

        # Get location entities from relevant documents
        location_pipeline = [
            {"$match": {
                "entity_type": "location",
                "source_doc_id": {"$in": ctx.relevant_doc_ids[:200]},
            }},
            {"$group": {
                "_id": "$name",
                "count": {"$sum": 1},
                "doc_ids": {"$addToSet": "$source_doc_id"},
                "contexts": {"$push": "$context"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 50},
        ]

        locations = list(self.db["entities"].aggregate(location_pipeline))

        # Also get GLiNER-extracted locations if available
        gliner_locations = []
        if ctx.relevant_doc_ids:
            gliner_docs = list(self.db["gliner_entities"].find(
                {"doc_id": {"$in": ctx.relevant_doc_ids[:100]}},
                {"doc_id": 1, "locations": 1}
            ))
            loc_counts = {}
            for doc in gliner_docs:
                for loc in doc.get("locations", []):
                    loc_counts[loc] = loc_counts.get(loc, 0) + 1
            gliner_locations = sorted(loc_counts.items(), key=lambda x: x[1], reverse=True)[:20]

        location_data = []
        for loc in locations:
            location_data.append({
                "name": loc["_id"],
                "frequency": loc["count"],
                "doc_count": len(loc.get("doc_ids", [])),
                "sample_context": (loc.get("contexts") or [""])[:1],
                "source": "claude_extraction",
            })

        for name, count in gliner_locations:
            # Check if already in list
            existing = next((l for l in location_data if l["name"] == name), None)
            if existing:
                existing["gliner_count"] = count
            else:
                location_data.append({
                    "name": name,
                    "frequency": count,
                    "doc_count": count,
                    "source": "gliner_extraction",
                })

        ctx.location_data = location_data

        ctx.phase_results["geospatial"] = {
            "total_locations": len(location_data),
            "top_locations": [
                {"name": l["name"], "frequency": l["frequency"]}
                for l in location_data[:15]
            ],
        }

        log.info("[Phase 3] Complete. Locations found: %d", len(location_data))
        return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Network Agent
# ═══════════════════════════════════════════════════════════════════════════════
class NetworkAgent:
    """
    Phase 4: Run NetworkX graph analysis on the relationship map from Phase 2.
    Computes centrality, community detection, and bridge nodes.
    """

    def __init__(self, db: Database):
        self.db = db

    def run(self, ctx: PhaseContext) -> PhaseContext:
        log.info("[Phase 4 — Network] Running graph analysis")

        if not NX_AVAILABLE:
            log.warning("[Phase 4] NetworkX not available — skipping")
            ctx.phase_results["network"] = {"error": "NetworkX not installed"}
            return ctx

        edges = ctx.relationship_map.get("network_edges", [])
        if not edges:
            log.warning("[Phase 4] No edges to analyze")
            ctx.phase_results["network"] = {"error": "No network edges found"}
            return ctx

        G = nx.Graph()
        for edge in edges:
            G.add_edge(
                edge["person1"], edge["person2"],
                weight=edge["weight"],
            )

        # Centrality metrics
        degree = nx.degree_centrality(G)
        top_degree = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:15]

        if G.number_of_nodes() < 2000:
            betweenness = nx.betweenness_centrality(G, weight="weight")
        else:
            betweenness = nx.betweenness_centrality(G, k=300, weight="weight")
        top_betweenness = sorted(betweenness.items(), key=lambda x: x[1], reverse=True)[:15]

        # Community detection
        communities = []
        try:
            comms = list(nx.community.greedy_modularity_communities(G, weight="weight"))
            for i, comm in enumerate(comms[:8]):
                members = sorted(comm, key=lambda n: degree.get(n, 0), reverse=True)
                communities.append({
                    "id": i,
                    "size": len(comm),
                    "top_members": members[:5],
                    "contains_subject": ctx.subject in comm,
                })
        except Exception as e:
            log.warning("[Phase 4] Community detection failed: %s", e)

        # Subject-specific metrics
        subject_metrics = {}
        if ctx.subject in G:
            subject_metrics = {
                "degree": G.degree(ctx.subject),
                "degree_centrality": round(degree.get(ctx.subject, 0), 4),
                "betweenness_centrality": round(betweenness.get(ctx.subject, 0), 4),
                "neighbors": list(G.neighbors(ctx.subject))[:20],
            }

        ctx.network_metrics = {
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "density": round(nx.density(G), 4),
            "top_degree": [{"name": n, "centrality": round(c, 4)} for n, c in top_degree],
            "top_betweenness": [{"name": n, "centrality": round(c, 4)} for n, c in top_betweenness],
            "communities": communities,
            "subject_metrics": subject_metrics,
        }

        ctx.phase_results["network"] = ctx.network_metrics
        log.info("[Phase 4] Complete. Nodes: %d, Edges: %d, Communities: %d",
                 G.number_of_nodes(), G.number_of_edges(), len(communities))
        return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5 — Pattern Detector Agent
# ═══════════════════════════════════════════════════════════════════════════════
class PatternDetectorAgent:
    """
    Phase 5: Detect temporal, behavioral, and structural patterns across
    all data gathered in Phases 1–4.
    """

    def __init__(self, db: Database, claude_client):
        self.db = db
        self.claude = claude_client

    def run(self, ctx: PhaseContext) -> PhaseContext:
        log.info("[Phase 5 — Pattern Detector] Analyzing patterns for %s", ctx.subject)

        # Get date distribution for relevant documents
        date_pipeline = [
            {"$match": {"doc_id": {"$in": ctx.relevant_doc_ids[:200]}}},
            {"$group": {
                "_id": "$date_range",
                "count": {"$sum": 1},
                "doc_types": {"$addToSet": "$document_type"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 30},
        ]
        date_distribution = list(self.db["documents"].aggregate(date_pipeline))

        # Get redaction patterns for relevant documents
        redaction_pipeline = [
            {"$match": {
                "doc_id": {"$in": ctx.relevant_doc_ids[:200]},
                "redaction_analysis.has_redactions": True,
            }},
            {"$group": {
                "_id": "$section",
                "avg_density": {"$avg": "$redaction_analysis.redaction_density"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"avg_density": -1}},
        ]
        redaction_patterns = list(self.db["documents"].aggregate(redaction_pipeline))

        # Get cryptonyms from GLiNER if available
        cryptonym_pipeline = [
            {"$match": {"doc_id": {"$in": ctx.relevant_doc_ids[:200]}}},
            {"$unwind": "$cryptonyms"},
            {"$group": {"_id": "$cryptonyms", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 20},
        ]
        cryptonyms = list(self.db["gliner_entities"].aggregate(cryptonym_pipeline))

        # Ask Claude to identify patterns
        pattern_data = {
            "subject": ctx.subject,
            "date_distribution": date_distribution[:20],
            "redaction_patterns": redaction_patterns,
            "cryptonyms": cryptonyms,
            "network_communities": ctx.network_metrics.get("communities", []),
            "top_locations": ctx.phase_results.get("geospatial", {}).get("top_locations", []),
            "top_connections": ctx.phase_results.get("mapping", {}).get("top_connections", []),
        }

        pattern_prompt = f"""Analyze these data patterns from the DOJ Epstein investigation.

INVESTIGATION SUBJECT: {ctx.subject}
ORIGINAL QUESTION: {ctx.question}

DATA:
{json.dumps(pattern_data, indent=2, default=str)[:10000]}

Identify significant patterns. Return JSON:
{{
  "temporal_patterns": [
    {{"pattern": "description", "significance": "why it matters", "confidence": 0.8}}
  ],
  "network_patterns": [
    {{"pattern": "description", "significance": "why it matters", "confidence": 0.8}}
  ],
  "redaction_patterns": [
    {{"pattern": "description", "significance": "why it matters", "confidence": 0.8}}
  ],
  "behavioral_patterns": [
    {{"pattern": "description", "significance": "why it matters", "confidence": 0.8}}
  ],
  "anomalies": [
    {{"anomaly": "description", "why_suspicious": "explanation", "confidence": 0.7}}
  ],
  "overall_pattern_summary": "2-3 sentence summary of the most significant patterns"
}}"""

        try:
            message = self.claude.messages.create(
                model=MODEL, max_tokens=3000,
                messages=[{"role": "user", "content": pattern_prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            patterns_data = json.loads(text)
        except Exception as e:
            log.error("[Phase 5] Pattern detection failed: %s", e)
            patterns_data = {"overall_pattern_summary": f"Pattern detection failed: {e}"}

        ctx.patterns = patterns_data
        ctx.phase_results["patterns"] = patterns_data

        log.info("[Phase 5] Complete. Patterns identified.")
        return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6 — Synthesizer Agent
# ═══════════════════════════════════════════════════════════════════════════════
class SynthesizerAgent:
    """
    Phase 6: Synthesize all phase outputs into a comprehensive intelligence
    report. Optionally routes key findings through Courthouse Debate.
    """

    def __init__(self, db: Database, claude_client, use_courthouse: bool = True):
        self.db = db
        self.claude = claude_client
        self.use_courthouse = use_courthouse

    def run(self, ctx: PhaseContext) -> dict:
        log.info("[Phase 6 — Synthesizer] Producing final report")

        synthesis_prompt = f"""You are producing a comprehensive intelligence report for the
DOJ Epstein investigation.

ORIGINAL QUESTION: {ctx.question}
PRIMARY SUBJECT: {ctx.subject}

PHASE RESULTS SUMMARY:

1. EXTRACTION (Phase 1):
{json.dumps(ctx.phase_results.get('extraction', {}), indent=2, default=str)[:2000]}

2. RELATIONSHIP MAPPING (Phase 2):
{json.dumps(ctx.phase_results.get('mapping', {}), indent=2, default=str)[:2000]}

3. GEOSPATIAL (Phase 3):
{json.dumps(ctx.phase_results.get('geospatial', {}), indent=2, default=str)[:1500]}

4. NETWORK ANALYSIS (Phase 4):
{json.dumps(ctx.phase_results.get('network', {}), indent=2, default=str)[:2000]}

5. PATTERNS (Phase 5):
{json.dumps(ctx.phase_results.get('patterns', {}), indent=2, default=str)[:2000]}

Produce a comprehensive intelligence report. Return JSON:
{{
  "executive_summary": "3-5 sentence answer to the original question",
  "key_findings": [
    {{
      "finding": "specific, evidence-grounded finding",
      "supporting_phases": ["extraction", "network"],
      "confidence": 0.9,
      "evidence_doc_ids": [],
      "significance": "why this matters to the investigation"
    }}
  ],
  "entity_profile": {{
    "subject": "{ctx.subject}",
    "key_associates": [],
    "key_organizations": [],
    "key_locations": [],
    "active_period": "date range"
  }},
  "network_assessment": "summary of network position and significance",
  "pattern_assessment": "summary of most significant patterns",
  "redaction_assessment": "what the redaction patterns suggest",
  "contradictions": ["any contradictory findings"],
  "open_questions": ["what still needs investigation"],
  "recommended_next_steps": ["specific next investigation steps"],
  "overall_confidence": 0.8
}}"""

        try:
            message = self.claude.messages.create(
                model=MODEL, max_tokens=5000,
                messages=[{"role": "user", "content": synthesis_prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            report = json.loads(text)
        except Exception as e:
            log.error("[Phase 6] Synthesis failed: %s", e)
            report = {
                "executive_summary": f"Synthesis failed: {e}",
                "key_findings": [],
                "overall_confidence": 0.0,
            }

        # Optional: Route through Courthouse Debate
        if self.use_courthouse and report.get("key_findings"):
            try:
                from courthouse_debate import CourthouseDebate
                log.info("[Phase 6] Running Courthouse Debate on %d findings...",
                         len(report["key_findings"]))
                debate = CourthouseDebate(self.claude)
                report = debate.adjudicate_report(report)
                log.info("[Phase 6] Courthouse complete: %s",
                         report.get("courthouse_summary", {}))
            except ImportError:
                log.warning("[Phase 6] courthouse_debate.py not found — skipping debate")
            except Exception as e:
                log.warning("[Phase 6] Courthouse debate failed: %s", e)

        # Add phase metadata
        report["intelligence_orchestrator_meta"] = {
            "question": ctx.question,
            "subject": ctx.subject,
            "relevant_docs": len(ctx.relevant_doc_ids),
            "phases_completed": list(ctx.phase_results.keys()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        log.info("[Phase 6] Complete. Findings: %d, Confidence: %.2f",
                 len(report.get("key_findings", [])),
                 report.get("overall_confidence", 0))
        return report


# ═══════════════════════════════════════════════════════════════════════════════
# Intelligence Orchestrator — main entry point
# ═══════════════════════════════════════════════════════════════════════════════
class IntelligenceOrchestratorAgent:
    """
    Full 6-phase intelligence orchestrator.
    Implements the same Agent interface as swarm.py agents for drop-in integration.
    """

    name = "intelligence_orchestrator"
    description = ("Full 6-phase intelligence analysis: Extract → Map → Geospatial → "
                   "Network → Patterns → Synthesize. Use for complex questions requiring "
                   "comprehensive corpus analysis.")
    requires_claude = True

    def __init__(self, db: Database, claude_client, weaviate_client=None,
                 use_courthouse: bool = True):
        self.db = db
        self.claude = claude_client
        self.weaviate = weaviate_client
        self.use_courthouse = use_courthouse

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        question = task.get("question", "")
        if not question:
            result.error = "No question provided"
            return result

        ctx = PhaseContext(question=question, subject=question)

        phases = [
            ("Phase 1 — Extract",    lambda: ExtractorAgent(self.db, self.claude, self.weaviate).run(ctx)),
            ("Phase 2 — Map",        lambda: MapperAgent(self.db, self.claude).run(ctx)),
            ("Phase 3 — Geospatial", lambda: GeospatialAgent(self.db, self.claude).run(ctx)),
            ("Phase 4 — Network",    lambda: NetworkAgent(self.db).run(ctx)),
            ("Phase 5 — Patterns",   lambda: PatternDetectorAgent(self.db, self.claude).run(ctx)),
        ]

        for phase_name, phase_fn in phases:
            try:
                log.info("Running %s...", phase_name)
                ctx = phase_fn()
            except Exception as e:
                log.error("%s failed: %s", phase_name, e)
                # Continue with partial context

        # Phase 6: Synthesize
        try:
            synthesizer = SynthesizerAgent(self.db, self.claude, self.use_courthouse)
            report = synthesizer.run(ctx)
        except Exception as e:
            log.error("Phase 6 — Synthesize failed: %s", e)
            report = {"executive_summary": f"Synthesis failed: {e}", "key_findings": []}

        result.findings = [
            {"type": "intelligence_report", "data": report}
        ] + report.get("key_findings", [])[:10]

        result.confidence = report.get("overall_confidence", 0.5)
        result.open_questions = report.get("open_questions", [])
        result.duration_seconds = time.time() - start

        # Estimate API calls: ~2 per phase + 1 for synthesis + courthouse
        result.api_calls = 12
        result.estimated_cost = result.api_calls * 0.006

        return result


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="E-FINDER Intelligence Orchestrator — 6-phase analysis"
    )
    parser.add_argument("--question", "-q", required=True,
                        help="Research question to investigate")
    parser.add_argument("--no-courthouse", action="store_true",
                        help="Skip Courthouse Debate validation")
    args = parser.parse_args()

    if not MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  E-FINDER — Intelligence Orchestrator")
    print(f"{'='*60}\n")
    print(f"  Question: {args.question}\n")

    mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    db = mongo[DATABASE_NAME]
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    agent = IntelligenceOrchestratorAgent(
        db, claude,
        use_courthouse=not args.no_courthouse,
    )
    result = agent.run({"question": args.question})

    print(f"\n{'='*60}")
    print(f"  INTELLIGENCE REPORT")
    print(f"{'='*60}")
    if result.findings:
        report_data = result.findings[0].get("data", {})
        print(f"\n  EXECUTIVE SUMMARY:")
        print(f"  {report_data.get('executive_summary', 'N/A')}")
        print(f"\n  KEY FINDINGS:")
        for f in result.findings[1:6]:
            print(f"    • [{f.get('courthouse_verdict', {}).get('status', 'UNVERIFIED')}] "
                  f"{f.get('finding', '')}")
        if result.open_questions:
            print(f"\n  OPEN QUESTIONS:")
            for q in result.open_questions[:5]:
                print(f"    ? {q}")
    print(f"\n  Confidence: {result.confidence:.2f}")
    print(f"  Duration: {result.duration_seconds:.1f}s")


if __name__ == "__main__":
    main()
