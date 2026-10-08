#!/usr/bin/env python3
"""Scan known Greenhouse boards once and combine role matches into one JSON file."""

import argparse
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

import greenhouse_api as greenhouse


DEFAULT_OUTPUT = Path(__file__).with_name("greenhouse_role_jobs.json")
TIME_RANGES = ("hour", "day", "week", "month", "year", "any")


def title_words(value: str) -> set[str]:
    value = re.sub(r"\bsr\.?\b", "senior", value.lower())
    return set(re.findall(r"[a-z0-9]+", value))


def load_keywords(path: Path, inline_keywords: list[str]) -> list[str]:
    keywords = list(inline_keywords)
    if path:
        keywords.extend(path.read_text(encoding="utf-8").splitlines())

    unique = []
    seen = set()
    for keyword in keywords:
        keyword = keyword.strip()
        normalized = keyword.casefold()
        if keyword and normalized not in seen:
            seen.add(normalized)
            unique.append(keyword)
    if not unique:
        raise ValueError("Provide at least one role with --keywords-file or --keyword")
    return unique


async def scan(args: argparse.Namespace) -> list[dict]:
    keywords = load_keywords(args.keywords_file, args.keyword)
    role_words = {role: title_words(role) for role in keywords}
    companies = greenhouse.load_companies()
    greenhouse.SEARCH_ATTRIBUTES["post_time"] = args.time_range
    greenhouse.REQUEST_TIMEOUT = args.timeout
    greenhouse.RETRY_COUNT = args.retries

    scraper = greenhouse.DirectGreenhouseScraper(companies)
    semaphore = asyncio.Semaphore(args.concurrency)
    matched: dict[str, dict] = {}
    completed = 0
    unavailable = 0
    started = time.monotonic()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }

    print(
        f"Scanning {len(companies)} Greenhouse boards for {len(keywords)} roles "
        f"(time range: {args.time_range})",
        flush=True,
    )
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=args.timeout,
        limits=httpx.Limits(
            max_connections=max(args.concurrency, 100),
            max_keepalive_connections=min(args.concurrency, 200),
        ),
    ) as client:

        async def fetch_company(company: str) -> None:
            nonlocal completed, unavailable
            async with semaphore:
                result = await scraper._fetch_company_jobs(client, company, "", "")
            completed += 1
            unavailable += int(result.is_404)

            for job in result.jobs:
                words = title_words(job.get("job_title") or "")
                roles = [role for role, required in role_words.items() if required <= words]
                if not roles:
                    continue
                key = str(job.get("job_url") or job.get("id"))
                if key in matched:
                    matched[key]["matched_keywords"] = sorted(
                        set(matched[key]["matched_keywords"]) | set(roles)
                    )
                else:
                    job["matched_keywords"] = roles
                    matched[key] = job

            if completed % args.progress_every == 0 or completed == len(companies):
                elapsed = max(time.monotonic() - started, 0.001)
                rate = completed / elapsed
                eta = (len(companies) - completed) / rate
                print(
                    f"  boards {completed}/{len(companies)} | matches={len(matched)} | "
                    f"{rate:.1f}/s | ETA ~{eta / 60:.1f}m",
                    flush=True,
                )

        await asyncio.gather(*(fetch_company(company) for company in companies))

    jobs = list(matched.values())
    jobs.sort(
        key=lambda job: (job.get("created_at") or "", job.get("job_title") or ""),
        reverse=True,
    )
    print(
        f"Finished: jobs={len(jobs)} unavailable_boards={unavailable} "
        f"elapsed={time.monotonic() - started:.1f}s",
        flush=True,
    )
    return jobs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find role titles across known Greenhouse boards and save one JSON file."
    )
    parser.add_argument("--keywords-file", type=Path, help="Text file with one role per line")
    parser.add_argument(
        "--keyword", action="append", default=[], help="Role to match; repeat as needed"
    )
    parser.add_argument("--time-range", choices=TIME_RANGES, default="month")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--progress-every", type=int, default=250)
    args = parser.parse_args()
    if args.concurrency < 1 or args.timeout <= 0 or args.retries < 1:
        parser.error("concurrency, timeout, and retries must be positive")
    return args


def main() -> int:
    args = parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        jobs = asyncio.run(scan(args))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(jobs)} jobs to {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted by user.")
