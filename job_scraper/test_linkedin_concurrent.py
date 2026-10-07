import requests
from concurrent.futures import ThreadPoolExecutor
import time

url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def fetch_page(start):
    params = {
        "keywords": "Software Engineer",
        "location": "United States",
        "start": start
    }
    resp = requests.get(url, headers=headers, params=params)
    return start, resp.status_code

print("Firing 10 concurrent requests to LinkedIn...")
start_time = time.time()

with ThreadPoolExecutor(max_workers=10) as executor:
    results = list(executor.map(fetch_page, range(0, 250, 25)))

end_time = time.time()

for start, status in results:
    print(f"Start: {start:3d} | Status: {status}")

print(f"Total time: {end_time - start_time:.2f} seconds")
