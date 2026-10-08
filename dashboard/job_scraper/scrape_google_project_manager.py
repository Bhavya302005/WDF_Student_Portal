import sys
import os
import pandas as pd

sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))
from jobspy import scrape_jobs

print("🚀 Starting Google Jobs Scraper...")
print("Role: Project Manager")
print("Time: Past 24 hours")

jobs = scrape_jobs(
    site_name=["google"],
    search_term="Project Manager",
    location="United States",
    results_wanted=900,  # Max allowed by Google
    hours_old=24
)

print(f"\n✅ Finished! Found {len(jobs)} jobs.")

output_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/google_project_manager_24h.csv"
if not jobs.empty:
    jobs.to_csv(output_file, index=False)
    print(f"Saved to: {output_file}")
else:
    print("No jobs found or Google blocked the request.")
