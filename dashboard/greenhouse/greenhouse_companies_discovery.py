"""
Discover companies hosting jobs on Greenhouse ATS — free, no API key.

Method: query Common Crawl's CDX index (a free lookup service over its
web archive) for every URL ever seen under boards.greenhouse.io/* and
job-boards.greenhouse.io/*. This is an index lookup, not a scrape, so
it returns tens of thousands of company slugs in minutes.

Optionally validates each slug against Greenhouse's live public
Boards API (boards-api.greenhouse.io) to drop dead/renamed boards.

Usage:
    pip install requests
    python greenhouse_company_discovery.py
    python greenhouse_company_discovery.py --crawls 3 --no-validate --workers 40
"""

import argparse
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

CDX_SERVER = "https://index.commoncrawl.org"
BOARD_PREFIXES = ("boards.greenhouse.io/", "job-boards.greenhouse.io/")
SLUG_RE = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9][a-zA-Z0-9\-]{1,60})", re.I)


def list_crawls(limit, session, year=None):
    """Most recent Common Crawl monthly snapshot IDs, newest first."""
    r = session.get(f"{CDX_SERVER}/collinfo.json", timeout=30)
    r.raise_for_status()
    crawls = []
    for c in r.json():
        if year and str(year) not in c["id"]:
            continue
        crawls.append(c["id"])
        if len(crawls) >= limit:
            break
    return crawls


def query_cdx(prefix, crawl_id, session):
    """All URLs under a prefix from one crawl snapshot (paginated)."""
    base = f"{CDX_SERVER}/{crawl_id}-index"
    common = {"url": prefix, "matchType": "prefix", "output": "json", "fl": "url", "filter": "status:200"}

    try:
        meta = session.get(base, params={**common, "showNumPages": "true"}, timeout=30).json()
        num_pages = meta.get("pages", 1)
    except Exception:
        num_pages = 1

    urls = set()
    for page in range(num_pages):
        try:
            resp = session.get(base, params={**common, "page": page}, timeout=60)
            if resp.status_code != 200:
                continue
            for line in resp.text.splitlines():
                if not line.strip():
                    continue
                try:
                    urls.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    continue
        except requests.RequestException:
            continue
    return urls


def extract_slugs(urls):
    slugs = set()
    for u in urls:
        m = SLUG_RE.search(u)
        if m:
            slugs.add(m.group(1).lower())
    return slugs


def validate_slug(slug, session):
    """True if the board is still live on Greenhouse's public API."""
    try:
        r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawls", type=int, default=6, help="how many recent monthly snapshots to scan")
    ap.add_argument("--year", type=str, default=None, help="filter snapshots by year (e.g. 2024)")
    ap.add_argument("--no-validate", action="store_true", help="skip live-API validation (fastest, may include stale slugs)")
    ap.add_argument("--workers", type=int, default=30, help="parallel threads")
    ap.add_argument("--out", default="greenhouse_companies.txt")
    args = ap.parse_args()

    session = requests.Session()
    crawls = list_crawls(args.crawls, session, args.year)
    print(f"Scanning {len(crawls)} snapshots: {crawls}")

    jobs = [(prefix, crawl) for crawl in crawls for prefix in BOARD_PREFIXES]
    all_urls = set()
    with ThreadPoolExecutor(max_workers=min(args.workers, len(jobs))) as ex:
        futures = {ex.submit(query_cdx, prefix, crawl, session): (prefix, crawl) for prefix, crawl in jobs}
        for fut in as_completed(futures):
            prefix, crawl = futures[fut]
            found = fut.result()
            print(f"  {crawl} / {prefix}: {len(found)} URLs")
            all_urls |= found

    slugs = extract_slugs(all_urls)
    print(f"Total unique candidate company slugs found from Common Crawl: {len(slugs)}")
    
    # Load existing to avoid re-validating
    existing = set()
    try:
        with open(args.out, "r") as f:
            existing = {line.strip().lower() for line in f if line.strip()}
    except FileNotFoundError:
        pass
    
    print(f"Already in {args.out}: {len(existing)}")
    
    new_slugs = sorted(slugs - existing)
    print(f"Brand new slugs to check: {len(new_slugs)}")

    if not new_slugs:
        print("No new slugs found. Exiting.")
        return

    live_slugs = []
    if args.no_validate:
        live_slugs = new_slugs
    else:
        print("Validating new slugs against live Boards API...")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(validate_slug, s, session): s for s in new_slugs}
            done = 0
            for fut in as_completed(futures):
                s = futures[fut]
                is_live = fut.result()
                if is_live:
                    live_slugs.append(s)
                done += 1
                if done % 500 == 0 or done == len(new_slugs):
                    print(f"  checked {done}/{len(new_slugs)} ({len(live_slugs)} live so far)")

    live_slugs.sort()
    if live_slugs:
        with open(args.out, "a") as f:
            for s in live_slugs:
                f.write(s + "\n")
        print(f"Done! Successfully appended {len(live_slugs)} new valid companies to {args.out}")
    else:
        print(f"Done! No new valid companies found.")


if __name__ == "__main__":
    main()