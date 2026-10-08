import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import logging
import re
import json
import random
import httpx
from datetime import datetime, timezone, timedelta
import html as html_lib

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

try:
    from supabase import create_client
except ImportError:
    create_client = None

try:
    from scraper_utils import enrich_raw_job
except ImportError:
    def enrich_raw_job(job: dict) -> dict:
        return job

_HAS_TENANT_MODULE = True
try:
    try:
        from . import workable_jobs_board_api
    except ImportError:
        import workable_jobs_board_api
except ImportError:
    _HAS_TENANT_MODULE = False
    workable_jobs_board_api = None

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "workable_jobs"
WORKABLE_SOURCE = os.getenv("WORKABLE_SOURCE", "global").lower().strip()

SEARCH_ATTRIBUTES = {
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
log = logging.getLogger("workable_scraper")

def clean_html_text(text: str) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

US_STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming"]
US_STATE_CODES_RE = re.compile(r'\b(?:ca|ny|tx|wa|ma|il)\b')
SALARY_RE = re.compile(r'[$£€][\d,]+[kK]?\s*(?:-|to|—|–)\s*[$£€]?[\d,]+[kK]?|[$£€][\d,]+[kK]')
EXPERIENCE_RE = re.compile(r'(\d+)\s*(?:-|to|—|–)?\s*(\d+)?\s*(?:\+)?\s*(?:years?|yrs?)(?:\s*of)?\s*(?:experience)', re.IGNORECASE)
COMMON_SKILLS = ["Python", "Java", "C\\+\\+", "Go", "Rust", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node", "SQL", "NoSQL", "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Machine Learning", "Golang"]
SKILL_RES = [(sk.replace("\\+", "+"), re.compile(rf'\b{sk}\b', re.IGNORECASE)) for sk in COMMON_SKILLS]


class AdaptiveRateLimiter:
    """Global rate limiter shared by all request tasks.

    - Paces requests to at most `rate` req/s (spacing, not bursts).
    - On 429, pauses ALL tasks (honoring Retry-After if present) and
      shrinks the rate. Successes slowly grow it back.
    """
    def __init__(self, rate: float = 1.0, min_rate: float = 0.3, max_rate: float = 5.0):
        self.rate = rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self._next_slot = 0.0
        self._lock = asyncio.Lock()
        self._resume_event = asyncio.Event()
        self._resume_event.set()

    async def acquire(self):
        while True:
            await self._resume_event.wait()
            async with self._lock:
                if not self._resume_event.is_set():
                    continue  # a 429 pause landed while we held the lock queue
                now = asyncio.get_event_loop().time()
                wait = self._next_slot - now
                self._next_slot = max(now, self._next_slot) + (1.0 / self.rate)
            if wait > 0:
                await asyncio.sleep(wait)
            return

    def on_success(self):
        # Slowly recover throughput (additive increase)
        self.rate = min(self.max_rate, self.rate + 0.05)

    async def on_429(self, retry_after: float | None):
        # Multiplicative decrease + global pause
        self.rate = max(self.min_rate, self.rate * 0.5)
        if self._resume_event.is_set():
            pause = retry_after if retry_after else 15.0
            pause = min(pause, 120.0)
            log.warning(f"429 received — pausing all requests for {pause:.0f}s, rate now {self.rate:.1f} req/s")
            self._resume_event.clear()
            await asyncio.sleep(pause)
            # Stagger the first slot so workers can't burst after resume
            self._next_slot = asyncio.get_event_loop().time() + (1.0 / self.rate)
            self._resume_event.set()
        else:
            # Another task is already handling the pause; just wait it out
            await self._resume_event.wait()


class OptimizedWorkableScraper:
    """Optimized Workable scraper using a 2-phase approach.
    Phase 1: Hits the lightweight v1 widget API to get titles/dates.
    Phase 2: Only fetches the markdown description for jobs that pass the title/date filter.
    All requests share one adaptive rate limiter so a 429 throttles everything globally.
    """
    LIST_CONCURRENCY = 1   # Fully serial — Workable bans concurrent access hard
    DETAIL_CONCURRENCY = 1
    MAX_RETRIES = 5

    def __init__(self, companies: list[str]):
        self.companies = companies
        self.limiter = AdaptiveRateLimiter()

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float | None:
        val = resp.headers.get("Retry-After")
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        """Rate-limited GET with retries and exponential backoff on 429/5xx/transport errors."""
        for attempt in range(self.MAX_RETRIES + 1):
            await self.limiter.acquire()
            try:
                resp = await client.get(url)
            except httpx.TransportError as e:
                if attempt == self.MAX_RETRIES:
                    log.debug(f"Transport error for {url}: {e}")
                    return None
                await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                continue

            if resp.status_code == 429:
                await self.limiter.on_429(self._parse_retry_after(resp))
                if attempt == self.MAX_RETRIES:
                    return None
                continue
            if resp.status_code >= 500:
                if attempt == self.MAX_RETRIES:
                    return None
                await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                continue

            self.limiter.on_success()
            return resp
        return None

    async def _fetch_list(self, client: httpx.AsyncClient, company: str, profiles: list[str], max_age: timedelta | None, now_utc: datetime) -> list[dict]:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{company}"
        try:
            resp = await self._get(client, url)
            if resp is None or resp.status_code in (403, 401):
                return []
            if resp.status_code == 404:
                return [{"is_404": True, "company": company}]

            resp.raise_for_status()
            data = resp.json()
            
            jobs = []
            for job in data.get("jobs", []):
                title = job.get("title", "")
                if not title:
                    continue
                
                # Fast Title Filter
                if profiles and not any(p in title.lower() for p in profiles):
                    continue
                    
                # Fast Date Filter
                pub_date = job.get("published_on")
                if max_age and pub_date:
                    try:
                        dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if now_utc - dt > max_age:
                            continue
                    except ValueError:
                        pass
                
                loc_obj = job.get("location", {})
                loc_str = f"{loc_obj.get('city', '')} {loc_obj.get('region', '')} {loc_obj.get('country', '')}".strip()
                
                jobs.append({
                    "id": job.get("shortcode", ""),
                    "job_title": title,
                    "company": company,
                    "location": loc_str,
                    "job_url": job.get("url", ""),
                    "created_at": pub_date,
                    "department": job.get("department", "")
                })
            return jobs
        except Exception as e:
            log.debug(f"Skipping {company} list: {e}")
            return []

    async def _fetch_detail(self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, job: dict, filters: dict, now_utc: datetime) -> dict | None:
        async with semaphore:
            shortcode = job["id"]
            company = job["company"]

            # Fetch the markdown description endpoint (much lighter than v3)
            md_url = f"https://apply.workable.com/{company}/jobs/view/{shortcode}.md"
            try:
                resp = await self._get(client, md_url)
                if resp is None or resp.status_code != 200:
                    return None

                desc_text = resp.text
                
                # Check remaining filters (location, work mode, skills, etc)
                text_to_search = f"{job['job_title']} {job['location']} {desc_text}".lower()
                
                loc_filter = filters["location"]
                if loc_filter in ["usa", "united states", "us"]:
                    if any(st in text_to_search for st in US_STATES) or US_STATE_CODES_RE.search(text_to_search):
                        text_to_search += " usa united states"
                
                if loc_filter and loc_filter not in text_to_search: return None
                if filters["work_mode"] and filters["work_mode"] not in text_to_search: return None
                if filters["job_type"] and filters["job_type"] not in text_to_search: return None
                if filters["experience"] and filters["experience"] not in text_to_search: return None
                if filters["skills"] and not all(s in text_to_search for s in filters["skills"]): return None
                if filters["excluded"] and any(e in text_to_search for e in filters["excluded"]): return None

                # Extract Salary
                salary = ""
                sal_match = SALARY_RE.search(desc_text)
                if sal_match:
                    salary = sal_match.group(0)

                # Extract Experience
                experience = ""
                exp_match = EXPERIENCE_RE.search(desc_text)
                if exp_match:
                    experience = exp_match.group(0)

                # Extract Skills
                found_skills = [name for name, rx in SKILL_RES if rx.search(desc_text)]
                skills_str = ", ".join(found_skills)

                # Determine Work Mode
                work_mode = ""
                if re.search(r'\b(remote|wfh|work from home)\b', text_to_search): work_mode = "Remote"
                elif re.search(r'\b(hybrid)\b', text_to_search): work_mode = "Hybrid"
                elif re.search(r'\b(onsite|in-office|in-person)\b', text_to_search): work_mode = "Onsite"

                return enrich_raw_job({
                    "work_mode": work_mode,
                    "job_title": job["job_title"],
                    "company": job["company"].replace("-", " ").title(),
                    "location": job["location"],
                    "job_url": job["job_url"],
                    "apply_url": job["job_url"],
                    "description": desc_text,
                    "salary": salary,
                    "experience": experience,
                    "skills": skills_str,
                    "source_board": "Workable",
                    "scraper_type": "api",
                    "is_remote": (work_mode == "Remote"),
                    "job_type": "full_time",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "created_at": job["created_at"] or datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                log.debug(f"Error fetching detail for {shortcode}: {e}")
                return None

    async def scrape(self, job_profile: str, location: str, max_jobs: int = 99999) -> list[dict]:
        profiles = [p.strip().lower() for p in job_profile.split(",") if p.strip()]
        loc_filter = location.lower().strip()
        
        post_time_str = SEARCH_ATTRIBUTES.get("post_time", "any").lower().strip()
        now_utc = datetime.now(timezone.utc)
        max_age = None
        if post_time_str == "hour": max_age = timedelta(hours=1)
        elif post_time_str == "day": max_age = timedelta(days=1)
        elif post_time_str == "week": max_age = timedelta(weeks=1)
        elif post_time_str == "month": max_age = timedelta(days=30)
        
        filters = {
            "location": loc_filter,
            "work_mode": SEARCH_ATTRIBUTES.get("work_mode", "").lower().strip(),
            "job_type": SEARCH_ATTRIBUTES.get("job_type", "").lower().strip(),
            "experience": SEARCH_ATTRIBUTES.get("experience", "").lower().strip(),
            "skills": [s.strip().lower() for s in SEARCH_ATTRIBUTES.get("skills", "").split(",") if s.strip()],
            "excluded": [e.strip().lower() for e in SEARCH_ATTRIBUTES.get("excluded_words", "").split(",") if e.strip()]
        }

        all_jobs = []
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"
        }

        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=30, max_keepalive_connections=20), headers=headers, follow_redirects=True, timeout=20) as client:
            list_sem = asyncio.Semaphore(self.LIST_CONCURRENCY)

            async def bounded_list(comp):
                async with list_sem:
                    return await self._fetch_list(client, comp, profiles, max_age, now_utc)

            list_tasks = [bounded_list(c) for c in self.companies]
            company_results = await asyncio.gather(*list_tasks, return_exceptions=True)
            
            flat_jobs = []
            missing_companies = []
            for res in company_results:
                if isinstance(res, list):
                    if len(res) == 1 and res[0].get("is_404"):
                        missing_companies.append(res[0]["company"])
                        continue
                    flat_jobs.extend(res)
                    
            log.info(f"Workable Phase 1: Found {len(flat_jobs)} jobs matching title/date across {len(self.companies)} companies.")
            
            detail_sem = asyncio.Semaphore(self.DETAIL_CONCURRENCY)
            detail_tasks = [self._fetch_detail(client, detail_sem, job, filters, now_utc) for job in flat_jobs]
            
            detailed_results = await asyncio.gather(*detail_tasks, return_exceptions=True)
            
            for res in detailed_results:
                if isinstance(res, dict) and res:
                    all_jobs.append(res)
                    
            if missing_companies:
                out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workable_404_companies.txt")
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

class SupabaseUpserter:
    def __init__(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            self.client = None
            return
        self.client = create_client(SUPABASE_URL, SUPABASE_KEY)

    def upsert(self, jobs: list[dict]):
        if not self.client or not jobs:
            return
        inserted = skipped = 0
        try:
            urls = [j["job_url"] for j in jobs]
            existing_urls = set()
            # Batch the existence check to avoid one round-trip per job
            for i in range(0, len(urls), 200):
                res = self.client.table(SUPABASE_TABLE).select("job_url").in_("job_url", urls[i:i+200]).execute()
                existing_urls.update(row["job_url"] for row in (res.data or []))

            seen = set()
            new_jobs = []
            for job in jobs:
                url = job["job_url"]
                if url in existing_urls or url in seen:
                    skipped += 1
                    continue
                seen.add(url)
                new_jobs.append(job)

            for i in range(0, len(new_jobs), 100):
                batch = new_jobs[i:i+100]
                self.client.table(SUPABASE_TABLE).insert(batch).execute()
                inserted += len(batch)
        except Exception as e:
            log.error(f"Supabase upsert error: {e}")
        log.info(f"  DONE => Inserted: {inserted} | Skipped: {skipped}")

def load_companies() -> list[str]:
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workable_companies.txt")
    if not os.path.exists(filepath): return []
    with open(filepath, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]

async def scrape(
    job_profile: str,
    location: str,
    max_jobs: int = 99999,
    write_queue: asyncio.Queue | None = None,
) -> list[dict]:
    if WORKABLE_SOURCE != "tenant":
        workable_jobs_board_api.SEARCH_ATTRIBUTES.update(SEARCH_ATTRIBUTES)
        scraper = workable_jobs_board_api.WorkableJobsBoardScraper()
        return await scraper.scrape(
            job_profile,
            location,
            max_jobs,
            write_queue=write_queue,
        )

    companies = load_companies()
    if not companies: return []
    scraper = OptimizedWorkableScraper(companies)
    jobs = await scraper.scrape(job_profile, location, max_jobs)
    if write_queue is not None and jobs:
        for i in range(0, len(jobs), 500):
            await write_queue.put(jobs[i:i + 500])
    return jobs

async def main():
    print("=" * 60)
    print("Workable Job Scraper (Public Global API)")
    print(f"Source: {WORKABLE_SOURCE} API | post_time={SEARCH_ATTRIBUTES['post_time']}")
    print("=" * 60)
    all_jobs = []
    
    for country in COUNTRIES:
        for keyword in KEYWORDS:
            jobs = await scrape(keyword, country, 0)
            for j in jobs:
                j["role_category"] = keyword
                j["country"] = country
                all_jobs.append(j)
                
    if all_jobs:
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_workable_jobs.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_jobs, f, indent=4)
        print(f"Saved {len(all_jobs)} jobs to {output_path}.")
        upserter = SupabaseUpserter()
        upserter.upsert(all_jobs)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user.")
