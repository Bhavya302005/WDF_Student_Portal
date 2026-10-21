"""Find ATS companies we do not scrape yet, from the apply links of LinkedIn/Indeed jobs.

A job found on LinkedIn or Indeed often links straight to the employer's own applicant
tracking system (https://jobs.lever.co/acme/..., https://acme.wd5.myworkdayjobs.com/...).
That is a company known to use an ATS we have a scraper for, with a live posting, so its
slug is a good addition to that scraper's company list.

    python discover_ats_companies.py --jobs strict_jobs.csv other_jobs.csv          # report only
    python discover_ats_companies.py --jobs strict_jobs.csv --apply                 # add to the lists
    python discover_ats_companies.py --neon --apply                                 # use jobs stored in Neon

Companies already in a list (including disabled "#slug" lines and *_404_companies.txt) are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
csv.field_size_limit(sys.maxsize)

# board -> (list file, format). "txt": one slug per line; "csv": name,slug,url rows.
LISTS = {
    "Greenhouse": ("greenhouse/greenhouse_companies.txt", "txt"),
    "Lever": ("lever/lever_companies.txt", "txt"),
    "Ashby": ("ashby/ashby_companies.txt", "txt"),
    "SmartRecruiters": ("smartrecruiter/smartrecruiter_companies.txt", "txt"),
    "Workable": ("workable/workable_companies.txt", "txt"),
    "BambooHR": ("bamboohr/bamboohr_companies.txt", "txt"),
    "iCIMS": ("icims/icims_companies.txt", "txt"),
    "Jobvite": ("jobvite/jobvite_companies.txt", "txt"),
    "Teamtailor": ("teamtailor/teamtailor.csv", "csv"),
    "Workday": ("workday/workday.csv", "csv"),
    "Adp": ("adp/adp.csv", "csv"),
}

# Platforms seen in apply links that we have no scraper for (reported, never added).
UNSUPPORTED = {
    "paylocity.com": "Paylocity", "dayforcehcm.com": "Dayforce", "taleo.net": "Taleo", "oraclecloud.com": "Oracle Recruiting",
    "successfactors.com": "SuccessFactors", "sapsf.com": "SuccessFactors", "ats.rippling.com": "Rippling",
    "applytojob.com": "JazzHR", "breezy.hr": "Breezy", "recruitee.com": "Recruitee", "ultipro.com": "UKG",
    "paycomonline.net": "Paycom", "governmentjobs.com": "NEOGOV",
}

SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # a job id, not a company
RESERVED = {"embed", "jobs", "careers", "career", "www", "api", "app", "apply", "j", "o", "p", "static", "assets", "login",
            "oneclick-ui", "company", "documentation", "help", "support", "careersection", "recruiting"}


@dataclass(frozen=True)
class Company:
    board: str
    key: str            # what the scraper's list stores: slug, "tenant/site", or "cid/ccId"
    name: str           # display name (csv lists)
    url: str            # csv lists only

    @property
    def norm(self) -> str:
        return self.key.lower()


def _title(slug: str) -> str:
    return re.sub(r"[-_.]+", " ", slug).strip().title()


def _segments(parsed) -> list[str]:
    return [unquote(part) for part in parsed.path.split("/") if part]


def parse_ats_url(url: str, display_name: str = "") -> Company | None:
    """The company an ATS apply link belongs to, or None (not an ATS we scrape, or no slug in it)."""
    if not url or not url.startswith("http"):
        return None
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    parts = _segments(parsed)
    query = parse_qs(parsed.query)
    first = parts[0].lower() if parts else ""

    def txt(board: str, slug: str, *, keep_case: bool = False) -> Company | None:
        slug = slug if keep_case else slug.lower()
        if not SLUG_OK.match(slug.lower()) or slug.lower() in RESERVED or UUID.match(slug.lower()):
            return None
        return Company(board, slug, "", "")

    if host.endswith("greenhouse.io"):
        slug = (query.get("for") or [""])[0] if first == "embed" else first
        return txt("Greenhouse", slug)
    if host.endswith("lever.co"):
        return txt("Lever", first)
    if host.endswith("ashbyhq.com") and host.startswith(("jobs.", "www.")) or host == "ashbyhq.com":
        return txt("Ashby", first)
    if host == "apply.workable.com":
        return txt("Workable", first)  # "/j/<shortcode>" links carry no slug
    if host.endswith(".workable.com") and not host.startswith(("apply.", "www.")):
        return txt("Workable", host.split(".")[0])
    if host.endswith("smartrecruiters.com"):
        slug = parts[2] if first == "oneclick-ui" and len(parts) > 2 else parts[0] if parts else ""
        return txt("SmartRecruiters", slug, keep_case=True)
    if host.endswith(".bamboohr.com"):
        return txt("BambooHR", host.split(".")[0])
    if host == "jobs.jobvite.com" or host == "www.jobvite.com":
        slug = parts[1] if first == "careers" and len(parts) > 1 else first
        return txt("Jobvite", slug)
    if host.endswith(".teamtailor.com"):
        tenant = host.split(".")[0]
        company = txt("Teamtailor", tenant)
        return Company("Teamtailor", company.key, display_name or _title(tenant), f"https://{tenant}.teamtailor.com") if company else None
    if host.endswith(".icims.com"):
        label = host.split(".")[0]
        if not SLUG_OK.match(label) or label in RESERVED:
            return None
        # The scraper adds "careers-" itself, and takes "<prefix>careers-<name>" or a full URL as given.
        if label.startswith("careers-"):
            return Company("iCIMS", label[len("careers-"):], "", "")
        if re.match(r"^[a-z]+careers-", label):
            return Company("iCIMS", label, "", "")
        return Company("iCIMS", f"https://{host}", "", "")
    myworkday = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if myworkday:
        tenant, wd = myworkday.groups()
        site_parts = parts[1:] if parts and re.match(r"^[a-z]{2}-[A-Za-z]{2}$", parts[0]) else parts
        site = site_parts[0] if site_parts else ""
        if not site or site.lower() in ("job", "jobs", "login"):
            return None
        return Company("Workday", f"{tenant}/{site}".lower(), display_name or _title(tenant), f"https://{tenant}.{wd}.myworkdayjobs.com/{site}")
    if host.endswith("adp.com") and "cid" in query:
        cid, cc = query["cid"][0], (query.get("ccId") or ["19000101_000001"])[0]
        if re.fullmatch(r"[0-9a-f-]{36}", cid.lower()):
            return Company("Adp", f"{cid}/{cc}".lower(), display_name or cid[:8],
                           f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&ccId={cc}&lang=en_US")
    return None


def unsupported_platform(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower() if url else ""
    for domain, name in UNSUPPORTED.items():
        if host == domain or host.endswith("." + domain) or domain in host:
            return name
    return None


# ---------------------------------------------------------------------------
# What we already have
# ---------------------------------------------------------------------------

def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def load_known(root: Path = ROOT) -> dict[str, set[str]]:
    """Everything already listed per board, lower-cased: active, disabled ("#slug") and known-dead (404 files)."""
    known: dict[str, set[str]] = {board: set() for board in LISTS}
    for board, (rel, fmt) in LISTS.items():
        path = root / rel
        if fmt == "txt":
            known[board] |= {line.strip().lstrip("#").strip().lower() for line in _read_lines(path) if line.strip()}
            for dead in path.parent.glob("*404*.txt"):
                known[board] |= {line.split(",")[0].strip().lstrip("#").strip().lower() for line in _read_lines(dead) if line.strip()}
            known[board] |= {k.replace("https://", "").split(".")[0].replace("careers-", "") for k in known[board] if k.startswith("https://")}
        elif path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    known[board].add((row.get("slug") or "").strip().lower())
            if board == "Workday":  # a known tenant covers its other sites too
                known[board] |= {slug.split("/")[0] for slug in known[board] if "/" in slug}
    # txt lists keep icims slugs both with and without the "careers-" prefix
    known["iCIMS"] |= {k.replace("careers-", "", 1) for k in known["iCIMS"]}
    return known


def is_known(company: Company, known: dict[str, set[str]]) -> bool:
    keys = known.get(company.board, set())
    if company.norm in keys:
        return True
    if company.board == "Workday" and company.norm.split("/")[0] in keys:
        return True
    if company.board == "iCIMS" and company.norm.replace("https://", "").split(".")[0].replace("careers-", "") in keys:
        return True
    return False


# ---------------------------------------------------------------------------
# Reading jobs
# ---------------------------------------------------------------------------

def read_jobs_file(path: Path):
    """(apply_url, job_url, company) for every job in a CSV or JSON export."""
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("jobs", [])
    else:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    for row in rows:
        yield (row.get("apply_url") or "", row.get("job_url") or row.get("url") or "",
               row.get("company") or row.get("company_name") or "")


def read_jobs_neon():
    import psycopg
    from psycopg.conninfo import make_conninfo
    url = make_conninfo(os.environ["NEON_DATABASE_URL"], options="-c default_transaction_read_only=on")
    with psycopg.connect(url) as conn:
        cur = conn.execute("SELECT coalesce(apply_url, ''), coalesce(job_url, ''), coalesce(company, '') FROM public.jobs "
                           "WHERE source_board IN ('LinkedIn', 'Indeed')")
        yield from cur


async def resolve_short_links(urls: list[str], concurrency: int = 8, timeout: float = 12.0) -> dict[str, str]:
    """grnh.se short links redirect once to the company's Greenhouse board (or to its own site)."""
    import httpx
    results: dict[str, str] = {}
    gate = asyncio.Semaphore(concurrency)

    async def one(client, url):
        async with gate:
            try:
                response = await client.head(url)
                location = response.headers.get("location", "")
                if location.startswith("http"):
                    results[url] = location
            except Exception:  # noqa: BLE001 - an unresolvable link is just skipped
                pass
            await asyncio.sleep(0.05)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        await asyncio.gather(*(one(client, url) for url in urls))
    return results


# ---------------------------------------------------------------------------
# Discovery and writing
# ---------------------------------------------------------------------------

def discover(jobs, known: dict[str, set[str]], resolve=None) -> dict:
    """New companies per board, how many jobs pointed at each, and platforms we cannot scrape."""
    found: dict[tuple[str, str], list] = {}
    unsupported = collections.Counter()
    short: dict[str, str] = {}
    total = direct = 0
    for apply_url, job_url, company in jobs:
        total += 1
        for url in (apply_url, job_url):
            if "grnh.se" in url:
                short[url] = company
            company_row = parse_ats_url(url, company.strip())
            if company_row:
                direct += 1
                entry = found.setdefault((company_row.board, company_row.norm), [company_row, 0])
                entry[1] += 1
                break
            name = unsupported_platform(url)
            if name:
                unsupported[name] += 1
    resolved = resolve(list(short)) if resolve and short else {}
    for url, location in resolved.items():
        company_row = parse_ats_url(location, short[url].strip())
        if company_row:
            entry = found.setdefault((company_row.board, company_row.norm), [company_row, 0])
            entry[1] += 1
    new: dict[str, list[tuple[Company, int]]] = collections.defaultdict(list)
    already = collections.Counter()
    for (board, _), (company_row, count) in found.items():
        if is_known(company_row, known):
            already[board] += 1
        else:
            new[board].append((company_row, count))
    for board in new:
        new[board].sort(key=lambda item: (-item[1], item[0].norm))
    return {"jobs": total, "direct": direct, "short_links": len(short), "short_resolved": len(resolved),
            "new": dict(new), "already": dict(already), "unsupported": dict(unsupported.most_common())}


def append_companies(new: dict[str, list[tuple[Company, int]]], root: Path = ROOT) -> dict[str, int]:
    """Add the new companies to each board's list file; returns how many were written per board."""
    written: dict[str, int] = {}
    for board, items in new.items():
        rel, fmt = LISTS[board]
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "txt":
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            path.write_text(existing + prefix + "".join(f"{company.key}\n" for company, _ in items), encoding="utf-8")
        else:
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                if exists and not path.read_bytes().endswith(b"\n"):
                    handle.write("\n")
                writer = csv.writer(handle)
                if not exists:
                    writer.writerow(["name", "slug", "url"])
                for company, _ in items:
                    writer.writerow([company.name or _title(company.key.split("/")[0]), company.key, company.url])
        written[board] = len(items)
    return written


def report(result: dict, written: dict[str, int] | None) -> str:
    lines = [f"Read {result['jobs']:,} jobs; {result['direct']:,} link straight to a supported ATS, "
             f"{result['short_links']:,} are grnh.se short links ({result['short_resolved']:,} resolved).", ""]
    lines.append("| Board | New companies | Already listed |")
    lines.append("|---|---:|---:|")
    boards = sorted(set(result["new"]) | set(result["already"]))
    for board in boards:
        lines.append(f"| {board} | {len(result['new'].get(board, []))} | {result['already'].get(board, 0)} |")
    lines.append(f"| **Total** | **{sum(len(v) for v in result['new'].values())}** | **{sum(result['already'].values())}** |")
    if result["unsupported"]:
        lines += ["", "Platforms in apply links that we have no scraper for: " +
                  ", ".join(f"{name} ({count})" for name, count in list(result["unsupported"].items())[:8])]
    if written is not None:
        lines += ["", "Added to the lists: " + (", ".join(f"{b} {n}" for b, n in written.items()) or "nothing")]
    else:
        lines += ["", "Report only. Run with --apply to add them to the lists."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", nargs="*", type=Path, default=[], help="CSV/JSON job exports with apply_url")
    parser.add_argument("--neon", action="store_true", help="also read LinkedIn/Indeed jobs from NEON_DATABASE_URL (read-only)")
    parser.add_argument("--apply", action="store_true", help="add the new companies to the list files")
    parser.add_argument("--no-resolve", action="store_true", help="do not follow grnh.se short links")
    args = parser.parse_args(argv)
    if not args.jobs and not args.neon:
        parser.error("give --jobs FILE... and/or --neon")

    def all_jobs():
        for path in args.jobs:
            yield from read_jobs_file(path)
        if args.neon:
            yield from read_jobs_neon()

    resolve = None if args.no_resolve else (lambda urls: asyncio.run(resolve_short_links(urls)))
    result = discover(all_jobs(), load_known(), resolve)
    written = append_companies(result["new"]) if args.apply else None
    text = report(result, written)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### New ATS companies from apply links\n" + text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
