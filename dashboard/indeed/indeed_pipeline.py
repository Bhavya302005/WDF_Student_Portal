#!/usr/bin/env python3
"""
Indeed Pipeline — Sequential Company Scraper
=============================================
Scrapes Indeed for jobs from a list of target companies, one at a time,
with aggressive rate-limit protection to avoid 429s and IP blocks.

Safety features:
  • Sequential company processing (never parallel)
  • Adaptive cooldowns between companies (20-35s light, 60-90s after heavy load/429)
  • Exponential backoff on retries (up to 5 min)
  • Auto-pause on 429 detection (5-minute cooldown, long cooldowns for next companies)
  • Incremental state saving (resume-safe)
  • Supabase upsert after each company batch (currently disabled)

Usage:
  python3 indeed/indeed_pipeline.py                        # scrape all companies
  python3 indeed/indeed_pipeline.py --post-time day        # last 24h (default)
  python3 indeed/indeed_pipeline.py --post-time week       # last 7 days
  python3 indeed/indeed_pipeline.py --location "USA"       # change location
  python3 indeed/indeed_pipeline.py --resume               # resume from last state
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import math
import time
import random
import hashlib
import logging
import argparse
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

try:
    import pandas as pd
    from jobspy import scrape_jobs
except ImportError as e:
    print(f"ERROR: Missing required package: {e.name}. Run: pip install python-jobspy pandas")
    raise SystemExit(1)

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ─── Supabase ───────────────────────────────────────────────────────────────    
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from scraper_utils import enrich_raw_job
except ImportError:
    def enrich_raw_job(j): return j

# ─── Rate-limit tuning ─────────────────────────────────────────────────────
BATCH_SIZE            = 50          # jobs per Indeed request (fewer, larger pages)
MAX_RESULTS_PER_CO    = 300         # max jobs to fetch per company
MAX_RETRIES           = 4           # retries per batch (non-429 errors)
MAX_429_PAUSES        = 2           # 429 pauses allowed per batch before giving up
BASE_DELAY            = 5           # seconds between batches
COMPANY_COOLDOWN_MIN  = 20          # min seconds between companies (light load)
COMPANY_COOLDOWN_MAX  = 35          # max seconds between companies (light load)
HOT_COOLDOWN_MIN      = 60          # min seconds between companies (heavy load / after 429)
HOT_COOLDOWN_MAX      = 90          # max seconds between companies (heavy load / after 429)
HOT_STREAK_COMPANIES  = 3           # companies to keep hot cooldown after a 429
BACKOFF_429           = 300         # 5-minute pause on 429

# ─── Post-time mapping ─────────────────────────────────────────────────────
POST_TIME_MAP = {
    "hour":  1,
    "day":   24,
    "week":  168,
    "month": 720,
    "any":   None,
}

# ─── Target companies ──────────────────────────────────────────────────────
# Format: (search_term, company_match_substring)
# search_term is what goes into Indeed's search bar
# company_match is the case-insensitive substring to filter results by company name

TARGET_COMPANIES = [
    ("TSMC", "tsmc"),
    ("Saudi Aramco", "saudi aramco"),
    ("Tesla", "tesla"),
    ("Berkshire Hathaway", "berkshire hathaway"),
    ("SK Hynix", "sk hynix"),
    ("General Electric", "general electric"),
    ("Coca-Cola", "coca-cola"),
    ("Goldman Sachs", "goldman sachs"),
    ("Bank of China", "bank of china"),
    ("Alibaba", "alibaba"),
    ("L'Oréal", "l'oréal"),
    ("Siemens", "siemens"),
    ("PetroChina", "petrochina"),
    ("Novo Nordisk", "novo nordisk"),
    ("China Mobile", "china mobile"),
    ("Toronto Dominion Bank", "toronto dominion bank"),
    ("IBM", "ibm"),
    ("McDonald", "mcdonald"),
    ("Pepsico", "pepsico"),
    ("Reliance Industries", "reliance industries"),
    ("Nextera Energy", "nextera energy"),
    ("Walt Disney", "walt disney"),
    ("Gilead Sciences", "gilead sciences"),
    ("Safran", "safran"),
    ("AT&T", "at&t"),
    ("Deutsche Telekom", "deutsche telekom"),
    ("Siemens Energy", "siemens energy"),
    ("China Shenhua Energy", "china shenhua energy"),
    ("CVS Health", "cvs health"),
    ("Sony", "sony"),
    ("Progressive", "progressive"),
    ("Lockheed Martin", "lockheed martin"),
    ("Petrobras", "petrobras"),
    ("Zijin Mining", "zijin mining"),
    ("Recruit", "recruit"),
    ("AXA", "axa"),
    ("China Yangtze Power", "china yangtze power"),
    ("Duke Energy", "duke energy"),
    ("Valero Energy", "valero energy"),
    ("Canadian Natural Resources", "canadian natural resources"),
    ("Marathon Petroleum", "marathon petroleum"),
    ("Constellation Energy", "constellation energy"),
    ("Tata Consultancy Services", "tata consultancy services"),
    ("NetEase", "netease"),
    ("Intercontinental Exchange", "intercontinental exchange"),
    ("Aon", "aon"),
    ("NTT", "ntt"),
    ("Vinci", "vinci"),
    ("EOG Resources", "eog resources"),
    ("Suncor Energy", "suncor energy"),
    ("American Electric Power", "american electric power"),
    ("SLB", "slb"),
    ("Energy Transfer LP", "energy transfer lp"),
    ("ADNOC Gas", "adnoc gas"),
    ("Bajaj Finance", "bajaj finance"),
    ("Z.AI", "z.ai"),
    ("Monolithic Power Systems", "monolithic power systems"),
    ("Dominion Energy", "dominion energy"),
    ("Imperial Oil", "imperial oil"),
    ("Targa Resources", "targa resources"),
    ("Saudi Telecom Company", "saudi telecom company"),
    ("Cheniere Energy", "cheniere energy"),
    ("Occidental Petroleum", "occidental petroleum"),
    ("Larsen & Toubro", "larsen & toubro"),
    ("China Telecom", "china telecom"),
    ("LG Energy Solution", "lg energy solution"),
    ("Cenovus Energy", "cenovus energy"),
    ("Hoya", "hoya"),
    ("Devon Energy", "devon energy"),
    ("Swiss Re", "swiss re"),
    ("State Street Corporation", "state street corporation"),
    ("Walmex", "walmex"),
    ("Mercedes-Benz", "mercedes-benz"),
]

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("indeed_pipeline")

OUTPUT_DIR = Path(__file__).parent / "indeed_jobs"
OUTPUT_DIR.mkdir(exist_ok=True)
STATE_FILE = OUTPUT_DIR / "pipeline_state.json"


# ─── State Management ──────────────────────────────────────────────────────

def load_pipeline_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("Corrupt state file, starting fresh")
    return {"completed_companies": [], "total_jobs": 0}


def save_pipeline_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


# ─── JSON sanitization ─────────────────────────────────────────────────────

def sanitize(obj):
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, float) and math.isnan(obj):
        return None
    try:
        if obj is pd.NaT or obj is pd.NA:
            return None
    except Exception:
        pass
    return obj


def fetch_existing_ids(job_ids: list[str]) -> set[str]:
    """Query Supabase for which of these job IDs already exist in the DB."""
    if not SUPABASE_URL or not SUPABASE_KEY or not HAS_HTTPX or not job_ids:
        return set()
    try:
        base = SUPABASE_URL.rstrip("/")
        url = f"{base}/rest/v1/jobs" if "/rest/v1" not in base else f"{base}/jobs"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        }
        id_list = ",".join(job_ids)
        resp = httpx.get(
            url,
            headers=headers,
            params={"id": f"in.({id_list})", "select": "id"},
            timeout=20,
        )
        if resp.status_code == 200:
            return {row["id"] for row in resp.json()}
    except Exception as e:
        log.warning(f"  Could not fetch existing IDs from Supabase: {e}")
    return set()


def upsert_to_supabase(jobs: list[dict]):
    """Upsert only NEW jobs to Supabase, skipping duplicates that already exist."""
    if not SUPABASE_URL or not SUPABASE_KEY or not HAS_HTTPX:
        return
    if not jobs:
        return

    base = SUPABASE_URL.rstrip("/")
    url = f"{base}/rest/v1/jobs" if "/rest/v1" not in base else f"{base}/jobs"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    # 1. Strip fields not in DB schema
    STRIP_FIELDS = {"salary_currency", "also_on"}
    clean_jobs = [{k: v for k, v in j.items() if k not in STRIP_FIELDS} for j in jobs]

    # 2. Deduplicate within the batch (same id twice → Supabase 500)
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for job in clean_jobs:
        jid = job.get("id", "")
        if jid and jid not in seen_ids:
            seen_ids.add(jid)
            deduped.append(job)
    if len(deduped) < len(clean_jobs):
        log.info(f"  Removed {len(clean_jobs) - len(deduped)} within-batch duplicate IDs")

    # 3. Check which IDs already exist in DB and skip them
    all_ids = [j["id"] for j in deduped if j.get("id")]
    existing = fetch_existing_ids(all_ids)
    new_jobs = [j for j in deduped if j.get("id") not in existing]

    skipped = len(deduped) - len(new_jobs)
    if skipped:
        log.info(f"  Skipping {skipped} jobs already in DB. Upserting {len(new_jobs)} new.")
    if not new_jobs:
        log.info("  All jobs already in DB — nothing to upsert.")
        return

    # 4. Upsert in chunks of 200
    for i in range(0, len(new_jobs), 200):
        chunk = new_jobs[i : i + 200]
        try:
            resp = httpx.post(url, headers=headers, json=chunk, timeout=30)
            if resp.status_code in (200, 201):
                log.info(f"  Upserted {len(chunk)} new jobs to Supabase")
            else:
                log.warning(f"  Supabase upsert returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"  Supabase upsert failed: {e}")



# ─── Single company scraper ────────────────────────────────────────────────

def clean_num(val):
    """Return val unless it's a NaN float, in which case None."""
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


def scrape_company(
    search_term: str,
    company_match: str,
    location: str,
    hours_old: int | None,
    country: str,
) -> tuple[list[dict], dict]:
    """Scrape Indeed for a single company.

    Returns (normalized job dicts, stats) where stats has:
      batches   — number of successful fetches made
      hit_429   — whether any 429 was seen
    """

    log.info(f"  Scraping Indeed for '{search_term}' (filter: '{company_match}')")

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    stats = {"batches": 0, "hit_429": False}

    while offset < MAX_RESULTS_PER_CO:
        # Rate-limit between batches
        if offset > 0:
            delay = BASE_DELAY + random.uniform(1, 3)
            log.info(f"    Sleeping {delay:.1f}s between batches...")
            time.sleep(delay)

        # Fetch with retry + exponential backoff.
        # 429s get their own counter so a rate-limit pause doesn't burn a retry.
        batch_df = None
        pauses_429 = 0
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                log.info(f"    Fetching offset={offset} (attempt {attempt + 1}/{MAX_RETRIES})")
                kwargs = {
                    "site_name": ["indeed"],
                    "search_term": search_term,
                    "location": location,
                    "results_wanted": BATCH_SIZE,
                    "offset": offset,
                    "country_indeed": country,
                    "verbose": 0,
                }
                if hours_old is not None:
                    kwargs["hours_old"] = hours_old

                batch_df = scrape_jobs(**kwargs)
                break  # success

            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "too many" in err_str or "rate" in err_str:
                    stats["hit_429"] = True
                    pauses_429 += 1
                    if pauses_429 > MAX_429_PAUSES:
                        log.warning(f"    Repeated 429s at offset={offset}. Giving up on this batch.")
                        break
                    log.warning(f"    429 detected! Pausing {BACKOFF_429}s ({pauses_429}/{MAX_429_PAUSES})...")
                    time.sleep(BACKOFF_429)
                else:
                    attempt += 1
                    if attempt >= MAX_RETRIES:
                        log.warning(f"    Batch at offset={offset} abandoned after {MAX_RETRIES} failed attempts.")
                        break
                    wait = min(BASE_DELAY * (2 ** attempt) + random.uniform(0, 2), 300)
                    log.warning(f"    Error: {e}. Retrying in {wait:.0f}s...")
                    time.sleep(wait)

        if batch_df is None or (hasattr(batch_df, "empty") and batch_df.empty):
            log.info(f"    No more results for '{search_term}'. Moving on.")
            break

        stats["batches"] += 1

        # Filter by company name (case-insensitive substring)
        if "company" in batch_df.columns:
            matched = batch_df[
                batch_df["company"].str.lower().str.contains(
                    company_match.lower(), regex=False, na=False
                )
            ].copy()
        else:
            matched = batch_df.copy()

        # Dedup
        for _, row in matched.iterrows():
            url = row.get("job_url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Build normalized job dict
            title = str(row.get("title", ""))
            company = str(row.get("company", ""))
            loc = str(row.get("location", ""))
            desc = str(row.get("description", ""))[:5000]
            date_posted = row.get("date_posted")
            if hasattr(date_posted, "isoformat"):
                date_str = date_posted.isoformat()
            else:
                date_str = str(date_posted) if date_posted else None

            raw_key = f"indeed:{company}:{title}:{loc}"
            dedupe_id = hashlib.md5(raw_key.encode()).hexdigest()

            job = enrich_raw_job({
                "id": dedupe_id,
                "job_title": title,
                "company": company,
                "location": loc,
                "job_url": str(url),
                "apply_url": str(row.get("job_url_direct", url)),
                "description": desc,
                "salary": str(row.get("min_amount", "")) if row.get("min_amount") else None,
                "salary_min": clean_num(row.get("min_amount")),
                "salary_max": clean_num(row.get("max_amount")),
                "source_board": "Indeed",
                "scraper_type": "api",
                "job_type": str(row.get("job_type", "")).lower().replace(" ", "_") if row.get("job_type") else "full_time",
                "is_remote": bool(row.get("is_remote", False)),
                "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "created_at": date_str,
            })
            all_jobs.append(sanitize(job))

        log.info(f"    Batch: {len(matched)} matched / {len(batch_df)} total. Running total: {len(all_jobs)}")

        # Short page = last page; don't waste a request confirming it's empty
        if len(batch_df) < BATCH_SIZE:
            log.info(f"    Short page ({len(batch_df)} < {BATCH_SIZE}) — last page reached.")
            break
        offset += BATCH_SIZE

    return all_jobs, stats


# ─── Main pipeline ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Indeed Pipeline — Sequential Company Scraper")
    parser.add_argument("--post-time", choices=["hour", "day", "week", "month", "any"], default="day",
                        help="How old jobs can be (default: day = 24h)")
    parser.add_argument("--location", default="USA", help="Location to search (default: USA)")
    parser.add_argument("--country", default="usa", help="Indeed country code (default: usa)")
    parser.add_argument("--resume", action="store_true", help="Resume from last saved state")
    parser.add_argument("--company", type=str, help="Scrape only this one company (search term)")
    args = parser.parse_args()

    hours_old = POST_TIME_MAP.get(args.post_time)

    log.info("=" * 60)
    log.info("INDEED PIPELINE — Sequential Company Scraper")
    log.info(f"  Post-time:  {args.post_time} ({hours_old}h)" if hours_old else f"  Post-time:  any")
    log.info(f"  Location:   {args.location}")
    log.info(f"  Country:    {args.country}")
    log.info(f"  Companies:  {len(TARGET_COMPANIES)}")
    log.info("=" * 60)

    # Load state
    state = load_pipeline_state() if args.resume else {"completed_companies": [], "total_jobs": 0}
    completed = set(state["completed_companies"])

    # Filter to single company if requested
    companies = TARGET_COMPANIES
    if args.company:
        companies = [(s, m) for s, m in TARGET_COMPANIES if args.company.lower() in s.lower()]
        if not companies:
            log.error(f"No company matching '{args.company}' in target list.")
            return

    grand_total = state["total_jobs"]
    scraped_any = False        # whether we've made at least one scrape this run
    hot_remaining = 0          # companies left on the hot (long) cooldown after a 429
    prev_was_heavy = False     # previous company needed multiple batches

    for idx, (search_term, company_match) in enumerate(companies, 1):
        if search_term in completed:
            log.info(f"[{idx}/{len(companies)}] {search_term} — already completed, skipping")
            continue

        log.info(f"\n[{idx}/{len(companies)}] ── {search_term} ──────────────────────")

        # Adaptive cooldown between companies (skip before the first scrape).
        # Light 20-35s normally; full 60-90s after a heavy company or recent 429.
        if scraped_any:
            if hot_remaining > 0 or prev_was_heavy:
                cooldown = random.uniform(HOT_COOLDOWN_MIN, HOT_COOLDOWN_MAX)
                hot_remaining = max(0, hot_remaining - 1)
            else:
                cooldown = random.uniform(COMPANY_COOLDOWN_MIN, COMPANY_COOLDOWN_MAX)
            log.info(f"  Cooling down {cooldown:.0f}s before next company...")
            time.sleep(cooldown)

        jobs, stats = scrape_company(
            search_term=search_term,
            company_match=company_match,
            location=args.location,
            hours_old=hours_old,
            country=args.country,
        )
        scraped_any = True
        prev_was_heavy = stats["batches"] > 1
        if stats["hit_429"]:
            hot_remaining = HOT_STREAK_COMPANIES
            log.warning(f"  429 seen — using long cooldowns for next {HOT_STREAK_COMPANIES} companies")

        if jobs:
            log.info(f"  ✓ Found {len(jobs)} jobs for {search_term}")
            grand_total += len(jobs)
            # upsert_to_supabase(jobs)  # persistence disabled
        else:
            log.info(f"  ○ No jobs found for {search_term}")

        # Mark as completed & save state
        completed.add(search_term)
        state["completed_companies"] = list(completed)
        state["total_jobs"] = grand_total
        save_pipeline_state(state)

    log.info("\n" + "=" * 60)
    log.info("INDEED PIPELINE COMPLETE")
    log.info(f"  Total companies processed: {len(completed)}")
    log.info(f"  Total jobs collected: {grand_total}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
