import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from supabase import acreate_client, create_client

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job
from job_extraction import database_rows
from scraper_runtime import bounded_map, retry_after_seconds, paced_request
from collections import Counter

load_dotenv()

SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE  = "jobs"

MAX_CONCURRENT_COMPANIES = int(os.getenv("SR_MAX_CONCURRENT", "30"))  # All tenants share api.smartrecruiters.com.
REQUEST_TIMEOUT          = float(os.getenv("SR_TIMEOUT", "15"))        # 15s — faster fail
RETRY_COUNT              = int(os.getenv("RETRY_COUNT", "2"))           # 2 retries
SUPABASE_BATCH_SIZE      = int(os.getenv("SUPABASE_BATCH_SIZE", "500"))
MAX_JOBS_PER_COMPANY     = int(os.getenv("SR_MAX_JOBS_PER_COMPANY", "0"))  # 0 = all pages
DETAIL_CONCURRENCY = max(1, int(os.getenv("SR_DETAIL_CONCURRENCY", "80")))

# ── Scrape config (overridden by pipeline) ──────────────────────────────────
SEARCH_ATTRIBUTES = {
    "skills": "",
    "experience": "",
    "job_type": "",
    "work_mode": "",
    "excluded_words": "",
    "post_time": "day",   # "hour" | "day" | "week" | "month" | "year" | "any"
}

KEYWORDS  = [""]
COUNTRIES = [""]

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("direct_smartrecruiter")

# ── Constants ────────────────────────────────────────────────────────────────
US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}
STATE_CODES = {"ca", "ny", "tx", "wa", "ma", "il"}

COMMON_SKILLS = [
    "Python", "Java", "C++", "C#", "Go", "Rust", "JavaScript", "TypeScript",
    "React", "Angular", "Vue", "Node.js", "SQL", "NoSQL", "PostgreSQL",
    "MongoDB", "Redis", "AWS", "GCP", "Azure", "Docker", "Kubernetes",
    "Machine Learning", "Golang",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def clean_html_text(text: str) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()] if value else []


def post_time_to_delta(value: str) -> timedelta | None:
    mapping = {
        "hour":  timedelta(hours=1),
        "day":   timedelta(days=1),
        "week":  timedelta(weeks=1),
        "month": timedelta(days=30),
        "year":  timedelta(days=365),
    }
    return mapping.get((value or "any").lower().strip())


def normalize_company_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def extract_work_mode(title: str, location: str, desc: str) -> str:
    content = f"{title} {location} {desc}".lower()
    if re.search(r"\b(remote|wfh|work from home|telecommute)\b", content):
        return "Remote"
    if re.search(r"\bhybrid\b", content):
        return "Hybrid"
    if re.search(r"\b(onsite|in-office|in office|in-person)\b", content):
        return "Onsite"
    return ""


def extract_experience(text: str) -> str:
    m = re.search(
        r"(\d+)\s*(?:-|to|—|–)?\s*(\d+)?\s*(?:\+)?\s*(?:years?|yrs?)(?:\s*of)?\s*(?:experience)",
        text, re.I,
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
        "profiles":  split_csv(job_profile),
        "location":  location.lower().strip(),
        "work_mode": SEARCH_ATTRIBUTES.get("work_mode", "").lower().strip(),
        "job_type":  SEARCH_ATTRIBUTES.get("job_type", "").lower().strip(),
        "experience": SEARCH_ATTRIBUTES.get("experience", "").lower().strip(),
        "skills":    split_csv(SEARCH_ATTRIBUTES.get("skills", "")),
        "excluded":  split_csv(SEARCH_ATTRIBUTES.get("excluded_words", "")),
        "max_age":   post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "any")),
    }


def matches_filters(
    title: str,
    job_location: str,
    desc: str,
    released_date: str | None,
    filters: dict[str, Any],
    now_utc: datetime,
) -> bool:
    if not title:
        return False

    text = f"{title} {job_location} {desc}".lower()

    if filters["profiles"] and not any(p in title.lower() for p in filters["profiles"]):
        return False

    loc = filters["location"]
    if loc:
        if loc in {"usa", "united states", "us"}:
            loc_lower = job_location.lower()
            if not (
                any(st in loc_lower for st in US_STATES)
                or any(re.search(rf"\b{code}\b", loc_lower) for code in STATE_CODES)
                or re.search(r"\b(us|usa|united states)\b", loc_lower)
            ):
                return False
        elif loc not in text:
            return False

    if filters["work_mode"]  and filters["work_mode"]  not in text: return False
    if filters["job_type"]   and filters["job_type"]   not in text: return False
    if filters["experience"] and filters["experience"] not in text: return False
    if filters["skills"]     and not all(s in text for s in filters["skills"]): return False
    if filters["excluded"]   and any(e in text for e in filters["excluded"]): return False

    max_age = filters["max_age"]
    if max_age and released_date:
        try:
            pub = datetime.fromisoformat(released_date.replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if now_utc - pub > max_age:
                return False
        except ValueError:
            pass

    return True


def description_text(job: dict[str, Any]) -> str:
    sections = (job.get("jobAd") or {}).get("sections") or {}
    return clean_html_text(" ".join(
        section.get("text", "") for section in sections.values() if isinstance(section, dict)
    ) or job.get("snippet") or "")


def normalize_job(job: dict[str, Any], company_slug: str) -> dict[str, Any]:
    title        = (job.get("name") or "").strip()
    loc_obj      = job.get("location") or {}
    job_location = " ".join(filter(None, [
        loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country")
    ])).strip()
    desc         = description_text(job)
    released     = job.get("releasedDate") or ""

    # Salary from custom fields
    custom_fields = job.get("customField") or []
    min_sal = max_sal = ""
    custom_wm = ""
    for cf in custom_fields:
        lbl = cf.get("fieldLabel", "").lower()
        val = cf.get("valueLabel", "") or ""
        if "ways of working" in lbl:
            if   "remote" in val.lower(): custom_wm = "Remote"
            elif "hybrid" in val.lower(): custom_wm = "Hybrid"
            elif "onsite" in val.lower(): custom_wm = "Onsite"
        elif "min. salary" in lbl and val: min_sal = val
        elif "max. salary" in lbl and val: max_sal = val

    salary = f"{min_sal} - {max_sal}" if min_sal and max_sal else (min_sal or max_sal)
    if not salary:
        m = re.search(r"(?:[$£€])[\d,]+[kK]?\s*(?:-|to)?\s*(?:[$£€])?[\d,]+[kK]?", desc)
        salary = m.group(0) if m else ""

    structured_mode = "Hybrid" if loc_obj.get("hybrid") else "Remote" if loc_obj.get("remote") else ""
    work_mode = custom_wm or structured_mode or extract_work_mode(title, job_location, desc)

    job_id  = job.get("id") or title
    raw_key = f"smartrecruiters:{company_slug}:{job_id}"
    dedupe_key = hashlib.md5(raw_key.encode()).hexdigest()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return enrich_raw_job({
        "id":          dedupe_key,
        "job_title":   title,
        "company":     (job.get("company") or {}).get("name") or normalize_company_name(company_slug),
        "location":    job_location,
        "job_url":     f"https://jobs.smartrecruiters.com/{company_slug}/{job_id}",
        "apply_url":   job.get("applyUrl") or f"https://jobs.smartrecruiters.com/{company_slug}/{job_id}",
        "description": desc,
        "description_raw": "\n\n".join(section.get("text", "") for section in ((job.get("jobAd") or {}).get("sections") or {}).values() if isinstance(section, dict)) or desc,
        "structured_fields": {"work_mode": custom_wm or structured_mode, "job_type": (job.get("typeOfEmployment") or {}).get("label")},
        "salary":      salary,
        "salary_min":  None,
        "salary_max":  None,
        "experience":  extract_experience(desc),
        "skills":      extract_skills(desc),
        "work_mode":   work_mode,
        "source_board": "SmartRecruiters",
        "scraper_type": "api",
        "is_remote":   (work_mode == "Remote"),
        "job_type":    (job.get("typeOfEmployment") or {}).get("label", ""),
        "scraped_at":  now_str,
        "created_at":  released or None,
        "posted_at":   released or None,
        "also_on":     [],
    })


# ── HTTP with retry (same pattern as Ashby) ──────────────────────────────────

async def fetch_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    retries: int = RETRY_COUNT,
) -> Any | None:
    base_delay = 0.5  # Faster first retry
    for attempt in range(retries):
        try:
            resp = await paced_request(client, 'get', url, board='SR', rate=15, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 404:
                return {"is_404_error": True}

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = retry_after_seconds(retry_after)
                if wait is None:
                    wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning("429 for %s, sleeping %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503, 504):
                if attempt < retries - 1:
                    wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    log.warning("Server error %s for %s, retry in %.1fs", resp.status_code, url, wait)
                    await asyncio.sleep(wait)
                    continue
                return None

            resp.raise_for_status()
            return resp.json()

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            if attempt == retries - 1:
                log.warning("Final failure for %s: %s", url, exc)
                return None
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning("Transient error for %s: %s. Retry in %.1fs", url, exc, wait)
            await asyncio.sleep(wait)

        except Exception as exc:
            log.exception("Unexpected error for %s: %s", url, exc)
            return None

    return None


# ── Scraper class (Ashby-style) ───────────────────────────────────────────────

@dataclass
class FetchResult:
    company: str
    jobs: list[dict[str, Any]]
    is_404: bool = False


class DirectSmartrecruiterScraper:
    def __init__(self, companies: list[str]):
        self.companies = list(dict.fromkeys(c.strip() for c in companies if c.strip() and not c.lstrip().startswith('#')))
        self._detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        self.stats = Counter()

    async def _fetch_company_jobs(
        self,
        client: httpx.AsyncClient,
        company: str,
        job_profile: str,
        location: str,
    ) -> FetchResult:
        company_slug = company.strip()
        filters = build_filters(job_profile, location)
        now_utc = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []

        try:
            offset = 0
            limit  = 100
            # An API query built from only the first role loses all other OR roles.
            q_param = ""
            seen_ids = set()

            async def enrich_listing(job):
                title = (job.get("name") or "").strip()
                loc = job.get("location") or {}
                location_text = " ".join(str(loc.get(k) or "") for k in ('city', 'region', 'country'))
                # Only title/date are safe to reject before reading the description.
                early = dict(filters, location="", work_mode="", job_type="", experience="", skills=[], excluded=[])
                if not matches_filters(title, location_text, "", job.get("releasedDate"), early, now_utc):
                    self.stats['filtered_before_detail'] += 1
                    return None
                detail_url = f"https://api.smartrecruiters.com/v1/companies/{company_slug}/postings/{job['id']}"
                async with self._detail_sem:
                    detail = await fetch_json_with_retry(client, detail_url)
                if isinstance(detail, dict) and detail.get('is_404_error'):
                    self.stats['expired_details'] += 1
                    return None
                if isinstance(detail, dict) and detail.get('id'):
                    job = {**job, **detail}
                    self.stats['details_ok'] += 1
                else:
                    self.stats['details_failed'] += 1
                desc = description_text(job)
                normalized = normalize_job(job, company_slug)
                filter_text = ' '.join([desc, normalized['work_mode'], normalized['job_type']])
                if matches_filters(title, location_text, filter_text, job.get('releasedDate'), filters, now_utc):
                    return normalized
                return None

            while True:
                url  = f"https://api.smartrecruiters.com/v1/companies/{company_slug}/postings"
                page = f"{url}?limit={limit}&offset={offset}{q_param}"
                data = await fetch_json_with_retry(client, page)

                if data and isinstance(data, dict) and data.get("is_404_error"):
                    return FetchResult(company, [], is_404=True)

                if data is None:
                    self.stats['list_pages_failed'] += 1
                    break

                content = data.get("content") or []
                fresh = []
                for job in content:
                    if job.get('id') and job['id'] not in seen_ids:
                        seen_ids.add(job['id'])
                        fresh.append(job)
                if content and not fresh:
                    self.stats['repeated_pages'] += 1
                    break
                enriched = await bounded_map(enrich_listing, fresh, DETAIL_CONCURRENCY)
                out.extend(job for job in enriched if job)
                self.stats['list_pages_ok'] += 1

                if len(content) < limit:
                    break
                offset += limit
                # Cap per-company to avoid megacompanies (e.g. Dominos ~25k) blocking others
                if MAX_JOBS_PER_COMPANY > 0 and offset >= MAX_JOBS_PER_COMPANY:
                    log.info("Capped %s at %d jobs (MAX_JOBS_PER_COMPANY)", company, MAX_JOBS_PER_COMPANY)
                    break

            log.debug("Finished %s: found %d jobs", company, len(out))
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

        semaphore = asyncio.Semaphore(max(1, MAX_CONCURRENT_COMPANIES))
        self._detail_sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        self.stats.clear()
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
                    f"SmartRecruiters progress: {completed}/{len(self.companies)} companies "
                    f"({completed*100//len(self.companies)}%) | {rate:.1f} co/s | ETA ~{eta_s//60}m{eta_s%60:02d}s"
                )
            return result

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=600, max_keepalive_connections=300),
            headers=headers,
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        ) as client:
            results = await bounded_map(lambda comp: bounded_fetch(client, comp), self.companies, MAX_CONCURRENT_COMPANIES)
        log.info("SmartRecruiters request stats: %s", dict(self.stats))

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
            out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartrecruiter_404_companies.txt")
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


# ── Supabase writer (Ashby-style batch upsert) ───────────────────────────────

STRIP_FIELDS = {"salary_currency", "also_on"}

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
        clean = [{k: v for k, v in r.items() if k not in STRIP_FIELDS} for r in rows]
        for i in range(0, len(clean), SUPABASE_BATCH_SIZE):
            chunk = clean[i : i + SUPABASE_BATCH_SIZE]
            try:
                await self.client.table(self.table_name).upsert(database_rows(chunk), on_conflict="id").execute()
                log.info("Upserted %d jobs to Supabase", len(chunk))
            except Exception as e:
                log.warning("Failed to upsert job chunk: %s", e)


# ── Company loader ────────────────────────────────────────────────────────────

def load_companies() -> list[str]:
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartrecruiter_companies.txt")
    if not os.path.exists(filepath):
        log.warning("%s not found.", filepath)
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]


# ── Pipeline entry point (called by pipeline.py) ──────────────────────────────

async def run_scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
    companies = load_companies()
    if not companies:
        log.warning("SmartRecruiters: no companies to scrape.")
        return []
    scraper = DirectSmartrecruiterScraper(companies)
    return await scraper.scrape(job_profile, location, max_jobs)


# ── Standalone main ───────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("SmartRecruiters Job Scraper (Ashby-style Direct API)")
    print(f"Keywords : {KEYWORDS}")
    print(f"Countries: {COUNTRIES}")
    print("=" * 60)

    companies = load_companies()
    print(f"Loaded {len(companies)} companies from smartrecruiter_companies.txt")
    if not companies:
        print("No companies to scrape. Exiting.")
        return

    scraper    = DirectSmartrecruiterScraper(companies)
    writer     = AsyncSupabaseWriter()
    await writer.init()

    all_jobs: list[dict[str, Any]] = []
    master_jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for country in COUNTRIES:
        for keyword in KEYWORDS:
            print(f"\n  → '{keyword}' | {country}")
            jobs = await scraper.scrape(keyword, country, 0)
            new = 0
            for job in jobs:
                url = job.get("job_url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                all_jobs.append(job)
                master_jobs.append(job)
                new += 1

            print(f"    ✓ {new} jobs collected")

            if len(all_jobs) >= SUPABASE_BATCH_SIZE:
                await writer.upsert_batch(all_jobs)
                all_jobs = []

    print(f"\n{'=' * 60}")
    print("Scrape complete")

    if all_jobs:
        await writer.upsert_batch(all_jobs)

    total = len(seen_urls)
    if total > 0:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_smartrecruiter_jobs.json")
        # Load existing to merge
        existing = []
        if os.path.exists(out_path):
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                pass
        # Simpler: just dump what we collected this run merged with existing
        existing_urls = {j.get("job_url") for j in existing}
        merged = existing + [j for j in list({j["job_url"]: j for j in master_jobs}.values()) if j.get("job_url") not in existing_urls]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        print(f"Saved to all_smartrecruiter_jobs.json (total: {len(merged)})")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
