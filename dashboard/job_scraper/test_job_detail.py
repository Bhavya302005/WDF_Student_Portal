import requests
from bs4 import BeautifulSoup
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

# LinkedIn has a guest-accessible job detail page!
job_url = "https://www.linkedin.com/jobs/view/project-manager-commercial-hvac-projects-at-jobot-4465970902"

print(f"Fetching job details from: {job_url}")
resp = requests.get(job_url, headers=headers, timeout=10)
print(f"Status: {resp.status_code}")

if resp.status_code == 200:
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    # Get job description
    desc = soup.find('div', class_='description__text')
    if not desc:
        desc = soup.find('div', class_='show-more-less-html__markup')
    
    # Get poster/recruiter info
    poster = soup.find('a', class_='sub-nav-cta__optional-url')
    
    # Get all the metadata
    criteria = soup.find_all('li', class_='description__job-criteria-item')
    
    print(f"\n--- JOB DETAILS ---")
    
    if desc:
        desc_text = desc.get_text(separator='\n').strip()
        print(f"Description length: {len(desc_text)} chars")
        print(f"First 500 chars:\n{desc_text[:500]}")
        
        # Extract emails from description
        emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', desc_text)
        print(f"\nEmails found: {emails if emails else 'None'}")
    else:
        print("No description found")
    
    if poster:
        print(f"\nPoster link: {poster.get('href', 'N/A')}")
        print(f"Poster text: {poster.text.strip()}")
    
    if criteria:
        print(f"\nJob Criteria:")
        for c in criteria:
            header = c.find('h3')
            value = c.find('span')
            if header and value:
                print(f"  {header.text.strip()}: {value.text.strip()}")
