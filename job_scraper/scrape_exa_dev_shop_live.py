import os
import json
import pandas as pd
from exa_py import Exa
import re

def extract_emails(text):
    if not text: return ""
    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    return ", ".join(list(set(emails)))

def main():
    print("🚀 Initializing Live Exa Search for Dev Shop Leads...")
    
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        print("❌ Error: EXA_API_KEY environment variable is not set in the .env file.")
        print("Please add EXA_API_KEY=your_key to /Users/AminBhavya/Ai_Sales_Agent/.env")
        return
        
    exa = Exa(api_key)
    
    # Exa uses neural search, so natural language questions work best
    query = 'Here are recent posts from founders or companies looking to hire a software development agency, dev shop, or app developer to build their MVP:'
    
    print(f"🔍 Searching Exa for: '{query}'")
    try:
        response = exa.search_and_contents(
            query,
            type="neural",
            use_autoprompt=True,
            num_results=20,
            text=True,
            highlights=True
        )
        
        extracted_data = []
        for result in response.results:
            text = result.text or ""
            extracted_data.append({
                "Post Title": result.title,
                "Post URL": result.url,
                "Published Date": result.published_date,
                "Contact Email": extract_emails(text),
                "Full Post Text": text[:1000] + "..." if len(text) > 1000 else text
            })
            
        if extracted_data:
            df = pd.DataFrame(extracted_data)
            output_path = "job_scraper/Exa_Dev_Shop_Leads.xlsx"
            df.to_excel(output_path, index=False)
            
            with open("job_scraper/Exa_Dev_Shop_Leads.json", "w") as f:
                json.dump(extracted_data, f, indent=4)
                
            print(f"✅ Successfully found {len(extracted_data)} leads via Exa!")
            print(f"💾 Saved to: {output_path}")
            
            print("\n📊 Preview:")
            for index, row in df.head(3).iterrows():
                print(f"- {row['Post Title'][:60]}... | URL: {row['Post URL']}")
        else:
            print("⚠️ No posts found.")
            
    except Exception as e:
        print(f"❌ Exa API Error: {e}")

if __name__ == "__main__":
    main()
