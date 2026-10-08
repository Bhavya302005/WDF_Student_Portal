import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re

roles = [
    "Quality Assurance Manager", "Project Management Specialist", "Project Administrator", "Project Specialist", "Project Control Analyst",
    "Project Manager", "Senior Project Manager", "Project Engineer", "Assistant Project Manager", "Technical Project Manager",
    "Information Technology Project Analyst", "Project Development Specialist", "Project Support Coordinator", "Senior Project Lead", "Senior Operations Project Manager",
    "Project Control Coordinator", "Special Project Administrator", "Lead Project Engineer", "Project Management Administrator", "Technical Project Specialist",
    "Business Project Manager", "Junior Project Manager", "Information Technology Operations Project Manager", "Operations Project Manager", "Technical Project Lead",
    "Project Team Lead", "Information Technology Project Lead", "Recruiting Operations Project Manager", "Project Planning Specialist", "Information Technology Project Manager",
    "Senior Project Analyst", "Project Finance Specialist", "Project Assistant", "Project Consultant", "Project Sales Specialist",
    "Project Implementation Specialist", "Lead Project Analyst", "Service Project Manager", "Project Analyst", "Information Technology Project Coordinator",
    "Project Lead", "Software Project Lead", "Business Analyst Project Lead", "Project Management Analyst",
    "Special Project Manager", "Project Business Analyst", "Lead Project Manager", "Senior Project Administrator", "Project Support Analyst"
]

all_jobs = []
seen_urls = set()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

def clean_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)

print("🚀 MASSIVE SCRAPE: Fetching up to 300 jobs per role (Last 24h)...")

for i, role in enumerate(roles):
    role_count = 0
    print(f"\n[{i+1}/{len(roles)}] Scraping: {role}")
    
    # Paginate up to 30 pages (300 jobs max per role)
    for page in range(30):
        start = page * 10
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={requests.utils.quote(role)}&f_TPR=r86400&sortBy=DD&start={start}"
        
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                print(f"  -> Rate limited/Blocked on page {page+1}. Moving to next role...")
                break
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            jobs = soup.find_all('li')
            
            if len(jobs) == 0:
                # No more jobs for this role in the last 24h
                break
                
            for job in jobs:
                title_elem = job.find('h3', class_='base-search-card__title')
                company_elem = job.find('h4', class_='base-search-card__subtitle')
                location_elem = job.find('span', class_='job-search-card__location')
                link_elem = job.find('a', class_='base-card__full-link')
                time_elem = job.find('time', class_='job-search-card__listdate--new') or job.find('time', class_='job-search-card__listdate')
                
                if title_elem and link_elem and 'href' in link_elem.attrs:
                    job_url = link_elem['href'].split('?')[0]
                    if job_url not in seen_urls:
                        seen_urls.add(job_url)
                        all_jobs.append({
                            'Searched_Role': role,
                            'Title': clean_text(title_elem.text.strip()),
                            'Company': clean_text(company_elem.text.strip()) if company_elem else "",
                            'Location': clean_text(location_elem.text.strip()) if location_elem else "",
                            'Posted_Time': clean_text(time_elem.text.strip()) if time_elem else "",
                            'Job_URL': job_url
                        })
                        role_count += 1
                        
            # Polite delay between pages to avoid instant bans
            time.sleep(1)
            
        except Exception as e:
            print(f"  -> Error on page {page+1}: {e}")
            break
            
    print(f"  -> Collected {role_count} total unique jobs for {role}")
    # Extra delay between roles
    time.sleep(2)

output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/massive_official_jobs_24h.csv'
df = pd.DataFrame(all_jobs)
df.to_csv(output_file, index=False)

print(f"\n🎉 MASSIVE SCRAPE DONE! Collected {len(df)} UNIQUE latest jobs!")
print(f"Saved to {output_file}")
