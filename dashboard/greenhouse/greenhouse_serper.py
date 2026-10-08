#!/usr/bin/env python3
"""Discover Google-indexed Greenhouse jobs with Serper and enrich via Greenhouse."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import random
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from greenhouse_serpapi import (
    SerpApiError,
    SerpApiGreenhouseScraper,
    _result_urls,
    load_keywords,
    parse_args,
    parse_greenhouse_job_url,
)


load_dotenv()

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPER_ENDPOINT = os.getenv("SERPER_ENDPOINT", "https://google.serper.dev/search")
DEFAULT_OUTPUT = Path(__file__).with_name("greenhouse_serper_jobs.json")

log = logging.getLogger("greenhouse_serper")


class SerperError(SerpApiError):
    pass


def build_serper_query(keyword: str, location_query: str) -> str:
    """Build a Serper query — free plan forbids quotes and site: operators."""
    plain_keyword = keyword.replace('"', "")
    parts = [plain_keyword, "greenhouse.io"]
    if location_query.strip():
        parts.append(location_query.strip())
    return " ".join(parts)


class SerperGreenhouseScraper(SerpApiGreenhouseScraper):
    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self.endpoint = SERPER_ENDPOINT

    async def scrape(
        self,
        keywords: list[str],
        time_range: str,
        max_jobs: int = 0,
    ) -> list[dict[str, Any]]:
        """Override parent scrape to use relaxed title matching.

        Google/Serper already returns role-relevant results, so we skip the
        strict "all keyword words must appear in title" filter from the parent
        and instead keep any valid Greenhouse job within the time window.
        """
        from greenhouse_serpapi import TIME_RANGES, title_words, _is_within_time_range
        from greenhouse_searxng import GreenhouseJobRef, normalize_api_job

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
            from datetime import datetime, timezone
            discovered_at = datetime.now(timezone.utc).isoformat()

            async def fetch(ref: GreenhouseJobRef) -> dict[str, Any] | None:
                async with detail_sem:
                    raw = await self._greenhouse_json(client, ref)
                if not raw or not raw.get("title"):
                    return None
                if not _is_within_time_range(raw.get("updated_at"), time_range):
                    self.stats["outside_board_date_window"] += 1
                    return None
                # Relaxed matching: check if ANY keyword has ≥50% word overlap
                words = title_words(str(raw.get("title") or ""))
                matched = []
                for role, required in role_words.items():
                    overlap = len(required & words)
                    if overlap >= max(1, len(required) // 2):
                        matched.append(role)
                if not matched:
                    self.stats["title_mismatch"] += 1
                    return None
                job = normalize_api_job(raw, ref, discovered_at)
                job["scraper_type"] = "google_serper"
                job["search_provider"] = "Serper Google Search"
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
                f"First failure: {failures[0]}"
            )
        jobs = [item for item in detail_results if isinstance(item, dict)]
        jobs.sort(
            key=lambda job: (job.get("created_at") or "", job.get("job_title") or ""),
            reverse=True,
        )
        self.stats["jobs_collected"] = len(jobs)
        return jobs

    async def _serper_json(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        last_error = "unknown Serper failure"
        for attempt in range(self.retries):
            try:
                response = await client.post(
                    self.endpoint,
                    headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                    json=payload,
                )
                self.stats["serper_requests"] += 1
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.retries - 1:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = min(float(retry_after), 60.0) if retry_after else 1.0 * (2**attempt)
                        except ValueError:
                            delay = 1.0 * (2**attempt)
                        await asyncio.sleep(delay + random.uniform(0, 0.25))
                        continue
                response.raise_for_status()
                data = response.json()
                error = data.get("error") or data.get("message")
                if error:
                    raise SerperError(str(error))
                return data
            except SerperError:
                raise
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                try:
                    data = exc.response.json()
                    message = str(data.get("message") or data.get("error") or f"HTTP {code}")
                except ValueError:
                    message = f"HTTP {code}"
                if code in (400, 401, 403, 429):
                    raise SerperError(message) from exc
                last_error = message
            except (httpx.TimeoutException, httpx.NetworkError, ValueError) as exc:
                last_error = type(exc).__name__
                if attempt < self.retries - 1:
                    await asyncio.sleep(1.0 * (2**attempt) + random.uniform(0, 0.25))
                    continue
            break
        raise SerperError(f"Serper request failed after {self.retries} attempts: {last_error}")

    async def _discover_keyword(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        time_range: str,
    ) -> dict[str, Any]:
        refs: dict[str, Any] = {}
        query = build_serper_query(keyword, self.location_query)
        consecutive_empty = 0

        for page in range(1, self.max_pages + 1):
            payload: dict[str, Any] = {
                "q": query,
                "gl": self.google_country,
                "hl": self.google_language,
                "num": min(self.results_per_page, 10),  # free plan caps at 10
                "page": page,
            }
            if time_range != "any":
                from greenhouse_serpapi import TIME_RANGE_CODES

                payload["tbs"] = f"qdr:{TIME_RANGE_CODES[time_range]}"

            data = await self._serper_json(client, payload)
            organic = data.get("organic") or []
            self.stats["organic_results"] += len(organic)
            before = len(refs)
            for result in organic:
                if not isinstance(result, dict):
                    continue
                for url in _result_urls(result):
                    ref = parse_greenhouse_job_url(url)
                    if ref:
                        refs.setdefault(ref.url, ref)

            added = len(refs) - before
            log.info(
                "%s: Google page %d/%d, organic=%d, new_jobs=%d, total=%d",
                keyword,
                page,
                self.max_pages,
                len(organic),
                added,
                len(refs),
            )
            if not organic:
                break
            # Stop early if two consecutive pages yield no new greenhouse URLs
            if added == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    log.info("%s: stopping early — 2 consecutive pages with no new jobs", keyword)
                    break
            else:
                consecutive_empty = 0

        return refs


def main() -> int:
    args = parse_args()
    if args.output == Path(__file__).with_name("greenhouse_serpapi_jobs.json"):
        args.output = DEFAULT_OUTPUT
    if not SERPER_API_KEY:
        raise SystemExit("SERPER_API_KEY is not set in the repository .env file.")
    try:
        keywords = load_keywords(args.keywords_file, args.keyword)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc

    max_searches = len(keywords) * args.pages
    print(
        f"Searching live Google results through Serper for {len(keywords)} roles; "
        f"up to {max_searches} search credits",
        flush=True,
    )
    scraper = SerperGreenhouseScraper(
        SERPER_API_KEY,
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
    except (SerperError, RuntimeError, httpx.HTTPError) as exc:
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
