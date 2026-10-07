import subprocess
import json
import re

query = 'site:linkedin.com "#Hiring" "Project Manager" "Junior"'
all_parsed_jobs = {}

for i in range(3):
    print(f"Running search {i+1}/3 with EXACT SAME QUERY...")
    cmd = f'source ~/.agent-reach-venv/bin/activate && mcporter call exa.web_search_exa query=\'{query}\' numResults=100 objective="Only return job postings or posts published recently for Project Manager roles." --output json'
    
    result = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error running search {i+1}")
        continue
        
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Error parsing JSON from search {i+1}")
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
            
        url_match = re.search(r'URL:\s*(.+)', post)
        url = url_match.group(1).strip() if url_match else ''
        
        if not url:
            continue
            
        count_this_run += 1
        all_parsed_jobs[url] = True
        
    print(f"  -> Search {i+1} returned {count_this_run} results.")

print(f"\nTotal UNIQUE jobs collected across all 3 identical runs: {len(all_parsed_jobs)}")
if len(all_parsed_jobs) == 100:
    print("Conclusion: Running the exact same query multiple times returns the exact same 100 results every time.")
else:
    print(f"Conclusion: There was some variation. You got {len(all_parsed_jobs) - 100} extra jobs.")
