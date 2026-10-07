import sys
import os
import pandas as pd

# Ensure JobSpy is in the path
sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))

from jobspy import scrape_jobs

print("🚀 Starting optimized Indeed Scraper...")
print("Role: Operations Project Manager (24th role)")
print("Cap: 1000 jobs")
print("Time: Past 2 weeks (336 hours)")

jobs = scrape_jobs(
    site_name=["indeed"],
    search_term="Operations Project Manager",
    location="United States",
    results_wanted=1000,
    hours_old=336,  # 14 days * 24 hours
    country_indeed='usa'
)

print(f"\n✅ Finished! Found {len(jobs)} jobs.")

output_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_operations_project_manager.csv"
if not jobs.empty:
    jobs.to_csv(output_file, index=False)
    print(f"Saved to: {output_file}")
else:
    print("No jobs found or Indeed blocked the request.")
