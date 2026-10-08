import os
import csv
import asyncio
import httpx
from httpx import Limits
import sys

async def check_slug(client, semaphore, slug, valid_file):
    url = f"https://jobs.jobvite.com/{slug}"
    async with semaphore:
        try:
            # use timeout=10 to be safe
            response = await client.get(url, follow_redirects=True, timeout=10.0)
            if response.status_code == 200 and 'support' not in str(response.url).lower() and 'search' not in str(response.url).lower():
                # Valid!
                print(f"[VALID] {slug} -> {response.url}")
                with open(valid_file, "a") as f:
                    f.write(slug + "\n")
                return slug
        except Exception:
            pass
    return None

async def main():
    dir_path = '/Users/AminBhavya/Downloads/ats-scrapers-main/ats-scrapers-main/ats-companies'
    valid_file = '/Users/AminBhavya/Downloads/all_ 2/jobvite/jobvite_companies.txt'
    
    # load existing to skip
    existing = set()
    with open(valid_file, 'r') as f:
        for line in f:
            if line.strip() and not line.startswith('#'):
                existing.add(line.strip().lower())

    slugs = set()
    for file in os.listdir(dir_path):
        if file.endswith('.csv'):
            with open(os.path.join(dir_path, file), 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    comp = row.get('slug') or row.get('Company') or row.get('company')
                    if comp:
                        c = comp.strip().lower()
                        if c not in existing:
                            slugs.add(c)
    
    slug_list = list(slugs)
    print(f"Loaded {len(slug_list)} unique candidates to test for Jobvite...")
    
    semaphore = asyncio.Semaphore(100) # conservative to prevent dropping connections
    limits = Limits(max_connections=200, max_keepalive_connections=200)
    
    valid_count = 0
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [check_slug(client, semaphore, slug, valid_file) for slug in slug_list]
        for i, coro in enumerate(asyncio.as_completed(tasks)):
            res = await coro
            if res:
                valid_count += 1
            if (i+1) % 500 == 0:
                print(f"Progress: {i+1}/{len(slug_list)} tested. Found {valid_count} new valid Jobvite companies.")

    print(f"Finished discovery. Added {valid_count} new valid Jobvite companies.")

if __name__ == '__main__':
    asyncio.run(main())
