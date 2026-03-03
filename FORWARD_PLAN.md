# DOJ Epstein Document Analysis Pipeline — Forward Plan

**Last updated:** March 2, 2026
**Status:** Phase 3 complete. Phase 4 (swarm) complete. Phase 5 (IntellYWeave) complete. **Phase 6 (Docker deployment) complete.**

---

## What We Have

### Completed ✓

- **26,140 PDFs downloaded** across 52 sections (12 Data Sets + 34 court cases + 4 FOIA + 2 prior disclosures)
- **Integrity verified** — zero invalid headers, zero duplicates by SHA-256 hash
- **MongoDB Atlas** set up — connection configured via `MONGODB_URI` env var (see `.env`)
- **26,138 documents ingested** into `documents` collection with metadata (page counts, hashes, sections)
- **Entity extraction pipeline** tested on 50 docs (48/50 successful), upgraded with entity ingestion + progress logging
- **Full extraction running** on Hetzner VPS — 26K docs via Claude Sonnet, ~6-7 day ETA, ~$150 API cost
- **Swarm architecture designed** — see `SWARM_ARCHITECTURE.md`
- **Entity resolver built** — fuzzy + exact deduplication, optional Claude disambiguation
- **Swarm coordinator + 4 agents** built — network mapper, document query, timeline builder, redaction analyst

### In Progress

- **VPS entity extraction** — running in tmux session `efinder`, monitor via `tail -f extraction_progress.log`

---

## Infrastructure

| Component | Location | Details |
|-----------|----------|---------|
| PDF Library | VPS: `doj_full_library/` | 26,140 files, 16GB |
| PDF Library (backup) | Mac: ProtonDrive backup | Same files |
| MongoDB Atlas | See `.env` | Set via `MONGODB_URI` env var |
| Weaviate | Docker: `efinder-weaviate` | Vector DB, port 8080, data in `weaviate_data` volume |
| Dashboard | Docker: `efinder-dashboard` | Flask + React UI, port 5000 |
| Swarm / pipeline tools | Docker: one-shot containers | `make swarm`, `make extract`, etc. |
| VPS (Hetzner) | See `.env` | Access via Tailscale |
| E-FINDER workspace | VPS: own venv + `.env` | Separate from other projects |

---

## MongoDB Collections

| Collection | Status | Records | Purpose |
|-----------|--------|---------|---------|
| `documents` | ✓ Active | 26,138 | Document metadata + extracted entities |
| `entities` | ✓ Being populated | Growing | Flat entity records for cross-doc queries |
| `canonical_entities` | ○ Ready | Empty | Deduplicated entities (run entity_resolver.py) |
| `network` | ○ Ready | Empty | Co-occurrence graph edges (run swarm.py --agent network_mapper) |
| `reports` | ○ Ready | Empty | Investigation reports from swarm |
| `redactions` | ○ Schema ready | Empty | Detailed redaction analysis |
| `gliner_entities` | ○ Ready | Empty | GLiNER-extracted cryptonyms, laws, events (run gliner_reextract.py) |
| `location_intelligence` | ○ Ready | Empty | Geocoded location records (run geospatial_agent.py) |

---

## Phase 4: OSINT Agent Swarm

### Step 1 — Entity Resolution (run after extraction completes)
```bash
cd ~/efinder
source .venv/bin/activate
export $(cat .env | xargs)
python3 _pipeline_output/entity_resolver.py --dry-run          # Preview
python3 _pipeline_output/entity_resolver.py --use-claude       # Full resolution
```

### Step 2 — Network Mapping
```bash
python3 _pipeline_output/swarm.py --agent network_mapper
```

### Step 3 — Interactive Investigation
```bash
python3 _pipeline_output/swarm.py --interactive
# Or single question:
python3 _pipeline_output/swarm.py -q "Who are the most connected people in the corpus?"
```

### Step 4 — Specific Agent Runs
```bash
# Timeline for a person
python3 _pipeline_output/swarm.py --agent timeline_builder --question "Jeffrey Epstein"

# Redaction analysis
python3 _pipeline_output/swarm.py --agent redaction_analyst

# Document search
python3 _pipeline_output/swarm.py --agent document_query -q "Find all financial records mentioning Deutsche Bank"
```

---

## Agents — Current Roster

### Built ✓ (Original)
| Agent | Type | Purpose |
|-------|------|---------|
| Entity Resolver | Batch | Dedup "Jeffrey Epstein" / "EPSTEIN, JEFFREY" / "J. Epstein" → canonical entities |
| Network Mapper | Corpus | Build co-occurrence graph, compute centrality, detect communities |
| Document Query | Corpus | Natural language search + synthesis across 26K documents |
| Timeline Builder | Analysis | Chronological reconstruction for any person or topic |
| Redaction Analyst | Analysis | Systematic analysis of what's being redacted and why |
| Coordinator | Orchestration | Routes questions to agents, synthesizes findings, stores reports |

### Built ✓ (IntellYWeave Integration — Phase 5)
| Agent / Module | Type | Source | Purpose |
|----------------|------|--------|---------|
| `DocumentQueryAgentV2` | Search | IntellYWeave (adapted) | Weaviate hybrid (BM25 + vector) + MongoDB fallback. Auto-replaces DocumentQueryAgent when Weaviate is available |
| `GLiNER Extractor` | Batch NER | IntellYWeave (adapted) | Zero-shot extraction of cryptonyms, laws, events, FOIA codes — no API cost |
| `Courthouse Debate` | Validation | IntellYWeave (adapted) | Prosecution → Defense → Judge adversarial validation of every finding before it enters a report |
| `Intelligence Orchestrator` | Analysis | IntellYWeave (adapted) | 6-phase analysis: Extract → Map → Geospatial → Network → Patterns → Synthesize |
| `Geospatial Analyst` | Analysis | IntellYWeave (adapted) | Location frequency, geocoding, co-occurrence with entities, GeoJSON export for Mapbox GL |
| `Weaviate Setup` | Infrastructure | IntellYWeave (adapted) | MongoDB → Weaviate migration, schema creation, hybrid search testing |

### Planned (Future)
| Agent | Type | Purpose |
|-------|------|---------|
| Link Analyst | Analysis | Find hidden connections through shared entities |
| Pattern Detector | Analysis | Recurring patterns (financial, temporal, communication) |
| Anomaly Flagger | Analysis | Things that don't fit expected patterns |
| Gap Analyst | Analysis | What's missing from the corpus |
| Corporate Records | OSINT | SEC EDGAR, state SOS corporate filings |
| News Archive | OSINT | Contemporaneous reporting from news archives |
| Court Records | OSINT | PACER, state court cross-reference |
| Flight Records | OSINT | FAA tail number lookups, airport records |

---

## File Inventory

| File | Purpose | Status |
|------|---------|--------|
| `FORWARD_PLAN.md` | This document | Updated |
| `SWARM_ARCHITECTURE.md` | Full swarm design document | Complete |
| `extract_entities.py` | PDF → text → Claude → MongoDB pipeline (v2) | Running on VPS |
| `entity_resolver.py` | Deduplicate entities collection | Ready |
| `swarm.py` | Coordinator + 6 agents (IntellYWeave-upgraded) | Updated |
| `weaviate_setup.py` | Weaviate schema + MongoDB → Weaviate migration | **New (IntellYWeave)** |
| `document_query_weaviate.py` | Upgraded DocumentQuery with hybrid search | **New (IntellYWeave)** |
| `gliner_reextract.py` | GLiNER secondary NER pass (cryptonyms, laws, events) | **New (IntellYWeave)** |
| `courthouse_debate.py` | Adversarial finding validation (prosecution/defense/judge) | **New (IntellYWeave)** |
| `intelligence_orchestrator.py` | 6-phase intelligence analysis orchestrator | **New (IntellYWeave)** |
| `geospatial_agent.py` | Location intelligence + geocoding + GeoJSON export | **New (IntellYWeave)** |
| `setup_mongodb.py` | MongoDB collection/index setup | Complete |
| `ingest_metadata.py` | Bulk metadata ingestion | Complete (26,138 docs) |
| `integrity_check.py` | PDF validation + dedup | Complete |
| `download_chunk3.py` | Court/FOIA/disclosure downloader | Complete |
| `download_chunk3_gaps.py` | Gap-fill for 6 large sections | Complete |
| `master_inventory.json` | Full file inventory with hashes | Generated |
| `file_hash_index.json` | Filename → hash mapping | Generated |

---

## Cost Tracking

| Phase | Estimated | Actual |
|-------|-----------|--------|
| Entity extraction (50-doc test) | $0.30 | $0.29 |
| Entity extraction (full 26K) | ~$150 | Running... |
| Entity resolution | ~$15 | Pending |
| Swarm investigations | ~$2-10 each | Pending |
| **Total estimated** | **~$170-200** | |

---

## Phase 6: Docker Deployment

The entire stack is now containerised. No virtual environments, no manual pip installs.

### New files
| File | Purpose |
|------|--------|
| `Dockerfile` | Python 3.11 image for all e-finder services |
| `docker-compose.yml` | Orchestrates Weaviate + dashboard + one-shot tool containers |
| `.env.example` | Template for secrets — copy to `.env` and fill in |
| `.dockerignore` | Keeps the image lean (excludes PDFs, logs, caches) |
| `requirements.txt` | All Python dependencies |
| `Makefile` | Shorthand `make` commands |
| `docker/entrypoint.sh` | Routes container CMD to the correct Python script |

### Quick start
```bash
# 1. Clone the repo and enter it
git clone https://github.com/download-from-mothership/e-finder.git
cd e-finder

# 2. Set up secrets
cp .env.example .env
# Edit .env — fill in MONGODB_URI, ANTHROPIC_API_KEY, OPENAI_API_KEY
# Optionally set PDF_LIBRARY_PATH to your local PDF library path

# 3. Start Weaviate + dashboard
make up
# → Dashboard: http://localhost:5000
# → Weaviate:  http://localhost:8080

# 4. Migrate corpus to Weaviate (first time only)
make migrate-test    # test with 100 docs first
make migrate         # full 26K migration

# 5. Run GLiNER extraction pass (first time only)
make gliner-test     # test with 500 docs
make gliner          # full corpus

# 6. Build the network map
make network-map

# 7. Run an investigation
make swarm Q="Who are the most connected people in the corpus?"
```

### All available commands
```bash
make up              # Start Weaviate + dashboard (detached)
make down            # Stop all services
make build           # Rebuild the app image
make logs            # Tail all service logs
make ps              # Show running containers

make migrate-test    # Migrate 100 docs → Weaviate (test)
make migrate         # Migrate full corpus → Weaviate

make gliner-test     # GLiNER NER pass on 500 docs (test)
make gliner          # GLiNER NER pass on full corpus

make extract-test    # Extract entities from 50 docs (test)
make extract         # Extract entities (resume from last position)

make network-map     # Build co-occurrence network in MongoDB
make swarm Q="..."   # Run a swarm investigation question
make shell           # Open a bash shell inside the app container
make clean           # Remove volumes and containers
```

### Running without Make
```bash
# Start everything
docker compose up -d

# Run a swarm question
docker compose run --rm swarm -q "What financial connections appear between Epstein and Deutsche Bank?"

# Run entity extraction (with extra args)
docker compose run --rm extract --resume
docker compose run --rm extract --section DataSet_01 --limit 20

# Migrate with args
docker compose run --rm migrate --setup
docker compose run --rm migrate --migrate --limit 100

# Debug shell
docker compose run --rm --entrypoint bash dashboard
```

## Phase 5: IntellYWeave Integration — Reference

All Phase 5 setup is now handled by the Docker commands above. For reference, the underlying scripts are:

| Script | Docker equivalent |
|--------|------------------|
| `weaviate_setup.py --setup && --migrate` | `make migrate` |
| `gliner_reextract.py --update-weaviate` | `make gliner` |
| `swarm.py -q "..."` | `make swarm Q="..."` |
| `geospatial_agent.py --analyze` | `docker compose run --rm swarm --agent geospatial_analyst` |
| `courthouse_debate.py --finding "..."` | `docker compose run --rm swarm` (auto-runs inside Coordinator) |

---

## Immediate Next Steps (Priority Order)

1. ✅ ~~Monitor VPS extraction progress~~ — Running, check with `tail -f`
2. **When extraction completes:** Run entity resolver (`entity_resolver.py --use-claude`)
3. **Then:** Run network mapper (`swarm.py --agent network_mapper`)
4. **Then:** Set up Weaviate + migrate corpus (Phase 5, Steps 1–2 above)
5. **Then:** Run GLiNER extraction pass (Phase 5, Step 3 above)
6. **Then:** Start interactive investigation (`swarm.py --interactive`)
7. **Future:** Dashboard visualization — integrate Mapbox GL with `location_intelligence.geojson`
