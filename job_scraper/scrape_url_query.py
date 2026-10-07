import sys
import os
import pandas as pd

sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))
from jobspy import scrape_jobs

print("🚀 Starting LinkedIn Scraper with User URL Parameters...")
print("Search Term: Project Manager")
print("Location: United States")
print("Time Filter: Past 1 Week (168 hours)")
print("Filter: Easy Apply ONLY (f_EA=true)")

try:
    jobs = scrape_jobs(
        site_name=["linkedin"],
        search_term="Project Manager",
        location="United States",
        results_wanted=1000,
        hours_old=168,
        linkedin_fetch_description=True,
        easy_apply=True  # Maps to f_AL=true in JobSpy which handles the Easy Apply filter
    )
    
    if not jobs.empty:
        # Clean string columns for Excel
        for col in jobs.select_dtypes(include=['object']).columns:
            jobs[col] = jobs[col].astype(str).replace('nan', '')
            jobs[col] = jobs[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) == 9 or ord(char) == 10 or ord(char) == 13) if isinstance(x, str) else x)
            
        output_file = "/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/Custom_URL_Project_Manager.xlsx"
        jobs.to_excel(output_file, index=False)
        print(f"\n✅ Finished! Found {len(jobs)} 'Easy Apply' Project Manager jobs.")
        print(f"Saved to: {output_file}")
    else:
        print("\nNo jobs found matching these exact criteria.")
except Exception as e:
    print(f"\nError: {e}")
