import json
import re
from datetime import datetime, timedelta
import csv

input_file = '/tmp/exa_results.json'
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/all_pm_jobs_1_day.csv'

with open(input_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

# Extract the raw text
raw_text = ""
if isinstance(data, dict) and 'content' in data:
    for c in data['content']:
        if c.get('type') == 'text':
            raw_text += c.get('text', '')

# Parse the text blocks separated by '---'
posts = raw_text.split('---')

parsed_jobs = []
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
    
    job['title'] = title_match.group(1).strip() if title_match else ''
    job['url'] = url_match.group(1).strip() if url_match else ''
    job['publishedDate'] = pub_match.group(1).strip() if pub_match else ''
    job['author'] = auth_match.group(1).strip() if auth_match else ''
    job['highlights'] = highlights_match.group(1).strip() if highlights_match else post
    
    parsed_jobs.append(job)

current_date = datetime.fromisoformat('2026-09-16T00:00:00+00:00')
one_day_ago = current_date - timedelta(days=1)

filtered_jobs = []
for job in parsed_jobs:
    pub_date_str = job.get('publishedDate')
    if pub_date_str:
        try:
            pub_date_str = pub_date_str.replace('Z', '+00:00')
            pub_date = datetime.fromisoformat(pub_date_str)
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=current_date.tzinfo)
            if pub_date >= one_day_ago:
                filtered_jobs.append(job)
        except Exception:
            pass

if filtered_jobs:
    headers = list(filtered_jobs[0].keys())
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in filtered_jobs:
            if 'highlights' in row and isinstance(row['highlights'], str):
                row['highlights'] = row['highlights'].replace('\n', ' ').strip()
            writer.writerow(row)

print(f"Parsed {len(parsed_jobs)} total jobs. Found {len(filtered_jobs)} jobs published in the last 1 day.")
