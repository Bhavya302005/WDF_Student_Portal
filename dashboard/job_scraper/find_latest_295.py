import csv
from datetime import datetime

input_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/combined_pm_jobs.csv'

jobs = []
with open(input_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        pub_date_str = row.get('publishedDate', '')
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
    # Sort descending
    jobs.sort(key=lambda x: x['parsed_date'], reverse=True)
    latest = jobs[0]
    print(f"The latest job is: {latest['title']}")
    print(f"Published Date: {latest['parsed_date'].strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"URL: {latest['url']}")
    
    # Let's show top 3 just in case
    print("\nTop 3 latest:")
    for j in jobs[:3]:
        print(f"- {j['parsed_date'].strftime('%Y-%m-%d')}: {j['title']}")
