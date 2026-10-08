from __future__ import annotations
import asyncio
import httpx
import json
import csv
import logging
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job, clean_text
from scraper_runtime import RequestGate, bounded_map, retry_after_seconds
from collections import Counter
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

# Setup simple logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger(__name__)

SEARCH_ATTRIBUTES: dict = {
    "post_time": "day",  # "hour" | "day" | "week" | "month" | "year" | "any"
}

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

API_ROOT = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
CAREERS_PATH = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"

PAGE_LIMIT = 100
WORKER_COUNT = max(1, int(os.getenv("ADP_COMPANY_CONCURRENCY", "5")))
DETAIL_CONCURRENCY = max(1, int(os.getenv("ADP_DETAIL_CONCURRENCY", "8")))
RETRY_COUNT = max(1, int(os.getenv("ADP_RETRY_COUNT", "4")))

# Tracking 404s
DIR_PATH = os.path.dirname(os.path.abspath(__file__))
TRACKING_FILE = os.path.join(DIR_PATH, "adp_404_companies.txt")
CSV_FILE = os.path.join(DIR_PATH, "adp.csv")
OUTPUT_FILE = os.path.join(DIR_PATH, "all_adp_jobs.json")

def load_404s() -> dict[str, int]:
    counts = {}
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.rsplit(",", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    counts[parts[0]] = int(parts[1])
                else:
                    counts[line] = 1
    return counts

def save_404s(counts: dict[str, int]):
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        for comp, count in sorted(counts.items()):
            f.write(f"{comp},{count}\n")

def load_companies() -> list[dict]:
    companies = []
    if not os.path.exists(CSV_FILE):
        log.error(f"{CSV_FILE} not found!")
        return []
        
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "slug" in row and "/" in row["slug"]:
                companies.append(row)
    return companies

def normalize_adp_job(item: dict, company: dict) -> dict:
    cid, ccid = company["slug"].split("/", 1)
    item_id = str(item.get("itemID") or "")
    url = f"{CAREERS_PATH}?{urlencode(dict(cid=cid, ccId=ccid, lang='en_US', jobId=item_id, source='CC2'))}"
    locations = [loc.get("nameCode", {}).get("shortName", "")
                 for loc in item.get("requisitionLocations", []) if isinstance(loc, dict)]
    posted = item.get("postDate")
    created = None
    if posted:
        try:
            date = datetime.fromisoformat(posted.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            created = date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            pass
    pay = item.get("payGradeRange") or {}
    minimum = pay.get("minimumRate") or {}
    maximum = pay.get("maximumRate") or {}
    return enrich_raw_job({
        "id": f"adp:{cid}:{item_id}", "job_title": item.get("requisitionTitle", ""),
        "company": company["name"], "location": "; ".join(filter(None, locations)),
        "job_url": url, "apply_url": url,
        "description": clean_text(item.get("requisitionDescription") or ""),
        "description_raw": item.get("requisitionDescription") or "",
        "structured_fields": {"job_type": (item.get("workLevelCode") or {}).get("shortName"),
            "salary": {"min": minimum.get("amountValue"), "max": maximum.get("amountValue"),
                       "currency": minimum.get("currencyCode") or maximum.get("currencyCode")}},
        "source_board": "ADP", "scraper_type": "api",
        "work_mode": "", "job_type": (item.get("workLevelCode") or {}).get("shortName", ""),
        "salary_min": minimum.get("amountValue"), "salary_max": maximum.get("amountValue"),
        "salary_currency": minimum.get("currencyCode") or maximum.get("currencyCode"),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": created, "posted_at": posted, "also_on": [],
    })


class AdpRequestError(RuntimeError):
    pass


async def request_adp(client, url, params, gate, sem, stats):
    for attempt in range(RETRY_COUNT):
        try:
            async with sem:
                await gate.acquire()
                resp = await client.get(url, params=params, headers={
                    "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"
                }, timeout=25)
            stats[f"status_{resp.status_code}"] += 1
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                await gate.on_throttle(retry_after_seconds(resp.headers.get("Retry-After")))
            elif resp.status_code >= 500:
                pass
            else:
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("ADP returned an unexpected JSON schema")
                gate.on_success()
                return data
        except (httpx.TransportError, ValueError) as exc:
            stats[type(exc).__name__] += 1
        if attempt + 1 < RETRY_COUNT:
            await asyncio.sleep(0.5 * 2 ** attempt)
    stats["requests_exhausted"] += 1
    raise AdpRequestError(f"ADP request failed after {RETRY_COUNT} attempts: {url}")


async def fetch_adp_jobs(client: httpx.AsyncClient, company: dict, gate=None, sem=None, stats=None) -> list[dict]:
    gate = gate if gate is not None else RequestGate(4, 0.5, 4, cooldown=10)
    sem = sem if sem is not None else asyncio.Semaphore(DETAIL_CONCURRENCY)
    stats = stats if stats is not None else Counter()
    cid, ccid = company["slug"].split("/", 1)
    params = dict(cid=cid, ccId=ccid, lang="en_US", locale="en_US")
    jobs, seen = [], set()
    offset = 0
    max_age = post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "any"))

    async def detail(item):
        if not item.get("requisitionDescription"):
            data = await request_adp(client, f"{API_ROOT}/{item['itemID']}", params, gate, sem, stats)
            if data is None:
                stats["expired_details"] += 1
                return None
            item = {**item, **data}
        stats["details_ok"] += 1
        return normalize_adp_job(item, company)

    while True:
        data = await request_adp(client, API_ROOT, {**params, "$top": PAGE_LIMIT, "$skip": offset}, gate, sem, stats)
        if data is None:
            if offset == 0:
                return [{"is_404": True}]
            raise AdpRequestError(f"ADP pagination disappeared for {company['name']} at {offset}")
        if not isinstance(data.get("jobRequisitions"), list):
            raise AdpRequestError("ADP response lacks jobRequisitions")
        items = data["jobRequisitions"]
        total = (data.get("meta") or {}).get("totalNumber")
        if not items:
            if total is not None and offset < int(total):
                raise AdpRequestError("ADP returned an empty page before its advertised total")
            break
        selected = []
        fresh = 0
        for item in items:
            item_id = item.get("itemID")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            fresh += 1
            if max_age and item.get("postDate"):
                try:
                    date = datetime.fromisoformat(item["postDate"].replace("Z", "+00:00"))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - date > max_age:
                        stats["old_postings"] += 1
                        continue
                except (ValueError, AttributeError):
                    pass
            selected.append(item)
        if not fresh:
            raise AdpRequestError("ADP repeated a page without pagination progress")
        jobs.extend(j for j in await bounded_map(detail, selected, DETAIL_CONCURRENCY) if j)
        offset += len(items)
        if total is not None and offset >= int(total):
            break
    return jobs

async def worker(queue: asyncio.Queue, client: httpx.AsyncClient, all_jobs: list, counts_404: dict, gate, sem, stats):
    while True:
        try:
            company = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
            
        jobs = await fetch_adp_jobs(client, company, gate, sem, stats)
        
        if jobs and jobs[0].get("is_404"):
            counts_404[company["slug"]] = counts_404.get(company["slug"], 0) + 1
            log.info(f"Finished {company['name']}: HTTP 404")
        else:
            if company["slug"] in counts_404:
                del counts_404[company["slug"]]
            all_jobs.extend(jobs)
            log.info(f"Finished {company['name']}: found {len(jobs)} jobs")
            
        queue.task_done()

class DirectAdpScraper:
    def __init__(self, companies: list[dict]):
        self.companies = companies

    async def scrape(self, write_queue: asyncio.Queue | None = None) -> list[dict]:
        counts_404 = load_404s()
        
        # Filter out companies with >= 3 strikes
        active_companies = []
        for c in self.companies:
            slug = c.get("slug")
            if slug and counts_404.get(slug, 0) < 3:
                active_companies.append(c)
                
        log.info(f"Loaded {len(active_companies)} active companies to scrape (skipped {len(self.companies) - len(active_companies)} with 404s)")
        
        queue = asyncio.Queue()
        for c in active_companies:
            queue.put_nowait(c)
            
        all_jobs = []
        self.stats = Counter()
        gate = RequestGate(4, 0.5, 4, cooldown=10)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)) as client:
            tasks = [asyncio.create_task(worker(queue, client, all_jobs, counts_404, gate, sem, self.stats)) for _ in range(WORKER_COUNT)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        log.info("ADP request stats: %s", dict(self.stats))
            
        save_404s(counts_404)
        log.info(f"Updated 404 counts for {len(counts_404)} companies")
        
        if all_jobs:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(all_jobs, f, indent=2, ensure_ascii=False)
            log.info(f"Saved {len(all_jobs)} jobs to {OUTPUT_FILE}")
        
        if write_queue is not None:
            await write_queue.put(all_jobs)
            
        return all_jobs


async def main():
    companies = load_companies()
    if not companies:
        return
        
    scraper = DirectAdpScraper(companies)
    all_jobs = await scraper.scrape()
    
    if all_jobs:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_jobs, f, indent=2, ensure_ascii=False)
        log.info(f"Scrape complete! Saved {len(all_jobs)} jobs to {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
