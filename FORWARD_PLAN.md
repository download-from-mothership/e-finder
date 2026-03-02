# DOJ Epstein Document Analysis Pipeline — Forward Plan

**Last updated:** March 1, 2026
**Status:** Phase 3 complete. Phase 4 (swarm) designed and built. Extraction running on VPS.

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

### Built ✓
| Agent | Type | Purpose |
|-------|------|---------|
| Entity Resolver | Batch | Dedup "Jeffrey Epstein" / "EPSTEIN, JEFFREY" / "J. Epstein" → canonical entities |
| Network Mapper | Corpus | Build co-occurrence graph, compute centrality, detect communities |
| Document Query | Corpus | Natural language search + synthesis across 26K documents |
| Timeline Builder | Analysis | Chronological reconstruction for any person or topic |
| Redaction Analyst | Analysis | Systematic analysis of what's being redacted and why |
| Coordinator | Orchestration | Routes questions to agents, synthesizes findings, stores reports |

### Planned (Phase 4 continued)
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
| `swarm.py` | Coordinator + 4 specialist agents | Ready |
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

## Immediate Next Steps (Priority Order)

1. ✅ ~~Monitor VPS extraction progress~~ — Running, check with `tail -f`
2. **When extraction completes:** Run entity resolver (`entity_resolver.py --use-claude`)
3. **Then:** Run network mapper (`swarm.py --agent network_mapper`)
4. **Then:** Start interactive investigation (`swarm.py --interactive`)
5. **Ongoing:** Build remaining Layer 2 + Layer 3 agents as needed
6. **Future:** Dashboard visualization (entity graph, timeline, redaction heatmap)
