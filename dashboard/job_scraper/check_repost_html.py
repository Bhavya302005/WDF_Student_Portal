import pandas as pd
import requests
from bs4 import BeautifulSoup
import random

df = pd.read_excel('/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/Custom_URL_Project_Manager.xlsx')
urls = df['job_url'].dropna().tolist()

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

print(f"Checking 10 random job URLs for the word 'repost'...")
found_repost = False
for url in random.sample(urls, min(20, len(urls))):
    try:
        resp = requests.get(url, headers=headers)
        if "repost" in resp.text.lower():
            print(f"Found 'repost' in HTML for: {url}")
            found_repost = True
            # Let's see the context
            soup = BeautifulSoup(resp.text, 'html.parser')
            for tag in soup.find_all(text=lambda t: t and 'repost' in t.lower()):
                print(f"  -> Tag Context: {tag.parent}")
    except Exception as e:
        print(f"Error fetching {url}: {e}")

if not found_repost:
    print("None of the 20 random jobs had the word 'repost' anywhere in their HTML.")
