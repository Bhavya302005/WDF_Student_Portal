import time
import json
import re
import pandas as pd
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def main():
    print("🚀 Initializing Advanced Playwright HTML Extraction for Naukri...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        
        print("🌍 Navigating to Naukri (fully rendering page)...")
        page.goto("https://www.naukri.com/project-manager-jobs", wait_until="networkidle")
        time.sleep(3)
        
        html = page.content()
        browser.close()
        
    print(f"✅ Extracted {len(html)} bytes of fully rendered HTML!")
    
    # Naukri embeds the actual job data in a JS variable or JSON inside a script tag.
    # Let's try to extract the main JSON state from the rendered HTML.
    
    # Try finding __PRELOADED_STATE__ or similar JSON data
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.find_all('script')
    
    job_details = []
    
    # Method 1: Look for jobDetails array in any script
    for s in scripts:
        if s.string and "jobDetails" in s.string:
            try:
                # Use regex to find the array assigned to jobDetails
                match = re.search(r'\"jobDetails\":(\[.*?\])', s.string)
                if match:
                    job_details = json.loads(match.group(1))
                    break
            except Exception:
                pass
                
    if not job_details:
        print("⚠️ Could not find jobDetails array in scripts. Trying to parse raw HTML cards...")
        cards = soup.select(".jobTuple, .srp-jobtuple-wrapper")
        print(f"Found {len(cards)} raw HTML job cards.")
        
        for card in cards:
            title_el = card.select_one(".title")
            company_el = card.select_one(".comp-name")
            if title_el and company_el:
                job_details.append({
                    "title": title_el.text.strip(),
                    "companyName": company_el.text.strip(),
                    "jobUrl": title_el.get("href", "")
                })
    
    if job_details:
        print(f"🎉 Successfully extracted {len(job_details)} jobs!")
        df = pd.DataFrame(job_details)
        print(df.head())
    else:
        print("❌ Failed to extract any jobs from the rendered page. WAF might be blocking Playwright, or the structure has completely changed.")

if __name__ == "__main__":
    main()
