import requests
from bs4 import BeautifulSoup
import time

url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Quality%20Assurance%20Manager&start=0"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}
response = requests.get(url, headers=headers)
print(f"Status Code: {response.status_code}")
if response.status_code == 200:
    soup = BeautifulSoup(response.text, 'html.parser')
    jobs = soup.find_all('li')
    print(f"Found {len(jobs)} jobs in this batch.")
    if len(jobs) > 0:
        title = jobs[0].find('h3', class_='base-search-card__title')
        if title:
            print(f"First job title: {title.text.strip()}")
