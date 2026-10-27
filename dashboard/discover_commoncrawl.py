"""
Find companies on an ATS platform from the Common Crawl URL index, check each against the platform's
API, and add the live ones to that platform's company list.

Common Crawl (index.commoncrawl.org) publishes which URLs its monthly web crawls saw. Asking it for
"*.myworkdayjobs.com" lists the Workday career sites anyone links to; each URL is cut down to the
platform's company id (Workday "tenant/site", Oracle "host/site"), probed once, and kept only when the
platform answers with public jobs. This is how freehire built its board catalogue
(cmd/harvest-boards/commoncrawl.go); the index is free and public.

    python discover_commoncrawl.py workday            # report only
    python discover_commoncrawl.py oracle --apply     # also add the new companies to oracle/oracle.csv

Polite by design: the index is read one page at a time, and probes run a few at a time per run.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("discover_commoncrawl")
HERE = Path(__file__).parent
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
UA = {"User-Agent": "getworkin-company-discovery/1.0 (job listings aggregator)"}
SNAPSHOTS = 12    # the latest monthly crawls
MAX_PAGES = 40    # index pages per snapshot (as freehire)
PROBES = 8        # probes at a time

_LEGAL = re.compile(r"[,.]?\s+(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|s\.?a\.?|n\.?v\.?|b\.?v\.?|"
                    r"s\.?a\.? de c\.?v\.?|sociedad an[oó]nima.*|pty|pvt|private)\.?$", re.I)
_BOILER = re.compile(r"\b(careers?( at| site| page| portal)?|jobs?( at| site| board| portal| search)?|external|"
                     r"candidate experience( page)?|recruiting|opportunities|work(ing)? (at|with)|join( us)?|home|welcome( to)?)\b", re.I)


def clean_name(value: str) -> str:
    value = re.sub(r"^\s*\d+\s+", "", value or "")  # Workday entity codes: "0010 eBay"
    value = _BOILER.sub(" ", value)
    value = re.sub(r"[\s\-_|:()]+", " ", value).strip(" -,")
    for _ in range(3):
        value = _LEGAL.sub("", value).strip(" ,")
    return value


@dataclass
class Found:
    slug: str
    name: str
    total: int
    sample: frozenset = frozenset()  # what the site lists first: sites of one tenant sharing most of it are one list


@dataclass
class Platform:
    key: str
    pattern: str                                  # Common Crawl url= pattern
    csv_path: Path
    slug_of: Callable[[str], str | None]          # crawled URL -> company id, or None
    probe: Callable                               # async (client, slug) -> Found | None
    row: Callable[[Found], dict]                  # Found -> CSV row
    tenant_of: Callable[[str], str]               # company id -> tenant (sites of one tenant may repeat jobs)


# ----- Workday ------------------------------------------------------------------------------------------
_WD_HOST = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
_WD_LOCALE = re.compile(r"^[a-z]{2}-[A-Za-z]{2}$")
_WD_NON_SITE = re.compile(r"^(?i:robots\.txt|sitemap.*|favicon.*|[a-z]{2}|wday|static|assets|api|images?|job|details)$")


def workday_slug(url: str) -> str | None:
    parts = httpx.URL(url)
    host = (parts.host or "").lower()
    match = _WD_HOST.match(host)
    if not match:
        return None
    for part in parts.path.split("/"):
        if not part or _WD_LOCALE.match(part):
            continue
        if _WD_NON_SITE.match(part):
            return None
        return f"{match.group(1)}.{match.group(2)}/{part}"
    return None


async def workday_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    host_part, site = slug.split("/", 1)
    tenant, instance = host_part.split(".", 1)
    api = f"https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    response = await client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
                                 headers={"Accept": "application/json", "Content-Type": "application/json"})
    if response.status_code != 200:
        return None
    data = response.json()
    postings = data.get("jobPostings") or []
    total = int(data.get("total") or 0)
    if not total or not postings:
        return None
    first = postings[0].get("externalPath") or ""
    name = ""
    page = await client.get(f"https://{tenant}.{instance}.myworkdayjobs.com/{site}", headers={"Accept": "text/html"})
    title = re.search(r'property="og:title" content="([^"]*)"', page.text or "")
    if title:
        name = clean_name(title.group(1))
    if len(name) < 2 and first:
        detail = await client.get(f"{api}{first}", headers={"Accept": "application/json"})
        if detail.status_code == 200:
            organisation = clean_name((detail.json().get("hiringOrganization") or {}).get("name") or "")
            if organisation and len(organisation.split()) <= 4:
                name = organisation
    if len(name) < 2:
        name = tenant.upper() if len(tenant) <= 3 else tenant.replace("-", " ").title()
    return Found(slug, name, total, frozenset(p.get("externalPath") for p in postings if p.get("externalPath")))


def workday_row(found: Found) -> dict:
    host_part, site = found.slug.split("/", 1)
    tenant, instance = host_part.split(".", 1)
    return {"name": found.name, "slug": f"{tenant}/{site}", "url": f"https://{tenant}.{instance}.myworkdayjobs.com/{site}"}


# ----- Oracle -------------------------------------------------------------------------------------------
_ORACLE = re.compile(r"https?://([a-z0-9.-]+\.oraclecloud\.com)(?::\d+)?/hcmUI/CandidateExperience/[a-z_-]+/sites/([A-Za-z0-9_]+)", re.I)
_TEST_HOST = re.compile(r"^[a-z0-9]+-(dev|test|tst|uat|stg|stage|stag|pre|sit|qa|trn|train|demo)\d*\.", re.I)


def oracle_slug(url: str) -> str | None:
    match = _ORACLE.search(url)
    if not match or _TEST_HOST.match(match.group(1).lower()):
        return None
    return f"{match.group(1).lower()}/{match.group(2)}"


async def oracle_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    host, site = slug.split("/", 1)
    response = await client.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                                f"&expand=requisitionList&finder=findReqs;siteNumber={site},limit=1,offset=0",
                                headers={"Accept": "application/json"})
    if response.status_code != 200:
        return None
    page = (response.json().get("items") or [{}])[0]
    total, listed = int(page.get("TotalJobsCount") or 0), page.get("requisitionList") or []
    if not total or not listed:
        return None
    sites = await client.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCESites?onlyData=true&q=SiteNumber='{site}'",
                             headers={"Accept": "application/json"})
    info = ((sites.json().get("items") or [{}])[0]) if sites.status_code == 200 else {}
    raw = info.get("SiteName") or ""
    if "internal" in raw.lower() or info.get("StatusCode") not in (None, "", "ORA_ACTIVE"):
        return None
    name = clean_name(raw)
    if len(name) < 3:
        careers = await client.get(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}", headers={"Accept": "text/html"})
        title = re.search(r"<title>(.*?)</title>", careers.text or "", re.S | re.I)
        name = clean_name(title.group(1)) if title else ""
    if len(name) < 3:
        return None  # an unnamed site would show a host code as the company
    return Found(slug, name, total, frozenset(str(p.get("Id")) for p in listed))


def oracle_row(found: Found) -> dict:
    host, site = found.slug.split("/", 1)
    return {"name": found.name, "slug": found.slug, "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/jobs"}


# ----- Greenhouse --------------------------------------------------------------------------------------
def greenhouse_slug(url: str) -> str | None:
    match = re.search(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/)?([a-z0-9_-]+)", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("embed", "api", "static", "assets", "docs", "v1"):
        return None
    return slug


async def greenhouse_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs") or []
    if not jobs:
        return None
    name = clean_name(jobs[0].get("company", {}).get("name", "")) if jobs else slug
    return Found(slug, name or slug, len(jobs))


def greenhouse_row(found: Found) -> dict:
    return {"slug": found.slug}


# ----- Lever -------------------------------------------------------------------------------------------
def lever_slug(url: str) -> str | None:
    match = re.search(r"jobs\.lever\.co/([a-z0-9_-]+)", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("api", "static", "assets"):
        return None
    return slug


async def lever_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15)
    if r.status_code != 200:
        return None
    jobs = r.json() if isinstance(r.json(), list) else []
    if not jobs:
        return None
    name = clean_name(jobs[0].get("categories", {}).get("team", "")) if jobs else slug
    return Found(slug, name or slug, len(jobs))


def lever_row(found: Found) -> dict:
    return {"slug": found.slug}


# ----- Ashby -------------------------------------------------------------------------------------------
def ashby_slug(url: str) -> str | None:
    match = re.search(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("api", "static", "assets"):
        return None
    return slug


async def ashby_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs") or []
    if not jobs:
        return None
    name = slug
    return Found(slug, name, len(jobs))


def ashby_row(found: Found) -> dict:
    return {"slug": found.slug}


# ----- SmartRecruiters ---------------------------------------------------------------------------------
def smartrecruiter_slug(url: str) -> str | None:
    match = re.search(r"jobs\.smartrecruiters\.com/([a-z0-9_.-]+?)(?:/|$)", url, re.I)
    if not match:
        return None
    slug = match.group(1)
    if slug.lower() in ("api", "static", "oneclick-recruiter"):
        return None
    return slug


async def smartrecruiter_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://jobs.smartrecruiters.com/{slug}", timeout=15)
    if r.status_code != 200 or "/404" in str(r.url):
        return None
    title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
    name = clean_name(title.group(1)) if title else slug
    has_jobs = bool(re.search(r'"totalFound"\s*:\s*(\d+)', r.text or ""))
    if not has_jobs:
        return None
    total = int(re.search(r'"totalFound"\s*:\s*(\d+)', r.text).group(1))
    return Found(slug, name or slug, total)


def smartrecruiter_row(found: Found) -> dict:
    return {"slug": found.slug}


# ----- Jobvite -----------------------------------------------------------------------------------------
def jobvite_slug(url: str) -> str | None:
    match = re.search(r"jobs\.jobvite\.com/([a-z0-9_-]+)", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("api", "static", "support"):
        return None
    return slug


async def jobvite_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://jobs.jobvite.com/{slug}", follow_redirects=True, timeout=15)
    if r.status_code != 200 or "support" in str(r.url).lower():
        return None
    title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
    name = clean_name(title.group(1)) if title else slug
    return Found(slug, name or slug, 1)


def jobvite_row(found: Found) -> dict:
    return {"slug": found.slug}


# ----- iCIMS -------------------------------------------------------------------------------------------
_ICIMS = re.compile(r"careers[.-]([a-z0-9_-]+)\.icims\.com", re.I)


def icims_slug(url: str) -> str | None:
    match = _ICIMS.search(url)
    return match.group(1).lower() if match else None


async def icims_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://careers-{slug}.icims.com/jobs/search", timeout=15, follow_redirects=True)
    if r.status_code != 200:
        return None
    title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
    name = clean_name(title.group(1)) if title else slug
    if not re.search(r'class="[^"]*iCIMS_JobsTable', r.text or ""):
        return None
    return Found(slug, name or slug, 1)


def icims_row(found: Found) -> dict:
    return {"name": found.name, "slug": found.slug, "url": f"https://careers-{found.slug}.icims.com"}


# ----- BambooHR ---------------------------------------------------------------------------------------
def bamboohr_slug(url: str) -> str | None:
    match = re.search(r"([a-z0-9_-]+)\.bamboohr\.com", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("www", "api", "app", "support", "help"):
        return None
    return slug


async def bamboohr_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://{slug}.bamboohr.com/careers/list", timeout=15, follow_redirects=True)
    if r.status_code != 200:
        return None
    jobs = re.findall(r'class="[^"]*BambooHR-ATS-board__job', r.text or "")
    if not jobs:
        return None
    title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
    name = clean_name(title.group(1)) if title else slug
    return Found(slug, name or slug, len(jobs))


def bamboohr_row(found: Found) -> dict:
    return {"name": found.name, "slug": found.slug, "url": f"https://{found.slug}.bamboohr.com/careers"}


# ----- Teamtailor --------------------------------------------------------------------------------------
def teamtailor_slug(url: str) -> str | None:
    match = re.search(r"([a-z0-9_-]+)\.teamtailor\.com", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("www", "api", "app", "support", "help", "docs"):
        return None
    return slug


async def teamtailor_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://{slug}.teamtailor.com/jobs.rss", timeout=15)
    if r.status_code != 200:
        return None
    items = re.findall(r"<item>", r.text or "")
    if not items:
        return None
    title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
    name = clean_name(title.group(1)) if title else slug
    return Found(slug, name or slug, len(items))


def teamtailor_row(found: Found) -> dict:
    return {"name": found.name, "slug": found.slug, "url": f"https://{found.slug}.teamtailor.com"}


# ----- Workable ----------------------------------------------------------------------------------------
def workable_slug(url: str) -> str | None:
    match = re.search(r"apply\.workable\.com/([a-z0-9_-]+)", url, re.I)
    if not match:
        return None
    slug = match.group(1).lower()
    if slug in ("api", "static", "assets", "o", "j"):
        return None
    return slug


async def workable_probe(client: httpx.AsyncClient, slug: str) -> Found | None:
    r = await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    jobs = data.get("jobs") or []
    if not jobs:
        return None
    name = clean_name(data.get("name", "")) or slug
    return Found(slug, name, len(jobs))


def workable_row(found: Found) -> dict:
    return {"name": found.name, "slug": found.slug, "url": f"https://apply.workable.com/{found.slug}"}


PLATFORMS = {
    "workday": Platform("workday", "*.myworkdayjobs.com", HERE / "workday" / "workday.csv", workday_slug, workday_probe,
                        workday_row, tenant_of=lambda slug: slug.split("/")[0]),
    "oracle": Platform("oracle", "*.oraclecloud.com/hcmUI/CandidateExperience/*", HERE / "oracle" / "oracle.csv",
                       oracle_slug, oracle_probe, oracle_row, tenant_of=lambda slug: slug.split("/")[0]),
    "greenhouse": Platform("greenhouse", "*.greenhouse.io/*", HERE / "greenhouse" / "greenhouse_companies.txt",
                           greenhouse_slug, greenhouse_probe, greenhouse_row, tenant_of=lambda slug: slug),
    "lever": Platform("lever", "jobs.lever.co/*", HERE / "lever" / "lever_companies.txt",
                      lever_slug, lever_probe, lever_row, tenant_of=lambda slug: slug),
    "ashby": Platform("ashby", "jobs.ashbyhq.com/*", HERE / "ashby" / "ashby_companies.txt",
                      ashby_slug, ashby_probe, ashby_row, tenant_of=lambda slug: slug),
    "smartrecruiter": Platform("smartrecruiter", "jobs.smartrecruiters.com/*", HERE / "smartrecruiter" / "smartrecruiter_companies.txt",
                               smartrecruiter_slug, smartrecruiter_probe, smartrecruiter_row, tenant_of=lambda slug: slug),
    "jobvite": Platform("jobvite", "jobs.jobvite.com/*", HERE / "jobvite" / "jobvite_companies.txt",
                        jobvite_slug, jobvite_probe, jobvite_row, tenant_of=lambda slug: slug),
    "icims": Platform("icims", "*.icims.com/*", HERE / "icims" / "icims.csv",
                      icims_slug, icims_probe, icims_row, tenant_of=lambda slug: slug),
    "bamboohr": Platform("bamboohr", "*.bamboohr.com/*", HERE / "bamboohr" / "bamboohr.csv",
                         bamboohr_slug, bamboohr_probe, bamboohr_row, tenant_of=lambda slug: slug),
    "teamtailor": Platform("teamtailor", "*.teamtailor.com/*", HERE / "teamtailor" / "teamtailor.csv",
                           teamtailor_slug, teamtailor_probe, teamtailor_row, tenant_of=lambda slug: slug),
    "workable": Platform("workable", "apply.workable.com/*", HERE / "workable" / "workable.csv",
                         workable_slug, workable_probe, workable_row, tenant_of=lambda slug: slug),
}


# ----- the index ----------------------------------------------------------------------------------------
async def _get_text(client: httpx.AsyncClient, url: str, params: dict) -> str:
    """One index page. The index sometimes cuts a response short: keep the complete lines read before the cut."""
    for attempt in range(4):
        chunks = []
        try:
            async with client.stream("GET", url, params=params, timeout=120) as response:
                if response.status_code == 503 or response.status_code == 429:
                    raise httpx.HTTPStatusError("busy", request=response.request, response=response)
                response.raise_for_status()
                async for chunk in response.aiter_text():
                    chunks.append(chunk)
            return "".join(chunks)
        except (httpx.HTTPError, httpx.StreamError) as exc:
            text = "".join(chunks)
            if text.count("\n") > 10:
                log.info("  index page cut short (%s); keeping %d lines", type(exc).__name__, text.count("\n"))
                return text.rsplit("\n", 1)[0]
            await asyncio.sleep(5 * (attempt + 1))
    return ""


async def harvest(client: httpx.AsyncClient, platform: Platform, snapshots: int = SNAPSHOTS,
                  max_pages: int = MAX_PAGES) -> set[str]:
    collections = (await client.get(COLLINFO, timeout=60)).json()[:snapshots]
    slugs: set[str] = set()
    for collection in collections:
        api = collection["cdx-api"]
        try:
            count = json.loads(await _get_text(client, api, {"url": platform.pattern, "output": "json", "showNumPages": "true"}))["pages"]
        except (ValueError, KeyError):
            log.warning("  %s: page count unavailable, snapshot skipped", collection["id"])
            continue
        for page in range(min(count, max_pages)):
            for line in (await _get_text(client, api, {"url": platform.pattern, "output": "json", "page": page})).splitlines():
                try:
                    slug = platform.slug_of(json.loads(line)["url"])
                except (ValueError, KeyError):
                    continue
                if slug:
                    slugs.add(slug)
            await asyncio.sleep(1)  # one page at a time, with a pause: the index is a shared public service
        log.info("  %s: %d index pages, %d candidates so far", collection["id"], min(count, max_pages), len(slugs))
    return slugs


def _is_txt(platform: Platform) -> bool:
    return platform.csv_path.suffix == ".txt"


def _known(platform: Platform) -> tuple[list[dict], set[str]]:
    rows = []
    if not platform.csv_path.exists():
        return rows, set()
    if _is_txt(platform):
        with open(platform.csv_path, encoding="utf-8") as handle:
            slugs = set()
            for line in handle:
                slug = line.strip().lstrip("#")
                if slug:
                    slugs.add(slug.lower())
                    rows.append({"slug": slug})
        # also load 404 file
        p404 = platform.csv_path.with_name(platform.csv_path.stem.replace("_companies", "_404_companies") + ".txt")
        if p404.exists():
            with open(p404, encoding="utf-8") as handle:
                for line in handle:
                    slug = line.strip().split(",")[0].strip()
                    if slug:
                        slugs.add(slug.lower())
        return rows, slugs
    with open(platform.csv_path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows, {row["slug"].lower() for row in rows}


def _overlaps(a: frozenset, b: frozenset) -> bool:
    return bool(a) and bool(b) and len(a & b) > 0.5 * min(len(a), len(b))


async def _distinct_lists(client, platform: Platform, found: list[Found], known: set[str], probe) -> list[Found]:
    """Sites of one tenant often show the same jobs (external, internal, per-brand copies). A new site is kept
    only when most of what it lists first is not on a site already kept or already on our list."""
    tenants = {platform.tenant_of(f.slug).split(".")[0] for f in found}
    kept: dict[str, list[frozenset]] = {}
    # what our current sites of those tenants list first (Workday CSV rows store tenant/site)
    current = [k for k in known if k.split("/")[0].split(".")[0] in tenants]
    if platform.key == "workday" and current:
        rows = {r["slug"].lower(): r for r in _known(platform)[0]}
        hosts = [httpx.URL(rows[k]["url"]).host.split(".myworkdayjobs.com")[0] + "/" + k.split("/", 1)[1] for k in current if k in rows]
        for item in await asyncio.gather(*(probe(h) for h in hosts)):
            if item:
                kept.setdefault(item.slug.split(".")[0], []).append(item.sample)
    unique = []
    for item in sorted(found, key=lambda f: -f.total):
        tenant = platform.tenant_of(item.slug).split(".")[0]
        if any(_overlaps(item.sample, other) for other in kept.get(tenant, [])):
            continue
        kept.setdefault(tenant, []).append(item.sample)
        unique.append(item)
    return unique


def _candidate_key(platform: Platform, slug: str) -> str:
    """A candidate in the form the CSV stores, lower-cased (Workday rows drop the wdN instance)."""
    return platform.row(Found(slug, "", 0)).get("slug", slug).lower()


async def discover(platform: Platform, apply: bool, snapshots: int, max_pages: int) -> dict:
    rows, known = _known(platform)
    async with httpx.AsyncClient(headers=UA, follow_redirects=True, timeout=40) as client:
        candidates = await harvest(client, platform, snapshots, max_pages)
        new = sorted(slug for slug in candidates if _candidate_key(platform, slug) not in known)
        log.info("%s: %d candidates, %d not in %s", platform.key, len(candidates), len(new), platform.csv_path.name)
        semaphore = asyncio.Semaphore(PROBES)

        async def probe(slug):
            async with semaphore:
                try:
                    return await platform.probe(client, slug)
                except (httpx.HTTPError, ValueError, KeyError, IndexError):
                    return None
        found = [f for f in await asyncio.gather(*(probe(s) for s in new)) if f]

        unique = await _distinct_lists(client, platform, found, known, probe)

    report = {"platform": platform.key, "candidates": len(candidates), "new_candidates": len(new),
              "live": len(found), "added": len(unique), "jobs_listed": sum(f.total for f in unique),
              "top": [(f.name, f.total) for f in unique[:15]]}
    if apply and unique:
        if _is_txt(platform):
            existing = set()
            if platform.csv_path.exists():
                with open(platform.csv_path, encoding="utf-8") as handle:
                    existing = {line.strip().lstrip("#") for line in handle if line.strip()}
            new_slugs = [f.slug for f in unique if f.slug not in existing]
            if new_slugs:
                with open(platform.csv_path, "a", encoding="utf-8") as handle:
                    for slug in sorted(new_slugs):
                        handle.write(slug + "\n")
            log.info("%s: added %d companies to %s", platform.key, len(new_slugs), platform.csv_path)
        else:
            fields = list(rows[0].keys()) if rows else ["name", "slug", "url"]
            added = [platform.row(f) for f in unique]
            with open(platform.csv_path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(sorted(rows + added, key=lambda r: r["name"].lower()))
            log.info("%s: added %d companies to %s", platform.key, len(added), platform.csv_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("platform", choices=sorted(PLATFORMS))
    parser.add_argument("--apply", action="store_true", help="add the live new companies to the platform's CSV")
    parser.add_argument("--snapshots", type=int, default=SNAPSHOTS)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    report = asyncio.run(discover(PLATFORMS[args.platform], args.apply, args.snapshots, args.max_pages))
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
