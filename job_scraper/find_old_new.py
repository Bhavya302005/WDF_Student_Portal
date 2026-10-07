import csv
from datetime import datetime

input_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/all_roles_jobs_100.csv'

jobs = []
with open(input_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        pub_date_str = row.get('Published_Date', '')
        if pub_date_str:
            try:
                pub_date_clean = pub_date_str.replace('Z', '+00:00')
                pub_date = datetime.fromisoformat(pub_date_clean)
                row['parsed_date'] = pub_date
                jobs.append(row)
            except Exception:
                pass

if not jobs:
    print("No parseable dates found.")
else:
    # Sort descending (newest first)
    jobs.sort(key=lambda x: x['parsed_date'], reverse=True)
    
    print("--- LATEST 5 POSTS ---")
    for i, j in enumerate(jobs[:5]):
        print(f"{i+1}. {j['parsed_date'].strftime('%Y-%m-%d')}: {j['Title']}")
        print(f"   URL: {j['URL']}")
        
    print("\n--- OLDEST 5 POSTS ---")
    for i, j in enumerate(jobs[-5:]):
        print(f"{i+1}. {j['parsed_date'].strftime('%Y-%m-%d')}: {j['Title']}")
        print(f"   URL: {j['URL']}")
