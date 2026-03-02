#!/usr/bin/env python3
"""
DOJ Epstein Library — Full Disclosure Crawler & Downloader
===========================================================
Crawls all sub-pages of justice.gov/epstein/doj-disclosures,
extracts every downloadable file link (PDFs, audio, video, etc.),
handles pagination, and downloads everything.

Usage:
  # Step 1: Crawl all pages and build manifest (no downloads)
  python3 crawl_doj_disclosures.py --crawl-only

  # Step 2: Download everything from manifest
  python3 crawl_doj_disclosures.py --download

  # Or do both in one go
  python3 crawl_doj_disclosures.py

  # Resume interrupted download
  python3 crawl_doj_disclosures.py --download --resume

Options:
  --crawl-only     Only crawl and build manifest, don't download
  --download       Download from existing manifest
  --resume         Skip already-downloaded files
  --dest-dir DIR   Destination directory (default: ./doj_disclosures)
  --delay N        Seconds between requests (default: 2.0)
  --manifest FILE  Manifest file path (default: ./doj_full_manifest.json)
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing required packages...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "requests", "beautifulsoup4", "--break-system-packages", "-q"])
    import requests
    from bs4 import BeautifulSoup

BASE_URL = "https://www.justice.gov"

# Full browser-like headers (critical for DOJ.gov)
SESSION_HEADERS = {
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
    "Referer": "https://www.justice.gov/epstein/doj-disclosures",
}

# All known sub-page paths from DOJ Disclosures
SECTION_PAGES = {
    "EFTA_H.R.4405": [
        "/epstein/doj-disclosures/data-set-1-files",
        "/epstein/doj-disclosures/data-set-2-files",
        "/epstein/doj-disclosures/data-set-3-files",
        "/epstein/doj-disclosures/data-set-4-files",
        "/epstein/doj-disclosures/data-set-5-files",
        "/epstein/doj-disclosures/data-set-6-files",
        "/epstein/doj-disclosures/data-set-7-files",
        "/epstein/doj-disclosures/data-set-8-files",
        "/epstein/doj-disclosures/data-set-9-files",
        "/epstein/doj-disclosures/data-set-10-files",
        "/epstein/doj-disclosures/data-set-11-files",
        "/epstein/doj-disclosures/data-set-12-files",
    ],
    "Court_Records": [
        "/epstein/doj-disclosures/court-records-ca-florida-holdings-llc-publisher-palm-beach-post-v-aronberg-no-50-2019-ca-014681-xxxx-mb",
        "/epstein/doj-disclosures/court-records-doe-17-v-indyke-no-119-cv-09610-sdny-2019",
        "/epstein/doj-disclosures/court-records-doe-1000-v-indyke-no-119-cv-10577-sdny-2019",
        "/epstein/doj-disclosures/court-records-doe-no-101-v-epstein-no-909-cv-80591-sd-fla-2009",
        "/epstein/doj-disclosures/court-records-doe-no-102-v-epstein-no-909-cv-80656-sd-fla-2009",
        "/epstein/doj-disclosures/court-records-doe-no-103-v-epstein-no-910-cv-80309-sd-fla-2010",
        "/epstein/doj-disclosures/court-records-doe-no-3-v-epstein-no-908-cv-80232-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-no-4-v-epstein-no-908-cv-80380-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-no-5-v-epstein-no-908-cv-80381-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-no-6-v-epstein-no-908-cv-80994-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-no-8-v-epstein-no-909-cv-80802-sd-fla-2009",
        "/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80069-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80119-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-v-epstein-no-908-cv-80804-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-doe-v-epstein-no-909-v-80469-sd-fla-2009",
        "/epstein/doj-disclosures/court-records-doe-v-indyke-no-119-cv-08673-sdny-2019",
        "/epstein/doj-disclosures/court-records-doe-v-indyke-no-119-cv-11869-sdny-2019",
        "/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-00484-sdny-2020",
        "/epstein/doj-disclosures/court-records-doe-v-indyke-no-120-cv-02365-sdny-2020",
        "/epstein/doj-disclosures/court-records-doe-v-united-states-no-908-cv-80736-sd-fla-2008",
        "/epstein/doj-disclosures/court-records-epstein-v-no-sc15-2286-fla-sup-ct-2015",
        "/epstein/doj-disclosures/court-records-epstein-v-rothstein-no-50-2009-ca-040800-xxxx-mb-fla-15th-cir-ct-2009",
        "/epstein/doj-disclosures/court-records-government-united-states-virgin-islands-v-jpmorgan-chase-bank-na-no-122-cv-10904-sdny-2022",
        "/epstein/doj-disclosures/court-records-jane-doe-43-v-epstein-no-117-cv-00616-sdny-2017",
        "/epstein/doj-disclosures/court-records-matter-estate-jeffrey-e-epstein-deceased-no-st-21-rv-00005-vi-super-ct-2021",
        "/epstein/doj-disclosures/court-records-maxwell-v-estate-jeffrey-epstein-no-st-20-cv-155-vi-super-ct-2020",
        "/epstein/doj-disclosures/court-records-maxwell-v-united-states-no-24-1073-us-2025-petition-cert",
        "/epstein/doj-disclosures/court-records-re-grand-jury-05-02-wpb-07-103-wpb-no-925-mc-80920-sd-fla-2025",
        "/epstein/doj-disclosures/court-records-state-florida-v-epstein-no-50-2006-cf-009454-axxx-mb-fla-15th-cir-ct-2006",
        "/epstein/doj-disclosures/court-records-state-florida-v-epstein-no-50-2008-cf-001828-axxx-mb-fla-15th-cir-ct-2008",
        "/epstein/doj-disclosures/court-records-united-states-v-epstein-no-119-cr-00490-sdny-2019",
        "/epstein/doj-disclosures/court-records-united-states-v-maxwell-no-120-cr-00330-sdny-2020",
        "/epstein/doj-disclosures/court-records-v-indyke-no-119-cv-10475-sdny-2019",
        "/epstein/doj-disclosures/court-records-v-maxwell-no-115-cv-07433-sdny-2015",
        # 2nd Circuit appeals
        "/epstein/doj-disclosures/court-records-doe-v-epstein-no-09-80656-cv-2nd-cir-2009",
        "/epstein/doj-disclosures/court-records-doe-v-epstein-no-09-80119-cv-2nd-cir-2009",
        "/epstein/doj-disclosures/court-records-doe-v-united-states-no-09-80736-cv-2nd-cir-2009",
        "/epstein/doj-disclosures/court-records-v-maxwell-no-15-cv-07433-2nd-cir-2016",
    ],
    "FOIA": [
        "/epstein/doj-disclosures/foia-customs-and-border-protection-cbp",
        "/epstein/doj-disclosures/foia-federal-bureau-investigation-fbi",
        "/epstein/doj-disclosures/foia-federal-bureau-prisons-bop",
        "/epstein/doj-disclosures/foia-florida",
    ],
    "Prior_DOJ_Disclosures": [
        "/epstein/doj-disclosures/first-phase-declassified-epstein-files",
        "/epstein/doj-disclosures/bop-video-footage",
        "/epstein/doj-disclosures/maxwell-proffer",
        "/epstein/doj-disclosures/memoranda-and-correspondence",
    ],
}


def create_session():
    """Create a requests session with browser-like headers."""
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    return session


def warmup_session(session, delay):
    """Visit the main page first to get cookies."""
    print("[*] Warming up session...")
    try:
        resp = session.get(f"{BASE_URL}/epstein/doj-disclosures", timeout=30)
        print(f"    Main page: {resp.status_code}")
        time.sleep(delay)
    except Exception as e:
        print(f"    Warmup failed: {e}")


def extract_file_links(soup, page_url):
    """Extract all downloadable file links from a page."""
    files = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(page_url, href)

        # Match file extensions
        parsed = urlparse(full_url)
        path_lower = parsed.path.lower()

        is_file = any(path_lower.endswith(ext) for ext in [
            '.pdf', '.mp3', '.mp4', '.wav', '.avi', '.mov', '.wmv',
            '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.txt', '.csv', '.zip', '.rar', '.7z', '.tar', '.gz',
            '.jpg', '.jpeg', '.png', '.gif', '.tiff', '.bmp',
        ])

        # Also match justice.gov/epstein/files/ pattern
        if not is_file and '/files/' in parsed.path and '/epstein/' in parsed.path:
            is_file = True

        if is_file:
            filename = os.path.basename(parsed.path)
            text = a.get_text(strip=True)[:200]
            files.append({
                "url": full_url,
                "filename": filename,
                "link_text": text,
            })

    return files


def get_pagination_urls(soup, page_url):
    """Find pagination links on the page."""
    paginated = []
    pager = soup.find("nav", class_="pager") or soup.find("ul", class_="pager")

    if not pager:
        # Try finding ?page= links anywhere
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "page=" in href:
                full_url = urljoin(page_url, href)
                paginated.append(full_url)
    else:
        for a in pager.find_all("a", href=True):
            full_url = urljoin(page_url, a["href"])
            paginated.append(full_url)

    # Also look for "last" page to determine total
    last_page = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "page=" in href:
            try:
                parsed = urlparse(urljoin(page_url, href))
                qs = parse_qs(parsed.query)
                page_num = int(qs.get("page", [0])[0])
                last_page = max(last_page, page_num)
            except (ValueError, IndexError):
                pass

    # Generate all page URLs if we found pagination
    if last_page > 0:
        parsed_base = urlparse(page_url)
        base_no_query = f"{parsed_base.scheme}://{parsed_base.netloc}{parsed_base.path}"
        all_pages = [f"{base_no_query}?page={i}" for i in range(1, last_page + 1)]
        return all_pages

    return list(set(paginated))


def crawl_page(session, url, delay=2.0):
    """Crawl a single page, return files and pagination URLs."""
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 403:
            print(f"    [403] Blocked: {url}")
            return [], [], 403
        if resp.status_code != 200:
            print(f"    [{resp.status_code}] Error: {url}")
            return [], [], resp.status_code

        soup = BeautifulSoup(resp.text, "html.parser")
        files = extract_file_links(soup, url)
        pagination = get_pagination_urls(soup, url)

        time.sleep(delay)
        return files, pagination, 200

    except Exception as e:
        print(f"    [ERROR] {url}: {e}")
        return [], [], -1


def crawl_all_sections(session, delay=2.0):
    """Crawl all sections and all paginated pages."""
    manifest = {}
    total_files = 0
    blocked_pages = []

    for section_name, paths in SECTION_PAGES.items():
        print(f"\n{'='*60}")
        print(f"  SECTION: {section_name} ({len(paths)} sub-pages)")
        print(f"{'='*60}")

        section_files = []

        for path in paths:
            url = f"{BASE_URL}{path}"
            short_name = path.split("/")[-1]
            print(f"\n  [{short_name}]")
            print(f"    Page 1: {url}")

            files, pagination, status = crawl_page(session, url, delay)

            if status == 403:
                blocked_pages.append(url)
                continue

            print(f"    Found {len(files)} files on page 1")
            section_files.extend(files)

            if pagination:
                print(f"    Pagination: {len(pagination)} additional pages")
                for i, purl in enumerate(pagination):
                    print(f"    Page {i+2}: fetching...", end=" ")
                    pfiles, _, pstatus = crawl_page(session, purl, delay)
                    if pstatus == 403:
                        print(f"BLOCKED")
                        blocked_pages.append(purl)
                    else:
                        print(f"{len(pfiles)} files")
                        section_files.extend(pfiles)

        # Deduplicate by URL
        seen = set()
        unique_files = []
        for f in section_files:
            if f["url"] not in seen:
                seen.add(f["url"])
                unique_files.append(f)

        manifest[section_name] = unique_files
        total_files += len(unique_files)
        print(f"\n  Section total: {len(unique_files)} unique files")

    print(f"\n{'='*60}")
    print(f"  CRAWL COMPLETE")
    print(f"  Total unique files: {total_files}")
    print(f"  Blocked pages: {len(blocked_pages)}")
    if blocked_pages:
        print(f"  Blocked URLs:")
        for bp in blocked_pages[:10]:
            print(f"    - {bp}")
        if len(blocked_pages) > 10:
            print(f"    ... and {len(blocked_pages) - 10} more")
    print(f"{'='*60}")

    return manifest, blocked_pages


def download_files(manifest, dest_dir, delay=1.0, resume=False):
    """Download all files from manifest."""
    session = create_session()

    total = sum(len(files) for files in manifest.values())
    downloaded = 0
    skipped = 0
    failed = 0

    for section_name, files in manifest.items():
        section_dir = Path(dest_dir) / section_name
        section_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n  Downloading {section_name}: {len(files)} files -> {section_dir}")

        for f in files:
            filename = f["filename"]
            filepath = section_dir / filename

            if resume and filepath.exists() and filepath.stat().st_size > 0:
                skipped += 1
                continue

            try:
                resp = session.get(f["url"], timeout=60, stream=True)
                if resp.status_code == 200:
                    with open(filepath, "wb") as fout:
                        for chunk in resp.iter_content(chunk_size=8192):
                            fout.write(chunk)
                    downloaded += 1
                    size_mb = filepath.stat().st_size / (1024 * 1024)
                    print(f"    [{downloaded + skipped}/{total}] {filename} ({size_mb:.1f} MB)")
                else:
                    failed += 1
                    print(f"    [FAIL {resp.status_code}] {filename}")

                time.sleep(delay)

            except Exception as e:
                failed += 1
                print(f"    [ERROR] {filename}: {e}")

    print(f"\n  Download complete: {downloaded} downloaded, {skipped} skipped, {failed} failed")


def main():
    parser = argparse.ArgumentParser(description="DOJ Epstein Library Full Crawler")
    parser.add_argument("--crawl-only", action="store_true", help="Only crawl, don't download")
    parser.add_argument("--download", action="store_true", help="Download from existing manifest")
    parser.add_argument("--resume", action="store_true", help="Skip already-downloaded files")
    parser.add_argument("--dest-dir", default="./doj_disclosures", help="Download destination")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between requests")
    parser.add_argument("--manifest", default="./_pipeline_output/doj_full_manifest.json", help="Manifest file")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)

    if args.download and manifest_path.exists():
        # Download from existing manifest
        with open(manifest_path) as f:
            manifest = json.load(f)
        print(f"Loaded manifest: {sum(len(v) for v in manifest.values())} files")
        download_files(manifest, args.dest_dir, args.delay, args.resume)
        return

    # Crawl
    session = create_session()
    warmup_session(session, args.delay)

    manifest, blocked = crawl_all_sections(session, args.delay)

    # Save manifest
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest saved to {manifest_path}")

    # Save blocked pages for browser-based fallback
    if blocked:
        blocked_path = manifest_path.parent / "blocked_pages.json"
        with open(blocked_path, "w") as f:
            json.dump(blocked, f, indent=2)
        print(f"Blocked pages saved to {blocked_path}")

    if not args.crawl_only:
        download_files(manifest, args.dest_dir, args.delay, args.resume)


if __name__ == "__main__":
    main()
