import csv
import logging
import time
from pathlib import Path
from jobspy import scrape_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")

ROLES = [
    "Project Lead",
    "Software Project Lead",
    "Business Analyst Project Lead",
    "Project Management Analyst",
    "Project Implementation Specialist",
    "Lead Project Analyst",
    "Service Project Manager",
    "Project Analyst",
    "Information Technology Project Coordinator",
    "Senior Project Analyst",
    "Project Finance Specialist",
    "Project Assistant",
    "Project Consultant",
    "Project Sales Specialist",
    "Project Team Lead",
    "Information Technology Project Lead",
    "Recruiting Operations Project Manager",
    "Project Planning Specialist",
    "Information Technology Project Manager",
    "Business Project Manager",
    "Junior Project Manager",
    "Information Technology Operations Project Manager",
    "Operations Project Manager",
    "Technical Project Lead",
    "Project Control Coordinator",
    "Special Project Administrator",
    "Lead Project Engineer",
    "Project Management Administrator",
    "Technical Project Specialist",
    "Information Technology Project Analyst",
    "Project Development Specialist",
    "Project Support Coordinator",
    "Senior Project Lead",
    "Senior Operations Project Manager",
    "Project Manager",
    "Senior Project Manager",
    "Project Engineer",
    "Assistant Project Manager",
    "Technical Project Manager",
    "Quality Assurance Manager",
    "Project Management Specialist",
    "Project Administrator",
    "Project Specialist",
    "Project Control Analyst",
    "Special Project Manager",
    "Project Business Analyst",
    "Lead Project Manager",
    "Senior Project Administrator",
    "Project Support Analyst"
]

def main():
    output_file = Path("all_49_roles_jobs.csv")
    
    # We want 2 weeks = 336 hours
    HOURS_OLD = 336
    # Setting results_wanted to a high number to "get all"
    RESULTS_WANTED = 500
    
    first_write = not output_file.exists()
    
    for idx, role in enumerate(ROLES):
        logging.info(f"Processing ({idx+1}/{len(ROLES)}): {role}")
        try:
            jobs_df = scrape_jobs(
                site_name=["linkedin", "indeed"],
                search_term=role,
                location="USA", # Defaulting to USA based on previous config
                hours_old=HOURS_OLD,
                results_wanted=RESULTS_WANTED,
                linkedin_fetch_description=False
            )
            
            if jobs_df is not None and not jobs_df.empty:
                logging.info(f"Found {len(jobs_df)} jobs for {role}")
                
                # Adding a role column to know which search query yielded the job
                jobs_df['search_role'] = role
                
                # Append to CSV
                jobs_df.to_csv(
                    output_file,
                    mode='a',
                    header=first_write,
                    quoting=csv.QUOTE_NONNUMERIC,
                    escapechar="\\",
                    index=False
                )
                first_write = False
            else:
                logging.warning(f"No jobs found for {role}")
                
        except Exception as e:
            logging.error(f"Error processing {role}: {e}")
            
        logging.info("Sleeping for 10 seconds to avoid rate limiting...")
        time.sleep(10)
        
    logging.info(f"Finished scraping all roles! Results saved to {output_file.absolute()}")

if __name__ == "__main__":
    main()
