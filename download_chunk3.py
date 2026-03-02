#!/usr/bin/env python3
"""
DOJ Epstein Library — Chunk 3 Downloader
==========================================
Downloads court records, FOIA docs, and prior disclosures.
Crawls each section's pages on justice.gov, extracts PDF links, and downloads them.

Usage:
  python3 _pipeline_output/download_chunk3.py
  python3 _pipeline_output/download_chunk3.py --dry-run
  python3 _pipeline_output/download_chunk3.py --section Court_Maxwell_2015
  python3 _pipeline_output/download_chunk3.py --workers 3
"""

import argparse
import json
import os
import sys
import time
import hashlib
import re
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote, urljoin
from html.parser import HTMLParser

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "requests", "--break-system-packages", "-q"])
    import requests

# ═══════════════════════════════════════════
# SECTION DEFINITIONS
# ═══════════════════════════════════════════

SECTIONS = {
    "Court_Maxwell_2015": "/epstein/doj-disclosures/court-records-v-maxwell-no-115-cv-07433-sdny-2015",
    "Court_USVI_JPMorgan": "/epstein/doj-disclosures/court-records-government-united-states-virgin-islands-v-jpmorgan-chase-bank-na-no-122-cv-10904-sdny-2022",
    "Court_Rothstein": "/epstein/doj-disclosures/court-records-epstein-v-rothstein-no-50-2009-ca-040800-xxxx-mb-fla-15th-cir-ct-2009",
    "Court_US_v_Maxwell": "/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-120-cr-00330-sdny-2020",
    "Court_Doe_80119": "/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80119-sd-fla-2008",
    "Court_Doe_US_80736": "/epstein/doj-disclosures/court-records-doe-v-united-states-no-908-cv-80736-sd-fla-2008",
    "Court_FL_Holdings": "/epstein/doj-disclosures/court-records-ca-florida-holdings-llc-publisher-palm-beach-post-v-aronberg-no-50-2019-ca-014681-xxxx-mb",
    "Court_JaneDoe43": "/epstein/doj-disclosures/court-records-jane-doe-43-v-epstein-no-117-cv-00616-sdny-2017",
    "Court_Doe3": "/epstein/doj-disclosures/court-records-doe-no-3-v-epstein-no-908-cv-80232-sd-fla-2008",
    "Court_Doe4": "/epstein/doj-disclosures/court-records-doe-no-4-v-epstein-no-908-cv-80380-sd-fla-2008",
    "Court_Doe5": "/epstein/doj-disclosures/court-records-doe-no-5-v-epstein-no-908-cv-80381-sd-fla-2008",
    "Court_Doe_Indyke_484": "/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-00484-sdny-2020",
    "Court_Doe6": "/epstein/doj-disclosures/court-records-doe-no-6-v-epstein-no-908-cv-80994-sd-fla-2008",
    "Court_Doe_Indyke_8673": "/epstein/doj-disclosures/court-records-doe-v-indyke-no-119-cv-08673-sdny-2019",
    "Court_FL_v_Epstein_2006": "/epstein/doj-disclosures/court-records-state-florida-v-epstein-no-50-2006-cf-009454-axxx-mb-fla-15th-cir-ct-2006",
    "Court_Indyke_10475": "/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10475-sdny-2019",
    "Court_US_v_Epstein": "/epstein/doj-disclosures/court-records-united-states-v-epstein-no-119-cr-00490-sdny-2019",
    "Court_Doe17_Indyke": "/epstein/doj-disclosures/court-records-doe-17-v-indyke-no-119-cv-09610-sdny-2019",
    "Court_Doe1000_Indyke": "/epstein/doj-disclosures/court-records-doe-1000-v-indyke-no-119-cv-10577-sdny-2019",
    "Court_Doe101": "/epstein/doj-disclosures/court-records-doe-no-101-v-epstein-no-909-cv-80591-sd-fla-2009",
    "Court_Doe102": "/epstein/doj-disclosures/court-records-doe-no-102-v-epstein-no-909-cv-80656-sd-fla-2009",
    "Court_Doe103": "/epstein/doj-disclosures/court-records-doe-no-103-v-epstein-no-910-cv-80309-sd-fla-2010",
    "Court_Doe8": "/epstein/doj-disclosures/court-records-doe-no-8-v-epstein-no-909-cv-80802-sd-fla-2009",
    "Court_Doe_80069": "/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80069-sd-fla-2008",
    "Court_Doe_80804": "/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80804-sd-fla-2008",
    "Court_Doe_80469": "/epstein/doj-disclosures/court-records-doe-v-epstein-no-909-v-80469-sd-fla-2009",
    "Court_Doe_Indyke_11869": "/epstein/doj-disclosures/court-records-doe-v-indyke-no-119-cv-11869-sdny-2019",
    "Court_Doe_Indyke_2365": "/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-02365-sdny-2020",
    "Court_Epstein_SupCt": "/epstein/doj-disclosures/court-records-epstein-v-no-sc15-2286-fla-sup-ct-2015",
    "Court_Estate": "/epstein/doj-disclosures/court-records-matter-estate-jeffrey-e-epstein-deceased-no-st-21-rv-00005-vi-super-ct-2021",
    "Court_Maxwell_Estate": "/epstein/doj-disclosures/court-records-maxwell-v-estate-jeffrey-epstein-no-st-20-cv-155-vi-super-ct-2020",
    "Court_Maxwell_Cert": "/epstein/doj-disclosures/court-records-maxwell-v-united-states-no-24-1073-us-2025-petition-cert",
    "Court_GrandJury": "/epstein/doj-disclosures/court-records-re-grand-jury-05-02-wpb-07-103-wpb-no-925-mc-80920-sd-fla-2025",
    "Court_FL_v_Epstein_2008": "/epstein/doj-disclosures/court-records-state-florida-v-epstein-no-50-2008-cf-001828-axxx-mb-fla-15th-cir-ct-2008",
    "FOIA_CBP": "/epstein/doj-disclosures/foia-customs-and-border-protection-cbp",
    "FOIA_FBI": "/epstein/doj-disclosures/foia-federal-bureau-investigation-fbi",
    "FOIA_BOP": "/epstein/doj-disclosures/foia-federal-bureau-prisons-bop",
    "FOIA_Florida": "/epstein/doj-disclosures/foia-florida",
    "Prior_Declassified": "/epstein/doj-disclosures/first-phase-declassified-epstein-files",
    "Prior_BOPVideo": "/epstein/doj-disclosures/bop-video-footage",
    "Prior_MaxwellProffer": "/epstein/doj-disclosures/maxwell-proffer",
    "Prior_Memos": "/epstein/doj-disclosures/memoranda-and-correspondence",
}

# ═══════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════

BASE_URL = "https://www.justice.gov"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "justiceGovAgeVerified=true",
}

DL_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.justice.gov/epstein/doj-disclosures",
    "Cookie": "justiceGovAgeVerified=true",
}


# ═══════════════════════════════════════════
# PDF LINK EXTRACTOR
# ═══════════════════════════════════════════

class PDFLinkExtractor(HTMLParser):
    """Extract PDF links from HTML."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if ".pdf" in href.lower():
                if not href.startswith("http"):
                    href = BASE_URL + href
                self.links.append(href)


def extract_pdf_links(html):
    """Extract all PDF links from an HTML page."""
    parser = PDFLinkExtractor()
    parser.feed(html)
    return parser.links


def crawl_section(session, section_name, section_path):
    """Crawl all pages of a section and return PDF file info list."""
    files = []
    seen_urls = set()
    page = 0

    while True:
        url = f"{BASE_URL}{section_path}?page={page}"
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                break

            links = extract_pdf_links(resp.text)
            page_count = 0
            for link in links:
                if link not in seen_urls:
                    seen_urls.add(link)
                    filename = unquote(link.split("/")[-1])
                    files.append({
                        "url": link,
                        "filename": filename,
                        "section": section_name,
                    })
                    page_count += 1

            # If we got fewer than 50 unique links, this is the last page
            if page_count < 50:
                break

            page += 1
            time.sleep(0.3)  # Be polite

            if page > 500:  # Safety limit
                print(f"  WARNING: Hit 500-page limit for {section_name}")
                break

        except Exception as e:
            print(f"  ERROR crawling {section_name} page {page}: {e}")
            break

    return files


# ═══════════════════════════════════════════
# DOWNLOADER
# ═══════════════════════════════════════════

def download_file(session, file_info, dest_dir, max_retries=3):
    """Download a single file with retry logic."""
    section = file_info["section"]
    url = file_info["url"]
    filename = file_info["filename"]

    # Clean filename
    filename = filename.replace("%20", " ").replace("?", "_").replace("&", "_")

    section_dir = Path(dest_dir) / section
    section_dir.mkdir(parents=True, exist_ok=True)
    filepath = section_dir / filename

    # Skip if already downloaded and valid
    if filepath.exists():
        size = filepath.stat().st_size
        if size > 1024:
            try:
                with open(filepath, "rb") as f:
                    if f.read(5) == b"%PDF-":
                        return "skipped"
            except:
                pass

    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=DL_HEADERS, timeout=60, stream=True)
            if resp.status_code == 200:
                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=16384):
                        f.write(chunk)

                # Validate
                size = filepath.stat().st_size
                if size > 0:
                    with open(filepath, "rb") as f:
                        header = f.read(5)
                    if header == b"%PDF-":
                        return "downloaded"
                    else:
                        # Not a PDF — might be age gate or HTML
                        if attempt < max_retries - 1:
                            filepath.unlink(missing_ok=True)
                            time.sleep(2)
                            continue
                        return "failed_not_pdf"
            elif resp.status_code == 404:
                return "failed_404"
            else:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return f"failed_{resp.status_code}"
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return f"failed_error"

    return "failed"


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="DOJ Epstein Chunk 3 Downloader")
    parser.add_argument("--dest-dir", default="./doj_full_library",
                       help="Destination directory (default: ./doj_full_library)")
    parser.add_argument("--section", help="Only download this section")
    parser.add_argument("--dry-run", action="store_true",
                       help="Crawl and count files without downloading")
    parser.add_argument("--workers", type=int, default=2,
                       help="Concurrent download threads (default: 2, max 4)")
    parser.add_argument("--delay", type=float, default=0.3,
                       help="Seconds between downloads per worker (default: 0.3)")
    parser.add_argument("--skip-crawl", action="store_true",
                       help="Use cached manifest instead of re-crawling")
    args = parser.parse_args()
    args.workers = min(args.workers, 4)

    manifest_cache = Path(args.dest_dir).parent / "_pipeline_output" / "doj_chunk3_manifest.json"

    sections = SECTIONS
    if args.section:
        if args.section not in sections:
            print(f"ERROR: Section '{args.section}' not found.")
            print(f"Available: {', '.join(sorted(sections.keys()))}")
            sys.exit(1)
        sections = {args.section: sections[args.section]}

    # Phase 1: Crawl pages to discover files
    if args.skip_crawl and manifest_cache.exists():
        print("Loading cached manifest...")
        with open(manifest_cache) as f:
            all_files = json.load(f)
        if args.section:
            all_files = [f for f in all_files if f["section"] == args.section]
    else:
        print(f"\n{'='*60}")
        print(f"  PHASE 1: Crawling {len(sections)} sections for PDF links")
        print(f"{'='*60}\n")

        session = requests.Session()
        all_files = []

        for i, (name, path) in enumerate(sections.items(), 1):
            print(f"  [{i}/{len(sections)}] {name}...", end=" ", flush=True)
            files = crawl_section(session, name, path)
            print(f"{len(files)} files")
            all_files.extend(files)

        print(f"\n  Total files discovered: {len(all_files):,}")

        # Cache the manifest
        manifest_cache.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_cache, "w") as f:
            json.dump(all_files, f)
        print(f"  Manifest saved to {manifest_cache}")

    if args.dry_run:
        print(f"\n  DRY RUN — File counts by section:")
        by_section = defaultdict(int)
        for f in all_files:
            by_section[f["section"]] += 1
        for section, count in sorted(by_section.items(), key=lambda x: -x[1]):
            print(f"    {section}: {count}")
        print(f"\n  Total: {len(all_files):,} files")
        return

    # Phase 2: Download
    print(f"\n{'='*60}")
    print(f"  PHASE 2: Downloading {len(all_files):,} files")
    print(f"  Destination: {args.dest_dir}")
    print(f"  Workers: {args.workers}")
    print(f"{'='*60}\n")

    session = requests.Session()
    stats = defaultdict(int)
    section_stats = defaultdict(lambda: defaultdict(int))
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for file_info in all_files:
            future = executor.submit(download_file, session, file_info, args.dest_dir)
            futures[future] = file_info

        completed = 0
        for future in as_completed(futures):
            file_info = futures[future]
            result = future.result()
            stats[result] += 1
            section_stats[file_info["section"]][result] += 1
            completed += 1

            if completed % 50 == 0 or completed == len(all_files):
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                print(f"  Progress: {completed:,}/{len(all_files):,} "
                      f"({completed/len(all_files)*100:.1f}%) "
                      f"| {rate:.1f} files/sec "
                      f"| dl={stats['downloaded']} skip={stats['skipped']} "
                      f"fail={sum(v for k,v in stats.items() if k.startswith('fail'))}")

            time.sleep(args.delay)

    # Report
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  DOWNLOAD COMPLETE")
    print(f"{'='*60}")
    print(f"  Downloaded:  {stats['downloaded']:,}")
    print(f"  Skipped:     {stats['skipped']:,}")
    print(f"  Failed:      {sum(v for k,v in stats.items() if k.startswith('fail')):,}")
    print(f"  Time:        {elapsed/60:.1f} minutes")
    print(f"{'='*60}\n")

    # Save report
    report = {
        "downloaded": stats["downloaded"],
        "skipped": stats["skipped"],
        "failed": sum(v for k, v in stats.items() if k.startswith("fail")),
        "duration_minutes": round(elapsed / 60, 1),
        "section_counts": {
            s: dict(counts) for s, counts in sorted(section_stats.items())
        },
    }
    report_path = Path(args.dest_dir) / "_chunk3_download_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Report saved to {report_path}")


if __name__ == "__main__":
    main()
