import pandas as pd
from jobspy import scrape_jobs

def main():
    print("🚀 Initializing Naukri Scraper for 'Project Manager'...")
    
    try:
        jobs = scrape_jobs(
            site_name=["naukri"],
            search_term="Project Manager",
            location="India",
            results_wanted=1,
            country_circa="india", # JobSpy uses this for localization
            hours_old=72 # Let's get fresh jobs from the last 3 days
        )
        
        print(f"✅ Found {len(jobs)} jobs!")
        
        if not jobs.empty:
            # Let's reorder columns to highlight Naukri's special data!
            naukri_columns = [
                "title", "company", "location", "vacancy_count", 
                "experience_range", "min_amount", "max_amount", "currency", 
                "company_rating", "company_reviews_count", "work_from_home_type", 
                "skills", "job_url"
            ]
            
            # Ensure all special columns exist even if some are missing in the current batch
            for col in naukri_columns:
                if col not in jobs.columns:
                    jobs[col] = None
                    
            # Bring special columns to the front, keep the rest at the end
            other_columns = [col for col in jobs.columns if col not in naukri_columns]
            final_columns = naukri_columns + other_columns
            jobs = jobs[final_columns]
            
            output_path = "/Users/AminBhavya/Ai_Sales_Agent/naukri_jobs_project_manager.xlsx"
            
            # Clean string columns for Excel compatibility
            for col in jobs.select_dtypes(include=['object']).columns:
                jobs[col] = jobs[col].astype(str).replace('nan', '')
                jobs[col] = jobs[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
            
            jobs.to_excel(output_path, index=False)
            
            print(f"💾 Data successfully saved to: {output_path}")
            print("\n📊 Data Preview (Top 5 Jobs):")
            print(jobs[["title", "company", "vacancy_count", "experience_range", "company_rating"]].head(5).to_string())
        else:
            print("⚠️ No jobs were found matching the criteria.")
            
    except Exception as e:
        print(f"❌ Error occurred during scraping: {e}")

if __name__ == "__main__":
    main()
