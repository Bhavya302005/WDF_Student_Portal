import csv
import json
from datetime import datetime, timedelta

input_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/all_roles_jobs_100.csv'
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/jobs_last_14_days.json'

current_date = datetime.fromisoformat("2026-09-16T22:24:24+05:30")
cutoff_date = current_date - timedelta(days=14)

recent_jobs = []

with open(input_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        pub_date_str = row.get('Published_Date', '')
        if pub_date_str:
            try:
                # Exa sometimes returns strings with 'Z' which python 3.10 and earlier doesn't like, but 3.12 handles it.
                pub_date_clean = pub_date_str.replace('Z', '+00:00')
                pub_date = datetime.fromisoformat(pub_date_clean)
                
                if pub_date >= cutoff_date:
                    recent_jobs.append(row)
            except Exception:
                pass

if recent_jobs:
    # Sort them newest first
    recent_jobs.sort(key=lambda x: datetime.fromisoformat(x['Published_Date'].replace('Z', '+00:00')), reverse=True)

with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(recent_jobs, f, indent=4)

print(f"Found {len(recent_jobs)} jobs posted in the last 2 weeks.")
