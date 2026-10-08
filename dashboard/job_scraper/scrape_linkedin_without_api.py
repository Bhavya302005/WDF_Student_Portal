import re
import pandas as pd
from duckduckgo_search import DDGS
from datetime import datetime

def extract_emails(text):
    if not text: return ""
    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    return ", ".join(list(set(emails)))

def main():
    print("🚀 Initializing 'Exa-Bypass' LinkedIn Scraper (via DuckDuckGo)...")
    
    # We use DuckDuckGo with strict site: operators to replicate Exa's search capability without an API key
    queries = [
        'site:linkedin.com/posts/ "SharePoint developer" "looking for"',
        'site:linkedin.com/posts/ "SharePoint consultant" "looking for"',
        'site:linkedin.com/posts/ "SharePoint migration" "help"',
        'site:linkedin.com/posts/ "recommend" "SharePoint agency"',
        'site:linkedin.com/posts/ "SharePoint implementation partner"'
    ]
    
    results = []
    seen_urls = set()
    
    with DDGS() as ddgs:
        for q in queries:
            print(f"🔍 Searching: {q} (Time limit: Past Month)")
            try:
                # timelimit='m' restricts results to the past month to ensure we grab the "latest" posts
                for r in ddgs.text(q, max_results=30, safesearch='off', timelimit='m'):
                    url = r.get('href', '')
                    if url in seen_urls: continue
                    seen_urls.add(url)
                    
                    text = r.get('body', '')
                    title = r.get('title', '')
                    
                    # Extract Author from Title (usually "Author Name on LinkedIn: ...")
                    author = "Unknown"
                    if " on LinkedIn" in title:
                        author = title.split(" on LinkedIn")[0].strip()
                    elif " - LinkedIn" in title:
                        author = title.split(" - LinkedIn")[0].strip()
                    
                    results.append({
                        "Author": author,
                        "Contact Email": extract_emails(text),
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
        
        output_path = "job_scraper/LinkedIn_Dev_Shop_Leads.xlsx"
        df.to_excel(output_path, index=False)
        
        import json
        with open("job_scraper/LinkedIn_Dev_Shop_Leads.json", "w") as f:
            json.dump(results, f, indent=4)
        
        print(f"\n✅ Successfully bypassed Exa and extracted {len(df)} latest LinkedIn posts!")
        print(f"💾 Saved to: {output_path}")
        
        print("\n📊 Preview:")
        for index, row in df.head(3).iterrows():
            print(f"- {row['Author']}: {row['Post Title'][:50]}... | URL: {row['Post URL']}")
    else:
        print("⚠️ No fresh LinkedIn posts found matching those exact queries in the past month.")

if __name__ == "__main__":
    main()
