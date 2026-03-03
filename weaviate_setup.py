#!/usr/bin/env python3
"""
E-FINDER — Weaviate Setup & MongoDB Migration
===============================================
Sets up a local Weaviate instance (via Docker) and migrates the existing
MongoDB document corpus into Weaviate for semantic + hybrid search.

This is the foundation for IntellYWeave integration — once documents are
in Weaviate, the upgraded DocumentQuery agent can use vector similarity
search alongside the existing MongoDB keyword queries.

Pipeline:
  MongoDB documents collection
    → fetch text + entity metadata
    → chunk into semantic segments (~500 tokens)
    → vectorize with OpenAI text-embedding-3-small
    → store in Weaviate EFINDER_CHUNKS collection

Usage:
  # 1. Start Weaviate (Docker required):
  #    docker run -d -p 8080:8080 -p 50051:50051 \\
  #      -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true \\
  #      -e PERSISTENCE_DATA_PATH=/var/lib/weaviate \\
  #      cr.weaviate.io/semitechnologies/weaviate:latest

  export MONGODB_URI="mongodb+srv://..."
  export OPENAI_API_KEY="sk-..."
  export WEAVIATE_URL="http://localhost:8080"   # or Weaviate Cloud URL
  export WEAVIATE_API_KEY=""                    # leave blank for local

  python3 weaviate_setup.py --setup            # Create schema only
  python3 weaviate_setup.py --migrate          # Full migration
  python3 weaviate_setup.py --migrate --limit 100   # Test with 100 docs
  python3 weaviate_setup.py --stats            # Show collection stats
  python3 weaviate_setup.py --test-search "Deutsche Bank financial records"
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = "doj_investigation"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
WEAVIATE_API_KEY = os.environ.get("WEAVIATE_API_KEY", "")

COLLECTION_NAME = "EfinderChunks"
CHUNK_SIZE = 500        # tokens (approximate — we use word count as proxy)
CHUNK_OVERLAP = 50      # words of overlap between chunks
BATCH_SIZE = 100        # objects per Weaviate batch insert
EMBEDDING_MODEL = "text-embedding-3-small"


# ── Dependency bootstrap ──────────────────────────────────────────────────────
def _install(pkg):
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          pkg, "--break-system-packages", "-q"])


try:
    from pymongo import MongoClient
except ImportError:
    _install("pymongo")
    from pymongo import MongoClient

try:
    import weaviate
    import weaviate.classes as wvc
    from weaviate.classes.config import Property, DataType, Configure
except ImportError:
    _install("weaviate-client>=4.0.0")
    import weaviate
    import weaviate.classes as wvc
    from weaviate.classes.config import Property, DataType, Configure

try:
    import openai
except ImportError:
    _install("openai")
    import openai


# ── Weaviate client factory ───────────────────────────────────────────────────
def get_weaviate_client():
    """Return a connected Weaviate v4 client."""
    if WEAVIATE_API_KEY:
        client = weaviate.connect_to_weaviate_cloud(
            cluster_url=WEAVIATE_URL,
            auth_credentials=wvc.init.Auth.api_key(WEAVIATE_API_KEY),
            headers={"X-OpenAI-Api-Key": OPENAI_API_KEY} if OPENAI_API_KEY else {},
        )
    else:
        client = weaviate.connect_to_local(
            host=WEAVIATE_URL.replace("http://", "").replace("https://", "").split(":")[0],
            port=int(WEAVIATE_URL.split(":")[-1]) if ":" in WEAVIATE_URL else 8080,
            headers={"X-OpenAI-Api-Key": OPENAI_API_KEY} if OPENAI_API_KEY else {},
        )
    return client


# ── Schema setup ─────────────────────────────────────────────────────────────
COLLECTION_SCHEMA = {
    "name": COLLECTION_NAME,
    "description": "E-FINDER document chunks with entity metadata for hybrid search",
    "properties": [
        # Core content
        Property(name="text",          data_type=DataType.TEXT,
                 description="Chunk text content"),
        Property(name="doc_id",        data_type=DataType.TEXT,
                 description="Source document ID from MongoDB"),
        Property(name="filename",      data_type=DataType.TEXT,
                 description="Original PDF filename"),
        Property(name="section",       data_type=DataType.TEXT,
                 description="DOJ corpus section (e.g. 'court_cases', 'foia')"),
        Property(name="document_type", data_type=DataType.TEXT,
                 description="Document type (e.g. 'deposition', 'email', 'financial_record')"),
        Property(name="chunk_index",   data_type=DataType.INT,
                 description="Position of this chunk within the document"),
        Property(name="document_summary", data_type=DataType.TEXT,
                 description="Claude-generated summary of the parent document"),
        Property(name="date_range",    data_type=DataType.TEXT,
                 description="Date range of the document"),

        # Entity arrays (from Claude extraction — stored as JSON strings for filtering)
        Property(name="persons",       data_type=DataType.TEXT_ARRAY,
                 description="Person names extracted from this chunk"),
        Property(name="organizations", data_type=DataType.TEXT_ARRAY,
                 description="Organization names extracted from this chunk"),
        Property(name="locations",     data_type=DataType.TEXT_ARRAY,
                 description="Location names extracted from this chunk"),
        Property(name="dates",         data_type=DataType.TEXT_ARRAY,
                 description="Dates extracted from this chunk"),
        Property(name="financial_amounts", data_type=DataType.TEXT_ARRAY,
                 description="Financial amounts extracted from this chunk"),

        # GLiNER-specific entity types (populated by gliner_reextract.py)
        Property(name="cryptonyms",    data_type=DataType.TEXT_ARRAY,
                 description="Code names / cryptonyms (GLiNER extraction)"),
        Property(name="laws",          data_type=DataType.TEXT_ARRAY,
                 description="Laws / statutes / FOIA codes (GLiNER extraction)"),
        Property(name="events",        data_type=DataType.TEXT_ARRAY,
                 description="Named events (GLiNER extraction)"),

        # Redaction metadata
        Property(name="has_redactions",    data_type=DataType.BOOL,
                 description="Whether the source document has redactions"),
        Property(name="redaction_density", data_type=DataType.NUMBER,
                 description="Fraction of content that is redacted (0.0–1.0)"),
    ],
    "vectorizer_config": Configure.Vectorizer.text2vec_openai(
        model=EMBEDDING_MODEL,
    ) if OPENAI_API_KEY else Configure.Vectorizer.none(),
}


def setup_schema(client):
    """Create the Weaviate collection if it does not already exist."""
    existing = [c.name for c in client.collections.list_all().values()]
    if COLLECTION_NAME in existing:
        log.info("Collection '%s' already exists — skipping creation.", COLLECTION_NAME)
        return

    log.info("Creating Weaviate collection '%s'...", COLLECTION_NAME)
    client.collections.create(**COLLECTION_SCHEMA)
    log.info("Collection created.")


# ── Text chunking ─────────────────────────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word-count chunks."""
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end == len(words):
            break
        start += chunk_size - overlap

    return chunks


# ── Entity extraction helpers ─────────────────────────────────────────────────
def _extract_entity_list(doc: dict, entity_type: str) -> list[str]:
    """Pull entity names from a MongoDB document's extracted_entities field."""
    entities = doc.get("extracted_entities", {})
    raw = entities.get(entity_type, [])
    names = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name") or item.get("amount") or item.get("date") or ""
            if name:
                names.append(str(name))
        elif isinstance(item, str):
            names.append(item)
    return names[:50]  # cap to avoid Weaviate property size limits


def _build_chunk_object(doc: dict, chunk_text: str, chunk_index: int) -> dict:
    """Build a Weaviate object dict from a MongoDB document + chunk text."""
    redaction = doc.get("redaction_analysis", {}) or {}
    return {
        "text":              chunk_text,
        "doc_id":            str(doc.get("doc_id", "")),
        "filename":          str(doc.get("filename", "")),
        "section":           str(doc.get("section", "")),
        "document_type":     str(doc.get("document_type", "")),
        "chunk_index":       chunk_index,
        "document_summary":  str(doc.get("document_summary", ""))[:1000],
        "date_range":        str(doc.get("date_range", "") or ""),
        "persons":           _extract_entity_list(doc, "people"),
        "organizations":     _extract_entity_list(doc, "organizations"),
        "locations":         _extract_entity_list(doc, "locations"),
        "dates":             _extract_entity_list(doc, "dates"),
        "financial_amounts": _extract_entity_list(doc, "financial_amounts"),
        # GLiNER fields — populated empty here; filled by gliner_reextract.py
        "cryptonyms":        [],
        "laws":              [],
        "events":            [],
        "has_redactions":    bool(redaction.get("has_redactions", False)),
        "redaction_density": float(redaction.get("redaction_density", 0.0) or 0.0),
    }


# ── Migration ─────────────────────────────────────────────────────────────────
def migrate(client, db, limit: Optional[int] = None, skip_existing: bool = True):
    """Migrate MongoDB documents → Weaviate chunks."""
    collection = client.collections.get(COLLECTION_NAME)

    # Find already-migrated doc_ids to support resume (paginated to avoid query limit)
    migrated_ids = set()
    if skip_existing:
        log.info("Checking for already-migrated documents...")
        PAGE_SIZE = 1000
        offset = 0
        while True:
            response = collection.query.fetch_objects(
                return_properties=["doc_id"],
                limit=PAGE_SIZE,
                offset=offset,
            )
            if not response.objects:
                break
            for obj in response.objects:
                migrated_ids.add(obj.properties.get("doc_id", ""))
            if len(response.objects) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        log.info("Already migrated: %d documents", len(migrated_ids))

    # Query MongoDB
    query_filter = {}
    if migrated_ids:
        query_filter["doc_id"] = {"$nin": list(migrated_ids)}

    cursor = db["documents"].find(
        query_filter,
        {
            "doc_id": 1, "filename": 1, "section": 1, "document_type": 1,
            "document_summary": 1, "date_range": 1, "extracted_entities": 1,
            "redaction_analysis": 1, "full_text": 1, "text": 1,
        }
    )
    if limit:
        cursor = cursor.limit(limit)

    total = db["documents"].count_documents(query_filter)
    if limit:
        total = min(total, limit)
    log.info("Documents to migrate: %d", total)

    batch_objects = []
    processed = 0
    skipped = 0
    errors = 0

    for doc in cursor:
        doc_id = str(doc.get("doc_id", ""))

        # Get text — try full_text first, then text, then summary
        text = (doc.get("full_text") or doc.get("text") or
                doc.get("document_summary") or "")
        if not text or not text.strip():
            skipped += 1
            continue

        chunks = chunk_text(text)
        for idx, chunk in enumerate(chunks):
            obj = _build_chunk_object(doc, chunk, idx)
            batch_objects.append(obj)

        # Flush batch
        if len(batch_objects) >= BATCH_SIZE:
            _flush_batch(collection, batch_objects)
            batch_objects = []

        processed += 1
        if processed % 500 == 0:
            pct = (processed / total * 100) if total else 0
            log.info("Progress: %d/%d (%.1f%%) — errors: %d, skipped: %d",
                     processed, total, pct, errors, skipped)

    # Final flush
    if batch_objects:
        _flush_batch(collection, batch_objects)

    log.info("Migration complete. Processed: %d, Skipped: %d, Errors: %d",
             processed, skipped, errors)


def _flush_batch(collection, objects: list):
    """Insert a batch of objects into Weaviate."""
    with collection.batch.dynamic() as batch:
        for obj in objects:
            batch.add_object(properties=obj)


# ── Stats ─────────────────────────────────────────────────────────────────────
def show_stats(client):
    """Print collection statistics."""
    collection = client.collections.get(COLLECTION_NAME)
    agg = collection.aggregate.over_all(total_count=True)
    total = agg.total_count
    print(f"\n  Weaviate collection: {COLLECTION_NAME}")
    print(f"  Total chunks:        {total:,}")
    print(f"  Weaviate URL:        {WEAVIATE_URL}")


# ── Test search ───────────────────────────────────────────────────────────────
def test_search(client, query: str, limit: int = 5):
    """Run a hybrid search and print results."""
    collection = client.collections.get(COLLECTION_NAME)

    print(f"\n  Hybrid search: '{query}'")
    print(f"  {'─'*60}")

    response = collection.query.hybrid(
        query=query,
        limit=limit,
        alpha=0.5,  # 0 = pure BM25, 1 = pure vector
        return_properties=["doc_id", "filename", "section",
                           "document_type", "text", "persons",
                           "date_range"],
        return_metadata=wvc.query.MetadataQuery(score=True),
    )

    for i, obj in enumerate(response.objects, 1):
        p = obj.properties
        score = obj.metadata.score if obj.metadata else "N/A"
        print(f"\n  [{i}] {p.get('filename', 'unknown')} "
              f"(section: {p.get('section', '?')}, "
              f"type: {p.get('document_type', '?')})")
        print(f"      Score: {score:.4f}" if isinstance(score, float) else f"      Score: {score}")
        print(f"      Date: {p.get('date_range', 'N/A')}")
        persons = p.get("persons", [])
        if persons:
            print(f"      Persons: {', '.join(persons[:5])}")
        text_preview = (p.get("text") or "")[:200].replace("\n", " ")
        print(f"      Text: {text_preview}...")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="E-FINDER Weaviate setup and MongoDB migration"
    )
    parser.add_argument("--setup",       action="store_true",
                        help="Create Weaviate schema only")
    parser.add_argument("--migrate",     action="store_true",
                        help="Migrate MongoDB documents to Weaviate")
    parser.add_argument("--limit",       type=int, default=None,
                        help="Limit number of documents to migrate (for testing)")
    parser.add_argument("--no-skip",     action="store_true",
                        help="Re-migrate already-migrated documents")
    parser.add_argument("--stats",       action="store_true",
                        help="Show Weaviate collection statistics")
    parser.add_argument("--test-search", type=str, metavar="QUERY",
                        help="Run a test hybrid search")
    parser.add_argument("--drop",        action="store_true",
                        help="Drop and recreate the collection (WARNING: destructive)")
    args = parser.parse_args()

    if not any([args.setup, args.migrate, args.stats, args.test_search, args.drop]):
        parser.print_help()
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"  E-FINDER — Weaviate Setup")
    print(f"{'='*60}\n")
    print(f"  Weaviate: {WEAVIATE_URL}")

    client = get_weaviate_client()
    log.info("Connected to Weaviate.")

    try:
        if args.drop:
            existing = [c.name for c in client.collections.list_all().values()]
            if COLLECTION_NAME in existing:
                log.warning("Dropping collection '%s'...", COLLECTION_NAME)
                client.collections.delete(COLLECTION_NAME)
                log.info("Dropped.")

        if args.setup or args.migrate:
            setup_schema(client)

        if args.migrate:
            if not MONGODB_URI:
                print("ERROR: Set MONGODB_URI environment variable")
                sys.exit(1)
            mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
            db = mongo[DATABASE_NAME]
            migrate(client, db, limit=args.limit, skip_existing=not args.no_skip)

        if args.stats:
            show_stats(client)

        if args.test_search:
            test_search(client, args.test_search)

    finally:
        client.close()

    print("\n  Done.")


if __name__ == "__main__":
    main()
