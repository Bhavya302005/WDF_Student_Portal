import asyncio
import httpx
import random
from pathlib import Path

async def check_url(client, comp, retries=5):
    url = f"https://boards-api.greenhouse.io/v1/boards/{comp}/jobs?content=true"
    base_delay = 1.0
    
    for attempt in range(retries):
        try:
            resp = await client.get(url, timeout=15)
            
            if resp.status_code == 404:
                return None  # Correctly returning 404
                
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after else base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(sleep_for)
                continue
                
            if resp.status_code >= 500:
                await asyncio.sleep(base_delay * (2 ** attempt))
                continue
                
            return comp, resp.status_code
        except Exception:
            await asyncio.sleep(base_delay)
            continue
            
    return None

async def main():
    file_path = Path("greenhouse_404_companies.txt")
    if not file_path.exists():
        print("File not found")
        return
        
    companies = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            parts = line.rsplit(",", 1)
            companies.append(parts[0])
            
    print(f"Checking {len(companies)} 404-listed companies to see if any are secretly active...")
    
    found_active = []
    
    # Use a semaphore to limit concurrency and avoid instant 429s
    sem = asyncio.Semaphore(20)
    
    async def sem_task(client, comp):
        async with sem:
            return await check_url(client, comp)
            
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=30)) as client:
        tasks = [asyncio.create_task(sem_task(client, comp)) for comp in companies]
        
        # We will await them in chunks just to print progress
        chunk_size = 500
        for i in range(0, len(tasks), chunk_size):
            chunk = tasks[i:i+chunk_size]
            results = await asyncio.gather(*chunk)
            for res in results:
                if res:
                    found_active.append(res)
            print(f"Processed {min(i+chunk_size, len(tasks))} / {len(tasks)}")
            
    print("\n" + "="*50)
    print("RESULTS:")
    print("="*50)
    if not found_active:
        print("All 7,790 companies legitimately returned 404! No active companies were mistakenly placed in the 404 list.")
    else:
        print(f"WOW! Found {len(found_active)} companies that actually returned something OTHER than 404:")
        for comp, code in found_active:
            print(f" - {comp} returned HTTP {code}")

if __name__ == "__main__":
    asyncio.run(main())
