import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job
import io
import asyncio
import logging
import os
import re
import json
import httpx
from datetime import datetime, timezone, timedelta
import html as html_lib
import urllib.parse

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "jobvite_jobs"

MAX_CONCURRENT_JOBS = 100

# ──────────────────────────────────────────────────────────────
#  SEARCH ATTRIBUTES — edit these values before running
# ──────────────────────────────────────────────────────────────
SEARCH_ATTRIBUTES = {
    "job_profile": "", # Comma-separated profiles; left blank so this board is unfiltered like every other Type A board
    "location": "",                # Example: usa, India, Bengaluru, London
    "skills": "",            # Comma-separated keywords
    "experience": "",        # Example: entry level, senior
    "job_type": "",            # Example: full time, internship, contract
    "work_mode": "",              # remote, hybrid, onsite, or blank for any
    "post_time": "day",  # "hour" | "day" | "week" | "month" | "year" | "any"
    "excluded_words": "",# Comma-separated words to exclude
}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("direct_jobvite")

def post_time_to_delta(value: str) -> timedelta | None:
    value = (value or "any").lower().strip()
    mapping = {
        "hour":  timedelta(hours=1),
        "day":   timedelta(days=1),
        "week":  timedelta(weeks=1),
        "month": timedelta(days=30),
        "year":  timedelta(days=365),
    }
    return mapping.get(value)

def clean_html_text(text: str) -> str:
    text = html_lib.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

class DirectJobviteScraper:
    def __init__(self, companies: list[str]):
        self.companies = companies

    async def _fetch_company_jobs_list(self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, company: str) -> list[dict]:
        async with semaphore:
            url = f"https://jobs.jobvite.com/{company}"
            log.info(f"Fetching job list for {company} at {url}")
            try:
                response = await client.get(url, timeout=15)
                if response.status_code == 404:
                    return [{"is_404_error": True}]
                response.raise_for_status()
                
                html_body = response.text
                jobs = []
                
                profiles_str = SEARCH_ATTRIBUTES.get("job_profile", "")
                profiles = [p.strip().lower() for p in profiles_str.split(",") if p.strip()]
                
                # Find all job links and titles
                # Jobvite typical link: <a href="/company/job/o12345">Software Engineer</a>
                # or <a href="/job/o12345">Software Engineer</a>
                links = re.findall(r'<a[^>]+href=["\'](/[^"\']*/job/[^"\']+)["\'][^>]*>(.*?)</a>', html_body, re.IGNORECASE)
                
                seen_links = set()
                for href, title_html in links:
                    if href in seen_links:
                        continue
                    seen_links.add(href)
                    
                    title = clean_html_text(title_html)
                    match = not profiles or any(p in title.lower() for p in profiles)
                    
                    if match and title:
                        full_url = f"https://jobs.jobvite.com{href}"
                        # NOTE: work_mode is NOT computed here. At this list-parsing stage only
                        # the title is known (no location/description yet -- those are fetched
                        # per-job in _scrape_job_details), so a title-only signal would be weak
                        # and is discarded anyway: _scrape_job_details recomputes work_mode from
                        # the real title+location+description once it has fetched them, and that
                        # recomputed value is what actually ships in the final job dict.
                        jobs.append({
                            "job_title": title,
                            "company": company.replace("-", " ").title(),
                            "job_url": full_url,
                        })
                return jobs
            except Exception as e:
                log.warning(f"Failed to fetch or parse job list for {company}: {e}")
                return []

    async def _scrape_job_details(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        job: dict
    ) -> dict | None:
        async with semaphore:
            url = job["job_url"]
            log.info(f"  Fetching details for: {job['job_title']} at {job['company']}")
            try:
                response = await client.get(url, timeout=15)
                response.raise_for_status()
                html_body = response.text
                
                desc_match = re.search(r'<div[^>]*class=["\'][^"\']*jv-job-detail-description[^"\']*["\'][^>]*>(.*?)</div>', html_body, re.DOTALL | re.IGNORECASE)
                if not desc_match:
                    desc_match = re.search(r'<article[^>]*class=["\'][^"\']*jv-job-detail-description[^"\']*["\'][^>]*>(.*?)</article>', html_body, re.DOTALL | re.IGNORECASE)
                if not desc_match:
                    desc_match = re.search(r'<div[^>]*class=["\'][^"\']*jv-wrapper[^"\']*["\'][^>]*>(.*?)</div>', html_body, re.DOTALL | re.IGNORECASE)

                job_description = clean_html_text(desc_match.group(1)) if desc_match else ""
                
                meta_match = re.search(r'<p[^>]*class=["\'][^"\']*jv-job-detail-meta[^"\']*["\'][^>]*>(.*?)</p>', html_body, re.DOTALL | re.IGNORECASE)
                job_meta = clean_html_text(meta_match.group(1)) if meta_match else ""
                
                text_to_search = (job['job_title'] + " " + job_meta + " " + job_description).lower()
                
                loc = SEARCH_ATTRIBUTES.get("location", "").lower().strip()
                if loc and loc not in text_to_search:
                    return None
                    
                wm = SEARCH_ATTRIBUTES.get("work_mode", "").lower().strip()
                if wm and wm not in text_to_search:
                    return None
                    
                jt = SEARCH_ATTRIBUTES.get("job_type", "").lower().strip()
                if jt and jt not in text_to_search:
                    return None
                    
                exp = SEARCH_ATTRIBUTES.get("experience", "").lower().strip()
                if exp and exp not in text_to_search:
                    return None
                    
                skills = SEARCH_ATTRIBUTES.get("skills", "")
                if skills:
                    skill_list = [s.strip().lower() for s in skills.split(",") if s.strip()]
                    if skill_list and not all(s in text_to_search for s in skill_list):
                        return None
                        
                excluded = SEARCH_ATTRIBUTES.get("excluded_words", "")
                if excluded:
                    ex_list = [e.strip().lower() for e in excluded.split(",") if e.strip()]
                    if ex_list and any(e in text_to_search for e in ex_list):
                        return None

                now = datetime.now(timezone.utc).isoformat()
                
                # Extract Salary
                salary = ""
                sal_match = re.search(r'(?:[\$£€])[\d,]+[kK]?\s*(?:-|to|—|–|&mdash;|&ndash;)\s*(?:[\$£€])?[\d,]+[kK]?|(?:[\$£€])[\d,]+[kK]', job_description)
                if sal_match:
                    salary = sal_match.group(0)

                # Extract Experience
                experience = ""
                exp_match = re.search(r'(\d+)\s*(?:-|to|—|–|&mdash;|&ndash;)?\s*(\d+)?\s*(?:\+)?\s*(?:years?|yrs?)(?:\s*of)?\s*(?:experience)', job_description, re.IGNORECASE)
                if exp_match:
                    experience = exp_match.group(0)

                # Extract basic Skills
                found_skills = []
                common_skills = ["Python", "Java", "C\\+\\+", "Go", "Rust", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node", "SQL", "NoSQL", "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Machine Learning", "Golang"]
                for sk in common_skills:
                    if re.search(rf'\b{sk}\b', job_description, re.IGNORECASE):
                        found_skills.append(sk.replace("\\+", "+"))
                skills_str = ", ".join(found_skills)

                # Find apply URL
                apply_match = re.search(r'<a[^>]+href=["\']([^"\']+/apply[^"\']*)["\']', html_body, re.IGNORECASE)
                apply_url = f"https://jobs.jobvite.com{apply_match.group(1)}" if apply_match and apply_match.group(1).startswith('/') else (apply_match.group(1) if apply_match else url)

                # Extract location + datePosted from JSON-LD
                job_location = ""
                date_posted = None
                ld_data = {}
                jsonld = re.search(r'<script[^>]*type=["\'](application/ld\+json)["\'][^>]*>(.*?)</script>', html_body, re.DOTALL | re.IGNORECASE)
                if jsonld:
                    try:
                        ld_data = json.loads(jsonld.group(2))
                        date_posted = ld_data.get('datePosted')  # e.g. "2026-07-30"
                        if 'jobLocation' in ld_data:
                            loc_data = ld_data['jobLocation']
                            if isinstance(loc_data, list) and len(loc_data) > 0:
                                loc_data = loc_data[0]
                            if isinstance(loc_data, dict) and 'address' in loc_data:
                                addr = loc_data['address']
                                parts = []
                                if addr.get('addressLocality'): parts.append(addr['addressLocality'])
                                if addr.get('addressRegion'): parts.append(addr['addressRegion'])
                                if addr.get('addressCountry'): parts.append(addr['addressCountry'])
                                job_location = ", ".join(parts)
                    except:
                        pass
                if not job_location:
                    job_location = job_meta

                # Apply post_time filter using datePosted from JSON-LD
                max_age = post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "any"))
                if max_age and date_posted:
                    try:
                        published = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) - published > max_age:
                            return None  # skip job older than max_age
                    except ValueError:
                        pass
                

                # --- Inject work_mode extraction ---
                _content = (job["job_title"] + " " + job_location + " " + job_description).lower()
                work_mode = ""
                if re.search(r'\b(remote|wfh|work from home)\b', _content):
                    work_mode = "Remote"
                elif re.search(r'\b(hybrid)\b', _content):
                    work_mode = "Hybrid"
                elif re.search(r'\b(onsite|in-office|in office|in-person)\b', _content):
                    work_mode = "Onsite"
                # -----------------------------------
                return enrich_raw_job({
                    "source_board": "Jobvite",
                    "baseSalary": ld_data.get("baseSalary") if isinstance(ld_data, dict) else None,
                    "structured_fields": {"job_type": ld_data.get("employmentType") if isinstance(ld_data, dict) else None},
                    "work_mode": work_mode,
                    "job_title": job["job_title"],
                    "company": job["company"],
                    "job_url": job["job_url"],
                    "apply_url": apply_url,
                    "location": job_location,
                    "description": job_description,
                    "salary": salary,
                    "experience": experience,
                    "skills": skills_str,
                    "timestamp": now,
                    "created_at": date_posted or now,
                })
            except Exception as e:
                log.error(f"  Error fetching job details for {url}: {e}")
                return None

    async def scrape(self) -> list[dict]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        }
        all_jobs = []
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=500, max_keepalive_connections=500), headers=headers, follow_redirects=True, timeout=20) as client:
            list_semaphore = asyncio.Semaphore(100)
            list_tasks = [self._fetch_company_jobs_list(client, list_semaphore, comp) for comp in self.companies]
            company_results = await asyncio.gather(*list_tasks)
            
            flat_jobs = []
            missing_companies = []
            for i, comp_jobs in enumerate(company_results):
                if comp_jobs and comp_jobs[0].get("is_404_error"):
                    missing_companies.append(self.companies[i])
                else:
                    for job in comp_jobs:
                        flat_jobs.append(job)
            log.info(f"Found {len(flat_jobs)} jobs matching '{SEARCH_ATTRIBUTES['job_profile']}' across {len(self.companies)} companies.")
            
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
            detail_tasks = [self._scrape_job_details(client, semaphore, job) for job in flat_jobs]
            detailed_jobs = await asyncio.gather(*detail_tasks)
            
            for d_job in detailed_jobs:
                if d_job is not None:
                    all_jobs.append(d_job)
                    
            if missing_companies:
                out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobvite_404_companies.txt")
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

        return all_jobs

class SupabaseUpserter:
    def __init__(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            # log.warning("SUPABASE_URL and SUPABASE_KEY must be set in .env")
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
    print("Direct Jobvite Job Scraper")
    print("=" * 60)

    try:
        with open("jobvite_companies.txt", "r") as f:
            companies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        print("Error: jobvite_companies.txt not found. Please create it and add company subdomains.")
        return

    if not companies:
        print("Error: No companies found in jobvite_companies.txt")
        return

    print(f"Loaded {len(companies)} companies to scrape.")
    print(f"Target Job Profiles (Expanded): {SEARCH_ATTRIBUTES['job_profile']}")
    print("=" * 60)

    scraper = DirectJobviteScraper(companies)
    upserter = SupabaseUpserter()
    
    results = await scraper.scrape()

    print(f"\n{'=' * 60}")
    print(f"PIPELINE SUMMARY: Total jobs extracted: {len(results)}")
    print(f"{'=' * 60}")

    if results:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_jobvite_jobs.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=4)
        print(f"Saved {len(results)} jobs to all_jobvite_jobs.json")
        # upserter.upsert(results)

if __name__ == "__main__":
    asyncio.run(main())

# ──────────────────────────────────────────────────────────────
#  IMPORTABLE ENTRY POINT (used by pipeline.py)
# ──────────────────────────────────────────────────────────────
async def scrape(job_profile: str = "", location: str = "", max_jobs: int = 99999) -> list[dict]:
    import os
    filepath = os.path.join(os.path.dirname(__file__), "jobvite_companies.txt")
    companies = []
    try:
        with open(filepath, "r") as f:
            companies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        pass
    if not companies:
        return []
    scraper = DirectJobviteScraper(companies)
    return await scraper.scrape()
