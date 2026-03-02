#!/usr/bin/env python3
"""
DOJ Library — Phase A: Bulk Metadata Ingestion into MongoDB
=============================================================
Ingests basic metadata for all PDFs into MongoDB without requiring PageIndex.
This creates the document inventory so queries can begin immediately.

Each document gets: filename, section, file_size, sha256, page_count, doc_id.
PageIndex trees and entity extraction are added later in Phase B.

Usage:
  export MONGODB_URI="mongodb+srv://efinder-db:PASSWORD@e-cluster0.ulpu7g.mongodb.net/?retryWrites=true&w=majority&appName=e-Cluster0"
  python3 _pipeline_output/ingest_metadata.py

Options:
  --library-dir DIR   Path to doj_full_library (default: ./doj_full_library)
  --batch-size N      Documents per batch insert (default: 500)
  --dry-run           Count files without ingesting
  --section NAME      Only ingest this section
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

try:
    from pymongo import MongoClient, ASCENDING, TEXT
    from pymongo.errors import (
        ConnectionFailure,
        ServerSelectionTimeoutError,
        BulkWriteError,
    )
except ImportError:
    print("Installing pymongo...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient, ASCENDING, TEXT
    from pymongo.errors import (
        ConnectionFailure,
        ServerSelectionTimeoutError,
        BulkWriteError,
    )

# Optional: faster page counting
try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

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


def get_page_count(filepath):
    """Get PDF page count. Uses PyMuPDF if available, else binary scan."""
    if HAS_PYMUPDF:
        try:
            doc = fitz.open(filepath)
            count = doc.page_count
            doc.close()
            return count
        except:
            return None

    # Fallback: scan binary for /Type /Page entries (rough estimate)
    try:
        with open(filepath, "rb") as f:
            content = f.read()
        # Count /Type /Page (but not /Type /Pages)
        import re
        pages = re.findall(rb"/Type\s*/Page(?!s)", content)
        return len(pages) if pages else None
    except:
        return None


def sha256_file(filepath):
    """Compute SHA-256 hash."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except:
        return None


def build_doc_id(filename, section):
    """Generate a doc_id from filename. Use EFTA number if present, else section/filename."""
    name = filename.replace(".pdf", "").replace(".PDF", "")
    if name.startswith("EFTA"):
        return name  # e.g., "EFTA02846457"
    return f"{section}/{name}"


def scan_library(library_dir, section_filter=None):
    """Scan library directory and return list of file metadata dicts."""
    library = Path(library_dir)
    files = []

    for section_dir in sorted(library.iterdir()):
        if not section_dir.is_dir() or section_dir.name.startswith("_"):
            continue
        if section_filter and section_dir.name != section_filter:
            continue

        section = section_dir.name
        for pdf in sorted(section_dir.glob("*.pdf")):
            files.append({
                "path": str(pdf),
                "filename": pdf.name,
                "section": section,
                "file_size": pdf.stat().st_size,
            })

    return files


def main():
    parser = argparse.ArgumentParser(description="Ingest PDF metadata into MongoDB")
    parser.add_argument("--library-dir", default="./doj_full_library",
                       help="Path to doj_full_library")
    parser.add_argument("--batch-size", type=int, default=500,
                       help="Documents per batch insert")
    parser.add_argument("--dry-run", action="store_true",
                       help="Scan files without ingesting")
    parser.add_argument("--section", help="Only ingest this section")
    parser.add_argument("--skip-hashes", action="store_true",
                       help="Skip SHA-256 computation (faster)")
    parser.add_argument("--skip-pages", action="store_true",
                       help="Skip page count extraction (faster)")
    args = parser.parse_args()

    # Scan library
    print(f"\n{'='*60}")
    print(f"  DOJ LIBRARY — METADATA INGESTION")
    print(f"{'='*60}\n")

    print(f"  Scanning {args.library_dir}...")
    files = scan_library(args.library_dir, args.section)
    print(f"  Found {len(files):,} PDF files across {len(set(f['section'] for f in files))} sections\n")

    if not files:
        print("  No files found!")
        return

    if args.dry_run:
        by_section = defaultdict(int)
        for f in files:
            by_section[f["section"]] += 1
        for section, count in sorted(by_section.items()):
            print(f"    {section}: {count:,}")
        print(f"\n  Total: {len(files):,} files")
        return

    # Connect to MongoDB
    if "<db_password>" in MONGODB_URI:
        print("  ERROR: Set MONGODB_URI environment variable with your password.")
        print('  export MONGODB_URI="mongodb+srv://efinder-db:PASSWORD@e-cluster0.ulpu7g.mongodb.net/..."')
        sys.exit(1)

    print("  Connecting to MongoDB...")
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
        print("  Connected successfully.\n")
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        print(f"  ERROR: Could not connect: {e}")
        sys.exit(1)

    db = client[DATABASE_NAME]
    coll = db["documents"]

    # Check what's already ingested
    existing_count = coll.count_documents({})
    if existing_count > 0:
        print(f"  Note: {existing_count:,} documents already in database.")
        print(f"  Will skip duplicates (upsert by doc_id).\n")

    # Process files
    batch = []
    stats = {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    start_time = time.time()

    for i, file_info in enumerate(files):
        doc_id = build_doc_id(file_info["filename"], file_info["section"])

        doc = {
            "doc_id": doc_id,
            "filename": file_info["filename"],
            "section": file_info["section"],
            "source": "DOJ Epstein Document Library",
            "file_type": "pdf",
            "file_size_bytes": file_info["file_size"],
            "sha256": None,
            "total_pages": None,
            "page_index_tree": {},
            "redaction_analysis": {
                "has_redactions": False,
                "pages_with_redactions": [],
                "redaction_density": 0.0,
                "redaction_types": [],
                "foia_codes": {},
            },
            "extracted_entities": {
                "people": [],
                "organizations": [],
                "locations": [],
                "dates": [],
                "case_numbers": [],
            },
            "document_type": "unknown",
            "date_range": None,
            "classification_markings": [],
            "processed_at": datetime.now(timezone.utc),
            "processing_stage": "metadata_only",
            "processing_notes": "",
        }

        # Compute hash
        if not args.skip_hashes:
            doc["sha256"] = sha256_file(file_info["path"])

        # Get page count
        if not args.skip_pages:
            doc["total_pages"] = get_page_count(file_info["path"])

        batch.append(doc)

        # Flush batch
        if len(batch) >= args.batch_size:
            _flush_batch(coll, batch, stats)
            batch = []

            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed
            remaining = (len(files) - i - 1) / rate if rate > 0 else 0
            print(f"  Progress: {i+1:,}/{len(files):,} "
                  f"({(i+1)/len(files)*100:.1f}%) "
                  f"| {rate:.0f} files/sec "
                  f"| ~{remaining:.0f}s remaining "
                  f"| ins={stats['inserted']} upd={stats['updated']}")

    # Flush remaining
    if batch:
        _flush_batch(coll, batch, stats)

    elapsed = time.time() - start_time

    print(f"\n{'='*60}")
    print(f"  INGESTION COMPLETE")
    print(f"{'='*60}")
    print(f"  Inserted:  {stats['inserted']:,}")
    print(f"  Updated:   {stats['updated']:,}")
    print(f"  Errors:    {stats['errors']:,}")
    print(f"  Time:      {elapsed:.0f} seconds ({elapsed/60:.1f} min)")
    print(f"  Rate:      {len(files)/elapsed:.0f} files/sec")
    print(f"{'='*60}")

    # Verify
    total = coll.count_documents({})
    print(f"\n  Total documents in MongoDB: {total:,}")

    # Section breakdown
    pipeline = [
        {"$group": {"_id": "$section", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    print(f"\n  Documents by section:")
    for result in coll.aggregate(pipeline):
        print(f"    {result['_id']}: {result['count']:,}")


def _flush_batch(coll, batch, stats):
    """Upsert a batch of documents."""
    from pymongo import UpdateOne

    operations = []
    for doc in batch:
        operations.append(
            UpdateOne(
                {"doc_id": doc["doc_id"]},
                {"$set": doc},
                upsert=True,
            )
        )

    try:
        result = coll.bulk_write(operations, ordered=False)
        stats["inserted"] += result.upserted_count
        stats["updated"] += result.modified_count
    except BulkWriteError as e:
        stats["errors"] += len(e.details.get("writeErrors", []))
        log.warning("Bulk write had %d errors", len(e.details.get("writeErrors", [])))


if __name__ == "__main__":
    main()
