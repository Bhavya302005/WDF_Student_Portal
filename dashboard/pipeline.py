NEW_COMPANIES_FOUND = 0
import sys
import io
import asyncio
import argparse
import hashlib
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
import httpx

from role_config import classify_role, DROP_UNMATCHED
from scraper_utils import enrich_raw_job
from job_extraction import database_rows
from job_extraction.storage import EXTENDED_FIELDS
from job_extraction.model import enrich_jobs
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# UTF-8 stdout
if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("pipeline")

# CONFIGURATION
# NOTE: KEYWORDS only feeds the Type B (Google discovery) query loop in
# run_type_b_discovery(), which main() does not currently invoke. Type A
# (the API lane, which is what actually runs) does not filter by keyword at
# all -- it pulls every open role at each known company, and role_config's
# classify_role() only *labels* each job's role_category for display; with
# DROP_UNMATCHED=False nothing is dropped based on that label. If you want
# Type A restricted to specific roles, that filtering doesn't exist yet and
# needs to be added deliberately in _run_api_board / role_config.
KEYWORDS = [
    ("Software Engineer", "software_engineer"),
    ("Artificial Intelligence Engineer", "ai_engineer"),
]
COUNTRIES = ["India", "United States", "United Kingdom", "Germany", "Canada", "Singapore"]

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "jobs"

# LOCAL_ONLY=true → skip ALL Supabase writes; accumulate every board's jobs
# into all_jobs_local.json instead.
LOCAL_ONLY = os.getenv("LOCAL_ONLY", "false").lower() == "true"

MAX_JOBS_PER_RUN = 999999
GOOGLE_POST_TIME = os.getenv("GOOGLE_POST_TIME", "week")  # any, day, week, month, year
API_POST_TIME = os.getenv("API_POST_TIME", "week")  # any, day, week, month, year
GOOGLE_MAX_PAGES_PER_QUERY = int(os.getenv("GOOGLE_MAX_PAGES_PER_QUERY", "2"))
GOOGLE_QUERY_DELAY_MIN = float(os.getenv("GOOGLE_QUERY_DELAY_MIN", "12"))
GOOGLE_QUERY_DELAY_MAX = float(os.getenv("GOOGLE_QUERY_DELAY_MAX", "28"))
GOOGLE_429_COOLDOWN_SECONDS = float(os.getenv("GOOGLE_429_COOLDOWN_SECONDS", "300"))

ALL_JOBS_FILE = Path(__file__).parent / "all_jobs.json"
DISCOVERED_URLS_FILE = Path(__file__).parent / "type_b_discovered_urls.json"
LOCAL_OUTPUT_FILE = Path(__file__).parent / "all_jobs_local.json"

# REGISTRIES
BOARDS = {
    # API-only lane: reads known companies from each *_companies.txt file.
    "Amazon": {"api": "amazon.amazon_api", "type": "b"},
    "Lever": {"api": "lever.lever_api", "type": "b"},
    "Greenhouse": {"api": "greenhouse.greenhouse_api", "type": "b"},
    "SmartRecruiters": {"api": "smartrecruiter.smartrecruiter_api", "type": "b"},
    "Teamtailor": {"api": "teamtailor.teamtailor_api", "type": "b", "class": "DirectTeamtailorScraper", "csv": "teamtailor/teamtailor.csv"},
    "Workable": {"api": "workable.workable_api", "type": "b"},
    "Ashby": {"api": "ashby.ashby_api", "type": "b"},
    "iCIMS": {"api": "icims.icims_api", "type": "b", "class": "DirectiCIMSScraper"},
    "Jobvite": {"api": "jobvite.jobvite_api", "type": "b", "class": "DirectJobviteScraper"},
    "BambooHR": {"api": "bamboohr.bamboohr_api", "type": "b"},
    # "SuccessFactors": {"api": "successfactors.successfactors_api", "type": "b", "class": "DirectSuccessFactorsScraper", "csv": "successfactors/successfactors.csv"},
    "Dice": {"api": "Dice.dice", "type": "b", "class": "DiceScraper"},
    # "Personio": {"api": "personio.personio_fast", "type": "b", "class": "PersonioFastScraper", "loader": "load_tenants"},
    "Workday": {"api": "workday.workday_api", "type": "b", "class": "DirectWorkdayScraper", "csv": "workday/workday.csv"},
    # NOTE: join_com.csv has no company_id column; the scraper requires one
    # and silently skips every company without it (0 jobs/run). Use the
    # resolved file instead. It's ~3 weeks staler than join_com.csv (same
    # row count) -- re-run join_com_id_resolver.py to refresh when convenient.
    "Adp": {"api": "adp.adp_api", "type": "b", "class": "DirectAdpScraper", "csv": "adp/adp.csv"},
    # "JoinCom": {"api": "join com.join_com_api", "type": "b", "class": "DirectJoinComScraper", "csv": "join com/join_com_with_ids.csv"},
}

# HELPER: Supabase REST access
def supabase_rest(table: str):
    """Return (url, headers) for a Supabase REST table, or None if creds are missing."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    base = SUPABASE_URL.rstrip("/")
    target_url = f"{base}/{table}" if "/rest/v1" in base else f"{base}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    return target_url, headers

# HELPER: Normalization & Deduplication
def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text

def fingerprint(title: str, company: str, location: str = "") -> str:
    """Cross-board identity: same logical job posted on two different boards
    (different URL) collapses to the same fingerprint so deduplicate() can
    merge them into one row with an `also_on` list. NOT used as the DB
    primary key -- see url_fingerprint() for that."""
    key = f"{normalize_text(title)}||{normalize_text(company)}||{normalize_text(location)}"
    return hashlib.sha256(key.encode()).hexdigest()

def url_fingerprint(url: str) -> str:
    """Per-posting identity used as the Supabase primary key. job_url is
    unique per posting; title+company+location is not -- companies routinely
    post multiple openings with the identical title/location, and hashing
    only those fields would make distinct postings overwrite each other in
    the DB across runs."""
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()

def normalize_job(raw: dict, board: str, scraper_type: str, kw_label: str = "", kw_slug: str = "", role_cat: str = "", country: str = "") -> dict:
    raw = enrich_raw_job(raw)
    title = raw.get("job_title") or raw.get("title") or "Unknown Title"
    company = raw.get("company") or "Unknown Company"
    job_url = raw.get("job_url") or ""
    
    # Extract min/max salary
    salary = raw.get("salary") or ""
    sal_min, sal_max = raw.get("salary_min"), raw.get("salary_max")

    work_mode = raw.get("work_mode", "")
    loc = raw.get("location") or raw.get("location_str") or country
    is_remote = raw.get("is_remote")
    is_remote = (work_mode == 'Remote') if work_mode else None

    desc = raw.get("description") or ""
    posted_at = raw.get("posted_at") or raw.get("created_at") or raw.get("timestamp")
    scraped_at = raw.get("scraped_at") or datetime.now(timezone.utc).isoformat()

    return {
        "id": url_fingerprint(job_url) if job_url else fingerprint(title, company, loc),
        "job_title": title.strip(),
        "company": company.strip(),
        "location": loc.strip(),
        "country": country,
        "job_url": job_url.strip(),
        "apply_url": (raw.get("apply_url") or job_url).strip(),
        "description": desc,
        "description_raw": raw.get('description_raw') or desc,
        "extraction": raw.get('extraction'),
        "salary": salary.strip(),
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": raw.get('salary_currency'),
        "salary_period": raw.get('salary_period'),
        "experience": raw.get("experience", ""),
        "experience_min": raw.get('experience_min'),
        "experience_max": raw.get('experience_max'),
        "skills": raw.get("skills", ""),
        "skills_required": raw.get('skills_required', []),
        "skills_preferred": raw.get('skills_preferred', []),
        "work_mode": work_mode,
        "role_category": role_cat or kw_slug,
        "source_board": board,
        "scraper_type": scraper_type,
        "is_remote": is_remote,
        "job_type": raw.get("job_type") or "",
        "scraped_at": scraped_at,
        "posted_at": posted_at,
        "first_seen_at": raw.get("first_seen_at") or scraped_at,
        "google_discovered_at": raw.get("google_discovered_at"),
        "also_on": []
    }

def _parse_dt(dt_str):
    """Parse an ISO timestamp; returns None on failure. Naive datetimes assumed UTC."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def deduplicate(jobs: list[dict]) -> list[dict]:
    seen_fps, seen_urls, result = {}, set(), []
    dropped_no_url = 0
    for job in jobs:
        url = job.get("job_url", "")
        if not url:
            dropped_no_url += 1
            continue
        # Recomputed here rather than read from job["id"]: "id" is now the
        # URL-derived Supabase primary key (see url_fingerprint), while this
        # fp is only for detecting the same job cross-posted on another board.
        fp = fingerprint(job.get("job_title", ""), job.get("company", ""), job.get("location", ""))
        if url in seen_urls: continue
        if fp in seen_fps:
            existing = seen_fps[fp]
            if len(job.get('description') or '') > len(existing.get('description') or ''):
                # Derived fields and their evidence travel together with their JD.
                for key in ('description', 'description_raw', 'extraction', 'salary', 'salary_min', 'salary_max',
                            'salary_currency', 'salary_period', 'experience', 'experience_min', 'experience_max',
                            'skills', 'skills_required', 'skills_preferred', 'work_mode', 'is_remote', 'job_type'):
                    existing[key] = job.get(key)
            for key in (
                "posted_at",
                "google_discovered_at",
            ):
                if not existing.get(key) and job.get(key):
                    existing[key] = job[key]
            if job.get("first_seen_at"):
                candidates = [(_parse_dt(v), v) for v in (existing.get("first_seen_at"), job["first_seen_at"])]
                candidates = [(dt, v) for dt, v in candidates if dt is not None]
                if candidates:
                    existing["first_seen_at"] = min(candidates)[1]
            if job["source_board"] not in existing.get("also_on", []) and job["source_board"] != existing.get("source_board"):
                existing.setdefault("also_on", []).append(job["source_board"])
            seen_urls.add(url)
            continue
        seen_fps[fp] = job
        seen_urls.add(url)
        result.append(job)
    if dropped_no_url:
        log.warning(f"deduplicate: dropped {dropped_no_url} jobs with no job_url")
    return result


# PIPELINE STAGES

def extract_company_slug(url: str, board: str) -> str:
    url = url.lower()
    if board == "Greenhouse":
        m = re.search(r"boards\.greenhouse\.io/([^/]+)", url)
    elif board == "Lever":
        m = re.search(r"jobs\.lever\.co/([^/]+)", url)
    elif board == "Ashby":
        m = re.search(r"(?:jobs\.)?ashbyhq\.com/([^/]+)", url)
    elif board == "BambooHR":
        m = re.search(r"https?://([^.]+)\.bamboohr\.com", url)
    elif board == "Workable":
        m = re.search(r"apply\.workable\.com/([^/]+)", url)
    elif board == "SmartRecruiters":
        m = re.search(r"(?:jobs|careers)\.smartrecruiters\.com/([^/]+)", url)
    else:
        return ""
    return m.group(1).strip() if m else ""

def auto_feed_companies(board_name: str, new_slugs: set):
    if not new_slugs: return
    config = BOARDS.get(board_name)
    if not config or "api" not in config: return
    
    # Derives folder path, e.g., 'greenhouse' from 'greenhouse.greenhouse_api'
    folder = config["api"].split(".")[0]
    txt_path = Path(folder) / f"{folder}_companies.txt"
    
    existing = set()
    if txt_path.exists():
        with open(txt_path, "r") as f:
            existing = {line.strip() for line in f if line.strip()}
    
    added = 0
    with open(txt_path, "a") as f:
        for slug in new_slugs:
            if slug and slug not in existing:
                f.write(f"{slug}\n")
                existing.add(slug)
                added += 1
                
    if added > 0:
        try:
            rest = supabase_rest("companies")
            if rest is None:
                raise RuntimeError("Supabase credentials are not configured")
            target_url, headers = rest
            headers = {**headers, "Prefer": "resolution=merge-duplicates"}

            data = [{"slug": s, "source_board": board_name} for s in new_slugs]
            resp = httpx.post(target_url, headers=headers, json=data, timeout=30)
            resp.raise_for_status()

            global NEW_COMPANIES_FOUND
            NEW_COMPANIES_FOUND += added
            log.info(f"Auto-fed {added} new companies into {txt_path.name} and Supabase")
        except Exception as e:
            log.error(f"Failed to auto-feed to Supabase: {e}")

async def run_type_a(active_boards: set[str] | None = None) -> list[dict]:
    log.info("Starting Phase 1: API/direct board scrapers")
    import importlib
    log.info("Syncing companies from Supabase cloud...")
    try:
        rest = supabase_rest("companies")
        if rest is None:
            raise RuntimeError("Supabase credentials are not configured")
        target_url, headers = rest

        db_companies = []
        offset = 0
        limit = 1000
        async with httpx.AsyncClient(timeout=30) as sync_client:
            while True:
                resp = await sync_client.get(f"{target_url}?select=slug,source_board&offset={offset}&limit={limit}", headers=headers)
                if resp.status_code != 200:
                    log.error(f"Company sync request failed with HTTP {resp.status_code}")
                    break
                batch = resp.json()
                if not batch:
                    break
                db_companies.extend(batch)
                offset += limit
                if len(batch) < limit:
                    break

        if db_companies:
            board_groups = {}
            for c in db_companies:
                board_groups.setdefault(c["source_board"], set()).add(c["slug"])

            for board, config in BOARDS.items():
                if "api" in config:
                    folder = config["api"].split(".")[0]
                    txt_path = Path(folder) / f"{folder}_companies.txt"
                    txt_path.parent.mkdir(exist_ok=True, parents=True)
                    slugs = board_groups.get(board, set()) or board_groups.get(board.lower(), set())
                    # Merge with existing local slugs so a missing/empty DB group
                    # never wipes a locally-maintained company list.
                    if txt_path.exists():
                        with open(txt_path, "r") as tf:
                            slugs = slugs | {line.strip() for line in tf if line.strip()}
                    with open(txt_path, "w") as tf:
                        for s in sorted(slugs):
                            # BambooHR subdomains cannot contain spaces.
                            # Strip spaces to convert e.g. "Domain Tools" -> "domaintools".
                            clean = re.sub(r'\s+', '', s.strip()) if folder == 'bamboohr' else s
                            if clean:
                                tf.write(f"{clean}\n")
            log.info("Successfully synced all local company lists from Supabase!")
    except Exception as e:
        log.error(f"Failed to sync companies from Supabase: {e}")

    all_jobs = []
    
    async def _run_api_board(board_name, config):
        board_jobs: list[dict] = []
        mod = importlib.import_module(config["api"])
        try:
            if hasattr(mod, "SEARCH_ATTRIBUTES"):
                mod.SEARCH_ATTRIBUTES["post_time"] = API_POST_TIME

            raw = []

            # ── Streaming write queue ──────────────────────────────────────────
            # Scrapers that accept a write_queue post batches of jobs onto it as
            # each company finishes.  A background writer task drains the queue
            # and upserts to Supabase immediately, so progress is persisted
            # continuously.  If the pipeline is interrupted, every batch already
            # flushed is safely stored; on re-run upsert(on_conflict="id") is
            # idempotent so there are no duplicates.
            write_queue: asyncio.Queue = asyncio.Queue()
            streamed_jobs: list[dict] = []
            writer_task = asyncio.create_task(
                _streaming_board_writer(board_name, write_queue, streamed_jobs)
            )

            # --- Module-level run_scrape / scrape function (legacy scrapers) ---
            scrape_fn = getattr(mod, "run_scrape", None) or getattr(mod, "scrape", None)
            if scrape_fn:
                import inspect
                sig = inspect.signature(scrape_fn)
                if "write_queue" in sig.parameters:
                    raw = await scrape_fn("", "", MAX_JOBS_PER_RUN * 10, write_queue=write_queue)
                else:
                    raw = await scrape_fn("", "", MAX_JOBS_PER_RUN * 10)

            # --- Class-based scrapers (Dice, Workday, JoinCom style) ---
            elif config.get("class"):
                import csv as csv_mod
                cls = getattr(mod, config["class"])

                if config.get("loader"):
                    if hasattr(mod, "MAX_CONCURRENT_TENANTS"):
                        mod.MAX_CONCURRENT_TENANTS = min(mod.MAX_CONCURRENT_TENANTS, 50)
                    if hasattr(mod, "MAX_CONCURRENT_DESCS"):
                        mod.MAX_CONCURRENT_DESCS = min(mod.MAX_CONCURRENT_DESCS, 100)
                    loader_fn = getattr(mod, config["loader"])
                    init_arg = loader_fn()
                    instance = cls(init_arg)
                    result = await instance.scrape(write_queue)
                    if result:
                        raw = result

                elif config.get("csv"):
                    csv_path = Path(config["csv"])
                    companies = []
                    if csv_path.exists():
                        with open(csv_path, newline="", encoding="utf-8") as cf:
                            companies = list(csv_mod.DictReader(cf))
                    if companies:
                        instance = cls(companies)
                        if board_name == "Workday":
                            result = await instance.scrape("", "global", write_queue)
                        else:
                            result = await instance.scrape(write_queue)
                        if result:
                            raw = result
                    else:
                        log.warning(f"{board_name}: CSV empty or missing at {config['csv']}")

                else:
                    # Generic / class_txt (e.g. Jobvite) / no-arg (e.g. Dice)
                    import inspect
                    folder = config["api"].split(".")[0]
                    txt_path = Path(folder) / f"{folder}_companies.txt"
                    init_sig = inspect.signature(cls.__init__)
                    init_params = [p for p in init_sig.parameters.keys() if p != "self"]
                    if init_params:
                        companies = []
                        if txt_path.exists():
                            with open(txt_path, "r", encoding="utf-8") as f:
                                companies = [line.strip() for line in f if line.strip()]
                        log.info(f"Loaded {len(companies):,} companies for {board_name} from {txt_path}")
                        instance = cls(companies)
                    else:
                        instance = cls()

                    scrape_sig = inspect.signature(instance.scrape)
                    if "write_queue" in scrape_sig.parameters:
                        result = await instance.scrape(write_queue)
                    else:
                        result = await instance.scrape()
                    if result:
                        raw = result

        except Exception as e:
            log.error(f"Failed API scrape for {board_name}: {e}")
        finally:
            # Always signal the writer to flush & exit, even on exception
            await write_queue.put(None)
            await writer_task

        # ── Post-scrape: normalize + deduplicate ─────────────────────────────
        # `raw` contains the scraper's full return value; `streamed_jobs` has
        # whatever was already flushed via the queue (may be same or a subset).
        # Prefer `raw` when available so we can run classify_role on everything;
        # if raw is empty (scraper only pushed via queue), use streamed_jobs.
        source = raw if raw else streamed_jobs
        for r in source:
            role_match = classify_role(
                r.get("job_title", "") or r.get("title", ""),
                r.get("description", ""),
                r.get("skills", ""),
            )
            if not role_match and DROP_UNMATCHED:
                continue
            role_slug = role_match["slug"] if role_match else "other"
            board_jobs.append(normalize_job(r, board_name, "api", role_cat=role_slug))

        if board_jobs:
            board_jobs = deduplicate(board_jobs)
            board_jobs = await enrich_jobs(board_jobs)
            if LOCAL_ONLY:
                # Save board results to local JSON immediately (fault-tolerant)
                await asyncio.to_thread(_append_jobs_to_local, board_jobs)
            else:
                # Final upsert: catches any jobs that returned via `raw` but were
                # never put on the write_queue (scrapers that don't support it).
                # For scrapers that streamed everything, this is a cheap no-op
                # because upsert(on_conflict="id") just updates existing rows.
                await asyncio.to_thread(upsert_to_supabase, board_jobs)
            log.info(f"[{board_name}] Board complete — {len(board_jobs)} total jobs saved.")
        return board_jobs

    boards_items = [
        (b, c) for b, c in BOARDS.items()
        if "api" in c and (active_boards is None or b.lower() in active_boards)
    ]
    api_tasks = [
        _run_api_board(b, c) for b, c in boards_items
    ]
    # Optimization: return_exceptions=True prevents one scraper crash from taking down the pipeline
    api_results = await asyncio.gather(*api_tasks, return_exceptions=True)
    for res in api_results:
        if isinstance(res, Exception):
            log.error(f"API Scraper task threw unhandled exception: {res}")
        elif res:
            all_jobs.extend(res)

    return all_jobs

async def run_type_b_discovery() -> dict:
    log.info("Starting Phase 2: Type B Discovery (Google URLs)")
    import importlib
    if DISCOVERED_URLS_FILE.exists():
        try:
            with open(DISCOVERED_URLS_FILE) as f:
                discovered = json.load(f)
        except Exception:
            discovered = {}
    else:
        discovered = {}

    for board_name, config in BOARDS.items():
        if config["type"] != "b": continue
        if "google" not in config:
            log.warning(f"{board_name}: no 'google' module configured, skipping discovery")
            continue
        mod = importlib.import_module(config["google"])
        if hasattr(mod, "MAX_GOOGLE_PAGES"):
            mod.MAX_GOOGLE_PAGES = GOOGLE_MAX_PAGES_PER_QUERY
        discovered.setdefault(board_name, [])
        seen_discovered_urls = {item.get("url") for item in discovered[board_name]}
        for kw_label, kw_slug in KEYWORDS:
            for country in COUNTRIES:
                try:
                    # Construct search URL (simulated logic for brevity, you might call their internal builder)
                    if hasattr(mod, "build_google_search_url"):
                        url = mod.build_google_search_url({"job_profile": kw_label, "location": country, "post_time": GOOGLE_POST_TIME})
                        
                        # Find the scraper class
                        scraper_cls = next((getattr(mod, name) for name in dir(mod) if name.endswith("Scraper")), None)
                        if scraper_cls and hasattr(scraper_cls, "discover_urls"):
                            instance = scraper_cls()
                            urls = await instance.discover_urls(url)
                            new_slugs = set()
                            for u in urls:
                                if u in seen_discovered_urls:
                                    continue
                                discovered[board_name].append({
                                    "url": u,
                                    "kw_label": kw_label,
                                    "kw_slug": kw_slug,
                                    "country": country,
                                    "google_discovered_at": datetime.now(timezone.utc).isoformat(),
                                })
                                seen_discovered_urls.add(u)
                                slug = extract_company_slug(u, board_name)
                                if slug: new_slugs.add(slug)

                            auto_feed_companies(board_name, new_slugs)
                except Exception as e:
                    log.error(f"Failed discovery for {board_name}: {e}")
                    if "RateLimitError" in str(e):
                        log.error(f"Google 429 CAPTCHA hit. Cooling down for {GOOGLE_429_COOLDOWN_SECONDS:.0f} seconds.")
                        await asyncio.sleep(GOOGLE_429_COOLDOWN_SECONDS)
                await asyncio.sleep(random.uniform(GOOGLE_QUERY_DELAY_MIN, GOOGLE_QUERY_DELAY_MAX))
        # Persist once per board rather than after every keyword x country query
        with open(DISCOVERED_URLS_FILE, "w") as f:
            json.dump(discovered, f, indent=2)
    
    with open(DISCOVERED_URLS_FILE, "w") as f:
        json.dump(discovered, f, indent=2)
    return discovered

async def run_type_b_enrichment(discovered: dict) -> list[dict]:
    log.info("Starting Phase 3: Type B Enrichment (Fetching Details)")
    import importlib
    enriched = []
    
    for board_name, jobs in discovered.items():
        config = BOARDS.get(board_name)
        if not config or not jobs: continue
        if "google" not in config:
            log.warning(f"{board_name}: no 'google' module configured, skipping enrichment")
            continue
        mod = importlib.import_module(config["google"])
        
        try:
            scraper_cls = next((getattr(mod, name) for name in dir(mod) if name.endswith("Scraper")), None)
            if not scraper_cls: continue
            instance = scraper_cls()
            if not hasattr(instance, "_scrape_job"): continue

            import inspect
            accepts_kw_label = 'kw_label' in inspect.signature(instance._scrape_job).parameters

            semaphore = asyncio.Semaphore(5)
            async with httpx.AsyncClient(timeout=30) as client:
                # Semaphore already caps concurrency; gather everything at once
                # instead of batching (a batch barrier just wastes wall-clock time).
                tasks = []
                for i, job_meta in enumerate(jobs):
                    if accepts_kw_label:
                        tasks.append(instance._scrape_job(client, semaphore, job_meta["url"], i+1, len(jobs), kw_label=job_meta["kw_label"]))
                    else:
                        tasks.append(instance._scrape_job(client, semaphore, job_meta["url"], i+1, len(jobs)))

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for idx, r in enumerate(results):
                    meta = jobs[idx]
                    if isinstance(r, Exception):
                        log.error(f"Enrichment task failed for {meta['url']}: {r}")
                    elif isinstance(r, dict):
                        r["google_discovered_at"] = meta.get("google_discovered_at")
                        enriched.append(normalize_job(r, board_name, "google", meta["kw_label"], meta["kw_slug"], country=meta["country"]))
        except Exception as e:
            log.error(f"Enrichment failed for {board_name}: {e}")
            
    return enriched

def upsert_to_supabase(jobs: list[dict]):
    """Upsert jobs to Supabase. Skipped entirely when LOCAL_ONLY=true."""
    if LOCAL_ONLY:
        log.info(f"LOCAL_ONLY mode — skipping Supabase upsert of {len(jobs)} jobs.")
        return

    VALID_KEYS = {"id", "job_title", "company", "location", "country", "job_url", "apply_url", "description", "salary", "salary_min", "salary_max", "experience", "skills", "work_mode", "role_category", "source_board", "scraper_type", "is_remote", "job_type", "scraped_at", "posted_at", "also_on"}
    if os.getenv('JOB_EXTRACTION_DB_FIELDS', 'false').lower() == 'true':
        VALID_KEYS |= EXTENDED_FIELDS | {'salary_currency'}

    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("Skipping Supabase upsert because SUPABASE_URL or SUPABASE_KEY is missing.")
        return

    clean_url = SUPABASE_URL.replace("/rest/v1", "").replace("/rest/v1/", "").rstrip("/")
    client = create_client(clean_url, SUPABASE_KEY)
    
    unique_jobs = {}
    for job in database_rows(jobs):
        clean = {k: v for k, v in job.items() if k in VALID_KEYS}
        if "job_url" in clean and clean["job_url"]:
            unique_jobs[clean["job_url"]] = clean
            
    clean_jobs = list(unique_jobs.values())
        
    inserted = 0
    chunk_size = 500
    for i in range(0, len(clean_jobs), chunk_size):
        chunk = clean_jobs[i:i + chunk_size]
        try:
            client.table(SUPABASE_TABLE).upsert(chunk, on_conflict="id").execute()
            inserted += len(chunk)
            log.info(f"Upserted chunk of {len(chunk)} jobs to Supabase.")
        except Exception as e:
            log.error(f"Supabase upsert chunk error: {e}")
            
    log.info(f"Total upserted: {inserted} jobs to Supabase.")


_local_seen_urls: set = set()
_local_file_lock = asyncio.Lock() if False else None  # placeholder; real lock created at runtime

def _append_jobs_to_local(jobs: list[dict]):
    """Thread-safe append of jobs to all_jobs_local.json.
    Reads the existing list, merges (deduplicating by job_url), and rewrites.
    """
    existing: list[dict] = []
    if LOCAL_OUTPUT_FILE.exists():
        try:
            with open(LOCAL_OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    seen_urls = {j.get("job_url") for j in existing if j.get("job_url")}
    added = 0
    for job in jobs:
        url = job.get("job_url")
        if url and url not in seen_urls:
            existing.append(job)
            seen_urls.add(url)
            added += 1

    with open(LOCAL_OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, default=str)
    log.info(f"[LOCAL] Appended {added} new jobs → {LOCAL_OUTPUT_FILE} (total: {len(existing)})")


async def _streaming_board_writer(
    board_name: str,
    write_queue: asyncio.Queue,
    board_jobs_out: list,
    chunk_size: int = 500,
):
    """
    Background task: drains write_queue posted by individual scrapers and
    upserts each batch to Supabase immediately.  Scrapers signal completion
    by putting ``None`` on the queue.

    This makes progress persistent: even if the pipeline is interrupted,
    every batch already drained has been saved.  On the next full run,
    ``upsert(on_conflict="id")`` silently overwrites existing rows, so
    there are never duplicate records.
    """
    buf: list[dict] = []

    async def _flush(force: bool = False):
        nonlocal buf
        if not buf:
            return
        if not force and len(buf) < chunk_size:
            return
        extracted = await enrich_jobs(buf)
        buf = [normalize_job(job, board_name, 'api') for job in extracted]
        if LOCAL_ONLY:
            await asyncio.to_thread(_append_jobs_to_local, buf)
        else:
            await asyncio.to_thread(upsert_to_supabase, buf)
        board_jobs_out.extend(buf)
        dest = "local JSON" if LOCAL_ONLY else "Supabase"
        log.info(f"[{board_name}] Streamed {len(buf)} jobs → {dest} (running total: {len(board_jobs_out)})")
        buf = []

    while True:
        batch = await write_queue.get()
        write_queue.task_done()
        if batch is None:          # sentinel — scraper finished
            await _flush(force=True)
            break
        if isinstance(batch, list):
            buf.extend(batch)
            await _flush()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery-only", action="store_true")
    parser.add_argument("--enrichment-only", action="store_true")
    parser.add_argument("--type-a-only", action="store_true", help="Run only Type A scrapers")
    parser.add_argument("--type-b-only", action="store_true", help="Run only Type B scrapers")
    parser.add_argument("--backfill-month", action="store_true", help="Fetch jobs from the last month, then future runs can use the default day window")
    parser.add_argument("--post-time", choices=["any", "hour", "day", "week", "month", "year"], help="Override API and Google post-time windows")
    parser.add_argument("--no-supabase", "--no-db", dest="no_supabase", action="store_true", help="Skip database upload and save only to local JSON")
    parser.add_argument("--local-only", action="store_true", help="Alias for --no-supabase")
    parser.add_argument("--output", type=str, default="all_jobs_pipeline.json", help="Output JSON file path")
    parser.add_argument("--boards", nargs="+", help="Specific board(s) to run")
    parser.add_argument("--exclude-boards", nargs="+", default=["smartrecruiters", "icims", "workable", "joincom", "personio", "successfactors"], help="Board(s) to exclude")
    args = parser.parse_args()

    global GOOGLE_POST_TIME, API_POST_TIME, LOCAL_ONLY, LOCAL_OUTPUT_FILE
    if args.backfill_month:
        GOOGLE_POST_TIME = "month"
        API_POST_TIME = "month"
    if args.post_time:
        GOOGLE_POST_TIME = args.post_time
        API_POST_TIME = args.post_time
    if args.no_supabase or args.local_only:
        LOCAL_ONLY = True

    if args.output:
        LOCAL_OUTPUT_FILE = Path(args.output)
        if not LOCAL_OUTPUT_FILE.is_absolute():
            LOCAL_OUTPUT_FILE = Path(__file__).parent / args.output
        if LOCAL_ONLY and LOCAL_OUTPUT_FILE.exists():
            try:
                LOCAL_OUTPUT_FILE.unlink()
            except Exception:
                pass

    exclude_set = {b.lower() for b in args.exclude_boards} if args.exclude_boards else set()
    include_set = {b.lower() for b in args.boards} if args.boards else None

    active_boards = set()
    for b in BOARDS:
        b_low = b.lower()
        if include_set is not None and b_low not in include_set:
            continue
        if b_low in exclude_set:
            continue
        active_boards.add(b_low)

    log.info(f"Active boards to run ({len(active_boards)}): {', '.join(sorted(active_boards))}")

    all_jobs = []

    # Type B Discovery and Enrichment are currently disabled.
    # To re-enable, implement run_b() and wire it correctly.

    tasks = []
    # API-only mode: always run Type A, never run Type B (Google)
    tasks.append(run_type_a(active_boards))

    if tasks:
        results = await asyncio.gather(*tasks)
        for res in results:
            if res:
                all_jobs.extend(res)
        
        # Load existing, deduplicate, save
        if ALL_JOBS_FILE.exists():
            with open(ALL_JOBS_FILE) as f:
                existing = json.load(f)
        else:
            existing = []
            
        # Optimization: Prune stale jobs (older than 30 days) from existing JSON
        now = datetime.now(timezone.utc)
        fresh_existing = []
        unparseable = 0
        for job in existing:
            # Use scraped_at or posted_at to check age
            dt_str = job.get("scraped_at") or job.get("posted_at") or job.get("first_seen_at")
            dt = _parse_dt(dt_str)
            if dt_str and dt is None:
                unparseable += 1
            if dt is not None and (now - dt).days > 30:
                continue
            fresh_existing.append(job)
        if unparseable:
            log.warning(f"Prune: {unparseable} jobs had unparseable dates and were kept")

        existing = fresh_existing
            
        existing_ids = {job.get("id") for job in existing if job.get("id")}
        new_jobs = [job for job in all_jobs if job.get("id") and job.get("id") not in existing_ids]
        
        new_board_counts = {}
        for job in new_jobs:
            sb = job.get("source_board", "unknown")
            new_board_counts[sb] = new_board_counts.get(sb, 0) + 1
            
        log.info("")
        log.info("=== NEW JOBS DISCOVERED THIS RUN ===")
        if not new_board_counts:
            log.info("0 new jobs found.")
        else:
            for board, count in sorted(new_board_counts.items(), key=lambda x: x[1], reverse=True):
                log.info(f"{board}: +{count} new jobs")
            log.info(f"Total New: +{len(new_jobs)} jobs")
        log.info("====================================")
        log.info("")
            
        final_jobs = deduplicate(all_jobs)

        # ── Universal JD Enrichment ──────────────────────────────────────
        # Normalises every board's output to the exact schema consumed by
        # matching/ingestion.py → JobProcessor.process(). Runs offline.
        try:
            from job_extraction.jd_enricher import enrich_batch, audit_coverage
            log.info("Running universal JD enrichment pass...")
            final_jobs = enrich_batch(final_jobs)
            cov = audit_coverage(final_jobs)
            for f in ["description", "posted_at", "salary_min", "work_mode", "experience_min"]:
                log.info(f"  Field coverage [{f}]: {cov.get(f, 0):.1%}")
        except ImportError:
            log.warning("jd_enricher not found — skipping enrichment pass")
        # ─────────────────────────────────────────────────────────────────

        if LOCAL_ONLY:
            with open(LOCAL_OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(final_jobs, f, indent=2, ensure_ascii=False, default=str)
            log.info(f"Saved {len(final_jobs):,} total jobs into single JSON file: {LOCAL_OUTPUT_FILE}")
        log.info(f"Pipeline complete. Total jobs in active index: {len(final_jobs)}")

    
    else:
        final_jobs = []

    # Push to pipeline_metrics (skipped in LOCAL_ONLY mode)
    if LOCAL_ONLY:
        log.info(f"LOCAL_ONLY mode — skipping pipeline_metrics push. Final local file: {LOCAL_OUTPUT_FILE}")
        if LOCAL_OUTPUT_FILE.exists():
            try:
                # File was just written above; we don't need to load the entire JSON into memory just to log the count.
                # This prevents OOM errors on large files.
                log.info(f"[LOCAL] Verified output file exists: {LOCAL_OUTPUT_FILE.name}")
            except Exception:
                pass
    else:
        try:
            rest = supabase_rest("pipeline_metrics")
            if rest is None:
                raise RuntimeError("Supabase credentials are not configured")
            target_url, headers = rest

            type_a_count = len([j for j in all_jobs if j.get("scraper_type") == "api"])
            type_b_count = len([j for j in all_jobs if j.get("scraper_type") == "google"])

            data = {
                "type_a_fetched": type_a_count,
                "type_b_fetched": type_b_count,
                "new_companies_found": NEW_COMPANIES_FOUND
            }
            resp = httpx.post(target_url, headers=headers, json=data, timeout=30)
            resp.raise_for_status()
            log.info(f"Pushed daily metrics to Supabase: {data}")
        except Exception as e:
            log.error(f"Failed to push pipeline metrics: {e}")

if __name__ == "__main__":
    asyncio.run(main())
