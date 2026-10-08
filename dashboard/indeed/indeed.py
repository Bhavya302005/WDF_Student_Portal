#!/usr/bin/env python3
"""
Perfect Indeed Job Scraper
- Incremental saves (never lose data if it crashes)
- Auto-retry with exponential backoff
- Smart query syntax for better results
- Progress logging
- Duplicate detection
- Exact company matching
- Strict date filtering
- JSON-safe serialization
"""

import csv
import time
import json
import math
import logging
from datetime import datetime, date
from pathlib import Path

try:
    import pandas as pd
    from jobspy import scrape_jobs
except ImportError as e:
    print(f"ERROR: Missing required package: {e.name}. Run: pip install python-jobspy pandas")
    raise SystemExit(1)

# ─── CONFIGURATION ─────────────────────────────────────────────────────────

INDIAN_STATES = [
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam",
    "Bihar", "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir",
    "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha",
    "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal"
]

SEARCH_TERM = 'Procurement Manager'

# FIX #1: this used to be "Amazon.com", which requires the literal substring
# "amazon.com" inside the company name. Indeed almost always lists the company
# as just "Amazon" (or "Amazon.in", "Amazon Development Center", etc.), none of
# which contain "amazon.com" — so the old value silently filtered out ~all jobs.
# Matching is still done as a case-insensitive substring check.
EXACT_COMPANY_MATCH = ""   # Matches any company name containing 'amazon'

LOCATION = "usa"
RESULTS_WANTED = 50      # Total jobs to fetch overall
BATCH_SIZE = 20               # Jobs per request (keep it low to avoid blocks)
HOURS_OLD = 24            # FIX #3: comment previously said "Last 7 days" - it's 24 hours
OUTPUT_DIR = Path("indeed_jobs")
MAX_RETRIES = 5
BASE_DELAY = 3                # Seconds between requests
DATE_FETCH_DELAY = 0.5         # FIX #4: small pause between per-job date requests

# ─── SETUP ──────────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(exist_ok=True)

# Stable filenames based on search term so different runs get different outputs
import re
safe_search = re.sub(r'[^a-zA-Z0-9]', '_', SEARCH_TERM).strip('_')
safe_search = re.sub(r'_+', '_', safe_search).lower()

csv_file = OUTPUT_DIR / f"indeed_jobs_{safe_search}.csv"
json_file = OUTPUT_DIR / f"indeed_jobs_{safe_search}.json"
state_file = OUTPUT_DIR / f"scraper_state_{safe_search}.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("indeed_scraper")

# ─── STATE MANAGEMENT ───────────────────────────────────────────────────────

def load_state():
    """Load persistent scraper state (seen URLs, matched URLs + offset)."""
    seen = set()
    matched = set()
    offset = 0
    loc_idx = 0

    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            seen = set(state.get("seen_urls", []))
            matched = set(state.get("matched_urls", seen))  # backward-compatible fallback
            offset = state.get("offset", 0)
            loc_idx = state.get("location_index", 0)
            logger.info(
                f"Resumed state: {len(seen)} seen URLs, {len(matched)} matched, "
                f"offset={offset}, loc_idx={loc_idx}"
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Corrupt state file, starting fresh: {e}")

    return seen, matched, offset, loc_idx


def save_state(seen, matched, offset, loc_idx=0):
    """Atomically save state so resume works even if crash mid-write."""
    tmp = state_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "seen_urls": list(seen),
            "matched_urls": list(matched),
            "offset": offset,
            "location_index": loc_idx,
            "last_updated": datetime.now().isoformat()
        }, f, indent=2)

    # On Windows, replace can fail if the file is open/locked in an editor
    try:
        tmp.replace(state_file)
    except PermissionError:
        for _ in range(3):
            time.sleep(0.5)
            try:
                if state_file.exists():
                    state_file.unlink()
                tmp.rename(state_file)
                break
            except Exception:
                pass


def load_existing_jobs():
    """Load already-scraped jobs from JSON (for verification only)."""
    jobs = []
    if json_file.exists():
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                jobs = json.load(f)
            logger.info(f"Loaded {len(jobs)} existing jobs from JSON")
        except json.JSONDecodeError as e:
            logger.warning(f"Corrupt JSON file, starting fresh: {e}")
    return jobs


# ─── JSON SANITIZATION ──────────────────────────────────────────────────────

def sanitize(obj):
    """Recursively make a value JSON-safe."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (datetime, date, pd.Timestamp)):
        return obj.isoformat() if hasattr(obj, "isoformat") else str(obj)
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if obj is pd.NaT or obj is pd.NA:
        return None
    return obj


# ─── BATCH SAVING ───────────────────────────────────────────────────────────

def save_batch(jobs_df, seen_urls, matched_urls):
    """Append batch to CSV and JSON, deduplicated and filtered.

    `seen_urls` tracks every job URL we've ever looked at (matched or not),
    so we never re-fetch exact dates for the same non-matching job twice.
    `matched_urls` tracks only jobs that passed the company filter and were
    actually saved — this is what counts toward RESULTS_WANTED.
    """
    if jobs_df is None or jobs_df.empty:
        return []

    required_cols = {"job_url", "company", "date_posted"}
    missing = required_cols - set(jobs_df.columns)
    if missing:
        logger.error(f"Missing columns in response: {missing} — skipping batch")
        return []

    # Dedup against everything we've ever encountered (not just matches)
    new_jobs = jobs_df[~jobs_df["job_url"].isin(seen_urls)].copy()
    if new_jobs.empty:
        return []

    # FIX #2: mark every newly-seen URL as processed *before* filtering,
    # so non-matching companies aren't repeatedly re-fetched/re-date-scraped
    # if they resurface in an overlapping page window.
    for url in new_jobs["job_url"]:
        seen_urls.add(url)

    # Company match (case-insensitive substring)
    if EXACT_COMPANY_MATCH:
        new_jobs = new_jobs[
            new_jobs["company"].str.lower().str.contains(
                EXACT_COMPANY_MATCH.lower(), regex=False, na=False
            )
        ]
        if new_jobs.empty:
            logger.info(f"All jobs filtered out by company match '{EXACT_COMPANY_MATCH}'")
            return []

    # Fetch exact dates for matched jobs using universal JobPosting schema
    if "job_url_direct" in new_jobs.columns:
        import requests
        from bs4 import BeautifulSoup

        def fetch_exact_date(row):
            url = row.get("job_url_direct")
            if not isinstance(url, str) or not url:
                return row["date_posted"]
            try:
                h = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                r = requests.get(url, headers=h, timeout=10, allow_redirects=True)
                soup = BeautifulSoup(r.text, 'html.parser')

                dates = []

                # Standard Schema.org JobPosting (Greenhouse, Workday, etc.)
                for s in soup.find_all('script', type='application/ld+json'):
                    try:
                        data = json.loads(s.string)
                        if isinstance(data, dict) and data.get('@type') == 'JobPosting' and 'datePosted' in data:
                            dates.append(pd.to_datetime(data['datePosted']).date())
                        elif isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict) and item.get('@type') == 'JobPosting' and 'datePosted' in item:
                                    dates.append(pd.to_datetime(item['datePosted']).date())
                    except Exception:
                        continue

                # Fallback: React-embedded postingDate fields
                for script in soup.find_all('script'):
                    if script.string and 'postingDate' in script.string:
                        matches = re.findall(r'\\\"postingDate\\\":\\\"([^\\]+)\\\"', script.string)
                        for match in matches:
                            dates.append(pd.to_datetime(match).date())
                        matches2 = re.findall(r'\"postingDate\":\"([^\"]+)\"', script.string)
                        for match2 in matches2:
                            dates.append(pd.to_datetime(match2).date())

                if dates:
                    return max(dates)
            except Exception:
                pass
            finally:
                time.sleep(DATE_FETCH_DELAY)
            return row["date_posted"]

        logger.info(f"Fetching exact posting dates for {len(new_jobs)} jobs. This may take a moment...")
        new_jobs["date_posted"] = new_jobs.apply(fetch_exact_date, axis=1)

    # Update matched set (this is what counts toward RESULTS_WANTED)
    for url in new_jobs["job_url"]:
        matched_urls.add(url)

    records = [sanitize(r) for r in new_jobs.to_dict("records")]

    # ── CSV: append, write header ONLY if file is newly created ──
    write_header = not csv_file.exists() or csv_file.stat().st_size == 0
    new_jobs.to_csv(
        csv_file,
        mode="a",
        header=write_header,
        quoting=csv.QUOTE_NONNUMERIC,
        escapechar="\\",
        index=False
    )

    # ── JSON: append efficiently ──
    existing = []
    if json_file.exists() and json_file.stat().st_size > 0:
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            logger.warning("Corrupt JSON, overwriting")

    existing.extend(records)
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    logger.info(f"Saved {len(records)} new jobs (total matched: {len(matched_urls)})")
    return records


# ─── SCRAPING WITH RETRY ────────────────────────────────────────────────────

def scrape_with_retry(offset, current_location):
    """Scrape with exponential backoff on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Fetching batch for {current_location} at offset {offset} (attempt {attempt}/{MAX_RETRIES})...")

            is_india = "india" in current_location.lower() or current_location in INDIAN_STATES
            country_val = "india" if is_india else "USA"
            jobs = scrape_jobs(
                site_name=["indeed"],
                search_term=SEARCH_TERM,
                location=current_location,
                results_wanted=BATCH_SIZE,
                offset=offset,
                hours_old=HOURS_OLD,
                country_indeed=country_val,
                verbose=0,
            )

            if jobs is None:
                logger.warning("scrape_jobs returned None")
                return None

            return jobs

        except Exception as e:
            wait = BASE_DELAY * (2 ** attempt)
            logger.warning(f"Error: {e}. Retrying in {wait}s...")
            time.sleep(wait)

    logger.error(f"Failed after {MAX_RETRIES} attempts at offset {offset}")
    return None


# ─── MAIN SCRAPER ───────────────────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info("INDEED JOB SCRAPER STARTED")
    logger.info(f"Search: {SEARCH_TERM}")
    logger.info(f"Location: {LOCATION}")
    logger.info(f"Company filter: '{EXACT_COMPANY_MATCH}'")
    logger.info(f"Target: {RESULTS_WANTED} jobs")
    logger.info("=" * 50)

    locations_to_scrape = (["India"] + INDIAN_STATES) if LOCATION.lower() == "india" else [LOCATION]

    # Load state from disk (single source of truth)
    seen_urls, matched_urls, offset, loc_idx = load_state()

    # Verify consistency with JSON
    existing_jobs = load_existing_jobs()
    existing_ids = {j["job_url"] for j in existing_jobs}

    if len(existing_jobs) != len(matched_urls):
        logger.warning(
            f"Mismatch: JSON has {len(existing_jobs)} jobs but state has {len(matched_urls)} matched URLs. "
            "Merging them to avoid duplicates."
        )
        matched_urls.update(existing_ids)
        seen_urls.update(existing_ids)

    while len(matched_urls) < RESULTS_WANTED and loc_idx < len(locations_to_scrape):
        current_location = locations_to_scrape[loc_idx]

        # Rate limit: be polite, cap jitter at reasonable max
        if offset > 0:
            jitter = min(BASE_DELAY + (offset // 20), 120)
            logger.info(f"Sleeping {jitter}s to avoid rate limits...")
            time.sleep(jitter)

        batch = scrape_with_retry(offset, current_location)

        if batch is None:
            logger.error("Fatal error. Stopping.")
            save_state(seen_urls, matched_urls, offset, loc_idx)
            break

        if hasattr(batch, "empty") and batch.empty:
            logger.info(f"No more jobs found for {current_location}. Moving to next location.")
            loc_idx += 1
            offset = 0
            save_state(seen_urls, matched_urls, offset, loc_idx)
            continue

        saved = save_batch(batch, seen_urls, matched_urls)

        # Persist state after every successful batch
        offset += BATCH_SIZE
        save_state(seen_urls, matched_urls, offset, loc_idx)

        if len(saved) < BATCH_SIZE and len(saved) > 0:
            logger.info(f"Partial batch — may be near end of results for {current_location}.")

        # Safety: Indeed caps around 1000 results
        if offset >= 1000:
            logger.info(f"Hit Indeed's ~1000 result cap for {current_location}. Moving to next.")
            loc_idx += 1
            offset = 0
            save_state(seen_urls, matched_urls, offset, loc_idx)

    logger.info("=" * 50)
    logger.info("SCRAPING COMPLETE")
    logger.info(f"Total matched jobs: {len(matched_urls)}")
    logger.info(f"CSV: {csv_file}")
    logger.info(f"JSON: {json_file}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()