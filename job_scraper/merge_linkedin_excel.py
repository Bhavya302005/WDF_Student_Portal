import pandas as pd

excel_file = "/Users/AminBhavya/Ai_Sales_Agent/JobSpy-main/Linkedin+Indeed_23_roles_2w.xlsx"
csv_file = "/Users/AminBhavya/Ai_Sales_Agent/linkedin_missing_26_roles.csv"
output_file = "/Users/AminBhavya/Ai_Sales_Agent/Strictly_49_Roles_LinkedIn.xlsx"

print("Reading the original Excel file (roles 1-23)...")
try:
    df_excel = pd.read_excel(excel_file)
    # Filter for only LinkedIn jobs
    if 'site' in df_excel.columns:
        df_linkedin_excel = df_excel[df_excel['site'].str.lower() == 'linkedin'].copy()
    else:
        print("Warning: 'site' column not found in excel file. Taking all rows.")
        df_linkedin_excel = df_excel.copy()
        
    print(f"Extracted {len(df_linkedin_excel)} LinkedIn jobs from the Excel file (roles 1-23).")
except Exception as e:
    print(f"Error reading Excel file: {e}")
    df_linkedin_excel = pd.DataFrame()

print("Reading the CSV file (roles 24-49)...")
try:
    df_csv = pd.read_csv(csv_file)
    print(f"Loaded {len(df_csv)} LinkedIn jobs from the CSV file.")
except Exception as e:
    print(f"Error reading CSV file: {e}")
    df_csv = pd.DataFrame()

# Concatenate them
print("Merging data...")
merged_df = pd.concat([df_linkedin_excel, df_csv], ignore_index=True)

# Clean up illegal characters for Excel export
print("Cleaning string columns for Excel compatibility...")
for col in merged_df.select_dtypes(include=['object']).columns:
    merged_df[col] = merged_df[col].astype(str).replace('nan', '')
    merged_df[col] = merged_df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) == 9 or ord(char) == 10 or ord(char) == 13) if isinstance(x, str) else x)

# Save to a new master Excel
print(f"Exporting to {output_file} (this might take a few seconds)...")
merged_df.to_excel(output_file, index=False)
print(f"✅ Success! Merged a grand total of {len(merged_df)} LinkedIn jobs across all 49 roles.")
print(f"Saved to: {output_file}")
