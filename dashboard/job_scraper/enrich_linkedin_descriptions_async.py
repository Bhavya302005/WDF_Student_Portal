"""
enrich_linkedin_descriptions_async.py
=======================================
Fetches job descriptions for all LinkedIn jobs using an async aiohttp pipeline
that hits LinkedIn's internal guest API endpoint directly.

Speed design:
  - asyncio.Semaphore(15) → 15 concurrent HTTP requests at all times
  - Connection pooling via aiohttp.TCPConnector
  - Exponential backoff on 429 rate-limit responses
  - Processes 7000+ jobs in ~5-8 minutes vs hours with sequential requests

Then runs full NLP extraction:
  - reports_to, works_with_cxo, cxo_titles_found, team_size_managed, key_stakeholders
"""

import sys
import os
import re
import asyncio
import aiohttp
import random
import time
import pandas as pd
from bs4 import BeautifulSoup
from typing import Optional

# ─── CONFIG ───────────────────────────────────────────────────────────────────
INPUT_FILE  = "job_scraper/linkedin_jobs/AI_PM_5_Locations_MAXJOBS_LinkedIn.xlsx"
OUTPUT_FILE = "job_scraper/linkedin_jobs/AI_PM_5_Locations_MAXJOBS_ENRICHED.xlsx"

CONCURRENCY  = 2        # low concurrency for Jina API free tier
TIMEOUT_SECS = 12       # per-request timeout
MAX_RETRIES  = 3        # retries on 429 / network error
BASE_BACKOFF = 3.0      # seconds before first retry (doubles each attempt)
BATCH_SIZE   = 500      # save progress checkpoint every N jobs
# ─────────────────────────────────────────────────────────────────────────────

# ─── ELITE WAF BYPASS: JINA READER PROXY ───────────────────────────────────────

VIEW_API = "https://r.jina.ai/https://www.linkedin.com/jobs/view/{job_id}"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
]

def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

# ─── NLP PATTERNS ─────────────────────────────────────────────────────────────

REPORTS_TO_RE = re.compile(
    r'(?:this\s+(?:role|position)\s+)?(?:will\s+)?report(?:ing|s)?\s+(?:directly\s+)?to[\s:,]+([A-Z][^\n.!?,;]{3,60})'
    r'|(?:directly\s+)?report(?:ing|s)?\s+to\s+the\s+([A-Z][^\n.!?,;]{3,60})'
    r'|position\s+reports?\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})'
    r'|you\s+will\s+report\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})',
    re.IGNORECASE,
)
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

# ─── NLP FUNCTIONS ────────────────────────────────────────────────────────────

def extract_reports_to(desc: str) -> Optional[str]:
    if not isinstance(desc, str) or not desc.strip(): return None
    m = REPORTS_TO_RE.search(desc)
    if not m: return None
    captured = next((g for g in m.groups() if g), None)
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
    return next((g for g in m.groups() if g), None) if m else None

def extract_stakeholders(desc: str) -> Optional[str]:
    if not isinstance(desc, str): return None
    matches = STAKEHOLDER_RE.findall(desc)
    senior = [m.strip() for m in matches if SENIORITY_RE.search(m)]
    seen, result = set(), []
    for s in senior:
        k = s.lower()[:30]
        if k not in seen: seen.add(k); result.append(s[:60])
    return ' | '.join(result[:3]) if result else None


# ─── ASYNC DESCRIPTION FETCHER ────────────────────────────────────────────────

def extract_job_id(job_url: str) -> Optional[str]:
    """Extract numeric job ID from LinkedIn URL."""
    m = re.search(r'/jobs/view/(\d+)', str(job_url))
    return m.group(1) if m else None

def parse_description(text: str) -> Optional[str]:
    """Extract plain text from Jina Reader markdown response."""
    if not text: return None
    # Jina returns raw markdown, we can just return it directly for the regex to parse!
    return text if len(text) > 100 else None

async def fetch_description(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                             job_id: str, idx: int, total: int) -> tuple[str, Optional[str]]:
    """Fetch one job description with retry + exponential backoff."""
    url = VIEW_API.format(job_id=job_id)
    
    for attempt in range(MAX_RETRIES):
        try:
            async with sem:
                await asyncio.sleep(random.uniform(2.0, 4.0))  # Stay below Jina's rate limits
                headers = get_random_headers()
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECS)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        desc = parse_description(html)
                        if idx % 200 == 0:
                            pct = idx / total * 100
                            print(f"  📥 {idx}/{total} ({pct:.0f}%) — latest: {job_id}", flush=True)
                        return job_id, desc
                    elif resp.status == 429:
                        wait = BASE_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                        print(f"  ⚠️  Rate limited on {job_id}, waiting {wait:.1f}s...")
                        await asyncio.sleep(wait)
                    else:
                        return job_id, None
        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BASE_BACKOFF * (attempt + 1))
        except Exception:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BASE_BACKOFF)
    
    return job_id, None


async def fetch_all_descriptions(job_ids: list[str]) -> dict[str, Optional[str]]:
    """Fire all requests with controlled concurrency via asyncio.Semaphore."""
    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY + 5, ttl_dns_cache=300)
    total = len(job_ids)
    results = {}

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            fetch_description(session, sem, job_id, idx, total)
            for idx, job_id in enumerate(job_ids, 1)
        ]
        completed = await asyncio.gather(*tasks, return_exceptions=False)
        for job_id, desc in completed:
            results[job_id] = desc

    return results


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n📂 Loading: {INPUT_FILE}")
    df = pd.read_excel(INPUT_FILE)
    print(f"   {len(df)} jobs loaded")

    # Extract job IDs
    df['_job_id'] = df['job_url'].apply(extract_job_id)
    valid = df[df['_job_id'].notna()]
    invalid = df[df['_job_id'].isna()]
    print(f"   Valid job IDs: {len(valid)} | Skipped (no ID): {len(invalid)}")

    job_ids = valid['_job_id'].tolist()
    
    print(f"\n🚀 Fetching {len(job_ids)} descriptions via async aiohttp")
    print(f"   Concurrency: {CONCURRENCY} | Timeout: {TIMEOUT_SECS}s | Retries: {MAX_RETRIES}")
    t0 = time.time()
    
    desc_map = asyncio.run(fetch_all_descriptions(job_ids))
    
    elapsed = time.time() - t0
    fetched = sum(1 for v in desc_map.values() if v)
    print(f"\n✅ Fetched {fetched}/{len(job_ids)} descriptions in {elapsed:.0f}s ({elapsed/60:.1f} min)")

    # Map descriptions back to dataframe
    df['description'] = df['_job_id'].map(desc_map)
    df = df.drop(columns=['_job_id'])

    # ── NLP Extraction ────────────────────────────────────────────────────────
    print("\n🧠 Running NLP intelligence extraction...")
    descs = df['description'].fillna('').astype(str)
    
    df['reports_to']        = descs.apply(extract_reports_to)
    cxo                     = descs.apply(extract_cxo_info)
    df['works_with_cxo']    = cxo.apply(lambda x: x[0])
    df['cxo_titles_found']  = cxo.apply(lambda x: x[1])
    df['team_size_managed'] = descs.apply(extract_team_size)
    df['key_stakeholders']  = descs.apply(extract_stakeholders)

    total = len(df)
    print(f"\n📊 Intelligence Extraction ({total} jobs):")
    print(f"  reports_to        : {df['reports_to'].notna().sum()} ({df['reports_to'].notna().sum()/total*100:.1f}%)")
    print(f"  works_with_cxo=Yes: {(df['works_with_cxo']=='Yes').sum()} ({(df['works_with_cxo']=='Yes').sum()/total*100:.1f}%)")
    print(f"  team_size_managed : {df['team_size_managed'].notna().sum()} ({df['team_size_managed'].notna().sum()/total*100:.1f}%)")
    print(f"  key_stakeholders  : {df['key_stakeholders'].notna().sum()} ({df['key_stakeholders'].notna().sum()/total*100:.1f}%)")

    print("\n🔎 Sample 'reports_to':")
    for _, row in df[df['reports_to'].notna()][['title','company','reports_to']].head(5).iterrows():
        print(f"  [{row['title']} @ {row['company']}] → {row['reports_to']}")

    print("\n🔎 Sample 'works_with_cxo=Yes':")
    for _, row in df[df['works_with_cxo']=='Yes'][['title','company','cxo_titles_found']].head(5).iterrows():
        print(f"  [{row['title']} @ {row['company']}] → {row['cxo_titles_found']}")

    # Column ordering
    priority = ['id','site','job_url','title','company','location','search_location',
                'time_window','date_posted']
    intel    = ['reports_to','works_with_cxo','cxo_titles_found','team_size_managed','key_stakeholders']
    rest     = [c for c in df.columns if c not in priority + intel]
    df = df[[c for c in priority if c in df.columns] + intel + rest]

    # Excel clean
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).replace({'nan':'','None':''})
        df[col] = df[col].apply(
            lambda x: ''.join(ch for ch in x if ord(ch)>31 or ord(ch) in [9,10,13]) if isinstance(x,str) else x
        )

    df.to_excel(OUTPUT_FILE, index=False)
    total_time = time.time() - t0 + elapsed
    print(f"\n💾 Saved → {OUTPUT_FILE}")
    print(f"⏱️  Total pipeline time: {(time.time()-t0)/60:.1f} min")
    print("✅ Done!\n")


if __name__ == '__main__':
    main()
