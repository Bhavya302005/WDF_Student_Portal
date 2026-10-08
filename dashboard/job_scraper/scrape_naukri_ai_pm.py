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
    'https://www.naukri.com/sitemap/jobDescPagesBangalore.xml',
    'https://www.naukri.com/sitemap/jobDescPagesPune.xml',
    'https://www.naukri.com/sitemap/jobDescPagesDelhi.xml',
    'https://www.naukri.com/sitemap/jobDescPagesChennai.xml',
    'https://www.naukri.com/sitemap/jobDescPagesNoida.xml'
]
URL_KEYWORD = "product-manager"
STRICT_TITLE_KEYWORD = r'\b(ai|artificial intelligence)\b' # Regex pattern for title match
DAYS_AGO = 14
MAX_JOBS_TO_FETCH = 1000 
OUTPUT_FILE = "job_scraper/Naukri_AI_PM_5States_ALL.xlsx"

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
                    # lastmod is like '2026-09-17T01:49:27.269+05:30'
                    date_str = lastmod.text[:10] # just grab 'YYYY-MM-DD'
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
        res = requests.get(jina_url, timeout=5) # Fail fast on Jina rate limits
        if res.status_code != 200:
            raise Exception("Jina returned non-200")
        text = res.text
        
        title_line = "Unknown"
        for line in text.split('\n'):
            if line.startswith('Title:'):
                title_line = line.replace('Title:', '').strip()
                break
                
        # Strictly filter the title using regex to avoid matching "Chennai", but catching "Artificial Intelligence"
        if not re.search(STRICT_TITLE_KEYWORD, title_line, re.IGNORECASE):
            # Check if it's explicitly in the URL as a fallback if the title got mangled
            if not re.search(r'-(ai|artificial-intelligence)-', url.lower()):
                return None
            
        return {
            "Job Title/Company Details": title_line,
            "Posted Date (Accurate)": job_date,
            "Job URL": url,
            "Raw Markdown": text[:2500] 
        }
    except Exception as e:
        # Fallback: Parse from URL slug if Jina fails (due to rate limits)
        if re.search(r'-(ai|artificial-intelligence)-', url.lower()):
            slug = url.split('job-listings-')[-1].split('?')[0]
            title_line = slug.replace('-', ' ').title()
            return {
                "Job Title/Company Details": title_line,
                "Posted Date (Accurate)": job_date,
                "Job URL": url,
                "Raw Markdown": "Rate limited by proxy. View URL directly." 
            }
        return None

def main():
    all_job_info = []
    
    for sm in SITEMAPS:
        jobs = get_recent_job_urls(sm)
        all_job_info.extend(jobs)
        print(f" -> Found {len(jobs)} recent '{URL_KEYWORD}' jobs.")
        
    print(f"\nTotal recent matching URLs found across 5 states: {len(all_job_info)}")
    target_jobs = all_job_info[:MAX_JOBS_TO_FETCH]
    print(f"Processing top {len(target_jobs)} jobs through Jina AI Proxy...")
    
    job_data = []
    # Reduced workers to 2 to heavily avoid Jina rate limit timeouts (429 errors)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_to_job = {executor.submit(parse_jina_job, j): j for j in target_jobs}
        for future in concurrent.futures.as_completed(future_to_job):
            data = future.result()
            if data:
                job_data.append(data)
                print(f"✅ Extracted AI PM: {data['Job Title/Company Details'][:60]}... Date: {data['Posted Date (Accurate)']}")
            time.sleep(1.0) # Slower delay to ensure all jobs process properly without failing
            
    if job_data:
        df = pd.DataFrame(job_data)
        df.to_excel(OUTPUT_FILE, index=False)
        print(f"\n🎉 Successfully saved {len(job_data)} strictly matched AI jobs to {OUTPUT_FILE}!")
    else:
        print("No AI matching job data extracted.")

if __name__ == "__main__":
    main()
