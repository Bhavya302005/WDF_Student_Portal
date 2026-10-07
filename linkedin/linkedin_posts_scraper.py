#!/usr/bin/env python3
"""
LinkedIn Recruiter Posts Scraper
================================
Scrapes LinkedIn Posts for 68 procurement/sourcing/buying roles.
This script uses Playwright to log in via a session cookie (`li_at`)
and searches the "Posts" tab for hiring signals.

Usage
-----
  # 1. Add your cookie to .env:
  # LINKEDIN_LI_AT=your_cookie_here

  # Basic (last 7 days):
  python3 linkedin/linkedin_posts_scraper.py

  # Only one role (for testing):
  python3 linkedin/linkedin_posts_scraper.py --role "Procurement Manager"

  # Limit roles:
  python3 linkedin/linkedin_posts_scraper.py --limit 5

Requirements
------------
  pip install playwright pandas python-dotenv
  playwright install chromium
"""

import sys
import os
import json
import csv
import time
import random
import hashlib
import asyncio
import logging
import argparse
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import pandas as pd
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError as exc:
    print(f"ERROR: Missing package. Run: pip install playwright pandas")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

LI_AT_COOKIE = os.getenv("LINKEDIN_LI_AT", "")

# We import ROLES from the existing file to keep it consistent
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from linkedin_68_roles import ROLES, sanitize, make_job_id
except ImportError:
    print("Could not import ROLES from linkedin_68_roles.py")
    sys.exit(1)

# -- Output paths --
OUTPUT_DIR = Path(__file__).parent / "linkedin_posts"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

JSON_FILE  = OUTPUT_DIR / "linkedin_68roles_posts.json"
CSV_FILE   = OUTPUT_DIR / "linkedin_68roles_posts.csv"
STATE_FILE = OUTPUT_DIR / "linkedin_68roles_posts_state.json"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("linkedin_posts")

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"completed_roles": [], "total_posts": 0, "seen_ids": []}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def append_to_disk(new_posts: list[dict]):
    if not new_posts:
        return
    existing = []
    if JSON_FILE.exists() and JSON_FILE.stat().st_size > 0:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    existing.extend(new_posts)
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
    df = pd.DataFrame(new_posts)
    df.to_csv(CSV_FILE, mode="a", header=write_header, index=False, quoting=csv.QUOTE_NONNUMERIC, escapechar="\\")

def is_recruiter_title(title: str) -> bool:
    """Filter to ensure the post author is likely HR, Recruiting, or a Manager hiring."""
    if not title:
        return False
    t = title.lower()
    keywords = ["recruiter", "talent", "acquisition", "hr", "human resources", "people", "hiring", "manager", "director", "head", "vp"]
    return any(k in t for k in keywords)

async def auto_scroll(page: Page, max_scrolls: int = 15):
    for i in range(max_scrolls):
        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(2.0, 3.5))
        new_height = await page.evaluate("document.body.scrollHeight")
        
        # Click "Show more results" if it exists
        try:
            load_more = await page.query_selector("button.scaffold-finite-scroll__load-button")
            if load_more:
                await load_more.click()
                await asyncio.sleep(random.uniform(2.0, 3.0))
        except:
            pass
            
        if new_height == prev_height:
            break

async def scrape_role_posts(page: Page, role: str, global_seen_ids: set, hr_only: bool) -> list[dict]:
    log.info(f"  Scraping posts for: '{role}'")
    
    # LinkedIn Search URL for Posts (past week)
    query = f"hiring {role}"
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.linkedin.com/search/results/content/?datePosted=%22past-week%22&keywords={encoded_query}&sortBy=%22date_posted%22"
    
    await page.goto(url)
    await asyncio.sleep(random.uniform(3, 5))
    
    # Check for login wall or empty results
    if "login" in page.url or "checkpoint" in page.url:
        log.error("  Redirected to login. Your li_at cookie may be invalid or expired.")
        return []

    log.info("  Scrolling to load posts...")
    await auto_scroll(page)
    
    posts = []
    
    # Extract post containers
    # Note: LinkedIn DOM classes change frequently. Using generic semantic selectors where possible.
    post_elements = await page.query_selector_all("div.feed-shared-update-v2")
    
    log.info(f"  Found {len(post_elements)} post elements on page.")
    
    for el in post_elements:
        try:
            # Author Name
            author_el = await el.query_selector(".update-components-actor__name")
            author_name = await author_el.inner_text() if author_el else "Unknown"
            author_name = author_name.split("\\n")[0].strip() # Clean up newlines if any
            
            # Author Title/Headline
            title_el = await el.query_selector(".update-components-actor__description")
            author_title = await title_el.inner_text() if title_el else ""
            author_title = author_title.strip()
            
            if hr_only and not is_recruiter_title(author_title):
                continue
            
            # Post Text
            text_el = await el.query_selector(".update-components-text")
            if not text_el:
                continue
            
            # Click "see more" if present to get full text
            see_more = await text_el.query_selector("button")
            if see_more:
                try:
                    await see_more.click()
                    await asyncio.sleep(0.5)
                except:
                    pass
                    
            post_text = await text_el.inner_text()
            post_text = post_text.strip()
            
            # Author URL
            link_el = await el.query_selector("a.update-components-actor__container-link")
            author_url = await link_el.get_attribute("href") if link_el else ""
            if author_url and author_url.startswith("/"):
                author_url = "https://www.linkedin.com" + author_url.split("?")[0]
                
            # Post Date (usually like "2d" or "1w")
            date_el = await el.query_selector("span.update-components-actor__sub-description")
            posted_at = await date_el.inner_text() if date_el else ""
            posted_at = posted_at.split("•")[0].strip() if "•" in posted_at else posted_at.strip()
            
            # Post URL (from the 'copy link to post' menu or timestamp link, tricky on feed)
            # Often the timestamp is a link to the post itself
            post_link_el = await el.query_selector("a.update-components-actor__sub-description-link")
            post_url = await post_link_el.get_attribute("href") if post_link_el else ""
            if post_url and post_url.startswith("/"):
                post_url = "https://www.linkedin.com" + post_url.split("?")[0]
            
            # Deduplication
            # Hash of author name + first 50 chars of text
            dedup_str = f"{author_name.lower()}:{post_text[:50].lower()}"
            post_id = hashlib.md5(dedup_str.encode()).hexdigest()
            
            if post_id in global_seen_ids:
                continue
            global_seen_ids.add(post_id)
            
            post_data = {
                "id": post_id,
                "search_role": role,
                "author_name": author_name,
                "author_title": author_title,
                "author_profile_url": author_url,
                "post_text": post_text,
                "post_url": post_url,
                "posted_at_relative": posted_at,
                "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            }
            
            posts.append(post_data)
        except Exception as e:
            log.debug(f"  Error parsing a post: {e}")
            continue
            
    log.info(f"  Extracted {len(posts)} new relevant posts for '{role}'")
    return posts

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", type=str, help="Single role to scrape")
    parser.add_argument("--limit", type=int, help="Limit number of roles")
    parser.add_argument("--all-authors", action="store_true", help="Don't filter authors by HR/Recruiter titles")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    
    if not LI_AT_COOKIE:
        log.error("Missing LINKEDIN_LI_AT in .env! Cannot scrape LinkedIn posts without authentication.")
        log.error("Please add LINKEDIN_LI_AT=your_cookie_here to your .env file.")
        return

    roles_to_run = ROLES
    if args.role:
        roles_to_run = [r for r in ROLES if args.role.lower() in r.lower()]
    if args.limit:
        roles_to_run = roles_to_run[:args.limit]
        
    state = load_state() if args.resume else {"completed_roles": [], "total_posts": 0, "seen_ids": []}
    completed = set(state.get("completed_roles", []))
    global_seen_ids = set(state.get("seen_ids", []))
    grand_total = state.get("total_posts", 0)
    
    hr_only = not args.all_authors

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        
        # Inject auth cookie
        await context.add_cookies([{
            "name": "li_at",
            "value": LI_AT_COOKIE,
            "domain": ".linkedin.com",
            "path": "/"
        }])
        
        page = await context.new_page()
        
        # Verify login
        await page.goto("https://www.linkedin.com/feed/")
        await asyncio.sleep(3)
        if "login" in page.url or "checkpoint" in page.url:
            log.error("Login verification failed! Your li_at cookie is likely invalid/expired.")
            await browser.close()
            return
        log.info("Successfully authenticated with LinkedIn.")

        for idx, role in enumerate(roles_to_run, 1):
            if role in completed:
                log.info(f"[{idx}/{len(roles_to_run)}] Skipping '{role}' - already done.")
                continue
                
            log.info(f"\n[{idx}/{len(roles_to_run)}] Processing '{role}'")
            
            try:
                posts = await scrape_role_posts(page, role, global_seen_ids, hr_only)
            except Exception as e:
                log.error(f"Error scraping {role}: {e}")
                posts = []
                
            if posts:
                append_to_disk(posts)
                grand_total += len(posts)
                
            completed.add(role)
            state["completed_roles"] = list(completed)
            state["total_posts"] = grand_total
            state["seen_ids"] = list(global_seen_ids)
            save_state(state)
            
            # Anti-ban delay
            delay = random.uniform(20, 45)
            log.info(f"Sleeping {delay:.1f}s before next role...")
            await asyncio.sleep(delay)

        await browser.close()
        
    log.info("\nDONE!")
    log.info(f"Total posts collected: {grand_total}")
    log.info(f"Saved to {JSON_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
