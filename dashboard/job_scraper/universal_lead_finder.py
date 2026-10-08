"""
Universal Buyer-Intent Lead Finder
====================================
Usage:
  python universal_lead_finder.py "Microsoft 365, SharePoint, Azure"
  python universal_lead_finder.py "Shopify development, eCommerce"
  python universal_lead_finder.py "Mobile app development, React Native"
  python universal_lead_finder.py "SEO, digital marketing agency"

Outputs leads from LinkedIn, Twitter, Reddit, Upwork, Freelancer, RFP boards
— all from the past 30 days — with buyer intent (companies SEEKING a partner,
NOT freelancers/agencies advertising their own services).
"""

import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, UTC

CUTOFF_DAYS  = 30
NUM_RESULTS  = 12
MAX_WORKERS  = 6
THIRTY_AGO   = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

THIS_MONTH   = datetime.now().strftime("%B %Y")   # e.g. "September 2026"
LAST_MONTH   = (datetime.now(UTC) - timedelta(days=30)).strftime("%B %Y")  # "August 2026"
YEAR         = datetime.now().strftime("%Y")


def build_queries(services: str) -> list[tuple[str, str, list[str]]]:
    """
    Auto-generate 30+ targeted buyer-intent queries from a services string.
    Returns list of (label, query, include_domains).
    """
    # Parse services into a list
    svc_list = [s.strip() for s in services.split(",")]
    svc_str  = " OR ".join(f'"{s}"' for s in svc_list)       # "svc1" OR "svc2"
    svc_any  = " OR ".join(svc_list)                           # svc1 OR svc2
    svc_first = svc_list[0]                                    # primary service

    LI  = ["linkedin.com"]
    TW  = ["twitter.com", "x.com"]
    RD  = ["reddit.com"]
    UP  = ["upwork.com"]
    FL  = ["freelancer.com"]
    RFP = []

    queries = [
        # ── LinkedIn: exact buyer-intent phrases ─────────────────────────
        (f"LI | Looking for implementation partner {THIS_MONTH}",
         f'site:linkedin.com/posts ("looking for" OR "seeking") '
         f'("implementation partner" OR "trusted partner" OR "agency" OR "vendor") '
         f'({svc_str}) "{THIS_MONTH}"', LI),

        (f"LI | Looking for partner {LAST_MONTH}",
         f'site:linkedin.com/posts ("looking for" OR "seeking") '
         f'("implementation partner" OR "trusted partner" OR "agency") '
         f'({svc_str}) "{LAST_MONTH}"', LI),

        ("LI | DMs open / referrals welcome",
         f'site:linkedin.com/posts ("DMs open" OR "referrals welcome" OR "comment below" '
         f'OR "drop a comment") ({svc_str}) "{YEAR}"', LI),

        ("LI | Anyone recommend a partner",
         f'site:linkedin.com/posts ("recommend" OR "recommendation" OR "anyone know" '
         f'OR "can anyone suggest") ({svc_str}) partner OR agency "{YEAR}"', LI),

        ("LI | Need help building / migrating",
         f'site:linkedin.com/posts ("need help" OR "need to build" OR "want to build" '
         f'OR "need to migrate") ({svc_str}) "{YEAR}"', LI),

        ("LI | Hiring agency / dev shop",
         f'site:linkedin.com/posts ("hiring" OR "looking to hire") '
         f'("agency" OR "development shop" OR "consulting firm" OR "partner company") '
         f'({svc_str}) "{YEAR}"', LI),

        # ── LinkedIn neural (semantic) queries ───────────────────────────
        (f"LI Neural | Company seeking partner {THIS_MONTH}",
         f"A company posted in {THIS_MONTH} on LinkedIn that they are looking for a "
         f"{svc_first} implementation partner or agency to help with an upcoming project:", LI),

        (f"LI Neural | Buyer seeking help {LAST_MONTH}",
         f"In {LAST_MONTH} a business posted on LinkedIn that they need an agency or "
         f"consulting firm to help them with {svc_first}:", LI),

        (f"LI Neural | Company needs to build {THIS_MONTH}",
         f"Here is a {THIS_MONTH} LinkedIn post from a company or startup looking to "
         f"hire a developer or agency to build or implement {svc_first}:", LI),

        # ── Twitter / X ──────────────────────────────────────────────────
        (f"TW | Recommendation needed {THIS_MONTH}",
         f'site:x.com OR site:twitter.com ("recommend" OR "looking for" OR "need a good") '
         f'({svc_str}) ("agency" OR "partner" OR "vendor" OR "dev shop") '
         f'"{THIS_MONTH}" OR "{LAST_MONTH}"', TW),

        ("TW | Need help / hiring agency",
         f'site:x.com OR site:twitter.com '
         f'("need help" OR "looking for someone" OR "hiring") '
         f'({svc_str}) "{YEAR}"', TW),

        # ── Reddit ───────────────────────────────────────────────────────
        ("RD | Consultant / partner recommendation",
         f'site:reddit.com '
         f'("looking for" OR "need a consultant" OR "recommend a partner" '
         f'OR "anyone have experience" OR "vendor recommendation") '
         f'({svc_str}) "{YEAR}"', RD),

        ("RD | Best agency / MSP",
         f'site:reddit.com '
         f'("best agency" OR "good MSP" OR "reliable vendor" OR "recommended company") '
         f'({svc_str}) "{YEAR}"', RD),

        ("RD | Help needed / hiring",
         f'site:reddit.com '
         f'("need help" OR "need someone" OR "looking to hire") '
         f'({svc_str}) "{YEAR}"', RD),

        # ── Upwork ───────────────────────────────────────────────────────
        (f"UP | Project posted {THIS_MONTH}",
         f'site:upwork.com/jobs ({svc_str}) "{YEAR}"', UP),

        ("UP | Project seeking agency",
         f'site:upwork.com ("We are looking for" OR "seeking" OR "need to hire") '
         f'({svc_str}) "{YEAR}"', UP),

        # ── Freelancer.com ────────────────────────────────────────────────
        (f"FL | Project posted {THIS_MONTH}",
         f'site:freelancer.com/projects ({svc_str}) "{YEAR}"', FL),

        (f"FL | Project {LAST_MONTH}",
         f'site:freelancer.com/projects ({svc_str}) "{LAST_MONTH}"', FL),

        # ── RFP Boards ───────────────────────────────────────────────────
        (f"RFP | {svc_first} RFP {THIS_MONTH}",
         f'("request for proposal" OR "RFP" OR "tender" OR "procurement") '
         f'({svc_str}) "{THIS_MONTH}" OR "{LAST_MONTH}"', RFP),

        (f"RFP | {svc_first} RFP {YEAR}",
         f'("request for proposal" OR "RFP" OR "invitation to tender") '
         f'({svc_str}) "{YEAR}"', RFP),

        # ── General buyer-intent (all domains) ───────────────────────────
        (f"WEB | Company seeking {svc_first} partner {THIS_MONTH}",
         f'("looking for" OR "seeking") ("implementation partner" OR "agency" OR "vendor") '
         f'({svc_str}) "{THIS_MONTH}"', []),

        (f"WEB | Business needs {svc_first} help {YEAR}",
         f'("need help with" OR "looking for help" OR "seeking expertise") '
         f'({svc_str}) company OR business OR organization "{YEAR}"', []),
    ]

    return queries


def fetch(label: str, query: str, domains: list[str]) -> list[dict]:
    """Run one Exa query; return only results ≤ CUTOFF_DAYS old."""
    executable = shutil.which("mcporter")
    if not executable:
        return []

    args: dict = {"query": query, "numResults": NUM_RESULTS, "startPublishedDate": THIRTY_AGO}
    if domains:
        args["includeDomains"] = domains

    print(f"  ↳ {label}", flush=True)
    completed = subprocess.run(
        [executable, "call", "exa.web_search_exa",
         "--output", "json", "--args", json.dumps(args),
         "--timeout", "35000", "--no-oauth"],
        capture_output=True, check=False, text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []

    kept = []
    for block in payload.get("content", []):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")

        # Date filter
        dm = re.search(r"Published:\s*(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?)", text)
        if not dm:
            continue
        raw = dm.group(1).split(".")[0].rstrip("Z")
        if "T" not in raw:
            raw += "T00:00:00"
        try:
            from datetime import timezone
            pub = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - pub).days
        except ValueError:
            continue
        if age > CUTOFF_DAYS:
            print(f"    ✗ {age}d — skipped", flush=True)
            continue
        print(f"    ✓ {age}d — KEPT", flush=True)

        title_m  = re.search(r"Title:\s*(.+)", text)
        url_m    = re.search(r"URL:\s*(https?://\S+)", text)
        author_m = re.search(r"Author:\s*(.+)", text)

        url = url_m.group(1).strip() if url_m else "—"

        # Platform
        if "linkedin" in url:
            platform = "LinkedIn"
        elif "twitter" in url or "x.com" in url:
            platform = "Twitter/X"
        elif "reddit" in url:
            platform = "Reddit"
        elif "upwork" in url:
            platform = "Upwork"
        elif "freelancer" in url:
            platform = "Freelancer"
        elif any(k in text.lower() for k in ["rfp", "request for proposal", "tender"]):
            platform = "RFP Board"
        else:
            platform = "Web"

        kept.append({
            "title":        (title_m.group(1).strip()  if title_m  else "—")[:120],
            "url":          url,
            "author":       (author_m.group(1).strip() if author_m else "—"),
            "published_at": raw,
            "age_days":     age,
            "platform":     platform,
            "query_label":  label,
            "snippet":      text[:500],
        })
    return kept


def main():
    if len(sys.argv) < 2:
        print("Usage: python universal_lead_finder.py \"Service1, Service2, Service3\"")
        print('Example: python universal_lead_finder.py "Microsoft 365, SharePoint, Azure"')
        sys.exit(1)

    services = sys.argv[1]
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n🚀 Universal Lead Finder  [{now_str}]")
    print(f"   Services : {services}")
    print(f"   Cutoff   : {CUTOFF_DAYS} days ({LAST_MONTH} → {THIS_MONTH})")
    print(f"   Platforms: LinkedIn, Twitter, Reddit, Upwork, Freelancer, RFP Boards\n")

    queries = build_queries(services)
    print(f"   Generated {len(queries)} queries\n")

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch, label, query, domains): label
            for label, query, domains in queries
        }
        for future in as_completed(futures):
            try:
                for row in future.result():
                    if row["url"] not in seen_urls:
                        seen_urls.add(row["url"])
                        all_results.append(row)
            except Exception as exc:
                print(f"  [ERROR]: {exc}", flush=True)

    all_results.sort(key=lambda x: x["age_days"])
    total = len(all_results)
    print(f"\n✅ {total} unique buyer-intent leads found within the past {CUTOFF_DAYS} days.\n")

    # ── Output paths based on services ───────────────────────────────────
    slug = re.sub(r'[^a-z0-9]+', '_', services.lower())[:40]
    base = f"/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Leads_{slug}"

    # Markdown
    md = [
        f"# Buyer-Intent Leads: {services}",
        f"_Generated: {now_str} | Past {CUTOFF_DAYS} days_\n",
        f"**{total} unique leads found**\n",
        "| # | Platform | Age | Title | URL |",
        "|---|----------|-----|-------|-----|",
    ]
    for i, r in enumerate(all_results, 1):
        md.append(
            f"| {i} | {r['platform']} | {r['age_days']}d "
            f"| {r['title'][:55]}... | [{r['url'][:45]}...]({r['url']}) |"
        )
    md.append("\n---\n")
    for i, r in enumerate(all_results, 1):
        md += [
            f"## Lead {i} — {r['title']}",
            f"- **Platform:** {r['platform']}  |  **Age:** {r['age_days']} days",
            f"- **URL:** {r['url']}",
            f"- **Author:** {r['author']}",
            f"- **Published:** {r['published_at']}",
            f"\n**Snippet:**\n```\n{r['snippet']}\n```\n",
            "---\n",
        ]

    with open(f"{base}.md", "w") as f:
        f.write("\n".join(md))
    with open(f"{base}.json", "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"💾 MD  : {base}.md")
    print(f"💾 JSON: {base}.json")
    print(f"\n📋 Quick summary:")
    for r in all_results:
        print(f"   [{r['platform']:10}] {r['age_days']:2}d — {r['title'][:60]}")


if __name__ == "__main__":
    main()
