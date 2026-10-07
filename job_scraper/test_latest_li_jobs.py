import requests
from bs4 import BeautifulSoup

# Added f_TPR=r86400 (Past 24 hours) and sortBy=DD (Date Descending/Newest)
url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Quality%20Assurance%20Manager&f_TPR=r86400&sortBy=DD&start=0"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

response = requests.get(url, headers=headers)
print(f"Status Code: {response.status_code}")

if response.status_code == 200:
    soup = BeautifulSoup(response.text, 'html.parser')
    jobs = soup.find_all('li')
    print(f"Found {len(jobs)} 'LATEST' jobs in this batch.")
    for job in jobs[:3]:
        title = job.find('h3', class_='base-search-card__title')
        time_elem = job.find('time', class_='job-search-card__listdate--new') or job.find('time', class_='job-search-card__listdate')
        if title:
            post_time = time_elem.text.strip() if time_elem else "Unknown time"
            print(f"- {title.text.strip()} (Posted: {post_time})")
