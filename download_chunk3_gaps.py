#!/usr/bin/env python3
"""
Re-crawl the 6 sections with gaps using aggressive pagination.
Instead of stopping at < 50 PDFs per page, we check for the actual 'next' pager link.
"""

import json
import os
import sys
import time
import re
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--break-system-packages", "-q"])
    import requests

BASE_URL = "https://www.justice.gov"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Cookie": "justiceGovAgeVerified=true",
}
DL_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    "Referer": "https://www.justice.gov/epstein/doj-disclosures",
    "Cookie": "justiceGovAgeVerified=true",
}

GAP_SECTIONS = {
    "Court_Maxwell_2015": "/epstein/doj-disclosures/court-records-v-maxwell-no-115-cv-07433-sdny-2015",
    "Court_USVI_JPMorgan": "/epstein/doj-disclosures/court-records-government-united-states-virgin-islands-v-jpmorgan-chase-bank-na-no-122-cv-10904-sdny-2022",
    "Court_US_v_Maxwell": "/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-120-cr-00330-sdny-2020",
    "Court_Rothstein": "/epstein/doj-disclosures/court-records-epstein-v-rothstein-no-50-2009-ca-040800-xxxx-mb-fla-15th-cir-ct-2009",
    "Court_Doe_US_80736": "/epstein/doj-disclosures/court-records-doe-v-united-states-no-908-cv-80736-sd-fla-2008",
    "Court_Doe_Indyke_484": "/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-00484-sdny-2020",
}


def crawl_section(session, section_name, section_path):
    """Crawl with aggressive pagination - check for 'next' link in HTML."""
    files = []
    seen_urls = set()
    page = 0

    while True:
        url = f"{BASE_URL}{section_path}?page={page}"
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                break

            html = resp.text

            # Extract PDF links
            pdf_links = re.findall(r'href="([^"]*\.pdf[^"]*)"', html, re.IGNORECASE)
            for href in pdf_links:
                if not href.startswith("http"):
                    href = BASE_URL + href
                if href not in seen_urls:
                    seen_urls.add(href)
                    filename = unquote(href.split("/")[-1].split("?")[0])
                    files.append({"url": href, "filename": filename, "section": section_name})

            # Check for next page: look for ?page=N+1 in the HTML
            has_next = f"page={page + 1}" in html
            if not has_next:
                break

            page += 1
            time.sleep(0.3)

            if page > 500:
                print(f"    WARNING: Safety limit at page {page}")
                break

        except Exception as e:
            print(f"    ERROR on page {page}: {e}")
            break

    return files


def download_file(session, file_info, dest_dir, max_retries=3):
    section = file_info["section"]
    url = file_info["url"]
    filename = file_info["filename"].replace("%20", " ").replace("?", "_").replace("&", "_")

    section_dir = Path(dest_dir) / section
    section_dir.mkdir(parents=True, exist_ok=True)
    filepath = section_dir / filename

    if filepath.exists() and filepath.stat().st_size > 1024:
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
                if filepath.stat().st_size > 0:
                    with open(filepath, "rb") as f:
                        if f.read(5) == b"%PDF-":
                            return "downloaded"
                    if attempt < max_retries - 1:
                        filepath.unlink(missing_ok=True)
                        time.sleep(2)
                        continue
                    return "failed_not_pdf"
            elif resp.status_code == 404:
                return "failed_404"
        except:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
    return "failed"


def main():
    dest_dir = "./doj_full_library"
    session = requests.Session()

    print(f"\n{'='*60}")
    print(f"  CHUNK 3 GAP FILL — Re-crawling 6 sections")
    print(f"{'='*60}\n")

    all_files = []
    for name, path in GAP_SECTIONS.items():
        print(f"  Crawling {name}...", end=" ", flush=True)
        files = crawl_section(session, name, path)
        print(f"{len(files)} files found")
        all_files.extend(files)

    print(f"\n  Total files discovered: {len(all_files):,}")

    # Filter to only files we don't already have
    new_files = []
    for f in all_files:
        section_dir = Path(dest_dir) / f["section"]
        filepath = section_dir / f["filename"].replace("%20", " ").replace("?", "_").replace("&", "_")
        if filepath.exists() and filepath.stat().st_size > 1024:
            try:
                with open(filepath, "rb") as fh:
                    if fh.read(5) == b"%PDF-":
                        continue
            except:
                pass
        new_files.append(f)

    print(f"  New files to download: {len(new_files):,} (skipping {len(all_files) - len(new_files):,} already on disk)\n")

    if not new_files:
        print("  Nothing to download!")
        return

    print(f"  Downloading {len(new_files):,} files...\n")
    stats = defaultdict(int)
    start = time.time()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(download_file, session, f, dest_dir): f for f in new_files}
        done = 0
        for future in as_completed(futures):
            result = future.result()
            stats[result] += 1
            done += 1
            if done % 100 == 0 or done == len(new_files):
                elapsed = time.time() - start
                print(f"    {done:,}/{len(new_files):,} | dl={stats['downloaded']} skip={stats['skipped']} fail={sum(v for k,v in stats.items() if k.startswith('fail'))}")
            time.sleep(0.3)

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"  Downloaded: {stats['downloaded']:,}")
    print(f"  Skipped:    {stats['skipped']:,}")
    print(f"  Failed:     {sum(v for k,v in stats.items() if k.startswith('fail')):,}")
    print(f"  Time:       {elapsed/60:.1f} minutes")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
