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

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

def clean_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)

def get_job_details(job_url):
    """Fetch full job details from LinkedIn guest page"""
    try:
        resp = requests.get(job_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {}
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        details = {}
        
        # Full description
        desc = soup.find('div', class_='show-more-less-html__markup') or soup.find('div', class_='description__text')
        if desc:
            desc_text = desc.get_text(separator=' ').strip()
            details['Full_Description'] = clean_text(desc_text[:2000])  # Cap at 2000 chars
            
            # Extract emails
            emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', desc_text)
            details['Emails'] = ', '.join(set(emails)) if emails else ''
            
            # Try to extract recruiter/poster name from description
            hosted_by = re.search(r'hosted by[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)', desc_text)
            if hosted_by:
                details['Recruiter_Name'] = hosted_by.group(1)
        
        # Job criteria
        criteria = soup.find_all('li', class_='description__job-criteria-item')
        for c in criteria:
            h = c.find('h3')
            v = c.find('span')
            if h and v:
                key = clean_text(h.text.strip().replace(' ', '_'))
                details[key] = clean_text(v.text.strip())
        
        # Poster/company link
        poster = soup.find('a', class_='sub-nav-cta__optional-url')
        if poster:
            details['Poster_Company'] = clean_text(poster.text.strip())
        
        return details
    except Exception as e:
        return {}

# ============= MAIN EXECUTION =============
all_jobs = []
seen_urls = set()

print("🔥 PHASE 1: Getting latest jobs (Past 24h) from official LinkedIn Jobs...")
for i, role in enumerate(roles):
    print(f"[{i+1}/{len(roles)}] {role}")
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={requests.utils.quote(role)}&f_TPR=r86400&sortBy=DD&start=0"
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            jobs = soup.find_all('li')
            count = 0
            for job in jobs:
                title_elem = job.find('h3', class_='base-search-card__title')
                company_elem = job.find('h4', class_='base-search-card__subtitle')
                location_elem = job.find('span', class_='job-search-card__location')
                link_elem = job.find('a', class_='base-card__full-link')
                time_elem = job.find('time', class_='job-search-card__listdate--new') or job.find('time', class_='job-search-card__listdate')
                
                if title_elem and link_elem:
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
                        count += 1
            print(f"  -> {count} new unique jobs")
    except Exception as e:
        print(f"  -> Error: {e}")
    time.sleep(1)

print(f"\n✅ Phase 1 complete: {len(all_jobs)} unique jobs found.")

# Phase 2: Enrich with full details (for first 50 jobs to avoid rate limiting)
print(f"\n🔍 PHASE 2: Enriching top 50 jobs with full descriptions, recruiter names, emails...")
enriched_count = 0
for j in all_jobs[:50]:
    details = get_job_details(j['Job_URL'])
    j.update(details)
    if details:
        enriched_count += 1
    time.sleep(1)  # Be gentle

print(f"  -> Enriched {enriched_count}/50 jobs with full details.")

# Save
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/latest_enriched_jobs.csv'
df = pd.DataFrame(all_jobs)
df.to_csv(output_file, index=False)

print(f"\n🎯 DONE! Saved {len(df)} jobs to {output_file}")
print(f"   - {enriched_count} jobs have full descriptions, recruiter names, and emails.")
