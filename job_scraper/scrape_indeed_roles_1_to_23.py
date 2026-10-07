import sys
import os
import pandas as pd

sys.path.append(os.path.abspath('/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main'))
from jobspy import scrape_jobs

roles_49 = [
    "Quality Assurance Manager", "Project Management Specialist", "Project Administrator", "Project Specialist", "Project Control Analyst",
    "Project Manager", "Senior Project Manager", "Project Engineer", "Assistant Project Manager", "Technical Project Manager",
    "Information Technology Project Analyst", "Project Development Specialist", "Project Support Coordinator", "Senior Project Lead", "Senior Operations Project Manager",
    "Project Control Coordinator", "Special Project Administrator", "Lead Project Engineer", "Project Management Administrator", "Technical Project Specialist",
    "Business Project Manager", "Junior Project Manager", "Information Technology Operations Project Manager", "Operations Project Manager", "Technical Project Lead",
    "Project Team Lead", "Information Technology Project Lead", "Recruiting Operations Project Manager", "Project Planning Specialist", "Information Technology Project Manager",
    "Senior Project Analyst", "Project Finance Specialist", "Project Assistant", "Project Consultant", "Project Sales Specialist",
    "Project Implementation Specialist", "Lead Project Analyst", "Service Project Manager", "Project Analyst", "Information Technology Project Coordinator",
    "Project Lead", "Software Project Lead", "Business Analyst Project Lead", "Project Management Analyst",
    "Special Project Manager", "Project Business Analyst", "Lead Project Manager", "Senior Project Administrator", "Project Support Analyst"
]

target_roles = roles_49[:23]  # Roles 1 (index 0) to 23 (index 22)

print(f"🚀 Starting optimized Indeed Scraper for the first {len(target_roles)} roles...")
print("Cap: 1000 jobs per role")
print("Time: Past 2 weeks (336 hours)")

all_jobs = pd.DataFrame()

for idx, role in enumerate(target_roles):
    print(f"\n[{idx+1}/23] Scraping Indeed for: {role}")
    
    try:
        jobs = scrape_jobs(
            site_name=["indeed"],
            search_term=role,
            location="United States",
            results_wanted=1000,
            hours_old=336,
            country_indeed='usa'
        )
        
        if not jobs.empty:
            jobs['Searched_Role'] = role
            all_jobs = pd.concat([all_jobs, jobs], ignore_index=True)
            print(f"  -> Found {len(jobs)} jobs for {role}")
        else:
            print(f"  -> No jobs found for {role}")
            
    except Exception as e:
        print(f"  -> Error scraping {role}: {e}")

output_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_roles_1_to_23.csv"
master_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_all_49_roles_master.csv"
new_master_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_all_49_roles_master_v2.csv"

if not all_jobs.empty:
    all_jobs.to_csv(output_file, index=False)
    print(f"\n✅ Finished scraping! Found {len(all_jobs)} total jobs across the first 23 roles.")
    
    # Merge with the master file
    print("Merging with the master Indeed CSV...")
    try:
        df_master = pd.read_csv(master_file, low_memory=False)
        # Drop the old unlabelled Excel jobs (we assume they don't have Searched_Role or we just rely on drop_duplicates)
        merged = pd.concat([df_master, all_jobs], ignore_index=True)
        
        # Deduplicate based on job_url so we don't double count the Excel jobs we are replacing
        before_len = len(merged)
        merged = merged.drop_duplicates(subset=['job_url'], keep='last')
        after_len = len(merged)
        print(f"Dropped {before_len - after_len} duplicates.")
        
        merged.to_csv(new_master_file, index=False)
        print(f"✅ Success! The new Master File (v2) has {len(merged)} strictly unique jobs.")
        print(f"Saved to: {new_master_file}")
    except Exception as e:
        print(f"Error merging with master file: {e}")
        
else:
    print("\nNo jobs found overall. Indeed might have blocked the IP.")
