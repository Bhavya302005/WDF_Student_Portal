import sys
import os
sys.path.append(os.path.abspath('job_scraper/JobSpy-main'))
from jobspy import scrape_jobs

def main():
    print("Checking how many jobs exist in California...")
    jobs = scrape_jobs(
        site_name=["linkedin"],
        search_term="Ai Product Manager",
        location="California",
        results_wanted=2000,
        hours_old=336,
        country_circa="USA",
        linkedin_fetch_description=False # Don't fetch descriptions so it runs instantly!
    )
    print(f"Total jobs found in California: {len(jobs)}")

if __name__ == "__main__":
    main()
