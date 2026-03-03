#!/usr/bin/env python3
"""
E-FINDER — GLiNER Secondary Entity Extraction
===============================================
Runs GLiNER (zero-shot NER) over the existing MongoDB document corpus to
extract entity types that Claude may have missed or under-extracted:

  - cryptonyms   : code names, operation names (e.g. "PBSUCCESS", "LANCER")
  - laws         : statutes, FOIA exemption codes, case law citations
  - events       : named events, operations, incidents
  - organizations: cross-validates Claude's org extraction
  - locations    : cross-validates Claude's location extraction

GLiNER is a zero-shot NER model — it runs entirely locally with no API cost.
It uses a span-based architecture that can extract arbitrary entity types
from any text without task-specific fine-tuning.

Results are stored in:
  1. MongoDB `gliner_entities` collection (one doc per source document)
  2. Weaviate EfinderChunks collection (updates cryptonyms/laws/events arrays)

Usage:
  pip install gliner

  export MONGODB_URI="mongodb+srv://..."
  export WEAVIATE_URL="http://localhost:8080"   # optional

  python3 gliner_reextract.py --dry-run --limit 5    # Preview on 5 docs
  python3 gliner_reextract.py --limit 100            # Process 100 docs
  python3 gliner_reextract.py                        # Full corpus
  python3 gliner_reextract.py --update-weaviate      # Also patch Weaviate chunks
  python3 gliner_reextract.py --stats                # Show extraction stats
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = "doj_investigation"
WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
WEAVIATE_API_KEY = os.environ.get("WEAVIATE_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
COLLECTION_NAME = "EfinderChunks"

# GLiNER entity labels — these are the zero-shot labels passed to the model.
# GLiNER can extract any label you define; we use IntellYWeave's proven set
# plus Epstein-corpus-specific additions.
GLINER_LABELS = [
    "person",
    "organization",
    "location",
    "date",
    "event",
    "law",
    "cryptonym",          # code names, operation names, aliases
    "financial_amount",
    "case_number",        # court case numbers, docket numbers
    "foia_exemption",     # (b)(1), (b)(6), (b)(7)(C) etc.
]

# Minimum GLiNER confidence score to include an entity
GLINER_THRESHOLD = 0.4

# Batch size for GLiNER inference (adjust based on GPU/CPU memory)
GLINER_BATCH_SIZE = 8

# Maximum text length per document to send to GLiNER (characters)
MAX_TEXT_LENGTH = 10_000


# ── Dependency bootstrap ──────────────────────────────────────────────────────
def _install(pkg):
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          pkg, "--break-system-packages", "-q"])


try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.database import Database
except ImportError:
    _install("pymongo")
    from pymongo import MongoClient, UpdateOne
    from pymongo.database import Database

try:
    from gliner import GLiNER
    GLINER_AVAILABLE = True
except ImportError:
    log.warning("GLiNER not installed. Run: pip install gliner")
    GLINER_AVAILABLE = False


# ── GLiNER model loader ───────────────────────────────────────────────────────
_gliner_model = None

def get_gliner_model():
    """Load GLiNER model (cached singleton)."""
    global _gliner_model
    if _gliner_model is None:
        if not GLINER_AVAILABLE:
            raise RuntimeError(
                "GLiNER not installed. Run: pip install gliner"
            )
        log.info("Loading GLiNER model (urchade/gliner_mediumv2.1)...")
        # urchade/gliner_mediumv2.1 is the recommended general-purpose model
        # from IntellYWeave. It handles cryptonyms and legal text well.
        _gliner_model = GLiNER.from_pretrained("urchade/gliner_mediumv2.1")
        log.info("GLiNER model loaded.")
    return _gliner_model


# ── Entity extraction ─────────────────────────────────────────────────────────
def extract_entities_gliner(text: str, labels: list[str] = None,
                             threshold: float = GLINER_THRESHOLD) -> dict:
    """
    Run GLiNER on a text string and return entities grouped by label.

    Returns:
        {
          "person": ["Klaus Barbie", "Jeffrey Epstein"],
          "cryptonym": ["LANCER", "PBSUCCESS"],
          "law": ["18 U.S.C. § 2252", "(b)(7)(C)"],
          "event": ["Operation Paperclip"],
          ...
        }
    """
    if not text or not text.strip():
        return {label: [] for label in (labels or GLINER_LABELS)}

    model = get_gliner_model()
    labels = labels or GLINER_LABELS

    # Truncate to avoid memory issues
    text = text[:MAX_TEXT_LENGTH]

    try:
        entities = model.predict_entities(text, labels, threshold=threshold)
    except Exception as e:
        log.error("GLiNER prediction failed: %s", e)
        return {label: [] for label in labels}

    # Group by label, deduplicate
    grouped = defaultdict(set)
    for ent in entities:
        label = ent.get("label", "")
        value = ent.get("text", "").strip()
        if label and value and len(value) > 1:
            grouped[label].add(value)

    return {label: sorted(grouped.get(label, [])) for label in labels}


# ── MongoDB helpers ───────────────────────────────────────────────────────────
def get_document_text(doc: dict) -> str:
    """Extract the best available text from a MongoDB document."""
    return (doc.get("full_text") or doc.get("text") or
            doc.get("document_summary") or "")


def setup_gliner_collection(db: Database):
    """Create indexes on the gliner_entities collection."""
    coll = db["gliner_entities"]
    coll.create_index("doc_id", unique=True)
    coll.create_index("cryptonyms")
    coll.create_index("laws")
    coll.create_index("events")
    log.info("gliner_entities indexes created.")


# ── Weaviate update helper ────────────────────────────────────────────────────
def update_weaviate_chunks(weaviate_client, doc_id: str,
                           cryptonyms: list, laws: list, events: list):
    """
    Update the cryptonyms/laws/events arrays on all Weaviate chunks
    belonging to a given doc_id.
    """
    if not weaviate_client:
        return

    try:
        import weaviate.classes as wvc
        collection = weaviate_client.collections.get(COLLECTION_NAME)

        # Find all chunks for this doc_id
        response = collection.query.fetch_objects(
            filters=wvc.query.Filter.by_property("doc_id").equal(doc_id),
            return_properties=["doc_id"],
            limit=1000,
        )

        for obj in response.objects:
            collection.data.update(
                uuid=obj.uuid,
                properties={
                    "cryptonyms": cryptonyms,
                    "laws": laws,
                    "events": events,
                },
            )
    except Exception as e:
        log.warning("Weaviate update failed for doc_id=%s: %s", doc_id, e)


# ── Main extraction loop ──────────────────────────────────────────────────────
def run_extraction(db: Database, weaviate_client=None,
                   limit: Optional[int] = None,
                   dry_run: bool = False,
                   update_weaviate: bool = False,
                   skip_existing: bool = True):
    """
    Main extraction loop: iterate over MongoDB documents, run GLiNER,
    store results in gliner_entities collection.
    """
    setup_gliner_collection(db)

    # Find already-processed doc_ids
    processed_ids = set()
    if skip_existing:
        for rec in db["gliner_entities"].find({}, {"doc_id": 1}):
            processed_ids.add(rec["doc_id"])
        log.info("Already processed: %d documents", len(processed_ids))

    query_filter = {}
    if processed_ids:
        query_filter["doc_id"] = {"$nin": list(processed_ids)}

    cursor = db["documents"].find(
        query_filter,
        {"doc_id": 1, "full_text": 1, "text": 1, "document_summary": 1,
         "section": 1, "document_type": 1, "filename": 1}
    )
    if limit:
        cursor = cursor.limit(limit)

    total = db["documents"].count_documents(query_filter)
    if limit:
        total = min(total, limit)
    log.info("Documents to process: %d", total)

    # Weaviate client setup
    if update_weaviate and not weaviate_client:
        try:
            import weaviate
            import weaviate.classes as wvc
            if WEAVIATE_API_KEY:
                weaviate_client = weaviate.connect_to_weaviate_cloud(
                    cluster_url=WEAVIATE_URL,
                    auth_credentials=wvc.init.Auth.api_key(WEAVIATE_API_KEY),
                )
            else:
                host = WEAVIATE_URL.replace("http://", "").replace("https://", "").split(":")[0]
                port = int(WEAVIATE_URL.split(":")[-1]) if ":" in WEAVIATE_URL else 8080
                weaviate_client = weaviate.connect_to_local(host=host, port=port)
        except Exception as e:
            log.warning("Weaviate unavailable — skipping Weaviate updates: %s", e)
            weaviate_client = None

    processed = 0
    errors = 0
    skipped = 0
    total_cryptonyms = 0
    total_laws = 0
    total_events = 0

    bulk_ops = []

    for doc in cursor:
        doc_id = str(doc.get("doc_id", ""))
        text = get_document_text(doc)

        if not text or not text.strip():
            skipped += 1
            continue

        if dry_run:
            # Just show what would be extracted
            entities = extract_entities_gliner(text[:2000])
            print(f"\n  doc_id: {doc_id}")
            print(f"  filename: {doc.get('filename', 'N/A')}")
            for label, values in entities.items():
                if values:
                    print(f"    {label}: {values[:5]}")
            processed += 1
            if processed >= (limit or 5):
                break
            continue

        try:
            entities = extract_entities_gliner(text)
        except Exception as e:
            log.error("GLiNER failed on doc_id=%s: %s", doc_id, e)
            errors += 1
            continue

        cryptonyms = entities.get("cryptonym", [])
        laws = entities.get("law", []) + entities.get("foia_exemption", [])
        events = entities.get("event", [])

        # Deduplicate
        laws = list(set(laws))

        total_cryptonyms += len(cryptonyms)
        total_laws += len(laws)
        total_events += len(events)

        record = {
            "doc_id":        doc_id,
            "filename":      doc.get("filename", ""),
            "section":       doc.get("section", ""),
            "document_type": doc.get("document_type", ""),
            "persons":       entities.get("person", []),
            "organizations": entities.get("organization", []),
            "locations":     entities.get("location", []),
            "dates":         entities.get("date", []),
            "events":        events,
            "laws":          laws,
            "cryptonyms":    cryptonyms,
            "case_numbers":  entities.get("case_number", []),
            "financial_amounts": entities.get("financial_amount", []),
            "extracted_at":  datetime.now(timezone.utc),
        }

        bulk_ops.append(UpdateOne(
            {"doc_id": doc_id},
            {"$set": record},
            upsert=True,
        ))

        # Update Weaviate chunks if requested
        if update_weaviate and weaviate_client and (cryptonyms or laws or events):
            update_weaviate_chunks(weaviate_client, doc_id,
                                   cryptonyms, laws, events)

        # Flush bulk ops
        if len(bulk_ops) >= 200:
            db["gliner_entities"].bulk_write(bulk_ops, ordered=False)
            bulk_ops = []

        processed += 1
        if processed % 100 == 0:
            pct = (processed / total * 100) if total else 0
            log.info(
                "Progress: %d/%d (%.1f%%) — "
                "cryptonyms: %d, laws: %d, events: %d, errors: %d",
                processed, total, pct,
                total_cryptonyms, total_laws, total_events, errors
            )

    # Final flush
    if bulk_ops and not dry_run:
        db["gliner_entities"].bulk_write(bulk_ops, ordered=False)

    if not dry_run:
        log.info(
            "Extraction complete. Processed: %d, Skipped: %d, Errors: %d",
            processed, skipped, errors
        )
        log.info(
            "New entities found — Cryptonyms: %d, Laws: %d, Events: %d",
            total_cryptonyms, total_laws, total_events
        )


# ── Stats ─────────────────────────────────────────────────────────────────────
def show_stats(db: Database):
    """Print GLiNER extraction statistics."""
    total = db["gliner_entities"].count_documents({})
    print(f"\n  gliner_entities collection: {total:,} documents processed")

    # Top cryptonyms
    pipeline = [
        {"$unwind": "$cryptonyms"},
        {"$group": {"_id": "$cryptonyms", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    top_cryptonyms = list(db["gliner_entities"].aggregate(pipeline))
    if top_cryptonyms:
        print(f"\n  Top cryptonyms:")
        for c in top_cryptonyms:
            print(f"    {c['_id']}: {c['count']} documents")

    # Top laws/FOIA codes
    pipeline = [
        {"$unwind": "$laws"},
        {"$group": {"_id": "$laws", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    top_laws = list(db["gliner_entities"].aggregate(pipeline))
    if top_laws:
        print(f"\n  Top laws/FOIA codes:")
        for l in top_laws:
            print(f"    {l['_id']}: {l['count']} documents")

    # Top events
    pipeline = [
        {"$unwind": "$events"},
        {"$group": {"_id": "$events", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    top_events = list(db["gliner_entities"].aggregate(pipeline))
    if top_events:
        print(f"\n  Top events:")
        for e in top_events:
            print(f"    {e['_id']}: {e['count']} documents")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="E-FINDER GLiNER secondary entity extraction"
    )
    parser.add_argument("--dry-run",         action="store_true",
                        help="Preview extraction without writing to MongoDB")
    parser.add_argument("--limit",           type=int, default=None,
                        help="Limit number of documents to process")
    parser.add_argument("--no-skip",         action="store_true",
                        help="Re-process already-extracted documents")
    parser.add_argument("--update-weaviate", action="store_true",
                        help="Also update Weaviate chunk objects with new entities")
    parser.add_argument("--stats",           action="store_true",
                        help="Show extraction statistics")
    parser.add_argument("--test",            type=str, metavar="TEXT",
                        help="Test GLiNER on a text string")
    args = parser.parse_args()

    if args.test:
        print("\n  GLiNER test extraction:")
        entities = extract_entities_gliner(args.test)
        for label, values in entities.items():
            if values:
                print(f"    {label}: {values}")
        return

    if args.stats:
        if not MONGODB_URI:
            print("ERROR: Set MONGODB_URI environment variable")
            sys.exit(1)
        mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        db = mongo[DATABASE_NAME]
        show_stats(db)
        return

    if not MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  E-FINDER — GLiNER Entity Extraction")
    print(f"{'='*60}\n")

    mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    db = mongo[DATABASE_NAME]

    run_extraction(
        db,
        limit=args.limit,
        dry_run=args.dry_run,
        update_weaviate=args.update_weaviate,
        skip_existing=not args.no_skip,
    )

    print("\n  Done.")


if __name__ == "__main__":
    main()
