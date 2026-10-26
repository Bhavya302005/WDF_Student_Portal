"""
LinkedIn's public job search (no login), one batch of results at a time.

The search page returns 10 jobs per request and refuses to go past result 1,000, so one search can never
show more than about 1,000 jobs (linkedin_68_roles.py splits a search that reaches it). A batch comes
back as a DataFrame with the columns JobSpy used, so the scraper's row handling is unchanged.

A request every 3.5-6.5 s: LinkedIn refuses shared GitHub runners after a few faster ones.
Rate limits (HTTP 429) raise an error mentioning 429, so the caller pauses and retries the batch instead of
taking a short batch for the end of the results.
"""
from __future__ import annotations

import html
import random
import re
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
import pandas as pd

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE = 10
LIMIT = 1000  # LinkedIn answers HTTP 400 from this result on
# LinkedIn's public search ignores its work-type (f_WT) and job-type (f_JT) filters: the same jobs come back
# whatever they say. A search that reaches the 1,000 limit is split by place instead, which it does honour.
US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas",
    "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]
WORK_TYPES = {"1": "on-site", "2": "remote", "3": "hybrid"}  # kept for callers; the filter has no effect
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_CARD = re.compile(r'<li>(.*?)</li>', re.S)
_ID = re.compile(r"urn:li:jobPosting:(\d+)")
_TITLE = re.compile(r'base-search-card__title">\s*(.*?)\s*</h3>', re.S)
_COMPANY = re.compile(r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*(.*?)\s*(?:</a>)?\s*</h4>', re.S)
_LOCATION = re.compile(r'job-search-card__location">\s*(.*?)\s*</span>', re.S)
_DATE = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"')
_SALARY = re.compile(r'job-search-card__salary-info">\s*(.*?)\s*</span>', re.S)

_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True)
    return _client


def _text(pattern: re.Pattern, block: str) -> str:
    match = pattern.search(block)
    return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else ""


def _salary(text: str) -> tuple[float | None, float | None, str, str]:
    """ "$80,000.00 - $95,000.00" (per year) -> (80000, 95000, USD, year). """
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\$([\d,]+(?:\.\d+)?)", text)]
    if not amounts:
        return None, None, "", ""
    period = "hour" if max(amounts) < 500 else "year"
    return min(amounts), max(amounts), "USD", period


def parse_cards(page_html: str) -> list[dict]:
    rows = []
    for block in _CARD.findall(page_html):
        job_id = _ID.search(block)
        if not job_id:
            continue
        date = _DATE.search(block)
        low, high, currency, period = _salary(_text(_SALARY, block))
        rows.append({
            "id": job_id.group(1),
            "title": _text(_TITLE, block),
            "company": _text(_COMPANY, block),
            "location": _text(_LOCATION, block),
            "job_url": f"https://www.linkedin.com/jobs/view/{job_id.group(1)}",
            "date_posted": datetime.strptime(date.group(1), "%Y-%m-%d").date() if date else None,
            "min_amount": low, "max_amount": high, "currency": currency, "interval": period,
            "job_type": "", "is_remote": None, "job_level": "", "description": "", "job_url_direct": None,
        })
    return rows


def search_batch(role: str, location: str, hours_old: int | None, offset: int, size: int,
                 work_type: str | None = None, pause: tuple[float, float] = (3.5, 6.5),
                 filters: dict[str, str] | None = None) -> pd.DataFrame:
    """Results offset .. offset+size of one search, 10 per request.

    df.attrs["end"] is True when LinkedIn has no more results (an empty page, or its 1,000 limit).
    """
    query = {"keywords": role, "location": location}
    if hours_old:
        query["f_TPR"] = f"r{int(hours_old) * 3600}"
    if work_type:
        query["f_WT"] = work_type
    if filters:  # other LinkedIn filters: f_JT job type (F,P,C,T,I...), f_E experience level (1-6), f_SB2 salary...
        query.update(filters)
    rows: list[dict] = []
    end = False
    for start in range(offset, offset + size, PAGE):
        if start >= LIMIT:
            end = True
            break
        response = _http().get(f"{SEARCH}?{urlencode(query)}&start={start}")
        if response.status_code == 429:
            raise RuntimeError(f"429 rate limit at start={start}")
        if response.status_code == 400 and start >= LIMIT - 100:
            end = True  # past LinkedIn's limit
            break
        response.raise_for_status()
        cards = parse_cards(response.text)
        if not cards:
            end = True
            break
        rows += cards
        time.sleep(random.uniform(*pause))
    frame = pd.DataFrame(rows)
    frame.attrs["end"] = end
    return frame
