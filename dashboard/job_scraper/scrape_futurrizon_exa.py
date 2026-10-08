"""
Futurrizon Lead Scraper v2 — Multi-Platform, 35+ Queries, Full Field Extraction
================================================================================
Platforms: LinkedIn, Twitter/X, Reddit, Upwork, Freelancer, RFP Boards, Google Groups

Extracts ALL fields needed by the platform CSV importer:
  company, requirement, source_url, contact_name, phone, location, timezone, consent_basis

Also extracts display fields for the dashboard:
  title, published_at, age_days, lead_type, platform, email, snippet

Strict 30-day published-date filter on every result.
"""

import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone, UTC

# ─── Config ──────────────────────────────────────────────────────────────────
CUTOFF_DAYS   = 30
NUM_RESULTS   = 15   # per query; raise to 25 if Exa quota allows
MAX_WORKERS   = 6    # parallel queries
OUTPUT_MD     = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Exa_Leads.md"
OUTPUT_JSON   = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Exa_Leads.json"
OUTPUT_CSV    = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Exa_Leads.csv"
THIRTY_AGO    = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

# ─── Platform-specific domain lists ──────────────────────────────────────────
LI   = ["linkedin.com"]
TW   = ["twitter.com", "x.com"]
RD   = ["reddit.com"]
UP   = ["upwork.com"]
FL   = ["freelancer.com"]
RFP  = []   # no domain restriction — cast wide net

# ─── 35 targeted queries ─────────────────────────────────────────────────────
QUERIES = [
    # ── LinkedIn exact-phrase buyer posts ────────────────────────────────────
    ("LI | Seeking implementation partner Sep 2026",
     'site:linkedin.com/posts ("looking for" OR "seeking") '
     '("implementation partner" OR "trusted partner" OR "agency") '
     '("SharePoint" OR "Microsoft 365" OR "Azure" OR "Power Apps" OR "Dynamics 365") '
     '"September 2026"', LI),

    ("LI | Seeking implementation partner Aug 2026",
     'site:linkedin.com/posts ("looking for" OR "seeking") '
     '("implementation partner" OR "trusted partner" OR "agency") '
     '("SharePoint" OR "Microsoft 365" OR "Azure" OR "Power Apps" OR "Dynamics 365") '
     '"August 2026"', LI),

    ("LI | DMs open referrals welcome Sep/Aug 2026",
     'site:linkedin.com/posts ("DMs open" OR "referrals welcome" OR "comment below") '
     '("SharePoint" OR "Microsoft 365" OR "Azure" OR "Power Apps" OR "Dynamics 365") '
     '"2026"', LI),

    ("LI | Recommendation needed MS 365 partner",
     'site:linkedin.com/posts ("recommend" OR "recommendation" OR "anyone know") '
     '("Microsoft 365 partner" OR "SharePoint partner" OR "Azure partner") '
     '"2026"', LI),

    ("LI | Azure cloud migration hiring 2026",
     'site:linkedin.com/posts ("Azure migration" OR "cloud migration") '
     '("hiring" OR "looking for" OR "seeking vendor" OR "RFP") "2026"', LI),

    ("LI | Power Apps agency needed 2026",
     'site:linkedin.com/posts ("Power Apps" OR "Power Platform") '
     '("looking for an agency" OR "seeking a partner" OR "need to build" '
     '"OR "need a developer") "2026"', LI),

    ("LI | Dynamics 365 implementation partner needed",
     'site:linkedin.com/posts ("Dynamics 365" OR "D365") '
     '("looking for" OR "seeking" OR "need implementation") '
     '("implementation partner" OR "agency" OR "consulting firm") "2026"', LI),

    ("LI | AI chatbot Copilot Studio partner needed",
     'site:linkedin.com/posts ("AI chatbot" OR "Copilot Studio" OR "GPT chatbot" '
     'OR "enterprise AI" OR "Copilot for M365") '
     '("looking for" OR "want to build" OR "need help building") "2026"', LI),

    ("LI | M365 tenant migration partner Sep 2026",
     'site:linkedin.com/posts ("tenant migration" OR "tenant-to-tenant" OR "M365 migration") '
     '("looking for" OR "need help" OR "seeking") "September 2026" OR "August 2026"', LI),

    ("LI | SharePoint intranet partner 2026",
     'site:linkedin.com/posts ("SharePoint intranet" OR "intranet redesign" '
     'OR "SharePoint Online") ("looking for" OR "need help" OR "partner") '
     '"2026"', LI),

    # ── LinkedIn neural queries ───────────────────────────────────────────────
    ("LI Neural | Company seeking SharePoint partner Sep 2026",
     "A company posted in September 2026 on LinkedIn that they are looking for a SharePoint "
     "or Microsoft 365 implementation partner to help with an upcoming project:", LI),

    ("LI Neural | Business seeking Azure migration firm Aug 2026",
     "In August 2026 a company or business posted on LinkedIn that they need an agency to "
     "migrate their infrastructure to Azure cloud:", LI),

    ("LI Neural | Power Platform dev needed Sep 2026",
     "Here is a September 2026 LinkedIn post from a company looking to hire a Power Apps "
     "or Power Platform developer or agency for an enterprise project:", LI),

    ("LI Neural | Dynamics 365 CRM implementation Sep 2026",
     "A business posted in September 2026 on LinkedIn looking for a Dynamics 365 CRM "
     "implementation partner or agency:", LI),

    ("LI Neural | AI automation chatbot needed Sep 2026",
     "In September 2026 a company posted on LinkedIn that they want to build an enterprise "
     "AI chatbot or automate their business processes using Microsoft Copilot Studio or Azure AI:", LI),

    # ── Twitter / X ───────────────────────────────────────────────────────────
    ("TW | MS 365 partner recommendation Sep 2026",
     'site:x.com OR site:twitter.com ("recommend" OR "looking for" OR "need a good") '
     '("Microsoft 365" OR "SharePoint" OR "Azure" OR "Power Apps") '
     '("agency" OR "partner" OR "vendor") "September 2026"', TW),

    ("TW | Need help Azure migration Sep 2026",
     'site:x.com OR site:twitter.com '
     '("need help" OR "looking for someone" OR "hiring") '
     '("Azure migration" OR "Microsoft 365" OR "Dynamics 365") '
     '"September 2026" OR "August 2026"', TW),

    ("TW | Power Apps developer needed 2026",
     'site:x.com OR site:twitter.com ("Power Apps" OR "PowerApps" OR "Power Platform") '
     '("need a developer" OR "looking for" OR "anyone know") "2026"', TW),

    # ── Reddit ────────────────────────────────────────────────────────────────
    ("RD | M365 consultant needed Sep 2026",
     'site:reddit.com ("r/sysadmin" OR "r/office365" OR "r/microsoft365") '
     '("looking for" OR "need a consultant" OR "recommend a partner" OR "anyone have experience") '
     '("Microsoft 365" OR "SharePoint" OR "Azure" OR "Power Apps") "2026"', RD),

    ("RD | Azure migration partner recommendation",
     'site:reddit.com ("r/azure" OR "r/sysadmin" OR "r/msp") '
     '("hiring" OR "vendor recommendation" OR "looking for an agency" OR "best MSP") '
     '"Azure" "2026"', RD),

    ("RD | Power Platform implementation partner",
     'site:reddit.com ("r/PowerApps" OR "r/PowerPlatform" OR "r/MicrosoftPartners") '
     '("looking for" OR "need" OR "recommendations") '
     '("Power Apps" OR "Power Platform" OR "implementation partner") "2026"', RD),

    ("RD | Office 365 migration help Sep 2026",
     'site:reddit.com ("r/Office365" OR "r/sysadmin") '
     '("looking for" OR "recommend" OR "need a company") '
     '("Office 365" OR "Microsoft 365") "migration" "2026"', RD),

    ("RD | SharePoint consultant help",
     'site:reddit.com ("SharePoint" OR "SharePoint Online") '
     '("looking for a consultant" OR "need help" OR "anyone recommend") "2026"', RD),

    # ── Upwork ────────────────────────────────────────────────────────────────
    ("UP | Power Apps project Sep 2026",
     'site:upwork.com/jobs ("Power Apps" OR "Power Platform" OR "Power Automate") '
     '"2026"', UP),

    ("UP | Azure cloud migration project Sep 2026",
     'site:upwork.com/jobs ("Azure migration" OR "Microsoft Azure" OR "Azure cloud") '
     '"2026"', UP),

    ("UP | SharePoint Online implementation project",
     'site:upwork.com/jobs ("SharePoint" OR "SharePoint Online") '
     '("implementation" OR "migration" OR "setup") "2026"', UP),

    ("UP | Dynamics 365 project 2026",
     'site:upwork.com/jobs ("Dynamics 365" OR "D365" OR "Microsoft CRM") '
     '"2026"', UP),

    ("UP | Microsoft 365 project 2026",
     'site:upwork.com/jobs ("Microsoft 365" OR "M365" OR "Office 365") '
     '"2026"', UP),

    # ── Freelancer.com ────────────────────────────────────────────────────────
    ("FL | Microsoft 365 / SharePoint project Sep 2026",
     'site:freelancer.com/projects '
     '("Microsoft 365" OR "SharePoint" OR "Power Apps" OR "Dynamics 365" OR "Azure") '
     '"2026"', FL),

    ("FL | AI chatbot / Copilot project 2026",
     'site:freelancer.com/projects '
     '("AI chatbot" OR "Copilot" OR "Microsoft AI" OR "GPT chatbot") '
     '"2026"', FL),

    # ── RFP Boards ────────────────────────────────────────────────────────────
    ("RFP | Microsoft 365 SharePoint RFP Sep/Oct 2026",
     '("request for proposal" OR "RFP" OR "tender" OR "procurement") '
     '("Microsoft 365" OR "SharePoint" OR "Office 365") '
     '"September 2026" OR "October 2026"', RFP),

    ("RFP | Azure cloud RFP 2026",
     '("request for proposal" OR "RFP") '
     '("Azure migration" OR "Microsoft Azure" OR "cloud migration") '
     '"2026"', RFP),

    ("RFP | Dynamics 365 CRM RFP Sep/Aug 2026",
     '("request for proposal" OR "RFP") '
     '("Dynamics 365" OR "Microsoft CRM" OR "D365 implementation") '
     '"2026"', RFP),

    ("RFP | Power Platform implementation RFP 2026",
     '("request for proposal" OR "RFP") '
     '("Power Platform" OR "Power Apps" OR "Power Automate") '
     '"2026"', RFP),

    ("RFP | Enterprise AI chatbot RFP 2026",
     '("request for proposal" OR "RFP") '
     '("AI chatbot" OR "enterprise AI" OR "Copilot Studio") '
     '"2026"', RFP),
]

# ─── Field extraction helpers ─────────────────────────────────────────────────
EMAIL_RE   = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
PHONE_RE   = re.compile(r'(\+[1-9]\d{7,14}|\b0\d{9,13}|\b[1-9]\d{9})')   # loose
COUNTRY_TZ = {
    "india": "Asia/Kolkata", "us": "America/New_York", "usa": "America/New_York",
    "united states": "America/New_York", "uk": "Europe/London",
    "united kingdom": "Europe/London", "australia": "Australia/Sydney",
    "canada": "America/Toronto", "uae": "Asia/Dubai", "dubai": "Asia/Dubai",
    "singapore": "Asia/Singapore", "nigeria": "Africa/Lagos",
    "south africa": "Africa/Johannesburg", "kenya": "Africa/Nairobi",
    "pakistan": "Asia/Karachi", "bangladesh": "Asia/Dhaka",
    "germany": "Europe/Berlin", "france": "Europe/Paris",
}

def guess_timezone(text: str) -> str:
    low = text.lower()
    for keyword, tz in COUNTRY_TZ.items():
        if keyword in low:
            return tz
    for tz_token in re.findall(r'[A-Z][a-z]+/[A-Z][a-z]+', text):
        return tz_token
    return "UTC"

def extract_email(text: str) -> str:
    mails = set(EMAIL_RE.findall(text)) - {"example.com", "domain.com"}
    return next(iter(mails), "")

def extract_phone(text: str) -> str:
    hits = PHONE_RE.findall(text)
    for h in hits:
        h = re.sub(r'[^\d+]', '', h)
        if re.fullmatch(r'\+[1-9]\d{7,14}', h):
            return h
    return ""

def extract_company(text: str) -> str:
    # Try "Author: Name" — often the company / poster
    m = re.search(r'Author:\s*(.+)', text)
    if m:
        val = m.group(1).strip()
        if val and val.lower() not in ("n/a", "unknown", ""):
            return val[:200]
    # Try "## Company Name" heading
    m = re.search(r'##\s+(.+)', text)
    if m:
        return m.group(1).strip()[:200]
    return "Unknown"

def extract_location(text: str) -> str:
    # Explicit "Location:" label
    m = re.search(r'Location:\s*([^\n]+)', text)
    if m:
        return m.group(1).strip()[:200]
    # City, State / Country patterns
    m = re.search(
        r'\b([A-Z][a-z]+(?: [A-Z][a-z]+)?,\s*'
        r'(?:[A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)?))\b',
        text,
    )
    if m:
        return m.group(1).strip()[:200]
    return ""

def extract_contact_name(text: str) -> str:
    m = re.search(r'Author:\s*(.+)', text)
    if m:
        val = m.group(1).strip()
        if val and val.lower() not in ("n/a", "unknown"):
            return val[:120]
    return "Unknown"

def build_requirement(text: str, label: str) -> str:
    """Generate a clear requirement sentence from the post snippet."""
    # Look for explicit requirement signals in the text
    lower = text.lower()
    services = []
    if "sharepoint" in lower:
        services.append("SharePoint Online")
    if "microsoft 365" in lower or "m365" in lower or "office 365" in lower:
        services.append("Microsoft 365")
    if "azure" in lower:
        services.append("Azure Cloud")
    if "power apps" in lower or "power platform" in lower:
        services.append("Power Platform / Power Apps")
    if "dynamics 365" in lower or "d365" in lower:
        services.append("Dynamics 365")
    if "ai chatbot" in lower or "copilot" in lower or "chatbot" in lower:
        services.append("Enterprise AI / Chatbot")

    if services:
        return f"Seeking implementation partner for {', '.join(services)}."
    # Fallback: extract a highlights snippet
    m = re.search(r'Highlights:\s*(.{30,300})', text, re.DOTALL)
    if m:
        snippet = re.sub(r'\s+', ' ', m.group(1)).strip()
        return snippet[:400]
    return f"Lead from {label}"

# ─── Core Exa fetch ──────────────────────────────────────────────────────────
def fetch(label: str, query: str, domains: list[str]) -> list[dict]:
    executable = shutil.which("mcporter")
    if not executable:
        return []

    args: dict = {
        "query": query,
        "numResults": NUM_RESULTS,
        "startPublishedDate": THIRTY_AGO,
    }
    if domains:
        args["includeDomains"] = domains

    print(f"  ↳ [{label}]", flush=True)

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

        # ── Date filter ──────────────────────────────────────────────────
        date_m = re.search(
            r"Published:\s*(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?)",
            text,
        )
        if not date_m:
            print("    ✗ No date — skipping", flush=True)
            continue
        raw = date_m.group(1).rstrip("Z").split(".")[0]
        if "T" not in raw:
            raw += "T00:00:00"
        try:
            pub_dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"    ✗ Bad date '{raw}' — skipping", flush=True)
            continue
        age = (datetime.now(timezone.utc) - pub_dt).days
        if age > CUTOFF_DAYS:
            print(f"    ✗ {age}d old — too old", flush=True)
            continue
        print(f"    ✓ {age}d old — KEPT", flush=True)

        # ── Extract display fields ───────────────────────────────────────
        title_m  = re.search(r"Title:\s*(.+)", text)
        url_m    = re.search(r"URL:\s*(https?://\S+)", text)
        title    = title_m.group(1).strip()[:120] if title_m else "—"
        url      = url_m.group(1).strip()         if url_m   else "—"

        company      = extract_company(text)
        contact_name = extract_contact_name(text)
        location     = extract_location(text)
        email        = extract_email(text)
        phone        = extract_phone(text)
        requirement  = build_requirement(text, label)
        tz           = guess_timezone(text + " " + location)

        # Platform label
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
        elif any(kw in text.lower() for kw in ["rfp", "request for proposal", "tender"]):
            platform = "RFP Board"
        else:
            platform = "Web"

        kept.append({
            # ── Platform CSV import fields ─────────────────────────────
            "company":       company,
            "requirement":   requirement,
            "source_url":    url,
            "contact_name":  contact_name,
            "phone":         phone,
            "location":      location,
            "timezone":      tz,
            "consent_basis": f"Public post scraped from {platform}",
            # ── Dashboard display extras ───────────────────────────────
            "title":         title,
            "published_at":  raw,
            "age_days":      age,
            "platform":      platform,
            "email":         email,
            "label":         label,
            "snippet":       text[:700],
        })
    return kept

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n🚀 Futurrizon Lead Scraper v2  [{now_str}]")
    print(f"   Cutoff: {CUTOFF_DAYS} days | Queries: {len(QUERIES)} | Workers: {MAX_WORKERS}\n")

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch, label, query, domains): label
            for label, query, domains in QUERIES
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                for row in future.result():
                    if row["source_url"] not in seen_urls:
                        seen_urls.add(row["source_url"])
                        all_results.append(row)
            except Exception as exc:
                print(f"  [ERROR] {label}: {exc}", flush=True)

    all_results.sort(key=lambda x: x["age_days"])
    total = len(all_results)
    print(f"\n✅  {total} unique leads found within the past {CUTOFF_DAYS} days.\n")

    # ── Markdown output ───────────────────────────────────────────────────
    md = [
        f"# Futurrizon Buyer-Intent Leads — Past {CUTOFF_DAYS} Days",
        f"_Generated: {now_str}_\n",
        f"**Total:** {total} unique leads | **Cutoff:** {CUTOFF_DAYS} days\n",
        "| # | Company | Platform | Age | Title | URL |",
        "|---|---------|----------|-----|-------|-----|",
    ]
    for i, r in enumerate(all_results, 1):
        md.append(
            f"| {i} | {r['company']} | {r['platform']} | {r['age_days']}d | "
            f"{r['title'][:50]}... | [{r['source_url'][:40]}...]({r['source_url']}) |"
        )
    md.append("\n---\n")
    for i, r in enumerate(all_results, 1):
        md += [
            f"## Lead {i} — {r['title']}",
            f"- **Platform:** {r['platform']}  |  **Age:** {r['age_days']} days old",
            f"- **URL:** {r['source_url']}",
            f"- **Company / Author:** {r['company']}",
            f"- **Contact Name:** {r['contact_name']}",
            f"- **Location:** {r['location'] or '—'}",
            f"- **Timezone:** {r['timezone']}",
            f"- **Email:** {r['email'] or '—'}",
            f"- **Phone:** {r['phone'] or '—'}",
            f"- **Requirement:** {r['requirement']}",
            f"- **Consent Basis:** {r['consent_basis']}",
            f"\n**Snippet:**\n```\n{r['snippet']}\n```\n",
            "---\n",
        ]

    with open(OUTPUT_MD, "w") as f:
        f.write("\n".join(md))

    # ── JSON output ───────────────────────────────────────────────────────
    with open(OUTPUT_JSON, "w") as f:
        json.dump(all_results, f, indent=2)

    # ── CSV output (ready for platform import) ────────────────────────────
    import csv
    platform_fields = [
        "company", "requirement", "source_url", "contact_name",
        "phone", "location", "timezone", "consent_basis",
    ]
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=platform_fields, extrasaction="ignore")
        writer.writeheader()
        # Only rows that have a phone (required for calling import)
        importable = [r for r in all_results if r.get("phone")]
        writer.writerows(importable)
    print(f"   → {len(importable)} rows with phone numbers written to CSV (platform-importable)")

    print(f"\n💾 MD  : {OUTPUT_MD}")
    print(f"💾 JSON: {OUTPUT_JSON}")
    print(f"💾 CSV : {OUTPUT_CSV}  (import-ready)")

if __name__ == "__main__":
    main()
