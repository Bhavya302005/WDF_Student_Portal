import os
import re
import pandas as pd
from exa_py import Exa

# Smart Regex for Email Extraction
def extract_emails(text):
    if not text:
        return ""
    email_regex = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    emails = re.findall(email_regex, text)
    return ", ".join(list(set(emails)))

# Smart Regex for Salary Extraction
def extract_salary(text):
    if not text:
        return ""
    # Matches ranges like "$95K/yr - $130K/yr", "$50 - $60 /hr", "₹605K - ₹937K"
    # REQUIRES a currency symbol ($£€₹) to prevent matching phone numbers!
    range_pattern = r'([$£€₹]\s*\d+(?:[,.]\d+)*\s*[kKmM]?)\s*(?:-|to|—|–)\s*([$£€₹]?\s*\d+(?:[,.]\d+)*\s*[kKmM]?\s*(?:/hr|/yr|/hour|/year|/month|/mo|a year)?)'
    
    # Matches single rates like "$50/hr", "$130K/yr"
    single_pattern = r'([$£€₹]\s*\d+(?:[,.]\d+)*\s*[kKmM]?\s*(?:/hr|/yr|/hour|/year|/month|/mo|a year))'
    
    # Try range match first
    range_match = re.search(range_pattern, text, re.IGNORECASE)
    if range_match:
        # Clean up spacing
        return f"{range_match.group(1).strip()} - {range_match.group(2).strip()}"
        
    # Try single rate if no range
    single_match = re.search(single_pattern, text, re.IGNORECASE)
    if single_match:
        return single_match.group(1).strip()
        
    return ""

def main():
    print("🚀 Initializing Exa Human Posts Parser...")
    
    try:
        import json
        with open("/Users/AminBhavya/Ai_Sales_Agent/exa_output_human.json", "r") as f:
            response = json.load(f)
            
        extracted_data = []
        raw_text = response.get("content", [])[0].get("text", "")
        
        # Exa CLI separates results with '---\n'
        posts = raw_text.split('\n---\n')
        
        for post_text in posts:
            if not post_text.strip():
                continue
                
            author = "Unknown"
            title = "LinkedIn Post"
            published_date = ""
            url = ""
            
            lines = post_text.split('\n')
            for line in lines:
                if line.startswith('Title: '):
                    title = line.replace('Title: ', '').strip()
                elif line.startswith('Author: '):
                    author = line.replace('Author: ', '').strip()
                elif line.startswith('Published: '):
                    published_date = line.replace('Published: ', '').strip()
                elif line.startswith('URL: '):
                    url = line.replace('URL: ', '').strip()
            
            # Clean author
            if author.endswith(" · LinkedIn"):
                author = author.replace(" · LinkedIn", "")
                
            email = extract_emails(post_text)
            salary = extract_salary(post_text)
            
            extracted_data.append({
                "Author (Recruiter)": author,
                "Contact Email": email,
                "Extracted Salary": salary,
                "Post Title": title,
                "Published Date": published_date,
                "Post URL": url,
                "Full Post Text": post_text.strip()
            })
            
        if extracted_data:
            df = pd.DataFrame(extracted_data)
            
            # Clean string columns for Excel
            for col in df.select_dtypes(include=['object']).columns:
                df[col] = df[col].astype(str).replace('nan', '')
                df[col] = df[col].apply(lambda x: ''.join(char for char in x if ord(char) > 31 or ord(char) in [9, 10, 13]) if isinstance(x, str) else x)
            
            output_path = "/Users/AminBhavya/Ai_Sales_Agent/linkedin_jobs/Human_Recruiter_Posts.xlsx"
            df.to_excel(output_path, index=False)
            
            print(f"✅ Successfully extracted and structured {len(df)} human posts!")
            print(f"💾 Saved to: {output_path}")
            
            # Print a quick preview of successfully extracted salaries/emails
            print("\n📊 Extraction Preview:")
            for index, row in df.iterrows():
                if row['Contact Email'] or row['Extracted Salary']:
                    print(f"- {row['Author (Recruiter)']}: Email={row['Contact Email'] or 'None'}, Salary={row['Extracted Salary'] or 'None'}")
                    
        else:
            print("⚠️ No posts found.")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
