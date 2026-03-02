#!/usr/bin/env python3
"""
download_all_pdfs.py — Download all PDFs from DOJ Epstein court record pages

This script:
1. Reads all saved HTML court record index pages from the E-FINDER folder
2. Visits the live DOJ.gov pages to check for additional paginated results
3. Downloads all linked PDFs that aren't already in the folder
4. Handles rate limiting, retries, and resume

Requirements:
    pip install requests beautifulsoup4 tqdm

Usage:
    cd /path/to/E-FINDER
    python3 _pipeline_output/download_all_pdfs.py

    # Options:
    python3 _pipeline_output/download_all_pdfs.py --dry-run          # Just list files
    python3 _pipeline_output/download_all_pdfs.py --skip-pagination  # Only use local HTML
    python3 _pipeline_output/download_all_pdfs.py --max-concurrent 3 # Parallel downloads
    python3 _pipeline_output/download_all_pdfs.py --subfolders       # Organize by case
"""

import os
import sys
import re
import json
import time
import hashlib
import argparse
import logging
from pathlib import Path
from urllib.parse import urljoin, unquote, quote

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Install dependencies: pip install requests beautifulsoup4")
    sys.exit(1)

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    print("(Optional: pip install tqdm for progress bars)")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.justice.gov"
EPSTEIN_BASE = "https://www.justice.gov/epstein"

# Rate limiting: be respectful of DOJ servers
REQUEST_DELAY = 3.0        # seconds between page fetches (DOJ blocks fast requests)
DOWNLOAD_DELAY = 1.0       # seconds between PDF downloads
MAX_RETRIES = 3
RETRY_DELAY = 5.0          # seconds before retry
TIMEOUT = 60               # request timeout in seconds

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("_pipeline_output/download.log"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL Extraction
# ---------------------------------------------------------------------------


def extract_pdf_links_from_html(html_content: str, base_url: str = BASE_URL) -> list:
    """Extract all PDF download links from an HTML page."""
    soup = BeautifulSoup(html_content, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if ".pdf" in href.lower():
            # Make absolute
            if href.startswith("/"):
                href = base_url + href
            elif not href.startswith("http"):
                href = urljoin(base_url, href)
            links.append({"filename": text, "url": href})
    return links


def get_pagination_urls(html_content: str, page_url: str) -> list:
    """Extract pagination URLs from a DOJ court records page."""
    soup = BeautifulSoup(html_content, "html.parser")
    pagination_urls = set()

    # Look for pager elements
    pagers = soup.find_all(class_=lambda x: x and "pager" in str(x).lower())
    for pager in pagers:
        for a in pager.find_all("a", href=True):
            href = a["href"]
            if "page=" in href:
                # It's a relative URL like ?page=1
                if href.startswith("?"):
                    base = page_url.split("?")[0]
                    full_url = base + href
                elif href.startswith("/"):
                    full_url = BASE_URL + href
                else:
                    full_url = urljoin(page_url, href)
                pagination_urls.add(full_url)

    return sorted(pagination_urls)


def get_live_page_url(local_filename: str) -> str:
    """Construct the live DOJ URL from a local HTML filename."""
    # Remove .html extension and 'court-records-' prefix
    slug = local_filename.replace(".html", "")

    # The DOJ URL pattern is:
    # https://www.justice.gov/epstein/court-records/[case-name]
    return f"{EPSTEIN_BASE}/court-records/{slug.replace('court-records-', '')}"


# ---------------------------------------------------------------------------
# Download Functions
# ---------------------------------------------------------------------------


def download_file(url: str, dest_path: str, session: requests.Session) -> bool:
    """Download a single file with retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            # Check if file already exists and is non-empty
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                log.debug("Already exists: %s", os.path.basename(dest_path))
                return True

            response = session.get(url, timeout=TIMEOUT, stream=True)
            response.raise_for_status()

            # Get file size for progress
            total_size = int(response.headers.get("content-length", 0))

            # Write to temp file first, then rename (atomic)
            temp_path = dest_path + ".tmp"
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            os.rename(temp_path, dest_path)
            log.info("Downloaded: %s (%s)",
                     os.path.basename(dest_path),
                     f"{total_size / 1024 / 1024:.1f} MB" if total_size > 1024 * 1024
                     else f"{total_size / 1024:.0f} KB")
            return True

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = RETRY_DELAY * (attempt + 2)
                log.warning("Rate limited. Waiting %ds...", wait)
                time.sleep(wait)
            elif e.response.status_code == 404:
                log.error("Not found (404): %s", url)
                return False
            else:
                log.warning("HTTP %d on attempt %d for %s",
                           e.response.status_code, attempt + 1,
                           os.path.basename(dest_path))
                time.sleep(RETRY_DELAY)

        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            log.warning("Connection error on attempt %d: %s", attempt + 1, str(e)[:100])
            time.sleep(RETRY_DELAY * (attempt + 1))

    log.error("Failed after %d attempts: %s", MAX_RETRIES, url)
    return False


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------


def collect_all_pdf_urls(base_dir: str, skip_pagination: bool = False) -> dict:
    """
    Collect all PDF URLs from local HTML files and (optionally) live paginated pages.
    Returns dict mapping filename -> url.
    """
    all_pdfs = {}  # filename -> url
    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: Extract from local HTML files
    html_files = sorted([f for f in os.listdir(base_dir) if f.endswith(".html")])
    log.info("Found %d local HTML files", len(html_files))

    for fn in html_files:
        with open(os.path.join(base_dir, fn), "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        links = extract_pdf_links_from_html(content)
        for link in links:
            all_pdfs[link["filename"]] = link["url"]

        log.info("  %s: %d PDFs (local)", fn[:60], len(links))

    log.info("Total from local HTML: %d unique PDFs", len(all_pdfs))

    # Step 2: Check live pages for pagination (additional pages)
    if not skip_pagination:
        log.info("\nChecking live DOJ pages for additional paginated results...")

        # Warm up session — visit the main epstein page first to get cookies
        try:
            log.info("  Warming up session (visiting main DOJ Epstein page)...")
            warmup = session.get(f"{EPSTEIN_BASE}", timeout=TIMEOUT)
            log.info("  Warmup status: %d", warmup.status_code)
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            log.warning("  Warmup failed: %s (continuing anyway)", str(e)[:100])

        pages_to_check = []

        for fn in html_files:
            with open(os.path.join(base_dir, fn), "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            live_url = get_live_page_url(fn)
            pagination_urls = get_pagination_urls(content, live_url)

            if pagination_urls:
                # We already have page 0 (the local file), so get page 1+
                for purl in pagination_urls:
                    if "page=0" not in purl:
                        pages_to_check.append((fn, purl))

        log.info("Found %d additional paginated pages to fetch", len(pages_to_check))

        for fn, purl in pages_to_check:
            try:
                time.sleep(REQUEST_DELAY)
                log.info("  Fetching: %s", purl)
                resp = session.get(purl, timeout=TIMEOUT)
                resp.raise_for_status()

                new_links = extract_pdf_links_from_html(resp.text)
                new_count = 0
                for link in new_links:
                    if link["filename"] not in all_pdfs:
                        all_pdfs[link["filename"]] = link["url"]
                        new_count += 1

                log.info("    Found %d new PDFs (page: %s)", new_count, purl.split("page=")[-1])

            except Exception as e:
                log.error("    Failed to fetch %s: %s", purl, str(e)[:100])

    log.info("\nTotal unique PDFs to download: %d", len(all_pdfs))
    return all_pdfs


def main():
    parser = argparse.ArgumentParser(description="Download DOJ Epstein case PDFs")
    parser.add_argument("--dry-run", action="store_true",
                       help="List files without downloading")
    parser.add_argument("--skip-pagination", action="store_true",
                       help="Only use local HTML files, skip live page checks")
    parser.add_argument("--subfolders", action="store_true",
                       help="Organize downloads into case subfolders")
    parser.add_argument("--base-dir", type=str, default=".",
                       help="Path to E-FINDER folder (default: current dir)")
    parser.add_argument("--dest-dir", type=str, default=None,
                       help="Download destination (default: base-dir/downloads)")

    args = parser.parse_args()
    base_dir = os.path.abspath(args.base_dir)
    dest_dir = args.dest_dir or os.path.join(base_dir, "downloads")

    log.info("Base directory: %s", base_dir)
    log.info("Download destination: %s", dest_dir)

    # Collect all PDF URLs
    all_pdfs = collect_all_pdf_urls(base_dir, skip_pagination=args.skip_pagination)

    # Filter out already-downloaded files
    os.makedirs(dest_dir, exist_ok=True)
    existing = set(os.listdir(base_dir)) | set(os.listdir(dest_dir))

    to_download = {fn: url for fn, url in all_pdfs.items() if fn not in existing}
    already_have = len(all_pdfs) - len(to_download)

    log.info("\nAlready have: %d", already_have)
    log.info("Need to download: %d", len(to_download))

    if args.dry_run:
        print("\n--- DRY RUN: Files to download ---")
        for fn in sorted(to_download.keys()):
            print(f"  {fn}")
        print(f"\nTotal: {len(to_download)} files")

        # Save download manifest
        manifest_path = os.path.join(base_dir, "_pipeline_output", "download_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(to_download, f, indent=2)
        print(f"Download manifest saved to: {manifest_path}")
        return

    # Download
    session = requests.Session()
    session.headers.update(HEADERS)

    success = 0
    failed = 0
    items = sorted(to_download.items())

    if HAS_TQDM:
        items_iter = tqdm(items, desc="Downloading", unit="file")
    else:
        items_iter = items

    for fn, url in items_iter:
        dest_path = os.path.join(dest_dir, fn)

        if download_file(url, dest_path, session):
            success += 1
        else:
            failed += 1

        time.sleep(DOWNLOAD_DELAY)

    log.info("\n=== Download Complete ===")
    log.info("  Downloaded: %d", success)
    log.info("  Failed: %d", failed)
    log.info("  Already had: %d", already_have)
    log.info("  Total collection: %d", success + already_have)

    # Save results
    results_path = os.path.join(base_dir, "_pipeline_output", "download_results.json")
    with open(results_path, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "downloaded": success,
            "failed": failed,
            "already_had": already_have,
            "total_available": len(all_pdfs),
        }, f, indent=2)


if __name__ == "__main__":
    main()
