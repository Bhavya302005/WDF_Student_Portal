import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job
import io
import asyncio
import logging
import os
import re
import urllib.parse
import json
import httpx
from datetime import datetime, timezone
import html as html_lib

from dotenv import load_dotenv
from scrapling.fetchers import StealthyFetcher
from supabase import create_client

"""Scrape Workable jobs returned by the configured Google search URL."""

# ──────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "workable_jobs"

MAX_RETRIES = 2
MAX_CONCURRENT_JOBS = 20
GOOGLE_RESULTS_PER_PAGE = 10
MAX_GOOGLE_PAGES = int(os.getenv("GOOGLE_MAX_PAGES_PER_QUERY", "2"))

POST_TIME_FILTERS = {
    "hour": "h",
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
    "any": None,
}

# ──────────────────────────────────────────────────────────────
#  SEARCH ATTRIBUTES — edit these values before running
# ──────────────────────────────────────────────────────────────
SEARCH_ATTRIBUTES = {
    "job_profile": "software engineer, developer, backend, full stack, artificial intelligence engineer, ai engineer, machine learning engineer, ml engineer",
    "location": "",
    "skills": "",
    "experience": "",
    "job_type": "",
    "company": "",
    "work_mode": "",
    "excluded_words": "",
    "post_time": "day",
}

# ── Multi-run config: scraper loops through all combinations below ──
KEYWORDS = [
    "software engineer",
    # "developer",
    # "backend",
    # "full stack",
    # "artificial intelligence engineer",
    # "machine learning engineer",
]
COUNTRIES = [
    "usa"
]


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("workable_stealth")


def build_google_search_url(attributes: dict[str, str]) -> str:
    """Build a Workable-only Google search URL from user-supplied filters."""
    query_parts = ["site:apply.workable.com", str(attributes["job_profile"])]

    for field in ("location", "experience", "job_type", "company", "work_mode"):
        value = str(attributes.get(field) or "").strip()
        if value:
            query_parts.append(value)

    skills = str(attributes.get("skills") or "")
    query_parts.extend(skill.strip() for skill in skills.split(",") if skill.strip())

    excluded = str(attributes.get("excluded_words") or "")
    query_parts.extend(
        f'-"{word.strip()}"' for word in excluded.split(",") if word.strip()
    )

    params = {"q": " ".join(query_parts), "num": str(GOOGLE_RESULTS_PER_PAGE), "filter": "0"}
    post_time_name = str(attributes.get("post_time") or "any").lower()
    if post_time_name not in POST_TIME_FILTERS:
        valid_values = ", ".join(POST_TIME_FILTERS)
        raise ValueError(f"post_time must be one of: {valid_values}")
    post_time_code = POST_TIME_FILTERS[post_time_name]
    if post_time_code:
        params["tbs"] = f"qdr:{post_time_code}"
    return "https://www.google.com/search?" + urllib.parse.urlencode(params)

def clean_html_text(text: str) -> str:
    text = html_lib.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def extract_job_title(body_html: str) -> str:
    """Extract a title from Workable job-page formats."""
    # Priority 1: Workable specific data-ui tag
    match = re.search(r'<h1[^>]*data-ui=["\']job-title["\'][^>]*>(.*?)</h1>', body_html, re.DOTALL | re.IGNORECASE)
    if match:
        return clean_html_text(match.group(1))

    # Priority 2: Workable standard h1 header
    match = re.search(r'<h1[^>]*class=["\'][^"\']*(?:job-title|header--title)[^"\']*["\'][^>]*>(.*?)</h1>', body_html, re.DOTALL | re.IGNORECASE)
    if match:
        return clean_html_text(match.group(1))

    # Fallback to og:title or <title>
    patterns = [
        r'<meta[^>]*(?:property|name)=["\']og:title["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:title["\']',
        r'<title[^>]*>(.*?)</title>'
    ]
    for pattern in patterns:
        match = re.search(pattern, body_html, re.DOTALL | re.IGNORECASE)
        if match:
            title = clean_html_text(match.group(1))
            # Workable format is often "Job Title - Company - Workable"
            title = re.sub(r'\s*[-|—-]\s*(?:Workable|Careers).*$', '', title, flags=re.IGNORECASE).strip()
            # Then strip out the company name if "Job Title - Company"
            title = re.sub(r'\s*[-|—-]\s*.*$', '', title, flags=re.IGNORECASE).strip()
            if title and title.lower() not in ["jobs", "workable", "careers"]:
                return title

    match = re.search(r'<h1[^>]*>(.*?)</h1>', body_html, re.DOTALL | re.IGNORECASE)
    if match:
        return clean_html_text(match.group(1))

    return "Unknown Title"

class StealthWorkableScraper:
    def __init__(self):
        self._seen_urls: set[str] = set()

    async def _fetch_google_page(self, url: str) -> str | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info(f"    Fetching Google (attempt {attempt}/{MAX_RETRIES})")
                page = await StealthyFetcher.async_fetch(url, headless=True, network_idle=True, timeout=30000)
                body_str = page.body.decode('utf-8', errors='replace') if isinstance(page.body, bytes) else str(page.body)
                if "unusual traffic" in body_str.lower() or "sorry, you have been blocked" in body_str.lower():
                    raise Exception("RateLimitError: Google 429 Captcha triggered")
                return body_str
            except Exception as e:
                log.error(f"    Google fetch error: {e}")
                await asyncio.sleep(1)
        return None

    async def _fetch_job_page(self, url: str) -> str | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # We MUST use StealthyFetcher with network_idle for Workable, since it's a JS heavy SPA!
                page = await StealthyFetcher.async_fetch(url, headless=True, network_idle=True, timeout=30000)
                body_str = page.body.decode('utf-8', errors='replace') if isinstance(page.body, bytes) else str(page.body)
                if "unusual traffic" in body_str.lower() or "sorry, you have been blocked" in body_str.lower():
                    raise Exception("RateLimitError: Google 429 Captcha triggered")
                return body_str
            except Exception as e:
                log.error(f"    Job fetch error for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(0.5)
        return None

    async def _scrape_job(
        self,
        semaphore: asyncio.Semaphore,
        job_url: str,
        index: int,
        total: int,
    ) -> dict | None:
        async with semaphore:
            log.info(f"  [{index}/{total}] Fetching job details: {job_url}")
            job_body = await self._fetch_job_page(job_url)

        if not job_body:
            return None

        job_title = extract_job_title(job_body)

        company_name = "Unknown Company"
        parsed_url = urllib.parse.urlparse(job_url)
        path_parts = [p for p in parsed_url.path.strip('/').split('/') if p]
        
        # https://apply.workable.com/{Company}/j/{JobID}
        if len(path_parts) >= 1:
            company_name = path_parts[0].replace("-", " ").title()

        # Try to extract from the specific Workable container
        desc_match = re.search(r'<div[^>]*data-ui=["\']job-description["\'][^>]*>(.*?)</div>', job_body, re.DOTALL | re.IGNORECASE)
        if not desc_match:
            desc_match = re.search(r'<main[^>]*>(.*?)</main>', job_body, re.DOTALL | re.IGNORECASE)
        if not desc_match:
            # Fallback
            desc_match = re.search(r'<body[^>]*>(.*?)</body>', job_body, re.DOTALL | re.IGNORECASE)
            
        job_description = clean_html_text(desc_match.group(1)) if desc_match else ""
        now = datetime.now(timezone.utc).isoformat()
        log.info(f"    ✓ Extracted: {job_title} at {company_name}")
        return enrich_raw_job({
            "job_title": job_title,
            "company": company_name,
            "job_url": job_url,
            "description": job_description[:5000],
            "timestamp": now,
            "created_at": now,
        })

    async def discover_urls(self, url: str) -> list[str]:
        log.info(f"  Fetching all Google result pages for supplied URL: {url}")
        unique_urls: list[str] = []
        seen_urls: set[str] = set()

        for page_number in range(MAX_GOOGLE_PAGES):
            offset = page_number * GOOGLE_RESULTS_PER_PAGE
            if offset == 0:
                page_url = url
            else:
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                params = [(key, value) for key, value in params if key != "start"]
                params.append(("start", str(offset)))
                page_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))

            log.info(f"  Google results page {page_number + 1} (start={offset})")
            body = await self._fetch_google_page(page_url)
            import random as _rnd
            import asyncio as _asyncio
            await _asyncio.sleep(_rnd.uniform(2, 4))
            if not body:
                log.warning("  Google returned no page content; pagination complete")
                break

            # Look for apply.workable.com or any workable.com
            job_urls = re.findall(
                r'https?://(?:[a-zA-Z0-9-]+\.)*workable\.com/[^"\'&<>\s]+',
                urllib.parse.unquote(body),
            )
            page_urls = list(dict.fromkeys(
                urllib.parse.urldefrag(found_url)[0]
                for found_url in job_urls
            ))
            new_urls = [found_url for found_url in page_urls if found_url not in seen_urls]
            if not new_urls:
                log.info("  No new Workable jobs on this page; pagination complete")
                break

            for found_url in new_urls:
                seen_urls.add(found_url)
                unique_urls.append(found_url)
            log.info(f"  Collected {len(unique_urls)} unique Workable job URLs so far")

        log.info(f"  Found {len(unique_urls)} total unique Workable job URLs")

        return unique_urls



    async def scrape_google_url(self, url: str) -> list[dict]:


        unique_urls = await self.discover_urls(url)


        if not unique_urls:


            return []


        semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        
        tasks = [
            self._scrape_job(semaphore, job_url, i, len(unique_urls))
            for i, job_url in enumerate(unique_urls, 1)
        ]
        jobs = await asyncio.gather(*tasks)

        unique_jobs: dict[str, dict] = {}
        for job in jobs:
            if job is not None:
                unique_jobs[job["job_url"]] = job
        return list(unique_jobs.values())

class SupabaseUpserter:
    def __init__(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            log.warning("SUPABASE_URL and SUPABASE_KEY must be set in .env")
            self.client = None
            return
        self.client = create_client(SUPABASE_URL, SUPABASE_KEY)
        self.table_name = SUPABASE_TABLE

    def upsert(self, jobs: list[dict]):
        if not self.client or not jobs:
            return
            
        inserted = skipped = 0
        for job in jobs:
            try:
                existing = self.client.table(self.table_name).select("id").eq("job_url", job["job_url"]).execute()
                if existing.data:
                    skipped += 1
                    continue
                
                self.client.table(self.table_name).insert(job).execute()
                inserted += 1
            except Exception as e:
                log.error(f"  Supabase insert error: {e}")
        
        log.info(f"  DONE => Inserted: {inserted} | Skipped (Duplicates): {skipped}")

async def main():
    print("=" * 60)
    print("Workable Job Scraper — Multi-Keyword × Multi-Country")
    print(f"  Keywords : {KEYWORDS}")
    print(f"  Countries: {COUNTRIES}")
    print("=" * 60)

    all_jobs: list[dict] = []
    seen_urls = set()

    for country in COUNTRIES:


        for keyword in KEYWORDS:


            print(f"\n  → '{keyword}' | {country}")


            jobs = await scrape(keyword, country, 99999)


            new_jobs_count = 0



            for job in jobs:



                url = job.get("job_url", "")



                if url and url in seen_urls:



                    continue



                seen_urls.add(url)



                job["_keyword"] = keyword



                job["_country"] = country



                all_jobs.append(job)



                new_jobs_count += 1



            



            print(f"    ✓ {new_jobs_count} jobs collected")


            await asyncio.sleep(3)

    print(f"\n{'=' * 60}")
    print(f"TOTAL: {len(all_jobs)} jobs across all combinations")
    print(f"{'=' * 60}")

    if all_jobs:
        with open("workable_jobs.json", "w", encoding="utf-8") as f:
            json.dump(all_jobs, f, indent=4)
        print(f"Saved {len(all_jobs)} jobs to workable_jobs.json")
        print("Run: python3 orchestrator.py --merge-only")

# ──────────────────────────────────────────────────────────────
#  IMPORTABLE ENTRY POINT (used by orchestrator.py)
# ──────────────────────────────────────────────────────────────
async def scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict]:
    """Called by orchestrator.py. Returns up to max_jobs normalized job dicts."""
    attrs_search = {
        "job_profile": job_profile,
        "location": location,
        "skills": "",
        "experience": "",
        "job_type": "",
        "company": "",
        "work_mode": "",
        "excluded_words": "",
        "post_time": "day",
    }
    scraper = StealthWorkableScraper()
    url = build_google_search_url(attrs_search)
    results = await scraper.scrape_google_url(url)

    return results[:max_jobs]


if __name__ == "__main__":
    asyncio.run(main())
