import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
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

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "600"))  # 600 — centralized API, server response time is bottleneck
REQUEST_TIMEOUT = float(os.getenv("ASHBY_TIMEOUT", "15"))           # 15s — faster fail for dead boards
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "2"))                     # 2 retries
BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN = int(os.getenv("BREAKER_COOLDOWN", "60"))
SUPABASE_BATCH_SIZE = int(os.getenv("SUPABASE_BATCH_SIZE", "500"))

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

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("greenhouse_scraper")

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

    job_location = (job.get("location") or {}).get("name") or ""
    job_location = job_location.strip()
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
    published_at = job.get("updated_at")
    if max_age and published_at:
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if now_utc - published > max_age:
                return False
        except ValueError:
            pass

    return True


def format_iso_time(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def normalize_job(job: dict[str, Any], company_slug: str) -> dict[str, Any]:
    title = (job.get("title") or "").strip()
    location = (job.get("location", {}).get("name") or "").strip()
    desc_text = clean_html_text(job.get("content", "")).strip()

    job_id = str(job.get("id") or title)
    raw_key = f"greenhouse:{company_slug}:{job_id}"
    dedupe_key = hashlib.md5(raw_key.encode()).hexdigest()

    # Greenhouse's API doesn't expose salary/work-mode/experience/skills as
    # structured fields, so those are left for enrich_raw_job() below to
    # derive from the description via scraper_utils' shared extraction.
    return enrich_raw_job({
        "id": dedupe_key,
        "job_title": title,
        "company": normalize_company_name(company_slug),
        "location": location,
        "job_url": job.get("absolute_url") or "",
        "apply_url": job.get("absolute_url") or "",
        "description": desc_text,
        "description_raw": job.get("content") or desc_text,
        "source_board": "Greenhouse",
        "scraper_type": "api",
        "job_type": "full_time",
        "scraped_at": format_iso_time(datetime.now(timezone.utc).isoformat()),
        "created_at": format_iso_time(job.get("updated_at")) or format_iso_time(datetime.now(timezone.utc).isoformat()),
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
    is_404: bool = False


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


def to_greenhouse_slug(name: str) -> str:
    """Convert a company display name to a Greenhouse API slug.
    Greenhouse slugs are lowercase alphanumeric strings (hyphens allowed).
    This strips spaces, special characters, and lowercases the name."""
    slug = name.lower()
    # Remove anything that isn't alphanumeric or a hyphen
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    # Collapse multiple hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


async def fetch_json_with_retry(client: httpx.AsyncClient, url: str, retries: int = RETRY_COUNT) -> dict[str, Any] | None:
    base_delay = 0.5  # Faster first retry

    for attempt in range(retries):
        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 404:
                return {"is_404_error": True}

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after else base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning("Rate limited for %s, sleeping %.2fs", url, sleep_for)
                await asyncio.sleep(sleep_for)
                continue

            if resp.status_code in (500, 502, 503, 504):
                if attempt < retries - 1:
                    sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    log.warning("Server error %s for %s, retry in %.2fs", resp.status_code, url, sleep_for)
                    await asyncio.sleep(sleep_for)
                    continue
                return None

            resp.raise_for_status()
            return resp.json()

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            if attempt == retries - 1:
                log.debug("Final failure for %s: %s", url, exc)
                return None
            sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.debug("Transient error for %s, retry in %.2fs", url, sleep_for)
            await asyncio.sleep(sleep_for)
        except Exception as exc:
            log.exception("Unexpected error for %s: %s", url, exc)
            return None

    return None


class DirectGreenhouseScraper:
    def __init__(self, companies: list[str]):
        self.companies = companies

    async def _fetch_company_jobs(
        self,
        client: httpx.AsyncClient,
        company: str,
        job_profile: str,
        location: str,
    ) -> FetchResult:
        slug = to_greenhouse_slug(company)
        if not slug:
            return FetchResult(company, [])
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        filters = build_filters(job_profile, location)
        now_utc = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []

        try:
            data = await fetch_json_with_retry(client, url, retries=RETRY_COUNT)
            if isinstance(data, dict) and data.get("is_404_error"):
                return FetchResult(company, [], is_404=True)
            if not data:
                return FetchResult(company, [])

            for job in data.get("jobs", []):
                if not matches_filters(job, filters, now_utc):
                    continue
                out.append(normalize_job(job, company))

            log.debug("Finished %s: found %s jobs", company, len(out))
            return FetchResult(company, out)
        except Exception as exc:
            log.exception("Failed to fetch or parse jobs for %s: %s", company, exc)
            return FetchResult(company, [])

    async def scrape(self, job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        completed = 0
        t0 = time.time()

        async def bounded_fetch(client: httpx.AsyncClient, company: str):
            nonlocal completed
            async with semaphore:
                result = await self._fetch_company_jobs(client, company, job_profile, location)
            completed += 1
            if completed % 250 == 0 or completed == len(self.companies):
                elapsed = time.time() - t0
                rate = completed / elapsed
                eta_s = int((len(self.companies) - completed) / max(rate, 0.01))
                log.info(
                    f"Greenhouse progress: {completed}/{len(self.companies)} companies "
                    f"({completed*100//len(self.companies)}%) | {rate:.1f} co/s | ETA ~{eta_s//60}m{eta_s%60:02d}s"
                )
            return result

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=800, max_keepalive_connections=400),
            headers=headers,
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        ) as client:
            tasks = [bounded_fetch(client, comp) for comp in self.companies]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        missing_companies: list[str] = []

        for result in results:
            if isinstance(result, Exception):
                log.warning("Company scrape task failed: %s", result)
                continue

            if result.is_404:
                missing_companies.append(result.company)

            for job in result.jobs:
                url = job.get("job_url") or ""
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                all_jobs.append(job)

                if 0 < max_jobs <= len(all_jobs):
                    break

        if missing_companies:
            out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "greenhouse_404_companies.txt")
            
            # Read existing counts
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
                            counts[line] = 1  # Legacy format fallback

            # Increment counts for this run
            for comp in missing_companies:
                counts[comp] = counts.get(comp, 0) + 1
            
            # Write back
            with open(out_file, "w") as f:
                for comp in sorted(counts.keys()):
                    f.write(f"{comp},{counts[comp]}\n")
            
            log.info("Updated 404 counts for %d companies in %s", len(counts), out_file)

        return all_jobs[:max_jobs] if max_jobs > 0 else all_jobs


def load_companies() -> list[str]:
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "greenhouse_companies.txt")
    if not os.path.exists(filepath):
        log.warning("%s not found. Please create it with one company name per line.", filepath)
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]


def normalize_company_row(company: dict[str, Any]) -> dict[str, Any]:
    return company


def chunked(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def run_scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
    companies = load_companies()
    if not companies:
        return []
    scraper = DirectGreenhouseScraper(companies)
    return await scraper.scrape(job_profile, location, max_jobs)


async def main():
    scrape_start_time = datetime.now(timezone.utc).isoformat()
    print("=" * 60)
    print("Greenhouse Job Scraper (Direct API) - Full Combined Version")
    print(f"Keywords : {KEYWORDS}")
    print(f"Countries: {COUNTRIES}")
    print("=" * 60)

    companies = load_companies()
    print(f"Loaded {len(companies)} companies from greenhouse_companies.txt")
    if not companies:
        print("No companies to scrape. Exiting.")
        return

    scraper = DirectGreenhouseScraper(companies)
    # job_writer = AsyncSupabaseWriter()
    # await job_writer.init()

    # company_writer = SupabaseCompanyWriter()
    # company_writer.init()

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
                    "source_board": "Greenhouse",
                    "discovered_at": datetime.now(timezone.utc).isoformat()
                }
                all_company_rows.append(normalize_company_row(company_row))

            print(f"   [OK] {new_jobs_count} jobs collected")

            if len(all_jobs) >= SUPABASE_BATCH_SIZE:
                # await job_writer.upsert_batch(all_jobs)
                all_jobs = []

            if len(all_company_rows) >= SUPABASE_BATCH_SIZE:
                # company_writer.upsert_batch(all_company_rows)
                all_company_rows = []

    print(f"\n{'=' * 60}")
    print("Scrape complete")
    print(f"{'=' * 60}")

    if all_jobs:
        # await job_writer.upsert_batch(all_jobs)
        pass

    if all_company_rows:
        # company_writer.upsert_batch(all_company_rows)
        pass

    if total_collected_jobs:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_greenhouse_jobs.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(total_collected_jobs, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(total_collected_jobs)} jobs to all_greenhouse_jobs.json")

    # print("\nRunning expiration cleanup RPC...")
    # try:
    #     await job_writer.client.rpc("mark_expired_jobs", {"p_scrape_start_time": scrape_start_time, "p_source_board": "Greenhouse"}).execute()
    #     print("Successfully expired stale jobs.")
    # except Exception as e:
    #     print(f"Failed to expire stale jobs: {e}")

    print("Run: python3 orchestrator.py --merge-only")


if __name__ == "__main__":
    asyncio.run(main())
