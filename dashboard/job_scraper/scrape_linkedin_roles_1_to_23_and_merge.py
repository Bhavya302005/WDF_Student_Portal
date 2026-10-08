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

target_roles = roles_49[:23]  # Roles 1 to 23

print(f"\n🚀 Starting hyper-optimized LinkedIn Scraper for roles 1 to 23...")
print("Cap: 1000 jobs per role")
print("Time: Past 2 weeks (336 hours)")

all_jobs = pd.DataFrame()

for idx, role in enumerate(target_roles):
    actual_number = idx + 1
    print(f"\n[{actual_number}/23] Scraping LinkedIn for: {role}")
    
    try:
        jobs = scrape_jobs(
            site_name=["linkedin"],
            search_term=role,
            location="United States",
            results_wanted=1000,
            hours_old=336,
            linkedin_fetch_description=False
        )
        
        if not jobs.empty:
            jobs['Searched_Role'] = role
            all_jobs = pd.concat([all_jobs, jobs], ignore_index=True)
            print(f"  -> Found {len(jobs)} jobs for {role}")
        else:
            print(f"  -> No jobs found for {role}")
            
    except Exception as e:
        print(f"  -> Error scraping {role}: {e}")

output_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/linkedin_roles_1_to_23.csv"

if not all_jobs.empty:
    all_jobs.to_csv(output_file, index=False)
    print(f"\n✅ Finished! Found {len(all_jobs)} total jobs across {len(target_roles)} roles.")
    print(f"Saved to: {output_file}")
    
    # Immediately merge with the 26 roles CSV and create the strict Excel
    csv_file_26 = "/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/linkedin_missing_26_roles.csv"
    strict_excel = "/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/Strictly_49_Roles_LinkedIn.xlsx"
    
    try:
        df_26 = pd.read_csv(csv_file_26)
        print("Merging with the 26 missing roles CSV...")
        merged_df = pd.concat([all_jobs, df_26], ignore_index=True)
        
        # Clean string columns for Excel compatibility
        for col in merged_df.select_dtypes(include=['object']).columns:
            merged_df[col] = merged_df[col].astype(str).replace('nan', '')
            merged_df[col] = merged_df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) == 9 or ord(char) == 10 or ord(char) == 13) if isinstance(x, str) else x)
            
        print("Exporting strict 49 roles to Excel...")
        merged_df.to_excel(strict_excel, index=False)
        print(f"✅ Success! Generated STRICT LinkedIn Excel with {len(merged_df)} jobs.")
        print(f"Saved to: {strict_excel}")
        
    except Exception as e:
        print(f"Error during merge/export: {e}")
else:
    print("\nNo jobs found overall. LinkedIn might have blocked the IP.")
