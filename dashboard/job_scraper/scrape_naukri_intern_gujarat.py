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
    'https://www.naukri.com/sitemap/jobDescPagesAhmedabad.xml' # Ahmedabad is the main IT hub of Gujarat
]
URL_KEYWORD = "intern" # Match any internship in URL first
STRICT_TITLE_KEYWORD = r'\b(software|sde|frontend|backend|full stack|web|app|android|ios|python|java|react)\b.*\bintern(ship)?\b' # Strict title match
DAYS_AGO = 7 # Last 1 week
MAX_JOBS_TO_FETCH = 1000 
OUTPUT_FILE = "job_scraper/Naukri_SDE_Intern_Gujarat.xlsx"
OUTPUT_JSON = "job_scraper/Naukri_SDE_Intern_Gujarat.json"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
}

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
                if URL_KEYWORD.lower() in loc_text.lower():
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
    jina_url = f"https://r.jina.ai/{url}"
    try:
        res = requests.get(jina_url, timeout=5) 
        if res.status_code != 200:
            raise Exception("Jina returned non-200")
        text = res.text
        
        title_line = "Unknown"
        for line in text.split('\n'):
            if line.startswith('Title:'):
                title_line = line.replace('Title:', '').strip()
                break
                
        if not re.search(STRICT_TITLE_KEYWORD, title_line, re.IGNORECASE):
            # Check URL fallback
            if not re.search(STRICT_TITLE_KEYWORD, url.replace('-', ' '), re.IGNORECASE):
                return None
            
        return {
            "Job Title/Company Details": title_line,
            "Posted Date (Accurate)": job_date,
            "Job URL": url,
            "Raw Markdown": text[:2500] 
        }
    except Exception as e:
        slug = url.split('job-listings-')[-1].split('?')[0]
        title_line = slug.replace('-', ' ').title()
        
        # Verify strict match against the slug since Jina failed
        if not re.search(STRICT_TITLE_KEYWORD, title_line, re.IGNORECASE):
            return None
            
        return {
            "Job Title/Company Details": title_line,
            "Posted Date (Accurate)": job_date,
            "Job URL": url,
            "Raw Markdown": "Rate limited by proxy. View URL directly." 
        }

def main():
    all_job_info = []
    
    for sm in SITEMAPS:
        jobs = get_recent_job_urls(sm)
        all_job_info.extend(jobs)
        print(f" -> Found {len(jobs)} recent '{URL_KEYWORD}' jobs in {sm.split('/')[-1]}.")
        
    print(f"\nTotal recent matching URLs found in Gujarat: {len(all_job_info)}")
    target_jobs = all_job_info[:MAX_JOBS_TO_FETCH]
    print(f"Processing top {len(target_jobs)} jobs through Jina AI Proxy...")
    
    job_data = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_to_job = {executor.submit(parse_jina_job, j): j for j in target_jobs}
        for future in concurrent.futures.as_completed(future_to_job):
            data = future.result()
            if data:
                job_data.append(data)
                print(f"✅ Extracted SDE Intern: {data['Job Title/Company Details'][:60]}... Date: {data['Posted Date (Accurate)']}")
            time.sleep(1.0) 
            
    if job_data:
        df = pd.DataFrame(job_data)
        df.to_excel(OUTPUT_FILE, index=False)
        df.to_json(OUTPUT_JSON, orient='records', indent=4)
        print(f"\n🎉 Successfully saved {len(job_data)} matched Software Dev Intern jobs to {OUTPUT_FILE} and JSON!")
    else:
        print("No Software Development Intern jobs found in Gujarat for the last 1 week.")

if __name__ == "__main__":
    main()
