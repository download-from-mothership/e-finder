# E-FINDER — OSINT Agent Swarm Architecture

**Status:** Design phase
**Foundation:** 26,138 DOJ documents in MongoDB, entity extraction running on VPS

---

## Overview

The swarm is a set of specialist Claude agents orchestrated by a coordinator. Each agent has a narrow focus, queries MongoDB for its inputs, and writes structured findings back. The coordinator takes a research question, routes it to the right agents, collects their findings, and synthesizes a final report with evidence citations.

Everything runs on Python + Claude API + MongoDB. No extra infrastructure.

---

## Architecture

```
                    ┌────────────────────┐
                    │    COORDINATOR     │
                    │   (takes a research │
                    │    question, routes │
                    │    to specialists)  │
                    └─────────┬──────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
    ┌─────▼─────┐      ┌─────▼─────┐      ┌─────▼─────┐
    │  LAYER 1  │      │  LAYER 2  │      │  LAYER 3  │
    │  Corpus   │      │  Analysis │      │  OSINT    │
    │  Agents   │      │  Agents   │      │  Agents   │
    └───────────┘      └───────────┘      └───────────┘
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │     MongoDB        │
                    │  (documents,       │
                    │   entities,        │
                    │   findings)        │
                    └────────────────────┘
```

---

## Layer 1: Corpus Agents (query the documents)

These agents work entirely within the downloaded DOJ corpus via MongoDB.

### 1.1 — Entity Resolver
**Purpose:** Deduplicate and normalize the entities collection.
- "Jeffrey Epstein", "EPSTEIN, JEFFREY", "J. Epstein", "Epstein" → single canonical entity
- Groups variants using fuzzy string matching + co-occurrence signals
- Builds a `canonical_entities` collection with merged records
- Runs once as a batch job after extraction completes, then incrementally

**Why first:** Every other agent depends on clean, deduplicated entities.

### 1.2 — Network Mapper
**Purpose:** Build relationship graphs from entity co-occurrence.
- Queries entities collection: which people appear together across documents?
- Computes co-occurrence scores, weighted by document type and frequency
- Identifies clusters (who are the tight groups?), bridges (who connects groups?), and isolates (who appears alone?)
- Stores edges in a `network` collection: `{person1, person2, weight, shared_docs[], relationship_types[]}`
- Uses NetworkX for graph metrics (betweenness centrality, community detection)

### 1.3 — Timeline Builder
**Purpose:** Reconstruct chronological narratives from scattered document mentions.
- Queries documents collection for date_range and extracted_entities.dates
- Groups events by person, location, or theme
- Identifies temporal clusters (bursts of activity)
- Flags gaps (periods with no documents despite expected continuity)
- Stores timelines in a `timelines` collection

### 1.4 — Redaction Analyst
**Purpose:** Analyze redaction patterns systematically.
- Queries documents with redaction_analysis.has_redactions = true
- Cross-references redaction density with document type, section, date range
- Identifies which categories of information are most heavily redacted
- Flags documents where redaction patterns are inconsistent (e.g., a name redacted in one doc but visible in another)
- Looks for partial redactions that reveal information through context

### 1.5 — Document Query Agent
**Purpose:** Natural language interface to the corpus.
- Takes a plain English question ("What documents mention financial transactions involving Deutsche Bank?")
- Translates to MongoDB queries against documents + entities collections
- Returns ranked results with summaries and citations
- Can do multi-hop queries: "Who traveled with Person X?" → find Person X's travel docs → extract companions → find those companions' other documents

---

## Layer 2: Analysis Agents (synthesize findings)

These agents work on the outputs of Layer 1 to produce higher-level analysis.

### 2.1 — Link Analyst
**Purpose:** Find hidden connections between entities.
- Takes two entities and finds all paths between them in the network graph
- Identifies connections through shared addresses, phone numbers, corporations, travel dates
- Computes connection strength scores
- Flags "suspicious" connections (indirect links through shell companies, intermediaries)

### 2.2 — Pattern Detector
**Purpose:** Find recurring patterns across the corpus.
- Temporal patterns: same events happening on a schedule
- Financial patterns: round-number transactions, escalating amounts, triangular transfers
- Communication patterns: who contacts whom before/after key events
- Redaction patterns: systematic removal of specific categories of information
- Stores patterns in a `patterns` collection with confidence scores

### 2.3 — Anomaly Flagger
**Purpose:** Identify things that don't fit.
- Documents whose metadata doesn't match their content
- People who appear in unexpected contexts
- Dates that don't align with known timelines
- Financial amounts that are outliers for their context
- Stores anomalies in a `findings` collection with severity scores

### 2.4 — Gap Analyst
**Purpose:** Identify what's missing from the corpus.
- Cross-reference known events (from news, court records) against document coverage
- Flag time periods with suspiciously few documents
- Identify people who should appear (based on relationships) but don't
- Note document types that are absent (e.g., tax records, bank statements for known accounts)

---

## Layer 3: OSINT Agents (external corroboration)

These agents reach outside the corpus to corroborate or extend findings.

### 3.1 — Corporate Records Agent
**Purpose:** Look up corporate filings for entities found in documents.
- Query SEC EDGAR for company names and officer names
- Query state SOS databases for LLC/corporate registrations
- Cross-reference officer lists with people in the corpus
- Store findings in `osint_corporate` collection

### 3.2 — Property Records Agent
**Purpose:** Cross-reference property ownership.
- Look up known addresses from documents in county assessor databases
- Trace ownership chains
- Cross-reference with entity names from corpus
- Store in `osint_property` collection

### 3.3 — News Archive Agent
**Purpose:** Find contemporaneous reporting.
- Search news archives for key names and events
- Find reporting from the time period that documents reference
- Compare document claims against contemporaneous reporting
- Flag discrepancies between documents and public record
- Store in `osint_news` collection

### 3.4 — Court Records Agent
**Purpose:** Cross-reference with PACER and state court systems.
- Look up case numbers found in documents
- Find related cases not in the DOJ release
- Trace the full docket history for key cases
- Store in `osint_court` collection

### 3.5 — Flight Records Agent
**Purpose:** Cross-reference travel claims with FAA records.
- Look up tail numbers mentioned in flight logs
- Cross-reference with FAA registration database
- Check airport records for corroboration
- Store in `osint_flights` collection

---

## Coordinator

The coordinator is the top-level agent. It:

1. **Takes a research question** from the user (e.g., "Map all financial connections between Jeffrey Epstein and Deutsche Bank")
2. **Decomposes the question** into sub-tasks for specialist agents
3. **Routes sub-tasks** to the appropriate layer 1/2/3 agents
4. **Collects findings** from each agent
5. **Synthesizes a report** with:
   - Executive summary (2-3 sentences)
   - Key findings (with confidence scores)
   - Evidence chain (which documents support each finding, with doc_ids)
   - Open questions (what couldn't be answered, what needs more investigation)
   - Contradictions (where evidence conflicts)
6. **Stores the report** in a `reports` collection

### Coordination Logic

The coordinator doesn't just fan out — it chains agents intelligently:

```
Question: "Who facilitated financial transactions for Epstein?"

Step 1: Document Query Agent → find all financial_record documents
Step 2: Entity Resolver → normalize all people/orgs in those docs
Step 3: Network Mapper → find who co-occurs in financial contexts
Step 4: Link Analyst → trace connections between financial entities
Step 5: Corporate Records Agent → verify corporate structures
Step 6: Pattern Detector → find recurring transaction patterns
Step 7: Coordinator synthesizes all findings into a report
```

Each step uses the outputs of previous steps. The coordinator decides which agents to invoke based on what the question requires.

---

## Data Model

### New Collections

```
canonical_entities    — deduplicated entity records
  _id, canonical_name, variants[], entity_type, total_doc_count,
  first_seen, last_seen, key_roles[], merged_from[]

network              — relationship edges
  _id, person1, person2, weight, shared_doc_ids[],
  relationship_types[], first_co_occurrence, last_co_occurrence

timelines            — chronological event sequences
  _id, subject (person/org/location), events[{date, event, doc_id, confidence}],
  gaps[{from, to, expected_activity}]

patterns             — detected patterns
  _id, pattern_type, description, confidence, supporting_docs[],
  entities_involved[], date_range

findings             — anomalies and high-value discoveries
  _id, finding_type (anomaly/lead/contradiction/gap),
  severity (high/medium/low), description, evidence[{doc_id, excerpt}],
  status (new/investigating/confirmed/dismissed)

reports              — synthesized analysis reports
  _id, question, summary, findings[], evidence_chain[],
  open_questions[], agents_used[], created_at, confidence

osint_*              — one collection per OSINT source
  _id, source, query, results, linked_entity, linked_doc_ids[],
  retrieved_at, confidence
```

### Indexes

```
canonical_entities:  canonical_name (unique), variants (array), entity_type
network:            person1+person2 (compound unique), weight (descending)
timelines:          subject, events.date
patterns:           pattern_type, confidence
findings:           finding_type, severity, status
reports:            question (text), created_at
```

---

## Implementation Order

### Phase 1: Foundation (do first, enables everything else)
1. **Entity Resolver** — batch job to deduplicate the entities collection
2. **Network Mapper** — build the co-occurrence graph
3. **Document Query Agent** — natural language queries against MongoDB

### Phase 2: Analysis
4. **Timeline Builder** — chronological reconstruction
5. **Link Analyst** — connection tracing
6. **Redaction Analyst** — systematic redaction analysis

### Phase 3: Intelligence
7. **Pattern Detector** — recurring patterns
8. **Anomaly Flagger** — things that don't fit
9. **Gap Analyst** — what's missing

### Phase 4: External Corroboration
10. **Corporate Records Agent** — SEC EDGAR, SOS databases
11. **News Archive Agent** — contemporaneous reporting
12. **Court Records Agent** — PACER, state courts
13. **Property Records Agent** — county assessors
14. **Flight Records Agent** — FAA records

### Phase 5: Orchestration
15. **Coordinator** — ties it all together with intelligent routing
16. **Report Generator** — produces formatted investigation reports

---

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Agent runtime | Python 3.12 | Already deployed on VPS |
| LLM | Claude Sonnet 4.5 | Best cost/quality for structured extraction |
| Database | MongoDB Atlas | See `.env` for connection details |
| Graph analysis | NetworkX | Lightweight, pure Python, excellent algorithms |
| Web scraping (OSINT) | httpx + BeautifulSoup | Async, handles rate limiting |
| Orchestration | Custom Python coordinator | Simple, no extra infrastructure |
| Progress tracking | MongoDB `agent_runs` collection | Query from anywhere |

---

## Agent Interface Contract

Every agent follows the same interface:

```python
class Agent:
    name: str                    # e.g. "network_mapper"
    description: str             # what this agent does
    input_schema: dict           # what it needs to run
    output_collection: str       # where it writes results

    def run(self, task: dict, db: Database) -> AgentResult:
        """Execute the agent's task. Returns structured findings."""
        ...

class AgentResult:
    agent_name: str
    task: dict
    findings: list[dict]         # structured findings
    evidence: list[dict]         # {doc_id, excerpt, relevance}
    confidence: float            # 0.0–1.0
    open_questions: list[str]    # what couldn't be resolved
    duration_seconds: float
    api_calls: int
    estimated_cost: float
```

---

## Cost Estimates

| Agent | Per-run cost | Frequency |
|-------|-------------|-----------|
| Entity Resolver | ~$15 (one-time batch over 26K entities) | Once |
| Network Mapper | ~$0 (MongoDB aggregation + NetworkX) | Once + incremental |
| Document Query | ~$0.01–0.05 per query | On demand |
| Timeline Builder | ~$5 (batch over date entities) | Once |
| Analysis agents | ~$0.05–0.50 per investigation | On demand |
| OSINT agents | ~$0.01–0.10 per lookup | On demand |
| Full investigation | ~$2–10 per research question | On demand |

Total setup cost (phases 1–3): ~$25–50
Per-investigation cost: ~$2–10

---

## Security & Ethics Notes

- All OSINT queries use only publicly available data sources
- No scraping of private or access-restricted databases
- Rate limiting on all external queries (respect robots.txt, API limits)
- All findings stored with provenance (which agent, which sources, when)
- Confidence scores on everything — no presenting speculation as fact
- Human review required before any findings are shared externally
