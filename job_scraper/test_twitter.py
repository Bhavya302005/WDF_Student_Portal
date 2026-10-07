import subprocess
import json
import re

# Remove the date restriction to see if Exa has any Twitter data at all
query = 'site:x.com OR site:twitter.com "#hiring" "Project Manager"'
cmd = f'source ~/.agent-reach-venv/bin/activate && mcporter call exa.web_search_exa query=\'{query}\' numResults=5 --output json'

print(f"Searching X (Twitter) without date limits...")
result = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True)

if result.returncode == 0:
    try:
        data = json.loads(result.stdout)
        raw = ""
        if isinstance(data, dict) and 'content' in data:
            for c in data['content']:
                if c.get('type') == 'text':
                    raw += c.get('text', '')
        
        posts = [p for p in raw.split('---') if p.strip()]
        print(f"Found {len(posts)} posts from X:")
        
        for i, p in enumerate(posts):
            title_m = re.search(r'Title:\s*(.+)', p)
            url_m = re.search(r'URL:\s*(.+)', p)
            pub_m = re.search(r'Published:\s*(.+)', p)
            auth_m = re.search(r'Author:\s*(.+)', p)
            hl_m = re.search(r'Highlights:\n(.*)', p, re.DOTALL)
            
            title = title_m.group(1).strip() if title_m else ''
            url = url_m.group(1).strip() if url_m else ''
            
            if url:
                print(f"\n{i+1}. {title}")
                print(f"   URL: {url}")
    except Exception as e:
        print(f"Error parsing Exa output: {e}")
else:
    print(f"Exa command failed: {result.stderr[:200]}")
