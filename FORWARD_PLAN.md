# DOJ Epstein Document Analysis Pipeline — Forward Plan

**Last updated:** March 2, 2026
**Status:** Phase 3 complete. Phase 4 (swarm) complete. **IntellYWeave integration complete (Phase 5).**

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
| Extraction pipeline | VPS: `extract_entities.py` | Running in tmux |
| Swarm scripts | Mac: local workspace | Ready to deploy |
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

## Phase 5: IntellYWeave Integration — Setup Steps

Run these **after** entity extraction completes, in order:

### Step 1 — Start Weaviate (Docker required)
```bash
docker run -d -p 8080:8080 -p 50051:50051 \
  -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true \
  -e PERSISTENCE_DATA_PATH=/var/lib/weaviate \
  -v weaviate_data:/var/lib/weaviate \
  cr.weaviate.io/semitechnologies/weaviate:latest
```

### Step 2 — Migrate MongoDB → Weaviate
```bash
export OPENAI_API_KEY="sk-..."
export WEAVIATE_URL="http://localhost:8080"
python3 weaviate_setup.py --setup           # Create schema
python3 weaviate_setup.py --migrate --limit 100  # Test with 100 docs
python3 weaviate_setup.py --migrate         # Full migration (26K docs)
python3 weaviate_setup.py --stats           # Verify
```

### Step 3 — Run GLiNER extraction
```bash
pip install gliner
python3 gliner_reextract.py --dry-run --limit 5   # Preview
python3 gliner_reextract.py --limit 500           # Test batch
python3 gliner_reextract.py --update-weaviate     # Full run + update Weaviate
python3 gliner_reextract.py --stats               # Show cryptonyms/laws/events found
```

### Step 4 — Test upgraded swarm
```bash
# Document query now uses Weaviate hybrid search automatically
python3 swarm.py -q "What financial connections appear between Epstein and Deutsche Bank?"

# Geospatial analysis
python3 swarm.py --agent geospatial_analyst

# Full 6-phase intelligence orchestrator
python3 swarm.py -q "Provide a comprehensive analysis of Jeffrey Epstein's network"

# Courthouse debate standalone test
python3 courthouse_debate.py \
  --finding "Epstein had financial ties to Deutsche Bank" \
  --evidence '[{"doc_id": "doc123", "relevance": "wire transfer records"}]'
```

---

## Immediate Next Steps (Priority Order)

1. ✅ ~~Monitor VPS extraction progress~~ — Running, check with `tail -f`
2. **When extraction completes:** Run entity resolver (`entity_resolver.py --use-claude`)
3. **Then:** Run network mapper (`swarm.py --agent network_mapper`)
4. **Then:** Set up Weaviate + migrate corpus (Phase 5, Steps 1–2 above)
5. **Then:** Run GLiNER extraction pass (Phase 5, Step 3 above)
6. **Then:** Start interactive investigation (`swarm.py --interactive`)
7. **Future:** Dashboard visualization — integrate Mapbox GL with `location_intelligence.geojson`
