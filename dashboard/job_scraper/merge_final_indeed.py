import pandas as pd
import os

xlsx_file = "/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main/Linkedin+Indeed_23_roles_2w.xlsx"
csv_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_roles_24_to_49.csv"
output_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_all_49_roles_master.csv"

print("Reading Excel file (this might take a few seconds)...")
try:
    df_excel = pd.read_excel(xlsx_file)
    # Filter for only Indeed jobs from the excel file
    if 'site' in df_excel.columns:
        df_indeed_excel = df_excel[df_excel['site'].str.lower() == 'indeed'].copy()
    else:
        print("Warning: 'site' column not found in excel file. Taking all rows.")
        df_indeed_excel = df_excel.copy()
        
    print(f"Extracted {len(df_indeed_excel)} Indeed jobs from the Excel file (roles 1-23).")
except Exception as e:
    print(f"Error reading Excel file: {e}")
    df_indeed_excel = pd.DataFrame()

print("Reading the CSV file (roles 24-49)...")
try:
    df_csv = pd.read_csv(csv_file)
    print(f"Loaded {len(df_csv)} Indeed jobs from the CSV file.")
except Exception as e:
    print(f"Error reading CSV file: {e}")
    df_csv = pd.DataFrame()

# Concatenate them
print("Merging data...")
merged_df = pd.concat([df_indeed_excel, df_csv], ignore_index=True)

# Save to a new master CSV
merged_df.to_csv(output_file, index=False)
print(f"✅ Success! Merged a grand total of {len(merged_df)} Indeed jobs across all 49 roles.")
print(f"Saved to: {output_file}")
