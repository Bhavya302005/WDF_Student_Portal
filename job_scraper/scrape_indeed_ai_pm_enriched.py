"""
scrape_indeed_ai_pm_enriched.py
================================
Scrapes Indeed for "AI Product Manager" across 5 US locations (no cap, last 2 weeks)
and immediately enriches results with:
  - reports_to         : Who the role reports to
  - works_with_cxo     : Yes / No / Not Mentioned
  - cxo_titles_found   : Specific CXO titles (CEO, VP, etc.)
  - team_size_managed  : Team size if mentioned
  - key_stakeholders   : Senior stakeholders mentioned
"""

import sys
import os
import re
import time
import pandas as pd
from typing import Optional

sys.path.insert(0, os.path.abspath('job_scraper/JobSpy-main'))
from jobspy import scrape_jobs

# ─── NLP EXTRACTION PATTERNS ─────────────────────────────────────────────────

REPORTS_TO_PATTERNS = [
    r'(?:this\s+(?:role|position)\s+)?(?:will\s+)?report(?:ing|s)?\s+(?:directly\s+)?to[\s:,]+([A-Z][^\n.!?,;]{3,60})',
    r'(?:directly\s+)?report(?:ing|s)?\s+to\s+the\s+([A-Z][^\n.!?,;]{3,60})',
    r'position\s+reports?\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
    r'you\s+will\s+report\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
]
REPORTS_TO_RE = re.compile('|'.join(REPORTS_TO_PATTERNS), re.IGNORECASE)

ROLE_WORDS_RE = re.compile(
    r'\b(?:manager|director|president|officer|lead|head|chief|vp|vice\s+president|'
    r'supervisor|executive|owner|partner|founder|ceo|cto|cfo|coo|cmo|cpo|'
    r'principal|coordinator|engineer|analyst|architect|team|board)\b',
    re.IGNORECASE,
)

CXO_TITLES = [
    'Chief Executive Officer', 'Chief Technology Officer', 'Chief Financial Officer',
    'Chief Operating Officer', 'Chief Marketing Officer', 'Chief Product Officer',
    'Chief Revenue Officer', 'Chief Information Officer', 'Chief People Officer',
    'Chief Human Resources Officer', 'Chief Security Officer', 'Chief Data Officer',
    'CEO', 'CTO', 'CFO', 'COO', 'CMO', 'CPO', 'CRO', 'CIO', 'CISO', 'CHRO', 'CDO',
    'Co-Founder', 'Cofounder', 'Founder',
    'President', 'Vice President', 'VP', 'SVP', 'EVP',
    'Managing Director', 'Executive Director',
    'C-Suite', 'C-Level', 'CXO', 'Board of Directors',
]
CXO_RE = re.compile(
    r'\b(' + '|'.join(re.escape(t) for t in CXO_TITLES) + r')\b',
    re.IGNORECASE,
)

INTERACTION_TRIGGERS = re.compile(
    r'(?:work(?:ing)?\s+(?:directly\s+)?with|partner(?:ing)?\s+with|'
    r'collaborat(?:e|ing)\s+with|interact(?:ing)?\s+with|'
    r'interface\s+with|liaise\s+with|reports?\s+to|reporting\s+to)',
    re.IGNORECASE,
)

TEAM_SIZE_RE = re.compile(
    r'(?:manag(?:e|es|ing)|lead(?:s|ing)|oversee(?:s|ing)?|supervis(?:e|es|ing)?|coordinating)\s+'
    r'(?:a\s+(?:cross[- ]?functional\s+)?team\s+of\s+)?(\d+[\+\-]?(?:\s*(?:to|-|–)\s*\d+)?)'
    r'\s*(?:person|people|member|engineer|developer|report|staff|employee|contractor)',
    re.IGNORECASE,
)

STAKEHOLDER_RE = re.compile(
    r'(?:work(?:ing)?\s+(?:closely\s+)?with|partner(?:ing)?\s+with|'
    r'collaborat(?:e|ing)\s+(?:closely\s+)?with|alongside)'
    r'\s+([A-Z][^.\n!?,;]{3,60})',
    re.IGNORECASE,
)

SENIORITY_RE = re.compile(
    r'\b(director|manager|lead|head|senior|principal|vp|vice\s+president|'
    r'chief|president|exec|owner|founder|board|stakeholder)\b',
    re.IGNORECASE,
)

# ─── EXTRACTION FUNCTIONS ─────────────────────────────────────────────────────

def extract_reports_to(desc: str) -> Optional[str]:
    if not isinstance(desc, str) or not desc.strip():
        return None
    match = REPORTS_TO_RE.search(desc)
    if not match:
        return None
    captured = next((g for g in match.groups() if g), None)
    if not captured:
        return None
    captured = re.sub(r'\*+', '', captured).strip()
    captured = re.sub(r'\s+', ' ', captured)
    captured = re.split(r'\s+(?:and|or|while|who|with|as|to|for|in|on)\s+', captured, maxsplit=1)[0]
    captured = re.split(r'[,;:|]', captured)[0].strip()
    if len(captured) > 65:
        captured = captured[:65].rsplit(' ', 1)[0]
    if not ROLE_WORDS_RE.search(captured):
        return None
    return captured.strip() if len(captured) > 3 else None


def extract_cxo_info(desc: str) -> tuple:
    if not isinstance(desc, str) or not desc.strip():
        return 'Not Mentioned', ''
    sentences = re.split(r'[.\n!?]', desc)
    direct_cxo_titles = set()
    for sentence in sentences:
        cxo_matches = CXO_RE.findall(sentence)
        if not cxo_matches:
            continue
        has_trigger = bool(INTERACTION_TRIGGERS.search(sentence))
        if has_trigger:
            for title in cxo_matches:
                normalized = {
                    'vp': 'VP', 'svp': 'SVP', 'evp': 'EVP',
                    'ceo': 'CEO', 'cto': 'CTO', 'cfo': 'CFO',
                    'coo': 'COO', 'cmo': 'CMO', 'cpo': 'CPO',
                    'cro': 'CRO', 'cio': 'CIO', 'ciso': 'CISO',
                    'chro': 'CHRO', 'cdo': 'CDO', 'cxo': 'CXO',
                }.get(title.lower(), title)
                direct_cxo_titles.add(normalized)
    if direct_cxo_titles:
        return 'Yes', ', '.join(sorted(direct_cxo_titles))
    if CXO_RE.search(desc):
        return 'No', ''
    return 'Not Mentioned', ''


def extract_team_size(desc: str) -> Optional[str]:
    if not isinstance(desc, str) or not desc.strip():
        return None
    match = TEAM_SIZE_RE.search(desc)
    if not match:
        return None
    size = next((g for g in match.groups() if g), None)
    return size.strip() if size else None


def extract_key_stakeholders(desc: str) -> Optional[str]:
    if not isinstance(desc, str) or not desc.strip():
        return None
    matches = STAKEHOLDER_RE.findall(desc)
    if not matches:
        return None
    senior_matches = [m.strip() for m in matches if SENIORITY_RE.search(m)]
    if not senior_matches:
        return None
    seen = set()
    result = []
    for s in senior_matches:
        key = s.lower()[:30]
        if key not in seen:
            seen.add(key)
            result.append(s[:60])
    return ' | '.join(result[:3]) if result else None


def apply_intelligence(df: pd.DataFrame) -> pd.DataFrame:
    descs = df['description'].fillna('').astype(str)
    print("  🔍 Extracting: reports_to...")
    df['reports_to']        = descs.apply(extract_reports_to)
    print("  🔍 Extracting: works_with_cxo & cxo_titles_found...")
    cxo_results             = descs.apply(extract_cxo_info)
    df['works_with_cxo']    = cxo_results.apply(lambda x: x[0])
    df['cxo_titles_found']  = cxo_results.apply(lambda x: x[1])
    print("  🔍 Extracting: team_size_managed...")
    df['team_size_managed'] = descs.apply(extract_team_size)
    print("  🔍 Extracting: key_stakeholders...")
    df['key_stakeholders']  = descs.apply(extract_key_stakeholders)
    return df


# ─── MAIN PIPELINE ────────────────────────────────────────────────────────────

def main():
    search_term = "AI Product Manager"
    locations   = ["California", "New York", "Boston", "Seattle", "Texas"]
    hours_old   = 336   # 14 days × 24 hours
    results_wanted = 1000  # Indeed has a practical page limit of ~1000 per search

    all_dfs = []

    for loc in locations:
        print(f"\n🌎 Scraping Indeed — '{search_term}' in {loc} (last 14 days, no cap)...")
        try:
            jobs = scrape_jobs(
                site_name=["indeed"],
                search_term=search_term,
                location=loc,
                results_wanted=results_wanted,
                hours_old=hours_old,
                country_circa="USA",
            )
            print(f"  ✅ Found {len(jobs)} jobs in {loc}")
            if not jobs.empty:
                jobs['search_location'] = loc
                all_dfs.append(jobs)
        except Exception as e:
            print(f"  ❌ Error scraping {loc}: {e}")

        if loc != locations[-1]:
            print("  ⏳ Waiting 6 seconds before next location...")
            time.sleep(6)

    if not all_dfs:
        print("\n⚠️ No jobs found across any location.")
        return

    print("\n📦 Merging all locations...")
    df = pd.concat(all_dfs, ignore_index=True)
    initial = len(df)
    df = df.drop_duplicates(subset=['job_url'])
    print(f"  Unique jobs: {len(df)} (dropped {initial - len(df)} duplicates)")

    print("\n🧠 Running intelligence extraction on descriptions...")
    df = apply_intelligence(df)

    # ── Summary stats ──────────────────────────────────────────────────────────
    total = len(df)
    print(f"\n📊 Extraction Results ({total} total jobs):")
    print(f"  reports_to        : {df['reports_to'].notna().sum()} ({df['reports_to'].notna().sum()/total*100:.1f}%)")
    print(f"  works_with_cxo=Yes: {(df['works_with_cxo']=='Yes').sum()} ({(df['works_with_cxo']=='Yes').sum()/total*100:.1f}%)")
    print(f"  team_size_managed : {df['team_size_managed'].notna().sum()} ({df['team_size_managed'].notna().sum()/total*100:.1f}%)")
    print(f"  key_stakeholders  : {df['key_stakeholders'].notna().sum()} ({df['key_stakeholders'].notna().sum()/total*100:.1f}%)")

    # ── Sample outputs ─────────────────────────────────────────────────────────
    print("\n🔎 Sample 'reports_to':")
    for _, row in df[df['reports_to'].notna()][['title','company','reports_to']].head(5).iterrows():
        print(f"  [{row['title']} @ {row['company']}] → {row['reports_to']}")

    print("\n🔎 Sample 'works_with_cxo = Yes':")
    for _, row in df[df['works_with_cxo']=='Yes'][['title','company','cxo_titles_found']].head(5).iterrows():
        print(f"  [{row['title']} @ {row['company']}] → {row['cxo_titles_found']}")

    # ── Column ordering: put new intel cols right after core identity cols ──────
    priority = ['id','site','job_url','title','company','location','search_location','date_posted','Searched_Role']
    intel    = ['reports_to','works_with_cxo','cxo_titles_found','team_size_managed','key_stakeholders']
    rest     = [c for c in df.columns if c not in priority + intel]
    df = df[[c for c in priority if c in df.columns] + intel + rest]

    # ── Clean for Excel ────────────────────────────────────────────────────────
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).replace({'nan': '', 'None': ''})
        df[col] = df[col].apply(
            lambda x: ''.join(ch for ch in x if ord(ch) > 31 or ord(ch) in [9,10,13])
            if isinstance(x, str) else x
        )

    output = "job_scraper/indeed_jobs/AI_PM_5_Locations_ENRICHED.xlsx"
    df.to_excel(output, index=False)
    print(f"\n💾 Saved enriched file → {output}")
    print("✅ All done!\n")


if __name__ == '__main__':
    main()
