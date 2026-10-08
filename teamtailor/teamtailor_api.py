"""Teamtailor scraper.

Teamtailor's main API requires authentication (`api.teamtailor.com/v1/...`
returns 406 without an API key), but every public careers site exposes a
free RSS feed at `/jobs.rss` with all the structured fields we need:

    GET https://{slug}.teamtailor.com/jobs.rss

Each `<item>` carries title, link, pubDate, guid, custom `tt:` location
(city, country, name), `tt:department`, `remoteStatus`, and an HTML
description. This is a single-request scrape -- Teamtailor's RSS includes
every open job, no pagination.
"""
import asyncio
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job
import hashlib
import html as html_lib
import logging
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from dotenv import load_dotenv

load_dotenv()

MAX_CONCURRENT_COMPANIES = int(os.getenv("MAX_CONCURRENT_JOBS", "100"))
REQUEST_TIMEOUT = float(os.getenv("TEAMTAILOR_TIMEOUT", "20"))
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("teamtailor_scraper")

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

RSS_TEMPLATE = "https://{slug}.teamtailor.com/jobs.rss"
TT_NS = {"tt": "https://teamtailor.com/locations"}

_URL_ID_RE = re.compile(r"/jobs/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_html_text(text: str | None) -> str:
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def format_iso_time(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_pubdate(value: str | None) -> datetime | None:
    """RFC 2822 dates from RSS, e.g. 'Fri, 20 Mar 2026 09:30:04 +0100'."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _format_location(item: ET.Element) -> str:
    """Compose 'City, Country' from the first <tt:location> child."""
    loc = item.find("tt:locations/tt:location", TT_NS)
    if loc is None:
        return ""
    parts = []
    for tag in ("city", "country"):
        value = (loc.findtext(f"tt:{tag}", namespaces=TT_NS) or "").strip()
        if value:
            parts.append(value)
    if parts:
        return ", ".join(parts)
    return (loc.findtext("tt:name", namespaces=TT_NS) or "").strip()


def _extract_remote(item: ET.Element) -> bool | None:
    """<remoteStatus> is one of: 'fully', 'temporary', 'hybrid', 'none'.
    Map the unambiguous extremes; treat hybrid/temporary as None (unknown) --
    pipeline.py's regex fallback covers those from title/location/description."""
    status = (item.findtext("remoteStatus") or "").strip().lower()
    if status == "fully":
        return True
    if status == "none":
        return False
    return None


def normalize_job(item: ET.Element, company_name: str, company_slug: str) -> dict[str, Any] | None:
    link = (item.findtext("link") or "").strip()
    if not link:
        return None

    # Prefer the numeric ID from the URL -- stable, public, shorter than
    # the GUID UUID. Fall back to the GUID if the URL lacks one.
    ats_id = ""
    m = _URL_ID_RE.search(link)
    if m:
        ats_id = m.group(1)
    if not ats_id:
        ats_id = (item.findtext("guid") or "").strip()
    if not ats_id:
        return None

    title = (item.findtext("title") or "").strip() or "Untitled"
    description = clean_html_text(item.findtext("description"))
    department = (item.findtext("tt:department", namespaces=TT_NS) or "").strip()
    location = _format_location(item)
    is_remote = _extract_remote(item)
    posted = _parse_pubdate(item.findtext("pubDate"))

    # Apply post_time filter on pubDate
    max_age = post_time_to_delta(SEARCH_ATTRIBUTES.get("post_time", "any"))
    if max_age and posted:
        if datetime.now(timezone.utc) - posted > max_age:
            return None  # skip jobs older than max_age

    dedupe_key = hashlib.md5(f"teamtailor:{company_slug}:{ats_id}".encode()).hexdigest()

    return enrich_raw_job({
        "id": dedupe_key,
        "job_title": title,
        "company": company_name,
        "location": location,
        "job_url": link,
        "apply_url": link,
        "description": description,
        "department": department or None,
        "is_remote": is_remote,
        "source_board": "Teamtailor",
        "scraper_type": "api",
        "job_type": "full_time",
        "scraped_at": format_iso_time(datetime.now(timezone.utc)),
        "created_at": format_iso_time(posted) or format_iso_time(datetime.now(timezone.utc)),
        "also_on": [],
    })


async def fetch_rss_with_retry(client: httpx.AsyncClient, url: str, retries: int = RETRY_COUNT) -> str | None:
    base_delay = 1.5
    for attempt in range(retries):
        try:
            # asyncio.wait_for forces a hard ceiling on top of httpx's own
            # timeout= param -- seen live on the sibling SuccessFactors
            # scraper that some hosts hang past httpx's internal timeout
            # entirely (blocking DNS/connect stall). Cheap insurance here
            # even though Teamtailor's own subdomains are more uniform.
            resp = await asyncio.wait_for(
                client.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, text/xml"},
                ),
                timeout=REQUEST_TIMEOUT + 5,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < retries - 1:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                return None
            resp.raise_for_status()
            return resp.text
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, asyncio.TimeoutError):
            if attempt == retries - 1:
                return None
            await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception:
            log.exception("Unexpected error fetching %s", url)
            return None
    return None


class DirectTeamtailorScraper:
    def __init__(self, companies: list[dict[str, str]]):
        self.companies = companies

    async def _fetch_company(self, client: httpx.AsyncClient, company: dict[str, str]) -> list[dict[str, Any]]:
        name = company.get("name") or company.get("slug") or ""
        slug = company.get("slug") or ""
        if not slug:
            return []
        url = RSS_TEMPLATE.format(slug=slug)

        xml_text = await fetch_rss_with_retry(client, url)
        if not xml_text:
            return []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            log.warning("Teamtailor (%s) returned malformed RSS", slug)
            return []

        # A response that parses as XML but isn't RSS (e.g. an HTML error
        # page) would otherwise silently look like "tenant has 0 jobs".
        if root.tag.lower() != "rss" and root.find(".//channel") is None:
            log.warning("Teamtailor (%s) returned non-RSS XML (root <%s>)", slug, root.tag)
            return []

        out: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in root.iter("item"):
            job = normalize_job(item, name, slug)
            if job is None or job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            out.append(job)
        return out

    async def scrape(self, write_queue: "asyncio.Queue | None" = None) -> list[dict[str, Any]]:
        headers = {"User-Agent": "Mozilla/5.0"}
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_COMPANIES)

        async def bounded_fetch(client: httpx.AsyncClient, company: dict[str, str]):
            async with semaphore:
                return await self._fetch_company(client, company)

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            headers=headers,
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        ) as client:
            tasks = [bounded_fetch(client, c) for c in self.companies]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for result in results:
            if isinstance(result, Exception):
                log.warning("Teamtailor company scrape task failed: %s", result)
                continue
            for job in result:
                url = job.get("job_url") or ""
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                all_jobs.append(job)

        if write_queue is not None:
            await write_queue.put(all_jobs)

        log.info("Teamtailor: %d companies scraped, %d jobs found", len(self.companies), len(all_jobs))
        return all_jobs


if __name__ == "__main__":
    import csv as _csv
    import json as _json

    _DIR = os.path.dirname(os.path.abspath(__file__))
    _CSV  = os.path.join(_DIR, "teamtailor.csv")
    _OUT  = os.path.join(_DIR, "all_teamtailor_jobs.json")

    def _load_companies() -> list[dict[str, str]]:
        companies = []
        if not os.path.exists(_CSV):
            log.error("teamtailor.csv not found at %s", _CSV)
            return []
        with open(_CSV, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                if row.get("slug"):
                    companies.append(row)
        return companies

    async def _main():
        companies = _load_companies()
        if not companies:
            return
        log.info("Loaded %d companies from teamtailor.csv", len(companies))
        scraper = DirectTeamtailorScraper(companies)
        jobs = await scraper.scrape()
        with open(_OUT, "w", encoding="utf-8") as f:
            _json.dump(jobs, f, indent=2)
        log.info("Saved %d jobs to %s", len(jobs), _OUT)

    asyncio.run(_main())
