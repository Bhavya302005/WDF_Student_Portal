"""
scrape_x_ai_pm.py
======================================
ELITE TWITTER (X) SCRAPER BYPASS
Bypasses Twitter login/API limits by scraping X posts natively via DuckDuckGo Search.
Extracts: Recruiter Handle, Post Text, URL, Date, Salary, and Email.
"""

import re
import pandas as pd
from duckduckgo_search import DDGS
from datetime import datetime

# Regex from our Exa script
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
    print("🚀 Initializing Elite X (Twitter) Scraper Bypass...")
    
    # We use very specific dorks to find hiring posts for AI Project Managers
    queries = [
        'site:twitter.com "AI Project Manager" (hiring OR "looking for" OR "join us" OR "we are hiring")',
        'site:x.com "AI Project Manager" (hiring OR "looking for" OR "join us" OR "we are hiring")',
        'site:twitter.com "AI PM" (hiring OR "looking for")',
        'site:x.com "AI PM" (hiring OR "looking for")'
    ]
    
    results = []
    seen_urls = set()
    
    with DDGS() as ddgs:
        for q in queries:
            print(f"🔍 Searching: {q}")
            try:
                # We pull top 50 results per query
                for r in ddgs.text(q, max_results=50, safesearch='off'):
                    url = r.get('href', '')
                    if url in seen_urls: continue
                    seen_urls.add(url)
                    
                    text = r.get('body', '')
                    title = r.get('title', '')
                    
                    # Extract Twitter Handle
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
        
        # Clean string columns for Excel
        for col in df.select_dtypes(include=['object']).columns:
            df[col] = df[col].astype(str).replace('nan', '')
            df[col] = df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
        
        output_path = "job_scraper/linkedin_jobs/X_AI_Project_Manager_Posts.xlsx"
        df.to_excel(output_path, index=False)
        
        print(f"\\n✅ Successfully extracted {len(df)} hiring posts from X (Twitter)!")
        print(f"💾 Saved to: {output_path}")
        
        print("\\n📊 Preview:")
        for index, row in df.head(5).iterrows():
            print(f"- {row['Author (Handle)']}: {row['Post Title'][:50]}... | Email: {row['Contact Email'] or 'None'}")
    else:
        print("⚠️ No posts found.")

if __name__ == "__main__":
    main()
