from __future__ import annotations
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import Counter
from itertools import count
from email.utils import parsedate_to_datetime

import asyncio
import html
import contextlib
import json
import logging
import re
import hashlib
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

try:
    from supabase import acreate_client
except ImportError:
    acreate_client = None

# ── Standalone-safe imports (works with or without the internal packages) ──
try:
    from scraper_utils import enrich_raw_job
except ImportError:
    def enrich_raw_job(job: dict) -> dict:
        return job

try:
    from job_extraction import database_rows
except ImportError:
    def database_rows(rows: list[dict]) -> list[dict]:
        return rows

try:
    from scraper_runtime import bounded_map, retry_after_seconds, paced_request
except ImportError:
    def retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None

    async def bounded_map(fn, items, concurrency: int):
        sem = asyncio.Semaphore(max(1, concurrency))
        async def _run(item):
            async with sem:
                return await fn(item)
        return await asyncio.gather(*(_run(i) for i in items), return_exceptions=True)

    _BOARD_GATES: dict[str, dict[str, float]] = {}
    _BOARD_LOCKS: dict[str, asyncio.Lock] = {}

    async def paced_request(client: httpx.AsyncClient, method: str, url: str,
                             board: str = "DEFAULT", rate: float = 50.0, **kwargs) -> httpx.Response:
        lock = _BOARD_LOCKS.setdefault(board, asyncio.Lock())
        state = _BOARD_GATES.setdefault(board, {"next_slot": 0.0})
        async with lock:
            now = asyncio.get_running_loop().time()
            slot = max(now, state["next_slot"])
            state["next_slot"] = slot + (1.0 / rate)
            delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)
        return await client.request(method, url, **kwargs)

class CompanyNotFoundError(Exception):
    pass

class ScraperError(Exception):
    pass

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "jobs"
COMPANIES_TABLE = "companies"
ERROR_LOG_TABLE = "scrape_error_logs"
SKIP_SUPABASE = os.getenv("SKIP_SUPABASE", "true").lower() == "true"
LOCAL_OUTPUT = os.getenv("LOCAL_OUTPUT", "true").lower() == "true"

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "200"))
REQUEST_TIMEOUT = float(os.getenv("ICIMS_TIMEOUT", "45"))
CONNECT_TIMEOUT = float(os.getenv("ICIMS_CONNECT_TIMEOUT", "8"))

# Retries for individual HTTP calls to iCIMS pages.
ICIMS_FETCH_RETRIES = int(os.getenv("RETRY_COUNT", "3"))
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN = int(os.getenv("BREAKER_COOLDOWN", "60"))

# Retries for batched Supabase upserts (separate concern from HTTP fetch retries).
SUPABASE_UPSERT_RETRIES = int(os.getenv("UPSERT_RETRIES", "4"))
UPSERT_BASE_DELAY = float(os.getenv("UPSERT_BASE_DELAY", "1.0"))
SUPABASE_BATCH_SIZE = int(os.getenv("SUPABASE_BATCH_SIZE", "300"))

DETAIL_CONCURRENCY = int(os.getenv("DETAIL_CONCURRENCY", "300"))
MAX_PAGES = int(os.getenv("ICIMS_MAX_PAGES", "0"))  # 0 = follow pages to the end
PAGE_BATCH_SIZE = int(os.getenv("ICIMS_PAGE_BATCH_SIZE", "10"))

SEARCH_ATTRIBUTES = {
    "location": "",
    "skills": "",
    "experience": "",
    "job_type": "",
    "work_mode": "",
    "excluded_words": "",
    "post_time": "day",
}

KEYWORDS = [""]
COUNTRIES = [""]
US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
    "dc", "puerto rico", "pr"
}
STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky",
    "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd",
    "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy"
}

COMMON_SKILLS = [
    "Python", "Java", "C++", "Go", "Rust", "JavaScript", "TypeScript", "React", "Angular", "Vue",
    "Node", "SQL", "NoSQL", "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Machine Learning", "Golang",
]

_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]+?)</script>',
    re.IGNORECASE,
)

_JOB_CARD_RE = re.compile(
    r'<li[^>]+class="[^"]*iCIMS_JobCardItem[^"]*"[^>]*>(?P<body>.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(?P<href>https?://[^"]*?/jobs/(?P<id>\d+)/[^"]*?/job[^"]*)"[^>]*'
    r'class="[^"]*iCIMS_Anchor[^"]*"[^>]*>'
    r'(?P<inner>.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(r'<h3[^>]*>(?P<title>.*?)</h3>', re.DOTALL | re.IGNORECASE)
_LOCATION_RE = re.compile(
    r'<span[^>]+class="[^"]*sr-only[^"]*field-label[^"]*"[^>]*>\s*Job Locations\s*</span>'
    r'\s*<span[^>]*>\s*(?P<loc>[^<]*?)\s*</span>',
    re.DOTALL | re.IGNORECASE,
)
_DATE_TITLE_RE = re.compile(
    r'<span[^>]+title="(?P<date>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)"',
    re.IGNORECASE,
)
_HEADER_TAG_RE = re.compile(
    r'<dt[^>]*>(?P<label_html>.*?)</dt>'
    r'\s*<dd[^>]*>\s*<span[^>]*>(?P<value>.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)
_DESC_RE = re.compile(
    r'<div[^>]+class="[^"]*col-xs-12[^"]*description[^"]*"[^>]*>(?P<desc>.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_DASH_LOC_RE = re.compile(
    r'^(?P<country>[A-Z]{2,3})-(?P<state>[A-Z0-9 ]{1,40})(?:-(?P<city>[^-].*))?$'
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("icims_scraper")


def clean_html_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()] if value else []


def post_time_to_delta(value: str) -> timedelta | None:
    value = (value or "any").lower().strip()
    mapping = {
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
        "week": timedelta(weeks=1),
        "month": timedelta(days=30),
        "year": timedelta(days=365),
    }
    return mapping.get(value)


def normalize_company_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Field extraction. These were placeholder stubs (always returning "") — now
# implemented the same way as the other scrapers in this family.
# ---------------------------------------------------------------------------

def extract_work_mode(title: str, location: str, description: str) -> str:
    content = f"{title} {location} {description}".lower()
    if re.search(r"\b(remote|wfh|work from home)\b", content):
        return "Remote"
    if re.search(r"\bhybrid\b", content):
        return "Hybrid"
    if re.search(r"\b(onsite|in-office|in office|in-person)\b", content):
        return "Onsite"
    return ""


def extract_salary(text: str, compensation: dict[str, Any] | None = None) -> str:
    compensation = compensation or {}
    salary = compensation.get("compensationTierSummary") or compensation.get("scrapeableCompensationSalarySummary") or ""
    if salary:
        return salary
    m = re.search(
        r"(?:[\$£€])[\d,]+[kK]?\s*(?:-|to|—|–|&mdash;|&ndash;)\s*(?:[\$£€])?[\d,]+[kK]?|(?:[\$£€])[\d,]+[kK]?",
        text,
    )
    return m.group(0) if m else ""


def extract_experience(text: str) -> str:
    m = re.search(
        r"(\d+)\s*(?:-|to|—|–|&mdash;|&ndash;)?\s*(\d+)?\s*(?:\+)?\s*(?:years?|yrs?)(?:\s*of)?\s*(?:experience)",
        text,
        re.I,
    )
    return m.group(0) if m else ""


def extract_skills(text: str) -> str:
    found = []
    for skill in COMMON_SKILLS:
        if re.search(rf"\b{re.escape(skill)}\b", text, re.I):
            found.append(skill)
    return ", ".join(dict.fromkeys(found))


def build_filters(job_profile: str, location: str) -> dict[str, Any]:
    return {
        "profiles": split_csv(job_profile),
        "location": (SEARCH_ATTRIBUTES.get("location", "") or location or "").lower().strip(),
        "work_mode": SEARCH_ATTRIBUTES.get("work_mode", "").lower().strip(),
        "job_type": SEARCH_ATTRIBUTES.get("job_type", "").lower().strip(),
        "experience": SEARCH_ATTRIBUTES.get("experience", "").lower().strip(),
        "skills": split_csv(SEARCH_ATTRIBUTES.get("skills", "")),
        "excluded": split_csv(SEARCH_ATTRIBUTES.get("excluded_words", "")),
        "max_age": post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "day")),
    }


def pre_matches_filters(job: dict[str, Any], filters: dict[str, Any]) -> bool:
    title = (job.get("name") or job.get("title") or "").strip()
    if not title:
        return False
        
    job_location = str(job.get("location") or "").strip()
    text = f"{title} {job_location}".lower()
    
    profiles = filters.get("profiles")
    if profiles and not any(p in title.lower() for p in profiles):
        return False

    loc_filter = filters.get("location")
    if loc_filter:
        if loc_filter in {"usa", "united states", "united states of america", "us"}:
            loc_lower = job_location.lower()
            if not (
                any(st in loc_lower for st in US_STATES)
                or any(re.search(rf"\b{code}\b", loc_lower) for code in STATE_CODES)
            ):
                return False
        elif loc_filter not in text:
            return False
            
    return True

def matches_filters(job: dict[str, Any], filters: dict[str, Any], now_utc: datetime) -> bool:
    title = (job.get("name") or job.get("title") or "").strip()
    if not title:
        return False

    loc_obj = job.get("location") or job.get("workLocations") or job.get("workLocation")
    job_location = ""
    if isinstance(loc_obj, list) and loc_obj:
        first = loc_obj[0]
        if isinstance(first, str):
            job_location = first.strip()
        elif isinstance(first, dict):
            job_location = str(first.get("label") or first.get("displayName") or first.get("name") or "").strip()
    elif isinstance(loc_obj, dict):
        job_location = str(loc_obj.get("name") or loc_obj.get("label") or loc_obj.get("city") or "").strip()
    else:
        job_location = str(loc_obj or "").strip()

    desc_text = clean_html_text(job.get("description") or job.get("content") or "")
    text = f"{title} {job_location} {desc_text}".lower()

    profiles = filters.get("profiles")
    if profiles and not any(p in title.lower() for p in profiles):
        return False

    loc_filter = filters.get("location")
    if loc_filter:
        if loc_filter in {"usa", "united states", "united states of america", "us"}:
            loc_lower = job_location.lower()
            if not (
                any(st in loc_lower for st in US_STATES)
                or any(re.search(rf"\b{code}\b", loc_lower) for code in STATE_CODES)
            ):
                return False
        elif loc_filter not in text:
            return False

    if filters.get("work_mode") and filters["work_mode"] not in text:
        return False
    if filters.get("job_type") and filters["job_type"] not in text:
        return False
    if filters.get("experience") and filters["experience"] not in text:
        return False
    if filters.get("skills") and not all(skill in text for skill in filters["skills"]):
        return False
    if filters.get("excluded") and any(word in text for word in filters["excluded"]):
        return False

    max_age = filters.get("max_age")
    if max_age:
        published_at = job.get("updated_at") or job.get("createdOn") or job.get("created_at") or job.get("published_date") or job.get("posted_at") or job.get("postedDate")
        if published_at:
            try:
                pub_str = str(published_at).strip().replace("Z", "+00:00")
                published = datetime.fromisoformat(pub_str)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                if len(pub_str) == 10:
                    published = published.replace(hour=23, minute=59, second=59)
                if now_utc - published > max_age:
                    return False
            except ValueError:
                pass  # If date can't be parsed, let the job through

    return True


def format_iso_time(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def normalize_company_row(company: dict[str, Any]) -> dict[str, Any]:
    """Normalize a company row before upsert.

    NOTE: only add keys here that actually exist as columns on the `companies`
    table — extra unknown top-level keys will make the whole upsert batch fail.
    Verify against your schema before adding fields beyond what's listed below.
    """
    slug = (company.get("slug") or "").strip().lower()
    return {
        "slug": slug,
        "name": company.get("name") or company.get("company_name") or normalize_company_name(slug),
        "source_board": company.get("source_board", "iCIMS"),
        "status": company.get("status", "active"),
        "discovered_at": company.get("discovered_at") or datetime.now(timezone.utc).isoformat(),
        "last_scraped_at": company.get("last_scraped_at") or datetime.now(timezone.utc).isoformat(),
    }


def chunked(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield i // size, items[i:i + size]


@dataclass
class FetchResult:
    company: str
    jobs: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True  # False only on a genuine fetch/parse failure, not "site not found"/"0 matches"
    is_404: bool = False


# ---------------------------------------------------------------------------
# Circuit breaker (ported from the sibling scrapers). A 404 ("this iCIMS site
# doesn't exist") is an expected per-company outcome and counts as success()
# — only rate limiting, server errors, and transport errors count as failures.
# ---------------------------------------------------------------------------

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = None

    def allow_request(self) -> bool:
        if self.state == "open":
            if time.time() - self.opened_at >= self.recovery_timeout:
                self.state = "half_open"
                return True
            return False
        return True

    def success(self):
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = None

    def failure(self):
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.time()




class AsyncSupabaseWriter:
    def __init__(self, table_name: str = SUPABASE_TABLE):
        self.table_name = table_name
        self.client = None

    async def init(self):
        if SKIP_SUPABASE or not SUPABASE_URL or not SUPABASE_KEY:
            return
        self.client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

    async def upsert_batch(self, rows: list[dict[str, Any]], on_conflict: str = "id"):
        if SKIP_SUPABASE or not self.client or not rows:
            return
        await self.client.table(self.table_name).upsert(database_rows(rows) if on_conflict == 'id' else rows, on_conflict=on_conflict).execute()


class AsyncErrorLogger:
    """Logs failed batches to Supabase, with a local-file fallback if even
    the error-log write fails."""

    def __init__(self, table_name: str = ERROR_LOG_TABLE):
        self.table_name = table_name
        self.client = None

    async def init(self):
        if SKIP_SUPABASE or not SUPABASE_URL or not SUPABASE_KEY:
            return
        self.client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

    async def log_failure(
        self,
        run_id: str,
        batch_index: int,
        rows: list[dict],
        exc: Exception,
        attempt_count: int,
        table_name: str,
    ):
        if not self.client:
            return

        log_row = {
            "run_id": run_id,
            "batch_index": batch_index,
            "table_name": table_name,
            "row_count": len(rows),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "attempt_count": attempt_count,
            "payload_sample": rows[:3],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await self.client.table(self.table_name).insert(log_row).execute()
        except Exception as log_exc:
            log.error("Failed to write error log to Supabase: %s", log_exc)
            with open("failed_upsert_error_logs.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(log_row, ensure_ascii=False) + "\n")


async def upsert_with_retry(
    writer: AsyncSupabaseWriter,
    error_logger: AsyncErrorLogger,
    rows: list[dict],
    run_id: str,
    batch_index: int,
    on_conflict: str,
) -> bool:
    """Retry with exponential backoff; on final failure, log the error to
    Supabase (with a local-file fallback) and write a local payload backup so
    no data is silently lost."""
    last_exc = None

    for attempt in range(1, SUPABASE_UPSERT_RETRIES + 1):
        try:
            await writer.upsert_batch(rows, on_conflict=on_conflict)
            log.info("[%s] Batch %s upserted successfully (%s rows)", writer.table_name, batch_index, len(rows))
            return True
        except Exception as exc:
            last_exc = exc
            delay = UPSERT_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "[%s] Batch %s failed on attempt %s/%s: %s. Retrying in %.2fs",
                writer.table_name, batch_index, attempt, SUPABASE_UPSERT_RETRIES, exc, delay,
            )
            if attempt < SUPABASE_UPSERT_RETRIES:
                await asyncio.sleep(delay)

    log.error("[%s] Batch %s failed after %s attempts", writer.table_name, batch_index, SUPABASE_UPSERT_RETRIES)

    try:
        await error_logger.log_failure(
            run_id=run_id,
            batch_index=batch_index,
            rows=rows,
            exc=last_exc or Exception("Unknown batch upsert failure"),
            attempt_count=SUPABASE_UPSERT_RETRIES,
            table_name=writer.table_name,
        )
    except Exception as log_exc:
        log.error("Failed to log batch failure: %s", log_exc)
        fallback = {
            "run_id": run_id,
            "batch_index": batch_index,
            "table_name": writer.table_name,
            "row_count": len(rows),
            "error_type": type(last_exc).__name__ if last_exc else "Exception",
            "error_message": str(last_exc) if last_exc else "Unknown batch upsert failure",
            "attempt_count": SUPABASE_UPSERT_RETRIES,
            "payload_sample": rows[:3],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with open("failed_upserts_fallback.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(fallback, ensure_ascii=False) + "\n")

    # Payload backup: dump the whole failed batch to disk so it can be replayed later.
    with open(f"failed_batch_{writer.table_name}_{batch_index}.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    return False


async def upsert_all_jobs(all_jobs: list[dict], run_id: str) -> tuple[int, int]:
    writer = AsyncSupabaseWriter(table_name=SUPABASE_TABLE)
    error_logger = AsyncErrorLogger()
    await writer.init()
    await error_logger.init()

    success_count = failure_count = 0
    for batch_index, batch in chunked(all_jobs, SUPABASE_BATCH_SIZE):
        ok = await upsert_with_retry(writer, error_logger, batch, run_id, batch_index, on_conflict="id")
        if ok:
            success_count += 1
        else:
            failure_count += 1
    return success_count, failure_count


async def upsert_all_companies(all_companies: list[dict], run_id: str) -> tuple[int, int]:
    writer = AsyncSupabaseWriter(table_name=COMPANIES_TABLE)
    error_logger = AsyncErrorLogger()
    await writer.init()
    await error_logger.init()

    success_count = failure_count = 0
    for batch_index, batch in chunked(all_companies, SUPABASE_BATCH_SIZE):
        ok = await upsert_with_retry(writer, error_logger, batch, run_id, batch_index, on_conflict="slug")
        if ok:
            success_count += 1
        else:
            failure_count += 1
    return success_count, failure_count


def _iter_ld_dicts(node: object):
    if isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from (g for g in graph if isinstance(g, dict))
    elif isinstance(node, list):
        for item in node:
            yield from _iter_ld_dicts(item)


def _find_job_posting(html_text: str) -> dict | None:
    for match in _JSON_LD_RE.finditer(html_text):
        body = match.group(1).strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        for candidate in _iter_ld_dicts(data):
            if candidate.get("@type") == "JobPosting" or "JobPosting" in (candidate.get("@type") or []):
                return candidate
    return None


# Placeholder / template tokens iCIMS embeds in JSON-LD when the real
# value isn't available.  Anything matching is treated as missing.
_JSONLD_JUNK_RE = re.compile(
    r"\$\{|UNAVAILABLE|Hidden\s*\(|^\s*$",
    re.IGNORECASE,
)


def _location_from_jsonld(value: object) -> str | None:
    candidates = value if isinstance(value, list) else [value]
    for c in candidates:
        if not isinstance(c, dict):
            continue
        addr = c.get("address")
        if not isinstance(addr, dict):
            continue
        parts = [
            str(addr.get(k) or "").strip()
            for k in ("addressLocality", "addressRegion", "addressCountry")
            if addr.get(k)
        ]
        # Skip entries that contain template placeholders or junk
        if any(_JSONLD_JUNK_RE.search(p) for p in parts):
            continue
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined
    return None


def _strip(text: str) -> str:
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_location(card_body: str) -> str | None:
    match = _LOCATION_RE.search(card_body)
    if match:
        raw = _strip(match.group("loc"))
        if raw:
            return _normalize_location(raw)
    parts: dict[str, str] = {}
    for tag in _HEADER_TAG_RE.finditer(card_body):
        label = _strip(tag.group("label_html")).lower()
        value = _strip(tag.group("value"))
        if not value:
            continue
        if "city" in label:
            parts["city"] = value
        elif "state" in label or "province" in label:
            parts["state"] = value
        elif "country" in label:
            parts["country"] = value
    if parts:
        ordered = [parts.get(k) for k in ("city", "state", "country")]
        return ", ".join(p for p in ordered if p)
    return None


def _normalize_location(raw: str) -> str:
    match = _DASH_LOC_RE.match(raw)
    if not match:
        return raw
    parts = [match.group("city"), match.group("state"), match.group("country")]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _extract_posted_at(card_body: str) -> datetime | None:
    match = _DATE_TITLE_RE.search(card_body)
    if not match:
        return None
    raw = match.group("date").strip()
    # Try both US (MM/DD/YYYY) and European (DD/MM/YYYY) formats.
    # When both parse successfully, prefer the one that yields a past date
    # (European iCIMS sites serve DD/MM/YYYY which US-only parsing misreads
    # as future dates).
    candidates: list[datetime] = []
    for fmt in (
        "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
        "%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M",
    ):
        try:
            candidates.append(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    if not candidates:
        return None
    now = datetime.now()
    past = [c for c in candidates if c <= now]
    return past[0] if past else candidates[0]


def _extract_description(card_body: str) -> str | None:
    match = _DESC_RE.search(card_body)
    if not match:
        return None
    text = _strip(match.group("desc"))
    return text or None


def _extract_header_value(card_body: str, label_match: str) -> str | None:
    needle = label_match.lower()
    for tag in _HEADER_TAG_RE.finditer(card_body):
        if _strip(tag.group("label_html")).lower() == needle:
            value = _strip(tag.group("value"))
            return value or None
    return None


def _extract_requisition_id(card_body: str) -> str | None:
    return _extract_header_value(card_body, "Requisition ID") or _extract_header_value(card_body, "ID")


def _apply_jsonld_to_job(job: dict[str, Any], html_text: str) -> None:
    posting = _find_job_posting(html_text)
    if posting is None:
        return

    desc_html = posting.get("description")
    if isinstance(desc_html, str) and desc_html.strip():
        # Search cards contain a summary; the detail page has the full posting.
        job["description"] = _strip(desc_html) or job.get("description")
        job["description_raw"] = desc_html

    job["baseSalary"] = posting.get("baseSalary")
    emp_raw = posting.get("employmentType")
    if isinstance(emp_raw, str):
        norm = emp_raw.strip().upper().replace("-", "_").replace(" ", "_")
        mapped = _EMPLOYMENT_TYPE_MAP.get(norm)
        if mapped and not job.get("employment_type"):
            job["employment_type"] = mapped

    if not job.get("posted_at"):
        date_raw = posting.get("datePosted")
        if isinstance(date_raw, str) and date_raw:
            with contextlib.suppress(ValueError):
                job["posted_at"] = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))

    if not job.get("location"):
        loc_str = _location_from_jsonld(posting.get("jobLocation"))
        if loc_str:
            job["location"] = loc_str


def load_companies() -> list[str]:
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icims_companies.txt")
    if not os.path.exists(filepath):
        log.warning("%s not found. Please create it with one company name per line.", filepath)
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def normalize_job_dict(
    parsed_job: dict[str, Any],
    company_slug: str,
) -> dict[str, Any]:
    """Normalize a parsed iCIMS job into a row matching the `jobs` table schema.

    Search metadata (keyword/country) is intentionally NOT injected as a
    top-level key here — a prior version of this scraper added
    `job["role_category"]`/`job["country"]` onto the row after building it,
    which would make Supabase reject the whole batch since those aren't real
    table columns. Callers should track that separately (see
    DirectiCIMSScraper.search_metadata).
    """
    ats_id = str(parsed_job.get("ats_id") or "")
    title = (parsed_job.get("title") or "Untitled").strip()
    url = parsed_job.get("url") or ""

    job_location = parsed_job.get("location") or ""
    desc_text = clean_html_text(parsed_job.get("description", ""))

    job_id = ats_id or title
    raw_key = f"icims:{company_slug}:{job_id}"
    dedupe_key = hashlib.md5(raw_key.encode()).hexdigest()

    comp: dict[str, Any] = {}
    sal_str = extract_salary(desc_text, comp)
    salary_min = None
    salary_max = None
    if sal_str:
        sal_norm = sal_str.lower().replace("l", "00000").replace("k", "000")
        nums = []
        for n in re.findall(r"[\d,]+\.?\d*", sal_norm):
            try:
                nums.append(int(float(n.replace(",", ""))))
            except ValueError:
                pass
        if len(nums) >= 2:
            salary_min, salary_max = nums[0], nums[1]
        elif len(nums) == 1:
            salary_min = salary_max = nums[0]

    wm = extract_work_mode(title, job_location, desc_text)

    published_at = parsed_job.get("posted_at")
    if isinstance(published_at, datetime):
        published_at = published_at.isoformat()

    enriched = enrich_raw_job({
        "id": dedupe_key,
        "job_title": title,
        "company": normalize_company_name(company_slug),
        "location": job_location,
        "job_url": url,
        "apply_url": url,
        "description": desc_text,
        "description_raw": parsed_job.get("description_raw") or desc_text,
        "baseSalary": parsed_job.get("baseSalary"),
        "structured_fields": {"job_type": parsed_job.get("employment_type")},
        "salary": sal_str,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "experience": extract_experience(desc_text),
        "skills": extract_skills(desc_text),
        "work_mode": wm,
        "source_board": "iCIMS",
        "scraper_type": "api",
        "is_remote": (wm == "Remote"),
        "job_type": parsed_job.get("employment_type") or "full_time",
        "scraped_at": format_iso_time(datetime.now(timezone.utc).isoformat()),
        # Only set created_at when we have a real datePosted from JSON-LD.
        # Leave it None otherwise — better an honest null than a fake scraped_at.
        "created_at": format_iso_time(published_at) if published_at else None,
        "also_on": [],
    })


    return enriched


class DirectiCIMSScraper:
    def __init__(self, companies: list[str], company_sem: asyncio.Semaphore | None = None):
        self.companies = list(dict.fromkeys(c.strip() for c in companies if c.strip() and not c.lstrip().startswith('#')))
        self._external_list_sem = company_sem
        self._detail_sem = asyncio.Semaphore(max(1, DETAIL_CONCURRENCY))
        self._page_sem = asyncio.Semaphore(max(1, int(os.getenv("ICIMS_PAGE_CONCURRENCY", "300"))))
        self.stats = Counter()
        # Populated after each scrape() call: company_slug -> whether the check
        # succeeded (jobs found, 0 matches, or confirmed site-not-found — all
        # "ok"; only a genuine fetch/parse error counts as failed).
        self.company_status: dict[str, bool] = {}
        # Populated after each scrape() call: dedupe_key -> {keyword, country}
        # search metadata. Kept separate from the upserted rows (see normalize_job_dict).
        self.search_metadata: dict[str, dict[str, str]] = {}

    # Regex that matches slugs which already carry a subdomain prefix
    # (e.g. "uscareers-acme", "ukcareers-bae", "globalcareers-cbre").
    _PREFIXED_SLUG_RE = re.compile(r"^[a-z]+careers-", re.IGNORECASE)

    def _resolve_base_url(self, slug: str) -> str:
        if slug.startswith(("http://", "https://")):
            return slug.rstrip("/")
        # Slugs that already include a *careers- prefix should NOT get
        # another "careers-" prepended — they map directly to
        # https://{slug}.icims.com.
        if self._PREFIXED_SLUG_RE.match(slug):
            return f"https://{slug}.icims.com"
        return f"https://careers-{slug}.icims.com"

    async def _fetch_page(self, client: httpx.AsyncClient, base_url: str, page: int) -> str:
        url = f"{base_url}/jobs/search"
        params = {"ss": "1", "pr": page, "in_iframe": "1"}
        for attempt in range(1, ICIMS_FETCH_RETRIES + 1):
            try:
                async with self._page_sem:
                    response = await paced_request(client, 'get', url, board='ICIMS', rate=100, params=params, headers={"User-Agent": "Mozilla/5.0"})
            except httpx.HTTPError as exc:
                if attempt == ICIMS_FETCH_RETRIES:
                    raise ScraperError(f"iCIMS fetch failed for {base_url} at page={page}: {exc}") from exc
                await asyncio.sleep(1.5 * attempt)
                continue
            if response.status_code == 404:
                raise CompanyNotFoundError(f"iCIMS site not found: {base_url}")
            if response.status_code == 200:
                return response.text
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == ICIMS_FETCH_RETRIES:
                    raise ScraperError(f"iCIMS returned {response.status_code} for {base_url} at page={page}")
                retry_after = response.headers.get("Retry-After")
                delay = retry_after_seconds(retry_after)
                if delay is None:
                    delay = 1.5 * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            raise ScraperError(f"iCIMS returned {response.status_code} for {base_url} at page={page}")
        raise ScraperError(f"iCIMS exhausted retries for {base_url} at page={page}")

    def _parse_page_to_dicts(self, html_text: str, base_url: str, company_slug: str = "") -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        seen_in_page: set[str] = set()

        # Use the canonical slug passed by the caller instead of
        # re-deriving it from the URL (which broke for prefixed slugs).
        if not company_slug:
            host = base_url.replace("https://", "").replace("http://", "")
            host = host.split("/", 1)[0]
            if host.startswith("careers-"):
                company_slug = host.removeprefix("careers-").split(".", 1)[0]
            elif host.startswith("uscareers-"):
                company_slug = host.removeprefix("uscareers-").split(".", 1)[0]
            else:
                company_slug = host.split(".", 1)[0]

        for card in _JOB_CARD_RE.finditer(html_text):
            body = card.group("body")
            anchor = _JOB_ANCHOR_RE.search(body)
            if anchor is None:
                continue
            ats_id = anchor.group("id")
            if ats_id in seen_in_page:
                continue
            seen_in_page.add(ats_id)
            title_match = _TITLE_RE.search(anchor.group("inner"))
            if not title_match:
                continue
            title = _strip(title_match.group("title"))
            if not title:
                continue

            job_dict = {
                "url": html.unescape(anchor.group("href")),
                "title": title,
                "company_slug": company_slug,
                "ats_id": ats_id,
                "location": _extract_location(body),
                # NOTE: _extract_posted_at() captures the iCIMS page-render
                # timestamp, not the real job posting date. The genuine
                # datePosted is only available via JSON-LD on the detail page
                # (fetched by _apply_jsonld_to_job). We intentionally do NOT
                # set posted_at here so the detail fetch remains the sole source.
                "description": _extract_description(body),
            }
            jobs.append(job_dict)
        return jobs

    async def _enrich_detail(self, client: httpx.AsyncClient, sem: asyncio.Semaphore, job: dict[str, Any]) -> None:
        url = job.get("url")
        if not url:
            return
        for attempt in range(max(1, ICIMS_FETCH_RETRIES)):
            try:
                async with sem:
                    response = await client.get(str(url), headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
                self.stats[f'detail_status_{response.status_code}'] += 1
                if response.status_code == 200:
                    _apply_jsonld_to_job(job, response.text)
                    self.stats['details_ok'] += 1
                    if not job.get('posted_at'):
                        self.stats['details_missing_date'] += 1
                    return
                if response.status_code in (404, 410):
                    job['_expired'] = True
                    return
                if response.status_code == 429:
                    delay = retry_after_seconds(response.headers.get('Retry-After'))
                    delay = max(delay if delay is not None else 2.0, 2.0)
                    if attempt + 1 < ICIMS_FETCH_RETRIES:
                        await asyncio.sleep(delay)
                    continue
                if response.status_code < 500:
                    break
                delay = retry_after_seconds(response.headers.get('Retry-After'))
            except httpx.HTTPError as exc:
                self.stats[type(exc).__name__] += 1
                delay = None
            if attempt + 1 < ICIMS_FETCH_RETRIES:
                await asyncio.sleep(delay if delay is not None else 0.5 * 2 ** attempt)
        self.stats['details_failed'] += 1

    async def _fetch_company_jobs(
        self,
        client: httpx.AsyncClient,
        company: str,
        job_profile: str,
        location: str,
    ) -> FetchResult:
        slug = company.strip()
        base_url = self._resolve_base_url(slug)
        filters = build_filters(job_profile, location)
        now_utc = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []

        seen_ats_ids: set[str] = set()
        parsed_jobs: list[dict[str, Any]] = []
        listing_complete = True

        try:
            # ── Phase 1: fetch page 0 alone to confirm the site exists ──
            try:
                first_html = await self._fetch_page(client, base_url, page=0)
            except CompanyNotFoundError:
                raise  # propagated to the outer handler

            first_page_jobs = self._parse_page_to_dicts(first_html, base_url, company_slug=slug)
            new_first = [j for j in first_page_jobs if j["ats_id"] not in seen_ats_ids]
            for j in new_first:
                seen_ats_ids.add(j["ats_id"])
            parsed_jobs.extend(new_first)

            # ── Phase 2: fetch remaining pages in batches ──
            if new_first:  # only continue if page 0 had results
                for batch_start in count(1, max(1, PAGE_BATCH_SIZE)):
                    if MAX_PAGES > 0 and batch_start >= MAX_PAGES:
                        self.stats['companies_at_page_cap'] += 1
                        listing_complete = False
                        break
                    batch_end = batch_start + max(1, PAGE_BATCH_SIZE)
                    if MAX_PAGES > 0:
                        batch_end = min(batch_end, MAX_PAGES)
                    tasks = [self._fetch_page(client, base_url, page=p) for p in range(batch_start, batch_end)]

                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    stop_fetching = False
                    for p, res in zip(range(batch_start, batch_end), results):
                        if isinstance(res, CompanyNotFoundError):
                            # Later pages returning 404 just means we've gone past the end.
                            stop_fetching = True
                            break
                        elif isinstance(res, Exception):
                            self.stats['list_pages_failed'] += 1
                            listing_complete = False
                            log.warning('Failed page %s for %s: %s', p, company, res)
                            continue

                        html_text = res
                        page_jobs = self._parse_page_to_dicts(html_text, base_url, company_slug=slug)

                        new_jobs = [j for j in page_jobs if j["ats_id"] not in seen_ats_ids]
                        if not new_jobs:
                            stop_fetching = True
                            break

                        for j in new_jobs:
                            seen_ats_ids.add(j["ats_id"])

                        parsed_jobs.extend(new_jobs)

                    if stop_fetching or all(isinstance(result, Exception) for result in results):
                        break

            valid_jobs = []
            if parsed_jobs:
                valid_jobs = [j for j in parsed_jobs if pre_matches_filters(j, filters)]
                
                if valid_jobs:
                    await bounded_map(lambda j: self._enrich_detail(client, self._detail_sem, j), valid_jobs, DETAIL_CONCURRENCY)

            for item in valid_jobs:
                if item.get('_expired'):
                    continue
                if not matches_filters(item, filters, now_utc):
                    continue
                enriched = normalize_job_dict(item, slug)
                self.search_metadata[enriched["id"]] = {"keyword": job_profile, "country": location}
                out.append(enriched)

            log.info("Finished %s: found %s jobs (parsed %s, pre-filtered %s)",
                     company, len(out), len(parsed_jobs), len(valid_jobs))
            return FetchResult(company, out, ok=listing_complete)

        except CompanyNotFoundError as exc:
            # Expected outcome for slugs with no iCIMS site — not an error.
            log.info("No iCIMS site for %s: %s", company, exc)
            return FetchResult(company, [], ok=True, is_404=True)
        except ScraperError as exc:
            log.warning("Scraper error for %s: %s", company, exc)
            return FetchResult(company, [], ok=False)
        except Exception as exc:
            log.exception("Failed to fetch or parse jobs for %s: %s", company, exc)
            return FetchResult(company, [], ok=False)

    async def scrape(self, job_profile: str, location: str, max_jobs: int = 99999, shared_client: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }

        semaphore = self._external_list_sem or asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        total_companies = len(self.companies)
        completed = 0
        progress_lock = asyncio.Lock()

        async def _run(client: httpx.AsyncClient):
            nonlocal completed
            self._progress_start = time.monotonic()
            self._last_progress_log = self._progress_start

            async def bounded_fetch(client: httpx.AsyncClient, company: str):
                nonlocal completed
                async with semaphore:
                    result = await self._fetch_company_jobs(client, company, job_profile, location)
                async with progress_lock:
                    completed += 1
                    now = time.monotonic()
                    if completed % 50 == 0 or completed == total_companies or now - self._last_progress_log >= 15:
                        elapsed = max(now - self._progress_start, 0.001)
                        rate = completed / elapsed
                        eta = (total_companies - completed) / rate if rate else 0
                        log.info("Progress: %d/%d companies (%.1f%%) | %.1f/s | ETA ~%.1fm",
                                 completed, total_companies, completed * 100 / total_companies, rate, eta / 60)
                        self._last_progress_log = now
                return result

            tasks = [bounded_fetch(client, comp) for comp in self.companies]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_jobs: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            seen_ids: set[str] = set()
            missing_companies: list[str] = []

            for company, result in zip(self.companies, results):
                if isinstance(result, Exception):
                    log.warning("Company scrape task failed for %s: %s", company, result)
                    self.company_status[company] = self.company_status.get(company, False) or False
                    continue
                    
                if getattr(result, "is_404", False):
                    missing_companies.append(company)

                self.company_status[company] = self.company_status.get(company, False) or result.ok

                for job in result.jobs:
                    url = job.get("job_url") or ""
                    job_id = job.get("id") or ""
                    if (url and url in seen_urls) or (job_id and job_id in seen_ids):
                        continue
                    if url:
                        seen_urls.add(url)
                    if job_id:
                        seen_ids.add(job_id)
                    all_jobs.append(job)

                    if 0 < max_jobs <= len(all_jobs):
                        break

            if missing_companies:
                out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icims_404_companies.txt")
                counts = {}
                if os.path.exists(out_file):
                    with open(out_file, "r") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            parts = line.rsplit(",", 1)
                            if len(parts) == 2 and parts[1].isdigit():
                                counts[parts[0]] = int(parts[1])
                            else:
                                counts[line] = 1

                for comp in missing_companies:
                    counts[comp] = counts.get(comp, 0) + 1
                
                with open(out_file, "w") as f:
                    for comp in sorted(counts.keys()):
                        f.write(f"{comp},{counts[comp]}\n")
                
                log.info("Updated 404 counts for %d companies in %s", len(counts), out_file)

            if all_jobs and LOCAL_OUTPUT:
                out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_icims_jobs.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(all_jobs, f, indent=2, ensure_ascii=False)
                log.info("Saved %d jobs to %s", len(all_jobs), out_path)

            return all_jobs[:max_jobs] if max_jobs > 0 else all_jobs

        if shared_client:
            return await _run(shared_client)

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200),
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
        ) as client:
            return await _run(client)


async def main():
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    scrape_start_time = datetime.now(timezone.utc).isoformat()
    run_started = time.monotonic()

    print("=" * 60)
    print("iCIMS Scraper - Full Combined Version")
    print(f"Keywords : {KEYWORDS}")
    print(f"Countries: {COUNTRIES}")
    print(f"Skip Supa: {SKIP_SUPABASE}")
    print("=" * 60)

    companies = load_companies()
    print(f"Loaded {len(companies)} companies from icims_companies.txt")
    if not companies:
        print("No companies to scrape. Exiting.")
        return

    scraper = DirectiCIMSScraper(companies)

    total_collected_jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()

    for country in COUNTRIES:
        for keyword in KEYWORDS:
            print(f"\n--> '{keyword}' | {country}")
            jobs = await scraper.scrape(keyword, country, 0)
            new_jobs_count = 0

            for job in jobs:
                url = job.get("job_url", "")
                job_id = job.get("id", "")
                if (url and url in seen_urls) or (job_id and job_id in seen_ids):
                    continue
                if url:
                    seen_urls.add(url)
                if job_id:
                    seen_ids.add(job_id)
                total_collected_jobs.append(job)
                new_jobs_count += 1

            print(f"   [OK] {new_jobs_count} jobs collected")

    print(f"\n{'=' * 60}")
    print("Scrape complete")
    print(f"{'=' * 60}")

    if total_collected_jobs and LOCAL_OUTPUT:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_icims_jobs.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(total_collected_jobs, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(total_collected_jobs)} jobs to all_icims_jobs.json")

    # Search metadata written alongside the job dump instead of injected into the
    # upserted rows (the `jobs` table schema has no columns for it).
    if scraper.search_metadata and LOCAL_OUTPUT:
        meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icims_jobs_search_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(scraper.search_metadata, f, indent=2, ensure_ascii=False)
        print(f"Saved search metadata for {len(scraper.search_metadata)} jobs to icims_jobs_search_meta.json")

    # Company rows built once, from the real companies list, using the success/failure
    # status recorded during scrape() — not re-inserted once per keyword/country loop.
    all_company_rows = [
        normalize_company_row({
            "slug": company,
            "source_board": "iCIMS",
            "status": "active" if scraper.company_status.get(company, False) else "fetch_failed",
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "last_scraped_at": datetime.now(timezone.utc).isoformat(),
        })
        for company in companies
    ]

    if not SKIP_SUPABASE:
        print("\nStarting batched database upserts...")
        job_ok, job_fail = await upsert_all_jobs(total_collected_jobs, run_id)
        company_ok, company_fail = await upsert_all_companies(all_company_rows, run_id)

        print("\nRunning expiration cleanup RPC...")
        try:
            cleanup_writer = AsyncSupabaseWriter(table_name=SUPABASE_TABLE)
            await cleanup_writer.init()
            if cleanup_writer.client:
                await cleanup_writer.client.rpc(
                    "mark_expired_jobs",
                    {"p_scrape_start_time": scrape_start_time, "p_source_board": "iCIMS"},
                ).execute()
                print("Successfully expired stale jobs.")
        except Exception as e:
            print(f"Failed to expire stale jobs: {e}")
    else:
        print("\nSkipping database upserts (SKIP_SUPABASE=True)")
        job_ok = job_fail = company_ok = company_fail = 0

    # --- Metrics / structured run summary ---
    companies_ok = sum(1 for ok in scraper.company_status.values() if ok)
    metrics = {
        "run_id": run_id,
        "source_board": "iCIMS",
        "duration_seconds": round(time.monotonic() - run_started, 2),
        "companies_total": len(companies),
        "companies_ok": companies_ok,
        "companies_failed": len(companies) - companies_ok,
        "jobs_found": len(total_collected_jobs),
        "job_upsert_batches_ok": job_ok,
        "job_upsert_batches_failed": job_fail,
        "company_upsert_batches_ok": company_ok,
        "company_upsert_batches_failed": company_fail,
    }
    log.info("run_metrics %s", json.dumps(metrics))
    print(f"\nRun metrics: {json.dumps(metrics, indent=2)}")

    print("Run: python3 orchestrator.py --merge-only")


if __name__ == "__main__":
    asyncio.run(main())
# ──────────────────────────────────────────────────────────────
#  IMPORTABLE ENTRY POINT (used by pipeline.py)
# ──────────────────────────────────────────────────────────────
async def scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict]:
    companies = load_companies()
    if not companies:
        return []
    scraper = DirectiCIMSScraper(companies)
    return await scraper.scrape(job_profile, location, max_jobs)
