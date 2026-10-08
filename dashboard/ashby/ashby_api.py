import asyncio
import sys
import os
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

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "350"))  # 350 — boosted for speed
REQUEST_TIMEOUT = float(os.getenv("ASHBY_TIMEOUT", "15"))           # 15s — faster fail
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
log = logging.getLogger("ashby_scraper")

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

    job_location = (job.get("location") or "").strip()
    desc_plain = job.get("descriptionPlain") or ""
    desc_html = job.get("descriptionHtml") or ""
    desc_text = desc_plain or clean_html_text(desc_html)
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
    location = (job.get("location") or "").strip()
    desc_text = clean_html_text(job.get("descriptionHtml", "")) or (job.get("descriptionPlain") or "")

    job_id = str(job.get("id") or title)
    raw_key = f"ashby:{company_slug}:{job_id}"
    dedupe_key = hashlib.md5(raw_key.encode()).hexdigest()

    # Prioritize Ashby's own structured fields for work mode; when neither
    # is present, leave work_mode/is_remote unset so enrich_raw_job() below
    # (and pipeline.py's regex fallback) can derive them from the
    # title/location/description instead.
    workplace_type = job.get("workplaceType")
    is_remote_api = job.get("isRemote")

    wm = ""
    if workplace_type:
        wm_lower = workplace_type.lower()
        if wm_lower == "remote":
            wm = "Remote"
        elif wm_lower == "hybrid":
            wm = "Hybrid"
        elif wm_lower == "onsite":
            wm = "Onsite"
    elif is_remote_api is True:
        wm = "Remote"

    # workplaceType is the more specific/reliable signal when present: Ashby's
    # isRemote flag turns out to mean "has some remote flexibility" rather
    # than "fully remote" -- live data showed isRemote=true on ~100% of
    # sampled Hybrid-tagged jobs at Ramp/Vanta/Notion, which would otherwise
    # mislabel every hybrid role as fully remote. Only fall back to the raw
    # isRemote boolean when workplaceType didn't give us a concrete category.
    if wm:
        is_remote = (wm == "Remote")
    elif is_remote_api is True:
        is_remote = True
    elif is_remote_api is False:
        is_remote = False
    else:
        is_remote = None

    # _fetch_company_jobs requests ?includeCompensation=true, so pass Ashby's
    # structured compensation object through -- enrich_raw_job()'s
    # extract_salary() checks compensationTierSummary/scrapeableCompensation-
    # SalarySummary there before falling back to regex over the description.
    # experience/skills still aren't structured fields, so those stay
    # regex-derived from the description as before.
    return enrich_raw_job({
        "id": dedupe_key,
        "job_title": title,
        "compensation": job.get("compensation"),
        "company": normalize_company_name(company_slug),
        "location": location,
        "job_url": job.get("jobUrl") or "",
        "apply_url": job.get("applyUrl") or "",
        "description": desc_text,
        "description_raw": job.get("descriptionHtml") or job.get("descriptionPlain") or desc_text,
        "structured_fields": {"work_mode": wm, "job_type": job.get("employmentType")},
        "work_mode": wm,
        "source_board": "Ashby",
        "scraper_type": "api",
        "is_remote": is_remote,
        "job_type": "full_time",
        "scraped_at": format_iso_time(datetime.now(timezone.utc).isoformat()),
        "created_at": format_iso_time(job.get("publishedAt")) or format_iso_time(datetime.now(timezone.utc).isoformat()),
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
                log.warning("429 for %s, sleeping %.2fs", url, sleep_for)
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
                log.warning("Final failure for %s: %s", url, exc)
                return None
            sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning("Transient error for %s: %s. Retry in %.2fs", url, exc, sleep_for)
            await asyncio.sleep(sleep_for)
        except Exception as exc:
            log.exception("Unexpected error for %s: %s", url, exc)
            return None

    return None


class DirectAshbyScraper:
    def __init__(self, companies: list[str]):
        self.companies = companies

    async def _fetch_company_jobs(
        self,
        client: httpx.AsyncClient,
        company: str,
        job_profile: str,
        location: str,
    ) -> FetchResult:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"
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

            log.info("Finished %s: found %s jobs", company, len(out))
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
            if completed % 300 == 0 or completed == len(self.companies):
                elapsed = time.time() - t0
                rate = completed / elapsed
                eta_s = int((len(self.companies) - completed) / max(rate, 0.01))
                log.info(
                    f"Ashby progress: {completed}/{len(self.companies)} companies "
                    f"({completed*100//len(self.companies)}%) | {rate:.1f} co/s | ETA ~{eta_s//60}m{eta_s%60:02d}s"
                )
            return result

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=700, max_keepalive_connections=350),
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
            if getattr(result, "is_404", False):
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
            out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ashby_404_companies.txt")
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
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ashby_companies.txt")
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
    scraper = DirectAshbyScraper(companies)
    return await scraper.scrape(job_profile, location, max_jobs)


async def main():
    scrape_start_time = datetime.now(timezone.utc).isoformat()
    print("=" * 60)
    print("Ashby Job Scraper (Direct API) - Full Combined Version")
    print(f"Keywords : {KEYWORDS}")
    print(f"Countries: {COUNTRIES}")
    print("=" * 60)

    companies = load_companies()
    print(f"Loaded {len(companies)} companies from ashby_companies.txt")
    if not companies:
        print("No companies to scrape. Exiting.")
        return

    scraper = DirectAshbyScraper(companies)
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
                    "source_board": "Ashby",
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
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_ashby_jobs.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(total_collected_jobs, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(total_collected_jobs)} jobs to all_ashby_jobs.json")

    print("\nRunning expiration cleanup RPC...")
    try:
        await job_writer.client.rpc("mark_expired_jobs", {"p_scrape_start_time": scrape_start_time, "p_source_board": "Ashby"}).execute()
        print("Successfully expired stale jobs.")
    except Exception as e:
        print(f"Failed to expire stale jobs: {e}")

    print("Run: python3 orchestrator.py --merge-only")


if __name__ == "__main__":
    asyncio.run(main())



#------------------------------



# import asyncio
# import html as html_lib
# import json
# import logging
# import os
# import hashlib
# import random
# import re
# import time
# from dataclasses import dataclass
# from datetime import datetime, timedelta, timezone
# from typing import Any

# import httpx
# from dotenv import load_dotenv
# from supabase import acreate_client, create_client

# load_dotenv()

# SUPABASE_URL = os.getenv("SUPABASE_URL")
# SUPABASE_KEY = os.getenv("SUPABASE_KEY")
# SUPABASE_TABLE = "ashby_jobs"

# MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "10"))
# REQUEST_TIMEOUT = float(os.getenv("ASHBY_TIMEOUT", "20"))
# RETRY_COUNT = int(os.getenv("RETRY_COUNT", "3"))
# BREAKER_THRESHOLD = int(os.getenv("BREAKER_THRESHOLD", "5"))
# BREAKER_COOLDOWN = int(os.getenv("BREAKER_COOLDOWN", "60"))
# SUPABASE_BATCH_SIZE = int(os.getenv("SUPABASE_BATCH_SIZE", "300"))
# ERROR_LOG_TABLE = "scrape_error_logs"
# BATCH_SIZE = int(os.getenv("SUPABASE_BATCH_SIZE", "200"))
# MAX_RETRIES = int(os.getenv("UPSERT_RETRIES", "4"))
# BASE_DELAY = float(os.getenv("UPSERT_BASE_DELAY", "1.0"))

# SEARCH_ATTRIBUTES = {
#     "location": "",
#     "skills": "",
#     "experience": "",
#     "job_type": "",
#     "work_mode": "",
#     "excluded_words": "",
#     "post_time": "day",
# }

# KEYWORDS = [""]
# COUNTRIES = [""]

# logging.basicConfig(
#     level=logging.INFO,
#     format="[%(asctime)s] %(levelname)s: %(message)s",
#     datefmt="%Y-%m-%d %H:%M:%S",
# )
# log = logging.getLogger("ashby_scraper")

# US_STATES = {
#     "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
#     "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
#     "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
#     "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
#     "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
#     "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
#     "virginia", "washington", "west virginia", "wisconsin", "wyoming",
# }
# STATE_CODES = {"ca", "ny", "tx", "wa", "ma", "il"}
# COMMON_SKILLS = [
#     "Python", "Java", "C++", "Go", "Rust", "JavaScript", "TypeScript", "React", "Angular", "Vue",
#     "Node", "SQL", "NoSQL", "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Machine Learning", "Golang",
# ]


# def clean_html_text(text: str) -> str:
#     if not text:
#         return ""
#     text = html_lib.unescape(text)
#     text = re.sub(r"<[^>]+>", " ", text)
#     return re.sub(r"\s+", " ", text).strip()


# def split_csv(value: str) -> list[str]:
#     return [item.strip().lower() for item in value.split(",") if item.strip()] if value else []


# def post_time_to_delta(value: str) -> timedelta | None:
#     value = (value or "any").lower().strip()
#     mapping = {
#         "hour": timedelta(hours=1),
#         "day": timedelta(days=1),
#         "3_days": timedelta(days=3),
#         "week": timedelta(weeks=1),
#         "month": timedelta(days=30),
#         "year": timedelta(days=365),
#     }
#     return mapping.get(value)


# def normalize_company_name(slug: str) -> str:
#     return slug.replace("-", " ").replace("_", " ").title()


# def extract_work_mode(title: str, location: str, description: str) -> str:
#     content = f"{title} {location} {description}".lower()
#     if re.search(r"\b(remote|wfh|work from home)\b", content):
#         return "Remote"
#     if re.search(r"\bhybrid\b", content):
#         return "Hybrid"
#     if re.search(r"\b(onsite|in-office|in office|in-person)\b", content):
#         return "Onsite"
#     return ""


# def extract_salary(text: str, compensation: dict[str, Any] | None = None) -> str:
#     compensation = compensation or {}
#     salary = compensation.get("compensationTierSummary") or compensation.get("scrapeableCompensationSalarySummary") or ""
#     if salary:
#         return salary
#     m = re.search(
#         r"(?:[\$£€])[\d,]+[kK]?\s*(?:-|to|—|–|&mdash;|&ndash;)\s*(?:[\$£€])?[\d,]+[kK]?|(?:[\$£€])[\d,]+[kK]?",
#         text,
#     )
#     return m.group(0) if m else ""


# def extract_experience(text: str) -> str:
#     m = re.search(
#         r"(\d+)\s*(?:-|to|—|–|&mdash;|&ndash;)?\s*(\d+)?\s*(?:\+)?\s*(?:years?|yrs?)(?:\s*of)?\s*(?:experience)",
#         text,
#         re.I,
#     )
#     return m.group(0) if m else ""


# def extract_skills(text: str) -> str:
#     found = []
#     for skill in COMMON_SKILLS:
#         if re.search(rf"\b{re.escape(skill)}\b", text, re.I):
#             found.append(skill)
#     return ", ".join(dict.fromkeys(found))


# def build_filters(job_profile: str, location: str) -> dict[str, Any]:
#     return {
#         "profiles": split_csv(job_profile),
#         "location": (SEARCH_ATTRIBUTES.get("location", "") or location or "").lower().strip(),
#         "work_mode": SEARCH_ATTRIBUTES.get("work_mode", "").lower().strip(),
#         "job_type": SEARCH_ATTRIBUTES.get("job_type", "").lower().strip(),
#         "experience": SEARCH_ATTRIBUTES.get("experience", "").lower().strip(),
#         "skills": split_csv(SEARCH_ATTRIBUTES.get("skills", "")),
#         "excluded": split_csv(SEARCH_ATTRIBUTES.get("excluded_words", "")),
#         "max_age": post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "any")),
#     }


# def matches_filters(job: dict[str, Any], filters: dict[str, Any], now_utc: datetime) -> bool:
#     title = (job.get("title") or "").strip()
#     if not title:
#         return False

#     job_location = (job.get("location") or "").strip()
#     desc_plain = job.get("descriptionPlain") or ""
#     desc_html = job.get("descriptionHtml") or ""
#     desc_text = desc_plain or clean_html_text(desc_html)
#     text = f"{title} {job_location} {desc_text}".lower()

#     profiles = filters["profiles"]
#     if profiles and not any(p in title.lower() for p in profiles):
#         return False

#     loc_filter = filters["location"]
#     if loc_filter:
#         if loc_filter in {"usa", "united states", "united states of america", "us"}:
#             loc_lower = job_location.lower()
#             if not (
#                 any(st in loc_lower for st in US_STATES)
#                 or any(re.search(rf"\b{code}\b", loc_lower) for code in STATE_CODES)
#             ):
#                 return False
#         elif loc_filter not in text:
#             return False

#     if filters["work_mode"] and filters["work_mode"] not in text:
#         return False
#     if filters["job_type"] and filters["job_type"] not in text:
#         return False
#     if filters["experience"] and filters["experience"] not in text:
#         return False
#     if filters["skills"] and not all(skill in text for skill in filters["skills"]):
#         return False
#     if filters["excluded"] and any(word in text for word in filters["excluded"]):
#         return False

#     max_age = filters["max_age"]
#     published_at = job.get("publishedAt")
#     if max_age and published_at:
#         try:
#             published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
#             if now_utc - published > max_age:
#                 return False
#         except ValueError:
#             pass

#     return True


# def normalize_job(job: dict[str, Any], company_slug: str) -> dict[str, Any]:
#     title = (job.get("title") or "").strip()
#     location = (job.get("location") or "").strip()
#     desc_text = clean_html_text(job.get("descriptionHtml", "")) or (job.get("descriptionPlain") or "")
#     comp = job.get("compensation") or {}

#     job_id = str(job.get("id") or title)
#     raw_key = f"ashby:{company_slug}:{job_id}"
#     dedupe_key = hashlib.md5(raw_key.encode()).hexdigest()

#     return {
#         "dedupe_key": dedupe_key,
#         "ats": "ashby",
#         "source": "ashby_public_api",
#         "company_slug": company_slug,
#         "company": normalize_company_name(company_slug),
#         "job_title": title,
#         "location": location,
#         "job_url": job.get("jobUrl") or "",
#         "apply_url": job.get("applyUrl") or "",
#         "description": desc_text,
#         "salary": extract_salary(desc_text, comp),
#         "experience": extract_experience(desc_text),
#         "skills": extract_skills(desc_text),
#         "work_mode": extract_work_mode(title, location, desc_text),
#         "status": "active",
#         "published_at": job.get("publishedAt") or None,
#         "updated_at": job.get("updatedAt") or None,
#         "last_seen_at": datetime.now(timezone.utc).isoformat(),
#         "raw_payload": job,
#     }


# class CircuitBreaker:
#     def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
#         self.failure_threshold = failure_threshold
#         self.recovery_timeout = recovery_timeout
#         self.failure_count = 0
#         self.state = "closed"
#         self.opened_at = None

#     def allow_request(self) -> bool:
#         if self.state == "open":
#             if time.time() - self.opened_at >= self.recovery_timeout:
#                 self.state = "half_open"
#                 return True
#             return False
#         return True

#     def success(self):
#         self.failure_count = 0
#         self.state = "closed"
#         self.opened_at = None

#     def failure(self):
#         self.failure_count += 1
#         if self.failure_count >= self.failure_threshold:
#             self.state = "open"
#             self.opened_at = time.time()


# @dataclass
# class FetchResult:
#     company: str
#     jobs: list[dict[str, Any]]






# class AsyncSupabaseWriter:
#     def __init__(self, table_name: str = SUPABASE_TABLE):
#         self.table_name = table_name
#         self.client = None

#     async def init(self):
#         if not SUPABASE_URL or not SUPABASE_KEY:
#             raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
#         self.client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

#     async def upsert_batch(self, rows: list[dict], on_conflict: str = "job_url"):
#         if not self.client or not rows:
#             return
#         await self.client.table(self.table_name).upsert(
#             rows,
#             on_conflict=on_conflict,
#         ).execute()


# class AsyncErrorLogger:
#     def __init__(self, table_name: str = ERROR_LOG_TABLE):
#         self.table_name = table_name
#         self.client = None

#     async def init(self):
#         if not SUPABASE_URL or not SUPABASE_KEY:
#             raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
#         self.client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)

#     async def log_failure(
#         self,
#         run_id: str,
#         batch_index: int,
#         rows: list[dict],
#         exc: Exception,
#         attempt_count: int,
#         table_name: str = SUPABASE_TABLE,
#     ):
#         if not self.client:
#             return

#         payload_sample = rows[:3]
#         log_row = {
#             "run_id": run_id,
#             "batch_index": batch_index,
#             "table_name": table_name,
#             "row_count": len(rows),
#             "error_type": type(exc).__name__,
#             "error_message": str(exc),
#             "attempt_count": attempt_count,
#             "payload_sample": payload_sample,
#             "created_at": datetime.now(timezone.utc).isoformat(),
#         }

#         try:
#             await self.client.table(self.table_name).insert(log_row).execute()
#         except Exception as log_exc:
#             log.error("Failed to write error log to Supabase: %s", log_exc)
#             with open("failed_upsert_error_logs.jsonl", "a", encoding="utf-8") as f:
#                 f.write(json.dumps(log_row, ensure_ascii=False) + "\n")


# def chunked(items: list[dict], size: int):
#     for i in range(0, len(items), size):
#         yield i // size, items[i:i + size]


# async def upsert_with_retry(
#     writer: AsyncSupabaseWriter,
#     error_logger: AsyncErrorLogger,
#     rows: list[dict],
#     run_id: str,
#     batch_index: int,
#     on_conflict: str = "job_url",
# ):
#     last_exc = None

#     for attempt in range(1, MAX_RETRIES + 1):
#         try:
#             await writer.upsert_batch(rows, on_conflict=on_conflict)
#             log.info("Batch %s upserted successfully (%s rows)", batch_index, len(rows))
#             return True
#         except Exception as exc:
#             last_exc = exc
#             delay = BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
#             log.warning(
#                 "Batch %s failed on attempt %s/%s: %s. Retrying in %.2fs",
#                 batch_index,
#                 attempt,
#                 MAX_RETRIES,
#                 exc,
#                 delay,
#             )

#             if attempt < MAX_RETRIES:
#                 await asyncio.sleep(delay)

#     log.error("Batch %s failed after %s attempts", batch_index, MAX_RETRIES)

#     try:
#         await error_logger.log_failure(
#             run_id=run_id,
#             batch_index=batch_index,
#             rows=rows,
#             exc=last_exc or Exception("Unknown batch upsert failure"),
#             attempt_count=MAX_RETRIES,
#             table_name=writer.table_name,
#         )
#     except Exception as log_exc:
#         log.error("Failed to log batch failure: %s", log_exc)
#         fallback = {
#             "run_id": run_id,
#             "batch_index": batch_index,
#             "table_name": writer.table_name,
#             "row_count": len(rows),
#             "error_type": type(last_exc).__name__ if last_exc else "Exception",
#             "error_message": str(last_exc) if last_exc else "Unknown batch upsert failure",
#             "attempt_count": MAX_RETRIES,
#             "payload_sample": rows[:3],
#             "created_at": datetime.now(timezone.utc).isoformat(),
#         }
#         with open("failed_upserts_fallback.jsonl", "a", encoding="utf-8") as f:
#             f.write(json.dumps(fallback, ensure_ascii=False) + "\n")

#     with open(f"failed_batch_{batch_index}.json", "w", encoding="utf-8") as f:
#         json.dump(rows, f, indent=2, ensure_ascii=False)

#     return False


# async def upsert_all_jobs(all_jobs: list[dict]):
#     run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
#     writer = AsyncSupabaseWriter()
#     error_logger = AsyncErrorLogger()

#     await writer.init()
#     await error_logger.init()

#     success_count = 0
#     failure_count = 0

#     for batch_index, batch in chunked(all_jobs, BATCH_SIZE):
#         ok = await upsert_with_retry(
#             writer=writer,
#             error_logger=error_logger,
#             rows=batch,
#             run_id=run_id,
#             batch_index=batch_index,
#             on_conflict="dedupe_key",
#         )
#         if ok:
#             success_count += 1
#         else:
#             failure_count += 1

#     log.info(
#         "Finished run %s. Success batches: %s, Failed batches: %s",
#         run_id,
#         success_count,
#         failure_count,
#     )

# async def upsert_all_companies(all_companies: list[dict]):
#     run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
#     writer = AsyncSupabaseWriter(table_name="scraped_companies")
#     error_logger = AsyncErrorLogger(table_name="scraped_companies")

#     await writer.init()
#     await error_logger.init()

#     success_count = 0
#     failure_count = 0

#     for batch_index, batch in chunked(all_companies, BATCH_SIZE):
#         ok = await upsert_with_retry(
#             writer=writer,
#             error_logger=error_logger,
#             rows=batch,
#             run_id=run_id,
#             batch_index=batch_index,
#             on_conflict="company_slug",
#         )
#         if ok:
#             success_count += 1
#         else:
#             failure_count += 1

#     log.info(
#         "Finished companies run %s. Success batches: %s, Failed batches: %s",
#         run_id,
#         success_count,
#         failure_count,
#     )

# ashby_breaker = CircuitBreaker(BREAKER_THRESHOLD, BREAKER_COOLDOWN)


# async def fetch_json_with_retry(client: httpx.AsyncClient, url: str, retries: int = RETRY_COUNT) -> dict[str, Any] | None:
#     if not ashby_breaker.allow_request():
#         log.warning("Circuit open for %s, skipping request", url)
#         return None

#     base_delay = 1.0

#     for attempt in range(retries):
#         try:
#             resp = await client.get(url, timeout=REQUEST_TIMEOUT)

#             if resp.status_code == 404:
#                 ashby_breaker.success()
#                 return None

#             if resp.status_code == 429:
#                 ashby_breaker.failure()
#                 retry_after = resp.headers.get("Retry-After")
#                 sleep_for = float(retry_after) if retry_after else base_delay * (2 ** attempt) + random.uniform(0, 0.5)
#                 log.warning("429 for %s, sleeping %.2fs", url, sleep_for)
#                 await asyncio.sleep(sleep_for)
#                 continue

#             if resp.status_code in (500, 502, 503, 504):
#                 ashby_breaker.failure()
#                 if attempt < retries - 1:
#                     sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
#                     log.warning("Server error %s for %s, retry in %.2fs", resp.status_code, url, sleep_for)
#                     await asyncio.sleep(sleep_for)
#                     continue
#                 return None

#             resp.raise_for_status()
#             ashby_breaker.success()
#             return resp.json()

#         except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
#             ashby_breaker.failure()
#             if attempt == retries - 1:
#                 log.warning("Final failure for %s: %s", url, exc)
#                 return None
#             sleep_for = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
#             log.warning("Transient error for %s: %s. Retry in %.2fs", url, exc, sleep_for)
#             await asyncio.sleep(sleep_for)
#         except Exception as exc:
#             ashby_breaker.failure()
#             log.exception("Unexpected error for %s: %s", url, exc)
#             return None

#     return None


# class DirectAshbyScraper:
#     def __init__(self, companies: list[str]):
#         self.companies = companies

#     async def _fetch_company_jobs(
#         self,
#         client: httpx.AsyncClient,
#         company: str,
#         job_profile: str,
#         location: str,
#     ) -> FetchResult:
#         url = f"https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"
#         filters = build_filters(job_profile, location)
#         now_utc = datetime.now(timezone.utc)
#         out: list[dict[str, Any]] = []

#         try:
#             data = await fetch_json_with_retry(client, url, retries=RETRY_COUNT)
#             if not data:
#                 return FetchResult(company, [])

#             for job in data.get("jobs", []):
#                 if not matches_filters(job, filters, now_utc):
#                     continue
#                 out.append(normalize_job(job, company))

#             log.info("Finished %s: found %s jobs", company, len(out))
#             return FetchResult(company, out)
#         except Exception as exc:
#             log.exception("Failed to fetch or parse jobs for %s: %s", company, exc)
#             return FetchResult(company, [])

#     async def scrape(self, job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
#         headers = {
#             "User-Agent": (
#                 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
#                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
#             ),
#             "Accept": "application/json, text/plain, */*",
#         }

#         semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

#         async def bounded_fetch(client: httpx.AsyncClient, company: str):
#             async with semaphore:
#                 result = await self._fetch_company_jobs(client, company, job_profile, location)
#                 await asyncio.sleep(random.uniform(0.1, 0.3))
#                 return result

#         async with httpx.AsyncClient(
#             limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
#             headers=headers,
#             follow_redirects=True,
#             timeout=REQUEST_TIMEOUT,
#         ) as client:
#             tasks = [bounded_fetch(client, comp) for comp in self.companies]
#             results = await asyncio.gather(*tasks, return_exceptions=True)

#         all_jobs: list[dict[str, Any]] = []
#         seen_urls: set[str] = set()

#         for result in results:
#             if isinstance(result, Exception):
#                 log.warning("Company scrape task failed: %s", result)
#                 continue

#             for job in result.jobs:
#                 url = job.get("job_url") or ""
#                 if url and url in seen_urls:
#                     continue
#                 if url:
#                     seen_urls.add(url)
#                 all_jobs.append(job)

#                 if 0 < max_jobs <= len(all_jobs):
#                     return all_jobs[:max_jobs]

#         return all_jobs[:max_jobs] if max_jobs > 0 else all_jobs


# def load_companies() -> list[str]:
#     filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ashby_companies.txt")
#     if not os.path.exists(filepath):
#         log.warning("%s not found. Please create it with one company name per line.", filepath)
#         return []
#     with open(filepath, "r", encoding="utf-8") as f:
#         return [line.strip() for line in f if line.strip()]


# def normalize_company_row(company: dict[str, Any]) -> dict[str, Any]:
#     return {
#         "company_slug": company["company_slug"].strip().lower(),
#         "company_name": company.get("company_name", "").strip(),
#         "website": company.get("website", "").strip(),
#         "careers_url": company.get("careers_url", "").strip(),
#         "ats_type": company.get("ats_type", "").strip().lower(),
#         "country": company.get("country", "").strip().lower(),
#         "status": company.get("status", "active").strip().lower(),
#         "last_scraped_at": company.get("last_scraped_at") or datetime.now(timezone.utc).isoformat(),
#         "raw_payload": company.get("raw_payload", {}),
#         "updated_at": datetime.now(timezone.utc).isoformat(),
#     }


# def chunked(items: list[Any], size: int):
#     for i in range(0, len(items), size):
#         yield i // size, items[i:i + size]


# async def run_scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
#     companies = load_companies()
#     if not companies:
#         return []
#     scraper = DirectAshbyScraper(companies)
#     return await scraper.scrape(job_profile, location, max_jobs)


# async def main():
#     scrape_start_time = datetime.now(timezone.utc).isoformat()
#     print("=" * 60)
#     print("Ashby Job Scraper (Direct API) - Full Combined Version")
#     print(f"Keywords : {KEYWORDS}")
#     print(f"Countries: {COUNTRIES}")
#     print("=" * 60)

#     companies = load_companies()
#     print(f"Loaded {len(companies)} companies from ashby_companies.txt")
#     if not companies:
#         print("No companies to scrape. Exiting.")
#         return

#     scraper = DirectAshbyScraper(companies)

#     all_jobs: list[dict[str, Any]] = []
#     total_collected_jobs: list[dict[str, Any]] = []
#     all_company_rows: list[dict[str, Any]] = []
#     seen_urls: set[str] = set()

#     for country in COUNTRIES:
#         for keyword in KEYWORDS:
#             print(f"\n--> '{keyword}' | {country}")
#             jobs = await scraper.scrape(keyword, country, 0)
#             new_jobs_count = 0

#             for job in jobs:
#                 url = job.get("job_url", "")
#                 if url and url in seen_urls:
#                     continue
#                 if url:
#                     seen_urls.add(url)
#                 job["_keyword"] = keyword
#                 job["_country"] = country
#                 all_jobs.append(job)
#                 total_collected_jobs.append(job)
#                 new_jobs_count += 1

#             company_row = {
#                 "company_slug": country,
#                 "company_name": country.title(),
#                 "website": "",
#                 "careers_url": "",
#                 "ats_type": "ashby",
#                 "country": country,
#                 "status": "active",
#                 "last_scraped_at": datetime.now(timezone.utc).isoformat(),
#                 "raw_payload": {"keyword": keyword},
#             }
#             all_company_rows.append(normalize_company_row(company_row))

#             print(f"   [OK] {new_jobs_count} jobs collected")

#     print(f"\n{'=' * 60}")
#     print("Scrape complete")
#     print(f"{'=' * 60}")

#     if total_collected_jobs:
#         out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_ashby_jobs.json")
#         with open(out_path, "w", encoding="utf-8") as f:
#             json.dump(total_collected_jobs, f, indent=2, ensure_ascii=False)
#         print(f"Saved {len(total_collected_jobs)} jobs to all_ashby_jobs.json")

# #     print("\nStarting batched database upserts...")
# #     await upsert_all_jobs(total_collected_jobs)
# #     await upsert_all_companies(all_company_rows)
# # 
# #     print("\nRunning expiration cleanup RPC...")
# #     try:
# #         temp_client = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
# #         await temp_client.rpc("mark_expired_jobs", {"scrape_start_time": scrape_start_time}).execute()
# #         print("Successfully expired stale jobs.")
# #     except Exception as e:
# #         print(f"Failed to expire stale jobs: {e}")

#     print("Run: python3 orchestrator.py --merge-only")


# if __name__ == "__main__":
#     asyncio.run(main())
