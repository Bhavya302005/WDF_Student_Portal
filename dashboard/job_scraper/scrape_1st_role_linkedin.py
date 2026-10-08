import sys
import os
import pandas as pd

sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))
from jobspy import scrape_jobs

print("🚀 Starting optimized LinkedIn Scraper...")
print("Role: Quality Assurance Manager (1st role)")
print("Cap: 1000 jobs")
print("Time: Past 2 weeks (336 hours)")
print("Fetching full description: False (to avoid 429 blocks)")

jobs = scrape_jobs(
    site_name=["linkedin"],
    search_term="Quality Assurance Manager",
    location="United States",
    results_wanted=1000,
    hours_old=336,  # 14 days * 24 hours
    linkedin_fetch_description=False  # As discussed, safer
)

print(f"\n✅ Finished! Found {len(jobs)} jobs.")

output_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/linkedin_quality_assurance_manager.csv"
if not jobs.empty:
    jobs.to_csv(output_file, index=False)
    print(f"Saved to: {output_file}")
else:
    print("No jobs found or LinkedIn blocked the request.")
