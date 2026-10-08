"""
Futurrizon Lead Enricher — Free Decision-Maker Contact Finder
=============================================================
For each lead in Futurrizon_Exa_Leads.json, uses Exa neural search to:
  1. Find the company's official website → scrape contact page for phone
  2. Search LinkedIn for the key decision-maker's public contact info
  3. Search Google-dork style for "[person] [company] phone OR email"
  4. Extract any WhatsApp, Calendly, or email links from public posts

Outputs an enriched JSON + a CSV ready for platform import.
All searches use the already-integrated Exa MCP — 100% free.
"""

import csv
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from pathlib import Path

INPUT_JSON   = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Exa_Leads.json"
OUTPUT_JSON  = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Enriched_Leads.json"
OUTPUT_CSV   = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Enriched_Leads.csv"
OUTPUT_MD    = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/Futurrizon_Enriched_Leads.md"

# E.164 phone regex — also catches WhatsApp numbers
PHONE_RE  = re.compile(r'(\+[1-9]\d{7,14})')
EMAIL_RE  = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
CLEAN_URL = re.compile(r'https?://[^\s\)\]"\']+')

# Known junk / false-positive emails to skip
SKIP_EMAILS = {
    "jane@freelancer.com", "example.com", "domain.com",
    "noreply@", "support@", "info@microsoft.com"
}

PLATFORM_CSV_FIELDS = [
    "company", "requirement", "source_url", "contact_name",
    "phone", "location", "timezone", "consent_basis",
]


def exa_search(query: str, domains: list[str] = None, num: int = 5) -> str:
    """Run one Exa query and return the raw text block."""
    executable = shutil.which("mcporter")
    if not executable:
        return ""
    args: dict = {"query": query, "numResults": num}
    if domains:
        args["includeDomains"] = domains

    result = subprocess.run(
        [executable, "call", "exa.web_search_exa",
         "--output", "json", "--args", json.dumps(args),
         "--timeout", "30000", "--no-oauth"],
        capture_output=True, check=False, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        payload = json.loads(result.stdout)
        texts = [b["text"] for b in payload.get("content", []) if b.get("type") == "text"]
        return "\n".join(texts)
    except Exception:
        return ""


def pick_emails(text: str) -> list[str]:
    found = set(EMAIL_RE.findall(text))
    cleaned = []
    for e in found:
        low = e.lower()
        if any(skip in low for skip in SKIP_EMAILS):
            continue
        if low.endswith(".png") or low.endswith(".jpg") or "@2x" in low:
            continue
        cleaned.append(e)
    return cleaned


def pick_phones(text: str) -> list[str]:
    return list(set(PHONE_RE.findall(text)))


def enrich_lead(lead: dict) -> dict:
    """Run 3 Exa searches per lead to find decision-maker contact info."""
    company      = lead.get("company", "")
    contact_name = lead.get("contact_name", "")
    source_url   = lead.get("source_url", "")
    existing_email = lead.get("email", "")
    existing_phone = lead.get("phone", "")

    print(f"\n🔍 Enriching: {company} / {contact_name}", flush=True)

    all_text = ""

    # ── Search 1: Company contact page ───────────────────────────────────
    if company and company.lower() not in ("unknown", "—", "n/a"):
        q1 = f'"{company}" contact page phone email "reach us" OR "get in touch"'
        t1 = exa_search(q1, num=3)
        all_text += t1
        phones1 = pick_phones(t1)
        emails1 = pick_emails(t1)
        print(f"  [1] Company contact page → phones: {phones1}, emails: {emails1}", flush=True)

    # ── Search 2: Decision-maker LinkedIn / personal contact ─────────────
    if contact_name and contact_name.lower() not in ("unknown", "—", "n/a"):
        q2 = (
            f'"{contact_name}" "{company}" '
            f'(phone OR "WhatsApp" OR email OR "reach me" OR "DM me" OR "contact me")'
        )
        t2 = exa_search(q2, num=5)
        all_text += t2
        phones2 = pick_phones(t2)
        emails2 = pick_emails(t2)
        print(f"  [2] DM search → phones: {phones2}, emails: {emails2}", flush=True)

    # ── Search 3: Exa fetch the source URL itself for hidden contact info ─
    if source_url and source_url.startswith("http"):
        q3 = f'site:{source_url.split("/")[2]} (phone OR email OR WhatsApp OR "contact us")'
        t3 = exa_search(q3, domains=[source_url.split("/")[2]], num=3)
        all_text += t3
        phones3 = pick_phones(t3)
        emails3 = pick_emails(t3)
        print(f"  [3] Source domain search → phones: {phones3}, emails: {emails3}", flush=True)

    # ── Search 4: RFP-specific — search for tender contact officer ────────
    if lead.get("platform") == "RFP Board":
        q4 = (
            f'"{company}" RFP tender "contact" "officer" OR "procurement" '
            f'(phone OR email)'
        )
        t4 = exa_search(q4, num=3)
        all_text += t4
        phones4 = pick_phones(t4)
        emails4 = pick_emails(t4)
        print(f"  [4] RFP officer search → phones: {phones4}, emails: {emails4}", flush=True)

    # ── Aggregate best contact info ───────────────────────────────────────
    all_phones = pick_phones(all_text)
    all_emails = pick_emails(all_text)

    # Prefer existing values if we already had them
    final_phone = existing_phone or (all_phones[0] if all_phones else "")
    final_email = existing_email or (all_emails[0] if all_emails else "")

    # Also look for Calendly / WhatsApp links
    wa_links      = re.findall(r'https?://wa\.me/[^\s\)\]"\']+', all_text)
    calendly_links = re.findall(r'https?://calendly\.com/[^\s\)\]"\']+', all_text)

    enriched = dict(lead)  # copy
    enriched.update({
        "phone":           final_phone,
        "email":           final_email,
        "all_phones_found": all_phones,
        "all_emails_found": all_emails,
        "whatsapp_links":   wa_links,
        "calendly_links":   calendly_links,
        "enriched_at":      datetime.now(UTC).isoformat(),
    })

    status = "✅ PHONE FOUND" if final_phone else ("📧 EMAIL ONLY" if final_email else "❌ No contact")
    print(f"  → {status}  phone={final_phone or '—'}  email={final_email or '—'}", flush=True)
    return enriched


def main():
    leads_path = Path(INPUT_JSON)
    if not leads_path.exists():
        print(f"❌ Input file not found: {INPUT_JSON}")
        return

    with open(leads_path) as f:
        leads = json.load(f)

    if not leads:
        print("❌ No leads found in input JSON.")
        return

    print(f"\n🚀 Enriching {len(leads)} leads for decision-maker contact info...")
    print("   Method: Exa neural search (free) — company pages, LinkedIn, RFP officers\n")

    enriched_leads = []
    # Run enrichment in parallel (3 workers — don't hammer Exa too hard)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(enrich_lead, lead): lead for lead in leads}
        for future in as_completed(futures):
            try:
                enriched_leads.append(future.result())
            except Exception as exc:
                print(f"  [ERROR] {exc}", flush=True)

    # Sort by age
    enriched_leads.sort(key=lambda x: x.get("age_days", 999))

    # Count results
    with_phone = [r for r in enriched_leads if r.get("phone")]
    with_email = [r for r in enriched_leads if r.get("email")]
    print(f"\n📊 Enrichment complete:")
    print(f"   Total leads : {len(enriched_leads)}")
    print(f"   With phone  : {len(with_phone)}")
    print(f"   With email  : {len(with_email)}")
    print(f"   No contact  : {len(enriched_leads) - len(with_phone) - len(with_email)}")

    # ── Write JSON ─────────────────────────────────────────────────────────
    with open(OUTPUT_JSON, "w") as f:
        json.dump(enriched_leads, f, indent=2)

    # ── Write CSV (platform import — only rows with phone) ─────────────────
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PLATFORM_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(with_phone)
    print(f"   → {len(with_phone)} import-ready rows written to CSV")

    # ── Write Markdown report ──────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = [
        f"# Futurrizon Enriched Leads — Decision-Maker Contacts",
        f"_Generated: {now}_\n",
        f"**{len(enriched_leads)} leads enriched** | "
        f"**{len(with_phone)} with phone** | "
        f"**{len(with_email)} with email**\n",
        "| # | Company | Age | Platform | Phone | Email | WhatsApp | Calendly |",
        "|---|---------|-----|----------|-------|-------|----------|---------|",
    ]
    for i, r in enumerate(enriched_leads, 1):
        wa = r["whatsapp_links"][0] if r.get("whatsapp_links") else "—"
        cal = r["calendly_links"][0] if r.get("calendly_links") else "—"
        md.append(
            f"| {i} | {r.get('company', '—')[:30]} | {r.get('age_days', '?')}d "
            f"| {r.get('platform', '—')} | `{r.get('phone') or '—'}` "
            f"| {r.get('email') or '—'} | {wa} | {cal} |"
        )
    md.append("\n---\n")
    for i, r in enumerate(enriched_leads, 1):
        md += [
            f"## Lead {i} — {r.get('title', r.get('company', '—'))[:80]}",
            f"- **Platform:** {r.get('platform')}  |  **Age:** {r.get('age_days')} days",
            f"- **URL:** [{r.get('source_url', '—')}]({r.get('source_url', '')})",
            f"- **Company:** {r.get('company', '—')}",
            f"- **Contact:** {r.get('contact_name', '—')}",
            f"- **📞 Phone:** `{r.get('phone') or 'Not found'}`",
            f"- **📧 Email:** {r.get('email') or 'Not found'}",
            f"- **💬 WhatsApp:** {(r.get('whatsapp_links') or ['—'])[0]}",
            f"- **📅 Calendly:** {(r.get('calendly_links') or ['—'])[0]}",
            f"- **All phones found:** {', '.join(r.get('all_phones_found') or ['—'])}",
            f"- **All emails found:** {', '.join(r.get('all_emails_found') or ['—'])}",
            f"- **Location:** {r.get('location') or '—'}",
            f"- **Requirement:** {r.get('requirement', '—')}",
            "",
            "---\n",
        ]

    with open(OUTPUT_MD, "w") as f:
        f.write("\n".join(md))

    print(f"\n💾 JSON : {OUTPUT_JSON}")
    print(f"💾 CSV  : {OUTPUT_CSV}  (platform import-ready)")
    print(f"💾 MD   : {OUTPUT_MD}")


if __name__ == "__main__":
    main()
