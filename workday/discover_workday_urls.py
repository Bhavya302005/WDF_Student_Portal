import asyncio
import csv
import os
import re
import httpx
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TXT_FILE = os.path.join(BASE_DIR, "workday_companies.txt")
CSV_FILE = os.path.join(BASE_DIR, "workday.csv")

# Common Workday clusters
INSTANCES = ["wd1", "wd3", "wd5", "wd103", "wd104", "wd2", "wd4"]

# Common site path suffixes to try when probing
SITE_PATHS = ["external", "careers", "External", "Careers", "jobs", "ExternalCareerSite",
              "External_Career_Site", "en-US/External", "en-US/Careers", "en-US/External_Careers"]

def load_existing() -> set:
    """Return a set of cleaned URL-slugs (Workday subdomains) already in workday.csv.
    These are derived from the 'slug' column (e.g. '7eleven/7eleven' → '7eleven'),
    which matches what clean_slug() produces from the txt entries."""
    if not os.path.exists(CSV_FILE):
        return set()
    existing = set()
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # slug column: "7eleven/7eleven" or "3m/search" → take the subdomain part
            slug_col = (row.get("slug") or "").strip().lower()
            if slug_col:
                existing.add(slug_col.split("/")[0])
            # also index by cleaned name as fallback
            name = (row.get("name") or "").strip().lower()
            if name:
                existing.add(re.sub(r'[^a-z0-9]', '', name))
    return existing

def clean_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r'[^a-z0-9]', '', s)
    return s

async def probe_company(client: httpx.AsyncClient, name: str, semaphore: asyncio.Semaphore) -> tuple[str, str, str] | None:
    slug = clean_slug(name)
    if not slug:
        return None

    async def try_url(url: str) -> str | None:
        try:
            async with semaphore:
                resp = await client.get(url, follow_redirects=True)
            final = str(resp.url)
            if resp.status_code == 200 and "myworkdayjobs" in final.lower():
                return final
        except Exception:
            pass
        return None

    # Try each instance × site-path combination until one responds with 200
    for inst in INSTANCES:
        base = f"https://{slug}.{inst}.myworkdayjobs.com"
        for site in SITE_PATHS:
            res = await try_url(f"{base}/{site}")
            if res:
                return name, slug, res

    return None

async def main():
    existing = load_existing()
    if not os.path.exists(TXT_FILE):
        logging.error(f"{TXT_FILE} not found.")
        return

    with open(TXT_FILE, "r", encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip()]

    to_probe = [n for n in names if clean_slug(n) not in existing]
    logging.info(f"Found {len(names)} total names in txt.")
    logging.info(f"Found {len(existing)} existing names in csv.")
    logging.info(f"Probing {len(to_probe)} new companies...")

    if not to_probe:
        logging.info("Nothing to probe.")
        return

    # Open CSV in append mode
    csv_file = open(CSV_FILE, "a", encoding="utf-8", newline="")
    writer = csv.writer(csv_file)

    # Concurrency limit for how many *companies* we check at once
    semaphore = asyncio.Semaphore(200)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }

    found_count = 0
    processed = 0
    
    async def process(client, name):
        nonlocal found_count, processed
        res = await probe_company(client, name, semaphore)
        processed += 1
        
        if res:
            orig_name, slug, url = res
            # Extract path from URL for slug format in csv
            # e.g. https://slug.wd1.myworkdayjobs.com/en-US/external -> slug/external
            match = re.search(r'https://([^.]+)\.[^.]+\.myworkdayjobs\.com/(?:en-[^/]+/)?([^/?#]+)', url)
            csv_slug = f"{match.group(1)}/{match.group(2)}" if match else f"{slug}/careers"
            
            # Clean URL to base search
            base_url = re.sub(r'/en-[^/]+/', '/', url)
            
            writer.writerow([orig_name, csv_slug, base_url])
            csv_file.flush()
            found_count += 1
            
        if processed % 100 == 0:
            logging.info(f"Progress: {processed}/{len(to_probe)} checked... Found {found_count} valid Workday URLs so far.")

    # High timeout since we're hitting slow domains sometimes
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=1000), headers=headers, follow_redirects=True, timeout=12.0) as client:
        tasks = [process(client, name) for name in to_probe]
        await asyncio.gather(*tasks)

    csv_file.close()
    logging.info(f"Done! Discovered {found_count} new Workday URLs and appended to CSV.")

if __name__ == '__main__':
    asyncio.run(main())
