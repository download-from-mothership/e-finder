# E-FINDER

AI-powered investigation pipeline for analyzing the DOJ Epstein document corpus (26,138 documents).

## What It Does

E-FINDER extracts entities, maps relationships, and runs multi-agent investigations across the full corpus of declassified DOJ documents. It uses Claude for entity extraction, MongoDB Atlas for storage, NetworkX for graph analysis, and D3.js for visualization.

## Architecture

```
DOJ PDFs (26K docs)
  → crawl_doj_disclosures.py     # Scrape document URLs from DOJ site
  → download_full_library.py     # Download all PDFs
  → ingest_metadata.py           # Parse PDFs → MongoDB (documents collection)
  → extract_entities.py          # Claude extracts people, orgs, dates → entities collection
  → entity_resolver.py           # Deduplicate entities (fuzzy matching)
  → swarm.py                     # Multi-agent investigation coordinator
  → generate_network_map.py      # D3.js relationship visualization
  → dashboard.py                 # Flask web dashboard
```

## Pipeline Components

### Data Collection
- **crawl_doj_disclosures.py** — Scrapes the DOJ disclosure page index for document URLs
- **download_full_library.py** — Bulk downloads all PDFs with resume capability
- **ingest_metadata.py** — Extracts text from PDFs and stores in MongoDB

### Entity Extraction
- **extract_entities.py** — Uses Claude Sonnet 4.5 to extract structured entities (people, organizations, locations, dates, financial details) from each document. Batched ingestion, progress logging, resume support.

### Analysis
- **entity_resolver.py** — Two-pass entity deduplication: exact key normalization + fuzzy matching via rapidfuzz, with optional Claude disambiguation
- **generate_network_map.py** — Builds a force-directed D3.js graph from the network collection (co-occurrence of people across documents)

### Agent Swarm
- **swarm.py** — Coordinator + 4 specialist agents:
  - **NetworkMapper** — Builds co-occurrence graphs, computes centrality, detects communities
  - **DocumentQuery** — Natural language → MongoDB query translation via Claude
  - **TimelineBuilder** — Extracts chronological events for any subject
  - **RedactionAnalyst** — Analyzes redaction patterns and FOIA exemption codes
- **test_swarm.py** — Integration tests for all agents

### Visualization & Dashboard
- **generate_network_map.py** — Self-contained HTML with interactive D3.js force-directed graph
- **dashboard.py** — Flask app serving live data from MongoDB (stats, network map, reports, entity explorer)
- **dashboard_mockup.jsx** — React mockup of the full dashboard UI
- **start_dashboard.sh** — One-command launcher: generates map, starts server, opens Cloudflare Tunnel for sharing

## Infrastructure

- **VPS**: Hetzner CPX22 (Ubuntu 24.04) — runs extraction and dashboard
- **Database**: MongoDB Atlas — stores documents, entities, network edges, reports
- **AI**: Anthropic Claude Sonnet 4.5 — entity extraction + agent reasoning
- **Visualization**: D3.js v7 force-directed graph

## MongoDB Collections

| Collection | Records | Description |
|---|---|---|
| documents | 26,138 | Full document metadata + extracted text |
| entities | 40,749 | Extracted entities with type, context, section |
| network | 9,852 | Person-to-person co-occurrence edges with weights |
| reports | — | Investigation reports from the swarm |
| canonical_entities | — | Deduplicated entity records (after resolver) |

## Usage

```bash
# On VPS (~/efinder/)
source .venv/bin/activate
export $(cat .env | grep -v '^#' | xargs)

# Run an investigation
python3 _pipeline_output/swarm.py --question "What financial connections appear in the corpus?"

# Interactive mode
python3 _pipeline_output/swarm.py --interactive

# Generate and share the relationship map
bash _pipeline_output/start_dashboard.sh
```

## Corpus Stats

- **26,138** documents analyzed
- **22,298** entities extracted (people)
- **40,749** total entity records
- **9,852** network connections
- **$133.79** extraction cost (Claude API)
- **10.5 hours** extraction time

## Design Documents

- **SWARM_ARCHITECTURE.md** — Full agent swarm design (3-layer architecture)
- **FORWARD_PLAN.md** — Project roadmap and status tracking
