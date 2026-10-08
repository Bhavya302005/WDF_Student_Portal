import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import concurrent.futures
import os
import re
from typing import List, Dict

# User settings
KEYWORD = "procurement" # Change this for other jobs
MAX_JOBS = 50 
OUTPUT_FILE = "job_scraper/Naukri_Jobs_Jina.xlsx"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def get_job_sitemaps() -> List[str]:
    print("Fetching Sitemap Index from Naukri...")
    try:
        res = requests.get('https://www.naukri.com/sitemap/sitemap.xml', headers=HEADERS, timeout=10)
        soup = BeautifulSoup(res.text, 'xml')
        return [loc.text for loc in soup.find_all('loc') if 'jobDescPages' in loc.text]
    except Exception as e:
        print(f"Error fetching sitemap index: {e}")
        return []

def get_job_urls_from_sitemap(sitemap_url: str, keyword: str) -> List[str]:
    print(f"Parsing sitemap: {sitemap_url.split('/')[-1]}")
    try:
        res = requests.get(sitemap_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, 'xml')
        urls = [loc.text for loc in soup.find_all('loc')]
        # Filter by keyword
        return [u for u in urls if keyword.lower() in u.lower()]
    except Exception as e:
        print(f"Error fetching {sitemap_url}: {e}")
        return []

def parse_jina_job(url: str) -> Dict:
    jina_url = f"https://r.jina.ai/{url}"
    try:
        res = requests.get(jina_url, timeout=20)
        if res.status_code != 200:
            return None
        text = res.text
        
        # Jina perfectly captures the Title, Company, and Experience in the first line!
        title_line = "Unknown"
        for line in text.split('\n'):
            if line.startswith('Title:'):
                title_line = line.replace('Title:', '').strip()
                break
                
        # Extract Posted Time
        posted_time = "Unknown"
        match = re.search(r'Posted:\s*(.*?)\s*(Openings|Applicants|Job|Employment)', text, re.IGNORECASE)
        if match:
            posted_time = match.group(1).strip()
        
        return {
            "Job Title/Company Details": title_line,
            "Posted Time": posted_time,
            "Job URL": url,
            "Raw Markdown": text[:2500] # Save enough of the JD
        }
    except Exception as e:
        print(f"Error extracting {url}: {e}")
        return None

def main():
    sitemaps = get_job_sitemaps()
    print(f"Found {len(sitemaps)} Naukri Job Sitemaps.")
    
    all_job_urls = []
    # We parse the first 5 sitemaps to get a good spread (they are geographically or temporally split)
    for sm in sitemaps[:5]:
        urls = get_job_urls_from_sitemap(sm, KEYWORD)
        all_job_urls.extend(urls)
        print(f" -> Found {len(urls)} '{KEYWORD}' jobs in this sitemap.")
        if len(all_job_urls) >= MAX_JOBS * 2: 
            break
            
    print(f"\nTotal matching URLs found: {len(all_job_urls)}")
    target_urls = all_job_urls[:MAX_JOBS]
    print(f"Processing top {len(target_urls)} jobs through Jina AI Proxy to bypass Akamai Bot Manager...")
    
    job_data = []
    # 5 workers to stay under Jina's rate limits
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_url = {executor.submit(parse_jina_job, url): url for url in target_urls}
        for future in concurrent.futures.as_completed(future_to_url):
            data = future.result()
            if data:
                job_data.append(data)
                print(f"Extracted: {data['Job Title/Company Details'][:70]}...")
            time.sleep(0.5) 
            
    if job_data:
        df = pd.DataFrame(job_data)
        df.to_excel(OUTPUT_FILE, index=False)
        print(f"\n✅ Successfully saved {len(job_data)} enriched Naukri jobs to {OUTPUT_FILE}!")
    else:
        print("No job data extracted.")

if __name__ == "__main__":
    main()
