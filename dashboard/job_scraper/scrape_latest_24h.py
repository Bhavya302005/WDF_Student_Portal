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

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

def clean_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)

print("🔥 Scraping LATEST jobs (Past 24 Hours, Sorted by Newest) from official LinkedIn Jobs...")

for i, role in enumerate(roles):
    print(f"[{i+1}/{len(roles)}] {role}")
    # f_TPR=r86400 = Past 24 hours, sortBy=DD = Sort by Date Descending (Newest first)
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={requests.utils.quote(role)}&f_TPR=r86400&sortBy=DD&start=0"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            jobs = soup.find_all('li')
            count = 0
            
            for job in jobs:
                title_elem = job.find('h3', class_='base-search-card__title')
                company_elem = job.find('h4', class_='base-search-card__subtitle')
                location_elem = job.find('span', class_='job-search-card__location')
                link_elem = job.find('a', class_='base-card__full-link')
                time_elem = job.find('time', class_='job-search-card__listdate--new') or job.find('time', class_='job-search-card__listdate')
                
                if title_elem and link_elem:
                    all_jobs.append({
                        'Searched_Role': role,
                        'Title': clean_text(title_elem.text.strip()),
                        'Company': clean_text(company_elem.text.strip()) if company_elem else "",
                        'Location': clean_text(location_elem.text.strip()) if location_elem else "",
                        'Posted_Time': clean_text(time_elem.text.strip()) if time_elem else "",
                        'Job_URL': link_elem['href'].split('?')[0] if 'href' in link_elem.attrs else ""
                    })
                    count += 1
            print(f"  -> {count} latest jobs found.")
        else:
            print(f"  -> Blocked (Status: {response.status_code})")
    except Exception as e:
        print(f"  -> Error: {e}")
        
    time.sleep(2)

output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/latest_official_jobs_24h.csv'
df = pd.DataFrame(all_jobs)
df.drop_duplicates(subset=['Job_URL'], inplace=True)
df.to_csv(output_file, index=False)

print(f"\n🎯 Done! Collected {len(df)} UNIQUE latest jobs (posted in last 24 hours)!")
print(f"Saved to {output_file}")
