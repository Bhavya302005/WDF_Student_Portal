from __future__ import annotations
import asyncio
from collections import Counter, deque
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

try:
    import orjson
    JSON_LOADS = orjson.loads
    JSON_DUMPS = lambda x: orjson.dumps(x, option=orjson.OPT_INDENT_2).decode()
except ImportError:
    JSON_LOADS = json.loads
    JSON_DUMPS = lambda x: json.dumps(x, indent=2, ensure_ascii=False)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Standalone-safe imports ──
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
    from scraper_runtime import RequestGate, retry_after_seconds, paced_request
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

    class RequestGate:
        def __init__(self, rate, min_rate, max_rate, cooldown=2.0, recovery=0.01):
            self.rate = max(min_rate, min(rate, max_rate))
            self.min_rate = min_rate
            self.max_rate = max_rate
            self.cooldown = cooldown
            self.recovery = recovery
            self._next_slot = 0.0
            self._paused_until = 0.0
            self._lock = asyncio.Lock()

        async def acquire(self):
            async with self._lock:
                now = asyncio.get_running_loop().time()
                slot = max(now, self._next_slot, self._paused_until)
                self._next_slot = slot + (1.0 / self.rate)
                delay = slot - now
            if delay > 0:
                await asyncio.sleep(delay)

        async def on_throttle(self, retry_after=None):
            async with self._lock:
                self.rate = max(self.min_rate, self.rate * 0.7)
                pause = min(retry_after if retry_after is not None else self.cooldown, 30.0)
                now = asyncio.get_running_loop().time()
                self._paused_until = max(self._paused_until, now + pause)
                self._next_slot = max(self._next_slot, self._paused_until)

        def on_success(self):
            self.rate = min(self.max_rate, self.rate + self.recovery)

    _BOARD_GATES: dict[str, RequestGate] = {}

    async def paced_request(client, method, url, board="DEFAULT", rate=50.0, **kwargs):
        gate = _BOARD_GATES.setdefault(board, RequestGate(rate, rate * 0.1, rate))
        await gate.acquire()
        resp = await client.request(method, url, **kwargs)
        if resp.status_code == 429:
            await gate.on_throttle(retry_after_seconds(resp.headers.get("Retry-After")))
        else:
            gate.on_success()
        return resp

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "jobs"

MAX_CONCURRENT_COMPANIES = int(os.getenv("WORKDAY_COMPANY_CONCURRENCY", os.getenv("MAX_CONCURRENT_JOBS", "120")))
MAX_PAGES_PER_COMPANY    = int(os.getenv("WORKDAY_MAX_PAGES_PER_COMPANY", os.getenv("MAX_PAGES_PER_COMPANY", "100")))
MAX_CONCURRENT_PAGES     = int(os.getenv("WORKDAY_PAGE_CONCURRENCY", "180"))
PER_TENANT_PAGE_LIMIT    = int(os.getenv("WORKDAY_TENANT_PAGE_CONCURRENCY", "4"))
MAX_CONCURRENT_DESCS     = int(os.getenv("WORKDAY_DETAIL_CONCURRENCY", os.getenv("MAX_CONCURRENT_DESCS", "800")))
PER_TENANT_DESC_LIMIT    = int(os.getenv("WORKDAY_TENANT_DETAIL_CONCURRENCY", "40"))   # was 25→80→40; 80 caused 99% drops
DETAIL_REQUEST_RATE      = float(os.getenv("WORKDAY_DETAIL_REQUEST_RATE", "300"))
DETAIL_MIN_REQUEST_RATE  = float(os.getenv("WORKDAY_DETAIL_MIN_REQUEST_RATE", "30"))
DETAIL_MAX_REQUEST_RATE  = float(os.getenv("WORKDAY_DETAIL_MAX_REQUEST_RATE", "300"))
REQUEST_TIMEOUT          = float(os.getenv("WORKDAY_TIMEOUT", "10"))
DESC_TIMEOUT             = float(os.getenv("WORKDAY_DETAIL_TIMEOUT", os.getenv("DESC_TIMEOUT", "12")))   # was 15→8→12; 8s too aggressive
DESC_CONNECT_TIMEOUT     = float(os.getenv("WORKDAY_DETAIL_CONNECT_TIMEOUT", "6"))    # was 8→5→6
DESC_POOL_TIMEOUT        = float(os.getenv("WORKDAY_DETAIL_POOL_TIMEOUT", "4"))       # was 5→3→4
RETRY_COUNT              = int(os.getenv("WORKDAY_RETRY_COUNT", os.getenv("RETRY_COUNT", "3")))
DESC_RETRY_COUNT         = int(os.getenv("WORKDAY_DETAIL_RETRY_COUNT", "3"))           # was 4→2→3
BREAKER_THRESHOLD        = int(os.getenv("BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN         = int(os.getenv("BREAKER_COOLDOWN", "30"))
SUPABASE_BATCH_SIZE      = int(os.getenv("SUPABASE_BATCH_SIZE", "500"))
PAGE_LIMIT               = int(os.getenv("WORKDAY_PAGE_LIMIT", "20"))
QUERY_TOTAL_CAP          = int(os.getenv("WORKDAY_QUERY_TOTAL_CAP", "2000"))
SKIP_SUPABASE            = os.getenv("SKIP_SUPABASE", "true").lower() == "true"
LOCAL_OUTPUT             = os.getenv("LOCAL_OUTPUT", "true").lower() == "true"
# When true, skip the expensive per-job detail GET (description/skills).
# Jobs are still collected from list pages; only HTML descriptions are missing.
# Set to "false" to re-enable full description enrichment at the cost of ~10x longer runtime.
SKIP_DETAILS             = os.getenv("WORKDAY_SKIP_DETAILS", "true").lower() == "true"

SEARCH_ATTRIBUTES = {
    "location": "",
    "skills": "",
    "experience": "",
    "job_type": "",
    "work_mode": "",
    "excluded_words": "",
    "post_time": os.getenv("WORKDAY_POST_TIME", "day"),
}

KEYWORDS  = [""]
COUNTRIES = ["global"]

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
log = logging.getLogger("workday_api_scraper")

URL_PATTERN = re.compile(
    r"^https?://(?P<company>[^.]+)\.(?P<instance>wd\d+)\.myworkdayjobs\.com/(?P<site>[^/?#]+)"
)

_RAW_SKILLS = [
    "Python","Java","C++","C#","Go","Rust","Ruby","PHP","Swift","Kotlin",
    "JavaScript","TypeScript","React","Angular","Vue","Svelte","Node",
    "Django","Flask","Spring","SQL","NoSQL","PostgreSQL","MySQL","MongoDB",
    "Redis","Elasticsearch","GraphQL","AWS","GCP","Azure","Docker",
    "Kubernetes","Terraform","CI/CD","Machine Learning","AI","Data Science","Golang",
]
_SKILL_PATTERNS = [(s, re.compile(rf"\b{re.escape(s)}\b", re.IGNORECASE)) for s in _RAW_SKILLS]

_RE_REMOTE = re.compile(r"remote|wfh|work from home|telecommute|distributed|remote-first", re.I)
_RE_HYBRID = re.compile(r"hybrid", re.I)
_RE_ONSITE = re.compile(r"onsite|in-office|in office|in-person|in person", re.I)
_RE_DAYS   = re.compile(r"(\d+)\+?\s+days?", re.I)
# Same false-positive guards as scraper_utils.extract_work_mode: "remote
# locations" is geography not a work arrangement, and "not/non/no remote"
# is a negation a bare _RE_REMOTE match can't tell from an affirmative one.
_RE_REMOTE_GEOGRAPHIC = re.compile(r"remote\s+(?:location|area|region|site|communit)\w*", re.I)
_RE_REMOTE_NEGATION = re.compile(r"(?:not|non|no|isn't|aren't|without)[\s-]{0,15}?(?:a\s+|fully\s+|100%\s+)?remote", re.I)


def split_csv(value: str) -> list[str]:
    return [i.strip().lower() for i in value.split(",") if i.strip()] if value else []

def post_time_to_delta(value: str) -> timedelta | None:
    return {"hour": timedelta(hours=1), "day": timedelta(days=1),
            "week": timedelta(weeks=1), "month": timedelta(days=30),
            "year": timedelta(days=365)}.get((value or "any").lower().strip())

def extract_work_mode(title: str, location: str) -> str:
    content = f"{title} {location}"
    content = _RE_REMOTE_GEOGRAPHIC.sub("", content)
    content = _RE_REMOTE_NEGATION.sub("", content)
    if _RE_REMOTE.search(content): return "Remote"
    if _RE_HYBRID.search(content): return "Hybrid"
    if _RE_ONSITE.search(content): return "Onsite"
    return ""

def extract_skills(text: str) -> str:
    return ", ".join(s for s, pat in _SKILL_PATTERNS if pat.search(text))

def format_iso_time(ts: str | None) -> str | None:
    if not ts: return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts

def build_filters(job_profile: str, location: str) -> dict[str, Any]:
    return {
        "profiles":  split_csv(job_profile),
        "location":  (SEARCH_ATTRIBUTES.get("location","") or location or "").lower().strip(),
        "work_mode": SEARCH_ATTRIBUTES.get("work_mode","").lower().strip(),
        "job_type":  SEARCH_ATTRIBUTES.get("job_type","").lower().strip(),
        "excluded":  split_csv(SEARCH_ATTRIBUTES.get("excluded_words","")),
        "max_age":   post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time","any")),
    }

def posted_age_days(value: str | None) -> int | None:
    posted_on = (value or "").lower().strip()
    if not posted_on:
        return None
    if "yesterday" in posted_on:
        return 1
    if "today" in posted_on:
        return 0
    match = _RE_DAYS.search(posted_on)
    if not match:
        return None
    age = int(match.group(1))
    return age + 1 if "+" in match.group(0) else age


def page_is_past_date_window(jobs: list[dict], max_age: timedelta | None) -> bool:
    if not jobs or not max_age:
        return False
    ages = [posted_age_days(job.get("postedOn")) for job in jobs]
    return all(age is not None and age > max_age.days for age in ages)

def matches_filters(job: dict[str, Any], filters: dict[str, Any]) -> bool:
    title = (job.get("title") or "").strip()
    if not title: return False
    loc   = (job.get("locationsText") or "").strip()
    text  = f"{title} {loc}".lower()

    if filters["profiles"] and not any(p in title.lower() for p in filters["profiles"]):
        return False
    lf = filters["location"]
    if lf and lf not in {"global","any",""}:
        if lf not in text: return False
    if filters["work_mode"] and filters["work_mode"] not in text: return False
    if filters["job_type"]  and filters["job_type"]  not in text: return False
    if filters["excluded"]  and any(w in text for w in filters["excluded"]): return False

    max_age = filters["max_age"]
    if max_age:
        age_days = posted_age_days(job.get("postedOn"))
        if age_days is not None and age_days > max_age.days:
            return False
    return True

def normalize_job(job: dict[str, Any], company_name: str, base_url: str,
                  now_str: str) -> dict[str, Any]:
    title    = (job.get("title") or "").strip()
    location = (job.get("locationsText") or "").strip()
    ext_path = job.get("externalPath", "")
    job_url  = f"{base_url}{ext_path}" if ext_path else base_url
    job_id   = str(job.get("bulletFields",[None])[0] or ext_path.rsplit("/",1)[-1] or title)
    dedupe   = hashlib.md5(f"workday:{company_name}:{job_id}".encode()).hexdigest()

    rts = (job.get("remoteType") or "").lower()
    if "remote" in rts and "hybrid" not in rts: wm = "Remote"
    elif "hybrid" in rts:                        wm = "Hybrid"
    elif "onsite" in rts or "office" in rts:     wm = "Onsite"
    else:                                         wm = extract_work_mode(title, location)

    enriched = enrich_raw_job({
        "id": dedupe, "job_title": title, "company": company_name,
        "location": location, "job_url": job_url, "apply_url": job_url,
        "description": "", "salary": "", "salary_min": None, "salary_max": None,
        "experience": "", "skills": extract_skills(title),
        "work_mode": wm, "source_board": "Workday", "scraper_type": "api",
        "is_remote": (wm == "Remote"), "job_type": job.get("timeType",""),
        "scraped_at": now_str, "created_at": None, "also_on": [],
    })
    for k in ("experience_min", "experience_max"):
        enriched.pop(k, None)
    return enriched

def strip_html(raw: str) -> str:
    import html as html_lib
    text = html_lib.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class CircuitBreaker:
    __slots__ = ("threshold","cooldown","failures","state","opened_at")

    def __init__(self, threshold=5, cooldown=30):
        self.threshold = threshold; self.cooldown = cooldown
        self.failures  = 0;         self.state    = "closed"; self.opened_at = 0.0

    def allow(self) -> bool:
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self.cooldown:
                self.state = "half_open"; return True
            return False
        return True

    def ok(self):   self.failures = 0; self.state = "closed"
    def fail(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.state = "open"; self.opened_at = time.monotonic()


class AdaptiveRequestGate(RequestGate):
    def __init__(self):
        super().__init__(DETAIL_REQUEST_RATE, DETAIL_MIN_REQUEST_RATE,
                         DETAIL_MAX_REQUEST_RATE, cooldown=2.0, recovery=0.01)


class SupabaseWriter:
    def __init__(self):
        self.jobs_client      = None
        self.companies_client = None

    async def init(self):
        if SKIP_SUPABASE or not SUPABASE_URL or not SUPABASE_KEY: return
        self.jobs_client      = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
        self.companies_client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

    async def upsert_jobs(self, rows: list[dict]):
        if not self.jobs_client or not rows: return
        deduped = list({r["id"]: r for r in rows}.values())
        for i in range(0, len(deduped), SUPABASE_BATCH_SIZE):
            chunk = deduped[i:i+SUPABASE_BATCH_SIZE]
            try:
                await self.jobs_client.table(SUPABASE_TABLE).upsert(database_rows(chunk), on_conflict="id").execute()
            except Exception as e:
                log.warning("job upsert failed: %s", e)

    async def upsert_companies(self, rows: list[dict]):
        if not self.companies_client or not rows: return
        deduped = list({r["slug"]: r for r in rows}.values())
        for i in range(0, len(deduped), SUPABASE_BATCH_SIZE):
            chunk = deduped[i:i+SUPABASE_BATCH_SIZE]
            try:
                await self.companies_client.table("companies").upsert(chunk, on_conflict="slug").execute()
            except Exception as e:
                log.warning("company upsert failed: %s", e)

    async def expire_stale(self, scrape_start: str):
        if not self.jobs_client: return
        try:
            await self.jobs_client.rpc("mark_expired_jobs", {
                "p_scrape_start_time": scrape_start, "p_source_board": "Workday"
            }).execute()
            log.info("Successfully expired stale jobs.")
        except Exception as e:
            log.error("Failed to expire stale jobs: %s", e)


@dataclass
class FetchResult:
    company: str
    jobs: list[dict[str, Any]] = field(default_factory=list)
    # Stores (normalized_job_dict, externalPath, co, inst, site) for description fetch
    desc_info: list[tuple] = field(default_factory=list)
    is_404: bool = False


class DirectWorkdayScraper:
    def __init__(self, companies: list[dict[str, str]]):
        # ── Sharding: split company list across parallel runners ──
        # Set WORKDAY_SHARD="1/4" to process shard 1 of 4 (1-indexed).
        shard_spec = os.getenv("WORKDAY_SHARD", "")
        if shard_spec and "/" in shard_spec:
            idx_str, total_str = shard_spec.split("/", 1)
            shard_idx, shard_total = int(idx_str), int(total_str)
            # Deterministic split: sort companies by name, take every Nth
            sorted_companies = sorted(companies, key=lambda c: c["name"])
            companies = [c for i, c in enumerate(sorted_companies) if i % shard_total == (shard_idx - 1)]
            log.info("WORKDAY_SHARD=%s → processing %d/%d companies",
                     shard_spec, len(companies), len(sorted_companies))
        self.companies = companies
        self.breakers  = {c["name"]: CircuitBreaker(BREAKER_THRESHOLD, BREAKER_COOLDOWN)
                          for c in companies}
        self.stats: Counter[str] = Counter()
        self._page_semaphore: asyncio.Semaphore | None = None
        self._tenant_page_semaphores: dict[str, asyncio.Semaphore] = {}
        self._tenant_desc_semaphores: dict[str, asyncio.Semaphore] = {}
        self._detail_gate: AdaptiveRequestGate | None = None

    async def _post(self, client: httpx.AsyncClient, url: str,
                    payload: dict, name: str) -> dict | None:
        for attempt in range(RETRY_COUNT):
            try:
                r = await paced_request(client, 'post', url, board='WORKDAY', rate=DETAIL_REQUEST_RATE, json=payload, timeout=REQUEST_TIMEOUT)
                self.stats["list_requests"] += 1
                if r.status_code in (400, 404):
                    self.stats[f"list_status_{r.status_code}"] += 1
                    if r.status_code == 404:
                        return {"is_404_error": True}
                    return None
                if r.status_code in (401, 403, 429):
                    self.stats[f"list_status_{r.status_code}"] += 1
                    delay = retry_after_seconds(r.headers.get("Retry-After"))
                    if delay is None:
                        delay = 0.5 * (2 ** attempt)
                    if attempt < RETRY_COUNT - 1:
                        await asyncio.sleep(delay)
                    continue
                if r.status_code >= 500:
                    self.stats[f"list_status_{r.status_code}"] += 1
                    if attempt < RETRY_COUNT-1:
                        await asyncio.sleep(0.3*(2**attempt)); continue
                    break
                r.raise_for_status()
                try:
                    data = JSON_LOADS(r.content)
                except (ValueError, TypeError):
                    self.stats["list_invalid_json"] += 1
                    if attempt < RETRY_COUNT - 1:
                        await asyncio.sleep(0.3 * (2 ** attempt))
                        continue
                    break
                self.stats["list_pages_ok"] += 1
                return data
            except httpx.HTTPError as exc:
                self.stats[f"list_{type(exc).__name__}"] += 1
                if attempt < RETRY_COUNT-1: await asyncio.sleep(0.3*(2**attempt))
            except Exception as exc:
                self.stats[f"list_{type(exc).__name__}"] += 1
                break
        self.stats["list_pages_dropped"] += 1
        return None

    async def _get(self, client: httpx.AsyncClient, url: str) -> dict | None:
        """Fetch one job detail with retries.  No global rate gate — the
        per-tenant semaphore already limits concurrency per Workday instance."""
        timeout = httpx.Timeout(
            DESC_TIMEOUT,
            connect=DESC_CONNECT_TIMEOUT,
            pool=DESC_POOL_TIMEOUT,
        )
        for attempt in range(DESC_RETRY_COUNT):
            try:
                r = await client.get(url, timeout=timeout)
                self.stats["detail_requests"] += 1
                if r.status_code == 200:
                    try:
                        data = JSON_LOADS(r.content)
                    except (ValueError, TypeError):
                        self.stats["detail_invalid_json"] += 1
                    else:
                        self.stats["detail_pages_ok"] += 1
                        return data
                else:
                    self.stats[f"detail_status_{r.status_code}"] += 1
                    if r.status_code not in (403, 429) and r.status_code < 500:
                        break
                    delay = retry_after_seconds(r.headers.get("Retry-After"))
                    if delay is None:
                        delay = 0.3 * (2 ** attempt)
                    if r.status_code == 429:
                        delay = max(delay, 2.0)
                    if attempt < DESC_RETRY_COUNT - 1:
                        await asyncio.sleep(delay + random.uniform(0, 0.2))
                        continue
            except httpx.HTTPError as exc:
                self.stats[f"detail_{type(exc).__name__}"] += 1
            except Exception as exc:
                self.stats[f"detail_{type(exc).__name__}"] += 1
                break
            if attempt < DESC_RETRY_COUNT - 1:
                await asyncio.sleep(0.3 * (2 ** attempt) + random.uniform(0, 0.2))
        self.stats["detail_pages_dropped"] += 1
        return None

    async def _fetch_company(self, client: httpx.AsyncClient,
                              company: dict[str, str],
                              filters: dict[str, Any],
                              now_str: str) -> FetchResult:
        name = company["name"]
        url  = company["url"]

        m = URL_PATTERN.match(url.rstrip("/"))
        if not m: return FetchResult(name)

        co, inst, site = m.group("company"), m.group("instance"), m.group("site")
        api      = f"https://{co}.{inst}.myworkdayjobs.com/wday/cxs/{co}/{site}/jobs"
        base_url = url.split("/wday/")[0].rstrip("/")

        data = await self._post(client, api,
                                {"appliedFacets":{},"limit":PAGE_LIMIT,"offset":0,"searchText":""},
                                name)
        if isinstance(data, dict) and data.get("is_404_error"):
            return FetchResult(name, is_404=True)
            
        if not data: return FetchResult(name)

        total = int(data.get("total", 0))
        if total == 0: return FetchResult(name)

        out: list[dict]       = []
        desc_info: list[tuple] = []
        max_age = filters.get("max_age")

        def _add(job: dict):
            nj  = normalize_job(job, name, base_url, now_str)
            ext = job.get("externalPath", "")
            out.append(nj)
            # Skip detail fetches entirely when WORKDAY_SKIP_DETAILS=true.
            # This saves ~130K HTTP GETs (the main runtime bottleneck).
            if SKIP_DETAILS:
                return
            # Only fetch descriptions for jobs within the post_time window
            if ext and ext.startswith("/job/"):
                if max_age:
                    age = posted_age_days(job.get("postedOn"))
                    if age is not None and age <= max_age.days:
                        desc_info.append((nj, ext, co, inst, site))
                    # Skip desc fetch for jobs with no date or old dates
                else:
                    desc_info.append((nj, ext, co, inst, site))

        for job in data.get("jobPostings", []):
            if matches_filters(job, filters): _add(job)

        if page_is_past_date_window(data.get('jobPostings', []), filters.get('max_age')):
            self.stats['companies_stopped_at_date_cutoff'] += 1
            return FetchResult(name, out, desc_info)

        if total > PAGE_LIMIT:
            fetch_cap = min(total, QUERY_TOTAL_CAP, MAX_PAGES_PER_COMPANY * PAGE_LIMIT)
            tenant_key = f"{co}.{inst}"
            tenant_sem = self._tenant_page_semaphores.setdefault(
                tenant_key, asyncio.Semaphore(PER_TENANT_PAGE_LIMIT)
            )

            async def _page(offset: int):
                if self._page_semaphore is None:
                    raise RuntimeError("page semaphore was not initialized")
                async with self._page_semaphore, tenant_sem:
                    d = await self._post(
                        client,
                        api,
                        {"appliedFacets": {}, "limit": PAGE_LIMIT,
                         "offset": offset, "searchText": ""},
                        name,
                    )
                    return (d or {}).get("jobPostings", [])

            stopped_for_date = False
            offset = PAGE_LIMIT
            while offset < fetch_cap:
                offsets = list(range(
                    offset,
                    min(offset + PER_TENANT_PAGE_LIMIT * PAGE_LIMIT, fetch_cap),
                    PAGE_LIMIT,
                ))
                pages = await asyncio.gather(*(_page(page_offset) for page_offset in offsets))
                for batch in pages:
                    for job in batch:
                        if matches_filters(job, filters):
                            _add(job)
                stopped_for_date = all(
                    page_is_past_date_window(batch, filters.get("max_age"))
                    for batch in pages
                )
                offset += len(offsets) * PAGE_LIMIT
                if stopped_for_date:
                    self.stats["companies_stopped_at_date_cutoff"] += 1
                    self.stats["list_pages_avoided"] += max((fetch_cap - offset) // PAGE_LIMIT, 0)
                    break
            if not stopped_for_date and fetch_cap < total:
                self.stats["companies_at_page_cap"] += 1

        return FetchResult(name, out, desc_info)

    async def scrape(self, job_profile: str, location: str,
                     write_queue: asyncio.Queue | None = None) -> list[dict]:
        filters = build_filters(job_profile, location)
        now_str = format_iso_time(datetime.now(timezone.utc).isoformat())
        self.stats.clear()
        self._page_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
        self._tenant_page_semaphores.clear()
        self._tenant_desc_semaphores.clear()
        self._detail_gate = AdaptiveRequestGate()

        headers = {
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept":          "application/json",
            "Content-Type":    "application/json",
            "Accept-Encoding": "gzip, deflate, br",
        }

        company_sem  = asyncio.Semaphore(MAX_CONCURRENT_COMPANIES)
        done_count   = 0
        total_count  = len(self.companies)
        list_started = time.monotonic()
        last_company_log = list_started

        async def bounded(comp):
            nonlocal done_count, last_company_log
            async with company_sem:
                result = await self._fetch_company(client, comp, filters, now_str)
                done_count += 1
                now = time.monotonic()
                if done_count % 100 == 0 or done_count == total_count or now - last_company_log >= 10:
                    elapsed = max(now - list_started, 0.001)
                    rate = done_count / elapsed
                    eta = (total_count - done_count) / rate if rate else 0
                    log.info("  [%d/%d] companies | %.1f/s | ETA ~%.1fm", done_count, total_count, rate, eta/60)
                    last_company_log = now
                return result

        async with httpx.AsyncClient(
            http2=False,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=max(MAX_CONCURRENT_COMPANIES, MAX_CONCURRENT_PAGES, MAX_CONCURRENT_DESCS) + 40,
                max_keepalive_connections=max(MAX_CONCURRENT_DESCS, 80),
            ),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as client:

            # ── Phase 1: Scrape all company job lists (fast) ──────────────────
            results = await asyncio.gather(
                *(bounded(c) for c in self.companies),
                return_exceptions=True,
            )
            done = sum(1 for r in results if not isinstance(r, Exception))
            log.info("[done] %d/%d companies scraped", done, len(self.companies))

            # Build deduped job list + collect all description tasks
            all_jobs: list[dict]   = []
            seen: set[str]         = set()
            desc_by_id: dict[str, tuple] = {}
            missing_companies: list[str] = []

            for r in results:
                if isinstance(r, Exception): continue
                if getattr(r, "is_404", False):
                    missing_companies.append(r.company)
                for job in r.jobs:
                    jid = job.get("id","")
                    if jid in seen: continue
                    seen.add(jid)
                    all_jobs.append(job)
                for item in r.desc_info:
                    jid = item[0].get("id", "")
                    if jid and jid not in desc_by_id:
                        desc_by_id[jid] = item

            all_desc_info = list(desc_by_id.values())
            self.stats["jobs_unique"] = len(all_jobs)
            self.stats["detail_duplicates_skipped"] = sum(len(r.desc_info) for r in results if isinstance(r, FetchResult)) - len(all_desc_info)

            # ── Phase 2: Fetch descriptions via worker pool (not 14k tasks at once) ──
            if all_desc_info:
                log.info("Fetching descriptions for %d jobs ...", len(all_desc_info))
                desc_queue: asyncio.Queue = asyncio.Queue()
                groups: dict[str, deque] = {}
                for item in all_desc_info:
                    tenant_key = f"{item[2]}.{item[3]}"
                    groups.setdefault(tenant_key, deque()).append(item)
                while groups:
                    for tenant_key in list(groups):
                        group = groups[tenant_key]
                        desc_queue.put_nowait(group.popleft())
                        if not group:
                            del groups[tenant_key]

                num_workers = min(MAX_CONCURRENT_DESCS, len(all_desc_info))
                detail_done = 0
                detail_started = time.monotonic()
                last_detail_log = detail_started
                old_after_detail: set[str] = set()

                async def desc_worker():
                    nonlocal detail_done, last_detail_log
                    while True:
                        try:
                            item = desc_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        try:
                            job, ext, co, inst, site = item
                            detail_url = f"https://{co}.{inst}.myworkdayjobs.com/wday/cxs/{co}/{site}{ext}"
                            tenant_key = f"{co}.{inst}"
                            tenant_sem = self._tenant_desc_semaphores.setdefault(
                                tenant_key, asyncio.Semaphore(PER_TENANT_DESC_LIMIT)
                            )
                            async with tenant_sem:
                                data = await self._get(client, detail_url)
                            if data:
                                posting_info = data.get("jobPostingInfo") or {}
                                raw_html = posting_info.get("jobDescription", "")
                                if raw_html:
                                    desc = strip_html(raw_html)
                                    job["description"] = desc
                                    job['description_raw'] = raw_html
                                    job['structured_fields'] = {
                                        'job_type': posting_info.get('timeType'),
                                        'work_mode': posting_info.get('remoteType'),
                                    }
                                    job["skills"] = extract_skills(desc)
                                    job.update(enrich_raw_job(job))
                                    self.stats["detail_descriptions"] += 1
                                start_date = posting_info.get("startDate")
                                if start_date:
                                    job["posted_at"] = start_date
                                    try:
                                        pd = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                                        if pd.tzinfo is None:
                                            pd = pd.replace(tzinfo=timezone.utc)
                                        job["created_at"] = pd.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                                        max_age = filters.get("max_age")
                                        if max_age and datetime.now(timezone.utc) - pd.astimezone(timezone.utc) > max_age:
                                            old_after_detail.add(job.get("id", ""))
                                            self.stats["detail_old_postings"] += 1
                                    except (ValueError, AttributeError):
                                        job["created_at"] = start_date
                                    self.stats["detail_dates"] += 1
                        except Exception as exc:
                            self.stats[f"detail_processing_{type(exc).__name__}"] += 1
                        finally:
                            detail_done += 1
                            now = time.monotonic()
                            if detail_done % 1000 == 0 or detail_done == len(all_desc_info) or now - last_detail_log >= 10:
                                elapsed = max(now - detail_started, 0.001)
                                rate = detail_done / elapsed
                                eta = (len(all_desc_info) - detail_done) / rate if rate else 0
                                log.info(
                                    "  details %d/%d | ok=%d drops=%d | %.1f/s | ETA ~%.1fm",
                                    detail_done, len(all_desc_info),
                                    self.stats['detail_pages_ok'], self.stats['detail_pages_dropped'],
                                    rate, eta/60
                                )
                                last_detail_log = now
                            desc_queue.task_done()

                workers = [asyncio.create_task(desc_worker()) for _ in range(num_workers)]
                await asyncio.gather(*workers)
                log.info("Descriptions fetched.")

                if old_after_detail:
                    all_jobs = [job for job in all_jobs if job.get("id", "") not in old_after_detail]

            if write_queue is not None and all_jobs:
                for i in range(0, len(all_jobs), SUPABASE_BATCH_SIZE):
                    await write_queue.put(all_jobs[i:i + SUPABASE_BATCH_SIZE])

            if missing_companies:
                out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workday_404_companies.txt")
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

        log.info("Workday request stats: %s", dict(self.stats))

        if all_jobs and LOCAL_OUTPUT:
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_workday_api_jobs.json")
            with open(out, "w", encoding="utf-8") as f:
                f.write(JSON_DUMPS(all_jobs))
            log.info("Saved %d jobs → %s", len(all_jobs), out)

        return all_jobs


def load_companies() -> list[dict[str, str]]:
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workday.csv")
    if not os.path.exists(fp):
        log.error("workday.csv not found at %s", fp); return []
    out = []
    with open(fp, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("name") and row.get("url"):
                out.append({"name": row["name"], "url": row["url"]})
    return out


async def main():
    scrape_start = datetime.now(timezone.utc).isoformat()

    log.info("=" * 60)
    log.info("Workday Scraper  –  ULTRA FAST  (+descriptions)")
    log.info("Concurrency : %d companies | %d pages/company", MAX_CONCURRENT_COMPANIES, MAX_PAGES_PER_COMPANY)
    log.info("Desc workers: %d parallel", MAX_CONCURRENT_DESCS)
    log.info("Page size   : %d", PAGE_LIMIT)
    log.info("Retries     : %d", RETRY_COUNT)
    log.info("Skip Supa   : %s", SKIP_SUPABASE)
    log.info("=" * 60)

    companies = load_companies()
    if not companies:
        log.error("No companies — exiting."); return
    log.info("Loaded %d companies", len(companies))

    writer = SupabaseWriter()
    await writer.init()

    write_queue: asyncio.Queue = asyncio.Queue()
    all_jobs:    list[dict]    = []

    async def bg_writer():
        buf: list[dict] = []
        while True:
            batch = await write_queue.get()
            if batch is None:
                if buf: await writer.upsert_jobs(buf)
                write_queue.task_done(); break
            buf.extend(batch)
            write_queue.task_done()
            if len(buf) >= SUPABASE_BATCH_SIZE:
                await writer.upsert_jobs(buf); buf = []

    writer_task = asyncio.create_task(bg_writer())

    scraper  = DirectWorkdayScraper(companies)
    all_jobs = await scraper.scrape("", "global", write_queue)

    # Flush remaining jobs (descriptions may have enriched them — re-upsert)
    if all_jobs:
        await writer.upsert_jobs(all_jobs)

    await write_queue.put(None)
    await writer_task

    company_rows = list({c["name"]: {
        "slug": c["name"], "source_board": "Workday",
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    } for c in companies}.values())
    await writer.upsert_companies(company_rows)

    log.info("=" * 60)
    log.info("Scrape complete — %d unique jobs (with descriptions)", len(all_jobs))
    log.info("=" * 60)

    if all_jobs and LOCAL_OUTPUT:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_workday_api_jobs.json")
        log.info("Saved %d jobs → %s", len(all_jobs), out)

    log.info("Running expiration cleanup …")
    await writer.expire_stale(scrape_start)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user.")
