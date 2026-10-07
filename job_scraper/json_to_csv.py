import json
import csv

input_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/junior_pm_jobs.json'
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/junior_pm_jobs.csv'

with open(input_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

if not data:
    print("No data found in JSON.")
else:
    # Get headers from the first item
    headers = list(data[0].keys())
    
    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in data:
            # Clean up highlights to remove newlines for better CSV formatting
            if 'highlights' in row and isinstance(row['highlights'], str):
                row['highlights'] = row['highlights'].replace('\n', ' ').strip()
            writer.writerow(row)

    print(f"Successfully wrote {len(data)} rows to {output_file}")
