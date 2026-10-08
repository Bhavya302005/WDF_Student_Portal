"""Workable public catalog scraper with paced cursor pagination.

Use the unfiltered cursor so jobs with missing or new workplace/employment
values are included. Descriptions, requirements and benefits are inline.
At the default 1.5–2 requests/s and 20 jobs/page, 55,000 jobs require at least
23–31 minutes of requests; latency, retries and filters can add time.
"""

import asyncio
from collections import Counter
import hashlib
import html as html_lib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from scraper_utils import enrich_raw_job
except ImportError:
    def enrich_raw_job(job: dict) -> dict:
        return job

try:
    from scraper_runtime import RequestGate, retry_after_seconds
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
        """Adaptive rate gate with hang prevention."""
        MAX_SLEEP = 60.0  # Never sleep longer than this

        def __init__(self, rate, min_rate, max_rate, cooldown=2.0, recovery=0.05):
            self.rate = max(min_rate, min(rate, max_rate))
            self.min_rate = max(min_rate, 0.5)  # Floor at 0.5 req/s
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
                delay = min(slot - now, self.MAX_SLEEP)  # Cap sleep
            if delay > 0:
                await asyncio.sleep(delay)

        async def on_throttle(self, retry_after=None):
            async with self._lock:
                self.rate = max(self.min_rate, self.rate * 0.7)
                pause = min(retry_after if retry_after is not None else self.cooldown, 30.0)
                now = asyncio.get_running_loop().time()
                self._paused_until = max(self._paused_until, now + pause)
                self._next_slot = max(self._next_slot, self._paused_until)

        async def on_429(self, retry_after=None):
            await self.on_throttle(retry_after)

        def on_success(self):
            self.rate = min(self.max_rate, self.rate + self.recovery)

# -- Config -------------------------------------------------------------------
BASE_URL         = "https://jobs.workable.com/api/v1/jobs"
PAGE_SIZE        = 20  # Workable rejects larger values.
CONCURRENT_PAGES = max(1, int(os.getenv("WORKABLE_STREAM_CONCURRENCY", "25")))
REQUEST_TIMEOUT  = float(os.getenv("WORKABLE_TIMEOUT", "20"))
RETRY_COUNT      = int(os.getenv("WORKABLE_RETRY_COUNT", "5"))
REQUEST_RATE     = float(os.getenv("WORKABLE_REQUEST_RATE", "2.0"))
MIN_REQUEST_RATE = float(os.getenv("WORKABLE_MIN_REQUEST_RATE", "0.5"))
MAX_REQUEST_RATE = float(os.getenv("WORKABLE_MAX_REQUEST_RATE", "2.0"))
RATE_LIMIT_SLEEP = float(os.getenv("WORKABLE_RATE_LIMIT_SLEEP", "10"))
COOLDOWN_BUFFER  = float(os.getenv("WORKABLE_COOLDOWN_BUFFER", "2"))
PROXY_URL        = os.getenv("WORKABLE_PROXY", "").strip() or None


SEARCH_ATTRIBUTES = {
    "post_time": os.getenv("WORKABLE_POST_TIME", "month"),
}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("workable_jobs_board")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://jobs.workable.com/",
}


# -- Helpers ------------------------------------------------------------------
def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def section_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(section_text(item) for item in value.values() if item)
    if isinstance(value, list):
        return " ".join(section_text(item) for item in value if item)
    return str(value) if value is not None else ""


def post_time_to_cutoff(post_time: str) -> datetime | None:
    mapping = {
        "hour":  timedelta(hours=1),
        "day":   timedelta(days=1),
        "week":  timedelta(weeks=1),
        "month": timedelta(days=30),
        "year":  timedelta(days=365),
    }
    delta = mapping.get((post_time or "any").lower().strip())
    return (datetime.now(timezone.utc) - delta) if delta else None


def normalize_workplace(wp: str) -> str:
    return {"on_site": "Onsite", "remote": "Remote", "hybrid": "Hybrid"}.get(wp, "")


def matches_extra_filters(raw: dict) -> bool:
    description = " ".join(
        section_text(raw.get(key, ""))
        for key in ("description", "requirementsSection", "benefitsSection")
    )
    location = raw.get("location") or {}
    text = " ".join(
        str(value) for value in (
            raw.get("title", ""),
            location.get("city", ""),
            location.get("subregion", ""),
            location.get("countryName", ""),
            description,
        )
    ).lower()

    work_mode = (SEARCH_ATTRIBUTES.get("work_mode", "") or "").lower().strip()
    job_type = (SEARCH_ATTRIBUTES.get("job_type", "") or "").lower().strip().replace("-", "_")
    experience = (SEARCH_ATTRIBUTES.get("experience", "") or "").lower().strip()
    skills = [s.strip().lower() for s in (SEARCH_ATTRIBUTES.get("skills", "") or "").split(",") if s.strip()]
    excluded = [s.strip().lower() for s in (SEARCH_ATTRIBUTES.get("excluded_words", "") or "").split(",") if s.strip()]

    if work_mode and work_mode not in normalize_workplace(raw.get("workplace", "")).lower():
        return False
    normalized_type = (raw.get("employmentType") or "").lower().replace("-", "_").replace(" ", "_")
    if job_type and job_type not in normalized_type:
        return False
    if experience and experience not in text:
        return False
    if skills and not all(skill in text for skill in skills):
        return False
    if excluded and any(word in text for word in excluded):
        return False
    return True


def normalize_job(raw: dict) -> dict:
    company_info = raw.get("company") or {}
    loc = raw.get("location") or {}

    city    = loc.get("city", "")
    region  = loc.get("subregion", "")
    country = loc.get("countryName", "")
    location = ", ".join(p for p in [city, region, country] if p)

    desc_html = " ".join(
        section_text(part) for part in (
            raw.get("description", ""),
            raw.get("requirementsSection", ""),
            raw.get("benefitsSection", ""),
        )
        if part
    )
    desc_plain = clean_html(desc_html)

    title   = (raw.get("title") or "").strip()
    job_id  = raw.get("id") or raw.get("url") or title
    dedupe  = hashlib.md5(f"workable:{job_id}".encode()).hexdigest()

    created_raw = raw.get("created") or ""
    try:
        created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        created_at = created_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        posted_at  = created_at
    except (ValueError, AttributeError):
        created_at = None
        posted_at = None

    workplace = normalize_workplace(raw.get("workplace", ""))

    return enrich_raw_job({
        "id":           dedupe,
        "job_title":    title,
        "company":      company_info.get("title", ""),
        "location":     location,
        "job_url":      raw.get("url", ""),
        "apply_url":    raw.get("url", ""),
        "description":  desc_plain,
        "description_raw": desc_html,
        "structured_fields": {"work_mode": workplace, "job_type": raw.get("employmentType")},
        "work_mode":    workplace,
        "is_remote":    workplace == "Remote",
        "job_type":     (raw.get("employmentType") or "").lower().replace("-", "_"),
        "source_board": "Workable",
        "scraper_type": "api",
        "scraped_at":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "created_at":   created_at,
        "posted_at":    posted_at,
        "also_on":      [],
        "salary":       "",
        "salary_min":   None,
        "salary_max":   None,
    })


# -- HTTP layer ----------------------------------------------------------------
class AdaptiveRequestGate(RequestGate):
    def __init__(self):
        super().__init__(REQUEST_RATE, MIN_REQUEST_RATE, MAX_REQUEST_RATE,
                         cooldown=RATE_LIMIT_SLEEP + COOLDOWN_BUFFER)


def _retry_after_seconds(response):
    return retry_after_seconds(response.headers.get("Retry-After"))


class IncompleteScrapeError(RuntimeError):
    """Do not publish a partial cursor crawl as a successful complete output."""


async def fetch_page(
    client: httpx.AsyncClient,
    params: dict,
    semaphore: asyncio.Semaphore,
    gate: AdaptiveRequestGate,
    stats: Counter,
    stop_event: asyncio.Event | None = None,
) -> dict | None:
    async with semaphore:
        for attempt in range(RETRY_COUNT):
            if stop_event is not None and stop_event.is_set():
                return None
            await gate.acquire()
            if stop_event is not None and stop_event.is_set():
                return None
            try:
                r = await client.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
                stats["requests"] += 1
                if r.status_code == 429:
                    stats["status_429"] += 1
                    retry_after = _retry_after_seconds(r)
                    if retry_after is not None and retry_after > 300:
                        log.error("Workable returned 429 with Retry-After=%ds (%.1f hours) — IP rate-limited. Aborting.",
                                  int(retry_after), retry_after / 3600)
                        if stop_event is not None:
                            stop_event.set()
                        return None
                    log.warning("429 on page fetch (Retry-After=%s) — attempt %d/%d",
                                retry_after, attempt + 1, RETRY_COUNT)
                    await gate.on_429(retry_after)
                    continue
                if r.status_code >= 500:
                    stats[f"status_{r.status_code}"] += 1
                    if attempt < RETRY_COUNT - 1:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    break
                if r.status_code != 200:
                    stats[f"status_{r.status_code}"] += 1
                    log.error("HTTP %d on page fetch", r.status_code)
                    break
                try:
                    data = r.json()
                except ValueError:
                    stats["invalid_json"] += 1
                    if attempt < RETRY_COUNT - 1:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    break
                if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                    stats["invalid_schema"] += 1
                    break
                gate.on_success()
                stats["pages_ok"] += 1
                return data
            except httpx.TransportError as e:
                stats[type(e).__name__] += 1
                log.warning("Transport error: %s -- retry %d", e, attempt + 1)
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        stats["pages_dropped"] += 1
        return None


# -- Per-workplace stream ------------------------------------------------------
async def _scrape_stream(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    workplace: str,
    employment_type: str,
    base_params: dict,
    seen_ids: set,
    seen_lock: asyncio.Lock,
    result_queue: asyncio.Queue,
    gate: AdaptiveRequestGate,
    stats: Counter,
    stop_event: asyncio.Event,
    max_jobs: int,
    cutoff: datetime | None,
) -> int:
    """Crawls one workplace cursor chain and pushes normalised jobs to result_queue."""
    stream_name = f"{workplace}/{employment_type}" if workplace or employment_type else "catalog"
    stream_params = {
        **base_params,
        **({"workplace": workplace} if workplace else {}),
        **({"employment_type": employment_type} if employment_type else {}),
    }

    if stop_event.is_set():
        return 0
    first = await fetch_page(client, stream_params, semaphore, gate, stats, stop_event)
    if first is None:
        if stop_event.is_set():
            return 0
        log.error("Workable first page failed for %s — cannot scrape this stream", stream_name)
        stats['first_page_failures'] += 1
        # Returning 0 here would be indistinguishable from "this catalog is
        # empty". A board that 403s or times out on every request would look
        # perfectly healthy while silently producing no jobs, so fail loudly.
        raise IncompleteScrapeError(f"Workable first page failed: {stream_name}")

    total       = int(first.get("totalSize", 0))
    stats["partition_total"] += total
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    log.info("Stream %-24s total=%d (~%d pages)", stream_name, total, total_pages)

    collected = 0
    page_num  = 1

    async def process(raw_jobs: list[dict]) -> None:
        nonlocal collected
        for raw in raw_jobs:
            if stop_event.is_set():
                return
            if not matches_extra_filters(raw):
                continue
            if cutoff:
                try:
                    created = datetime.fromisoformat((raw.get("created") or "").replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < cutoff:
                        continue
                except (ValueError, AttributeError):
                    pass
            jid = raw.get("id") or raw.get("url")
            if not jid or not raw.get("title") or not raw.get("url"):
                stats["invalid_jobs"] += 1
                continue
            async with seen_lock:
                if jid in seen_ids:
                    continue
                if max_jobs > 0 and len(seen_ids) >= max_jobs:
                    stop_event.set()
                    return
                seen_ids.add(jid)
            # Extraction can be expensive for long JDs. Keep the event loop
            # available for the next cursor request and other pipeline boards.
            await result_queue.put(await asyncio.to_thread(normalize_job, raw))
            collected += 1
            if max_jobs > 0 and len(seen_ids) >= max_jobs:
                stop_event.set()

    raw_count = 0
    page_data = first
    tokens_seen = set()
    last_log = asyncio.get_running_loop().time()
    while not stop_event.is_set():
        raw_count += len(page_data["jobs"])
        next_token = page_data.get("nextPageToken")
        # At most one page ahead, through the same rate limiter. Avoid an
        # extra request when this page could satisfy the requested job limit.
        prefetch = None
        can_prefetch = not max_jobs or len(seen_ids) + len(page_data['jobs']) < max_jobs
        if next_token and next_token not in tokens_seen and can_prefetch:
            prefetch = asyncio.create_task(fetch_page(
                client, {**stream_params, 'pageToken': next_token}, semaphore, gate, stats, stop_event))
        try:
            await process(page_data['jobs'])
            if stop_event.is_set() or not next_token:
                break
            if next_token in tokens_seen:
                stats['repeated_cursors'] += 1
                raise IncompleteScrapeError(f'Workable repeated cursor: {stream_name}')
            tokens_seen.add(next_token)
            page_data = await prefetch if prefetch is not None else await fetch_page(
                client, {**stream_params, 'pageToken': next_token}, semaphore, gate, stats, stop_event)
            if page_data is None:
                log.warning('Workable page %d failed for %s — returning partial results (%d jobs)',
                            page_num + 1, stream_name, collected)
                stats['partial_scrapes'] += 1
                break
        finally:
            if prefetch is not None:
                if not prefetch.done():
                    prefetch.cancel()
                await asyncio.gather(prefetch, return_exceptions=True)
        page_num += 1
        now = asyncio.get_running_loop().time()
        # Compare against the last time we actually logged. Resetting the
        # timestamp before the comparison (as this did) made the elapsed check
        # always 0, so only the every-100-pages branch ever fired.
        if page_num % 100 == 0 or now - last_log >= 15:
            last_log = now
            pct = page_num * 100 // total_pages if total_pages else 0
            log.info("  Stream %-24s page %d/%d (%d%%) jobs=%d",
                     stream_name, page_num, total_pages, pct, collected)

    if not stop_event.is_set() and raw_count < total:
        gap = total - raw_count
        tolerance = max(10, int(total * 0.01))  # allow 1% or 10 jobs shortfall
        if gap > tolerance:
            stats["truncated_streams"] += 1
            # The cursor chain stopped well short of the advertised totalSize,
            # so this result set is known-incomplete. Surface it instead of
            # letting a truncated crawl masquerade as a full one.
            raise IncompleteScrapeError(
                f"Workable cursor ended after {raw_count}/{total} catalog jobs "
                f"(gap={gap} > tolerance={tolerance}): {stream_name}")
        else:
            log.warning("Stream %-24s cursor ended %d/%d (within tolerance)", stream_name, raw_count, total)
    stats["raw_jobs_seen"] += raw_count
    log.info("Stream %-24s DONE collected=%d", stream_name, collected)
    return collected


# -- Main scraper -------------------------------------------------------------
class WorkableJobsBoardScraper:

    async def scrape(
        self,
        job_profile: str = "",
        location: str = "",
        max_jobs: int = 0,
        upsert_fn=None,
        upsert_batch_size: int = 500,
        write_queue: asyncio.Queue | None = None,
    ) -> list[dict]:

        post_time = SEARCH_ATTRIBUTES.get("post_time", "month")

        base_params: dict[str, Any] = {"limit": PAGE_SIZE}
        if job_profile:
            base_params["query"] = job_profile
        if location:
            base_params["location"] = location
        if post_time == "month":
            base_params["day_range"] = 30
        elif post_time == "week":
            base_params["day_range"] = 7
        elif post_time == "day":
            base_params["day_range"] = 1
        elif post_time == "hour":
            base_params["day_range"] = 1
        elif post_time == "year":
            base_params["day_range"] = 365
        # "any" = no day_range param

        cutoff = post_time_to_cutoff(post_time)

        seen_ids: set         = set()
        seen_lock             = asyncio.Lock()
        result_queue          = asyncio.Queue()
        all_jobs: list[dict]  = []
        upsert_buffer: list[dict] = []
        stream_buffer: list[dict] = []
        upload_total = 0
        stats: Counter = Counter()
        self.stats = stats
        gate = AdaptiveRequestGate()
        stop_event = asyncio.Event()

        # Consumer: drain queue -> buffer -> upsert
        async def consumer():
            nonlocal upload_total
            while True:
                job = await result_queue.get()
                if job is None:      # sentinel
                    result_queue.task_done()
                    break
                all_jobs.append(job)
                if write_queue is not None:
                    stream_buffer.append(job)
                    if len(stream_buffer) >= upsert_batch_size:
                        await write_queue.put(stream_buffer[:])
                        stream_buffer.clear()
                if upsert_fn:
                    upsert_buffer.append(job)
                    if len(upsert_buffer) >= upsert_batch_size:
                        batch = upsert_buffer[:]
                        upsert_buffer.clear()
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, upsert_fn, batch)
                        upload_total += len(batch)
                        log.info("  Streamed %d jobs to Supabase (total=%d)",
                                 len(batch), upload_total)
                result_queue.task_done()

        consumer_task = asyncio.create_task(consumer())

        semaphore = asyncio.Semaphore(CONCURRENT_PAGES)
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, follow_redirects=True,
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=8.0, pool=5.0),
                limits=httpx.Limits(max_connections=CONCURRENT_PAGES + 10, max_keepalive_connections=CONCURRENT_PAGES),
                proxy=PROXY_URL,
            ) as client:
                if PROXY_URL:
                    log.info("Using proxy: %s", PROXY_URL.split("@")[-1] if "@" in PROXY_URL else PROXY_URL)
                await _scrape_stream(client, semaphore, '', '', base_params,
                    seen_ids, seen_lock, result_queue, gate, stats, stop_event, max_jobs, cutoff)
        except BaseException:
            consumer_task.cancel()
            await asyncio.gather(consumer_task, return_exceptions=True)
            raise


        # Signal consumer to stop and wait
        await result_queue.put(None)
        await consumer_task

        if write_queue is not None and stream_buffer:
            await write_queue.put(stream_buffer[:])

        # Flush remaining upsert buffer
        if upsert_fn and upsert_buffer:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, upsert_fn, upsert_buffer)
            upload_total += len(upsert_buffer)
            log.info("  Final flush: %d jobs (grand total=%d)", len(upsert_buffer), upload_total)

        log.info("Total unique jobs collected: %d", len(all_jobs))
        log.info("Workable HTTP stats: %s", dict(stats))
        return all_jobs


# -- Module-level entry-point (used by run_board.py) --------------------------
async def scrape(job_profile: str = "", location: str = "", max_jobs: int = 0) -> list[dict]:
    s = WorkableJobsBoardScraper()
    return await s.scrape(job_profile, location, max_jobs)


# -- Standalone CLI -----------------------------------------------------------
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Workable parallel scraper (jobs.workable.com)")
    parser.add_argument("--post-time", default="month",
                        choices=["any", "hour", "day", "week", "month", "year"])
    parser.add_argument("--query",    default="", help="Keyword filter")
    parser.add_argument("--location", default="", help="Location filter")
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--out",      default="all_workable_jobs.json")
    parser.add_argument("--upload",   action="store_true",
                        help="Stream-upload to Supabase while scraping")
    args = parser.parse_args()

    SEARCH_ATTRIBUTES["post_time"] = args.post_time

    upsert_fn = None
    if args.upload:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from pipeline import upsert_to_supabase
        upsert_fn = upsert_to_supabase
        log.info("Streaming upload to Supabase ENABLED.")

    t0   = datetime.now()
    jobs = asyncio.run(
        WorkableJobsBoardScraper().scrape(
            job_profile=args.query,
            location=args.location,
            max_jobs=args.max_jobs,
            upsert_fn=upsert_fn,
        )
    )
    elapsed = (datetime.now() - t0).total_seconds()

    print(f"\n  Collected {len(jobs):,} unique Workable jobs in {elapsed:.1f}s "
          f"({len(jobs)/max(elapsed, 1):.0f} jobs/sec)")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
    print(f"Saved -> {out_path}")
