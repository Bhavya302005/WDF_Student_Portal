import pandas as pd

csv_file = "/Users/AminBhavya/Ai_Sales_Agent/indeed_all_49_roles_master_v2.csv"
excel_file = "/Users/AminBhavya/Ai_Sales_Agent/indeed_all_49_roles_master_v2.xlsx"

print(f"Loading {csv_file}...")
df = pd.read_csv(csv_file, low_memory=False)

# Clean string columns of illegal characters that crash openpyxl
print("Cleaning data for Excel compatibility...")
for col in df.select_dtypes(include=['object']).columns:
    df[col] = df[col].astype(str).replace('nan', '')
    df[col] = df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) == 9 or ord(char) == 10 or ord(char) == 13) if isinstance(x, str) else x)

print("Exporting to Excel (This might take a minute due to the file size)...")
df.to_excel(excel_file, index=False)
print(f"Successfully created: {excel_file}")
