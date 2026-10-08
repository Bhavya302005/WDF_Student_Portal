#!/usr/bin/env python3
import argparse
import csv
import logging
import sys
import time
import random
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from bs4 import BeautifulSoup

# Use the same robust session creator from JobSpy
from jobspy.util import create_session

def setup_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("IndeedClaimChecker")
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
    return logger

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check if Indeed companies are unclaimed from a JobSpy CSV.")
    parser.add_argument("--input", type=Path, required=True, help="Input CSV file from JobSpy.")
    parser.add_argument("--output", type=Path, required=True, help="Output CSV file path.")
    parser.add_argument("--proxies", type=str, nargs="+", help="List of proxies to use (e.g. http://proxy.com:8080)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")
    return parser.parse_args()

def check_if_unclaimed(url: str, session, logger) -> bool:
    """Visits the company URL and checks if it's unclaimed."""
    try:
        response = session.get(url, timeout=10)
        
        # Check if we got blocked by Cloudflare
        if response.status_code != 200:
            logger.warning(f"Failed to fetch {url} (Status: {response.status_code})")
            if "Blocked" in response.text or response.status_code in [403, 429]:
                logger.error("You are being blocked by Indeed's Cloudflare. Proxies are required.")
            return False

        soup = BeautifulSoup(response.text, "html.parser")
        text_content = soup.get_text().lower()
        
        # Look for standard Indeed unclaimed indicators
        indicators = [
            "claim this company page",
            "claim this company profile",
            "/cmp/_/claim"
        ]
        
        # Also check for explicit links to the claim page
        for link in soup.find_all('a', href=True):
            if '/cmp/_/claim' in link['href'] or 'claim' in link.text.lower():
                return True
                
        for indicator in indicators:
            if indicator in text_content or indicator in response.text:
                return True
                
        return False
        
    except Exception as e:
        logger.error(f"Error checking {url}: {e}")
        return False

def main():
    args = parse_arguments()
    logger = setup_logger(args.verbose)
    
    if not args.input.exists():
        logger.error(f"Input file {args.input} does not exist.")
        sys.exit(1)
        
    logger.info(f"Loading jobs from {args.input}...")
    df = pd.read_csv(args.input)
    
    if "company_url" not in df.columns or "site" not in df.columns:
        logger.error("CSV must contain 'company_url' and 'site' columns from JobSpy.")
        sys.exit(1)
        
    # Filter for Indeed jobs that have a company URL
    indeed_jobs = df[(df["site"] == "indeed") & (df["company_url"].notna())]
    unique_urls = indeed_jobs["company_url"].unique()
    
    logger.info(f"Found {len(unique_urls)} unique Indeed company URLs to check.")
    
    # Create the session with proxies if provided
    session = create_session(proxies=args.proxies, is_tls=False)
    
    # Cache to avoid checking the same company multiple times
    claimed_status_cache = {}
    
    # Iterate through unique URLs
    for idx, url in enumerate(unique_urls):
        logger.info(f"Checking [{idx+1}/{len(unique_urls)}]: {url}")
        
        # Normalize URL to ensure it's absolute
        if not url.startswith("http"):
            url = f"https://www.indeed.com{url}"
            
        is_unclaimed = check_if_unclaimed(url, session, logger)
        claimed_status_cache[url] = is_unclaimed
        
        if is_unclaimed:
            logger.info(f" ---> UNCLAIMED COMPANY FOUND: {url}")
        else:
            logger.debug(f" ---> Claimed: {url}")
            
        # Polite delay to avoid rapid bans
        time.sleep(random.uniform(2.0, 4.0))
        
    # Map the results back to the dataframe
    df["is_unclaimed"] = df.apply(
        lambda row: claimed_status_cache.get(row["company_url"], False) 
        if row["site"] == "indeed" and pd.notna(row["company_url"]) else None, 
        axis=1
    )
    
    # Save the updated CSV
    logger.info(f"Saving updated data to {args.output}...")
    df.to_csv(
        args.output, 
        quoting=csv.QUOTE_NONNUMERIC, 
        escapechar="\\", 
        index=False
    )
    logger.info("Finished successfully!")

if __name__ == "__main__":
    main()
