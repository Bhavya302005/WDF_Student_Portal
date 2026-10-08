import subprocess
import json
import re
import csv
import os

queries = [
    'site:linkedin.com "#Hiring" "Project Manager" "Entry Level"',
    'site:linkedin.com "#Hiring" "Project Manager" "Junior"',
    'site:linkedin.com "#Hiring" "Project Manager" "Associate"'
]

all_parsed_jobs = {}

for i, query in enumerate(queries):
    print(f"Running search {i+1}/3: {query}")
    cmd = f'source ~/.agent-reach-venv/bin/activate && mcporter call exa.web_search_exa query=\'{query}\' numResults=100 objective="Only return job postings or posts published recently for Project Manager roles." --output json'
    
    result = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error running search {i+1}: {result.stderr}")
        continue
        
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Error parsing JSON from search {i+1}")
        continue
        
    # Extract the raw text
    raw_text = ""
    if isinstance(data, dict) and 'content' in data:
        for c in data['content']:
            if c.get('type') == 'text':
                raw_text += c.get('text', '')

    # Parse the text blocks separated by '---'
    posts = raw_text.split('---')
    
    for post in posts:
        post = post.strip()
        if not post:
            continue
        
        job = {}
        title_match = re.search(r'Title:\s*(.+)', post)
        url_match = re.search(r'URL:\s*(.+)', post)
        pub_match = re.search(r'Published:\s*(.+)', post)
        auth_match = re.search(r'Author:\s*(.+)', post)
        highlights_match = re.search(r'Highlights:\n(.*)', post, re.DOTALL)
        
        title = title_match.group(1).strip() if title_match else ''
        url = url_match.group(1).strip() if url_match else ''
        
        if not url:
            continue
            
        job['title'] = title
        job['url'] = url
        job['publishedDate'] = pub_match.group(1).strip() if pub_match else ''
        job['author'] = auth_match.group(1).strip() if auth_match else ''
        job['highlights'] = highlights_match.group(1).strip() if highlights_match else post
        
        # Deduplicate using URL
        if url not in all_parsed_jobs:
            all_parsed_jobs[url] = job

output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/combined_pm_jobs.csv'
final_jobs = list(all_parsed_jobs.values())

if final_jobs:
    headers = list(final_jobs[0].keys())
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in final_jobs:
            if 'highlights' in row and isinstance(row['highlights'], str):
                row['highlights'] = row['highlights'].replace('\n', ' ').strip()
            writer.writerow(row)

print(f"Successfully collected and deduplicated {len(final_jobs)} unique jobs across 3 searches.")
