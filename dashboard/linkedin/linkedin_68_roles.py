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
  - Search cards are collected through JobSpy, then each job page is fetched
    once to recover JSON-LD, job criteria, the direct apply URL, and the full
    description before matching fields are derived.
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
import html as html_lib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# -- Dependency checks --------------------------------------------------------
try:
    import pandas as pd
except ImportError:
    print("ERROR: Missing required package: pandas.\nRun: pip install pandas")
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
    from job_extraction.jd_enricher import (  # type: ignore
        enrich as enrich_matching_fields,
        split_jd_sections,
    )
except Exception:
    def enrich_raw_job(j: dict) -> dict:  # type: ignore[misc]
        return j

    def enrich_matching_fields(j: dict) -> dict:  # type: ignore[misc]
        return j

    def split_jd_sections(description: str) -> dict[str, str]:  # type: ignore[misc]
        return {}

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
BACKOFF_429_SEC      = 360    # (kept for the Indeed-style callers) see BACKOFF_429_STEPS
# LinkedIn's rate-limit windows on shared GitHub runners last a few minutes; waiting a long time after
# the first 429 only burns the job's time, so the waits grow: 45 s, 90 s, 180 s, then the role is left for later
BACKOFF_429_STEPS    = (45, 90, 180)
PARTIAL_EXIT_CODE    = 3      # exit code of a shard that saved its jobs but could not collect every role
MAX_BLOCKED_ROLES    = 3      # roles in a row left blocked: LinkedIn is refusing this runner, stop the shard
RETRY_BLOCKED_AFTER  = 480    # seconds to wait before trying the blocked roles once more
TIME_BUDGET_MIN      = float(os.getenv("LINKEDIN_TIME_BUDGET_MIN", "0") or 0)  # 0 = no limit

_deadline: float | None = None  # time.monotonic() value after which the run saves what it has and stops


def out_of_time(margin: float = 0.0) -> bool:
    return _deadline is not None and time.monotonic() + margin >= _deadline

# -- Parallel description fetching --------------------------------------------
DESC_WORKERS         = 8      # LinkedIn throttles aggressive detail-page bursts
DESC_RETRY_WORKERS   = 3      # slower second pass for jobs missed on the first pass
DESC_RETRY_DELAY     = 8      # cooldown before the second description pass
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


def clean_bool(val: Any) -> bool | None:
    """Preserve true/false values without treating pandas NaN as true."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


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
            row = {
                key: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list)) else value
                for key, value in job.items()
            }
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


# -- Parallel LinkedIn detail fetching ---------------------------------------

_DETAIL_FIELDS = (
    "description", "job_title", "company", "location", "posted_at",
    "apply_url", "job_type", "experience", "work_mode", "is_remote",
    "salary", "salary_min", "salary_max", "salary_currency", "salary_period",
    "skills", "education_requirements",
    "basic_qualifications", "preferred_qualifications", "key_responsibilities",
    "job_function", "company_industry",
    "poster_name", "poster_title", "poster_url",
)


def _clean_detail_text(value: Any, *, preserve_lines: bool = False) -> str:
    """Convert an HTML fragment to readable text while retaining JD headings."""
    if not value:
        return ""
    raw = html_lib.unescape(str(value))
    try:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(raw, "html.parser").get_text(
            "\n" if preserve_lines else " ", strip=True
        )
    except ImportError:
        text = re.sub(r"<(?:br|/p|/li|/h[1-6])[^>]*>", "\n", raw, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    if preserve_lines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", text).strip()


def _job_postings(value: Any):
    """Yield JobPosting objects from JSON-LD objects, lists, and @graph blocks."""
    if isinstance(value, list):
        for item in value:
            yield from _job_postings(item)
        return
    if not isinstance(value, dict):
        return
    item_type = value.get("@type")
    types = item_type if isinstance(item_type, list) else [item_type]
    if any(str(t).lower() == "jobposting" for t in types if t):
        yield value
    yield from _job_postings(value.get("@graph"))


def _json_ld_text(value: Any) -> str:
    """Render schema.org text/list/object values without serializing raw JSON."""
    if isinstance(value, list):
        return "\n".join(filter(None, (_json_ld_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("description", "name", "value", "credentialCategory"):
            if value.get(key):
                return _json_ld_text(value[key])
        return ""
    return _clean_detail_text(value, preserve_lines=True)


def _json_ld_location(posting: dict[str, Any]) -> str:
    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    rendered: list[str] = []
    for item in locations if isinstance(locations, list) else []:
        if not isinstance(item, dict):
            continue
        address = item.get("address") or item
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name") or country.get("value")
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            country,
        ]
        location = ", ".join(str(part).strip() for part in parts if part)
        if location and location not in rendered:
            rendered.append(location)
    return "; ".join(rendered)


def _json_ld_salary(posting: dict[str, Any]) -> dict[str, Any]:
    base = posting.get("baseSalary")
    if not isinstance(base, dict):
        return {}
    value = base.get("value")
    value = value if isinstance(value, dict) else {}
    low = value.get("minValue")
    high = value.get("maxValue")
    if low is None and high is None and isinstance(value.get("value"), (int, float)):
        low = high = value["value"]
    for name, amount in (("low", low), ("high", high)):
        if isinstance(amount, str):
            try:
                parsed = float(amount.replace(",", "").strip())
                if name == "low":
                    low = parsed
                else:
                    high = parsed
            except ValueError:
                pass
    if low is None and high is None:
        return {}
    currency = base.get("currency") or value.get("currency")
    unit = str(value.get("unitText") or "").lower()
    period = {
        "hour": "hour", "hourly": "hour", "day": "day", "daily": "day",
        "week": "week", "weekly": "week", "month": "month", "monthly": "month",
        "year": "year", "yearly": "year", "annual": "year",
    }.get(unit)
    result = {
        "salary_min": low,
        "salary_max": high,
        "salary_currency": currency,
        "salary_period": period,
    }
    amount = " - ".join(str(v) for v in (low, high) if v is not None)
    result["salary"] = " ".join(part for part in (str(currency or ""), amount, f"per {period}" if period else "") if part)
    return result


def _employment_type(value: Any) -> str:
    if isinstance(value, list):
        value = next((item for item in value if item), "")
    normalized = re.sub(r"[_-]+", " ", str(value or "")).strip().lower()
    mapping = {
        "full time": "full time", "part time": "part time",
        "contractor": "contract", "contract": "contract",
        "temporary": "temporary", "intern": "internship", "internship": "internship",
    }
    return mapping.get(normalized, normalized)


def _parse_linkedin_detail_html(page_html: str) -> dict[str, Any]:
    """Extract every useful, explicitly published field from a LinkedIn job page."""
    details: dict[str, Any] = {}
    posting: dict[str, Any] = {}

    for match in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            payload = json.loads(html_lib.unescape(match.group(1)).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        posting = next(_job_postings(payload), {})
        if posting:
            break

    if posting:
        organization = posting.get("hiringOrganization") or {}
        details.update({
            "job_title": _clean_detail_text(posting.get("title")),
            "company": _clean_detail_text(organization.get("name")) if isinstance(organization, dict) else "",
            "description": _clean_detail_text(posting.get("description"), preserve_lines=True)[:12_000],
            "posted_at": posting.get("datePosted"),
            "location": _json_ld_location(posting),
            "skills": _json_ld_text(posting.get("skills")),
            "experience": _json_ld_text(posting.get("experienceRequirements")),
            "education_requirements": _json_ld_text(posting.get("educationRequirements")),
            "basic_qualifications": _json_ld_text(posting.get("qualifications")),
            "preferred_qualifications": _json_ld_text(posting.get("preferredQualifications")),
            "key_responsibilities": _json_ld_text(posting.get("responsibilities")),
            "company_industry": _json_ld_text(posting.get("industry")),
        })
        job_type = _employment_type(posting.get("employmentType"))
        if job_type:
            details["job_type"] = job_type
        remote_type = str(posting.get("jobLocationType") or "").upper()
        if "TELECOMMUTE" in remote_type or "REMOTE" in remote_type:
            details.update(work_mode="remote", is_remote=True)
        details.update(_json_ld_salary(posting))

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page_html, "html.parser")
        if not details.get("description"):
            node = soup.find(
                ["div", "section"],
                class_=lambda value: value and any(
                    marker in " ".join(value) if isinstance(value, list) else marker in str(value)
                    for marker in ("show-more-less-html__markup", "description__text")
                ),
            ) or soup.find(attrs={"data-test-id": "job-description"})
            if node:
                details["description"] = _clean_detail_text(str(node), preserve_lines=True)[:12_000]

        for item in soup.find_all(class_=lambda value: value and "description__job-criteria-item" in str(value)):
            heading = item.find("h3")
            content = item.find("span", class_=lambda value: value and "description__job-criteria-text" in str(value))
            label = _clean_detail_text(heading.get_text(" ") if heading else "").lower()
            value = _clean_detail_text(content.get_text(" ") if content else "")
            if not label or not value:
                continue
            if "employment type" in label:
                details["job_type"] = _employment_type(value)
            elif "seniority level" in label:
                details["experience"] = value
            elif "job function" in label:
                details["job_function"] = value
            elif "industr" in label:
                details["company_industry"] = value
            elif "workplace" in label or "work mode" in label:
                mode = value.lower().replace("-", "")
                if "remote" in mode:
                    details.update(work_mode="remote", is_remote=True)
                elif "hybrid" in mode:
                    details.update(work_mode="hybrid", is_remote=False)
                elif "onsite" in mode or "on site" in value.lower():
                    details.update(work_mode="onsite", is_remote=False)

        # "Direct message the job poster from <company>": the person who posted it (on some jobs' public page)
        poster = soup.select_one(".message-the-recruiter")
        if poster:
            name = poster.select_one(".base-main-card__title")
            title = poster.select_one(".base-main-card__subtitle")
            link = poster.select_one("a.base-card__full-link[href]")
            if name and name.get_text(strip=True):
                details["poster_name"] = name.get_text(" ", strip=True)
                details["poster_title"] = title.get_text(" ", strip=True) if title else ""
                details["poster_url"] = (link["href"].split("?")[0] if link else "")

        apply_code = soup.find("code", id="applyUrl")
        if apply_code:
            encoded = html_lib.unescape(apply_code.decode_contents()).strip().strip('"')
            match = re.search(r"[?&]url=([^\"&<]+)", encoded)
            direct_url = unquote(match.group(1) if match else encoded)
            if direct_url.startswith(("http://", "https://")):
                details["apply_url"] = direct_url
    except ImportError:
        pass

    description = str(details.get("description") or "")
    if description:
        details.update(split_jd_sections(description))
    return {key: value for key, value in details.items() if value not in (None, "", [], {})}


from linkedin_guest_search import LIMIT as GUEST_LIMIT, US_STATES, WORK_TYPES, search_batch  # noqa: E402


def _fetch_one_job_details(url: str) -> dict[str, Any]:
    """Fetch and parse one LinkedIn detail page; return an empty dict on failure."""
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
                response = httpx.get(url, headers=headers, timeout=DESC_TIMEOUT, follow_redirects=True)
                if response.status_code == 200 and "/signup" not in str(response.url):
                    return _parse_linkedin_detail_html(response.text)
                if response.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
            except Exception:
                if attempt < DESC_RETRIES:
                    time.sleep(2)
    except ImportError:
        pass
    return {}


def _merge_job_details(job: dict[str, Any], details: dict[str, Any]) -> None:
    """Merge authoritative detail-page values without replacing useful data with blanks."""
    if not details:
        return
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if key == "description":
            if len(str(value)) > len(str(job.get(key) or "")):
                job[key] = value
        else:
            job[key] = value

    structured = dict(job.get("structured_fields") or {})
    if job.get("job_type"):
        structured["job_type"] = job["job_type"]
    if job.get("work_mode"):
        structured["work_mode"] = job["work_mode"]
    if job.get("salary_min") is not None or job.get("salary_max") is not None:
        structured["salary"] = {
            "min": job.get("salary_min"), "max": job.get("salary_max"),
            "currency": job.get("salary_currency"), "period": job.get("salary_period"),
        }
    job["structured_fields"] = structured


def async_fetch_job_details(
    jobs: list[dict],
    workers: int = DESC_WORKERS,
) -> list[dict]:
    """Fetch full LinkedIn details for every job and merge them in place."""
    to_fetch = [(index, job) for index, job in enumerate(jobs) if job.get("job_url")]
    if not to_fetch:
        return jobs
    log.info("    Fetching structured details for %d jobs via %d threads...", len(to_fetch), workers)
    fetched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_one_job_details, job["job_url"]): index
            for index, job in to_fetch
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                details = future.result()
                jobs[index]["_linkedin_detail_fetched"] = bool(details)
                if details:
                    _merge_job_details(jobs[index], details)
                    fetched += 1
            except Exception as exc:
                log.debug("    Detail fetch failed for job idx %d: %s", index, exc)
    log.info("    Structured details fetched: %d/%d", fetched, len(to_fetch))
    return jobs


def fetch_job_details_with_retry(jobs: list[dict], workers: int = DESC_WORKERS) -> list[dict]:
    """Fetch details, then retry blocked/incomplete pages at lower concurrency."""
    async_fetch_job_details(jobs, workers=workers)
    missing = [
        job for job in jobs
        if not job.get("_linkedin_detail_fetched")
        or len(str(job.get("description") or "")) < 200
    ]
    if missing:
        log.info(
            "    Retrying %d incomplete detail pages with %d workers after %ds...",
            len(missing), DESC_RETRY_WORKERS, DESC_RETRY_DELAY,
        )
        time.sleep(DESC_RETRY_DELAY)
        async_fetch_job_details(missing, workers=DESC_RETRY_WORKERS)
    return jobs


def enrich_jobs_after_details(jobs: list[dict]) -> list[dict]:
    """Run structured extraction only after the best description is available."""
    enriched: list[dict] = []
    for job in jobs:
        try:
            candidate = dict(job)
            candidate.pop("_linkedin_detail_fetched", None)
            # CSV reloads serialize the evidence object as text. Discard that
            # stale representation and rebuild it from the final description.
            if not isinstance(candidate.get("extraction"), dict):
                candidate.pop("extraction", None)
            result = enrich_raw_job(candidate)
            result = sanitize(enrich_matching_fields(result))
            result.pop("structured_fields", None)
            # Keep a stable set of columns across every CSV row.
            for key in _DETAIL_FIELDS:
                result.setdefault(key, None)
            result.setdefault("skills_required", [])
            result.setdefault("skills_preferred", [])
            result.setdefault("education_requirements", [])
            enriched.append(result)
        except Exception as exc:
            log.debug("    Structured extraction failed for %s: %s", job.get("id"), exc)
            enriched.append(sanitize(job))
    return enriched


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

# Words that only say how senior a role is ("Senior Buyer" is still a buyer)
_LEVEL_WORDS = {"senior", "sr", "junior", "jr", "assistant", "associate", "lead", "head", "of", "global", "strategic",
                "principal", "staff", "chief", "i", "ii", "iii", "the", "and", "&"}


def title_matches_role(role: str, title: str) -> bool:
    """Whether a job title is this role: every main word of the role is in it ("Buyer" / "Senior Buyer II")."""
    words = [w for w in re.findall(r"[a-z]+", role.lower()) if w not in _LEVEL_WORDS] or re.findall(r"[a-z]+", role.lower())
    title_words = set(re.findall(r"[a-z]+", title.lower()))
    stems = {w[:-1] if w.endswith("s") else w for w in title_words} | title_words
    return all(w in stems or (w.endswith("ing") and w[:-3] in stems) for w in words)


def scrape_role(
    role: str,
    location: str,
    hours_old: int | None,
    global_seen_ids: set[str],
    fetch_description: bool,
    max_results: int = 999999,
    base_delay_sec: float = BASE_DELAY_SEC,
    desc_workers: int = DESC_WORKERS,
    work_type: str | None = None,
) -> tuple[list[dict], dict]:
    """
    Scrape LinkedIn for one role title (optionally one work type: 1 on-site, 2 remote, 3 hybrid).
    Returns (job_list, stats)  where stats = {batches, hit_429, found, capped}: found is the number of
    distinct jobs LinkedIn listed for the search, capped whether it stopped at LinkedIn's ~1,000 limit.
    """
    log.info(f"  Role: '{role}'" + (f" ({WORK_TYPES[work_type]})" if work_type else ""))

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    offset = 0
    stats = {"batches": 0, "hit_429": False, "found": 0, "capped": False, "blocked": False}
    batches_without_new = 0
    batches_without_match = 0

    while offset < max_results:
        if out_of_time():
            log.warning("    Time budget used up: keeping what this role has so far.")
            stats["blocked"] = stats["blocked"] or stats["batches"] == 0
            break
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
                # LinkedIn's public search, 10 per request (descriptions are fetched in parallel below)
                batch_df = search_batch(role, location, hours_old, offset, BATCH_SIZE, work_type)
                break  # success

            except Exception as exc:
                err = str(exc).lower()

                if any(t in err for t in ("429", "too many", "rate limit", "ratelimit", "captcha")):
                    stats["hit_429"] = True
                    pauses_429 += 1
                    wait = BACKOFF_429_STEPS[min(pauses_429, len(BACKOFF_429_STEPS)) - 1] + random.uniform(0, 15)
                    if pauses_429 > len(BACKOFF_429_STEPS) or out_of_time(wait):
                        log.warning(
                            f"    Repeated 429s at offset={offset}. "
                            "Stopping pages for this role."
                        )
                        stats["blocked"] = True
                        return all_jobs, stats
                    log.warning(
                        f"    429/Rate-limit detected — pausing {wait:.0f}s "
                        f"({pauses_429}/{len(BACKOFF_429_STEPS)})..."
                    )
                    time.sleep(wait)
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
        # Jobs this search had not listed yet (before the cross-role dedup): LinkedIn repeats results near its limit
        listed_new = sum(1 for url in batch_df["job_url"] if str(url or "").strip() not in seen_urls)
        stats["found"] += listed_new
        # LinkedIn's search is loose and ranks real matches first: titles of this role thin out as it goes
        title_matches = sum(title_matches_role(role, str(title or "")) for title in batch_df.get("title", []))

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

            salary_min = clean_num(row.get("min_amount"))
            salary_max = clean_num(row.get("max_amount"))
            salary_currency = str(row.get("currency") or "")
            job_type = str(row.get("job_type") or "").strip()
            is_remote = clean_bool(row.get("is_remote"))
            structured_fields: dict[str, Any] = {}
            if salary_min is not None or salary_max is not None:
                structured_fields["salary"] = {
                    "min": salary_min,
                    "max": salary_max,
                    "currency": salary_currency or None,
                    "period": str(row.get("interval") or "") or None,
                }
            if job_type:
                structured_fields["job_type"] = job_type
            if is_remote:
                structured_fields["work_mode"] = "remote"

            raw_job: dict[str, Any] = {
                "id":              job_id,
                "search_role":     role,
                "role_category":   role,
                "job_title":       title,
                "company":         company,
                "location":        loc,
                "job_url":         url,
                "apply_url":       str(row.get("job_url_direct") or url),
                "description":     desc,
                "salary_min":      salary_min,
                "salary_max":      salary_max,
                "salary_currency": salary_currency,
                "job_type":        job_type,
                "is_remote":       is_remote,
                "structured_fields": structured_fields,
                "source_board":    "LinkedIn",
                "scraper_type":    "jobspy",
                "posted_at":       date_str,
                "scraped_at":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "experience":      str(row.get("job_level") or ""),
                "work_mode":       "remote" if is_remote else "",
                "salary":          "",
                "salary_period":   str(row.get("interval") or ""),
                "country":         None,
                "job_function":    str(row.get("job_function") or ""),
                "company_industry": str(row.get("company_industry") or ""),
                "basic_qualifications": None,
                "preferred_qualifications": None,
                "key_responsibilities": None,
            }

            # Detail fetching happens below. Extracting here would permanently
            # project empty skills/experience from a search-result summary.
            all_jobs.append(sanitize(raw_job))
            new_in_batch += 1

        log.info(
            f"    Page {offset // BATCH_SIZE + 1}: "
            f"{new_in_batch} new / {batch_len} returned "
            f"(role running total: {len(all_jobs)})"
        )

        # Fetch each job page once for all published structured details, then
        # derive matching fields only after the best description is available.
        if fetch_description and new_in_batch > 0:
            page_new = all_jobs[-new_in_batch:]
            page_new = fetch_job_details_with_retry(page_new, workers=desc_workers)
            all_jobs[-new_in_batch:] = enrich_jobs_after_details(page_new)
        elif new_in_batch > 0:
            page_new = all_jobs[-new_in_batch:]
            all_jobs[-new_in_batch:] = enrich_jobs_after_details(page_new)

        # The end is an empty page or LinkedIn's limit, not a short batch: a throttled or partly repeated
        # batch can be short with many results still to come
        batches_without_new = batches_without_new + 1 if listed_new == 0 else 0
        if batch_df.attrs.get("end"):
            stats["capped"] = offset + BATCH_SIZE >= GUEST_LIMIT
            log.info("    Reached LinkedIn's result limit." if stats["capped"] else "    End of results.")
            break
        if batches_without_new >= 2:
            log.info("    Only repeats in the last two batches: end of results.")
            break
        batches_without_match = batches_without_match + 1 if title_matches == 0 else 0
        if batches_without_match >= 2:
            log.info(f"    No '{role}' titles in the last two batches: the rest is loosely related, stopping.")
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
        "--time-budget",
        type=float,
        default=None,
        help="Minutes after which the run saves what it has and stops cleanly (default: LINKEDIN_TIME_BUDGET_MIN, 0 = no limit)",
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
    global _deadline
    budget = args.time_budget if args.time_budget is not None else TIME_BUDGET_MIN
    _deadline = time.monotonic() + budget * 60 if budget else None
    roles_to_run = ROLES
    if args.role:
        roles_to_run = [r for r in ROLES if args.role.lower() in r.lower()]
        if not roles_to_run:
            log.error(f"No role matching '{args.role}' in the 68-role list.")
            return
        log.info(f"  Filtered to {len(roles_to_run)} role(s) by name: {roles_to_run}")

    if args.start_idx or args.end_idx:
        start_i = max(0, (args.start_idx or 1) - 1)
        end_i = args.end_idx if args.end_idx is not None else len(roles_to_run)
        roles_to_run = roles_to_run[start_i:end_i]

    if args.limit:
        roles_to_run = roles_to_run[:args.limit]
        log.info(f"  Limited to top {len(roles_to_run)} role(s)")

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
    coverage:       list[tuple] = []  # (role, jobs LinkedIn listed, new jobs kept, note)
    hot_remaining:  int        = 0
    prev_was_heavy: bool       = False
    scraped_any:    bool       = False

    # Main loop
    blocked_roles: list[str] = []
    unrun: list[str] = []
    consecutive_blocked = 0
    stop_shard = False
    pass_roles = list(roles_to_run)
    for pass_number in (1, 2):
        if pass_number == 2:
            # Roles LinkedIn blocked: one more try after a rest, if the time budget and the runner allow
            retry = [role for role in blocked_roles if role not in completed]
            if not retry or stop_shard or out_of_time(RETRY_BLOCKED_AFTER + 600):
                break
            log.info(f"\nRetrying {len(retry)} blocked role(s) after {RETRY_BLOCKED_AFTER // 60} minutes' rest")
            time.sleep(RETRY_BLOCKED_AFTER)
            pass_roles, blocked_roles, consecutive_blocked = retry, [], 0
        else:
            blocked_roles = []
        for idx, role in enumerate(pass_roles, start=1):
            if role in completed:
                log.info(f"[{idx:>2}/{len(pass_roles)}] '{role}' — already completed, skipping")
                continue
            if out_of_time(120):
                log.warning("Time budget used up: saving what was collected and stopping the shard.")
                unrun.append(role)
                continue
            if stop_shard:
                unrun.append(role)
                continue

            log.info(f"\n[{idx:>2}/{len(pass_roles)}] -- {role} " + "-" * max(1, 48 - len(role)))

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
                capped_parts = []
                us_wide = args.location.strip().lower() in ("united states", "usa", "us", "united states of america")
                if stats["capped"] and not us_wide:
                    capped_parts.append("not split: the search is not US-wide")
                if stats["capped"] and us_wide:
                    # LinkedIn shows at most ~1,000 jobs per search: search each US state separately (its work-type
                    # and job-type filters are ignored). Jobs already found are skipped through global_seen_ids.
                    log.info(f"  '{role}' reached LinkedIn's limit: searching state by state")
                    for state_name in US_STATES:
                        if out_of_time() or stats.get("blocked"):
                            capped_parts.append(f"stopped before {state_name}")
                            break
                        more, part = scrape_role(
                            role=role, location=f"{state_name}, United States", hours_old=hours_old,
                            global_seen_ids=global_seen_ids, fetch_description=fetch_description,
                            max_results=max_results, base_delay_sec=base_delay, desc_workers=DESC_WORKERS)
                        jobs += more
                        stats["found"] += part["found"]
                        stats["hit_429"] = stats["hit_429"] or part["hit_429"]
                        stats["blocked"] = stats.get("blocked") or part.get("blocked")
                        if part["capped"]:
                            capped_parts.append(state_name)
                coverage.append((role, stats["found"], len(jobs), "still capped: " + ", ".join(capped_parts)
                                 if capped_parts else ("split by state" if stats["capped"] else "complete")))
            except KeyboardInterrupt:
                log.warning("\nInterrupted — saving state...")
                _flush(state, completed, grand_total, global_seen_ids, run_jobs, use_supabase)
                return
            except Exception as exc:
                log.error(f"Unexpected error on '{role}': {exc}", exc_info=True)
                jobs, stats = [], {"batches": 0, "hit_429": False, "found": 0, "capped": False}
                coverage.append((role, 0, 0, f"error: {exc}"[:80]))

            scraped_any    = True
            prev_was_heavy = stats["batches"] > 1
            if stats["hit_429"]:
                hot_remaining = HOT_STREAK_ROLES
                log.warning(f"  429 seen — hot cooldowns for next {HOT_STREAK_ROLES} roles.")

            # Blocked: LinkedIn refused this runner before the role gave anything. Not completed, so it is retried.
            if stats.get("blocked") and not jobs:
                blocked_roles.append(role)
                consecutive_blocked += 1
                if coverage and coverage[-1][0] == role:
                    coverage.pop()
                if consecutive_blocked >= MAX_BLOCKED_ROLES:
                    log.warning(f"  {consecutive_blocked} roles in a row blocked by LinkedIn: this runner is being "
                                "refused. Saving what was collected and stopping the shard.")
                    stop_shard = True
                continue
            consecutive_blocked = 0

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


    for role in blocked_roles:
        coverage.append((role, 0, 0, "blocked by LinkedIn (429), not collected"))
    for role in unrun:
        coverage.append((role, 0, 0, "not run: time budget used up or shard stopped"))
    if blocked_roles or unrun:
        log.warning(f"Not collected this run: {len(blocked_roles)} blocked role(s), {len(unrun)} not run. "
                    "What was collected is saved and goes on to the merge.")
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
    write_coverage(coverage, args.post_time)
    if blocked_roles or unrun:
        # Everything collected is saved. The distinct exit code tells the workflow this shard is partial: its
        # jobs are still merged, but it does not count as a complete scrape of the board (no expiry from it).
        sys.exit(PARTIAL_EXIT_CODE)


def write_coverage(coverage: list[tuple], post_time: str) -> None:
    """Per role: how many jobs LinkedIn listed and whether the search was complete (log + Actions summary)."""
    if not coverage:
        return
    capped = [row for row in coverage if row[3].startswith("still capped")]
    lines = [f"### LinkedIn coverage ({post_time} window)", "",
             f"{len(coverage)} roles, {sum(row[1] for row in coverage)} jobs listed, "
             f"{sum(row[2] for row in coverage)} new kept, {len(capped)} roles still at LinkedIn's limit", "",
             "| Role | Listed | New | Coverage |", "|---|---|---|---|"]
    lines += [f"| {role} | {listed} | {kept} | {note} |" for role, listed, kept, note in coverage]
    for role, listed, kept, note in coverage:
        log.info(f"  coverage: {role}: {listed} listed, {kept} new, {note}")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n\n")


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
