import pandas as pd
import math

v2_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_all_49_roles_master_v2.csv"
output_excel = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/Strictly_49_Roles_Indeed.xlsx"

roles_49 = [
    "Quality Assurance Manager", "Project Management Specialist", "Project Administrator", "Project Specialist", "Project Control Analyst",
    "Project Manager", "Senior Project Manager", "Project Engineer", "Assistant Project Manager", "Technical Project Manager",
    "Information Technology Project Analyst", "Project Development Specialist", "Project Support Coordinator", "Senior Project Lead", "Senior Operations Project Manager",
    "Project Control Coordinator", "Special Project Administrator", "Lead Project Engineer", "Project Management Administrator", "Technical Project Specialist",
    "Business Project Manager", "Junior Project Manager", "Information Technology Operations Project Manager", "Operations Project Manager", "Technical Project Lead",
    "Project Team Lead", "Information Technology Project Lead", "Recruiting Operations Project Manager", "Project Planning Specialist", "Information Technology Project Manager",
    "Senior Project Analyst", "Project Finance Specialist", "Project Assistant", "Project Consultant", "Project Sales Specialist",
    "Project Implementation Specialist", "Lead Project Analyst", "Service Project Manager", "Project Analyst", "Information Technology Project Coordinator",
    "Project Lead", "Software Project Lead", "Business Analyst Project Lead", "Project Management Analyst",
    "Special Project Manager", "Project Business Analyst", "Lead Project Manager", "Senior Project Administrator", "Project Support Analyst"
]

print("Loading v2 Master File...")
try:
    df = pd.read_csv(v2_file, low_memory=False)
except Exception as e:
    print(f"Error loading v2 file: {e}")
    exit()

# If the Excel unlabelled jobs are still in there, we can filter them out.
# We want STRICTLY jobs that have one of the 49 roles in the 'Searched_Role' column.
if 'Searched_Role' in df.columns:
    strict_df = df[df['Searched_Role'].isin(roles_49)].copy()
else:
    print("No Searched_Role column found!")
    strict_df = pd.DataFrame()

print(f"Original v2 length: {len(df)}")
print(f"Strict length: {len(strict_df)}")

unique_roles = strict_df['Searched_Role'].unique()
print(f"Unique explicit roles covered: {len(unique_roles)}")

if len(unique_roles) < 49:
    missing = [r for r in roles_49 if r not in unique_roles]
    print(f"Missing strictly {len(missing)} roles: {missing}")

# The user wants an excel file, let's create a very clean one.
# We will drop any columns that are entirely NaN/useless.
strict_df = strict_df.dropna(axis=1, how='all')

# Let's sort it by Searched_Role and Date
try:
    strict_df = strict_df.sort_values(by=['Searched_Role'])
except Exception:
    pass

# We can also clean up datetime strings that break excel
for col in strict_df.select_dtypes(include=['object']).columns:
    strict_df[col] = strict_df[col].astype(str).replace('nan', '')
    # openpyxl has issues with illegal characters, let's remove them
    strict_df[col] = strict_df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) == 9 or ord(char) == 10 or ord(char) == 13) if isinstance(x, str) else x)

print("Exporting to Excel (This might take a minute)...")
strict_df.to_excel(output_excel, index=False)
print(f"Saved highly cleaned, strict 49-role excel to: {output_excel}")
