import sys
import os
sys.path.append(os.path.abspath('job_scraper/JobSpy-main'))
import pandas as pd
from jobspy import scrape_jobs
import time

def main():
    search_term = "Ai Product Manager"
    locations = [
        "California",
        "New York",
        "Boston",
        "Seattle",
        "Texas"
    ]
    hours_old = 336 # 2 weeks (14 days * 24 hours)
    results_wanted = 2000 # High number to simulate "no cap" (LinkedIn guest usually caps around 1000 anyway)
    
    all_jobs_dfs = []
    
    for loc in locations:
        print(f"\n🚀 Scraping LinkedIn for '{search_term}' in {loc} (Last 2 weeks)...")
        try:
            jobs = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location=loc,
                results_wanted=results_wanted,
                hours_old=hours_old,
                country_circa="USA",
                linkedin_fetch_description=True # Explicitly fetch description
            )
            print(f"✅ Found {len(jobs)} jobs in {loc}")
            if not jobs.empty:
                # Add a column to track which location search found this job
                jobs['search_location'] = loc
                all_jobs_dfs.append(jobs)
        except Exception as e:
            print(f"❌ Error scraping {loc}: {e}")
            
        # Sleep to avoid aggressive rate limiting between location searches
        print("Waiting 5 seconds before next location...")
        time.sleep(5)
        
    if all_jobs_dfs:
        final_df = pd.concat(all_jobs_dfs, ignore_index=True)
        # Drop strict duplicates based on job_url
        initial_len = len(final_df)
        final_df = final_df.drop_duplicates(subset=['job_url'])
        print(f"\n📊 Total unique jobs found across all locations: {len(final_df)} (Dropped {initial_len - len(final_df)} cross-location duplicates)")
        
        output_path = "job_scraper/linkedin_jobs/AI_Product_Manager_5_Locations.xlsx"
        
        # Clean string columns for Excel compatibility
        for col in final_df.select_dtypes(include=['object']).columns:
            final_df[col] = final_df[col].astype(str).replace('nan', '')
            final_df[col] = final_df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
            
        final_df.to_excel(output_path, index=False)
        print(f"💾 Data successfully saved to: {output_path}")
    else:
        print("\n⚠️ No jobs were found across any locations.")

if __name__ == "__main__":
    main()
