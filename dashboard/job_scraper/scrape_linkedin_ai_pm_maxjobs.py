"""
scrape_linkedin_ai_pm_maxjobs.py
=================================
Breaks LinkedIn's 1000-job-per-search ceiling by running multiple
time-window slices per state in parallel, then deduplicating.

Strategy: 5 states × 3 time windows = 15 total searches
  - Window A: last 3 days  (hours_old=72)   → newest 1000 jobs
  - Window B: last 7 days  (hours_old=168)  → mid-range 1000 jobs
  - Window C: last 14 days (hours_old=336)  → oldest 1000 jobs

After deduplication this yields up to ~3000 unique jobs per state.
Semaphore(3) caps concurrency to avoid LinkedIn ban.
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

# ─── CONCURRENCY: 3 searches at a time across all 15 tasks ───────────────────
MAX_CONCURRENT = 3
semaphore = Semaphore(MAX_CONCURRENT)

# ─── NLP EXTRACTION ───────────────────────────────────────────────────────────

REPORTS_TO_PATTERNS = [
    r'(?:this\s+(?:role|position)\s+)?(?:will\s+)?report(?:ing|s)?\s+(?:directly\s+)?to[\s:,]+([A-Z][^\n.!?,;]{3,60})',
    r'(?:directly\s+)?report(?:ing|s)?\s+to\s+the\s+([A-Z][^\n.!?,;]{3,60})',
    r'position\s+reports?\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
    r'you\s+will\s+report\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
]
REPORTS_TO_RE = re.compile('|'.join(REPORTS_TO_PATTERNS), re.IGNORECASE)
ROLE_WORDS_RE = re.compile(
    r'\b(?:manager|director|president|officer|lead|head|chief|vp|vice\s+president|'
    r'supervisor|executive|owner|partner|founder|ceo|cto|cfo|coo|cmo|cpo|principal|board)\b',
    re.IGNORECASE,
)
CXO_TITLES = [
    'Chief Executive Officer','Chief Technology Officer','Chief Financial Officer',
    'Chief Operating Officer','Chief Marketing Officer','Chief Product Officer',
    'CEO','CTO','CFO','COO','CMO','CPO','CRO','CIO','CISO','CHRO',
    'Co-Founder','Cofounder','Founder','President','Vice President',
    'VP','SVP','EVP','Managing Director','Executive Director','C-Suite','C-Level','CXO',
]
CXO_RE = re.compile(r'\b(' + '|'.join(re.escape(t) for t in CXO_TITLES) + r')\b', re.IGNORECASE)
INTERACTION_TRIGGERS = re.compile(
    r'(?:work(?:ing)?\s+(?:directly\s+)?with|partner(?:ing)?\s+with|'
    r'collaborat(?:e|ing)\s+with|reports?\s+to|reporting\s+to)', re.IGNORECASE,
)
TEAM_SIZE_RE = re.compile(
    r'(?:manag(?:e|es|ing)|lead(?:s|ing)|oversee(?:s|ing)?|supervis(?:e|es|ing)?)\s+'
    r'(?:a\s+team\s+of\s+)?(\d+[\+\-]?(?:\s*(?:to|-)\s*\d+)?)'
    r'\s*(?:person|people|member|engineer|report|staff|employee)', re.IGNORECASE,
)
STAKEHOLDER_RE = re.compile(
    r'(?:work(?:ing)?\s+(?:closely\s+)?with|collaborat(?:e|ing)\s+with|alongside)'
    r'\s+([A-Z][^.\n!?,;]{3,60})', re.IGNORECASE,
)
SENIORITY_RE = re.compile(
    r'\b(director|manager|lead|head|senior|principal|vp|vice\s+president|'
    r'chief|president|exec|owner|founder|board)\b', re.IGNORECASE,
)

def extract_reports_to(desc):
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

def extract_cxo_info(desc):
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

def extract_team_size(desc):
    if not isinstance(desc, str): return None
    m = TEAM_SIZE_RE.search(desc)
    return next((g for g in m.groups() if g), None) if m else None

def extract_stakeholders(desc):
    if not isinstance(desc, str): return None
    matches = STAKEHOLDER_RE.findall(desc)
    senior = [m.strip() for m in matches if SENIORITY_RE.search(m)]
    seen, result = set(), []
    for s in senior:
        k = s.lower()[:30]
        if k not in seen: seen.add(k); result.append(s[:60])
    return ' | '.join(result[:3]) if result else None

def apply_intelligence(df):
    descs = df['description'].fillna('').astype(str)
    df['reports_to']        = descs.apply(extract_reports_to)
    cxo                     = descs.apply(extract_cxo_info)
    df['works_with_cxo']    = cxo.apply(lambda x: x[0])
    df['cxo_titles_found']  = cxo.apply(lambda x: x[1])
    df['team_size_managed'] = descs.apply(extract_team_size)
    df['key_stakeholders']  = descs.apply(extract_stakeholders)
    return df


# ─── PARALLEL SCRAPER WORKER ──────────────────────────────────────────────────

def scrape_task(args):
    """One (location, time_window) scrape task controlled by semaphore."""
    loc, search_term, hours_old, window_label = args
    with semaphore:
        label = f"{loc} [{window_label}]"
        print(f"  🟡 {label}")
        t0 = time.time()
        try:
            jobs = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location=loc,
                results_wanted=1000,
                hours_old=hours_old,
                country_circa="USA",
                linkedin_fetch_description=False,
            )
            elapsed = time.time() - t0
            print(f"  ✅ {label}: {len(jobs)} jobs ({elapsed:.0f}s)")
            if not jobs.empty:
                jobs['search_location'] = loc
                jobs['time_window']     = window_label
            return jobs
        except Exception as e:
            print(f"  ❌ {label} failed: {e}")
            return pd.DataFrame()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    search_term = "AI Product Manager"
    locations   = ["California", "New York", "Boston", "Seattle", "Texas"]

    # 3 time windows per state to break the 1000-job ceiling
    time_windows = [
        (72,  "Last 3 days"),
        (168, "Last 7 days"),
        (336, "Last 14 days"),
    ]

    total_tasks = len(locations) * len(time_windows)
    print(f"🚀 Max-Coverage LinkedIn Scrape — '{search_term}'")
    print(f"   {len(locations)} states × {len(time_windows)} time windows = {total_tasks} searches")
    print(f"   Concurrency: {MAX_CONCURRENT} simultaneous | Target: up to 3000/state\n")

    # Build all task args
    task_args = [
        (loc, search_term, hours_old, label)
        for loc in locations
        for hours_old, label in time_windows
    ]

    t_start = time.time()
    all_dfs = []

    with ThreadPoolExecutor(max_workers=total_tasks) as executor:
        futures = [executor.submit(scrape_task, args) for args in task_args]
        for future in as_completed(futures):
            df = future.result()
            if df is not None and not df.empty:
                all_dfs.append(df)

    print(f"\n📦 Merging {len(all_dfs)} result sets...")
    if not all_dfs:
        print("⚠️ No data collected."); return

    merged = pd.concat(all_dfs, ignore_index=True)
    raw_total = len(merged)
    merged = merged.drop_duplicates(subset=['job_url'])
    print(f"  Raw rows   : {raw_total}")
    print(f"  Unique jobs: {len(merged)} (dropped {raw_total - len(merged)} duplicates)")

    print("\n📊 Unique jobs per state:")
    for loc, count in merged['search_location'].value_counts().items():
        print(f"  {loc:<15}: {count:>5}")

    print("\n🧠 Running NLP intelligence extraction...")
    merged = apply_intelligence(merged)

    total = len(merged)
    print(f"\n📊 Intelligence results ({total} jobs):")
    print(f"  reports_to        : {merged['reports_to'].notna().sum()} ({merged['reports_to'].notna().sum()/total*100:.1f}%)")
    print(f"  works_with_cxo=Yes: {(merged['works_with_cxo']=='Yes').sum()} ({(merged['works_with_cxo']=='Yes').sum()/total*100:.1f}%)")
    print(f"  key_stakeholders  : {merged['key_stakeholders'].notna().sum()} ({merged['key_stakeholders'].notna().sum()/total*100:.1f}%)")

    # Column ordering
    priority = ['id','site','job_url','title','company','location','search_location','time_window','date_posted']
    intel    = ['reports_to','works_with_cxo','cxo_titles_found','team_size_managed','key_stakeholders']
    rest     = [c for c in merged.columns if c not in priority + intel]
    merged   = merged[[c for c in priority if c in merged.columns] + intel + rest]

    # Excel-safe strings
    for col in merged.select_dtypes(include=['object']).columns:
        merged[col] = merged[col].astype(str).replace({'nan':'','None':''})
        merged[col] = merged[col].apply(
            lambda x: ''.join(ch for ch in x if ord(ch)>31 or ord(ch) in [9,10,13]) if isinstance(x,str) else x
        )

    output = "job_scraper/linkedin_jobs/AI_PM_5_Locations_MAXJOBS_LinkedIn.xlsx"
    merged.to_excel(output, index=False)

    elapsed = time.time() - t_start
    print(f"\n💾 Saved → {output}")
    print(f"⏱️  Total time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print("✅ Done!\n")


if __name__ == '__main__':
    main()
