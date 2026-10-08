import time
import sys
import os
sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))

from jobspy.indeed import Indeed
from jobspy.model import ScraperInput, Site, Country

def main():
    print("Testing JobSpy Indeed Scraper Performance...")
    scraper = Indeed()
    scraper_input = ScraperInput(
        site_type=[Site.INDEED],
        country=Country.USA,
        search_term="software engineer",
        location="San Francisco, CA",
        results_wanted=100
    )
    
    start_time = time.time()
    response = scraper.scrape(scraper_input)
    end_time = time.time()
    
    print(f"Total time for 100 jobs: {end_time - start_time:.2f} seconds")
    print(f"Jobs found: {len(response.jobs)}")

if __name__ == "__main__":
    main()
