import asyncio
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job
from job_extraction import database_rows
import html as html_lib
import json
import logging
import os
import hashlib
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from supabase import acreate_client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "jobs"

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "10"))
REQUEST_TIMEOUT = float(os.getenv("BAMBOOHR_TIMEOUT", "10"))       # was 20 — most list responses <2s
BAMBOOHR_CONNECT_TIMEOUT = float(os.getenv("BAMBOOHR_CONNECT_TIMEOUT", "5"))       # was 8
BAMBOOHR_DETAIL_CONNECT_TIMEOUT = float(os.getenv("BAMBOOHR_DETAIL_CONNECT_TIMEOUT", "8"))  # was 15
BAMBOOHR_POOL_TIMEOUT = float(os.getenv("BAMBOOHR_POOL_TIMEOUT", "3"))             # was 5
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "2"))                   # was 4 — fewer wasted timeout cycles
BAMBOOHR_LIST_RETRY_COUNT = int(os.getenv("BAMBOOHR_LIST_RETRY_COUNT", "1"))       # was 2
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "50"))
BREAKER_COOLDOWN = int(os.getenv("BREAKER_COOLDOWN", "15"))
SUPABASE_BATCH_SIZE = int(os.getenv("SUPABASE_BATCH_SIZE", "500"))  # Larger batches = fewer round trips
# When true (default), skip the /detail fetch and normalise directly from list data.
# Saves ~82K extra HTTP calls — only set false when you need HTML descriptions.
# Automatically forced to False when description-dependent filters (work_mode,
# job_type, skills, excluded_words) are configured, since those require the full text.
SKIP_DETAILS = os.getenv("BAMBOOHR_SKIP_DETAILS", "true").lower() == "true"

SEARCH_ATTRIBUTES = {
    "location": "",
    "skills": "",
    "experience": "",
    "job_type": "",
    "work_mode": "",
    "excluded_words": "",
    # Reads BAMBOOHR_POST_TIME env var; run_board.py passes --post-time via env
    "post_time": os.getenv("BAMBOOHR_POST_TIME", "day"),
}

KEYWORDS = [""]
COUNTRIES = [""]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bamboohr_scraper")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}

STATE_CODES = {"ca", "ny", "tx", "wa", "ma", "il"}


def clean_html_text(text: str) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
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


def parse_bamboohr_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_company_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def build_filters(job_profile: str, location: str) -> dict[str, Any]:
    return {
        "profiles": split_csv(job_profile),
        "location": (SEARCH_ATTRIBUTES.get("location", "") or location or "").lower().strip(),
        "work_mode": SEARCH_ATTRIBUTES.get("work_mode", "").lower().strip(),
        "job_type": SEARCH_ATTRIBUTES.get("job_type", "").lower().strip(),
        "experience": SEARCH_ATTRIBUTES.get("experience", "").lower().strip(),
        "skills": split_csv(SEARCH_ATTRIBUTES.get("skills", "")),
        "excluded": split_csv(SEARCH_ATTRIBUTES.get("excluded_words", "")),
        "max_age": post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "any")),
    }


def matches_filters(job: dict[str, Any], filters: dict[str, Any], now_utc: datetime) -> bool:
    title = (job.get("title") or "").strip()
    if not title:
        return False

    job_location = job.get("location", "")
    if isinstance(job_location, dict):
        job_location = job_location.get("name", "")
    job_location = str(job_location).strip()
    
    desc_text = clean_html_text(job.get("content", ""))
    text = f"{title} {job_location} {desc_text}".lower()

    profiles = filters["profiles"]
    if profiles and not any(p in title.lower() for p in profiles):
        return False

    loc_filter = filters["location"]
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

    if filters["work_mode"] and filters["work_mode"] not in text:
        return False
    if filters["job_type"] and filters["job_type"] not in text:
        return False
    if filters["experience"] and filters["experience"] not in text:
        return False
    if filters["skills"] and not all(skill in text for skill in filters["skills"]):
        return False
    if filters["excluded"] and any(word in text for word in filters["excluded"]):
        return False

    max_age = filters["max_age"]
    published_at = job.get("publishedAt")
    if max_age and published_at:
        published = parse_bamboohr_datetime(published_at)
        if published:
            if now_utc - published > max_age:
                return False

    return True


def format_iso_time(ts: str | None) -> str | None:
    if not ts:
        return None
    dt = parse_bamboohr_datetime(ts)
    if not dt:
        return ts
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_job(job: dict[str, Any], company_slug: str) -> dict[str, Any]:
    title = (job.get("title") or "").strip()

    location = job.get("location", "")
    if isinstance(location, dict):
        location = location.get("name", "")
    location = str(location).strip()

    desc_text = clean_html_text(job.get("descriptionHtml", "")) or (job.get("descriptionPlain") or "")

    job_id = str(job.get("id") or title)
    raw_key = f"bamboohr:{company_slug}:{job_id}"
    dedupe_key = hashlib.md5(raw_key.encode()).hexdigest()

    # Preserve fields exposed by the detail API, then let enrich_raw_job()
    # supplement salary, work mode, experience, and skills from the text.
    return enrich_raw_job({
        "id": dedupe_key,
        "job_title": title,
        "company": normalize_company_name(company_slug),
        "location": location,
        "job_url": job.get("jobUrl") or "",
        "apply_url": job.get("applyUrl") or "",
        "description": desc_text,
        "description_raw": job.get("descriptionHtml") or desc_text,
        "structured_fields": job.get("structured_fields") or {},
        "source_board": "BambooHR",
        "scraper_type": "api",
        "salary": job.get("compensation") or "",
        "work_mode": job.get("work_mode") or "",
        "is_remote": job.get("is_remote"),
        "job_type": job.get("job_type") or "full_time",
        "scraped_at": format_iso_time(datetime.now(timezone.utc).isoformat()),
        "created_at": format_iso_time(job.get("publishedAt")),
        "posted_at": format_iso_time(job.get("publishedAt")),
        "also_on": []
    })


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


@dataclass
class FetchResult:
    company: str
    jobs: list[dict[str, Any]]


class AsyncSupabaseWriter:
    def __init__(self, table_name: str = SUPABASE_TABLE):
        self.table_name = table_name
        self.client = None

    async def init(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        self.client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

    async def upsert_batch(self, rows: list[dict[str, Any]]):
        if not self.client or not rows:
            return
        
        # Process in batches to avoid Supabase/PostgREST timeouts
        for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
            chunk = rows[i:i + SUPABASE_BATCH_SIZE]
            try:
                await self.client.table(self.table_name).upsert(database_rows(chunk), on_conflict="id").execute()
            except Exception as e:
                print(f"Failed to upsert job chunk: {e}")



class SupabaseCompanyWriter:
    def __init__(self, table_name: str = "companies"):
        self.table_name = table_name
        self.client = None

    def init(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        self.client = create_client(SUPABASE_URL, SUPABASE_KEY)

    def upsert_batch(self, rows: list[dict[str, Any]]):
        if not self.client or not rows:
            return
        
        # Process in batches to avoid Supabase/PostgREST timeouts
        for i in range(0, len(rows), SUPABASE_BATCH_SIZE):
            chunk = rows[i:i + SUPABASE_BATCH_SIZE]
            try:
                self.client.table(self.table_name).upsert(chunk, on_conflict="slug").execute()
            except Exception as e:
                print(f"Failed to upsert company chunk: {e}")





async def fetch_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    retries: int = RETRY_COUNT,
    follow_redirects: bool = False,
    timeout: httpx.Timeout | float | None = None,
    stats: Counter[str] | None = None,
    phase: str = "request",
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    base_delay = 0.5  # Faster first retry — most transients recover quickly

    for attempt in range(retries):
        try:
            request_headers = {"Accept": "application/json"}
            if headers:
                request_headers.update(headers)
            request_kwargs = {
                "headers": request_headers,
                "follow_redirects": follow_redirects,
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout
            resp = await client.get(url, **request_kwargs)
            if stats is not None:
                stats[f"{phase}_requests"] += 1

            if resp.status_code == 404:
                if stats is not None:
                    stats[f"{phase}_status_404"] += 1
                return {"is_404_error": True}

            if resp.status_code == 429:
                if stats is not None:
                    stats[f"{phase}_status_429"] += 1
                retry_after = resp.headers.get("Retry-After")
                try:
                    sleep_for = float(retry_after) if retry_after else base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                except ValueError:
                    sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning("429 for %s, sleeping %.2fs", url, sleep_for)
                await asyncio.sleep(sleep_for)
                continue

            if resp.status_code in (301, 302, 303, 307, 308):
                if stats is not None:
                    stats[f"{phase}_redirect"] += 1
                return None

            # Private boards redirect to bamboohr.com/login.php → 401/403 — skip silently
            if resp.status_code in (401, 403):
                if stats is not None:
                    stats[f"{phase}_status_{resp.status_code}"] += 1
                return None

            if resp.status_code in (500, 502, 503, 504):
                if stats is not None:
                    stats[f"{phase}_status_{resp.status_code}"] += 1
                if attempt < retries - 1:
                    sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    log.warning("Server error %s for %s, retry in %.2fs", resp.status_code, url, sleep_for)
                    await asyncio.sleep(sleep_for)
                    continue
                return None

            resp.raise_for_status()
            
            if "application/json" not in resp.headers.get("Content-Type", ""):
                if stats is not None:
                    stats[f"{phase}_non_json"] += 1
                return None

            try:
                data = resp.json()
            except ValueError:
                if stats is not None:
                    stats[f"{phase}_invalid_json"] += 1
                if attempt < retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.5))
                    continue
                return None
            if stats is not None:
                stats[f"{phase}_ok"] += 1
            return data

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            if stats is not None:
                stats[f"{phase}_{type(exc).__name__}"] += 1
            if attempt == retries - 1:
                log.debug("Final failure for %s: %s", url, exc)  # debug — not WARNING (too noisy at scale)
                return None
            sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.debug("Transient error for %s: %s. Retry in %.2fs", url, exc, sleep_for)
            await asyncio.sleep(sleep_for)
        except Exception as exc:
            if stats is not None:
                stats[f"{phase}_{type(exc).__name__}"] += 1
            log.debug("Unexpected error for %s: %s", url, exc)  # debug — 401 redirects cause this
            return None

    return None


class DirectBambooHRScraper:
    """Optimised BambooHR scraper.

    Key speed wins vs. original:
    1. List-fetch concurrency raised from 10 → 50 (each company is a
       different subdomain, so rate-limits are per-subdomain).
    2. Detail-fetch concurrency raised from 10 → 30.
    3. httpx connection pool bumped to 200 / 80 keep-alive.
    4. Adaptive backoff: if 429 rate starts spiking, the semaphore
       auto-shrinks via a short cooldown pause so we stay under limits.
    5. The list endpoint already returns publishedAt, so we can
       pre-filter by date *before* fetching expensive /detail pages,
       skipping thousands of wasted HTTP calls.
    """

    # Tunable concurrency — each company is its own subdomain so per-company rate limits don't stack.
    # Tuned to avoid overwhelming local DNS/socket pools on the 18k-company run.
    LIST_CONCURRENCY   = int(os.getenv("BAMBOOHR_LIST_CONCURRENCY", "500"))   # was 150 — each co is its own subdomain, no cross-talk
    DETAIL_CONCURRENCY = int(os.getenv("BAMBOOHR_DETAIL_CONCURRENCY", "150"))  # was 50 — per-company rate limits don't stack

    def __init__(self, companies: list[str]):
        self.companies = list(dict.fromkeys(c.strip() for c in companies if c.strip() and not c.lstrip().startswith('#')))
        self._tenant_detail_semaphores = {}
        self._consecutive_429s = 0
        self._drop_reasons: Counter[str] = Counter()
        self._request_stats: Counter[str] = Counter()

    async def _fetch_company_jobs_list(self, client: httpx.AsyncClient, company: str, filters: dict[str, Any]) -> list[dict]:
        url = f"https://{company.lower()}.bamboohr.com/careers/list"
        try:
            data = await fetch_json_with_retry(
                client,
                url,
                retries=BAMBOOHR_LIST_RETRY_COUNT,
                stats=self._request_stats,
                phase="list",
            )
            if isinstance(data, dict) and data.get("is_404_error"):
                return [{"is_404": True, "company": company}]
            if not data:
                self._request_stats["list_no_data"] += 1
                return []
                
            jobs = []
            profiles = filters.get("profiles", [])
            now_utc = datetime.now(timezone.utc)
            max_age = filters.get("max_age")

            for job in data.get("result", []):
                title = str(job.get("jobOpeningName", ""))
                if profiles and not any(p in title.lower() for p in profiles):
                    continue

                # Pre-filter by date using the list endpoint's data
                # so we skip the expensive /detail call for old postings
                published_at = job.get("publishedAt")
                if max_age and published_at:
                    published = parse_bamboohr_datetime(published_at)
                    if published:
                        if now_utc - published > max_age:
                            continue
                    
                loc_obj = job.get("location", {})
                loc_str = ""
                if isinstance(loc_obj, dict):
                    loc_str = f"{loc_obj.get('city', '')} {loc_obj.get('state', '')}"
                elif loc_obj:
                    loc_str = str(loc_obj)
                    
                jobs.append({
                    "id": str(job.get("id")),
                    "job_title": title,
                    "company": company,
                    "job_url": f"https://{company.lower()}.bamboohr.com/careers/{job.get('id')}",
                    "location_str": loc_str,
                    "publishedAt": published_at,
                    # Extra fields from the list endpoint used when SKIP_DETAILS=true
                    "department": job.get("department", ""),
                    "employmentType": job.get("employmentType", ""),
                    "isRemote": job.get("isRemote"),
                })
            if jobs:
                self._request_stats["list_companies_with_jobs"] += 1
                self._request_stats["list_jobs_found"] += len(jobs)
            else:
                self._request_stats["list_empty"] += 1
            return jobs
        except Exception as e:
            log.debug(f"Skipping {company} list fetch: {e}")
            return []

    async def _scrape_job_details(self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, job: dict, company: str, filters: dict[str, Any], now_utc: datetime) -> dict | None:
        tenant_sem = self._tenant_detail_semaphores.setdefault(
            company.lower(), asyncio.Semaphore(max(1, int(os.getenv("BAMBOOHR_TENANT_DETAIL_CONCURRENCY", "8"))))
        )
        async with tenant_sem, semaphore:
            # Adaptive backoff: if we're hitting too many 429s, pause briefly
            if self._consecutive_429s >= 5:
                pause = min(self._consecutive_429s * 0.5, 5.0)
                log.debug(f"Adaptive backoff: pausing {pause:.1f}s due to 429 streak")
                await asyncio.sleep(pause)

            url = job["job_url"]
            api_url = f"{url}/detail"
            try:
                data = await fetch_json_with_retry(
                    client,
                    api_url,
                    retries=RETRY_COUNT,
                    follow_redirects=True,
                    timeout=httpx.Timeout(
                        REQUEST_TIMEOUT,
                        connect=BAMBOOHR_DETAIL_CONNECT_TIMEOUT,
                        pool=BAMBOOHR_POOL_TIMEOUT,
                    ),
                    stats=self._request_stats,
                    phase="detail",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if not data:
                    self._drop_reasons["detail_empty_response"] += 1
                    return None
                if data.get('is_404_error'):
                    self._drop_reasons['detail_expired'] += 1
                    return None
                
                self._consecutive_429s = 0  # reset on success
                
                job_opening = data.get("result", {}).get("jobOpening", {})
                if not isinstance(job_opening, dict) or not job_opening:
                    self._drop_reasons["detail_missing_job_opening"] += 1
                    return None
                html_desc = job_opening.get("description", "")
                published_at = (
                    job.get("publishedAt")
                    or job_opening.get("datePosted")
                    or job_opening.get("publishedAt")
                )

                detail_location = job_opening.get("location") or {}
                if isinstance(detail_location, dict):
                    location_parts = [
                        str(detail_location.get(key) or "").strip()
                        for key in ("city", "state", "addressCountry")
                        if detail_location.get(key)
                    ]
                    location = ", ".join(location_parts) or job["location_str"]
                else:
                    location = str(detail_location).strip() or job["location_str"]

                employment_label = str(job_opening.get("employmentStatusLabel") or "").lower()
                if "part" in employment_label:
                    job_type = "part_time"
                elif any(word in employment_label for word in ("contract", "contractor")):
                    job_type = "contract"
                elif any(word in employment_label for word in ("temporary", "seasonal", "temp")):
                    job_type = "temporary"
                elif any(word in employment_label for word in ("intern", "internship")):
                    job_type = "internship"
                else:
                    job_type = "full_time"

                location_type_raw = job_opening.get("locationType")
                location_type = str(location_type_raw).lower().strip() if location_type_raw is not None else ""
                is_remote = (
                    location_type in {"1", "2", "true", "remote"} or "remote" in location_type
                    if location_type_raw is not None
                    else None
                )

                max_age = filters.get("max_age")
                published = parse_bamboohr_datetime(published_at)
                if max_age and published and now_utc - published > max_age:
                    self._drop_reasons["detail_old_posting"] += 1
                    return None
                if max_age and not published:
                    self._request_stats["detail_missing_posted_at"] += 1
                
                # Mock a job dict to use matches_filters and normalize_job
                mock_job = {
                    "id": job["id"],
                    "title": job["job_title"],
                    "location": location,
                    "content": html_desc,
                    "descriptionHtml": html_desc,
                    "structured_fields": {"job_type": employment_label, "work_mode": "Remote" if is_remote is True else None},
                    "jobUrl": job["job_url"],
                    "applyUrl": job["job_url"],
                    "publishedAt": published_at,
                    "compensation": job_opening.get("compensation") or "",
                    "job_type": job_type,
                    "work_mode": "Remote" if is_remote is True else "",
                    "is_remote": is_remote,
                }
                
                if not matches_filters(mock_job, filters, now_utc):
                    self._drop_reasons["detail_filter_reject"] += 1
                    return None
                    
                return normalize_job(mock_job, company)
            except Exception as e:
                if "429" in str(e):
                    self._consecutive_429s += 1
                self._drop_reasons["detail_exception"] += 1
                log.debug(f"Skipping detail for {url}: {e}")
                return None

    async def scrape(self, job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }

        filters = build_filters(job_profile, location)
        self._drop_reasons = Counter()
        self._request_stats = Counter()
        self._tenant_detail_semaphores.clear()
        now_utc = datetime.now(timezone.utc)
        all_jobs: list[dict] = []
        upload_buffer: list[dict] = []
        stream_upload_fn = getattr(self, "_stream_upload_fn", None)
        seen_urls: set[str] = set()
        missing_companies: list[str] = []

        # ── Shared pipeline queue: Phase 1 producers → Phase 2 consumers ────────
        # Each item is (job_stub, company_slug) or the sentinel None (end of stream)
        queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        STREAM_EVERY = 1000

        # Counters (mutated by coroutines — use list for nonlocal-safe mutation)
        p1_done = [0]        # companies whose list has been fetched
        p1_found = [0]       # pre-filtered jobs enqueued
        p1_missing_posted_at = [0]
        p2_done = [0]        # detail pages fetched
        p2_collected = [0]   # jobs passing detail filter

        t_start = time.time()

        # Determine whether we can skip detail fetches.
        # Detail fetch is required when any filter needs the full HTML description.
        _need_desc_filter = bool(
            filters.get("work_mode")
            or filters.get("job_type")
            or filters.get("skills")
            or filters.get("excluded")
        )
        _skip_details = SKIP_DETAILS and not _need_desc_filter
        if _skip_details:
            log.info("BambooHR SKIP_DETAILS=true — Phase 2 will use list data only (no /detail GETs)")
        else:
            log.info("BambooHR SKIP_DETAILS=false — Phase 2 will fetch full /detail pages")

        list_limit = max(self.LIST_CONCURRENCY, 1)
        detail_limit = max(self.DETAIL_CONCURRENCY, 1)
        connection_limit = min(max(list_limit + detail_limit + 25, 100), 600)
        keepalive_limit = min(max(detail_limit, 50), connection_limit)

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=connection_limit, max_keepalive_connections=keepalive_limit),
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(
                REQUEST_TIMEOUT,
                connect=BAMBOOHR_CONNECT_TIMEOUT,
                pool=BAMBOOHR_POOL_TIMEOUT,
            ),
        ) as client:
            detail_semaphore = asyncio.Semaphore(detail_limit)
            stop_event = asyncio.Event()
            n_consumers = min(detail_limit, 150)
            n_list_workers = min(list_limit, max(len(self.companies), 1))
            company_queue: asyncio.Queue[str] = asyncio.Queue()
            for comp in self.companies:
                company_queue.put_nowait(comp)
            last_progress_log = [0]

            def log_progress(force: bool = False):
                if not self.companies:
                    return
                if not force and p1_done[0] - last_progress_log[0] < 250:
                    return
                last_progress_log[0] = p1_done[0]
                elapsed = time.time() - t_start
                rate = p1_done[0] / max(elapsed, 1)
                remaining = max(len(self.companies) - p1_done[0], 0)
                eta_s = int(remaining / max(rate, 0.1))
                log.info(
                    f"BambooHR Phase 1: {p1_done[0]}/{len(self.companies)} companies "
                    f"({p1_done[0]*100//len(self.companies)}%) | "
                    f"{p1_found[0]} jobs queued | no_posted_at={p1_missing_posted_at[0]} | "
                    f"P2 fetched: {p2_done[0]} | "
                    f"queue={queue.qsize()} | rate={rate:.1f} companies/s | "
                    f"ETA ~{eta_s//60}m{eta_s%60:02d}s"
                )

            async def progress_heartbeat():
                while True:
                    await asyncio.sleep(10)
                    log_progress(force=True)

            # ── PRODUCER: Phase 1 — fetch job lists, push discovered jobs to queue ──
            async def list_producer():
                async def list_worker():
                    while not stop_event.is_set():
                        try:
                            comp = company_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                        try:
                            await asyncio.sleep(random.uniform(0.001, 0.005))  # was 0.01-0.05; reduced 5-10x
                            res = await self._fetch_company_jobs_list(client, comp, filters)
                            if isinstance(res, list):
                                if len(res) == 1 and res[0].get("is_404"):
                                    missing_companies.append(comp)
                                else:
                                    for job_stub in res:
                                        if stop_event.is_set():
                                            break
                                        await queue.put((job_stub, comp))
                                        p1_found[0] += 1
                                        if not job_stub.get("publishedAt"):
                                            p1_missing_posted_at[0] += 1
                        except Exception as exc:
                            log.debug("List worker error for %s: %s", comp, exc)
                        finally:
                            p1_done[0] += 1
                            company_queue.task_done()
                            log_progress(p1_done[0] == len(self.companies))

                workers = [asyncio.create_task(list_worker()) for _ in range(n_list_workers)]
                try:
                    await asyncio.gather(*workers)
                finally:
                    for task in workers:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)

                # Sentinel: one None per consumer signals end-of-stream
                for _ in range(n_consumers):
                    await queue.put(None)

            # ── CONSUMER: Phase 2 — drain queue, fetch full detail pages ─────────
            async def detail_consumer():
                while True:
                    item = await queue.get()
                    if item is None:
                        queue.task_done()
                        break
                    job_stub, comp = item
                    try:
                        if stop_event.is_set():
                            continue

                        # ── Fast path: skip /detail fetch when SKIP_DETAILS=true ──
                        # The list endpoint already provides title, location, publishedAt,
                        # and job URL. We synthesise a lightweight job record directly,
                        # saving ~82 K extra HTTP GETs per run.
                        effective_skip = _skip_details and not _need_desc_filter
                        if effective_skip:
                            loc = job_stub.get("location_str", "")
                            et  = str(job_stub.get("employmentType") or "").lower()
                            if "part" in et:
                                jtype = "part_time"
                            elif any(w in et for w in ("contract", "contractor")):
                                jtype = "contract"
                            elif any(w in et for w in ("temporary", "seasonal", "temp")):
                                jtype = "temporary"
                            elif any(w in et for w in ("intern", "internship")):
                                jtype = "internship"
                            else:
                                jtype = "full_time"
                            is_remote = bool(job_stub.get("isRemote")) or None
                            mock_job = {
                                "id": job_stub["id"],
                                "title": job_stub["job_title"],
                                "location": loc,
                                "content": "",
                                "descriptionHtml": "",
                                "structured_fields": {"job_type": et, "work_mode": "Remote" if is_remote else None},
                                "jobUrl": job_stub["job_url"],
                                "applyUrl": job_stub["job_url"],
                                "publishedAt": job_stub.get("publishedAt"),
                                "compensation": "",
                                "job_type": jtype,
                                "work_mode": "Remote" if is_remote else "",
                                "is_remote": is_remote,
                            }
                            if not matches_filters(mock_job, filters, now_utc):
                                self._drop_reasons["list_filter_reject"] += 1
                                p2_done[0] += 1
                                queue.task_done()
                                continue
                            d_job = normalize_job(mock_job, comp)
                            p2_done[0] += 1
                        else:
                            d_job = await self._scrape_job_details(
                                client, detail_semaphore, job_stub, comp, filters, now_utc
                            )
                            p2_done[0] += 1
                        if isinstance(d_job, dict) and d_job:
                            if 0 < max_jobs <= len(all_jobs):
                                stop_event.set()
                                continue
                            url = d_job.get("job_url") or ""
                            if url and url in seen_urls:
                                self._drop_reasons["duplicate_url"] += 1
                            else:
                                if url:
                                    seen_urls.add(url)
                                all_jobs.append(d_job)
                                upload_buffer.append(d_job)
                                p2_collected[0] += 1
                                if stream_upload_fn and len(upload_buffer) >= STREAM_EVERY:
                                    batch = upload_buffer[:]
                                    upload_buffer.clear()
                                    try:
                                        await stream_upload_fn(batch)
                                        elapsed = time.time() - t_start
                                        eta_s = int(
                                            (max(p1_found[0], p2_done[0]) - p2_done[0])
                                            / max(p2_done[0] / elapsed, 0.1)
                                        )
                                        log.info(
                                            f"BambooHR stream-upload: {len(batch)} jobs | "
                                            f"total={p2_collected[0]} | "
                                            f"P1:{p1_done[0]}/{len(self.companies)} "
                                            f"P2:{p2_done[0]}/{p1_found[0]} | "
                                            f"ETA ~{eta_s//60}m{eta_s%60:02d}s"
                                        )
                                    except Exception as ue:
                                        log.warning(f"Stream-upload failed: {ue}")
                                if 0 < max_jobs <= len(all_jobs):
                                    stop_event.set()
                    except Exception as exc:
                        log.debug("Detail consumer error: %s", exc)
                    finally:
                        queue.task_done()

            # ── Launch: producer + consumers run concurrently ─────────────────────
            log.info(
                f"BambooHR PIPELINE START — {len(self.companies)} companies | "
                f"LIST_CONCURRENCY={self.LIST_CONCURRENCY} DETAIL_CONCURRENCY={self.DETAIL_CONCURRENCY} | "
                f"post_time={SEARCH_ATTRIBUTES.get('post_time', 'any') or 'any'}"
            )
            heartbeat = asyncio.create_task(progress_heartbeat())
            consumers = [asyncio.create_task(detail_consumer()) for _ in range(n_consumers)]
            try:
                await list_producer()           # blocks until all companies are list-fetched
                await asyncio.gather(*consumers)  # drain remaining detail queue
            finally:
                for task in consumers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*consumers, return_exceptions=True)
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

            elapsed = time.time() - t_start
            log.info(
                f"BambooHR PIPELINE DONE — {p2_collected[0]} jobs | "
                f"{p2_done[0]} detail pages in {elapsed:.0f}s | "
                f"{p2_done[0]/max(elapsed,1):.1f} detail/s"
            )
            if self._drop_reasons:
                log.info("BambooHR detail drops: %s", dict(self._drop_reasons))
            log.info("BambooHR request stats: %s", dict(self._request_stats))

            # Upload any remaining buffered jobs
            if stream_upload_fn and upload_buffer:
                try:
                    await stream_upload_fn(upload_buffer[:])
                    log.info(f"BambooHR final stream-upload: {len(upload_buffer)} jobs")
                except Exception as ue:
                    log.warning(f"Final stream-upload failed: {ue}")
                upload_buffer.clear()

            # Write 404 companies file
            if missing_companies:
                out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bamboohr_404_companies.txt")
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

        return all_jobs[:max_jobs] if max_jobs > 0 else all_jobs



def load_companies() -> list[str]:
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bamboohr_companies.txt")
    if not os.path.exists(filepath):
        log.warning("%s not found. Please create it with one company name per line.", filepath)
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        companies = []
        for line in f:
            slug = line.strip()
            if not slug or slug.startswith("#"):
                continue
            # Strip spaces — subdomains cannot contain whitespace.
            # "Domain Tools" -> "domaintools", "Q Ctrl" -> "qctrl"
            slug = re.sub(r'\s+', '', slug)
            if slug:
                companies.append(slug)
        return companies


def normalize_company_row(company: dict[str, Any]) -> dict[str, Any]:
    return company


def chunked(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def run_scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
    companies = load_companies()
    if not companies:
        return []
    scraper = DirectBambooHRScraper(companies)
    return await scraper.scrape(job_profile, location, max_jobs)


async def main():
    scrape_start_time = datetime.now(timezone.utc).isoformat()
    print("=" * 60)
    print("BambooHR Job Scraper (Direct API) - Full Combined Version")
    print(f"Keywords : {KEYWORDS}")
    print(f"Countries: {COUNTRIES}")
    print("=" * 60)

    companies = load_companies()
    print(f"Loaded {len(companies)} companies from bamboohr_companies.txt")
    if not companies:
        print("No companies to scrape. Exiting.")
        return

    scraper = DirectBambooHRScraper(companies)
    job_writer = AsyncSupabaseWriter()
    await job_writer.init()

    company_writer = SupabaseCompanyWriter()
    company_writer.init()

    all_jobs: list[dict[str, Any]] = []
    total_collected_jobs: list[dict[str, Any]] = []
    all_company_rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for country in COUNTRIES:
        for keyword in KEYWORDS:
            print(f"\n--> '{keyword}' | {country}")
            jobs = await scraper.scrape(keyword, country, 0)
            new_jobs_count = 0

            for job in jobs:
                url = job.get("job_url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                job["role_category"] = keyword
                job["country"] = country
                all_jobs.append(job)
                total_collected_jobs.append(job)
                new_jobs_count += 1

            for comp in companies:
                company_row = {
                    "slug": comp,
                    "source_board": "BambooHR",
                    "discovered_at": datetime.now(timezone.utc).isoformat()
                }
                all_company_rows.append(normalize_company_row(company_row))

            print(f"   [OK] {new_jobs_count} jobs collected")

            if len(all_jobs) >= SUPABASE_BATCH_SIZE:
                await job_writer.upsert_batch(all_jobs)
                all_jobs = []

            if len(all_company_rows) >= SUPABASE_BATCH_SIZE:
                company_writer.upsert_batch(all_company_rows)
                all_company_rows = []

    print(f"\n{'=' * 60}")
    print("Scrape complete")
    print(f"{'=' * 60}")

    if all_jobs:
        await job_writer.upsert_batch(all_jobs)

    if all_company_rows:
        company_writer.upsert_batch(all_company_rows)

    if total_collected_jobs:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_bamboohr_jobs.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(total_collected_jobs, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(total_collected_jobs)} jobs to all_bamboohr_jobs.json")

    print("\nRunning expiration cleanup RPC...")
    try:
        await job_writer.client.rpc("mark_expired_jobs", {"p_scrape_start_time": scrape_start_time, "p_source_board": "BambooHR"}).execute()
        print("Successfully expired stale jobs.")
    except Exception as e:
        print(f"Failed to expire stale jobs: {e}")

    print("Run: python3 orchestrator.py --merge-only")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
