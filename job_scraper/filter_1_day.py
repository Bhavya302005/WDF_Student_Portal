import json
from datetime import datetime, timedelta

input_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/junior_pm_jobs.json'
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/junior_pm_jobs_1_day.csv'

with open(input_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

current_date = datetime.fromisoformat('2026-09-16T00:00:00+00:00')
one_day_ago = current_date - timedelta(days=1)

filtered_data = []
for job in data:
    pub_date_str = job.get('publishedDate')
    if pub_date_str:
        try:
            # Handle formats like 2026-09-04T00:00:00.000Z
            pub_date_str = pub_date_str.replace('Z', '+00:00')
            pub_date = datetime.fromisoformat(pub_date_str)
            # Ensure it's timezone aware for comparison
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=current_date.tzinfo)
            if pub_date >= one_day_ago:
                filtered_data.append(job)
        except Exception as e:
            print(f"Error parsing date {pub_date_str}: {e}")

import csv
if filtered_data:
    headers = list(filtered_data[0].keys())
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in filtered_data:
            if 'highlights' in row and isinstance(row['highlights'], str):
                row['highlights'] = row['highlights'].replace('\n', ' ').strip()
            writer.writerow(row)
            
print(f"Filtered to {len(filtered_data)} jobs that are 1 day old.")
