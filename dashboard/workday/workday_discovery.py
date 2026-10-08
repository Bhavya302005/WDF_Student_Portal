"""
Workday URL Discovery Script
-----------------------------
For each company in workday_companies.txt that doesn't have a URL in workday.csv,
this script tries to discover the correct Workday URL by:
1. Generating candidate slugs from the company name
2. Trying each of the known Workday instance numbers (wd1, wd3, ...)
3. Following the redirect — Workday auto-redirects to the real careers page
4. If found, writes the result to workday.csv

Usage:
    python3 workday_discovery.py
"""

import asyncio
import csv
import os
import re
import time

import httpx

# Known Workday instance numbers (from analyzing existing workday.csv)
INSTANCES = ["wd1", "wd3", "wd5", "wd10", "wd12", "wd102", "wd103", "wd105", "wd108", "wd115", "wd501", "wd502", "wd503", "wd504"]

MAX_CONCURRENT = 150       # parallel requests
CONNECT_TIMEOUT = 5.0      # bail fast on DNS misses
READ_TIMEOUT    = 10.0
OUTPUT_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workday.csv")
TXT_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workday_companies.txt")


def slugify(name: str) -> list[str]:
    """Generate candidate Workday subdomains from a company display name."""
    base = name.lower().strip()
    candidates = set()

    # Variation 1: strip all non-alphanumeric
    s1 = re.sub(r"[^a-z0-9]", "", base)
    if s1: candidates.add(s1)

    # Variation 2: strip spaces only (keep hyphens/dots)
    s2 = re.sub(r"\s+", "", base)
    s2 = re.sub(r"[^a-z0-9\-]", "", s2)
    if s2: candidates.add(s2)

    # Variation 3: replace spaces with hyphens
    s3 = re.sub(r"\s+", "-", base)
    s3 = re.sub(r"[^a-z0-9\-]", "", s3)
    if s3: candidates.add(s3)

    # Variation 4: the name as-is (already a slug in many cases)
    s4 = re.sub(r"[^a-z0-9\-]", "", base)
    if s4: candidates.add(s4)

    return list(candidates)


def load_existing_csv() -> dict[str, dict]:
    """Returns dict of name -> row for already-known companies."""
    known = {}
    if not os.path.exists(OUTPUT_FILE):
        return known
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("name"):
                known[row["name"].strip().lower()] = row
    return known


def load_txt_companies() -> list[str]:
    """Returns list of company names/slugs from the .txt file."""
    with open(TXT_FILE, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


async def try_discover(client: httpx.AsyncClient, slug: str, instance: str) -> str | None:
    """
    Try GET https://{slug}.{instance}.myworkdayjobs.com/ and follow redirects.
    Returns the final URL if it looks like a valid Workday careers page, else None.
    """
    url = f"https://{slug}.{instance}.myworkdayjobs.com/"
    try:
        resp = await client.get(url, follow_redirects=True,
                                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT))
        final_url = str(resp.url)
        # Valid if we end up on a myworkdayjobs.com page (not a generic error)
        if resp.status_code == 200 and "myworkdayjobs.com" in final_url:
            # Exclude generic Workday error/signin pages
            if any(bad in final_url for bad in ["/signin", "/error", "/page-not-found"]):
                return None
            return final_url.rstrip("/")
    except Exception:
        pass
    return None


async def discover_company(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    name: str,
    done_event: asyncio.Event,
) -> dict | None:
    """Try all slug+instance combos for a company. Return first hit."""
    slugs = slugify(name)
    async with sem:
        for slug in slugs:
            for instance in INSTANCES:
                result_url = await try_discover(client, slug, instance)
                if result_url:
                    # Parse the site path from the URL
                    # e.g. https://2020companies.wd1.myworkdayjobs.com/external_careers
                    after_domain = result_url.split(".myworkdayjobs.com/", 1)[-1]
                    site_path = after_domain.split("/")[0] if after_domain else "careers"
                    final_url  = f"https://{slug}.{instance}.myworkdayjobs.com/{site_path}"
                    print(f"  [FOUND] {name} → {final_url}")
                    done_event.set()
                    return {"name": name, "slug": f"{slug}/{site_path}", "url": final_url}
    return None


async def main():
    print("=" * 60)
    print("Workday URL Discovery Script")
    print("=" * 60)

    existing = load_existing_csv()
    all_companies = load_txt_companies()

    # Find companies not yet in CSV
    missing = [c for c in all_companies if c.strip().lower() not in existing]
    print(f"Total in .txt : {len(all_companies)}")
    print(f"Already in CSV: {len(existing)}")
    print(f"To discover   : {len(missing)}\n")

    if not missing:
        print("Nothing to discover!")
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    found = []
    done = 0
    total = len(missing)
    start = time.monotonic()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=400, max_keepalive_connections=100),
        headers=headers,
        follow_redirects=True,
    ) as client:

        async def process(name):
            nonlocal done
            ev = asyncio.Event()
            result = await discover_company(client, sem, name, ev)
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = time.monotonic() - start
                pct = done / total * 100
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (total - done) / rate if rate > 0 else 0
                print(f"  Progress: {done}/{total} ({pct:.1f}%) | {rate:.1f}/s | ETA: {remaining/60:.1f}min")
            if result:
                found.append(result)

        tasks = [process(name) for name in missing]
        await asyncio.gather(*tasks)

    print(f"\nDiscovered {len(found)} new companies!")

    if found:
        # Append to existing CSV
        fieldnames = ["name", "slug", "url"]
        file_exists = os.path.exists(OUTPUT_FILE)
        with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            for row in found:
                writer.writerow(row)
        print(f"Saved {len(found)} new entries to {OUTPUT_FILE}")
    else:
        print("No new companies discovered.")


if __name__ == "__main__":
    asyncio.run(main())
