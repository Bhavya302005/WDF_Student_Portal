import pandas as pd
import os

input_file = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/indeed_jobs/AI_PM_5_Locations_ENRICHED.xlsx"
output_file = "/Users/AminBhavya/Ai_Sales_Agent/job_scraper/indeed_jobs/STRICT_AI_PM_5_Locations_ENRICHED.xlsx"

print(f"Reading {input_file}...")
try:
    df = pd.read_excel(input_file)
    initial_length = len(df)
    print(f"Initial Jobs: {initial_length}")
    
    if 'title' not in df.columns:
        print("Error: 'title' column not found!")
        exit()
        
    # Keep only rows where the job title contains "Ai Product Manager" (case-insensitive)
    df_strict = df[df['title'].str.contains('ai product manager', case=False, na=False)].copy()
    
    final_length = len(df_strict)
    print(f"Jobs remaining after STRICT match: {final_length}")
    print(f"Jobs deleted: {initial_length - final_length}")
    
    # Clean string columns for Excel compatibility
    for col in df_strict.select_dtypes(include=['object']).columns:
        df_strict[col] = df_strict[col].astype(str).replace('nan', '')
        df_strict[col] = df_strict[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
        
    print(f"Exporting to {output_file}...")
    df_strict.to_excel(output_file, index=False)
    print("Success!")

except Exception as e:
    print(f"Error processing file: {e}")
