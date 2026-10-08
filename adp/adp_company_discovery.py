import argparse
import csv
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HEADERS = {"User-Agent": "adp-company-discovery/1.0 (research script)"}

ADP_PATH_PREFIX = "workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
CID_RE = re.compile(r"[?&]cid=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", re.I)
CCID_RE = re.compile(r"[?&]ccId=([^&]+)", re.I)

RESOLVE_URL = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/content-links/career-center"

# Real pairs pulled from public embed-code examples -- use these to sanity
# check the resolve step before trusting it at scale.
TEST_CASES = [
    {"cid": "cbfbdc9a-a7f6-4833-9211-9b7695508c8f", "ccId": "9200676824562_3"},
    {"cid": "687a01ed-c0c1-4af4-ba7e-69bf0c5dc833", "ccId": ""},
]


def _get_with_retry(session, url, params, tries=6, base_delay=2.0):
    resp = None
    for attempt in range(tries):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code == 200:
            return resp
        if resp is not None and resp.status_code in (429, 503):
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else base_delay * (2 ** attempt)
        else:
            delay = base_delay * (2 ** attempt)
        time.sleep(delay + random.uniform(0, 1.0))
    return resp


def discover_commoncrawl(crawl_id, session):
    base = f"https://index.commoncrawl.org/{crawl_id}-index"
    common = {"url": ADP_PATH_PREFIX, "matchType": "prefix", "output": "json", "fl": "url", "filter": "status:200"}
    meta = _get_with_retry(session, base, {**common, "showNumPages": "true"})
    try:
        num_pages = meta.json().get("pages", 1) if meta is not None else 1
    except Exception:
        num_pages = 1

    urls = set()
    for page in range(num_pages):
        resp = _get_with_retry(session, base, {**common, "page": page})
        if resp is None or resp.status_code != 200:
            continue
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            try:
                urls.add(json.loads(line)["url"])
            except (json.JSONDecodeError, KeyError):
                continue
        time.sleep(0.3)
    return urls


def discover_wayback(session, limit=5000):
    url = "https://web.archive.org/cdx/search/cdx"
    urls = set()
    resume_key = None
    while True:
        params = {
            "url": ADP_PATH_PREFIX,
            "matchType": "prefix",
            "output": "json",
            "fl": "original,statuscode",
            "collapse": "urlkey",
            "limit": limit,
            "showResumeKey": "true",
        }
        if resume_key:
            params["resumeKey"] = resume_key
        resp = _get_with_retry(session, url, params)
        if resp is None or resp.status_code != 200 or not resp.text.strip():
            break
        try:
            parsed = json.loads(resp.text)
        except json.JSONDecodeError:
            break
        if not parsed:
            break
        header, *data = parsed
        resume_key = None
        if len(data) >= 2 and data[-2] == [] and len(data[-1]) == 1:
            resume_key = data[-1][0]
            data = data[:-2]
        for row in data:
            if len(row) >= 2 and row[1] == "200":
                urls.add(row[0])
        if not resume_key:
            break
        time.sleep(0.3)
    return urls


def extract_ids(urls):
    pairs = {}  # cid -> ccId (first one seen)
    for u in urls:
        m_cid = CID_RE.search(u)
        if not m_cid:
            continue
        cid = m_cid.group(1).lower()
        m_ccid = CCID_RE.search(u)
        ccid = m_ccid.group(1) if m_ccid else ""
        pairs.setdefault(cid, ccid)
    return pairs


def resolve_company(cid, ccid, session):
    params = {"cid": cid, "ccId": ccid, "timeStamp": "0", "locale": "en_US", "lang": "en_US"}
    resp = _get_with_retry(session, RESOLVE_URL, params, tries=3, base_delay=1.5)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def guess_company_name(data):
    """Field name for the company display name is UNVERIFIED -- tries
    the most likely candidates. Check --test-resolve output and adjust
    this list if the real key differs."""
    if not isinstance(data, dict):
        return None
    candidates = ("companyName", "orgName", "clientName", "name", "title", "brandName")
    for key in candidates:
        if data.get(key):
            return data[key]
    for v in data.values():
        if isinstance(v, dict):
            for key in candidates:
                if v.get(key):
                    return v[key]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-resolve", action="store_true",
                     help="test the resolve endpoint against two known real pairs, print raw JSON, exit")
    ap.add_argument("--source", choices=["commoncrawl", "wayback"], default="wayback")
    ap.add_argument("--crawl", default="CC-MAIN-2024-18", help="only used with --source commoncrawl")
    ap.add_argument("--no-resolve", action="store_true", help="skip name resolution, just dump cid/ccId pairs")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="adp_companies.csv")
    args = ap.parse_args()

    session = requests.Session()

    if args.test_resolve:
        for case in TEST_CASES:
            print(f"Testing cid={case['cid']} ccId={case['ccId']!r}")
            data = resolve_company(case["cid"], case["ccId"], session)
            print(json.dumps(data, indent=2)[:2000] if data else "No response / non-JSON")
            print("---")
        return

    print(f"Discovering cid/ccId pairs via {args.source}...")
    urls = discover_commoncrawl(args.crawl, session) if args.source == "commoncrawl" else discover_wayback(session)

    pairs = extract_ids(urls)
    print(f"Found {len(pairs)} distinct company IDs")

    if args.no_resolve:
        rows = [[cid, ccid, None] for cid, ccid in pairs.items()]
    else:
        print("Resolving company names...")
        rows = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(resolve_company, cid, ccid, session): (cid, ccid) for cid, ccid in pairs.items()}
            done = 0
            for fut in as_completed(futures):
                cid, ccid = futures[fut]
                name = guess_company_name(fut.result())
                rows.append([cid, ccid, name])
                done += 1
                if done % 200 == 0:
                    print(f"  resolved {done}/{len(pairs)}")

    rows.sort(key=lambda r: (r[2] or "", r[0]))
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cid", "ccId", "company_name"])
        w.writerows(rows)

    resolved_count = sum(1 for r in rows if r[2])
    print(f"Done. {len(rows)} rows written to {args.out} ({resolved_count} names resolved)")


if __name__ == "__main__":
    main()
