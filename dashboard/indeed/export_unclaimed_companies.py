import json
import csv
from collections import defaultdict
from pathlib import Path

def main():
    input_file = Path("indeed_jobs/indeed_68roles_jobs_us.json")
    output_file = Path("indeed_jobs/unclaimed_companies_leads.csv")
    
    if not input_file.exists():
        print(f"Error: Could not find {input_file}")
        return

    print(f"Loading data from {input_file}...")
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # We will use a dictionary to store unique companies and aggregate their job data
    # Key: company name, Value: dict of data
    unclaimed_companies = defaultdict(lambda: {
        "company_name": "",
        "job_titles_hiring_for": set(),
        "locations": set(),
        "indeed_profile_url": "",
        "total_jobs_posted": 0
    })

    for job in data:
        # Heuristics for "Unclaimed"
        if not job.get("company_url_direct") and not job.get("company_logo"):
            company_name = job.get("company")
            if not company_name:
                continue
                
            c_data = unclaimed_companies[company_name]
            c_data["company_name"] = company_name
            c_data["total_jobs_posted"] += 1
            
            if job.get("job_title"):
                c_data["job_titles_hiring_for"].add(job["job_title"])
            if job.get("location"):
                c_data["locations"].add(job["location"])
            if job.get("company_url"):
                c_data["indeed_profile_url"] = job["company_url"]

    # Flatten the sets for CSV output
    flattened_leads = []
    for company, c_data in unclaimed_companies.items():
        flattened_leads.append({
            "Company Name": c_data["company_name"],
            "Total Open Jobs Scraped": c_data["total_jobs_posted"],
            "Locations": " | ".join(c_data["locations"]),
            "Hiring For": " | ".join(c_data["job_titles_hiring_for"]),
            "Indeed Profile URL": c_data["indeed_profile_url"]
        })

    # Sort by total jobs posted (descending) so the most active companies are at the top
    flattened_leads.sort(key=lambda x: x["Total Open Jobs Scraped"], reverse=True)

    print(f"Found {len(flattened_leads)} unclaimed companies.")
    
    # Write to CSV
    fieldnames = ["Company Name", "Total Open Jobs Scraped", "Locations", "Hiring For", "Indeed Profile URL"]
    with open(output_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened_leads)

    print(f"Success! Exported lead list to {output_file}")

if __name__ == "__main__":
    main()
