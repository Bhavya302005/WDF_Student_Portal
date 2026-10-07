import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import concurrent.futures
import re
from datetime import datetime, timedelta
from typing import List, Dict

# User settings
SITEMAPS = [
    'https://www.naukri.com/sitemap/jobDescPagesAhmedabad.xml'
]
DAYS_AGO = 7 
MAX_JOBS_TO_FETCH = 5000 # Increased to fetch ALL jobs
OUTPUT_FILE = "job_scraper/Naukri_Software_Engineer_Gujarat.xlsx"
OUTPUT_JSON = "job_scraper/Naukri_Software_Engineer_Gujarat.json"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
}

def is_sde_url(url: str) -> bool:
    # Quick filter based on URL slug
    slug = url.lower()
    return bool(re.search(r'-(software|developer|engineer|sde|frontend|backend|full-stack|fullstack|web-developer|java|python|react)-', slug))

def get_recent_job_urls(sitemap_url: str) -> List[Dict[str, str]]:
    print(f"Parsing sitemap: {sitemap_url.split('/')[-1]}")
    try:
        res = requests.get(sitemap_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, 'xml')
        
        cutoff_date = datetime.now() - timedelta(days=DAYS_AGO)
        valid_jobs = []
        
        for url_tag in soup.find_all('url'):
            loc = url_tag.find('loc')
            lastmod = url_tag.find('lastmod')
            
            if loc and lastmod:
                loc_text = loc.text
                if is_sde_url(loc_text):
                    date_str = lastmod.text[:10] 
                    try:
                        job_date = datetime.strptime(date_str, "%Y-%m-%d")
                        if job_date >= cutoff_date:
                            valid_jobs.append({"url": loc_text, "date": date_str})
                    except Exception:
                        pass
        return valid_jobs
    except Exception as e:
        print(f"Error fetching {sitemap_url}: {e}")
        return []

def parse_jina_job(job_info: Dict[str, str]) -> Dict:
    url = job_info['url']
    job_date = job_info['date']
    
    slug = url.split('job-listings-')[-1].split('?')[0]
    title_line = slug.replace('-', ' ').title()
    
    # Strict regex to ensure it's a software engineer role (excluding generic engineers like mechanical)
    strict_regex = r'\b(software|sde|frontend|backend|full stack|web|app|android|ios|python|java|react|angular|node|developer)\b'
    if not re.search(strict_regex, title_line, re.IGNORECASE):
        return None
        
    return {
        "Job Title/Company Details": title_line,
        "Posted Date (Accurate)": job_date,
        "Job URL": url,
        "Raw Markdown": "Direct URL Parse (Blazing Fast Mode)" 
    }

def main():
    all_job_info = []
    
    for sm in SITEMAPS:
        jobs = get_recent_job_urls(sm)
        all_job_info.extend(jobs)
        print(f" -> Found {len(jobs)} recent Software Engineer URLs in {sm.split('/')[-1]}.")
        
    print(f"\nTotal recent matching URLs found in Gujarat: {len(all_job_info)}")
    target_jobs = all_job_info[:MAX_JOBS_TO_FETCH]
    if len(all_job_info) > 0:
        print(f"Processing ALL {len(target_jobs)} jobs instantly...")
    
    job_data = []
    # No network requests made, just parsing strings, so this is instantaneous
    for j in target_jobs:
        data = parse_jina_job(j)
        if data:
            job_data.append(data)
            
    if job_data:
        df = pd.DataFrame(job_data)
        df.to_excel(OUTPUT_FILE, index=False)
        df.to_json(OUTPUT_JSON, orient='records', indent=4)
        print(f"\n🎉 Successfully saved {len(job_data)} Software Engineer jobs to {OUTPUT_FILE} and JSON!")
    else:
        print("No Software Engineer jobs found in Gujarat for the last 1 week.")

if __name__ == "__main__":
    main()
