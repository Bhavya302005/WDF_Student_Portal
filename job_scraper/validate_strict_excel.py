import pandas as pd

excel_file = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/Strictly_49_Roles_Indeed.xlsx"

try:
    df = pd.read_excel(excel_file)
    unique_roles = df['Searched_Role'].unique()
    print(f"Total Rows: {len(df)}")
    print(f"Total Unique Roles: {len(unique_roles)}")
    
    print("\nList of Roles found in the file:")
    for role in sorted(unique_roles):
        count = len(df[df['Searched_Role'] == role])
        print(f" - {role} ({count} jobs)")
        
except Exception as e:
    print(f"Error validating file: {e}")
