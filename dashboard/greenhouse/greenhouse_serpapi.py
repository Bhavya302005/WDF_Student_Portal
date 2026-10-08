#!/usr/bin/env python3
"""Discover Google-indexed Greenhouse jobs with SerpAPI and enrich via Greenhouse."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Iterable
import urllib.parse

import httpx
from dotenv import load_dotenv

from greenhouse_searxng import (
    GreenhouseJobRef,
    TIME_RANGES,
    _is_within_time_range,
    normalize_api_job,
    parse_greenhouse_job_url,
)


load_dotenv()

SERPAPI_ENDPOINT = os.getenv("SERPAPI_ENDPOINT", "https://serpapi.com/search.json")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY") or os.getenv("SERP_API_KEY")
DEFAULT_PAGES = int(os.getenv("SERPAPI_MAX_PAGES", "10"))
DEFAULT_RESULTS_PER_PAGE = int(os.getenv("SERPAPI_RESULTS_PER_PAGE", "100"))
SEARCH_CONCURRENCY = int(os.getenv("SERPAPI_SEARCH_CONCURRENCY", "3"))
DETAIL_CONCURRENCY = int(os.getenv("GREENHOUSE_SEARCH_CONCURRENCY", "30"))
REQUEST_TIMEOUT = float(os.getenv("GREENHOUSE_SEARCH_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("GREENHOUSE_SEARCH_RETRIES", "3"))
DEFAULT_OUTPUT = Path(__file__).with_name("greenhouse_serpapi_jobs.json")
TIME_RANGE_CODES = {
    "hour": "h",
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("greenhouse_serpapi")
logging.getLogger("httpx").setLevel(logging.WARNING)


class SerpApiError(RuntimeError):
    pass


def title_words(value: str) -> set[str]:
    value = re.sub(r"\bsr\.?\b", "senior", value.lower())
    return set(re.findall(r"[a-z0-9]+", value))


def load_keywords(path: Path | None, inline_keywords: list[str]) -> list[str]:
    keywords = list(inline_keywords)
    if path:
        keywords.extend(path.read_text(encoding="utf-8").splitlines())

    unique: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        keyword = keyword.strip()
        normalized = keyword.casefold()
        if keyword and normalized not in seen:
            seen.add(normalized)
            unique.append(keyword)
    if not unique:
        raise ValueError("Provide at least one role with --keywords-file or --keyword")
    return unique


def build_query(keyword: str, location_query: str) -> str:
    escaped_keyword = keyword.replace('"', "")
    parts = ["site:greenhouse.io", f'"{escaped_keyword}"']
    if location_query.strip():
        parts.append(location_query.strip())
    return " ".join(parts)


def _result_urls(result: dict[str, Any]) -> Iterable[str]:
    for field in ("link", "redirect_link"):
        value = result.get(field)
        if isinstance(value, str):
            yield value

    sitelinks = result.get("sitelinks")
    if not isinstance(sitelinks, dict):
        return
    for items in sitelinks.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("link")
            if isinstance(value, str):
                yield value


class SerpApiGreenhouseScraper:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = SERPAPI_ENDPOINT,
        max_pages: int = DEFAULT_PAGES,
        results_per_page: int = DEFAULT_RESULTS_PER_PAGE,
        search_concurrency: int = SEARCH_CONCURRENCY,
        detail_concurrency: int = DETAIL_CONCURRENCY,
        timeout: float = REQUEST_TIMEOUT,
        retries: int = MAX_RETRIES,
        google_country: str = "us",
        google_language: str = "en",
        location_query: str = "united states",
        allow_partial: bool = False,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.max_pages = max_pages
        self.results_per_page = results_per_page
        self.search_concurrency = search_concurrency
        self.detail_concurrency = detail_concurrency
        self.timeout = timeout
        self.retries = retries
        self.google_country = google_country
        self.google_language = google_language
        self.location_query = location_query
        self.allow_partial = allow_partial
        self.stats: Counter[str] = Counter()

    async def _serpapi_json(
        self,
        client: httpx.AsyncClient,
        params: dict[str, str],
    ) -> dict[str, Any]:
        request_params = dict(params)
        request_params["api_key"] = self.api_key
        last_error = "unknown SerpAPI failure"

        for attempt in range(self.retries):
            try:
                response = await client.get(self.endpoint, params=request_params)
                self.stats["serpapi_requests"] += 1
                if response.status_code in (429, 500, 503):
                    last_error = f"HTTP {response.status_code}"
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(float(retry_after), 60.0) if retry_after else 1.0 * (2**attempt)
                    except ValueError:
                        delay = 1.0 * (2**attempt)
                    if attempt < self.retries - 1:
                        await asyncio.sleep(delay + random.uniform(0, 0.25))
                        continue
                response.raise_for_status()
                payload = response.json()
                error = str(payload.get("error") or "").strip()
                status = str((payload.get("search_metadata") or {}).get("status") or "")
                if error and not (status == "Success" and not payload.get("organic_results")):
                    raise SerpApiError(error)
                return payload
            except SerpApiError:
                raise
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in (400, 401, 403, 429):
                    try:
                        message = str(exc.response.json().get("error") or f"HTTP {code}")
                    except ValueError:
                        message = f"HTTP {code}"
                    raise SerpApiError(message) from exc
                last_error = f"HTTP {code}"
            except (httpx.TimeoutException, httpx.NetworkError, ValueError) as exc:
                last_error = type(exc).__name__
                if attempt < self.retries - 1:
                    await asyncio.sleep(1.0 * (2**attempt) + random.uniform(0, 0.25))
                    continue
            break

        raise SerpApiError(f"SerpAPI request failed after {self.retries} attempts: {last_error}")

    async def _discover_keyword(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        time_range: str,
    ) -> dict[str, GreenhouseJobRef]:
        refs: dict[str, GreenhouseJobRef] = {}
        start = 0
        query = build_query(keyword, self.location_query)

        for page in range(1, self.max_pages + 1):
            params = {
                "engine": "google",
                "q": query,
                "start": str(start),
                "num": str(self.results_per_page),
                "filter": "0",
                "hl": self.google_language,
                "gl": self.google_country,
            }
            if time_range != "any":
                params["tbs"] = f"qdr:{TIME_RANGE_CODES[time_range]}"

            payload = await self._serpapi_json(client, params)
            organic = payload.get("organic_results") or []
            self.stats["organic_results"] += len(organic)
            before = len(refs)
            for result in organic:
                if not isinstance(result, dict):
                    continue
                for url in _result_urls(result):
                    ref = parse_greenhouse_job_url(url)
                    if ref:
                        refs.setdefault(ref.url, ref)

            log.info(
                "%s: Google page %d/%d, organic=%d, new_jobs=%d, total=%d",
                keyword,
                page,
                self.max_pages,
                len(organic),
                len(refs) - before,
                len(refs),
            )
            pagination = payload.get("serpapi_pagination") or {}
            next_url = pagination.get("next")
            if not organic or not isinstance(next_url, str):
                break
            next_params = urllib.parse.parse_qs(urllib.parse.urlparse(next_url).query)
            try:
                next_start = int((next_params.get("start") or [start + self.results_per_page])[0])
            except (TypeError, ValueError):
                next_start = start + self.results_per_page
            if next_start <= start:
                break
            start = next_start

        return refs

    async def _greenhouse_json(
        self,
        client: httpx.AsyncClient,
        ref: GreenhouseJobRef,
    ) -> dict[str, Any] | None:
        board = urllib.parse.quote(ref.board_token, safe="")
        job_id = urllib.parse.quote(ref.job_id, safe="")
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
        last_error = "unknown Greenhouse API failure"

        for attempt in range(self.retries):
            try:
                response = await client.get(url)
                self.stats["greenhouse_requests"] += 1
                if response.status_code == 404:
                    self.stats["closed_or_missing_jobs"] += 1
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.retries - 1:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = min(float(retry_after), 30.0) if retry_after else 0.75 * (2**attempt)
                        except ValueError:
                            delay = 0.75 * (2**attempt)
                        await asyncio.sleep(delay + random.uniform(0, 0.2))
                        continue
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError, ValueError) as exc:
                last_error = type(exc).__name__
                if attempt < self.retries - 1:
                    await asyncio.sleep(0.75 * (2**attempt))
                    continue
            except httpx.HTTPStatusError as exc:
                last_error = f"HTTP {exc.response.status_code}"
            break
        raise RuntimeError(f"{ref.url}: {last_error}")

    async def scrape(
        self,
        keywords: list[str],
        time_range: str,
        max_jobs: int = 0,
    ) -> list[dict[str, Any]]:
        if time_range not in TIME_RANGES:
            raise ValueError(f"time_range must be one of: {', '.join(TIME_RANGES)}")

        role_words = {role: title_words(role) for role in keywords}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "JobRadar Greenhouse Google Discovery/1.0",
        }
        limits = httpx.Limits(
            max_connections=max(self.detail_concurrency + self.search_concurrency, 20),
            max_keepalive_connections=max(self.detail_concurrency, 10),
        )
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.timeout,
            follow_redirects=True,
            limits=limits,
        ) as client:
            search_sem = asyncio.Semaphore(self.search_concurrency)

            async def discover(keyword: str) -> tuple[str, dict[str, GreenhouseJobRef]]:
                async with search_sem:
                    return keyword, await self._discover_keyword(client, keyword, time_range)

            discovered = await asyncio.gather(*(discover(keyword) for keyword in keywords))
            refs: dict[str, GreenhouseJobRef] = {}
            for _keyword, keyword_refs in discovered:
                refs.update(keyword_refs)
            if max_jobs:
                refs = dict(list(refs.items())[:max_jobs])
            self.stats["unique_google_job_urls"] = len(refs)
            log.info("Google discovery complete: %d unique Greenhouse job URLs", len(refs))

            detail_sem = asyncio.Semaphore(self.detail_concurrency)
            discovered_at = datetime.now(timezone.utc).isoformat()

            async def fetch(ref: GreenhouseJobRef) -> dict[str, Any] | None:
                async with detail_sem:
                    raw = await self._greenhouse_json(client, ref)
                if not raw or not raw.get("title"):
                    return None
                if not _is_within_time_range(raw.get("updated_at"), time_range):
                    self.stats["outside_board_date_window"] += 1
                    return None
                words = title_words(str(raw.get("title") or ""))
                matched = [role for role, required in role_words.items() if required <= words]
                if not matched:
                    self.stats["title_mismatch"] += 1
                    return None
                job = normalize_api_job(raw, ref, discovered_at)
                job["scraper_type"] = "google_serpapi"
                job["search_provider"] = "SerpAPI Google Search"
                job["matched_keywords"] = matched
                return job

            detail_results = await asyncio.gather(
                *(fetch(ref) for ref in refs.values()),
                return_exceptions=True,
            )

        failures = [item for item in detail_results if isinstance(item, BaseException)]
        self.stats["detail_failures"] = len(failures)
        if failures and not self.allow_partial:
            raise RuntimeError(
                f"{len(failures)} Greenhouse detail requests failed after retries; "
                "no output was replaced. Re-run, or use --allow-partial intentionally. "
                f"First failure: {failures[0]}"
            )
        jobs = [item for item in detail_results if isinstance(item, dict)]
        jobs.sort(
            key=lambda job: (job.get("created_at") or "", job.get("job_title") or ""),
            reverse=True,
        )
        self.stats["jobs_collected"] = len(jobs)
        return jobs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find Google-indexed Greenhouse jobs through SerpAPI."
    )
    parser.add_argument("--keywords-file", type=Path, help="Text file with one role per line")
    parser.add_argument("--keyword", action="append", default=[], help="Role; repeat as needed")
    parser.add_argument("--time-range", choices=tuple(TIME_RANGES), default="month")
    parser.add_argument("--location-query", default="united states")
    parser.add_argument("--google-country", default="us")
    parser.add_argument("--google-language", default="en")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="Maximum pages per role")
    parser.add_argument("--results-per-page", type=int, default=DEFAULT_RESULTS_PER_PAGE)
    parser.add_argument("--search-concurrency", type=int, default=SEARCH_CONCURRENCY)
    parser.add_argument("--detail-concurrency", type=int, default=DETAIL_CONCURRENCY)
    parser.add_argument("--max-jobs", type=int, default=0, help="Limit unique URLs; 0 is unlimited")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be at least 1")
    if not 1 <= args.results_per_page <= 100:
        parser.error("--results-per-page must be between 1 and 100")
    if args.search_concurrency < 1 or args.detail_concurrency < 1:
        parser.error("concurrency values must be positive")
    if args.max_jobs < 0:
        parser.error("--max-jobs cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    if not SERPAPI_API_KEY:
        raise SystemExit(
            "SERPAPI_API_KEY is not set. Add it to the repository .env file, then run again."
        )
    try:
        keywords = load_keywords(args.keywords_file, args.keyword)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc

    max_searches = len(keywords) * args.pages
    print(
        f"Searching Google via SerpAPI for {len(keywords)} roles; "
        f"up to {max_searches} SerpAPI requests",
        flush=True,
    )
    scraper = SerpApiGreenhouseScraper(
        SERPAPI_API_KEY,
        max_pages=args.pages,
        results_per_page=args.results_per_page,
        search_concurrency=args.search_concurrency,
        detail_concurrency=args.detail_concurrency,
        google_country=args.google_country,
        google_language=args.google_language,
        location_query=args.location_query,
        allow_partial=args.allow_partial,
    )
    started = time.monotonic()
    try:
        jobs = asyncio.run(scraper.scrape(keywords, args.time_range, args.max_jobs))
    except (SerpApiError, RuntimeError, httpx.HTTPError) as exc:
        raise SystemExit(f"Scrape failed: {exc}") from exc

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output)
    print(f"Collected {len(jobs)} jobs in {time.monotonic() - started:.1f}s")
    print(f"Stats: {dict(scraper.stats)}")
    print(f"Saved: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted by user; existing output was not replaced.")
