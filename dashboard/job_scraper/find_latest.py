import json
import re
from datetime import datetime

input_file = '/tmp/exa_results.json'

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
    pub_match = re.search(r'Published:\s*(.+)', post)
    
    title = title_match.group(1).strip() if title_match else ''
    pub_date_str = pub_match.group(1).strip() if pub_match else ''
    
    if pub_date_str and pub_date_str != 'None':
        try:
            pub_date_str_clean = pub_date_str.replace('Z', '+00:00')
            pub_date = datetime.fromisoformat(pub_date_str_clean)
            job['title'] = title
            job['publishedDate'] = pub_date
            parsed_jobs.append(job)
        except Exception:
            pass

# Sort by date descending
parsed_jobs.sort(key=lambda x: x['publishedDate'], reverse=True)

print("The 5 latest jobs found are:")
for job in parsed_jobs[:5]:
    print(f"- {job['title']} (Published: {job['publishedDate'].strftime('%Y-%m-%d')})")
