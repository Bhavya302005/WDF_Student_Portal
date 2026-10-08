import subprocess
import json
import re
import pandas as pd
import time

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

all_parsed_jobs = {}
total_roles = len(roles)

def clean_text(text):
    if not isinstance(text, str):
        return ""
    # Remove illegal characters for excel/csv
    return re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)

for i, role in enumerate(roles):
    print(f"Running search {i+1}/{total_roles}: {role} (Max 100)")
    query = f'site:linkedin.com "#Hiring" "{role}"'
    cmd = f'source ~/.agent-reach-venv/bin/activate && mcporter call exa.web_search_exa query=\'{query}\' numResults=100 objective="Only return very recent job postings for {role}." --output json'
    
    result = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True)
    
    if result.returncode != 0:
        continue
        
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        continue
        
    raw_text = ""
    if isinstance(data, dict) and 'content' in data:
        for c in data['content']:
            if c.get('type') == 'text':
                raw_text += c.get('text', '')

    posts = raw_text.split('---')
    count_this_run = 0
    
    for post in posts:
        post = post.strip()
        if not post:
            continue
        
        title_match = re.search(r'Title:\s*(.+)', post)
        url_match = re.search(r'URL:\s*(.+)', post)
        pub_match = re.search(r'Published:\s*(.+)', post)
        auth_match = re.search(r'Author:\s*(.+)', post)
        highlights_match = re.search(r'Highlights:\n(.*)', post, re.DOTALL)
        
        title = title_match.group(1).strip() if title_match else ''
        url = url_match.group(1).strip() if url_match else ''
        
        if not url:
            continue
            
        job = {
            'Searched_Role': role,
            'Title': clean_text(title),
            'URL': url,
            'Published_Date': clean_text(pub_match.group(1).strip() if pub_match else ''),
            'Author': clean_text(auth_match.group(1).strip() if auth_match else ''),
            'Highlights': clean_text((highlights_match.group(1).strip() if highlights_match else post).replace('\n', ' '))
        }
        
        if url not in all_parsed_jobs:
            all_parsed_jobs[url] = job
            count_this_run += 1
            
    print(f"  -> Found {count_this_run} new unique jobs for {role}.")
    time.sleep(1)

final_jobs = list(all_parsed_jobs.values())
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/all_roles_jobs_100.csv'

if final_jobs:
    df = pd.DataFrame(final_jobs)
    df.to_csv(output_file, index=False)
    print(f"Successfully collected {len(final_jobs)} total unique jobs.")
    print(f"Saved to {output_file}")
