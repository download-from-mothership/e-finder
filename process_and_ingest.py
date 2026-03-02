#!/usr/bin/env python3
"""
process_and_ingest.py — Batch processing pipeline for DOJ Document Investigation

Iterates through all files in the E-FINDER folder, runs PageIndex (if available),
extracts entities and redaction metadata, and ingests everything into MongoDB.

Requires:
    pip install pymongo pypdf pdfplumber beautifulsoup4

Usage:
    export MONGODB_URI="mongodb+srv://..."
    export CHATGPT_API_KEY="..."   # For PageIndex (optional)

    python3 process_and_ingest.py                    # Process all files
    python3 process_and_ingest.py --dry-run          # Preview without ingesting
    python3 process_and_ingest.py --file EFTA02847772.pdf  # Process single file
    python3 process_and_ingest.py --skip-pageindex   # Skip PageIndex step
"""

import os
import sys
import re
import json
import logging
import argparse
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from pypdf import PdfReader
except ImportError:
    print("ERROR: pypdf not installed. Run: pip install pypdf")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("WARNING: beautifulsoup4 not installed — HTML parsing will be limited.")

# Import MongoDB functions from setup script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup_mongodb import (
    get_client,
    get_database,
    ingest_document,
    ingest_entities,
    ingest_redactions,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.environ.get(
    "DOJ_FILES_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
OUTPUT_DIR = os.path.join(BASE_DIR, "_pipeline_output")
PAGEINDEX_DIR = os.path.join(OUTPUT_DIR, "pageindex_trees")
PAGEINDEX_REPO = os.environ.get("PAGEINDEX_PATH", "")  # Path to cloned PageIndex repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(OUTPUT_DIR, "processing.log")),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FOIA Redaction Detection
# ---------------------------------------------------------------------------

FOIA_PATTERN = re.compile(r"\(b\)\s*\((\d+)\)\s*(?:\((\w)\))?")


def detect_redactions_pdf(filepath: str, max_pages: int = 50) -> dict:
    """Scan a PDF for FOIA exemption codes and return redaction analysis."""
    try:
        reader = PdfReader(filepath)
        total_pages = len(reader.pages)
        scan_pages = min(total_pages, max_pages)

        foia_codes = {}
        pages_with_redactions = []
        total_instances = 0

        for i in range(scan_pages):
            text = reader.pages[i].extract_text() or ""
            matches = FOIA_PATTERN.findall(text)
            if matches:
                pages_with_redactions.append(i + 1)
                for m in matches:
                    code = f"(b)({m[0]})" + (f"({m[1]})" if m[1] else "")
                    foia_codes[code] = foia_codes.get(code, 0) + 1
                    total_instances += 1

            # Also check for explicit redaction text
            if any(
                term in text.upper()
                for term in [
                    "REDACTED",
                    "REDACTION",
                    "[SEALED]",
                    "REDACTED TO PROTECT",
                ]
            ):
                if (i + 1) not in pages_with_redactions:
                    pages_with_redactions.append(i + 1)

        density = len(pages_with_redactions) / scan_pages if scan_pages > 0 else 0.0

        # Infer redaction types from FOIA codes
        redaction_types = set()
        for code in foia_codes:
            if "(6)" in code:
                redaction_types.add("person_name")
            if "(7)(C)" in code:
                redaction_types.add("person_name")
            if "(7)(E)" in code:
                redaction_types.add("law_enforcement_technique")
            if "(7)(F)" in code:
                redaction_types.add("safety_concern")

        return {
            "has_redactions": bool(pages_with_redactions),
            "pages_with_redactions": sorted(pages_with_redactions),
            "redaction_density": round(density, 4),
            "redaction_types": list(redaction_types),
            "foia_codes": foia_codes,
            "total_instances": total_instances,
            "pages_scanned": scan_pages,
            "total_pages": total_pages,
        }

    except Exception as e:
        log.error("Redaction detection failed for %s: %s", filepath, e)
        return {
            "has_redactions": False,
            "pages_with_redactions": [],
            "redaction_density": 0.0,
            "redaction_types": [],
            "foia_codes": {},
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Entity Extraction
# ---------------------------------------------------------------------------


def extract_entities_pdf(filepath: str, max_pages: int = 30) -> dict:
    """Extract named entities from PDF text using regex patterns."""
    entities = {
        "people": [],
        "organizations": [],
        "locations": [],
        "dates": [],
        "case_numbers": [],
    }

    try:
        reader = PdfReader(filepath)
        scan_pages = min(len(reader.pages), max_pages)
        all_text = ""

        for i in range(scan_pages):
            text = reader.pages[i].extract_text() or ""
            all_text += text + "\n"

        # --- People ---
        # Known names from the Epstein case context
        known_names = [
            "Jeffrey Epstein",
            "Jeffrey Edward Epstein",
            "Ghislaine Maxwell",
            "Darren Indyke",
            "Darren K Indyke",
        ]
        for name in known_names:
            if name.upper() in all_text.upper():
                pages = []
                for i in range(scan_pages):
                    pg_text = reader.pages[i].extract_text() or ""
                    if name.upper() in pg_text.upper():
                        pages.append(i + 1)
                entities["people"].append(
                    {"name": name, "pages": pages, "confidence": 0.95}
                )

        # --- Organizations ---
        org_patterns = [
            r"(?:CBP|DHS|FBI|DOJ|SDNY|USAO)",
            r"Hyperion Air(?:\s+Inc)?",
            r"JPMorgan Chase",
            r"U\.S\.\s+Customs\s+and\s+Border\s+Protection",
        ]
        for pattern in org_patterns:
            matches = re.findall(pattern, all_text, re.IGNORECASE)
            if matches:
                name = matches[0].strip()
                entities["organizations"].append(
                    {"name": name, "pages": [], "confidence": 0.8}
                )

        # --- Locations ---
        location_patterns = [
            r"Palm Beach",
            r"Teterboro",
            r"St\.?\s*Thomas",
            r"Little St\.?\s*James",
            r"301 East 66th Street",
            r"Red Hook Quarter",
            r"Paris",
            r"London",
        ]
        for pattern in location_patterns:
            if re.search(pattern, all_text, re.IGNORECASE):
                entities["locations"].append(
                    {
                        "name": re.search(pattern, all_text, re.IGNORECASE).group(),
                        "pages": [],
                        "confidence": 0.85,
                    }
                )

        # --- Dates ---
        # MM/DD/YYYY format
        date_matches = re.findall(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", all_text)
        for d in set(date_matches[:20]):
            entities["dates"].append({"date": d, "pages": [], "context": ""})

        # Month DD, YYYY format
        date_matches2 = re.findall(
            r"\b((?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},?\s+\d{4})\b",
            all_text,
        )
        for d in set(date_matches2[:20]):
            entities["dates"].append({"date": d, "pages": [], "context": ""})

        # --- Case Numbers ---
        case_patterns = [
            r"\b(\d{1,2}:\d{2}-(?:cv|cr|mc|mj)-\d{3,6})\b",
            r"\b(CBP-\d{4}-\d{6})\b",
        ]
        for pattern in case_patterns:
            matches = re.findall(pattern, all_text, re.IGNORECASE)
            entities["case_numbers"].extend(list(set(matches)))

    except Exception as e:
        log.error("Entity extraction failed for %s: %s", filepath, e)

    return entities


def extract_entities_html(filepath: str) -> dict:
    """Extract entities from HTML court record pages."""
    entities = {
        "people": [],
        "organizations": [],
        "locations": [],
        "dates": [],
        "case_numbers": [],
    }

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Extract case number from filename
        fn = os.path.basename(filepath)
        case_match = re.search(
            r"no-(\d+-\w+-\d+)", fn.replace("court-records-", "")
        )
        if case_match:
            entities["case_numbers"].append(case_match.group(1))

        # Extract parties from filename
        fn_parts = fn.replace("court-records-", "").replace(".html", "")
        if "-v-" in fn_parts:
            parties = fn_parts.split("-v-")
            for party in parties:
                # Clean up party name
                name = party.split("-no-")[0].replace("-", " ").title().strip()
                if len(name) > 2 and name.lower() not in ["doe", "united states"]:
                    entities["people"].append(
                        {"name": name, "pages": [], "confidence": 0.7}
                    )

        # Extract court from filename
        court_patterns = {
            "sdny": "S.D.N.Y.",
            "sd-fla": "S.D. Fla.",
            "2d-cir": "2d Circuit",
            "fla-15th": "Florida 15th Circuit",
            "vi-super": "V.I. Superior Court",
        }
        for pattern, court in court_patterns.items():
            if pattern in fn:
                entities["organizations"].append(
                    {"name": court, "pages": [], "confidence": 0.9}
                )

        # Extract year
        year_match = re.findall(r"(\d{4})", fn)
        if year_match:
            entities["dates"].append(
                {"date": year_match[-1], "pages": [], "context": "filing year"}
            )

    except Exception as e:
        log.error("HTML entity extraction failed for %s: %s", filepath, e)

    return entities


# ---------------------------------------------------------------------------
# Document Type Classification
# ---------------------------------------------------------------------------


def classify_document(filename: str, first_page_text: str = "") -> str:
    """Classify document type based on filename and content."""
    fn_lower = filename.lower()
    text_lower = first_page_text.lower()

    if fn_lower.startswith("court-records"):
        return "court_record"

    # PDF classification based on content
    if "evidence" in text_lower or "item" in text_lower and "seized" in text_lower:
        return "evidence_inventory"
    if "tecs" in text_lower and "traveler" in text_lower:
        return "travel_record"
    if "tecs" in text_lower and "query" in text_lower:
        return "query_history"
    if "secondary inspection" in text_lower or "inspection" in text_lower:
        return "inspection_record"
    if "masseuse" in text_lower:
        return "victim_list"
    if "government exhibit" in text_lower:
        return "government_exhibit"
    if "foia" in text_lower or "tracking number" in text_lower:
        return "foia_tracking"
    if "cf-178" in text_lower or "private aircraft" in text_lower:
        return "aircraft_report"
    if any(x in text_lower for x in ["booking", "pnr", "recloc", "fare"]):
        return "booking_record"
    if "redaction" in text_lower and "contact" in text_lower:
        return "contact_list"

    return "unknown"


# ---------------------------------------------------------------------------
# PageIndex Integration
# ---------------------------------------------------------------------------


def run_pageindex(filepath: str, output_dir: str) -> Optional[dict]:
    """Run PageIndex on a PDF and return the tree JSON. Returns None if PageIndex unavailable."""
    if not PAGEINDEX_REPO or not os.path.exists(PAGEINDEX_REPO):
        log.warning(
            "PageIndex not available at %s — skipping tree generation.",
            PAGEINDEX_REPO,
        )
        return None

    output_name = Path(filepath).stem + "_pageindex.json"
    output_path = os.path.join(output_dir, output_name)

    try:
        cmd = [
            "python3",
            os.path.join(PAGEINDEX_REPO, "run_pageindex.py"),
            "--pdf_path",
            filepath,
            "--max-pages-per-node",
            "10",
            "--if-add-node-summary",
            "yes",
            "--if-add-doc-description",
            "yes",
        ]

        log.info("Running PageIndex on %s...", os.path.basename(filepath))
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )

        if result.returncode != 0:
            log.error("PageIndex failed: %s", result.stderr[:500])
            return None

        # PageIndex outputs to a default location — find and load it
        # (adjust this path based on PageIndex's actual output behavior)
        if os.path.exists(output_path):
            with open(output_path) as f:
                return json.load(f)
        else:
            log.warning("PageIndex output not found at %s", output_path)
            return None

    except subprocess.TimeoutExpired:
        log.error("PageIndex timed out on %s", filepath)
        return None
    except Exception as e:
        log.error("PageIndex error on %s: %s", filepath, e)
        return None


# ---------------------------------------------------------------------------
# Main Processing Pipeline
# ---------------------------------------------------------------------------


def process_file(filepath: str, db=None, dry_run: bool = False, skip_pageindex: bool = False) -> dict:
    """Process a single file: extract metadata, entities, redactions, run PageIndex."""
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    file_size = os.path.getsize(filepath)

    log.info("Processing: %s (%s)", filename, f"{file_size / 1024 / 1024:.2f} MB")

    # Generate doc_id from Bates number or filename hash
    bates_match = re.match(r"(EFTA\d+)", filename)
    doc_id = bates_match.group(1) if bates_match else hashlib.md5(filename.encode()).hexdigest()[:16]

    result = {
        "doc_id": doc_id,
        "filename": filename,
        "file_type": ext.lstrip("."),
        "file_size_bytes": file_size,
        "total_pages": 0,
        "document_type": "unknown",
        "redaction_analysis": {},
        "extracted_entities": {},
        "page_index_tree": {},
        "classification_markings": [],
        "processing_notes": "",
    }

    if ext == ".pdf":
        # Get page count
        try:
            reader = PdfReader(filepath)
            result["total_pages"] = len(reader.pages)
            first_text = reader.pages[0].extract_text() or "" if reader.pages else ""
        except Exception as e:
            result["processing_notes"] = f"PDF read error: {e}"
            first_text = ""

        # Classify document
        result["document_type"] = classify_document(filename, first_text)

        # Check for classification markings
        if "UNCLASSIFIED" in first_text.upper():
            result["classification_markings"].append("UNCLASSIFIED // FOR OFFICIAL USE ONLY")

        # Detect redactions
        result["redaction_analysis"] = detect_redactions_pdf(filepath)

        # Extract entities
        result["extracted_entities"] = extract_entities_pdf(filepath)

        # Run PageIndex (if available and not skipped)
        if not skip_pageindex:
            tree = run_pageindex(filepath, PAGEINDEX_DIR)
            if tree:
                result["page_index_tree"] = tree

    elif ext in (".html", ".htm"):
        result["total_pages"] = 1
        result["document_type"] = "court_record"
        result["extracted_entities"] = extract_entities_html(filepath)
        result["redaction_analysis"] = {
            "has_redactions": False,
            "pages_with_redactions": [],
            "redaction_density": 0.0,
            "redaction_types": [],
            "foia_codes": {},
        }

    else:
        result["processing_notes"] = f"Unsupported file type: {ext}"

    # Ingest into MongoDB
    if db and not dry_run:
        try:
            ingest_document(db, **{k: v for k, v in result.items() if k != "extracted_entities"})
            if result["extracted_entities"]:
                ingest_entities(db, doc_id, result["extracted_entities"])
            if result["redaction_analysis"].get("has_redactions"):
                ingest_redactions(db, doc_id, result["redaction_analysis"])
        except Exception as e:
            log.error("Ingestion failed for %s: %s", filename, e)
            result["processing_notes"] += f" | Ingestion error: {e}"

    return result


def process_all(
    base_dir: str = BASE_DIR,
    dry_run: bool = False,
    target_file: Optional[str] = None,
    skip_pageindex: bool = False,
) -> list:
    """Process all files in the base directory."""

    # Connect to MongoDB (unless dry run)
    db = None
    if not dry_run:
        try:
            client = get_client()
            db = client["doj_investigation"]
        except SystemExit:
            log.warning("MongoDB not available — running in dry-run mode.")
            dry_run = True

    results = []
    errors = []

    # Collect files
    files = []
    for root, dirs, filenames in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d != "_pipeline_output"]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            if target_file and fn != target_file:
                continue
            files.append(os.path.join(root, fn))

    log.info("Found %d files to process.", len(files))

    for i, filepath in enumerate(files, 1):
        log.info("--- [%d/%d] ---", i, len(files))
        try:
            result = process_file(filepath, db=db, dry_run=dry_run, skip_pageindex=skip_pageindex)
            results.append(result)
        except Exception as e:
            log.error("FATAL error processing %s: %s", filepath, e)
            errors.append({"file": filepath, "error": str(e)})

    # Save processing results
    output_path = os.path.join(OUTPUT_DIR, "processing_results.json")
    with open(output_path, "w") as f:
        json.dump(
            {
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "total_files": len(files),
                "successful": len(results),
                "errors": len(errors),
                "dry_run": dry_run,
                "results": results,
                "error_log": errors,
            },
            f,
            indent=2,
            default=str,
        )

    log.info(
        "Processing complete: %d successful, %d errors. Results saved to %s",
        len(results),
        len(errors),
        output_path,
    )

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DOJ Document Processing Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Preview without ingesting to MongoDB")
    parser.add_argument("--file", type=str, help="Process a single file by name")
    parser.add_argument("--skip-pageindex", action="store_true", help="Skip PageIndex step")
    parser.add_argument("--base-dir", type=str, default=BASE_DIR, help="Base directory of DOJ files")

    args = parser.parse_args()

    process_all(
        base_dir=args.base_dir,
        dry_run=args.dry_run,
        target_file=args.file,
        skip_pageindex=args.skip_pageindex,
    )
