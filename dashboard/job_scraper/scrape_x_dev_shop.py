"""
scrape_x_dev_shop.py
======================================
ELITE TWITTER (X) SCRAPER BYPASS
Extracts software development / dev shop leads from X natively via DuckDuckGo Search.
"""

import re
import pandas as pd
from duckduckgo_search import DDGS
from datetime import datetime

def extract_emails(text):
    if not text: return ""
    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    return ", ".join(list(set(emails)))

def extract_salary(text):
    if not text: return ""
    range_pattern = r'([$£€₹]\s*\d+(?:[,.]\d+)*\s*[kKmM]?)\s*(?:-|to|—|–)\s*([$£€₹]?\s*\d+(?:[,.]\d+)*\s*[kKmM]?\s*(?:/hr|/yr|/hour|/year|/month|/mo|a year)?)'
    single_pattern = r'([$£€₹]\s*\d+(?:[,.]\d+)*\s*[kKmM]?\s*(?:/hr|/yr|/hour|/year|/month|/mo|a year))'
    
    rm = re.search(range_pattern, text, re.IGNORECASE)
    if rm: return f"{rm.group(1).strip()} - {rm.group(2).strip()}"
    sm = re.search(single_pattern, text, re.IGNORECASE)
    if sm: return sm.group(1).strip()
    return ""

def main():
    print("🚀 Initializing X (Twitter) Scraper for Dev Shop Leads...")
    
    queries = [
        'site:twitter.com "looking for a dev shop"',
        'site:x.com "looking for a dev shop"',
        'site:twitter.com "recommend an app developer"',
        'site:x.com "recommend an app developer"',
        'site:twitter.com "agency to build our MVP"',
        'site:x.com "agency to build our MVP"',
        'site:twitter.com "freelance web developer" ("looking for" OR "need")',
        'site:x.com "freelance web developer" ("looking for" OR "need")'
    ]
    
    results = []
    seen_urls = set()
    
    with DDGS() as ddgs:
        for q in queries:
            print(f"🔍 Searching: {q}")
            try:
                for r in ddgs.text(q, max_results=50, safesearch='off'):
                    url = r.get('href', '')
                    if url in seen_urls: continue
                    seen_urls.add(url)
                    
                    text = r.get('body', '')
                    title = r.get('title', '')
                    
                    author = "Unknown"
                    handle_match = re.search(r'twitter\.com/([^/]+)/', url) or re.search(r'x\.com/([^/]+)/', url)
                    if handle_match:
                        author = f"@{handle_match.group(1)}"
                    
                    results.append({
                        "Author (Handle)": author,
                        "Contact Email": extract_emails(text),
                        "Extracted Salary": extract_salary(text),
                        "Post Title": title,
                        "Post URL": url,
                        "Full Post Text": text,
                        "Scraped Date": datetime.now().strftime("%Y-%m-%d")
                    })
            except Exception as e:
                print(f"⚠️ Search failed for query: {q} - {e}")

    if results:
        df = pd.DataFrame(results)
        
        for col in df.select_dtypes(include=['object']).columns:
            df[col] = df[col].astype(str).replace('nan', '')
            df[col] = df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
        
        output_path = "job_scraper/linkedin_jobs/X_Dev_Shop_Leads.xlsx"
        df.to_excel(output_path, index=False)
        
        print(f"\\n✅ Successfully extracted {len(df)} dev shop leads from X (Twitter)!")
        print(f"💾 Saved to: {output_path}")
        
        print("\\n📊 Preview:")
        for index, row in df.head(5).iterrows():
            print(f"- {row['Author (Handle)']}: {row['Post Title'][:50]}... | Email: {row['Contact Email'] or 'None'}")
    else:
        print("⚠️ No posts found.")

if __name__ == "__main__":
    main()
