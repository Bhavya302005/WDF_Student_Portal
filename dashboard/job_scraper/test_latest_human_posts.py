import requests
from bs4 import BeautifulSoup
import re
import json
import time

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

# Test 1: Try LinkedIn's own content/posts search (guest endpoint)
print("=== TEST 1: LinkedIn Posts Search (Guest API) ===")
url1 = "https://www.linkedin.com/search/results/content/?keywords=%23hiring%20Project%20Manager&datePosted=%22past-24h%22&sortBy=%22date_posted%22"
resp1 = requests.get(url1, headers=headers, allow_redirects=False)
print(f"Status: {resp1.status_code}")
print(f"Redirected to: {resp1.headers.get('Location', 'No redirect')}")

# Test 2: Try Google search with site:linkedin.com/posts and date filter (tbs=qdr:d = past day)
print("\n=== TEST 2: Google Search for LinkedIn Posts (Past 24h) ===")
url2 = "https://www.google.com/search?q=site%3Alinkedin.com%2Fposts+%22%23hiring%22+%22Project+Manager%22&tbs=qdr:d&num=10"
resp2 = requests.get(url2, headers=headers)
print(f"Status: {resp2.status_code}")
if resp2.status_code == 200:
    soup = BeautifulSoup(resp2.text, 'html.parser')
    # Look for search result links
    links = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'linkedin.com/posts/' in href:
            links.append(href)
    print(f"Found {len(links)} LinkedIn post links from Google")
    for link in links[:3]:
        print(f"  -> {link}")

# Test 3: Try DuckDuckGo (less aggressive anti-bot)
print("\n=== TEST 3: DuckDuckGo Search for LinkedIn Posts ===")
url3 = "https://html.duckduckgo.com/html/?q=site%3Alinkedin.com%2Fposts+%22%23hiring%22+%22Project+Manager%22&df=d"
resp3 = requests.get(url3, headers=headers)
print(f"Status: {resp3.status_code}")
if resp3.status_code == 200:
    soup3 = BeautifulSoup(resp3.text, 'html.parser')
    results = soup3.find_all('a', class_='result__a')
    print(f"Found {len(results)} results from DuckDuckGo")
    for r in results[:5]:
        href = r.get('href', '')
        title = r.text.strip()
        if 'linkedin' in href.lower() or 'linkedin' in title.lower():
            print(f"  -> {title[:80]}")
            print(f"     {href[:120]}")

# Test 4: Try Exa with explicit date filter
print("\n=== TEST 4: Exa Search with startPublishedDate ===")
import subprocess
from datetime import datetime, timedelta
yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%dT00:00:00.000Z')
cmd = f'source ~/.agent-reach-venv/bin/activate && mcporter call exa.web_search_exa query=\'site:linkedin.com/posts "#hiring" "Project Manager"\' numResults=10 startPublishedDate=\'{yesterday}\' --output json'
result = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True)
if result.returncode == 0:
    try:
        data = json.loads(result.stdout)
        raw = ""
        if isinstance(data, dict) and 'content' in data:
            for c in data['content']:
                if c.get('type') == 'text':
                    raw += c.get('text', '')
        # Count results
        posts = [p for p in raw.split('---') if p.strip()]
        print(f"Exa returned {len(posts)} results with date filter set to after {yesterday}")
        for p in posts[:3]:
            title_m = re.search(r'Title:\s*(.+)', p)
            pub_m = re.search(r'Published:\s*(.+)', p)
            if title_m:
                print(f"  -> {title_m.group(1).strip()[:80]}")
                if pub_m:
                    print(f"     Published: {pub_m.group(1).strip()}")
    except:
        print(f"Error parsing Exa output")
else:
    print(f"Exa command failed: {result.stderr[:200]}")
