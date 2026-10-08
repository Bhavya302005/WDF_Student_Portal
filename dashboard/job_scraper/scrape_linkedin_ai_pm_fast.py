"""
scrape_linkedin_ai_pm_fast.py
==============================
Scrapes LinkedIn for "AI Product Manager" across 5 US states in PARALLEL
using ThreadPoolExecutor + Semaphore to maximize speed while staying
within LinkedIn's guest rate limits.

Key speed decisions:
  - linkedin_fetch_description=False  → skips 1 HTTP req per job (10-50x faster)
  - Parallel location searches        → all 5 states run concurrently (max 2 at a time)
  - Semaphore(2)                      → caps concurrent workers to avoid ban
  - 14-day window, no job cap         → up to ~1000 per state (LinkedIn's hard ceiling)
"""

import sys
import os
import re
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from typing import Optional

sys.path.insert(0, os.path.abspath('job_scraper/JobSpy-main'))
from jobspy import scrape_jobs

# ─── CONCURRENCY CONTROL ──────────────────────────────────────────────────────
# LinkedIn will ban us if we fire 5 simultaneous searches.
# Semaphore(2) = max 2 location searches running at the same exact time.
MAX_CONCURRENT = 2
semaphore = Semaphore(MAX_CONCURRENT)

# ─── NLP EXTRACTION (runs on LinkedIn's listing-card text) ───────────────────

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
    r'principal|coordinator|engineer|analyst|architect|team|board)\b', re.IGNORECASE,
)
CXO_TITLES = [
    'Chief Executive Officer','Chief Technology Officer','Chief Financial Officer',
    'Chief Operating Officer','Chief Marketing Officer','Chief Product Officer',
    'Chief Revenue Officer','Chief Information Officer','Chief People Officer',
    'CEO','CTO','CFO','COO','CMO','CPO','CRO','CIO','CISO','CHRO','CDO',
    'Co-Founder','Cofounder','Founder',
    'President','Vice President','VP','SVP','EVP',
    'Managing Director','Executive Director','C-Suite','C-Level','CXO',
]
CXO_RE = re.compile(
    r'\b(' + '|'.join(re.escape(t) for t in CXO_TITLES) + r')\b', re.IGNORECASE,
)
INTERACTION_TRIGGERS = re.compile(
    r'(?:work(?:ing)?\s+(?:directly\s+)?with|partner(?:ing)?\s+with|'
    r'collaborat(?:e|ing)\s+with|reports?\s+to|reporting\s+to)', re.IGNORECASE,
)
TEAM_SIZE_RE = re.compile(
    r'(?:manag(?:e|es|ing)|lead(?:s|ing)|oversee(?:s|ing)?|supervis(?:e|es|ing)?)\s+'
    r'(?:a\s+team\s+of\s+)?(\d+[\+\-]?(?:\s*(?:to|-)\s*\d+)?)'
    r'\s*(?:person|people|member|engineer|report|staff|employee)',
    re.IGNORECASE,
)
STAKEHOLDER_RE = re.compile(
    r'(?:work(?:ing)?\s+(?:closely\s+)?with|collaborat(?:e|ing)\s+with|alongside)'
    r'\s+([A-Z][^.\n!?,;]{3,60})', re.IGNORECASE,
)
SENIORITY_RE = re.compile(
    r'\b(director|manager|lead|head|senior|principal|vp|vice\s+president|'
    r'chief|president|exec|owner|founder|board)\b', re.IGNORECASE,
)


def extract_reports_to(desc: str) -> Optional[str]:
    if not isinstance(desc, str) or not desc.strip(): return None
    match = REPORTS_TO_RE.search(desc)
    if not match: return None
    captured = next((g for g in match.groups() if g), None)
    if not captured: return None
    captured = re.sub(r'\*+|\n', ' ', captured).strip()
    captured = re.sub(r'\s+', ' ', captured)
    captured = re.split(r'\s+(?:and|or|while|who|with|as|to)\s+', captured, 1)[0]
    captured = re.split(r'[,;:|]', captured)[0].strip()
    if len(captured) > 65: captured = captured[:65].rsplit(' ', 1)[0]
    return captured.strip() if ROLE_WORDS_RE.search(captured) and len(captured) > 3 else None


def extract_cxo_info(desc: str) -> tuple:
    if not isinstance(desc, str) or not desc.strip(): return 'Not Mentioned', ''
    direct = set()
    for sentence in re.split(r'[.\n!?]', desc):
        titles = CXO_RE.findall(sentence)
        if titles and INTERACTION_TRIGGERS.search(sentence):
            norm = {'vp':'VP','svp':'SVP','evp':'EVP','ceo':'CEO','cto':'CTO','cfo':'CFO',
                    'coo':'COO','cmo':'CMO','cpo':'CPO','cro':'CRO','cio':'CIO','cxo':'CXO'}
            for t in titles: direct.add(norm.get(t.lower(), t))
    if direct: return 'Yes', ', '.join(sorted(direct))
    if CXO_RE.search(desc): return 'No', ''
    return 'Not Mentioned', ''


def extract_team_size(desc: str) -> Optional[str]:
    if not isinstance(desc, str): return None
    m = TEAM_SIZE_RE.search(desc)
    if not m: return None
    return next((g for g in m.groups() if g), None)


def extract_stakeholders(desc: str) -> Optional[str]:
    if not isinstance(desc, str): return None
    matches = STAKEHOLDER_RE.findall(desc)
    senior = [m.strip() for m in matches if SENIORITY_RE.search(m)]
    seen, result = set(), []
    for s in senior:
        k = s.lower()[:30]
        if k not in seen:
            seen.add(k); result.append(s[:60])
    return ' | '.join(result[:3]) if result else None


def apply_intelligence(df: pd.DataFrame) -> pd.DataFrame:
    descs = df['description'].fillna('').astype(str)
    df['reports_to']        = descs.apply(extract_reports_to)
    cxo                     = descs.apply(extract_cxo_info)
    df['works_with_cxo']    = cxo.apply(lambda x: x[0])
    df['cxo_titles_found']  = cxo.apply(lambda x: x[1])
    df['team_size_managed'] = descs.apply(extract_team_size)
    df['key_stakeholders']  = descs.apply(extract_stakeholders)
    return df


# ─── PARALLEL SCRAPER ─────────────────────────────────────────────────────────

def scrape_location(args: tuple) -> tuple[str, pd.DataFrame]:
    """Scrapes one location. Uses semaphore to cap concurrency."""
    loc, search_term, hours_old = args
    with semaphore:  # Only MAX_CONCURRENT locations active at once
        print(f"  🟡 Starting: {loc}...")
        t0 = time.time()
        try:
            jobs = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location=loc,
                results_wanted=1000,       # LinkedIn's practical ceiling per search
                hours_old=hours_old,
                country_circa="USA",
                linkedin_fetch_description=False,  # ← KEY: no per-job HTTP call = 50x faster
            )
            elapsed = time.time() - t0
            print(f"  ✅ {loc}: {len(jobs)} jobs in {elapsed:.0f}s")
            return loc, jobs
        except Exception as e:
            print(f"  ❌ {loc} failed: {e}")
            return loc, pd.DataFrame()


def main():
    search_term = "AI Product Manager"
    locations   = ["California", "New York", "Boston", "Seattle", "Texas"]
    hours_old   = 336  # 14 days

    print(f"🚀 Parallel LinkedIn scrape — '{search_term}' across {len(locations)} states")
    print(f"   Concurrency: {MAX_CONCURRENT} simultaneous searches | Window: 14 days\n")

    t_start = time.time()
    all_dfs = []

    # Fire all 5 location scrapes — semaphore ensures only 2 run at the same time
    args_list = [(loc, search_term, hours_old) for loc in locations]
    with ThreadPoolExecutor(max_workers=len(locations)) as executor:
        futures = {executor.submit(scrape_location, args): args[0] for args in args_list}
        for future in as_completed(futures):
            loc, df = future.result()
            if not df.empty:
                df['search_location'] = loc
                all_dfs.append(df)

    print(f"\n📦 Merging results...")
    if not all_dfs:
        print("⚠️ No data collected.")
        return

    merged = pd.concat(all_dfs, ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=['job_url'])
    print(f"  Total unique jobs: {len(merged)} (dropped {before - len(merged)} duplicates)")

    # Location breakdown
    print("\n📊 Jobs per location:")
    for loc, count in merged['search_location'].value_counts().items():
        print(f"  {loc:<15}: {count}")

    print("\n🧠 Running NLP intelligence extraction...")
    merged = apply_intelligence(merged)

    total = len(merged)
    print(f"\n📊 Intelligence Extraction (of {total} jobs):")
    print(f"  reports_to        : {merged['reports_to'].notna().sum()} ({merged['reports_to'].notna().sum()/total*100:.1f}%)")
    print(f"  works_with_cxo=Yes: {(merged['works_with_cxo']=='Yes').sum()} ({(merged['works_with_cxo']=='Yes').sum()/total*100:.1f}%)")
    print(f"  key_stakeholders  : {merged['key_stakeholders'].notna().sum()} ({merged['key_stakeholders'].notna().sum()/total*100:.1f}%)")

    # Reorder columns
    priority = ['id','site','job_url','title','company','location','search_location','date_posted']
    intel    = ['reports_to','works_with_cxo','cxo_titles_found','team_size_managed','key_stakeholders']
    rest     = [c for c in merged.columns if c not in priority + intel]
    merged   = merged[[c for c in priority if c in merged.columns] + intel + rest]

    # Clean for Excel
    for col in merged.select_dtypes(include=['object']).columns:
        merged[col] = merged[col].astype(str).replace({'nan':'','None':''})
        merged[col] = merged[col].apply(
            lambda x: ''.join(ch for ch in x if ord(ch)>31 or ord(ch) in [9,10,13]) if isinstance(x,str) else x
        )

    output = "job_scraper/linkedin_jobs/AI_PM_5_Locations_LinkedIn_ENRICHED.xlsx"
    merged.to_excel(output, index=False)

    elapsed_total = time.time() - t_start
    print(f"\n💾 Saved → {output}")
    print(f"⏱️  Total time: {elapsed_total:.0f} seconds ({elapsed_total/60:.1f} minutes)")
    print("✅ Done!\n")


if __name__ == '__main__':
    main()
