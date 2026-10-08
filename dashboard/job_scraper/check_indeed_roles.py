import pandas as pd

file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_all_49_roles_master.csv"
df = pd.read_csv(file)

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

# The roles from the Excel file might be in 'search_term', the ones from CSV in 'Searched_Role'
found_roles = set()

if 'search_term' in df.columns:
    found_roles.update(df['search_term'].dropna().unique())
    
if 'Searched_Role' in df.columns:
    found_roles.update(df['Searched_Role'].dropna().unique())

# Cross-reference with the official 49 list (case-insensitive to be safe)
official_roles_lower = {r.lower() for r in roles_49}

matched_roles = []
for r in found_roles:
    if r.lower() in official_roles_lower:
        matched_roles.append(r)

print(f"Total rows in master file: {len(df)}")
print(f"Total unique roles found from the 49 list: {len(matched_roles)}")
missing = [r for r in roles_49 if r.lower() not in [m.lower() for m in matched_roles]]
if missing:
    print(f"\nMissing roles ({len(missing)}):")
    for m in missing:
        print(f" - {m}")
