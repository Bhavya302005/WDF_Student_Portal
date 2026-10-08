"""
enrich_linkedin_descriptions_proxy.py
======================================
HIGH-SPEED ELITE PROXY ROTATION
Bypasses LinkedIn IP bans by leveraging hundreds of free proxies simultaneously.
Features:
- Auto-fetches fresh proxy lists from ProxyScrape
- Concurrency: 100 simultaneous requests
- Extremely fast timeout/fail-fast logic (ignores bad proxies)
- Fully asynchronous
- Expected time for 7k jobs: 5-10 minutes
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
from typing import Optional, List

INPUT_FILE  = "job_scraper/linkedin_jobs/AI_PM_5_Locations_MAXJOBS_LinkedIn.xlsx"
OUTPUT_FILE = "job_scraper/linkedin_jobs/AI_PM_5_Locations_MAXJOBS_ENRICHED.xlsx"

CONCURRENCY  = 100
TIMEOUT_SECS = 6
MAX_RETRIES  = 10   # High retries because free proxies often fail
BATCH_SIZE   = 500

VIEW_API = "https://www.linkedin.com/jobs/view/{job_id}"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

# ─── NLP PATTERNS ─────────────────────────────────────────────────────────────
REPORTS_TO_RE = re.compile(
    r'(?:this\s+(?:role|position)\s+)?(?:will\s+)?report(?:ing|s)?\s+(?:directly\s+)?to[\s:,]+([A-Z][^\n.!?,;]{3,60})'
    r'|(?:directly\s+)?report(?:ing|s)?\s+to\s+the\s+([A-Z][^\n.!?,;]{3,60})'
    r'|position\s+reports?\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})'
    r'|you\s+will\s+report\s+to[\s:]+([A-Z][^\n.!?,;]{3,60})', re.IGNORECASE)
ROLE_WORDS_RE = re.compile(
    r'\b(?:manager|director|president|officer|lead|head|chief|vp|vice\s+president|'
    r'supervisor|executive|owner|partner|founder|ceo|cto|cfo|coo|cmo|cpo|principal|board)\b', re.IGNORECASE)
CXO_TITLES = ['Chief Executive Officer','Chief Technology Officer','Chief Financial Officer',
    'Chief Operating Officer','Chief Marketing Officer','Chief Product Officer',
    'CEO','CTO','CFO','COO','CMO','CPO','CRO','CIO','CISO','CHRO',
    'Co-Founder','Cofounder','Founder','President','Vice President',
    'VP','SVP','EVP','Managing Director','Executive Director','C-Suite','C-Level','CXO']
CXO_RE = re.compile(r'\b(' + '|'.join(re.escape(t) for t in CXO_TITLES) + r')\b', re.IGNORECASE)
INTERACTION_TRIGGERS = re.compile(
    r'(?:work(?:ing)?\s+(?:directly\s+)?with|partner(?:ing)?\s+with|'
    r'collaborat(?:e|ing)\s+with|reports?\s+to|reporting\s+to)', re.IGNORECASE)
TEAM_SIZE_RE = re.compile(
    r'(?:manag(?:e|es|ing)|lead(?:s|ing)|oversee(?:s|ing)?|supervis(?:e|es|ing)?)\s+'
    r'(?:a\s+team\s+of\s+)?(\d+[\+\-]?(?:\s*(?:to|-)\s*\d+)?)'
    r'\s*(?:person|people|member|engineer|report|staff|employee)', re.IGNORECASE)
STAKEHOLDER_RE = re.compile(
    r'(?:work(?:ing)?\s+(?:closely\s+)?with|collaborat(?:e|ing)\s+with|alongside)'
    r'\s+([A-Z][^.\n!?,;]{3,60})', re.IGNORECASE)
SENIORITY_RE = re.compile(
    r'\b(director|manager|lead|head|senior|principal|vp|vice\s+president|'
    r'chief|president|exec|owner|founder|board)\b', re.IGNORECASE)

def extract_reports_to(desc):
    if not isinstance(desc, str) or not desc.strip(): return None
    m = REPORTS_TO_RE.search(desc)
    if not m: return None
    c = next((g for g in m.groups() if g), None)
    if not c: return None
    c = re.sub(r'\s+', ' ', re.sub(r'\*+|\n', ' ', c).strip())
    c = re.split(r'\s+(?:and|or|while|who|with|as|to)\s+', c, 1)[0]
    c = re.split(r'[,;:|]', c)[0].strip()
    if len(c) > 65: c = c[:65].rsplit(' ', 1)[0]
    return c.strip() if ROLE_WORDS_RE.search(c) and len(c) > 3 else None

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

# ─── ASYNC PROXY FETCHER ──────────────────────────────────────────────────────

async def get_proxies() -> List[str]:
    print("🔄 Fetching fresh proxy pool...")
    url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            text = await resp.text()
            pxs = [f"http://{p.strip()}" for p in text.split('\n') if p.strip()]
            print(f"✅ Loaded {len(pxs)} free proxies.")
            return pxs

def parse_description(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, 'html.parser')
    div = soup.find('div', class_='description__text') or soup.find('div', class_='show-more-less-html__markup')
    if div: return div.get_text(separator=' ', strip=True)
    all_divs = soup.find_all('div')
    if all_divs:
        biggest = max(all_divs, key=lambda d: len(d.get_text()))
        text = biggest.get_text(separator=' ', strip=True)
        return text if len(text) > 100 else None
    return None

async def fetch_description(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                            job_id: str, proxies: List[str], idx: int, total: int, stats: dict) -> tuple[str, Optional[str]]:
    url = VIEW_API.format(job_id=job_id)
    
    for attempt in range(MAX_RETRIES):
        proxy = random.choice(proxies)
        try:
            async with sem:
                headers = get_random_headers()
                async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECS)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        desc = parse_description(html)
                        stats['success'] += 1
                        if stats['success'] % 100 == 0:
                            pct = stats['success'] / total * 100
                            print(f"  📥 {stats['success']}/{total} ({pct:.0f}%) — latest: {job_id}", flush=True)
                        return job_id, desc
                    elif resp.status == 429:
                        # Proxy is rate limited by LinkedIn, immediately try another one
                        continue
        except Exception:
            # Free proxies die instantly, just ignore and retry
            pass
            
    stats['failed'] += 1
    return job_id, None

async def fetch_all_descriptions(job_ids: list[str]) -> dict[str, Optional[str]]:
    proxies = await get_proxies()
    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY + 20, ssl=False) # Disable SSL verification for shady proxies
    
    stats = {'success': 0, 'failed': 0}
    results = {}

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_description(session, sem, jid, proxies, idx, len(job_ids), stats) for idx, jid in enumerate(job_ids, 1)]
        completed = await asyncio.gather(*tasks)
        for job_id, desc in completed:
            results[job_id] = desc

    print(f"\nFinal Stats: Success={stats['success']} | Failed={stats['failed']}")
    return results

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    df = pd.read_excel(INPUT_FILE)
    
    # ─── STRICT FILTERING ───────────────────────────────────────────────────
    print(f"\n🔍 Applying strict filter for 'AI Product Manager' (Original: {len(df)} jobs)")
    # We allow some slight variations like "A.I. Product Manager" or "Artificial Intelligence Product Manager"
    # But for strictness, we will look for exact matches of "AI Product Manager" or "Artificial Intelligence Product Manager"
    pattern = r'\b(ai|artificial intelligence)\s+product\s+manager\b'
    strict_mask = df['title'].astype(str).str.contains(pattern, flags=re.IGNORECASE, regex=True)
    df = df[strict_mask].copy()
    print(f"   Strict match: {len(df)} jobs remain.")
    
    df['_job_id'] = df['job_url'].apply(lambda u: re.search(r'/jobs/view/(\d+)', str(u)).group(1) if pd.notna(u) and re.search(r'/jobs/view/(\d+)', str(u)) else None)
    job_ids = df[df['_job_id'].notna()]['_job_id'].tolist()
    
    print(f"\n🚀 PROXY SWARM ACTIVATED: Fetching {len(job_ids)} descriptions")
    print(f"   Concurrency: {CONCURRENCY} workers | Auto-rotating free proxy pool")
    t0 = time.time()
    
    desc_map = asyncio.run(fetch_all_descriptions(job_ids))
    elapsed = time.time() - t0
    
    df['description'] = df['_job_id'].map(desc_map)
    df = df.drop(columns=['_job_id'])

    print("\n🧠 Running NLP intelligence extraction...")
    descs = df['description'].fillna('').astype(str)
    df['reports_to'] = descs.apply(extract_reports_to)
    cxo = descs.apply(extract_cxo_info)
    df['works_with_cxo'] = cxo.apply(lambda x: x[0])
    df['cxo_titles_found'] = cxo.apply(lambda x: x[1])
    df['team_size_managed'] = descs.apply(extract_team_size)
    df['key_stakeholders'] = descs.apply(extract_stakeholders)

    # Column ordering & save
    priority = ['id','site','job_url','title','company','location','search_location','time_window','date_posted']
    intel = ['reports_to','works_with_cxo','cxo_titles_found','team_size_managed','key_stakeholders']
    rest = [c for c in df.columns if c not in priority + intel]
    df = df[[c for c in priority if c in df.columns] + intel + rest]

    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).replace({'nan':'','None':''})
        df[col] = df[col].apply(lambda x: ''.join(ch for ch in x if ord(ch)>31 or ord(ch) in [9,10,13]) if isinstance(x,str) else x)

    df.to_excel(OUTPUT_FILE, index=False)
    print(f"\n💾 Saved → {OUTPUT_FILE}")
    print(f"⏱️  Total time: {elapsed/60:.1f} min")
    print("✅ Done!\n")

if __name__ == '__main__':
    main()
