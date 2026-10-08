import os
import time
import json
import datetime
import pandas as pd
import re
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from dotenv import load_dotenv

# Load the user's specific _env file
load_dotenv("/Users/AminBhavya/Ai_Sales_Agent/_env")

def extract_emails(text):
    if not text: return ""
    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    return ", ".join(list(set(emails)))

def scrape_linkedin():
    print("🚀 Initializing Custom Authenticated LinkedIn Playwright Scraper...")

    li_at = os.getenv("LI_AT_COOKIE")
    jsessionid = os.getenv("LI_JSESSIONID")
    
    if not li_at or not jsessionid:
        raise RuntimeError("Set LI_AT_COOKIE and LI_JSESSIONID in _env before running")
        
    # Strip quotes from JSESSIONID if present
    jsessionid = jsessionid.strip('"')
    
    cookies = [
        {
            "name": "li_at",
            "value": li_at,
            "domain": ".www.linkedin.com",
            "path": "/"
        },
        {
            "name": "JSESSIONID",
            "value": f'"{jsessionid}"' if not jsessionid.startswith('"') else jsessionid,
            "domain": ".www.linkedin.com",
            "path": "/"
        }
    ]

    # Using SharePoint-specific unquoted keywords to match LinkedIn's corporate phrasing
    queries = [
        'hiring SharePoint consultant',
        'looking for SharePoint developer',
        'SharePoint migration help',
        'recommend SharePoint agency',
        'need SharePoint implementation partner'
    ]

    posts_data = []
    seen_posts = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        context.add_cookies(cookies)
        
        page = context.new_page()
        Stealth().apply_stealth_sync(page) # Apply stealth to bypass LinkedIn's anti-bot detection
        
        for query in queries:
            encoded_query = query.replace(" ", "%20").replace('"', '%22')
            # datePosted="past-month" and sorted by latest
            search_url = f"https://www.linkedin.com/search/results/content/?datePosted=%22past-month%22&keywords={encoded_query}&sortBy=%22date_posted%22"
            
            print(f"\n🔗 Navigating to LinkedIn Search: {query}")
            page.goto(search_url, timeout=60000)
            
            try:
                # Wait for the feed to load
                page.wait_for_selector('.feed-shared-update-v2', timeout=15000)
            except Exception:
                page.screenshot(path="linkedin_timeout.png")
                with open("linkedin_timeout.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print("⚠️ Timeout waiting for posts. Saved visual debugger to linkedin_timeout.png and linkedin_timeout.html")
                break
            
            print("📜 Scrolling to load data...")
            
            # Scroll to load dynamic content
            for _ in range(5):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2.5)
                
                articles = page.query_selector_all('.feed-shared-update-v2')
                for article in articles:
                    try:
                        # Extract text
                        text_elem = article.query_selector('.break-words')
                        text = text_elem.inner_text().strip() if text_elem else ""
                        
                        if text and text not in seen_posts:
                            seen_posts.add(text)
                            
                            # Extract author
                            author_elem = article.query_selector('.update-components-actor__name')
                            author = author_elem.inner_text().strip() if author_elem else "Unknown"
                            
                            # Extract post URL if available
                            link_elem = article.query_selector('a.app-aware-link')
                            post_url = link_elem.get_attribute('href') if link_elem else search_url
                            if post_url and "?" in post_url and "linkedin.com/search" not in post_url:
                                post_url = post_url.split("?")[0]
                                
                            posts_data.append({
                                "Author": author,
                                "Contact Email": extract_emails(text),
                                "Post URL": post_url,
                                "Full Post Text": text,
                                "Platform": "LinkedIn"
                            })
                    except Exception:
                        pass
        
        print(f"\n✅ Successfully scraped {len(posts_data)} distinct LinkedIn posts!")
        
        if posts_data:
            df = pd.DataFrame(posts_data)
            output_excel = "job_scraper/LinkedIn_Playwright_Dev_Shop.xlsx"
            output_json = "job_scraper/LinkedIn_Playwright_Dev_Shop.json"
            
            # Clean string columns for Excel
            for col in df.select_dtypes(include=['object']).columns:
                df[col] = df[col].astype(str).replace('nan', '')
                df[col] = df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
            
            df.to_excel(output_excel, index=False)
            with open(output_json, "w") as f:
                json.dump(posts_data, f, indent=4)
            print(f"💾 Saved to {output_excel} and {output_json}")
            
            print("\n📊 Preview:")
            for index, row in df.head(3).iterrows():
                print(f"- {row['Author']}: {row['Full Post Text'][:50]}...")
        else:
            print("⚠️ No posts found for any of the queries.")
            
        browser.close()

if __name__ == "__main__":
    scrape_linkedin()
