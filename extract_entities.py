#!/usr/bin/env python3
"""
DOJ Library — Text Extraction + Claude Entity Extraction Pipeline
===================================================================
Phase B: Extract text from PDFs with PyMuPDF, then use Claude to identify
entities, relationships, document types, and redaction patterns.

Results are upserted into MongoDB (both `documents` and `entities` collections).

Usage:
  # Test on 50 docs
  export MONGODB_URI="mongodb+srv://efinder-db:PASSWORD@e-cluster0.ulpu7g.mongodb.net/..."
  export ANTHROPIC_API_KEY="sk-ant-..."
  python3 _pipeline_output/extract_entities.py --limit 50

  # Full run with resume (recommended for VPS)
  python3 _pipeline_output/extract_entities.py --resume

  # Single section
  python3 _pipeline_output/extract_entities.py --section DataSet_01 --limit 20

  # Monitor remotely (from another terminal)
  tail -f _pipeline_output/extraction_progress.log
"""

import argparse
import json
import logging
import os
import sys
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Installing PyMuPDF...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "PyMuPDF", "--break-system-packages", "-q"])
    import fitz

try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import (
        ConnectionFailure, ServerSelectionTimeoutError, BulkWriteError
    )
except ImportError:
    print("Installing pymongo...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import (
        ConnectionFailure, ServerSelectionTimeoutError, BulkWriteError
    )

try:
    import anthropic
except ImportError:
    print("Installing anthropic SDK...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "anthropic", "--break-system-packages", "-q"])
    import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════

MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb+srv://efinder-db:<db_password>@e-cluster0.ulpu7g.mongodb.net/?retryWrites=true&w=majority&appName=e-Cluster0",
)
DATABASE_NAME = "doj_investigation"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Text extraction limits
MAX_CHARS_PER_DOC = 80000      # ~20K tokens — fits in Claude's context with room for the prompt
MAX_PAGES_TO_EXTRACT = 200     # Skip remaining pages for massive docs
CHARS_PER_PAGE_SAMPLE = 2000   # For docs over MAX_CHARS, sample this much per page

# Claude API settings
MODEL = "claude-sonnet-4-5-20250929"
MAX_TOKENS = 4096

# Entity ingestion
ENTITY_BATCH_SIZE = 200        # Flush entity records to MongoDB every N docs

# Progress log
PROGRESS_LOG_FILE = "_pipeline_output/extraction_progress.log"

# ═══════════════════════════════════════════
# Progress Logger (for remote monitoring)
# ═══════════════════════════════════════════

class ProgressLogger:
    """Writes structured progress to a log file for remote monitoring via `tail -f`."""

    def __init__(self, filepath, total_docs):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self.total = total_docs
        self.start_time = time.time()

    def log(self, i, stats, current_doc="", current_section=""):
        elapsed = time.time() - self.start_time
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        remaining = (self.total - i - 1) / rate if rate > 0 else 0
        pct = (i + 1) / self.total * 100 if self.total > 0 else 0
        cost = stats["api_calls"] * 0.006

        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{i+1}/{self.total} ({pct:.1f}%) | "
            f"{rate:.2f} doc/s | "
            f"entities={stats['entities_extracted']} "
            f"empty={stats['empty_docs']} "
            f"err={stats['errors']} | "
            f"~${cost:.2f} | "
            f"ETA {remaining/3600:.1f}h | "
            f"{current_section}/{current_doc}"
        )

        with open(self.filepath, "a") as f:
            f.write(line + "\n")

    def log_final(self, stats):
        elapsed = time.time() - self.start_time
        lines = [
            "",
            "=" * 60,
            f"  EXTRACTION COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            f"  Processed:          {stats['processed']:,}",
            f"  Entities extracted: {stats['entities_extracted']:,}",
            f"  Empty/minimal:      {stats['empty_docs']:,}",
            f"  Errors:             {stats['errors']:,}",
            f"  API calls:          {stats['api_calls']:,}",
            f"  Est. cost:          ${stats['api_calls'] * 0.006:.2f}",
            f"  Total time:         {elapsed/3600:.1f} hours ({elapsed/60:.0f} min)",
            f"  Entities in DB:     {stats.get('total_entity_records', 0):,}",
            "=" * 60,
        ]
        with open(self.filepath, "a") as f:
            f.write("\n".join(lines) + "\n")


# ═══════════════════════════════════════════
# Text Extraction
# ═══════════════════════════════════════════

def extract_text_from_pdf(filepath, max_chars=MAX_CHARS_PER_DOC, max_pages=MAX_PAGES_TO_EXTRACT):
    """Extract text from PDF using PyMuPDF. Returns (text, page_count, extraction_notes)."""
    try:
        doc = fitz.open(filepath)
        page_count = doc.page_count
        notes = []

        if page_count == 0:
            doc.close()
            return "", 0, "empty_document"

        pages_to_read = min(page_count, max_pages)
        if pages_to_read < page_count:
            notes.append(f"sampled {pages_to_read}/{page_count} pages")

        text_parts = []
        total_chars = 0
        blank_pages = 0
        pages_with_images_only = 0

        for i in range(pages_to_read):
            page = doc[i]
            page_text = page.get_text("text").strip()

            if not page_text:
                # Check if page has images (likely scanned)
                if page.get_images():
                    pages_with_images_only += 1
                else:
                    blank_pages += 1
                continue

            # Add page marker
            text_parts.append(f"\n--- PAGE {i+1} ---\n{page_text}")
            total_chars += len(page_text)

            if total_chars >= max_chars:
                notes.append(f"text truncated at page {i+1}/{page_count}")
                break

        doc.close()

        if blank_pages > 0:
            notes.append(f"{blank_pages} blank pages")
        if pages_with_images_only > 0:
            notes.append(f"{pages_with_images_only} image-only pages (may need OCR)")

        full_text = "\n".join(text_parts)

        # Trim to max chars
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[TEXT TRUNCATED]"

        return full_text, page_count, "; ".join(notes) if notes else "clean_extraction"

    except Exception as e:
        return "", 0, f"extraction_error: {str(e)}"


def detect_redactions_from_text(text, page_count):
    """Detect redaction markers in extracted text."""
    redaction_patterns = [
        (r'\(b\)\(\d\)', "foia_exemption"),
        (r'\(b\)\(\d\)\([A-F]\)', "foia_exemption_sub"),
        (r'REDACTED', "explicit_redaction"),
        (r'\[SEALED\]', "sealed"),
        (r'XXXXXX+', "x_redaction"),
        (r'█+', "block_redaction"),
        (r'■+', "block_redaction"),
        (r'\[REDACTED\]', "explicit_redaction"),
    ]

    foia_codes = defaultdict(int)
    redaction_types = set()
    has_redactions = False

    for pattern, rtype in redaction_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            has_redactions = True
            redaction_types.add(rtype)
            if "foia" in rtype:
                for m in matches:
                    foia_codes[m] += 1

    # Estimate redaction density (rough: ratio of redaction markers to text length)
    redaction_marker_chars = sum(len(m) for p, _ in redaction_patterns
                                 for m in re.findall(p, text, re.IGNORECASE))
    density = min(redaction_marker_chars / max(len(text), 1), 1.0)

    return {
        "has_redactions": has_redactions,
        "redaction_density": round(density, 4),
        "redaction_types": list(redaction_types),
        "foia_codes": dict(foia_codes),
        "pages_with_redactions": [],  # Would need per-page analysis for this
    }


# ═══════════════════════════════════════════
# Claude Entity Extraction
# ═══════════════════════════════════════════

EXTRACTION_PROMPT = """You are analyzing a document from the DOJ's Jeffrey Epstein document release. Extract structured information from the text below.

Return a JSON object with exactly these fields:

{
  "document_type": "one of: travel_record, phone_record, financial_record, court_filing, deposition, police_report, fbi_report, correspondence, evidence_inventory, booking_record, victim_statement, witness_statement, subpoena, search_warrant, plea_agreement, indictment, motion, order, exhibit, photograph_log, surveillance_log, interview_notes, medical_record, property_record, flight_log, contact_list, calendar, memo, report, other",
  "document_summary": "2-3 sentence summary of what this document contains",
  "date_range": "ISO date or range if identifiable, e.g. '2005-03-15' or '2003-01 to 2006-12', or null",
  "people": [
    {"name": "Full Name", "role": "role/context in this document", "frequency": 1}
  ],
  "organizations": [
    {"name": "Org Name", "type": "company/government/nonprofit/law_firm/financial", "context": "brief context"}
  ],
  "locations": [
    {"name": "Location", "type": "address/city/country/property", "context": "brief context"}
  ],
  "dates": [
    {"date": "YYYY-MM-DD or partial", "event": "what happened on this date"}
  ],
  "financial_amounts": [
    {"amount": "$X", "context": "what the money was for"}
  ],
  "case_numbers": ["list of any case/docket numbers mentioned"],
  "phone_numbers": ["list of phone numbers found"],
  "key_relationships": [
    {"person1": "Name", "person2": "Name or Org", "relationship": "description"}
  ],
  "notable_findings": ["list of 1-3 significant or unusual details worth flagging"]
}

Rules:
- Extract ONLY what is explicitly stated in the text. Do not infer or speculate.
- For heavily redacted documents, note what categories of information appear to be redacted.
- If the document is mostly illegible or blank, return minimal fields with a note.
- Names should be normalized: "EPSTEIN, JEFFREY" → "Jeffrey Epstein"
- Be thorough with names — even minor mentions matter for relationship mapping.
- Return valid JSON only. No markdown, no explanation outside the JSON.

DOCUMENT TEXT:
"""


def extract_entities_with_claude(client, text, filename, max_retries=3):
    """Send text to Claude for entity extraction. Returns parsed JSON dict."""
    if not text or len(text.strip()) < 50:
        return {
            "document_type": "other",
            "document_summary": "Document too short or empty for analysis",
            "date_range": None,
            "people": [],
            "organizations": [],
            "locations": [],
            "dates": [],
            "financial_amounts": [],
            "case_numbers": [],
            "phone_numbers": [],
            "key_relationships": [],
            "notable_findings": ["Document appears blank or nearly empty"],
        }

    prompt = EXTRACTION_PROMPT + text

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text.strip()

            # Handle case where Claude wraps in ```json blocks
            if response_text.startswith("```"):
                response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
                response_text = re.sub(r'\s*```$', '', response_text)

            result = json.loads(response_text)
            return result

        except json.JSONDecodeError as e:
            log.warning("JSON parse error for %s (attempt %d): %s", filename, attempt + 1, e)
            if attempt == max_retries - 1:
                return {
                    "document_type": "other",
                    "document_summary": "Entity extraction returned invalid JSON",
                    "processing_error": str(e),
                    "raw_response": response_text[:500] if 'response_text' in dir() else "",
                }
        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)  # escalating backoff: 60s, 120s, 180s
            log.warning("Rate limited — waiting %ds (attempt %d)...", wait, attempt + 1)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:  # overloaded
                wait = 30 * (attempt + 1)
                log.warning("API overloaded — waiting %ds (attempt %d)...", wait, attempt + 1)
                time.sleep(wait)
            else:
                log.error("Claude API error for %s: %s", filename, e)
                return {
                    "document_type": "other",
                    "document_summary": f"Entity extraction failed: {str(e)}",
                }
        except Exception as e:
            log.error("Claude API error for %s: %s", filename, e)
            return {
                "document_type": "other",
                "document_summary": f"Entity extraction failed: {str(e)}",
            }

    return {
        "document_type": "other",
        "document_summary": "Entity extraction failed after max retries",
    }


# ═══════════════════════════════════════════
# MongoDB Updates — Documents + Entities
# ═══════════════════════════════════════════

def update_document_in_mongo(db, doc_id, extraction_result, redaction_analysis, text_length, extraction_notes):
    """Update a document record with extraction results."""
    entities = extraction_result or {}

    update = {
        "$set": {
            "document_type": entities.get("document_type", "unknown"),
            "document_summary": entities.get("document_summary", ""),
            "date_range": entities.get("date_range"),
            "extracted_entities": {
                "people": entities.get("people", []),
                "organizations": entities.get("organizations", []),
                "locations": entities.get("locations", []),
                "dates": entities.get("dates", []),
                "case_numbers": entities.get("case_numbers", []),
                "phone_numbers": entities.get("phone_numbers", []),
                "financial_amounts": entities.get("financial_amounts", []),
                "key_relationships": entities.get("key_relationships", []),
                "notable_findings": entities.get("notable_findings", []),
            },
            "redaction_analysis": redaction_analysis,
            "processing_stage": "entities_extracted",
            "processed_at": datetime.now(timezone.utc),
            "processing_notes": extraction_notes,
            "text_length": text_length,
        }
    }

    db["documents"].update_one({"doc_id": doc_id}, update)


def build_entity_records(doc_id, section, extraction_result):
    """Convert Claude's extraction output into flat entity records for the entities collection.

    Each entity gets its own record, enabling cross-document queries like:
    "Find every document mentioning Ghislaine Maxwell"
    """
    if not extraction_result:
        return []

    records = []
    now = datetime.now(timezone.utc)

    # People
    for person in extraction_result.get("people", []):
        if isinstance(person, dict) and person.get("name"):
            records.append({
                "name": person["name"],
                "name_lower": person["name"].lower(),  # for case-insensitive lookups
                "entity_type": "person",
                "source_doc_id": doc_id,
                "section": section,
                "role": person.get("role", ""),
                "frequency": person.get("frequency", 1),
                "confidence": person.get("confidence", 0.9),
                "context": person.get("role", ""),
                "extracted_at": now,
            })

    # Organizations
    for org in extraction_result.get("organizations", []):
        if isinstance(org, dict) and org.get("name"):
            records.append({
                "name": org["name"],
                "name_lower": org["name"].lower(),
                "entity_type": "organization",
                "source_doc_id": doc_id,
                "section": section,
                "org_type": org.get("type", ""),
                "confidence": 0.9,
                "context": org.get("context", ""),
                "extracted_at": now,
            })

    # Locations
    for loc in extraction_result.get("locations", []):
        if isinstance(loc, dict) and loc.get("name"):
            records.append({
                "name": loc["name"],
                "name_lower": loc["name"].lower(),
                "entity_type": "location",
                "source_doc_id": doc_id,
                "section": section,
                "location_type": loc.get("type", ""),
                "confidence": 0.9,
                "context": loc.get("context", ""),
                "extracted_at": now,
            })

    # Dates
    for date in extraction_result.get("dates", []):
        if isinstance(date, dict) and date.get("date"):
            records.append({
                "name": date["date"],
                "name_lower": date["date"].lower(),
                "entity_type": "date",
                "source_doc_id": doc_id,
                "section": section,
                "confidence": 0.9,
                "context": date.get("event", ""),
                "extracted_at": now,
            })

    # Case numbers
    for cn in extraction_result.get("case_numbers", []):
        if cn:
            records.append({
                "name": cn,
                "name_lower": cn.lower(),
                "entity_type": "case_number",
                "source_doc_id": doc_id,
                "section": section,
                "confidence": 1.0,
                "context": "",
                "extracted_at": now,
            })

    # Key relationships (stored as entity pairs for graph queries)
    for rel in extraction_result.get("key_relationships", []):
        if isinstance(rel, dict) and rel.get("person1") and rel.get("person2"):
            records.append({
                "name": f"{rel['person1']} ↔ {rel['person2']}",
                "name_lower": f"{rel['person1'].lower()} ↔ {rel['person2'].lower()}",
                "entity_type": "relationship",
                "source_doc_id": doc_id,
                "section": section,
                "person1": rel["person1"],
                "person2": rel["person2"],
                "relationship": rel.get("relationship", ""),
                "confidence": 0.8,
                "context": rel.get("relationship", ""),
                "extracted_at": now,
            })

    return records


def flush_entity_batch(db, entity_buffer, stats):
    """Write accumulated entity records to MongoDB in bulk."""
    if not entity_buffer:
        return

    try:
        result = db["entities"].insert_many(entity_buffer, ordered=False)
        stats["total_entity_records"] += len(result.inserted_ids)
    except BulkWriteError as e:
        # Some duplicates are fine — count what succeeded
        n_ok = e.details.get("nInserted", 0)
        stats["total_entity_records"] += n_ok
        log.warning("Entity batch: %d inserted, %d errors",
                     n_ok, len(e.details.get("writeErrors", [])))
    except Exception as e:
        log.error("Entity batch insert error: %s", e)


# ═══════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Extract text + entities from DOJ PDFs")
    parser.add_argument("--library-dir", default="./doj_full_library")
    parser.add_argument("--limit", type=int, default=0, help="Max docs to process (0=all)")
    parser.add_argument("--section", help="Only process this section")
    parser.add_argument("--resume", action="store_true",
                       help="Skip docs already processed (processing_stage=entities_extracted)")
    parser.add_argument("--text-only", action="store_true",
                       help="Extract text and redactions only, skip Claude API calls")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be processed without doing it")
    parser.add_argument("--log-file", default=PROGRESS_LOG_FILE,
                       help="Path for progress log file")
    args = parser.parse_args()

    # Validate environment
    if not args.text_only and not args.dry_run:
        if not ANTHROPIC_API_KEY:
            print("ERROR: Set ANTHROPIC_API_KEY environment variable")
            print('  export ANTHROPIC_API_KEY="sk-ant-..."')
            sys.exit(1)

    if "<db_password>" in MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)

    # Connect to MongoDB
    print(f"\n{'='*60}")
    print(f"  DOJ LIBRARY — ENTITY EXTRACTION PIPELINE v2")
    print(f"{'='*60}\n")

    print("  Connecting to MongoDB...")
    client_mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client_mongo.admin.command("ping")
    db = client_mongo[DATABASE_NAME]
    print("  Connected.\n")

    # Ensure entities collection indexes exist
    print("  Ensuring entity indexes...")
    ent_coll = db["entities"]
    ent_coll.create_index("name_lower", name="idx_entity_name_lower")
    ent_coll.create_index("entity_type", name="idx_entity_type")
    ent_coll.create_index([("entity_type", 1), ("name_lower", 1)], name="idx_type_name_lower")
    ent_coll.create_index("source_doc_id", name="idx_source_doc")
    ent_coll.create_index([("entity_type", 1), ("source_doc_id", 1)], name="idx_type_doc")
    ent_coll.create_index("section", name="idx_entity_section")
    print("  Indexes ready.\n")

    # Build file list from MongoDB (ensures we process what's registered)
    query = {}
    if args.section:
        query["section"] = args.section
    if args.resume:
        query["processing_stage"] = {"$ne": "entities_extracted"}

    docs = list(db["documents"].find(query, {"doc_id": 1, "filename": 1, "section": 1}).sort("section", 1))

    if args.limit > 0:
        # Sample across sections for variety
        if not args.section:
            by_section = defaultdict(list)
            for d in docs:
                by_section[d["section"]].append(d)

            sampled = []
            sections = sorted(by_section.keys())
            per_section = max(1, args.limit // len(sections))
            for section in sections:
                sampled.extend(by_section[section][:per_section])
                if len(sampled) >= args.limit:
                    break
            # Fill remaining from largest sections
            if len(sampled) < args.limit:
                for section in sections:
                    for d in by_section[section][per_section:]:
                        sampled.append(d)
                        if len(sampled) >= args.limit:
                            break
                    if len(sampled) >= args.limit:
                        break
            docs = sampled[:args.limit]
        else:
            docs = docs[:args.limit]

    print(f"  Documents to process: {len(docs):,}")
    if args.resume:
        total = db["documents"].count_documents({"section": args.section} if args.section else {})
        already = total - len(docs)
        print(f"  Already processed:    {already:,}")
        existing_entities = db["entities"].count_documents({})
        print(f"  Entities in DB:       {existing_entities:,}")

    sections_in_batch = set(d["section"] for d in docs)
    print(f"  Sections represented: {len(sections_in_batch)}")
    print(f"  Progress log:         {args.log_file}")

    if args.dry_run:
        by_section = defaultdict(int)
        for d in docs:
            by_section[d["section"]] += 1
        for s, c in sorted(by_section.items()):
            print(f"    {s}: {c}")
        return

    if not docs:
        print("\n  Nothing to process! All documents already extracted.")
        return

    # Initialize Claude client
    claude_client = None
    if not args.text_only:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print(f"  Claude model: {MODEL}")

    library = Path(args.library_dir)

    # Initialize progress logger
    progress = ProgressLogger(args.log_file, len(docs))

    # Process documents
    stats = {
        "processed": 0, "text_extracted": 0, "entities_extracted": 0,
        "empty_docs": 0, "errors": 0, "api_calls": 0,
        "total_entity_records": 0,
    }
    entity_buffer = []
    start_time = time.time()

    print(f"\n  {'─'*50}")
    print(f"  Processing...\n")

    for i, doc_record in enumerate(docs):
        doc_id = doc_record["doc_id"]
        filename = doc_record["filename"]
        section = doc_record["section"]
        filepath = library / section / filename

        if not filepath.exists():
            log.warning("File not found: %s", filepath)
            stats["errors"] += 1
            continue

        # Step 1: Extract text
        text, page_count, extraction_notes = extract_text_from_pdf(str(filepath))
        stats["text_extracted"] += 1

        if not text or len(text.strip()) < 50:
            stats["empty_docs"] += 1
            extraction_notes = "empty_or_minimal_text"

        # Step 2: Detect redactions from text
        redaction_analysis = detect_redactions_from_text(text, page_count)

        # Step 3: Claude entity extraction
        extraction_result = None
        if claude_client and text and len(text.strip()) >= 50:
            extraction_result = extract_entities_with_claude(claude_client, text, filename)
            stats["api_calls"] += 1
            stats["entities_extracted"] += 1
            # Rate limiting
            time.sleep(0.5)

        # Step 4: Update documents collection
        update_document_in_mongo(
            db, doc_id, extraction_result, redaction_analysis,
            len(text), extraction_notes
        )

        # Step 5: Accumulate entity records for batch insert
        if extraction_result:
            new_records = build_entity_records(doc_id, section, extraction_result)
            entity_buffer.extend(new_records)

        stats["processed"] += 1

        # Flush entity buffer periodically
        if len(entity_buffer) >= ENTITY_BATCH_SIZE:
            flush_entity_batch(db, entity_buffer, stats)
            entity_buffer = []

        # Progress (terminal + log file)
        if (i + 1) % 10 == 0 or (i + 1) == len(docs):
            elapsed = time.time() - start_time
            rate = stats["processed"] / elapsed if elapsed > 0 else 0
            remaining = (len(docs) - i - 1) / rate if rate > 0 else 0
            cost_est = stats["api_calls"] * 0.006

            print(f"  [{i+1}/{len(docs)}] "
                  f"{rate:.2f} docs/sec | "
                  f"entities={stats['entities_extracted']} "
                  f"empty={stats['empty_docs']} "
                  f"err={stats['errors']} | "
                  f"~${cost_est:.2f} spent | "
                  f"~{remaining/3600:.1f}h remaining | "
                  f"ent_records={stats['total_entity_records']+len(entity_buffer)}")

            progress.log(i, stats, filename, section)

    # Flush remaining entities
    if entity_buffer:
        flush_entity_batch(db, entity_buffer, stats)

    elapsed = time.time() - start_time

    print(f"\n{'='*60}")
    print(f"  EXTRACTION COMPLETE")
    print(f"{'='*60}")
    print(f"  Processed:            {stats['processed']:,}")
    print(f"  Text extracted:       {stats['text_extracted']:,}")
    print(f"  Entities extracted:   {stats['entities_extracted']:,}")
    print(f"  Empty/minimal docs:   {stats['empty_docs']:,}")
    print(f"  Errors:               {stats['errors']:,}")
    print(f"  Claude API calls:     {stats['api_calls']:,}")
    print(f"  Est. API cost:        ${stats['api_calls'] * 0.006:.2f}")
    print(f"  Entity records in DB: {stats['total_entity_records']:,}")
    print(f"  Time:                 {elapsed:.0f}s ({elapsed/60:.1f}m)")
    print(f"{'='*60}")

    # Write final stats to log file
    progress.log_final(stats)

    # Final DB stats
    total_docs = db["documents"].count_documents({"processing_stage": "entities_extracted"})
    total_entities = db["entities"].count_documents({})
    print(f"\n  Total extracted docs in MongoDB:  {total_docs:,}")
    print(f"  Total entity records in MongoDB: {total_entities:,}")

    # Show top entities as a sanity check
    print(f"\n  Top 10 most-referenced people:")
    pipeline = [
        {"$match": {"entity_type": "person"}},
        {"$group": {"_id": "$name_lower", "name": {"$first": "$name"}, "doc_count": {"$sum": 1}}},
        {"$sort": {"doc_count": -1}},
        {"$limit": 10},
    ]
    for result in db["entities"].aggregate(pipeline):
        print(f"    {result['name']}: {result['doc_count']} docs")

    # Show sample results
    if stats["entities_extracted"] > 0:
        print(f"\n  Sample extraction results:")
        sample = db["documents"].find(
            {"processing_stage": "entities_extracted", "document_summary": {"$ne": ""}},
            {"doc_id": 1, "document_type": 1, "document_summary": 1,
             "extracted_entities.people": 1}
        ).limit(5)

        for doc in sample:
            people = doc.get("extracted_entities", {}).get("people", [])
            people_names = [p["name"] if isinstance(p, dict) else p for p in people[:5]]
            print(f"\n    {doc['doc_id']}:")
            print(f"      Type: {doc.get('document_type', 'unknown')}")
            print(f"      Summary: {doc.get('document_summary', '')[:120]}")
            if people_names:
                print(f"      People: {', '.join(people_names)}")


if __name__ == "__main__":
    main()
