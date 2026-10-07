import pandas as pd

ROLES = [
    "Project Lead",
    "Software Project Lead",
    "Business Analyst Project Lead",
    "Project Management Analyst",
    "Project Implementation Specialist",
    "Lead Project Analyst",
    "Service Project Manager",
    "Project Analyst",
    "Information Technology Project Coordinator",
    "Senior Project Analyst",
    "Project Finance Specialist",
    "Project Assistant",
    "Project Consultant",
    "Project Sales Specialist",
    "Project Team Lead",
    "Information Technology Project Lead",
    "Recruiting Operations Project Manager",
    "Project Planning Specialist",
    "Information Technology Project Manager",
    "Business Project Manager",
    "Junior Project Manager",
    "Information Technology Operations Project Manager",
    "Operations Project Manager",
    "Technical Project Lead",
    "Project Control Coordinator",
    "Special Project Administrator",
    "Lead Project Engineer",
    "Project Management Administrator",
    "Technical Project Specialist",
    "Information Technology Project Analyst",
    "Project Development Specialist",
    "Project Support Coordinator",
    "Senior Project Lead",
    "Senior Operations Project Manager",
    "Project Manager",
    "Senior Project Manager",
    "Project Engineer",
    "Assistant Project Manager",
    "Technical Project Manager",
    "Quality Assurance Manager",
    "Project Management Specialist",
    "Project Administrator",
    "Project Specialist",
    "Project Control Analyst",
    "Special Project Manager",
    "Project Business Analyst",
    "Lead Project Manager",
    "Senior Project Administrator",
    "Project Support Analyst"
]

def contains_role(title, roles):
    if not isinstance(title, str):
        return False
    title_lower = title.lower()
    for role in roles:
        if role.lower() in title_lower:
            return True
    return False

df = pd.read_csv("all_49_roles_jobs.csv")
strict_df = df[df['title'].apply(lambda x: contains_role(x, ROLES))].copy()

strict_df.to_excel("Linkedin+Indeed_strict_roles_2w.xlsx", index=False)
print(f"Filtered down from {len(df)} to {len(strict_df)} roles.")
