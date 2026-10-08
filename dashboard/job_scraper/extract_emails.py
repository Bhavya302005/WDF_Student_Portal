import pandas as pd
import re

input_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/all_roles_jobs.xlsx'
output_file = '/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/jobs_with_emails.xlsx'

# Regex pattern for extracting emails
email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'

# Load the Excel file
df = pd.read_excel(input_file, engine='openpyxl')

# Function to extract all emails from text
def extract_emails(text):
    if not isinstance(text, str):
        return ""
    emails = re.findall(email_pattern, text)
    # Deduplicate and join
    return ", ".join(list(set(emails)))

# Apply the function to create a new column
df['Extracted_Emails'] = df['Highlights'].apply(extract_emails)

# Filter out rows that do not have any extracted emails
df_with_emails = df[df['Extracted_Emails'] != ""]

if not df_with_emails.empty:
    df_with_emails.to_excel(output_file, index=False, engine='openpyxl')
    print(f"Success! Found {len(df_with_emails)} jobs containing email addresses.")
else:
    print("No emails were found in any of the job posts.")
