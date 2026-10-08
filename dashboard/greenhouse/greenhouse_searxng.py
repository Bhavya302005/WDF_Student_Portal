from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import sys
from typing import Any
import urllib.parse

import httpx
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scraper_utils import enrich_raw_job


load_dotenv()

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080").rstrip("/")
SEARXNG_ENGINES = os.getenv("SEARXNG_ENGINES", "google").strip()
SEARXNG_LANGUAGE = os.getenv("SEARXNG_LANGUAGE", "en-US").strip()
MAX_SEARCH_PAGES = int(os.getenv("SEARXNG_MAX_PAGES", "10"))
SEARCH_PAGE_DELAY = float(os.getenv("SEARXNG_PAGE_DELAY", "1"))
DETAIL_CONCURRENCY = int(os.getenv("GREENHOUSE_SEARCH_CONCURRENCY", "30"))
REQUEST_TIMEOUT = float(os.getenv("GREENHOUSE_SEARCH_TIMEOUT", "20"))
MAX_RETRIES = int(os.getenv("GREENHOUSE_SEARCH_RETRIES", "3"))

DEFAULT_QUERY = "site:greenhouse.io united states"
DEFAULT_OUTPUT = Path(__file__).with_name("greenhouse_searxng_jobs.json")

TIME_RANGE_CODES = {
    "h": "hour",
    "d": "day",
    "w": "week",
    "m": "month",
    "y": "year",
}
TIME_RANGES = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
    "any": None,
}
SEARX_TIME_RANGES = {"day", "week", "month", "year"}
GREENHOUSE_HOSTS = {
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
}
JOB_PATH_RE = re.compile(r"^/([^/]+)/(?:jobs|job)/(\d+)(?:/.*)?$", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("greenhouse_searxng")
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class GreenhouseJobRef:
    url: str
    board_token: str
    job_id: str


def build_google_search_url(attributes: dict[str, str]) -> str:
    """Build a portable Google URL which this scraper translates to SearXNG."""
    parts = ["site:greenhouse.io"]
    for key in ("job_profile", "location", "experience", "job_type", "company", "work_mode"):
        value = str(attributes.get(key) or "").strip()
        if value:
            parts.append(value)

    params = {"q": " ".join(parts), "filter": "0"}
    post_time = str(attributes.get("post_time") or "any").lower()
    reverse_codes = {value: key for key, value in TIME_RANGE_CODES.items()}
    if post_time not in TIME_RANGES:
        raise ValueError(f"post_time must be one of: {', '.join(TIME_RANGES)}")
    if post_time != "any":
        params["tbs"] = f"qdr:{reverse_codes[post_time]}"
    return "https://www.google.com/search?" + urllib.parse.urlencode(params)


def parse_google_search_url(url: str) -> tuple[str, str | None]:
    """Extract Google's query and relative time filter from a search URL."""
    parsed = urllib.parse.urlparse(url.replace("\\&", "&"))
    params = urllib.parse.parse_qs(parsed.query)
    query = (params.get("q") or [""])[0].strip()
    if not query:
        raise ValueError("The Google URL does not contain a non-empty q parameter")

    tbs = " ".join(params.get("tbs") or [])
    match = re.search(r"(?:^|,)qdr:([hdwmy])(?:,|$)", tbs, re.I)
    return query, TIME_RANGE_CODES.get(match.group(1).lower()) if match else None


def _unwrap_search_redirect(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("google.com") and parsed.path == "/url":
        params = urllib.parse.parse_qs(parsed.query)
        return (params.get("q") or params.get("url") or [url])[0]
    return url


def parse_greenhouse_job_url(raw_url: str) -> GreenhouseJobRef | None:
    """Validate and canonicalize supported Greenhouse job URL formats."""
    import html

    raw_url = urllib.parse.unquote(_unwrap_search_redirect(html.unescape(raw_url.strip())))
    parsed = urllib.parse.urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if host not in GREENHOUSE_HOSTS:
        return None

    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    match = JOB_PATH_RE.match(path)
    if match:
        board_token, job_id = match.groups()
    elif path == "/embed/job_app":
        params = urllib.parse.parse_qs(parsed.query)
        board_token = (params.get("for") or [""])[0]
        job_id = (params.get("token") or params.get("gh_jid") or [""])[0]
        if not board_token or not job_id.isdigit():
            return None
        path = f"/{board_token}/jobs/{job_id}"
    else:
        return None

    board_token = urllib.parse.unquote(board_token).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", board_token):
        return None

    canonical = urllib.parse.urlunparse((
        "https",
        host,
        path,
        "",
        "",
        "",
    ))
    return GreenhouseJobRef(canonical, board_token, job_id)


def _format_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clean_html(value: str) -> str:
    import html

    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_within_time_range(updated_at: str | None, time_range: str) -> bool:
    max_age = TIME_RANGES[time_range]
    if not max_age or not updated_at:
        return True
    try:
        posted = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - posted.astimezone(timezone.utc) <= max_age


def normalize_api_job(
    raw: dict[str, Any],
    ref: GreenhouseJobRef,
    discovered_at: str,
) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    location_data = raw.get("location") or {}
    location = str(location_data.get("name") or "").strip() if isinstance(location_data, dict) else ""
    description = _clean_html(str(raw.get("content") or ""))[:5000]
    company = str(raw.get("company_name") or ref.board_token).replace("-", " ").replace("_", " ").title()
    absolute_url = str(raw.get("absolute_url") or ref.url)
    canonical_ref = parse_greenhouse_job_url(absolute_url) or ref
    parsed_absolute_url = urllib.parse.urlparse(absolute_url)
    job_url = (
        canonical_ref.url
        if parsed_absolute_url.hostname in GREENHOUSE_HOSTS
        else absolute_url
        if parsed_absolute_url.scheme in {"http", "https"} and parsed_absolute_url.netloc
        else ref.url
    )
    identity = hashlib.md5(
        f"greenhouse:{ref.board_token.lower()}:{raw.get('id') or ref.job_id}".encode()
    ).hexdigest()

    return enrich_raw_job({
        "id": identity,
        "job_title": title,
        "company": company,
        "location": location,
        "job_url": job_url,
        "apply_url": job_url,
        "description": description,
        "source_board": "Greenhouse",
        "scraper_type": "google",
        "job_type": "full_time",
        "scraped_at": _format_time(datetime.now(timezone.utc).isoformat()),
        "created_at": _format_time(raw.get("updated_at")),
        "posted_at": _format_time(raw.get("updated_at")),
        "google_discovered_at": discovered_at,
        "also_on": [],
    })


class SearxGreenhouseScraper:
    def __init__(
        self,
        searxng_url: str = SEARXNG_URL,
        engines: str = SEARXNG_ENGINES,
        max_pages: int = MAX_SEARCH_PAGES,
    ) -> None:
        self.searxng_url = searxng_url.rstrip("/")
        self.engines = engines
        self.max_pages = max_pages
        self.stats: Counter[str] = Counter()
        self._refs_by_url: dict[str, GreenhouseJobRef] = {}

    @property
    def search_endpoint(self) -> str:
        return self.searxng_url if self.searxng_url.endswith("/search") else f"{self.searxng_url}/search"

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url, params=params)
                self.stats["requests"] += 1
                if response.status_code == 429 or response.status_code >= 500:
                    self.stats[f"status_{response.status_code}"] += 1
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(float(retry_after), 30.0) if retry_after else 0.75 * (2 ** attempt)
                    except ValueError:
                        delay = 0.75 * (2 ** attempt)
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(delay + random.uniform(0, 0.2))
                        continue
                    return None
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    if "application/json" not in response.headers.get("content-type", ""):
                        raise RuntimeError(
                            "SearXNG did not return JSON. Enable json in search.formats on the instance."
                        ) from exc
                    raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                self.stats[type(exc).__name__] += 1
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(0.75 * (2 ** attempt))
                    continue
            except httpx.HTTPStatusError as exc:
                self.stats[f"status_{exc.response.status_code}"] += 1
                if exc.response.status_code in (401, 403):
                    raise RuntimeError(
                        f"SearXNG rejected JSON search with HTTP {exc.response.status_code}; "
                        "use an instance that enables the JSON response format."
                    ) from exc
                break
        return None

    async def _discover(
        self,
        client: httpx.AsyncClient,
        query: str,
        time_range: str,
        max_jobs: int = 0,
    ) -> list[GreenhouseJobRef]:
        unique: dict[str, GreenhouseJobRef] = {}
        empty_pages = 0
        search_time_range = "day" if time_range == "hour" else time_range
        use_search_time_range = search_time_range in SEARX_TIME_RANGES

        for page in range(1, self.max_pages + 1):
            params = {
                "q": query,
                "format": "json",
                "pageno": str(page),
                "language": SEARXNG_LANGUAGE,
                "safesearch": "0",
            }
            if self.engines:
                params["engines"] = self.engines
            if use_search_time_range:
                params["time_range"] = search_time_range

            payload = await self._request_json(client, self.search_endpoint, params=params)
            if payload is None:
                if page == 1:
                    raise RuntimeError(
                        f"Could not query SearXNG at {self.search_endpoint}. "
                        "Check SEARXNG_URL and ensure JSON responses are enabled."
                    )
                log.warning("SearXNG page %d failed; stopping search", page)
                break
            results = payload.get("results") or []
            if page == 1 and not results and use_search_time_range:
                log.warning(
                    "SearXNG returned no time-filtered results; retrying without its date filter "
                    "and retaining the exact Greenhouse API date check"
                )
                use_search_time_range = False
                params.pop("time_range", None)
                payload = await self._request_json(client, self.search_endpoint, params=params)
                if payload is None:
                    raise RuntimeError("SearXNG fallback search failed")
                results = payload.get("results") or []
                self.stats["search_date_filter_fallback"] += 1
            self.stats["search_results"] += len(results)
            before = len(unique)
            for result in results:
                if not isinstance(result, dict):
                    continue
                ref = parse_greenhouse_job_url(str(result.get("url") or ""))
                if ref:
                    unique.setdefault(ref.url, ref)

            added = len(unique) - before
            log.info(
                "SearXNG page %d/%d: %d results, %d new Greenhouse jobs (%d total)",
                page,
                self.max_pages,
                len(results),
                added,
                len(unique),
            )
            empty_pages = empty_pages + 1 if added == 0 else 0
            if max_jobs > 0 and len(unique) >= max_jobs:
                break
            if not results or empty_pages >= 2:
                break
            if SEARCH_PAGE_DELAY > 0 and page < self.max_pages:
                await asyncio.sleep(SEARCH_PAGE_DELAY)

        refs = list(unique.values())
        if max_jobs > 0:
            refs = refs[:max_jobs]
        self._refs_by_url = {ref.url: ref for ref in refs}
        self.stats["unique_job_urls"] = len(refs)
        return refs

    async def discover_urls(self, google_url: str) -> list[str]:
        query, time_range = parse_google_search_url(google_url)
        time_range = time_range or "any"
        async with httpx.AsyncClient(
            headers={"Accept": "application/json", "User-Agent": "JobRadar/1.0"},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:
            refs = await self._discover(client, query, time_range)
        return [ref.url for ref in refs]

    async def _fetch_job_api(
        self,
        client: httpx.AsyncClient,
        ref: GreenhouseJobRef,
    ) -> dict[str, Any] | None:
        board = urllib.parse.quote(ref.board_token, safe="")
        job_id = urllib.parse.quote(ref.job_id, safe="")
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
        data = await self._request_json(client, url)
        if data:
            self.stats["details_ok"] += 1
        else:
            self.stats["details_dropped"] += 1
        return data

    async def _scrape_job(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        job_url: str,
        index: int,
        total: int,
        time_range: str = "any",
    ) -> dict[str, Any] | None:
        ref = self._refs_by_url.get(job_url) or parse_greenhouse_job_url(job_url)
        if not ref:
            return None
        async with semaphore:
            raw = await self._fetch_job_api(client, ref)
        if not raw or not raw.get("title"):
            return None
        if not _is_within_time_range(raw.get("updated_at"), time_range):
            self.stats["outside_board_date_window"] += 1
            return None
        if index % 100 == 0 or index == total:
            log.info("Greenhouse details: %d/%d", index, total)
        return normalize_api_job(raw, ref, datetime.now(timezone.utc).isoformat())

    async def scrape_query(
        self,
        query: str,
        time_range: str = "month",
        max_jobs: int = 0,
    ) -> list[dict[str, Any]]:
        if time_range not in TIME_RANGES:
            raise ValueError(f"time_range must be one of: {', '.join(TIME_RANGES)}")

        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        }
        async with httpx.AsyncClient(
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=DETAIL_CONCURRENCY + 20, max_keepalive_connections=DETAIL_CONCURRENCY),
        ) as client:
            refs = await self._discover(client, query, time_range, max_jobs)
            if not refs:
                return []
            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            results = await asyncio.gather(*(
                self._scrape_job(client, semaphore, ref.url, index, len(refs), time_range)
                for index, ref in enumerate(refs, 1)
            ))

        jobs = [job for job in results if job]
        self.stats["jobs_collected"] = len(jobs)
        return jobs

    async def scrape_google_url(
        self,
        google_url: str,
        time_range: str | None = None,
        max_jobs: int = 0,
    ) -> list[dict[str, Any]]:
        query, url_time_range = parse_google_search_url(google_url)
        return await self.scrape_query(query, time_range or url_time_range or "any", max_jobs)


async def scrape(job_profile: str, location: str, max_jobs: int = 99999) -> list[dict[str, Any]]:
    attributes = {
        "job_profile": job_profile,
        "location": location,
        "post_time": os.getenv("GOOGLE_POST_TIME", "month"),
    }
    url = build_google_search_url(attributes)
    return await SearxGreenhouseScraper().scrape_google_url(url, max_jobs=max_jobs)


async def async_main(args: argparse.Namespace) -> int:
    if args.google_url:
        query, url_time_range = parse_google_search_url(args.google_url)
        time_range = args.time_range or url_time_range or "any"
    else:
        query = args.query or DEFAULT_QUERY
        time_range = args.time_range or "month"

    scraper = SearxGreenhouseScraper(args.searxng_url, args.engines, args.pages)
    print("=" * 64)
    print("Greenhouse Search Scraper (SearXNG -> Greenhouse API)")
    print(f"SearXNG : {scraper.searxng_url}")
    print(f"Query   : {query}")
    print(f"Range   : {time_range}")
    print(f"Pages   : {args.pages}")
    print("=" * 64)

    try:
        jobs = await scraper.scrape_query(query, time_range, args.max_jobs)
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("Search failed: %s", exc)
        log.error("Set SEARXNG_URL to a reachable instance with JSON enabled.")
        return 1

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Collected {len(jobs)} jobs")
    print(f"Stats: {dict(scraper.stats)}")
    print(f"Saved: {output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover Greenhouse jobs through SearXNG and enrich them via the Greenhouse API."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--google-url", help="Google search URL; q and tbs are translated to SearXNG")
    source.add_argument("--query", help=f"Search query (default: {DEFAULT_QUERY!r})")
    parser.add_argument("--time-range", choices=TIME_RANGES, help="Override the URL/default time range")
    parser.add_argument("--searxng-url", default=SEARXNG_URL, help="SearXNG base URL or /search endpoint")
    parser.add_argument("--engines", default=SEARXNG_ENGINES, help="Comma-separated SearXNG engines")
    parser.add_argument("--pages", type=int, default=MAX_SEARCH_PAGES, help="Maximum SearXNG result pages")
    parser.add_argument("--max-jobs", type=int, default=0, help="Stop after this many discovered URLs (0 = unlimited)")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON path")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be at least 1")
    if args.max_jobs < 0:
        parser.error("--max-jobs cannot be negative")
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main(parse_args())))
    except KeyboardInterrupt:
        print("Interrupted by user.")
