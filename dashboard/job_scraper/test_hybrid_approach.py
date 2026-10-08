import requests
from bs4 import BeautifulSoup
import subprocess
import json
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

# Step 1: Get the latest job URLs from the official LinkedIn Jobs guest API (last 24 hours)
print("Step 1: Getting latest official job URLs...")
url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Project%20Manager&f_TPR=r86400&sortBy=DD&start=0"
resp = requests.get(url, headers=headers, timeout=10)
soup = BeautifulSoup(resp.text, 'html.parser')
jobs = soup.find_all('li')

job_urls = []
for job in jobs[:3]:  # Test with just 3
    link_elem = job.find('a', class_='base-card__full-link')
    title_elem = job.find('h3', class_='base-search-card__title')
    if link_elem and 'href' in link_elem.attrs:
        job_url = link_elem['href'].split('?')[0]
        title = title_elem.text.strip() if title_elem else "Unknown"
        job_urls.append((title, job_url))
        print(f"  -> {title}: {job_url}")

# Step 2: Use Exa's web_fetch to read the full content of each job page and extract recruiter info
print("\nStep 2: Using Exa web_fetch to extract recruiter details from each job...")
for title, job_url in job_urls:
    print(f"\n--- Fetching: {title} ---")
    cmd = f'source ~/.agent-reach-venv/bin/activate && mcporter call exa.web_fetch_exa url=\'{job_url}\' --output json'
    result = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True)
    
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            raw = ""
            if isinstance(data, dict) and 'content' in data:
                for c in data['content']:
                    if c.get('type') == 'text':
                        raw += c.get('text', '')
            
            # Extract emails
            emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', raw)
            
            # Look for recruiter/poster name patterns
            print(f"  Content length: {len(raw)} chars")
            print(f"  Emails found: {emails if emails else 'None'}")
            # Print first 500 chars to see what we get
            print(f"  Preview: {raw[:500]}")
        except:
            print(f"  Error parsing response")
    else:
        print(f"  Fetch failed: {result.stderr[:200]}")
