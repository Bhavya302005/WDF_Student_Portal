import os
import time
import json
import datetime
import pandas as pd
from playwright.sync_api import sync_playwright

def scrape_twitter():
    print("🚀 Initializing Custom MIT-Grade Twitter Playwright Scraper...")

    auth_token = os.getenv("X_AUTH_TOKEN")
    csrf_token = os.getenv("X_CT0")
    if not auth_token or not csrf_token:
        raise RuntimeError("Set X_AUTH_TOKEN and X_CT0 outside source control before running")
    
    cookies = [
        {
            "name": "auth_token",
            "value": auth_token,
            "domain": ".x.com",
            "path": "/"
        },
        {
            "name": "ct0",
            "value": csrf_token,
            "domain": ".x.com",
            "path": "/"
        }
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        context.add_cookies(cookies)
        
        page = context.new_page()
        # Build a natural query exactly like a user would type in the search bar
        import datetime
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        two_weeks_ago_str = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime('%Y-%m-%d')
        query = f'("SharePoint developer" OR "SharePoint consultant" OR "SharePoint migration" OR "SharePoint agency") ("looking for" OR "recommend" OR "hiring" OR "need help") until:{today_str} since:{two_weeks_ago_str}'
        encoded_query = query.replace(" ", "%20").replace('"', '%22').replace('#', '%23')
        search_url = f"https://x.com/search?q={encoded_query}&src=typed_query&f=live"
        
        print(f"🔗 Navigating to Twitter Search: {search_url}")
        
        page.goto(search_url, timeout=60000)
        
        print("⏳ Waiting for tweets to load...")
        try:
            page.wait_for_selector('article', timeout=15000)
        except Exception:
            print("⚠️ Timeout waiting for tweets. The cookies might be invalid or there are no results.")
        
        # Scroll to load more tweets
        print("📜 Scrolling to load more data...")
        tweets_data = []
        seen_tweets = set()
        
        for _ in range(30):
            articles = page.query_selector_all('article')
            for article in articles:
                try:
                    text_element = article.query_selector('div[data-testid="tweetText"]')
                    text = text_element.inner_text() if text_element else ""
                    
                    if text and text not in seen_tweets:
                        seen_tweets.add(text)
                        
                        # Try to get author and URL
                        links = article.query_selector_all('a[role="link"]')
                        tweet_url = ""
                        author = ""
                        for link in links:
                            href = link.get_attribute('href')
                            if href and '/status/' in href:
                                tweet_url = f"https://x.com{href}"
                                author = href.split('/')[1]
                                break
                                
                        # Try to get the post time
                        time_element = article.query_selector('time')
                        post_time = time_element.get_attribute('datetime') if time_element else "Unknown"
                        is_recent = True
                        if post_time != "Unknown":
                            try:
                                dt = datetime.datetime.fromisoformat(post_time.replace('Z', '+00:00'))
                                now = datetime.datetime.now(datetime.timezone.utc)
                                diff = now - dt
                                seconds = diff.total_seconds()
                                
                                # Temporarily disabled the 14-day filter so we can see when the actual latest post was
                                # if seconds > 14 * 86400:
                                #     is_recent = False
                                
                                if seconds < 60:
                                    post_time = f"{int(seconds)} seconds ago"
                                elif seconds < 3600:
                                    post_time = f"{int(seconds // 60)} minutes ago"
                                elif seconds < 86400:
                                    post_time = f"{int(seconds // 3600)} hours ago"
                                else:
                                    post_time = f"{int(seconds // 86400)} days ago"
                            except Exception:
                                pass
                        
                        if is_recent:
                            tweets_data.append({
                                "Author": author,
                                "Post URL": tweet_url,
                                "Posted Time": post_time,
                                "Full Post Text": text,
                                "Platform": "Twitter/X"
                            })
                except Exception:
                    pass
            
            # Scroll down
            page.evaluate("window.scrollBy(0, 2000)")
            time.sleep(2)
            
        print(f"✅ Successfully scraped {len(tweets_data)} distinct tweets!")
        
        if tweets_data:
            df = pd.DataFrame(tweets_data)
            df.to_excel("Twitter_Software_Dev_Interns.xlsx", index=False)
            with open("Twitter_Software_Dev_Interns.json", "w") as f:
                json.dump(tweets_data, f, indent=4)
            print("💾 Saved to Twitter_Software_Dev_Interns.xlsx and .json")
        
        browser.close()

if __name__ == "__main__":
    scrape_twitter()
