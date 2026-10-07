import requests
import time
from bs4 import BeautifulSoup

url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def test_fast_sequential():
    job_ids = set()
    pages_to_fetch = 40 # 1000 jobs
    start_time = time.time()
    
    print("Testing fast sequential pagination (0.5s delay)...")
    for i in range(pages_to_fetch):
        start_offset = i * 25
        params = {
            "keywords": "Quality Assurance Manager",
            "location": "United States",
            "start": start_offset,
            "f_TPR": "r1209600" # 14 days
        }
        
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=5)
            if resp.status_code != 200:
                print(f"Page {i}: Blocked! Status {resp.status_code}")
                break
                
            soup = BeautifulSoup(resp.text, "html.parser")
            job_cards = soup.find_all("div", class_="base-search-card")
            
            if not job_cards:
                print(f"Page {i}: No jobs found. Stopping.")
                break
                
            for card in job_cards:
                href_tag = card.find("a", class_="base-card__full-link")
                if href_tag and "href" in href_tag.attrs:
                    href = href_tag.attrs["href"].split("?")[0]
                    job_id = href.split("-")[-1]
                    job_ids.add(job_id)
                    
            print(f"Page {i} fetched. Total unique jobs so far: {len(job_ids)}")
            time.sleep(0.5) # Fast 500ms delay
            
        except Exception as e:
            print(f"Error on page {i}: {e}")
            break
            
    end_time = time.time()
    print(f"\nFinished in {end_time - start_time:.2f} seconds.")
    print(f"Total Unique Jobs Extracted: {len(job_ids)}")

if __name__ == "__main__":
    test_fast_sequential()
