#!/usr/bin/env python3
"""
LinkedIn 68-Roles Job Scraper
==============================
Scrapes LinkedIn for all 68 procurement/sourcing/buying roles, with:

  - Configurable post time  (--post-time day|week|month|any)
  - US-only results         (--location flag)
  - Role-level deduplication via MD5 of (title+company+location)
  - Incremental state saves  -> safe to Ctrl-C and resume (--resume)
  - Adaptive rate-limiting  -> detects 429s and backs off automatically
  - Exponential backoff     -> up to MAX_RETRIES per batch
  - Short-page detection    -> stops early when LinkedIn returns partial pages
  - LinkedIn's ~1000-result cap per role is handled gracefully
  - Single merged JSON + CSV output (all roles in one file)
  - Optional Supabase upsert (reads SUPABASE_URL / SUPABASE_KEY from .env)

Usage
-----
  # Basic (last 7 days, USA):
  python3 linkedin/linkedin_68_roles.py

  # Custom time window:
  python3 linkedin/linkedin_68_roles.py --post-time month

  # Resume a killed run:
  python3 linkedin/linkedin_68_roles.py --resume

  # Only one role (for testing):
  python3 linkedin/linkedin_68_roles.py --role "Procurement Manager"

  # First N roles only:
  python3 linkedin/linkedin_68_roles.py --limit 10

  # Specific role index range:
  python3 linkedin/linkedin_68_roles.py --start-idx 20 --end-idx 40

  # Different location:
  python3 linkedin/linkedin_68_roles.py --location "New York" --post-time week

  # Enable Supabase upsert:
  python3 linkedin/linkedin_68_roles.py --use-supabase

Notes on LinkedIn vs Indeed
----------------------------
  - LinkedIn is more aggressive about rate-limiting than Indeed.
    The cooldown values here are tuned HIGHER to be respectful.
  - LinkedIn does not use a `country_indeed` parameter; geo-filtering
    is done via the `location` field only ("United States" works best).
  - JobSpy's linkedin support uses `linkedin_fetch_description=True`
    to pull full job descriptions where available (slight extra latency).
  - LinkedIn's hard cap via JobSpy is typically ~1000 results/query.

Requirements
------------
  pip install python-jobspy pandas python-dotenv
"""

from __future__ import annotations

import sys
import os
import csv
import json
import math
import time
import random
import hashlib
import logging
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

# -- Dependency checks --------------------------------------------------------
try:
    import pandas as pd
    from jobspy import scrape_jobs
except ImportError as exc:
    print(
        f"ERROR: Missing required package: {exc.name}.\n"
        "Run: pip install python-jobspy pandas"
    )
    raise SystemExit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # optional

# -- Optional Supabase --------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

try:
    import httpx as _httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# -- Optional local enrichment ------------------------------------------------
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from scraper_utils import enrich_raw_job  # type: ignore
except Exception:
    def enrich_raw_job(j: dict) -> dict:  # type: ignore[misc]
        return j

# -- 68 Roles -----------------------------------------------------------------

ROLES: list[str] = [
    "Senior Commodity Manager",
    "Commodity Manager",
    "Commodity Buyer",
    "Commodity Lead",
    "Purchasing Commodity Manager",
    "Senior Commodity Buyer",
    "Global Supply Chain Analyst",
    "Global Supply Chain Specialist",
    "Global Supply Chain Lead",
    "Global Sourcing Specialist",
    "Global Commodity Manager",
    "Global Supply Chain Manager",
    "Commodity Specialist",
    "Procurement Manager",
    "Procurement Specialist",
    "Senior Procurement Specialist",
    "Senior Procurement Manager",
    "Assistant Procurement Manager",
    "Procurement Associate",
    "Senior Procurement Analyst",
    "Senior Procurement Officer",
    "Senior Procurement Engineer",
    "Senior Procurement Consultant",
    "Head of Procurement",
    "Procurement Officer",
    "Procurement Coordinator",
    "Procurement Engineer",
    "Procurement Assistant",
    "Sourcing Manager",
    "Sourcing Specialist",
    "Strategic Sourcing Manager",
    "Senior Manager Strategic Sourcing",
    "Senior Sourcing Analyst",
    "Senior Sourcing Manager",
    "Senior Sourcing Specialist",
    "Assistant Sourcing Manager",
    "Global Sourcing Lead",
    "Senior Manager Global Sourcing",
    "Strategic Sourcing Specialist",
    "Sourcing Lead",
    "Buyer",
    "Senior Buyer",
    "Purchasing Manager",
    "Purchasing Representative",
    "Procurement Buyer",
    "Commodity Analyst",
    "Senior Purchasing Agent",
    "Senior Purchasing Specialist",
    "Assistant Purchasing Agent",
    "Purchasing Assistant",
    "Senior Purchasing Analyst",
    "Senior Purchasing Officer",
    "Purchasing Coordinator",
    "Associate Buyer",
    "Senior Purchasing Assistant",
    "Purchasing Supervisor",
    "Purchase Specialist",
    "Junior Buyer",
    "Purchasing Officer",
    "Senior Purchasing Manager",
    "Purchasing Analyst",
    "Purchasing Buyer",
    "Materials Buyer",
    "Assistant Buyer",
    "Assistant Purchasing Manager",
    "Purchasing Consultant",
    "Purchasing Associate",
    "Purchasing Agent",
]

# -- Rate-limit / retry tuning ------------------------------------------------
# LinkedIn is MORE aggressive than Indeed. Cooldowns are intentionally higher.
BATCH_SIZE           = 50     # jobs per LinkedIn API call
MAX_RESULTS_PER_ROLE = 999999 # effectively unlimited; pagination stops on short page
MAX_RETRIES          = 5      # non-429 retries per batch
MAX_429_PAUSES       = 3      # 429-specific pauses before giving up on a batch
BASE_DELAY_SEC       = 5      # baseline sleep between pages (seconds)
ROLE_COOLDOWN_MIN    = 15     # min sleep between roles (normal)
ROLE_COOLDOWN_MAX    = 30     # max sleep between roles (normal)
HOT_COOLDOWN_MIN     = 90     # min sleep after a 429 event
HOT_COOLDOWN_MAX     = 150    # max sleep after a 429 event
HOT_STREAK_ROLES     = 4      # roles to stay on hot cooldown after a 429
BACKOFF_429_SEC      = 360    # hard 6-min pause on 429 detection (longer than Indeed)

# -- Parallel description fetching --------------------------------------------
DESC_WORKERS         = 32     # concurrent threads for description fetching
DESC_TIMEOUT         = 15     # seconds per description request
DESC_RETRIES         = 2      # retries per failed description fetch

# -- Post-time mapping --------------------------------------------------------
# LinkedIn JobSpy uses hours_old for recency filtering (same as Indeed).
POST_TIME_MAP: dict[str, int | None] = {
    "day":    24,
    "3day":   72,
    "week":   168,
    "2week":  336,
    "3week":  504,
    "month":  720,
    "any":    None,
}

# -- Output paths -------------------------------------------------------------
OUTPUT_DIR = Path(__file__).parent / "linkedin_jobs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CSV_FILE         = OUTPUT_DIR / "linkedin_68roles_jobs_us.csv"
CSV_STRICT_FILE  = OUTPUT_DIR / "linkedin_68roles_jobs_us_strict.csv"
STATE_FILE       = OUTPUT_DIR / ".linkedin_68roles_state.json"

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("linkedin_68roles")


# -- Helpers ------------------------------------------------------------------

def sanitize(obj: Any) -> Any:
    """Recursively make a value JSON/CSV safe."""
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


def make_job_id(title: str, company: str, location: str) -> str:
    """Stable MD5 dedup key — survives across roles and runs."""
    key = f"linkedin:{title.lower().strip()}:{company.lower().strip()}:{location.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def clean_num(val: Any) -> Any:
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


# -- State management ---------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            log.info(
                f"Resumed state: {len(state.get('completed_roles', []))} roles done, "
                f"{state.get('total_jobs', 0)} jobs so far."
            )
            return state
        except json.JSONDecodeError:
            log.warning("Corrupt state file — starting fresh.")
    return {"completed_roles": [], "total_jobs": 0, "seen_ids": []}


def save_state(state: dict) -> None:
    """Atomic write: .tmp then rename — never corrupts on crash."""
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# -- Disk I/O -----------------------------------------------------------------

def load_existing_jobs() -> list[dict]:
    if CSV_FILE.exists() and CSV_FILE.stat().st_size > 0:
        try:
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                jobs = list(reader)
            log.info(f"Loaded {len(jobs)} existing jobs from {CSV_FILE.name}")
            return jobs
        except Exception as e:
            log.warning(f"Could not load existing CSV: {e}")
    return []


def append_jobs_to_disk(new_jobs: list[dict]) -> None:
    if not new_jobs:
        return

    write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
    start_idx = 0
    if not write_header:
        try:
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                start_idx = max(0, sum(1 for _ in f) - 1)
        except Exception:
            start_idx = 0

    fieldnames = list(new_jobs[0].keys())
    if "Index_No" not in fieldnames:
        fieldnames = ["Index_No"] + fieldnames

    with open(CSV_FILE, "a", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_NONNUMERIC,
            escapechar="\\",
        )
        if write_header:
            writer.writeheader()
        for idx, job in enumerate(new_jobs, start=start_idx + 1):
            row = dict(job)
            row["Index_No"] = idx
            writer.writerow(row)


def export_strict_datasets() -> None:
    """Filter master CSV to only jobs where job_title strictly matches target role titles."""
    try:
        if not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0:
            return
        roles_sorted = sorted([r.lower() for r in ROLES], key=len, reverse=True)
        pattern = re.compile(r'\b(' + '|'.join(re.escape(r) for r in roles_sorted) + r')\b', re.IGNORECASE)

        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            strict_jobs = []
            for row in reader:
                if pattern.search(str(row.get("job_title", ""))):
                    strict_jobs.append(row)

        with open(CSV_STRICT_FILE, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                quoting=csv.QUOTE_NONNUMERIC,
                escapechar="\\",
            )
            writer.writeheader()
            for idx, row in enumerate(strict_jobs, start=1):
                row["Index_No"] = idx
                writer.writerow(row)

        log.info(f"Strict dataset updated: {len(strict_jobs)} jobs saved to {CSV_STRICT_FILE.name}")
    except Exception as e:
        log.warning(f"Could not export strict dataset: {e}")


# -- Parallel description fetching -------------------------------------------

def _fetch_one_description(url: str) -> str:
    """
    Fetch the full text description from a LinkedIn job URL.
    Tries to extract the description div from the HTML.
    Returns empty string on failure.
    """
    try:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        for attempt in range(DESC_RETRIES + 1):
            try:
                resp = httpx.get(url, headers=headers, timeout=DESC_TIMEOUT, follow_redirects=True)
                if resp.status_code == 200:
                    text = resp.text
                    # Extract description from LinkedIn's HTML
                    # Try the main description section first
                    patterns = [
                        r'<div[^>]*class="[^"]*description__text[^"]*"[^>]*>(.*?)</div>',
                        r'<section[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</section>',
                        r'<div[^>]*data-test-id="job-description"[^>]*>(.*?)</div>',
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
                        if match:
                            raw = match.group(1)
                            # Strip HTML tags
                            clean = re.sub(r'<[^>]+>', ' ', raw)
                            clean = re.sub(r'\s+', ' ', clean).strip()
                            if len(clean) > 100:
                                return clean[:8000]
                    # Fallback: look for JSON-LD structured data
                    jld = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', text, re.DOTALL)
                    if jld:
                        try:
                            import json as _json
                            data = _json.loads(jld.group(1))
                            desc = data.get("description", "")
                            if desc:
                                clean = re.sub(r'<[^>]+>', ' ', desc)
                                clean = re.sub(r'\s+', ' ', clean).strip()
                                return clean[:8000]
                        except Exception:
                            pass
                elif resp.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
            except Exception:
                if attempt < DESC_RETRIES:
                    time.sleep(2)
    except ImportError:
        pass  # httpx not available
    return ""


def async_fetch_descriptions(
    jobs: list[dict],
    workers: int = DESC_WORKERS,
) -> list[dict]:
    """
    Given a list of job dicts (each with 'job_url'), fetch full descriptions
    in parallel using a thread pool. Updates description field in-place.
    Only fetches for jobs with empty/short descriptions.
    """
    to_fetch = [
        (i, j) for i, j in enumerate(jobs)
        if not j.get("description") or len(str(j.get("description", ""))) < 200
    ]
    if not to_fetch:
        return jobs

    log.info(f"    Fetching descriptions for {len(to_fetch)} jobs via {workers} threads...")
    fetched = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(_fetch_one_description, j["job_url"]): i
            for i, j in to_fetch
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                desc = future.result()
                if desc:
                    jobs[idx]["description"] = desc
                    fetched += 1
            except Exception as e:
                log.debug(f"    Desc fetch failed for job idx {idx}: {e}")

    log.info(f"    Descriptions fetched: {fetched}/{len(to_fetch)}")
    return jobs


# -- Supabase (optional) ------------------------------------------------------

def upsert_to_supabase(jobs: list[dict]) -> None:
    if not (SUPABASE_URL and SUPABASE_KEY and HAS_HTTPX and jobs):
        return

    url = SUPABASE_URL.rstrip("/") + "/rest/v1/jobs"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }

    STRIP_FIELDS = {"salary_currency", "also_on"}
    clean = [{k: v for k, v in j.items() if k not in STRIP_FIELDS} for j in jobs]

    seen: set[str] = set()
    deduped = []
    for job in clean:
        jid = job.get("id", "")
        if jid and jid not in seen:
            seen.add(jid)
            deduped.append(job)

    for i in range(0, len(deduped), 200):
        chunk = deduped[i : i + 200]
        try:
            resp = _httpx.post(url, headers=headers, json=chunk, timeout=30)
            if resp.status_code in (200, 201):
                log.info(f"  Supabase: upserted {len(chunk)} jobs")
            else:
                log.warning(f"  Supabase {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"  Supabase upsert failed: {e}")


# -- Core: scrape one role ----------------------------------------------------

def scrape_role(
    role: str,
    location: str,
    hours_old: int | None,
    global_seen_ids: set[str],
    fetch_description: bool,
    max_results: int = 999999,
    base_delay_sec: float = BASE_DELAY_SEC,
    desc_workers: int = DESC_WORKERS,
) -> tuple[list[dict], dict]:
    """
    Scrape LinkedIn for one role title.
    Returns (job_list, stats)  where stats = {batches, hit_429}
    """
    log.info(f"  Role: '{role}'")

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    stats = {"batches": 0, "hit_429": False}

    while offset < max_results:
        # Polite delay between pages
        if offset > 0:
            delay = base_delay_sec + random.uniform(1.0, 3.0)
            log.info(f"    Sleeping {delay:.1f}s between pages...")
            time.sleep(delay)

        batch_df: "pd.DataFrame | None" = None
        pauses_429 = 0
        attempt = 0

        # Fetch with retry
        while attempt < MAX_RETRIES:
            try:
                log.info(f"    offset={offset} (attempt {attempt + 1}/{MAX_RETRIES})")
                kwargs: dict[str, Any] = {
                    "site_name":                ["linkedin"],
                    "search_term":              role,
                    "location":                 location,
                    "results_wanted":           BATCH_SIZE,
                    "offset":                   offset,
                    # Always False here — we fetch descriptions in parallel below
                    "linkedin_fetch_description": False,
                    "verbose":                  0,
                }
                if hours_old is not None:
                    kwargs["hours_old"] = hours_old

                batch_df = scrape_jobs(**kwargs)
                break  # success

            except Exception as exc:
                err = str(exc).lower()

                if any(t in err for t in ("429", "too many", "rate limit", "ratelimit", "captcha")):
                    stats["hit_429"] = True
                    pauses_429 += 1
                    if pauses_429 > MAX_429_PAUSES:
                        log.warning(
                            f"    Repeated 429s at offset={offset}. "
                            "Stopping pages for this role."
                        )
                        return all_jobs, stats
                    log.warning(
                        f"    429/Rate-limit detected — pausing {BACKOFF_429_SEC}s "
                        f"({pauses_429}/{MAX_429_PAUSES})..."
                    )
                    time.sleep(BACKOFF_429_SEC)
                    # Do NOT increment attempt — 429 pauses are not retries

                else:
                    attempt += 1
                    if attempt >= MAX_RETRIES:
                        log.warning(
                            f"    offset={offset} failed after {MAX_RETRIES} attempts. Skipping page."
                        )
                        break
                    wait = min(BASE_DELAY_SEC * (2 ** attempt) + random.uniform(0, 3), 300)
                    log.warning(f"    Error: {exc}. Retrying in {wait:.0f}s...")
                    time.sleep(wait)

        if batch_df is None or (hasattr(batch_df, "empty") and batch_df.empty):
            log.info(f"    No more results for '{role}'.")
            break

        stats["batches"] += 1
        batch_len = len(batch_df)
        new_in_batch = 0

        for _, row in batch_df.iterrows():
            url = str(row.get("job_url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title   = str(row.get("title",   "") or "").strip()
            company = str(row.get("company", "") or "").strip()
            loc     = str(row.get("location","") or "").strip()
            desc    = str(row.get("description", "") or "")[:5000]

            # Global cross-role dedup via MD5
            job_id = make_job_id(title, company, loc)
            if job_id in global_seen_ids:
                continue
            global_seen_ids.add(job_id)

            # Date normalisation
            date_posted = row.get("date_posted")
            if hasattr(date_posted, "isoformat"):
                date_str: str | None = date_posted.isoformat()
            elif date_posted:
                date_str = str(date_posted)
            else:
                date_str = None

            raw_job: dict[str, Any] = {
                "id":              job_id,
                "search_role":     role,
                "job_title":       title,
                "company":         company,
                "location":        loc,
                "job_url":         url,
                "apply_url":       str(row.get("job_url_direct") or url),
                "description":     desc,
                "salary_min":      clean_num(row.get("min_amount")),
                "salary_max":      clean_num(row.get("max_amount")),
                "salary_currency": str(row.get("currency") or ""),
                "job_type":        (str(row.get("job_type") or "").lower().replace(" ", "_") or "full_time"),
                "is_remote":       bool(row.get("is_remote", False)),
                "source_board":    "LinkedIn",
                "scraper_type":    "jobspy",
                "created_at":      date_str,
                "scraped_at":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }

            try:
                job = enrich_raw_job(raw_job)
            except Exception:
                job = raw_job

            all_jobs.append(sanitize(job))
            new_in_batch += 1

        log.info(
            f"    Page {offset // BATCH_SIZE + 1}: "
            f"{new_in_batch} new / {batch_len} returned "
            f"(role running total: {len(all_jobs)})"
        )

        # Parallel description fetch for this page's new jobs
        if fetch_description and new_in_batch > 0:
            page_new = all_jobs[-new_in_batch:]
            all_jobs[-new_in_batch:] = async_fetch_descriptions(page_new, workers=desc_workers)

        # Short page = last page (LinkedIn signals end of results this way)
        if batch_len < BATCH_SIZE:
            log.info(f"    Short page ({batch_len} < {BATCH_SIZE}) — last page.")
            break

        offset += BATCH_SIZE

    return all_jobs, stats


# -- Main pipeline ------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LinkedIn 68-Roles Scraper — procurement/sourcing/buying jobs"
    )
    parser.add_argument(
        "--post-time",
        choices=list(POST_TIME_MAP.keys()),
        default="week",
        help="How recent jobs must be (default: week = last 7 days)",
    )
    parser.add_argument(
        "--location",
        default="United States",
        help="Search location (default: United States)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last saved state",
    )
    parser.add_argument(
        "--role",
        type=str,
        default=None,
        help="Scrape only one role (case-insensitive substring match)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of roles to run (e.g., 10 for the first 10 roles)",
    )
    parser.add_argument(
        "--start-idx",
        type=int,
        default=1,
        help="Start role index (1-based, inclusive)",
    )
    parser.add_argument(
        "--end-idx",
        type=int,
        default=None,
        help="End role index (1-based, inclusive)",
    )
    parser.add_argument(
        "--no-descriptions",
        action="store_true",
        help="Skip fetching full job descriptions (faster, less data)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Explicit max hours old (overrides --post-time, e.g. 504 for 3 weeks)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Enable fast mode: drops cooldowns between roles to 3-6s and reduces page delays",
    )
    parser.add_argument(
        "--shard",
        type=str,
        default=None,
        help="Run only a partition of roles, e.g. 1/4, 2/4 (for parallel GitHub Actions runners)",
    )
    parser.add_argument(
        "--max-results",
        type=str,
        default="1000",
        help="Maximum results wanted per role, or 'all' for all available (default: 1000)",
    )
    parser.add_argument(
        "--use-supabase",
        action="store_true",
        help="Enable Supabase upsert if credentials are set (disabled by default)",
    )
    args = parser.parse_args()

    global CSV_FILE, STATE_FILE, CSV_STRICT_FILE
    if args.shard:
        shard_label = args.shard.replace("/", "-")
        CSV_FILE         = OUTPUT_DIR / f"linkedin_shard_{shard_label}_jobs.csv"
        CSV_STRICT_FILE  = OUTPUT_DIR / f"linkedin_shard_{shard_label}_jobs_strict.csv"
        STATE_FILE       = OUTPUT_DIR / f".linkedin_shard_{shard_label}_state.json"

    hours_old        = args.hours if args.hours is not None else POST_TIME_MAP.get(args.post_time)
    use_supabase     = args.use_supabase
    fetch_description = not args.no_descriptions

    if not args.max_results or str(args.max_results).lower() in ("all", "max", "0"):
        max_results = 999999  # fetch all available; pagination ends on short page
    else:
        try:
            max_results = max(1, int(args.max_results))
        except ValueError:
            max_results = 999999

    role_cd_min      = 3 if args.fast else ROLE_COOLDOWN_MIN
    role_cd_max      = 6 if args.fast else ROLE_COOLDOWN_MAX
    base_delay       = 2.0 if args.fast else BASE_DELAY_SEC

    log.info("=" * 65)
    log.info("LINKEDIN 68-ROLES SCRAPER")
    log.info(f"  Roles        : {len(ROLES)}")
    log.info(f"  Post-time    : {args.post_time}" + (f" ({hours_old}h)" if hours_old else " (any time)"))
    log.info(f"  Location     : {args.location}")
    log.info(f"  Descriptions : {'yes' if fetch_description else 'no (--no-descriptions)'}")
    log.info(f"  Output CSV   : {CSV_FILE}")
    log.info(f"  Strict CSV   : {CSV_STRICT_FILE}")
    log.info(f"  Supabase     : {'enabled' if (use_supabase and SUPABASE_URL and SUPABASE_KEY) else 'disabled'}")
    log.info("=" * 65)

    # Load / init state
    state = load_state() if args.resume else {"completed_roles": [], "total_jobs": 0, "seen_ids": []}

    completed:       set[str] = set(state.get("completed_roles", []))
    grand_total:     int      = state.get("total_jobs", 0)
    global_seen_ids: set[str] = set(state.get("seen_ids", []))

    # Always pull IDs from any existing output file to prevent redundancies
    for job in load_existing_jobs():
        if job.get("id"):
            global_seen_ids.add(job["id"])

    # Role filter
    roles_to_run = ROLES
    if args.role:
        roles_to_run = [r for r in ROLES if args.role.lower() in r.lower()]
        if not roles_to_run:
            log.error(f"No role matching '{args.role}' in the 68-role list.")
            return
        log.info(f"  Filtered to {len(roles_to_run)} role(s) by name: {roles_to_run}")

    if args.shard:
        try:
            part, total = map(int, args.shard.split("/"))
            chunk_size = math.ceil(len(roles_to_run) / total)
            start_i = (part - 1) * chunk_size
            end_i = min(len(roles_to_run), part * chunk_size)
            roles_to_run = roles_to_run[start_i:end_i]
            log.info(f"  Shard {args.shard}: running roles {start_i + 1} through {end_i} ({len(roles_to_run)} roles)")
        except Exception as e:
            log.error(f"Invalid shard format '{args.shard}': {e}")
            return

    run_jobs:       list[dict] = []
    hot_remaining:  int        = 0
    prev_was_heavy: bool       = False
    scraped_any:    bool       = False

    # Main loop
    for idx, role in enumerate(roles_to_run, start=1):
        if role in completed:
            log.info(f"[{idx:>2}/{len(roles_to_run)}] '{role}' — already completed, skipping")
            continue

        log.info(f"\n[{idx:>2}/{len(roles_to_run)}] -- {role} " + "-" * max(1, 48 - len(role)))

        # Inter-role cooldown
        if scraped_any:
            if hot_remaining > 0 or prev_was_heavy:
                cooldown = random.uniform(HOT_COOLDOWN_MIN, HOT_COOLDOWN_MAX)
                hot_remaining = max(0, hot_remaining - 1)
                log.info(f"  [HOT cooldown] Sleeping {cooldown:.0f}s...")
            else:
                cooldown = random.uniform(role_cd_min, role_cd_max)
                log.info(f"  Sleeping {cooldown:.0f}s before next role...")
            time.sleep(cooldown)

        try:
            jobs, stats = scrape_role(
                role=role,
                location=args.location,
                hours_old=hours_old,
                global_seen_ids=global_seen_ids,
                fetch_description=fetch_description,
                max_results=max_results,
                base_delay_sec=base_delay,
                desc_workers=DESC_WORKERS,
            )
        except KeyboardInterrupt:
            log.warning("\nInterrupted — saving state...")
            _flush(state, completed, grand_total, global_seen_ids, run_jobs, use_supabase)
            return
        except Exception as exc:
            log.error(f"Unexpected error on '{role}': {exc}", exc_info=True)
            jobs, stats = [], {"batches": 0, "hit_429": False}

        scraped_any    = True
        prev_was_heavy = stats["batches"] > 1
        if stats["hit_429"]:
            hot_remaining = HOT_STREAK_ROLES
            log.warning(f"  429 seen — hot cooldowns for next {HOT_STREAK_ROLES} roles.")

        if jobs:
            log.info(f"  Found {len(jobs)} new jobs for '{role}'")
            run_jobs.extend(jobs)
            grand_total += len(jobs)
            append_jobs_to_disk(jobs)
            if use_supabase:
                upsert_to_supabase(jobs)
        else:
            log.info(f"  No new jobs for '{role}'")

        completed.add(role)
        state["completed_roles"] = list(completed)
        state["total_jobs"]      = grand_total
        state["seen_ids"]        = list(global_seen_ids)
        save_state(state)

    export_strict_datasets()

    # Clean up state file on complete run so only the 2 CSV files remain
    if not args.shard and STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
        except Exception:
            pass

    log.info("\n" + "=" * 65)
    log.info("SCRAPER COMPLETE — 2 Master Files Generated:")
    log.info(f"  Roles processed   : {len(completed)}")
    log.info(f"  New jobs (run)    : {len(run_jobs)}")
    log.info(f"  Total jobs        : {grand_total}")
    log.info(f"  1. Master CSV     : {CSV_FILE}")
    log.info(f"  2. Strict CSV     : {CSV_STRICT_FILE}")
    log.info("=" * 65)


def _flush(
    state: dict,
    completed: set[str],
    grand_total: int,
    seen_ids: set[str],
    run_jobs: list[dict],
    use_supabase: bool,
) -> None:
    """Flush everything on KeyboardInterrupt — never lose data."""
    if run_jobs:
        append_jobs_to_disk(run_jobs)
        if use_supabase:
            upsert_to_supabase(run_jobs)
    export_strict_datasets()
    state["completed_roles"] = list(completed)
    state["total_jobs"]      = grand_total
    state["seen_ids"]        = list(seen_ids)
    save_state(state)
    log.info(f"State saved. Resume with: python3 {Path(__file__).name} --resume")


if __name__ == "__main__":
    main()
