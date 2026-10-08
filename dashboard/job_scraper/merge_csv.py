import pandas as pd
import os

file1 = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_operations_project_manager.csv"
file2 = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_roles_25_to_49.csv"
output = "/Users/AminBhavya/.gemini/antigravity-ide/brain/5ed83f56-2437-451b-a68c-d848c8d26ab9/scratch/indeed_roles_24_to_49.csv"

# In the 24th role script, I didn't explicitly add 'Searched_Role' column like I did in the 25-49 loop. 
# Let's add it if missing so the data aligns.
try:
    df1 = pd.read_csv(file1)
    if 'Searched_Role' not in df1.columns:
        df1['Searched_Role'] = 'Operations Project Manager'
except Exception as e:
    print(f"Could not read {file1}: {e}")
    df1 = pd.DataFrame()

try:
    df2 = pd.read_csv(file2)
except Exception as e:
    print(f"Could not read {file2}: {e}")
    df2 = pd.DataFrame()

merged = pd.concat([df1, df2], ignore_index=True)
merged.to_csv(output, index=False)

print(f"Merged successfully! Total jobs: {len(merged)}")
print(f"Saved to: {output}")
