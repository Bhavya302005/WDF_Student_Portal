import os
import re
import json
import subprocess
import pandas as pd
from datetime import datetime, timedelta

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

def extract_time(text, exa_published=""):
    if exa_published and exa_published != "N/A":
        # Sometimes Exa returns ISO strings like 2026-05-12T00:00:00.000Z
        return exa_published.split('T')[0]
    
    # Clean hidden RTL/LTR marks that appear in foreign text
    text_clean = text.replace('\u200e', '').replace('\u200f', '')
    
    # Try to find relative time in the text (English + Hebrew)
    time_pattern = r'(?:לפני\s*)?(\d+)\s*(minute|hour|day|week|month|year|דקות|שעות|ימים|שבועות|חודשים|שנה|שנים)s?(?:\s+ago)?'
    match = re.search(time_pattern, text_clean, re.IGNORECASE)
    if match:
        val = match.group(1)
        unit = match.group(2).lower()
        
        if unit == 'דקות': unit = 'minute'
        elif unit == 'שעות': unit = 'hour'
        elif unit == 'ימים': unit = 'day'
        elif unit == 'שבועות': unit = 'week'
        elif unit == 'חודשים': unit = 'month'
        elif unit in ['שנה', 'שנים']: unit = 'year'
        
        return f"{val} {unit}{'s' if int(val) > 1 else ''} ago"
        
    return "Unknown"

def is_recent(time_str):
    if not time_str or time_str.lower() == "unknown": 
        return False # Drop unknowns to guarantee freshness
    time_str = time_str.lower()
    if "month" in time_str or "year" in time_str:
        return False
    if "week" in time_str:
        # Drop anything 3 weeks or older (we want 14 days / 2 weeks max)
        if any(x in time_str for x in ["3", "4", "5"]):
            return False
    
    if re.match(r'\d{4}-\d{2}-\d{2}', time_str):
        try:
            post_date = datetime.strptime(time_str[:10], '%Y-%m-%d')
            if (datetime.now() - post_date).days > 14:
                return False
        except:
            pass
    return True

def main():
    print("🚀 Initializing Exa Job Scraper (MCP Exa Backend - No API Key Required)...")
    
    cities = ["Pune", "Delhi", "Chennai", "Noida", "Bangalore"]
    
    base_queries = [
        "site:linkedin.com/jobs/view/ 'AI Product Manager'",
        "site:lever.co OR site:boards.greenhouse.io OR site:jobs.ashbyhq.com 'AI Product Manager'",
        "site:linkedin.com/posts/ 'AI Product Manager' (hiring OR looking for)",
        "site:indeed.com/viewjob 'AI Product Manager'",
        "site:myworkdayjobs.com 'AI Product Manager'",
        "site:wellfound.com/jobs/ OR site:glassdoor.com/job-listing 'AI Product Manager'",
        "site:smartrecruiters.com OR site:breezy.hr 'AI Product Manager'"
    ]
    
    queries = []
    for city in cities:
        for bq in base_queries:
            queries.append(f"{bq} {city}")
    
    extracted_data = []
    seen_urls = set()
    
    for query in queries:
        print(f"🔍 Searching Exa via MCP for: {query}")
        
        two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        
        args = {
            "query": query,
            "numResults": 50, # Keep to 50 since we have 35 distinct queries now
            "startPublishedDate": two_weeks_ago,
            "objective": "Find recent posts from recruiters or companies hiring an AI Product Manager"
        }
        
        cmd = [
            "mcporter", "call", "--output", "json",
            "--args", json.dumps(args),
            "exa.web_search_exa"
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            output_json = json.loads(result.stdout)
            
            for block in output_json.get("content", []):
                if block.get("type") == "text":
                    text_content = block.get("text", "")
                    posts = text_content.split('\n---\n')
                    
                    for post_text in posts:
                        if not post_text.strip(): continue
                        if "No search results found" in post_text:
                            continue
                        
                        author = "Unknown"
                        title = "Job Post"
                        url = ""
                        published = ""
                        
                        lines = post_text.split('\n')
                        for line in lines:
                            if line.startswith('Title: '):
                                title = line.replace('Title: ', '').strip()
                            elif line.startswith('URL: '):
                                url = line.replace('URL: ', '').strip()
                            elif line.startswith('Published: '):
                                published = line.replace('Published: ', '').strip()
                        
                        # Filter out aggregator/search pages
                        lower_url = url.lower()
                        if any(x in lower_url for x in ['/search', '/q-', 'jobs-worldwide', '/jobs?', '/jobs/product', 'jobs-in-', 'jobs-at-']):
                            continue
                            
                        if url in seen_urls: continue
                        if url: seen_urls.add(url)
                        
                        # Identify source
                        handle_match = re.search(r'twitter\.com/([^/]+)/', url) or re.search(r'x\.com/([^/]+)/', url)
                        if handle_match:
                            author = f"@{handle_match.group(1)}"
                        elif 'linkedin.com/posts/' in url:
                            author = "LinkedIn Recruiter Post"
                        else:
                            domain_match = re.search(r'https?://(?:www\.)?([^/]+)', url)
                            if domain_match:
                                author = f"Source: {domain_match.group(1)}"
                        
                        posted_time = extract_time(post_text, published)
                        
                        if not is_recent(posted_time):
                            continue
                            
                        extracted_data.append({
                            "Author (Source)": author,
                            "Contact Email": extract_emails(post_text),
                            "Extracted Salary": extract_salary(post_text),
                            "Posted Time": posted_time,
                            "Post Title": title,
                            "Post URL": url,
                            "Full Post Text": post_text[:2000],
                            "Scraped Date": datetime.now().strftime("%Y-%m-%d")
                        })
        except Exception as e:
            print(f"❌ Exa MCP search failed for query: {query}. Error: {e}")
            
    if extracted_data:
        df = pd.DataFrame(extracted_data)
        
        for col in df.select_dtypes(include=['object']).columns:
            df[col] = df[col].astype(str).replace('nan', '')
            df[col] = df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
        
        output_excel = "X_AI_Product_Manager_Posts_Exa.xlsx"
        output_json = "X_AI_Product_Manager_Posts_Exa.json"
        
        df.to_excel(output_excel, index=False)
        df.to_json(output_json, orient='records', indent=4)
        
        print(f"\n✅ Successfully extracted {len(df)} AI Product Manager posts via Exa MCP!")
        print(f"💾 Saved Excel to: {output_excel}")
        print(f"💾 Saved JSON to: {output_json}")
        
        print("\n📊 Preview:")
        for index, row in df.head(5).iterrows():
            print(f"- {row['Author (Source)']}: {row['Post Title'][:50]}... | Time: {row['Posted Time']}")
    else:
        print("⚠️ No posts found using Exa.")

if __name__ == "__main__":
    main()
