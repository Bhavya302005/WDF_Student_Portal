"""
Deep Research Email Finder
==========================
Finds, predicts, and verifies corporate and professional email addresses using
a 5-layer intelligence pipeline:

1. Web Citation Mining: Discovers published email addresses and colleague patterns.
2. Pattern Reverse-Engineering: Inters company-wide naming formulas ({first}.{last}, {f}{last}, etc.).
3. Permutation Matrix: Generates candidate addresses ranked by corporate frequency.
4. DNS MX Diagnostics: Identifies mail provider (Google Workspace, Microsoft 365, etc.).
5. Zero-Send RFC 5321 SMTP Handshake: Probes the server to confirm mailbox existence & catch-all status.

Usage:
    # Single lookup
    python job_scraper/deep_email_finder.py "Tiffany Wilson" "kirbybuildingsystems.com" "Kirby Building Systems"
    python job_scraper/deep_email_finder.py --name "Satya Nadella" --domain "microsoft.com"

    # Batch enrichment
    python job_scraper/deep_email_finder.py --batch job_scraper/Futurrizon_DeepPhones.json
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import smtplib
import socket
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Load Environment Variables from .env ─────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _REPO_ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

EXA_API_KEY = os.environ.get("EXA_API_KEY", "")

try:
    import dns.resolver
    HAS_DNS = True
except ImportError:
    HAS_DNS = False

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

SKIP_DOMAINS = {
    "example.com", "w3.org", "sentry.io", "github.com", "schema.org",
    "domain.com", "email.com", "google.com", "linkedin.com", "twitter.com",
    "facebook.com", "instagram.com", "youtube.com", "gravatar.com"
}


# ── Mail Provider Identification ─────────────────────────────────────────────
def identify_mail_provider(mx_hosts: list[str]) -> str:
    """Identifies the mail infrastructure provider from MX hostnames."""
    if not mx_hosts:
        return "Unknown / No MX"
    joined = " ".join(mx_hosts).lower()
    if "google" in joined or "googlemail" in joined or "aspmx" in joined:
        return "Google Workspace"
    if "outlook" in joined or "protection.outlook.com" in joined or "office365" in joined:
        return "Microsoft 365 Exchange"
    if "pphosted" in joined or "proofpoint" in joined:
        return "Proofpoint Protection"
    if "mimecast" in joined:
        return "Mimecast Secure Gateway"
    if "zoho" in joined:
        return "Zoho Mail"
    if "barracuda" in joined:
        return "Barracuda Networks"
    if "amazonaws" in joined or "aws" in joined:
        return "Amazon SES / WorkMail"
    return mx_hosts[0]


# ── Pattern Detection ────────────────────────────────────────────────────────
def detect_company_pattern(emails: list[str], domain: str) -> Optional[dict[str, Any]]:
    """
    Analyzes colleague emails on a target domain to determine the company-wide pattern.
    Returns the dominant pattern and frequency statistics.
    """
    d = domain.lower().replace("www.", "").strip()
    local_parts = [e.split("@")[0].lower() for e in emails if e.lower().endswith(f"@{d}")]
    if not local_parts:
        return None

    patterns = []
    for lp in local_parts:
        if "." in lp and len(lp.split(".")) == 2:
            p1, p2 = lp.split(".")
            if len(p1) == 1 and len(p2) > 1:
                patterns.append("{f}.{last}")
            elif len(p1) > 1 and len(p2) == 1:
                patterns.append("{first}.{l}")
            else:
                patterns.append("{first}.{last}")
        elif "_" in lp and len(lp.split("_")) == 2:
            patterns.append("{first}_{last}")
        elif "-" in lp and len(lp.split("-")) == 2:
            patterns.append("{first}-{last}")
        elif len(lp) > 3:
            # check single word e.g. 'john' or 'jsmith'
            patterns.append("{first_or_flast}")

    if not patterns:
        return None

    counts = collections.Counter(patterns)
    most_common, count = counts.most_common(1)[0]
    total = len(patterns)
    pct = count / total

    return {
        "pattern": most_common,
        "sample_count": total,
        "pattern_matches": count,
        "confidence": round(pct, 2),
    }


# ── Candidate Email Permutation ──────────────────────────────────────────────
def generate_candidates(first: str, last: str, domain: str) -> list[dict[str, Any]]:
    """Generates standard corporate email permutations ranked by general prevalence."""
    f = re.sub(r"[^a-zA-Z]", "", first.lower())
    l = re.sub(r"[^a-zA-Z]", "", last.lower())
    d = domain.lower().replace("www.", "").strip()

    if not f or not d:
        return []

    if l:
        return [
            {"email": f"{f}.{l}@{d}", "pattern": "{first}.{last}", "weight": 0.45},
            {"email": f"{f}@{d}", "pattern": "{first}", "weight": 0.20},
            {"email": f"{f[0]}{l}@{d}", "pattern": "{f}{last}", "weight": 0.15},
            {"email": f"{f}_{l}@{d}", "pattern": "{first}_{last}", "weight": 0.05},
            {"email": f"{f}{l[0]}@{d}", "pattern": "{first}{l}", "weight": 0.05},
            {"email": f"{l}.{f}@{d}", "pattern": "{last}.{first}", "weight": 0.04},
            {"email": f"{f}{l}@{d}", "pattern": "{first}{last}", "weight": 0.03},
            {"email": f"{f[0]}.{l}@{d}", "pattern": "{f}.{last}", "weight": 0.02},
            {"email": f"{f}-{l}@{d}", "pattern": "{first}-{last}", "weight": 0.01},
        ]
    return [{"email": f"{f}@{d}", "pattern": "{first}", "weight": 0.85}]


# ── DNS MX Record Diagnostics ────────────────────────────────────────────────
def get_mx_records(domain: str) -> list[str]:
    """Retrieves sorted mail exchange (MX) server hostnames for a domain."""
    d = domain.lower().replace("www.", "").strip()
    if not HAS_DNS:
        return []
    try:
        answers = dns.resolver.resolve(d, "MX")
        mx_list = sorted([(r.preference, str(r.exchange).rstrip(".")) for r in answers])
        return [mx[1] for mx in mx_list]
    except Exception:
        return []


# ── Zero-Send RFC 5321 SMTP Handshake ────────────────────────────────────────
def verify_smtp_mailbox(
    mx_host: str,
    email: str,
    timeout: float = 2.0,
) -> tuple[bool, Optional[bool], str]:
    """
    Performs an RFC 5321 zero-send SMTP probe to verify mailbox deliverability.
    Executes in a separate thread with a hard timeout to prevent ISP tarpitting.
    """
    import concurrent.futures

    def _probe() -> tuple[bool, Optional[bool], str]:
        d = email.split("@")[-1]
        is_catch_all = None
        try:
            server = smtplib.SMTP(timeout=timeout)
            server.connect(mx_host, 25)
            server.helo("verify.sales-agent.local")
            server.mail("probe@sales-agent.local")

            # 1. Catch-all test
            fake_email = f"probe_test_nonexistent_xyz99@{d}"
            code_fake, _ = server.rcpt(fake_email)
            if code_fake == 250:
                is_catch_all = True

            # 2. Probe target candidate email
            code, message = server.rcpt(email)
            server.quit()

            msg_str = message.decode("utf-8", errors="ignore") if isinstance(message, bytes) else str(message)
            if code == 250:
                if is_catch_all:
                    return True, True, "250 OK (Domain is Catch-All: Accepts any address)"
                return True, False, "250 OK - Mailbox Verified Deliverable"
            elif code in (550, 551, 552, 553, 554):
                return False, False, f"{code} Mailbox Does Not Exist / Rejected"
            return False, is_catch_all, f"Code {code}: {msg_str}"
        except Exception as e:
            return False, None, f"SMTP Port 25 filtered or blocked by ISP ({e})"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_probe)
        try:
            return future.result(timeout=timeout + 0.5)
        except concurrent.futures.TimeoutError:
            return False, None, "SMTP Port 25 timed out (ISP firewall filter)"


# ── Web Citation & Directory Mining (Exa) ────────────────────────────────────
def search_web_emails(
    name: str,
    domain: Optional[str] = None,
    company: Optional[str] = None,
) -> list[str]:
    """
    Crawls indexed public documents, PDFs, directories, and press releases
    for email citations referencing the individual and their organization.
    """
    if not EXA_API_KEY:
        return []

    queries: list[str] = []
    if name:
        queries.append(f'"{name}" email OR contact')
    if domain:
        clean_d = domain.lower().replace("www.", "").strip()
        if name:
            queries.append(f'"{name}" "@{clean_d}"')
            queries.append(f'site:{clean_d} "{name}"')
        queries.append(f'site:{clean_d} email OR contact')
    if company and name:
        queries.append(f'"{name}" "{company}" email')
    if company and domain:
        queries.append(f'"{company}" "@{clean_d}" email')

    found: set[str] = set()

    for q in queries[:6]:
        req_data = json.dumps({
            "query": q,
            "numResults": 5,
            "contents": {"text": True}
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.exa.ai/search",
            headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
            data=req_data
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode())
                for r in data.get("results", []):
                    text = r.get("text", "")
                    for e in EMAIL_RE.findall(text):
                        dom = e.split("@")[-1].lower().strip(".")
                        if (
                            dom not in SKIP_DOMAINS
                            and not e.endswith(".png")
                            and not e.endswith(".jpg")
                            and "@2x" not in e
                            and len(e) < 64
                        ):
                            found.add(e.lower().strip(".,;:()<>"))
        except Exception:
            pass

    return sorted(found)


# ── Domain Resolver ──────────────────────────────────────────────────────────
def resolve_company_domain(company: str) -> Optional[str]:
    """Finds the official domain for a company name via search."""
    if not EXA_API_KEY or not company:
        return None
    req_data = json.dumps({
        "query": f'"{company}" official website homepage',
        "numResults": 3,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.exa.ai/search",
        headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
        data=req_data
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for r in data.get("results", []):
                url = r.get("url", "")
                parsed = urllib.parse.urlparse(url)
                netloc = parsed.netloc.replace("www.", "").strip()
                if netloc and not any(skip in netloc for skip in ("linkedin.com", "wikipedia.org", "facebook.com", "twitter.com")):
                    return netloc
    except Exception:
        pass
    return None


# ── Main Deep Email Discovery Engine ─────────────────────────────────────────
def deep_find_email(
    name: str,
    domain: Optional[str] = None,
    company: Optional[str] = None,
    verify_smtp: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Executes the 5-layer deep research email discovery pipeline.
    """
    clean_name = name.strip()
    clean_domain = domain.lower().replace("www.", "").strip() if domain else None
    clean_company = company.strip() if company else None

    # Resolve domain if missing but company is provided
    if not clean_domain and clean_company:
        if verbose:
            print(f"  🌐 Resolving official domain for '{clean_company}'...")
        clean_domain = resolve_company_domain(clean_company)
        if clean_domain and verbose:
            print(f"  ✅ Discovered domain: {clean_domain}")

    name_parts = clean_name.split()
    first = name_parts[0].lower() if name_parts else ""
    last = name_parts[-1].lower() if len(name_parts) > 1 else ""

    result: dict[str, Any] = {
        "name": clean_name,
        "company": clean_company,
        "domain": clean_domain,
        "best_email": None,
        "confidence": 0.0,
        "status": "not_found",
        "detected_pattern": None,
        "pattern_stats": None,
        "mail_provider": "Unknown",
        "mx_hosts": [],
        "web_citations": [],
        "smtp_verified": False,
        "is_catch_all": None,
        "smtp_detail": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if verbose:
        print("=" * 68)
        print(f"🔎 DEEP RESEARCH EMAIL FINDER")
        print(f"Target  : {clean_name}")
        print(f"Company : {clean_company or 'Not specified'}")
        print(f"Domain  : {clean_domain or 'Not specified'}")
        print("=" * 68)

    # ── Step 1: Web Crawl for Citations ──
    if verbose:
        print("\n[Layer 1] Mining Web Citations & Colleague Directory...")
    web_emails = search_web_emails(clean_name, clean_domain, clean_company)
    result["web_citations"] = web_emails

    # Check for direct matches on web
    direct_matches = []
    for e in web_emails:
        lp = e.split("@")[0].lower()
        if first and last and (first in lp and last in lp):
            direct_matches.append(e)
        elif first and (first in lp):
            direct_matches.append(e)

    if direct_matches:
        chosen = direct_matches[0]
        if verbose:
            print(f"  ✅ Direct email citation found on web: {chosen}")
        result["best_email"] = chosen
        result["confidence"] = 0.95
        result["status"] = "web_verified_direct"
    elif web_emails:
        if verbose:
            print(f"  ℹ️ Found {len(web_emails)} colleague/company email citations.")
    else:
        if verbose:
            print("  ℹ️ No public email citations found on indexed pages.")

    # ── Step 2: Company Pattern Reverse-Engineering ──
    if clean_domain:
        if verbose:
            print(f"\n[Layer 2] Reverse-Engineering Email Pattern for '{clean_domain}'...")
        pattern_info = detect_company_pattern(web_emails, clean_domain)
        if pattern_info:
            pat = pattern_info["pattern"]
            result["detected_pattern"] = pat
            result["pattern_stats"] = pattern_info
            if verbose:
                print(f"  🎯 Company pattern identified: {pat}@{clean_domain} (Confidence: {int(pattern_info['confidence']*100)}%)")

            if not result["best_email"]:
                if pat == "{first}.{last}" and first and last:
                    result["best_email"] = f"{first}.{last}@{clean_domain}"
                elif pat == "{f}.{last}" and first and last:
                    result["best_email"] = f"{first[0]}.{last}@{clean_domain}"
                elif pat == "{first}_{last}" and first and last:
                    result["best_email"] = f"{first}_{last}@{clean_domain}"
                elif pat == "{first}-{last}" and first and last:
                    result["best_email"] = f"{first}-{last}@{clean_domain}"
                elif pat == "{first}{last}" and first and last:
                    result["best_email"] = f"{first}{last}@{clean_domain}"
                elif pat == "{first}" and first:
                    result["best_email"] = f"{first}@{clean_domain}"

                if result["best_email"]:
                    result["confidence"] = 0.88
                    result["status"] = "pattern_inferred"

    # ── Step 3: Permutation Matrix Fallback ──
    if clean_domain and not result["best_email"]:
        if verbose:
            print(f"\n[Layer 3] Generating Candidate Permutation Matrix...")
        candidates = generate_candidates(first, last, clean_domain)
        if candidates:
            result["best_email"] = candidates[0]["email"]
            result["confidence"] = 0.65
            result["status"] = "most_likely_permutation"
            if verbose:
                print(f"  🔮 Top permutation candidate: {result['best_email']} ({int(candidates[0]['weight']*100)}% industry base)")

    # ── Step 4: DNS MX & Mail Infrastructure ──
    if clean_domain:
        if verbose:
            print(f"\n[Layer 4] DNS MX Infrastructure Diagnostics for '{clean_domain}'...")
        mx_hosts = get_mx_records(clean_domain)
        result["mx_hosts"] = mx_hosts
        provider = identify_mail_provider(mx_hosts)
        result["mail_provider"] = provider
        if mx_hosts:
            if verbose:
                print(f"  ✅ Mail Provider: {provider}")
                print(f"  📮 Primary MX   : {mx_hosts[0]}")
        else:
            if verbose:
                print("  ⚠️ No MX records discovered for domain.")
            result["confidence"] = min(result["confidence"], 0.30)
            result["status"] = "no_mx_record"

    # ── Step 5: Zero-Send RFC 5321 SMTP Handshake ──
    if verify_smtp and clean_domain and result["best_email"] and result["mx_hosts"]:
        primary_mx = result["mx_hosts"][0]
        if verbose:
            print(f"\n[Layer 5] RFC 5321 Zero-Send SMTP Probe on {primary_mx}...")
        is_deliv, is_catchall, detail = verify_smtp_mailbox(primary_mx, result["best_email"])
        result["smtp_verified"] = is_deliv
        result["is_catch_all"] = is_catchall
        result["smtp_detail"] = detail

        if verbose:
            print(f"  📡 Handshake Result: {detail}")

        if is_deliv and not is_catchall:
            result["confidence"] = max(result["confidence"], 0.94)
            result["status"] = "smtp_verified_mailbox"
        elif is_catchall:
            result["confidence"] = min(result["confidence"], 0.85)

    # Summary Display
    if verbose:
        print("\n" + "=" * 68)
        print("📊 DEEP EMAIL INTELLIGENCE REPORT")
        print(f"  🎯 Best Email       : {result['best_email'] or 'Not Discovered'}")
        print(f"  📈 Confidence Score : {int(result['confidence'] * 100)}%")
        print(f"  🏷️ Status           : {result['status']}")
        print(f"  🏢 Mail Provider    : {result['mail_provider']}")
        if result["detected_pattern"]:
            print(f"  🧬 Company Formula  : {result['detected_pattern']}@{clean_domain}")
        if result["is_catch_all"] is not None:
            print(f"  🛡️ Catch-All Domain : {'Yes' if result['is_catch_all'] else 'No'}")
        print(f"  🌐 Total Citations  : {len(result['web_citations'])} email(s) indexed")
        print("=" * 68)

    return result


# ── Batch Lead Processing ────────────────────────────────────────────────────
def run_batch_enrichment(input_path: str, output_prefix: Optional[str] = None) -> None:
    """Enriches a JSON or CSV list of leads with deep email intelligence."""
    inp = Path(input_path)
    if not inp.exists():
        print(f"Error: Input file {input_path} not found.")
        return

    if output_prefix is None:
        output_prefix = str(inp.with_suffix("")) + "_DeepEmails"

    out_json = Path(f"{output_prefix}.json")
    out_csv = Path(f"{output_prefix}.csv")
    out_md = Path(f"{output_prefix}.md")

    leads_data: list[dict[str, Any]] = []
    if inp.suffix.lower() == ".json":
        leads_data = json.loads(inp.read_text(encoding="utf-8"))
    elif inp.suffix.lower() == ".csv":
        with open(inp, mode="r", encoding="utf-8") as f:
            leads_data = list(csv.DictReader(f))

    print(f"Starting batch deep email enrichment for {len(leads_data)} leads from {inp.name}...")

    results = []
    for idx, lead in enumerate(leads_data, 1):
        name = lead.get("contact_name") or lead.get("name") or lead.get("author") or ""
        company = lead.get("company") or lead.get("organization") or ""
        domain = lead.get("domain") or ""
        url = lead.get("source_url") or lead.get("canonical_url") or ""

        # Extract domain from URL if missing
        if not domain and url:
            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc.replace("www.", "").strip()

        print(f"\n--- Lead {idx}/{len(leads_data)}: {name or 'Anonymous'} @ {company or 'Unknown'} ---")
        enriched = deep_find_email(name=name, domain=domain, company=company, verify_smtp=False, verbose=True)

        merged = {**lead, "deep_email": enriched}
        results.append(merged)

    # Write Output JSON
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✅ Exported JSON: {out_json}")

    # Write Output CSV
    csv_rows = []
    for r in results:
        e = r.get("deep_email", {})
        csv_rows.append({
            "name": e.get("name"),
            "company": e.get("company"),
            "domain": e.get("domain"),
            "best_email": e.get("best_email"),
            "confidence": e.get("confidence"),
            "status": e.get("status"),
            "mail_provider": e.get("mail_provider"),
            "detected_pattern": e.get("detected_pattern"),
        })

    with open(out_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "company", "domain", "best_email", "confidence", "status", "mail_provider", "detected_pattern"])
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"✅ Exported CSV: {out_csv}")

    # Write Output Markdown Report
    md_lines = [
        "# Deep Research Email Intelligence Report",
        f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
        f"**Total Leads**: {len(results)}  ",
        "",
        "| Name | Company / Domain | Best Email | Confidence | Status | Mail Infrastructure |",
        "| :--- | :--- | :--- | :---: | :--- | :--- |",
    ]
    for r in results:
        e = r.get("deep_email", {})
        pct = f"{int(e.get('confidence', 0)*100)}%"
        md_lines.append(
            f"| {e.get('name') or '-'} | {e.get('company') or e.get('domain') or '-'} | "
            f"`{e.get('best_email') or 'N/A'}` | **{pct}** | `{e.get('status')}` | {e.get('mail_provider')} |"
        )

    out_md.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"✅ Exported Markdown: {out_md}")


# ── Entrypoint ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Deep Research Email Finder")
    parser.add_argument("name", nargs="?", default=None, help="Target person name (e.g. 'Tiffany Wilson')")
    parser.add_argument("domain", nargs="?", default=None, help="Corporate domain (e.g. 'kirbybuildingsystems.com')")
    parser.add_argument("company", nargs="?", default=None, help="Company name (e.g. 'Kirby Building Systems')")
    parser.add_argument("--name", dest="flag_name", help="Target person name")
    parser.add_argument("--domain", dest="flag_domain", help="Target corporate domain")
    parser.add_argument("--company", dest="flag_company", help="Target company name")
    parser.add_argument("--smtp", dest="verify_smtp", action="store_true", help="Enable RFC 5321 zero-send SMTP mailbox probe")
    parser.add_argument("--batch", dest="batch_file", help="Path to JSON or CSV leads file to batch enrich")

    args = parser.parse_args()

    if args.batch_file:
        run_batch_enrichment(args.batch_file)
        return

    name = args.flag_name or args.name
    domain = args.flag_domain or args.domain
    company = args.flag_company or args.company

    if not name and not company and not domain:
        print("Usage: python job_scraper/deep_email_finder.py <name> [domain] [company]")
        print("   or: python job_scraper/deep_email_finder.py --batch <leads.json>")
        sys.exit(1)

    deep_find_email(
        name=name or "",
        domain=domain,
        company=company,
        verify_smtp=args.verify_smtp,
        verbose=True
    )


if __name__ == "__main__":
    main()
