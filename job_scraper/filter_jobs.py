import json
import re

input_file = '/tmp/exa_results.json'
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/junior_pm_jobs.json'

with open(input_file, 'r') as f:
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
    
    # Extract fields
    title_match = re.search(r'Title:\s*(.+)', post)
    url_match = re.search(r'URL:\s*(.+)', post)
    pub_match = re.search(r'Published:\s*(.+)', post)
    auth_match = re.search(r'Author:\s*(.+)', post)
    
    # Everything after Highlights: is content
    highlights_match = re.search(r'Highlights:\n(.*)', post, re.DOTALL)
    
    job['title'] = title_match.group(1).strip() if title_match else ''
    job['url'] = url_match.group(1).strip() if url_match else ''
    job['publishedDate'] = pub_match.group(1).strip() if pub_match else ''
    job['author'] = auth_match.group(1).strip() if auth_match else ''
    job['highlights'] = highlights_match.group(1).strip() if highlights_match else post
    
    parsed_jobs.append(job)

filtered_jobs = []

def expects_0_3_years(title, text):
    title = (title or '').lower()
    text = (text or '').lower()
    
    # Exclude if explicitly senior
    if any(word in title for word in ['senior', 'sr', 'director', 'manager of', 'lead', 'principal', 'vp', 'head']):
        return False
        
    # Exclude if explicitly high experience
    # Looking for things like "5+ years", "10 years", "5-7 years"
    high_exp = re.search(r'([4-9]|\d{2})\+?\s*years?', text)
    if high_exp:
        return False
        
    # Include if explicitly junior keywords
    if any(word in title for word in ['junior', 'jr', 'associate', 'assistant', 'entry', 'coordinator', 'coordinator']):
        return True
        
    # Include if explicitly says 0-3 years
    low_exp = re.search(r'([0-3])\+?\s*years?', text)
    if low_exp:
        return True
        
    # Include jobs that don't specify high experience and aren't senior in title
    return True

for job in parsed_jobs:
    if expects_0_3_years(job['title'], job['highlights']):
        filtered_jobs.append(job)

with open(output_file, 'w') as f:
    json.dump(filtered_jobs, f, indent=2)

print(f"Parsed {len(parsed_jobs)} jobs. Filtered {len(filtered_jobs)} junior/mid jobs.")
