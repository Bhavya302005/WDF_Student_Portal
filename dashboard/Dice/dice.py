#!/usr/bin/env python3
"""
dice.py — Dice.com Job Scraper (RSC / HTML-based)

Scrapes Dice.com search results by parsing React Server Component (RSC)
payloads embedded in the HTML.  No API keys needed — just plain HTTP GETs.

Strategy:
  1. Hit the search URL page-by-page (&page=1…N).  Each page embeds ~20 jobs
     as JSON in RSC chunks inside <script> tags.
  2. For each job, fetch the detail page and extract the full description
     from the embedded JSON-LD (schema.org JobPosting) blob.
  3. Enrich every record with salary / skills / experience parsing via
     scraper_utils, then push to Supabase.

Usage:
    python3 dice.py

Env vars (optional):
    DICE_CONCURRENCY        – parallel page fetches       (default 10)
    DICE_DESC_CONCURRENCY   – parallel description fetches (default 50)
    DICE_TIMEOUT            – request timeout in seconds   (default 15)
    SKIP_SUPABASE           – set "true" to skip DB writes
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
import html as html_mod

import httpx
from dotenv import load_dotenv
from supabase import acreate_client

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

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------
load_dotenv()
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from scraper_utils import enrich_raw_job, extract_skills
    from job_extraction import database_rows
except ImportError:
    def enrich_raw_job(job: dict) -> dict: return job
    def extract_skills(text: str) -> str: return ""


def format_iso_time(ts: str | None) -> str | None:
    if not ts: return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return None


def clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    if not text:
        return ""
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE      = "jobs"
SUPABASE_BATCH_SIZE = int(os.getenv("SUPABASE_BATCH_SIZE", "500"))
SKIP_SUPABASE       = os.getenv("SKIP_SUPABASE", "true").lower() == "true"

CONCURRENCY         = int(os.getenv("DICE_CONCURRENCY", "10"))
DESC_CONCURRENCY    = int(os.getenv("DICE_DESC_CONCURRENCY", "50"))
REQUEST_TIMEOUT     = float(os.getenv("DICE_TIMEOUT", "15"))
RETRY_COUNT         = 3

# Pipeline-compatible post_time integration
SEARCH_ATTRIBUTES: dict = {"post_time": "week"}

_POST_TIME_MAP = {
    "hour":  "ONE",    # closest Dice has is 1 day
    "day":   "ONE",    # last 24h
    "week":  "SEVEN",  # last 7 days
    "month": "THIRTY", # last 30 days
    "year":  "THIRTY", # Dice max is 30 days
    "any":   None,     # no date filter
}

BASE_SEARCH_URL     = "https://www.dice.com/jobs"

# ┌──────────────────────────────────────────────────────────────────────┐
# │  EDIT YOUR FILTERS BELOW                                           │
# │                                                                    │
# │  • To DISABLE a filter → comment out or delete its line.           │
# │  • To combine values   → separate with |  (pipe character).        │
# │  • Never set a filter to "false" — just remove the line instead.   │
# └──────────────────────────────────────────────────────────────────────┘
DEFAULT_FILTERS = {
    # ── Search Keyword & Location ────────────────────────────────────
    #   Left blank so this board pulls the full unfiltered set of listings,
    #   consistent with every other Type A board in pipeline.py (role
    #   labeling happens later via role_config.classify_role, not here).
    #   Set "q" to narrow by keyword, or "location"/"countryCode" to
    #   restrict geography, same as before.
    "q": "crm+consultant",

    #   Location settings
    # "location": "United+states",
    # "latitude": "38.7945952",
    # "longitude": "-106.5348379",
    # "countryCode": "US",
    # "locationPrecision": "Country",

    # ── Posted date ──────────────────────────────────────────────────
    #   Options: ONE (last 24h), THREE (3 days), SEVEN (7 days),
    #            THIRTY (30 days)   |  remove line = all time
    #   This is overridden at runtime by SEARCH_ATTRIBUTES["post_time"]
    #   via the pipeline's --post-time / --backfill-month flags.
    "filters.postedDate": "ONE",

    # ── Employment type ──────────────────────────────────────────────
    #   Options: FULLTIME, PARTTIME, CONTRACTS, THIRD_PARTY
    #   Combine with |, e.g. "FULLTIME|PARTTIME"

    # "filters.employmentType":  "FULLTIME|PARTTIME|CONTRACTS|THIRD_PARTY",
    # "filters.employmentType":  "FULLTIME|PARTTIME|CONTRACTS|THIRD_PARTY",
    "filters.employmentType":  "CONTRACTS",

    # ── Employer type ────────────────────────────────────────────────
    #   Options: Direct Hire, Recruiter, Other
    "filters.employerType":    "Direct Hire|Recruiter|Other",

    # ── Workplace type ───────────────────────────────────────────────
    #   Options: On-Site, Hybrid, Remote
    "filters.workplaceTypes":  "Remote",

    # ── Willing to sponsor visa ──────────────────────────────────────
    #   Set to "true" to only show sponsoring jobs.
    #   Comment out the line to include all jobs.
    "filters.willingToSponsor": "false",

    # ── Easy Apply only ──────────────────────────────────────────────
    #   Set to "true" to only show Easy Apply jobs.
    #   Comment out the line to include all jobs.
    "filters.easyApply":     "false",
    
    # ── Max Pages ────────────────────────────────────────────────────
    #   Limit how many pages to fetch (useful for broad searches).
    #   Comment out or set to a high number to fetch all available.
    "maxPages": "3",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Supabase Writer  (same pattern as join_com_api_v2.py)
# ---------------------------------------------------------------------------
class SupabaseWriter:
    def __init__(self):
        self.client = None

    async def init(self):
        if SKIP_SUPABASE or not SUPABASE_URL or not SUPABASE_KEY:
            return
        self.client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

    async def upsert_jobs(self, rows: list[dict]):
        if not self.client:
            return
        seen = set()
        deduped = []
        for r in rows:
            rid = r.get("id")
            if rid not in seen:
                seen.add(rid)
                deduped.append(r)
        for i in range(0, len(deduped), SUPABASE_BATCH_SIZE):
            chunk = deduped[i:i + SUPABASE_BATCH_SIZE]
            try:
                await self.client.table(SUPABASE_TABLE).upsert(
                    database_rows(chunk), on_conflict="id"
                ).execute()
            except Exception as e:
                print(f"[WARN] job upsert failed: {e}")

# ---------------------------------------------------------------------------
# RSC Payload Parser
# ---------------------------------------------------------------------------

_RSC_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[(.*?)\]\)', re.DOTALL)


def _unescape(text: str) -> str:
    """Unescape the double-escaped JSON inside RSC chunks."""
    return text.replace('\\"', '"')


def extract_jobs_from_search_html(html: str) -> tuple[list[dict], int, int]:
    """
    Parse the RSC chunks from a Dice search page.
    Returns: (list_of_job_dicts, total_page_count, total_results_count)
    """
    chunks = _RSC_CHUNK_RE.findall(html)
    jobs: list[dict] = []
    page_count = 1
    total_results = 0

    for chunk in chunks:
        c = _unescape(chunk)

        # Check if this chunk contains job data  (has jobList and data array)
        if '"data":[{' not in c or '"companyName"' not in c:
            continue

        # Extract page count
        pc_match = re.search(r'"pageCount":(\d+)', c)
        if pc_match:
            page_count = int(pc_match.group(1))

        # Extract total results
        tr_match = re.search(r'"totalJobCount":(\d+)', c)
        if tr_match:
            total_results = int(tr_match.group(1))

        # Extract individual job objects from the data array
        # Find the start of the data array
        data_start = c.find('"data":[{')
        if data_start < 0:
            continue

        arr_start = c.index('[', data_start)

        # Walk through to find the matching close bracket
        depth = 0
        arr_end = arr_start
        for j in range(arr_start, len(c)):
            ch = c[j]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    arr_end = j + 1
                    break

        arr_str = c[arr_start:arr_end]

        try:
            parsed = JSON_LOADS(arr_str)
            if isinstance(parsed, list):
                jobs.extend(parsed)
        except Exception:
            # Fallback: extract individual job objects via regex
            for m in re.finditer(
                r'\{"id":"[a-f0-9]{32}".*?"companyProfileId":"[^"]*"\}', c
            ):
                try:
                    jobs.append(JSON_LOADS(m.group(0)))
                except Exception:
                    pass

    return jobs, page_count, total_results


def extract_description_from_detail_html(html: str) -> str:
    """
    Extract the full job description from a Dice job detail page.
    Looks for the JSON-LD (schema.org JobPosting) blob first,
    then falls back to the largest HTML content chunk.
    """
    chunks = _RSC_CHUNK_RE.findall(html)

    # Strategy 1: JSON-LD schema.org
    for chunk in chunks:
        c = _unescape(chunk)
        if '"@type"' in c and '"JobPosting"' in c:
            json_start = c.find('{"')
            if json_start < 0:
                json_start = c.find('{\\n')
            if json_start >= 0:
                raw = c[json_start:]
                raw = raw.replace('\\n', '\n').replace('\\t', '\t')
                raw = raw.rstrip().rstrip('"').rstrip()
                try:
                    data = json.loads(raw)
                    desc = data.get("description", "")
                    if desc:
                        # Dice's RSC payload escapes '<'/'>'/'&' inside the
                        # JSON-LD description a second time (Next.js's
                        # script-safe JSON serializer), so json.loads() alone
                        # leaves literal "<..." text behind instead of
                        # real tags for clean_html() to strip.
                        desc = (desc.replace("\\u003c", "<")
                                    .replace("\\u003e", ">")
                                    .replace("\\u0026", "&"))
                        return clean_html(desc)
                except Exception:
                    pass

    # Strategy 2: Find the largest HTML content chunk (the description body)
    for chunk in chunks:
        c = _unescape(chunk)
        if '\\u003cp\\u003e' in c or '<p>' in c:
            html_content = c
            try:
                html_content = html_content.encode().decode('unicode_escape')
            except Exception:
                pass
            cleaned = clean_html(html_content)
            if len(cleaned) > 200:
                return cleaned[:25000]

    return ""


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
class DiceScraper:
    def __init__(self, filters: dict[str, str] | None = None):
        self.filters = dict(filters or DEFAULT_FILTERS)
        
        # Extract maxPages so it doesn't get sent as a URL parameter
        self.max_pages = int(self.filters.pop("maxPages", "999999"))
        
        # Apply current SEARCH_ATTRIBUTES post_time → Dice filter value
        dice_date = _POST_TIME_MAP.get(SEARCH_ATTRIBUTES.get("post_time", "day"))
        if dice_date is None:
            self.filters.pop("filters.postedDate", None)  # "any" = no filter
        else:
            self.filters["filters.postedDate"] = dice_date

    def _build_url(self, page: int) -> str:
        """Build the search URL for a given page number."""
        # Ignore filters that are empty or explicitly set to "false"
        active_filters = {
            k: v for k, v in self.filters.items() 
            if v and str(v).lower() != "false"
        }
        params = "&".join(f"{k}={v}" for k, v in active_filters.items())
        return f"{BASE_SEARCH_URL}?{params}&page={page}"

    async def _get(
        self, client: httpx.AsyncClient, url: str, timeout: float | None = None
    ) -> tuple[int, str]:
        """HTTP GET with retries.  Returns (status_code, body)."""
        tout = timeout or REQUEST_TIMEOUT
        for attempt in range(RETRY_COUNT):
            try:
                r = await client.get(url, timeout=tout, headers=HEADERS)
                print(f"[{r.status_code}] GET {url}")
                if r.status_code == 200:
                    return r.status_code, r.text
                if r.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    print(f"[RATE-LIMITED] Waiting {wait}s …")
                    await asyncio.sleep(wait)
                    continue
                return r.status_code, ""
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                print(f"[ERR] GET {url} - {type(e).__name__}")
                if attempt < RETRY_COUNT - 1:
                    await asyncio.sleep(1 * (attempt + 1))
        return 0, ""

    async def _fetch_page(
        self, client: httpx.AsyncClient, page: int
    ) -> list[dict]:
        """Fetch a single search result page and extract job records."""
        url = self._build_url(page)
        status, body = await self._get(client, url)
        if status != 200 or not body:
            return []
        jobs, _, _ = extract_jobs_from_search_html(body)
        return jobs

    async def _fetch_description(
        self, client: httpx.AsyncClient, detail_url: str
    ) -> str:
        """Fetch a job detail page and extract the full description."""
        _, body = await self._get(client, detail_url, timeout=REQUEST_TIMEOUT)
        if not body:
            return ""
        return extract_description_from_detail_html(body)

    async def scrape(self, write_queue: asyncio.Queue | None = None) -> list[dict]:
        """Main scrape loop.  Returns all scraped jobs."""
        now_str = format_iso_time(datetime.now(timezone.utc).isoformat())

        limits = httpx.Limits(
            max_connections=CONCURRENCY + DESC_CONCURRENCY + 10,
            max_keepalive_connections=CONCURRENCY + DESC_CONCURRENCY,
        )
        async with httpx.AsyncClient(
            limits=limits,
            follow_redirects=True,
            http2=True,
        ) as client:

            # --- Phase 1: Discover total pages from page 1 ---
            url1 = self._build_url(1)
            status, body = await self._get(client, url1)
            if status != 200 or not body:
                print("[FATAL] Could not fetch page 1")
                return []

            first_page_jobs, page_count, total_results = \
                extract_jobs_from_search_html(body)
                
            page_count = min(page_count, self.max_pages)
            
            print(f"\nTotal results: {total_results}  |  Pages: {page_count}")
            print(f"Jobs on page 1: {len(first_page_jobs)}")

            all_raw: list[dict] = list(first_page_jobs)
            seen_ids: set[str] = {j.get("id", "") for j in all_raw}

            # --- Phase 2: Fetch remaining pages concurrently ---
            if page_count > 1:
                remaining_pages = list(range(2, page_count + 1))
                page_sem = asyncio.Semaphore(CONCURRENCY)
                done_pages = 1

                async def fetch_page_bounded(pg: int):
                    nonlocal done_pages
                    async with page_sem:
                        jobs = await self._fetch_page(client, pg)
                        done_pages += 1
                        if done_pages % 10 == 0 or done_pages == page_count:
                            print(f"  [{done_pages}/{page_count}] pages scraped …")
                        return jobs

                tasks = [fetch_page_bounded(p) for p in remaining_pages]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        continue
                    for job in result:
                        jid = job.get("id", "")
                        if jid and jid not in seen_ids:
                            seen_ids.add(jid)
                            all_raw.append(job)

            print(f"\n[done] {len(all_raw)} unique jobs collected from {page_count} pages")

            # --- Phase 3: Normalize into our standard schema ---
            all_jobs: list[dict] = []
            desc_tasks: list[tuple[dict, str]] = []  # (enriched_job, detail_url)

            for raw in all_raw:
                loc_obj = raw.get("jobLocation") or {}
                location = loc_obj.get("displayName", "")
                title = raw.get("title", "").strip()
                company = raw.get("companyName", "").strip()
                job_id = raw.get("id", "")
                guid = raw.get("guid", "")
                detail_url = raw.get("detailsPageUrl", "")
                posted = raw.get("postedDate", "")
                salary_text = raw.get("salary", "")
                emp_type = raw.get("employmentType", "")
                is_remote = raw.get("isRemote", False)
                workplace = raw.get("workplaceTypes", [])
                summary = raw.get("summary", "")

                dedupe_key = hashlib.md5(
                    f"dice:{company}:{job_id}".encode()
                ).hexdigest()

                work_mode = "Remote" if is_remote else ""
                if not work_mode and workplace:
                    if "Hybrid" in workplace:
                        work_mode = "Hybrid"
                    elif "On-Site" in workplace:
                        work_mode = "On-site"

                enriched = enrich_raw_job({
                    "id":            dedupe_key,
                    "job_title":     title,
                    "company":       company,
                    "location":      location,
                    "job_url":       detail_url,
                    "apply_url":     detail_url,
                    "description":   clean_html(summary),
                    "salary":        salary_text,
                    "skills":        extract_skills(title + " " + summary),
                    "structured_fields": {"work_mode": work_mode, "job_type": emp_type},
                    "work_mode":     work_mode,
                    "source_board":  "Dice",
                    "scraper_type":  "api",
                    "is_remote":     is_remote,
                    "job_type":      emp_type or "FULL_TIME",
                    "scraped_at":    now_str,
                    "created_at":    format_iso_time(posted) or None,  # None = honest: no date from listing API
                    "also_on":       [],
                })

                all_jobs.append(enriched)
                if detail_url:
                    desc_tasks.append((enriched, detail_url))

            # --- Phase 4: Fetch full descriptions concurrently ---
            if desc_tasks:
                total_descs = len(desc_tasks)
                print(f"\nFetching descriptions for {total_descs} jobs …")
                desc_queue: asyncio.Queue = asyncio.Queue()
                for item in desc_tasks:
                    await desc_queue.put(item)

                desc_done = 0

                async def desc_worker():
                    nonlocal desc_done
                    while True:
                        try:
                            item = desc_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        job, url = item
                        try:
                            desc = await self._fetch_description(client, url)
                            if desc:
                                job["description"] = desc
                                job["skills"] = extract_skills(desc)
                                job.update(enrich_raw_job(job))
                        except Exception:
                            pass
                        finally:
                            desc_queue.task_done()
                            desc_done += 1
                            if desc_done % 100 == 0 or desc_done == total_descs:
                                print(f"  [{desc_done}/{total_descs}] descriptions fetched …")

                workers = [
                    asyncio.create_task(desc_worker())
                    for _ in range(DESC_CONCURRENCY)
                ]
                await asyncio.gather(*workers)
                print("Descriptions fetched.")

            # --- Save local JSON ---
            if all_jobs:
                out_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "all_dice_jobs.json"
                )
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(JSON_DUMPS(all_jobs))
                print(f"Saved {len(all_jobs)} jobs → {out_path}")

            # --- Push to write_queue (pipeline handles the sentinel) ---
            if write_queue is not None:
                if all_jobs:
                    await write_queue.put(all_jobs)
                # NOTE: do NOT send None sentinel here — pipeline.py's
                # _run_api_board() already sends it in its finally block.

        return all_jobs


# ---------------------------------------------------------------------------
# Expiration cleanup  (same pattern as join_com scraper)
# ---------------------------------------------------------------------------
async def expire_stale_jobs():
    if SKIP_SUPABASE or not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await (
            client.table(SUPABASE_TABLE)
            .update({"is_active": False})
            .eq("source_board", "Dice")
            .lt("scraped_at", cutoff)
            .execute()
        )
        print("Successfully expired stale jobs.")
    except Exception as e:
        print(f"[WARN] expiration cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    scrape_start = datetime.now(timezone.utc).isoformat()

    print("=" * 60)
    print("Dice.com Scraper  –  RSC HTML Parser")
    print(f"Page concurrency  : {CONCURRENCY}")
    print(f"Desc concurrency  : {DESC_CONCURRENCY}")
    print(f"Skip Supabase     : {SKIP_SUPABASE}")
    print("=" * 60)

    if not SKIP_SUPABASE:
        writer = SupabaseWriter()
        await writer.init()

        write_queue: asyncio.Queue = asyncio.Queue()
        buf: list[dict] = []

        async def db_writer():
            while True:
                batch = await write_queue.get()
                if batch is None:
                    if buf:
                        await writer.upsert_jobs(buf)
                    write_queue.task_done()
                    break
                buf.extend(batch)
                write_queue.task_done()
                if len(buf) >= SUPABASE_BATCH_SIZE:
                    await writer.upsert_jobs(buf)
                    buf.clear()

        db_task = asyncio.create_task(db_writer())
        scraper = DiceScraper()
        all_jobs = await scraper.scrape(write_queue)
        await db_task
    else:
        scraper = DiceScraper()
        all_jobs = await scraper.scrape()

    print(f"\n{'=' * 60}")
    print(f"Scrape complete — {len(all_jobs)} unique jobs (with descriptions)")
    print("=" * 60)

    # Save to file
    if all_jobs:
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "all_dice_jobs.json"
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(JSON_DUMPS(all_jobs))
        print(f"Saved {len(all_jobs)} jobs → {out_path}")

    # Expiration cleanup
    if not SKIP_SUPABASE:
        print("\nRunning expiration cleanup …")
        await expire_stale_jobs()


if __name__ == "__main__":
    asyncio.run(main())
