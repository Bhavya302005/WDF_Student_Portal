import sys
import os
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


"""Scrape Workday jobs returned by the configured Google search URL."""

# ──────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "workday_jobs"

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
    "job_profile": "software engineer, developer, backend, full stack, artificial intelligence engineer",
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
    "developer",
    "backend",
    "full stack",
    "artificial intelligence engineer",
    # "ai engineer",
    # "machine learning engineer",
    # "ml engineer"
]
COUNTRIES = [
    # "india", "usa", "uk", "germany", "canada", "singapore"
    "usa",
]


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("workday_stealth")


def build_google_search_url(attributes: dict[str, str]) -> str:
    """Build a Workday-only Google search URL from user-supplied filters."""
    query_parts = ["site:myworkdayjobs.com", str(attributes["job_profile"])]

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
    """Extract a title from Workday job-page formats."""
    patterns = [
        r'<h1[^>]*class=["\'][^"\']*(?:title|heading)[^"\']*["\'][^>]*>(.*?)</h1>',
        r'<h1[^>]*>(.*?)</h1>',
        r'<meta[^>]*(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:title|twitter:title)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, body_html, re.DOTALL | re.IGNORECASE)
        if match:
            title = clean_html_text(match.group(1))
            if title:
                return title

    for script in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        body_html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(html_lib.unescape(script))
            records = data if isinstance(data, list) else [data]
            for record in records:
                if isinstance(record, dict) and record.get("@type") == "JobPosting" and record.get("title"):
                    return clean_html_text(str(record["title"]))
        except (json.JSONDecodeError, TypeError):
            continue

    title_tag = re.search(r'<title[^>]*>(.*?)</title>', body_html, re.DOTALL | re.IGNORECASE)
    if title_tag:
        title = clean_html_text(title_tag.group(1))
        title = re.sub(r'\s*[|—-]\s*Workday\s*$', '', title, flags=re.IGNORECASE).strip()
        if title:
            return title

    return "Unknown Title"

class StealthWorkdayScraper:
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

    async def _fetch_job_page(self, client: httpx.AsyncClient, url: str) -> str | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
            except Exception as e:
                log.error(f"    Job fetch error for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
                await asyncio.sleep(0.5)
        return None

    async def _scrape_job(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        job_url: str,
        index: int,
        total: int,
        post_time: str = "any",
    ) -> dict | None:
        async with semaphore:
            log.info(f"  [{index}/{total}] Fetching job details: {job_url}")
            job_body = await self._fetch_job_page(client, job_url)

        if not job_body:
            return None

        job_title = extract_job_title(job_body)

        company_name = "Unknown Company"
        url_parts = urllib.parse.urlparse(job_url).netloc.split('.')
        if len(url_parts) >= 2 and 'myworkdayjobs' in job_url:
            company_name = url_parts[0].capitalize()

        # Try to find a better description match or just grab body text
        desc_match = re.search(r'<div[^>]*class=["\'][^"\']*description[^"\']*["\'][^>]*>(.*?)</div>', job_body, re.DOTALL | re.IGNORECASE)
        if not desc_match:
            desc_match = re.search(r'<div[^>]*id="content"[^>]*>(.*?)</div>', job_body, re.DOTALL | re.IGNORECASE)
        
        job_description = clean_html_text(desc_match.group(1)) if desc_match else ""

        # Extract Salary
        salary = ""
        sal_match = re.search(r'\$[\d,]+\s*(?:-|to)\s*\$[\d,]+|\$[\d,]+[kK]', job_description)
        if sal_match:
            salary = sal_match.group(0)

        # Extract Experience
        experience = ""
        exp_match = re.search(r'(\d+)[-to\s]*(\d+)?\s*(?:\+)?\s*(?:years?|yrs?)(?:\s*of)?\s*(?:experience)', job_description, re.IGNORECASE)
        if exp_match:
            experience = exp_match.group(0)

        # Extract Skills
        found_skills = []
        common_skills = ["Python", "Java", "C\\+\\+", "Go", "Rust", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node", "SQL", "NoSQL", "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Machine Learning", "Golang"]
        for sk in common_skills:
            if re.search(rf'\b{sk}\b', job_description, re.IGNORECASE):
                found_skills.append(sk.replace("\\+", "+"))
        skills_str = ", ".join(found_skills)

        # Try to find a specific apply url, otherwise use job_url
        apply_url_match = re.search(r'href=["\']([^"\']*?/apply[^"\']*)["\']', job_body, re.IGNORECASE)
        apply_url = apply_url_match.group(1) if apply_url_match else job_url
        if apply_url.startswith('/'):
            parsed_job = urllib.parse.urlparse(job_url)
            apply_url = f"{parsed_job.scheme}://{parsed_job.netloc}{apply_url}"

        # Location extraction from workday body
        location = ""
        loc_match = re.search(r'<div[^>]*class=["\'][^"\']*location[^"\']*["\'][^>]*>(.*?)</div>', job_body, re.DOTALL | re.IGNORECASE)
        if loc_match:
            location = clean_html_text(loc_match.group(1))

        
        now = datetime.now(timezone.utc).isoformat()
        
        # Enforce post_time limits by parsing "Posted X Days Ago"
        created_at = now
        from datetime import timedelta
        posted_match = re.search(r'Posted\s+(\d+)\s+Days?\s+Ago', job_body, re.IGNORECASE)
        if posted_match:
            days_ago = int(posted_match.group(1))
            created_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        elif re.search(r'Posted\s+30\+\s+Days?\s+Ago', job_body, re.IGNORECASE):
            created_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        elif re.search(r'Posted\s+Yesterday', job_body, re.IGNORECASE):
            created_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            
        post_time_setting = post_time.lower()
        if post_time_setting != "any":
            try:
                parsed_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if parsed_date.tzinfo is None:
                    parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - parsed_date).days
                limits = {"day": 1, "week": 7, "month": 30, "year": 365}
                max_days = limits.get(post_time_setting)
                if max_days is not None and age_days > max_days:
                    log.info(f"    ✗ Dropped (Too Old): {job_title} at {company_name} (Posted {age_days} days ago)")
                    return None
            except ValueError:
                pass

        log.info(f"    ✓ Extracted: {job_title} at {company_name}")

        return enrich_raw_job({
            "job_title": job_title,
            "company": company_name,
            "location": location,
            "job_url": job_url,
            "apply_url": apply_url,
            "description": job_description[:5000],
            "salary": salary,
            "experience": experience,
            "skills": skills_str,
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

            workday_urls = re.findall(
                r'https?://(?:[a-zA-Z0-9-]+\.)*myworkdayjobs\.com[^"\'&<>\s]+',
                urllib.parse.unquote(body),
            )
            page_urls = list(dict.fromkeys(
                urllib.parse.urldefrag(found_url)[0]
                for found_url in workday_urls
                if '/job/' in found_url or 'job-details' in found_url
            ))
            new_urls = [found_url for found_url in page_urls if found_url not in seen_urls]
            if not new_urls:
                log.info("  No new Workday jobs on this page; pagination complete")
                break

            for found_url in new_urls:
                seen_urls.add(found_url)
                unique_urls.append(found_url)
            log.info(f"  Collected {len(unique_urls)} unique Workday job URLs so far")

        log.info(f"  Found {len(unique_urls)} total unique Workday job URLs")

        return unique_urls



    async def scrape_google_url(self, url: str, post_time: str = "any") -> list[dict]:


        unique_urls = await self.discover_urls(url)


        if not unique_urls:


            return []


        semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            tasks = [
                self._scrape_job(client, semaphore, job_url, i, len(unique_urls))
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
    print("Workday Job Scraper — Multi-Keyword × Multi-Country")
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
        with open("workday_jobs.json", "w", encoding="utf-8") as f:
            json.dump(all_jobs, f, indent=4)
        print(f"Saved {len(all_jobs)} jobs to workday_jobs.json")
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
    scraper = StealthWorkdayScraper()
    url = build_google_search_url(attrs_search)
    results = await scraper.scrape_google_url(url, attrs_search["post_time"])

    return results[:max_jobs]


if __name__ == "__main__":
    asyncio.run(main())
