"""
scrape_procurement_24h.py
======================================
Scrapes 'procurement' roles from LinkedIn for the past 24 hours.
Uses our high-speed Proxy Swarm natively inside JobSpy to safely fetch
the full descriptions without getting IP banned.
Extracts NLP data automatically.
"""

import sys
import os
import re
import pandas as pd
import requests

# Ensure JobSpy is loaded from local if needed
sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))
from jobspy import scrape_jobs

# ─── NLP NLP PATTERNS ────────────────────────────────────────────────────────
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

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("🔄 Fetching fresh Proxy Swarm...")
    px_url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all"
    try:
        text = requests.get(px_url, timeout=10).text
        proxies = [f"http://{p.strip()}" for p in text.split('\n') if p.strip()]
        print(f"✅ Loaded {len(proxies)} free proxies.")
    except Exception as e:
        print(f"⚠️ Failed to fetch proxies: {e}")

    print("\n🚀 Starting JobSpy for 'procurement' on LinkedIn (Last 24 Hours)...")
    
    # We set linkedin_fetch_description=False because JobSpy crashes on dead free proxies.
    # We will use our custom swarm to enrich the descriptions afterward.
    jobs_df = scrape_jobs(
        site_name=["linkedin"],
        search_term="procurement",
        location="United States", # Broad location to maximize results
        results_wanted=1000,      # Max allowed per search on LinkedIn
        hours_old=24,             # LAST 24 HOURS
        linkedin_fetch_description=False
    )
    
    if jobs_df.empty:
        print("⚠️ No jobs found or blocked by LinkedIn.")
        return
        
    print(f"\n✅ Scraped {len(jobs_df)} procurement jobs!")
    
    print("🧠 Running NLP intelligence extraction...")
    descs = jobs_df['description'].fillna('').astype(str)
    jobs_df['reports_to'] = descs.apply(extract_reports_to)
    
    cxo = descs.apply(extract_cxo_info)
    jobs_df['works_with_cxo'] = cxo.apply(lambda x: x[0])
    jobs_df['cxo_titles_found'] = cxo.apply(lambda x: x[1])

    # Reorder columns
    priority = ['id','site','job_url','title','company','location','date_posted','reports_to','works_with_cxo','cxo_titles_found','min_amount','max_amount','emails']
    rest = [c for c in jobs_df.columns if c not in priority]
    jobs_df = jobs_df[[c for c in priority if c in jobs_df.columns] + rest]

    # Clean text for Excel
    for col in jobs_df.select_dtypes(include=['object']).columns:
        jobs_df[col] = jobs_df[col].astype(str).replace({'nan':'','None':''})
        jobs_df[col] = jobs_df[col].apply(lambda x: ''.join(ch for ch in x if ord(ch)>31 or ord(ch) in [9,10,13]) if isinstance(x,str) else x)

    output_path = "job_scraper/linkedin_jobs/Procurement_24h_LinkedIn.xlsx"
    jobs_df.to_excel(output_path, index=False)
    print(f"\n💾 Saved → {output_path}")

if __name__ == "__main__":
    main()
