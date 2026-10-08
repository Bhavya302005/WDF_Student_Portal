import pandas as pd

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

# Convert to lowercase for case-insensitive matching
roles_49_lower = [r.lower() for r in roles_49]

def filter_ultra_strict(input_file, output_file, source_name):
    print(f"\nProcessing {source_name}...")
    try:
        if input_file.endswith(".xlsx"):
            df = pd.read_excel(input_file)
        else:
            df = pd.read_csv(input_file, low_memory=False)
            
        initial_length = len(df)
        print(f"Initial Jobs: {initial_length}")
        
        if 'title' not in df.columns:
            print("Error: 'title' column not found!")
            return
            
        # Keep only rows where the actual job 'title' exactly matches one of the 49 roles (case-insensitive)
        df_strict = df[df['title'].str.lower().isin(roles_49_lower)].copy()
        
        final_length = len(df_strict)
        print(f"Jobs remaining after ULTRA STRICT Employer Title match: {final_length}")
        print(f"Jobs deleted: {initial_length - final_length}")
        
        # Clean string columns for Excel compatibility
        for col in df_strict.select_dtypes(include=['object']).columns:
            df_strict[col] = df_strict[col].astype(str).replace('nan', '')
            df_strict[col] = df_strict[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) == 9 or ord(char) == 10 or ord(char) == 13) if isinstance(x, str) else x)
            
        print(f"Exporting to {output_file}...")
        df_strict.to_excel(output_file, index=False)
        print("Success!")
        
    except Exception as e:
        print(f"Error processing {source_name}: {e}")

# Process Indeed
filter_ultra_strict(
    "/Users/AminBhavya/Ai_Sales_Agent/indeed_jobs/Strictly_49_Roles_Indeed.xlsx",
    "/Users/AminBhavya/Ai_Sales_Agent/indeed_jobs/ULTRA_STRICT_49_Roles_Indeed.xlsx",
    "Indeed"
)

# Process LinkedIn
filter_ultra_strict(
    "/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/linkedin_all_49_roles_master.xlsx",
    "/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/ULTRA_STRICT_49_Roles_LinkedIn.xlsx",
    "LinkedIn"
)
