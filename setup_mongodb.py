#!/usr/bin/env python3
"""
setup_mongodb.py — MongoDB Atlas setup for DOJ Document Investigation Pipeline

Creates database, collections, indexes, and provides ingestion functions.
Requires: pymongo (pip install pymongo)

Usage:
    1. Set MONGODB_URI environment variable or edit PLACEHOLDER below
    2. python3 setup_mongodb.py --setup     # Create DB, collections, indexes
    3. python3 setup_mongodb.py --verify    # Verify setup
    4. Import ingest_document() in other scripts for document ingestion
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from typing import Optional

try:
    from pymongo import MongoClient, ASCENDING, TEXT
    from pymongo.errors import (
        ConnectionFailure,
        OperationFailure,
        DuplicateKeyError,
        ServerSelectionTimeoutError,
    )
except ImportError:
    print("ERROR: pymongo not installed. Run: pip install pymongo")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb+srv://user:password@your-cluster.mongodb.net/?retryWrites=true&w=majority",
)

DATABASE_NAME = "doj_investigation"

COLLECTIONS = {
    "documents": "Primary document store — one record per file",
    "entities": "Extracted entities (people, orgs, locations, dates, case numbers)",
    "redactions": "Redaction metadata and analysis per document/page",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MongoDB Document Schema (matches CLAUDE.md specification)
# ---------------------------------------------------------------------------

DOCUMENT_SCHEMA = {
    "doc_id": "string — unique identifier (Bates number or generated UUID)",
    "filename": "string — original filename",
    "source": "string — DOJ dump batch/source URL",
    "file_type": "string — pdf/txt/html/etc",
    "total_pages": "number",
    "file_size_bytes": "number",
    "page_index_tree": "object — full PageIndex JSON tree",
    "redaction_analysis": {
        "has_redactions": "boolean",
        "pages_with_redactions": "array of page numbers",
        "redaction_density": "float 0-1",
        "redaction_types": "array — person_name/location/date/case_number/org/unknown",
        "foia_codes": "object — code: count mapping",
    },
    "extracted_entities": {
        "people": "array of {name, pages, confidence}",
        "organizations": "array of {name, pages, confidence}",
        "locations": "array of {name, pages, confidence}",
        "dates": "array of {date, pages, context}",
        "case_numbers": "array of strings",
    },
    "document_type": "string — evidence_inventory/travel_record/inspection_record/"
    "contact_list/victim_list/booking_record/query_history/"
    "foia_tracking/aircraft_report/court_record/government_exhibit/unknown",
    "date_range": "string — ISO date range if identifiable",
    "classification_markings": "array — any classification/handling markings found",
    "processed_at": "datetime",
    "processing_notes": "string — any issues during processing",
}


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def get_client(uri: Optional[str] = None, timeout_ms: int = 10_000) -> MongoClient:
    """Return a MongoClient, verifying the connection is alive."""
    uri = uri or MONGODB_URI
    if "<db_password>" in uri or "<username>" in uri or "<password>" in uri:
        log.error(
            "MongoDB URI still contains placeholders. "
            "Set MONGODB_URI env var or edit the script."
        )
        sys.exit(1)

    client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
    try:
        client.admin.command("ping")
        log.info("Connected to MongoDB Atlas successfully.")
    except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
        log.error("Could not connect to MongoDB: %s", exc)
        sys.exit(1)
    return client


def get_database(client: Optional[MongoClient] = None):
    """Return the investigation database handle."""
    client = client or get_client()
    return client[DATABASE_NAME]


# ---------------------------------------------------------------------------
# Setup: create collections + indexes
# ---------------------------------------------------------------------------


def setup_database(client: Optional[MongoClient] = None) -> None:
    """Create collections and indexes for the investigation pipeline."""
    client = client or get_client()
    db = client[DATABASE_NAME]

    # --- documents collection ---
    docs = db["documents"]
    log.info("Creating indexes on 'documents' collection...")

    # Unique index on doc_id
    docs.create_index("doc_id", unique=True, name="idx_doc_id_unique")

    # Text index for full-text search across extracted text summaries
    docs.create_index(
        [
            ("filename", TEXT),
            ("processing_notes", TEXT),
            ("document_type", TEXT),
        ],
        name="idx_documents_text",
    )

    # Index on document type for filtering
    docs.create_index("document_type", name="idx_document_type")

    # Index on redaction density for finding most-redacted docs
    docs.create_index(
        "redaction_analysis.redaction_density",
        name="idx_redaction_density",
    )

    # Index on processing timestamp
    docs.create_index("processed_at", name="idx_processed_at")

    # Compound index for file type + date range queries
    docs.create_index(
        [("file_type", ASCENDING), ("date_range", ASCENDING)],
        name="idx_filetype_daterange",
    )

    log.info("  'documents' indexes created.")

    # --- entities collection ---
    entities = db["entities"]
    log.info("Creating indexes on 'entities' collection...")

    # Index on entity name for lookups
    entities.create_index("name", name="idx_entity_name")

    # Index on entity type
    entities.create_index("entity_type", name="idx_entity_type")

    # Compound index for type + name
    entities.create_index(
        [("entity_type", ASCENDING), ("name", ASCENDING)],
        name="idx_type_name",
    )

    # Index on source document
    entities.create_index("source_doc_id", name="idx_source_doc")

    # Text index on entity name for fuzzy matching
    entities.create_index([("name", TEXT)], name="idx_entity_name_text")

    log.info("  'entities' indexes created.")

    # --- redactions collection ---
    redactions = db["redactions"]
    log.info("Creating indexes on 'redactions' collection...")

    # Index on source document
    redactions.create_index("doc_id", name="idx_redaction_doc_id")

    # Index on FOIA exemption code
    redactions.create_index("foia_code", name="idx_foia_code")

    # Index on page number for page-level queries
    redactions.create_index(
        [("doc_id", ASCENDING), ("page_number", ASCENDING)],
        name="idx_doc_page",
    )

    # Index on redaction type (inferred category)
    redactions.create_index("inferred_type", name="idx_inferred_type")

    log.info("  'redactions' indexes created.")

    log.info("Database setup complete: %s", DATABASE_NAME)


# ---------------------------------------------------------------------------
# Ingestion functions
# ---------------------------------------------------------------------------


def ingest_document(
    db,
    doc_id: str,
    filename: str,
    file_type: str,
    total_pages: int,
    file_size_bytes: int,
    source: str = "E-FINDER DOJ dump",
    page_index_tree: Optional[dict] = None,
    redaction_analysis: Optional[dict] = None,
    extracted_entities: Optional[dict] = None,
    document_type: str = "unknown",
    date_range: Optional[str] = None,
    classification_markings: Optional[list] = None,
    processing_notes: str = "",
) -> str:
    """
    Ingest a single processed document into MongoDB.

    Returns the inserted document's _id as a string.
    """
    doc = {
        "doc_id": doc_id,
        "filename": filename,
        "source": source,
        "file_type": file_type,
        "total_pages": total_pages,
        "file_size_bytes": file_size_bytes,
        "page_index_tree": page_index_tree or {},
        "redaction_analysis": redaction_analysis
        or {
            "has_redactions": False,
            "pages_with_redactions": [],
            "redaction_density": 0.0,
            "redaction_types": [],
            "foia_codes": {},
        },
        "extracted_entities": extracted_entities
        or {
            "people": [],
            "organizations": [],
            "locations": [],
            "dates": [],
            "case_numbers": [],
        },
        "document_type": document_type,
        "date_range": date_range,
        "classification_markings": classification_markings or [],
        "processed_at": datetime.now(timezone.utc),
        "processing_notes": processing_notes,
    }

    try:
        result = db["documents"].insert_one(doc)
        log.info("Ingested document %s -> _id=%s", doc_id, result.inserted_id)
        return str(result.inserted_id)
    except DuplicateKeyError:
        log.warning("Document %s already exists — updating.", doc_id)
        db["documents"].replace_one({"doc_id": doc_id}, doc)
        return doc_id


def ingest_entities(db, doc_id: str, extracted_entities: dict) -> int:
    """
    Ingest extracted entities into the entities collection.
    Returns count of entities inserted.
    """
    records = []
    for entity_type, items in extracted_entities.items():
        if entity_type == "case_numbers":
            for cn in items:
                records.append(
                    {
                        "name": cn,
                        "entity_type": "case_number",
                        "source_doc_id": doc_id,
                        "pages": [],
                        "confidence": 1.0,
                        "context": "",
                    }
                )
        else:
            for item in items:
                if isinstance(item, dict):
                    records.append(
                        {
                            "name": item.get("name", item.get("date", "")),
                            "entity_type": entity_type.rstrip("s"),  # people -> person
                            "source_doc_id": doc_id,
                            "pages": item.get("pages", []),
                            "confidence": item.get("confidence", 0.0),
                            "context": item.get("context", ""),
                        }
                    )

    if records:
        db["entities"].insert_many(records)
    log.info("Ingested %d entities for %s", len(records), doc_id)
    return len(records)


def ingest_redactions(db, doc_id: str, redaction_analysis: dict) -> int:
    """
    Ingest page-level redaction data into the redactions collection.
    Returns count of redaction records inserted.
    """
    records = []
    foia_codes = redaction_analysis.get("foia_codes", {})
    pages = redaction_analysis.get("pages_with_redactions", [])

    # One record per (doc, page, foia_code) if we have granular data
    if foia_codes:
        for code, count in foia_codes.items():
            records.append(
                {
                    "doc_id": doc_id,
                    "foia_code": code,
                    "instance_count": count,
                    "pages_with_redactions": pages,
                    "inferred_type": _infer_redaction_type(code),
                    "density": redaction_analysis.get("redaction_density", 0.0),
                }
            )
    elif pages:
        records.append(
            {
                "doc_id": doc_id,
                "foia_code": "visual_redaction",
                "instance_count": len(pages),
                "pages_with_redactions": pages,
                "inferred_type": "unknown",
                "density": redaction_analysis.get("redaction_density", 0.0),
            }
        )

    if records:
        db["redactions"].insert_many(records)
    log.info("Ingested %d redaction records for %s", len(records), doc_id)
    return len(records)


def _infer_redaction_type(foia_code: str) -> str:
    """Map FOIA exemption codes to human-readable redaction categories."""
    mapping = {
        "(b)(6)": "personal_privacy",
        "(b)(7)(C)": "law_enforcement_personal_privacy",
        "(b)(7)(E)": "law_enforcement_technique",
        "(b)(7)(F)": "endanger_safety",
        "(b)(7)(A)": "pending_enforcement",
        "(b)(7)(D)": "confidential_source",
        "(b)(1)": "national_security",
        "(b)(3)": "statutory_exemption",
        "(b)(4)": "trade_secrets",
        "(b)(5)": "deliberative_process",
    }
    for code_prefix, category in mapping.items():
        if foia_code.startswith(code_prefix) or code_prefix in foia_code:
            return category
    return "unknown"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_setup(client: Optional[MongoClient] = None) -> None:
    """Print current state of the database and indexes."""
    client = client or get_client()
    db = client[DATABASE_NAME]

    print(f"\nDatabase: {DATABASE_NAME}")
    print(f"Collections: {db.list_collection_names()}")

    for coll_name in COLLECTIONS:
        coll = db[coll_name]
        indexes = list(coll.list_indexes())
        count = coll.count_documents({})
        print(f"\n  {coll_name}: {count} documents, {len(indexes)} indexes")
        for idx in indexes:
            print(f"    - {idx['name']}: {idx['key']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 setup_mongodb.py [--setup | --verify]")
        print("  --setup   Create database, collections, and indexes")
        print("  --verify  Show current database state")
        print("\nSet MONGODB_URI environment variable before running.")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "--setup":
        client = get_client()
        setup_database(client)
        verify_setup(client)
    elif cmd == "--verify":
        client = get_client()
        verify_setup(client)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
