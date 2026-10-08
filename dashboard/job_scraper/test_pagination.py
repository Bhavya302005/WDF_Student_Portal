import requests
from bs4 import BeautifulSoup
import time

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

role = "Project Manager"
total_jobs = 0
seen_urls = set()

print(f"Testing pagination for: {role}")
print(f"{'Page':>6} | {'Start':>6} | {'Jobs Found':>10} | {'New Unique':>10} | {'Total':>6}")
print("-" * 60)

for page in range(50):  # Test up to 500 jobs (50 pages x 10 per page)
    start = page * 10
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={requests.utils.quote(role)}&sortBy=DD&start={start}"
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"  Page {page+1}: Blocked (Status {resp.status_code}) - stopping.")
            break
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        jobs = soup.find_all('li')
        
        new_count = 0
        for job in jobs:
            link = job.find('a', class_='base-card__full-link')
            if link and 'href' in link.attrs:
                job_url = link['href'].split('?')[0]
                if job_url not in seen_urls:
                    seen_urls.add(job_url)
                    new_count += 1
        
        total_jobs = len(seen_urls)
        print(f"{page+1:>6} | {start:>6} | {len(jobs):>10} | {new_count:>10} | {total_jobs:>6}")
        
        if len(jobs) == 0:
            print("  -> No more results. Hit the pagination limit!")
            break
            
    except Exception as e:
        print(f"  Page {page+1}: Error - {e}")
        break
    
    time.sleep(1)

print(f"\n🎯 RESULT: Successfully scraped {total_jobs} unique jobs for '{role}' using pagination!")
