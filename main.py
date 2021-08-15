import joblist as joblist
import requests
from bs4 import BeautifulSoup
import pandas as pd

def extract(page):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36'}
    url = (f'https://www.indeed.com/jobs?q=fpga%20engineer&l=California&vjk={page}')
    r = requests.get(url,headers)
    soup = BeautifulSoup(r.content,'html.parser')
    return soup

def transform(soup):
    divs = soup.find_all('div', class_='slider_container')
    for item in divs:
        job_title = item.find('h2', class_='jobTitle').text.strip()
        company = item.find('span', class_='companyName').text.strip()
        job_location = item.find('div', 'companyLocation').text.strip()
        post_date = item.find('span', 'date').text
        try:
            salary = item.find('span', class_='salary-snippet').text.strip()
        except:
            salary=''
        summary = item.find('div', class_='job-snippet').text.strip().replace('\n', ' ')

        job = {'Title': job_title,
               'Company':company,
               'Location':job_location,
               'Salary':salary,
               'Summary':summary,
               'Post Data':post_date}
        joblist.append(job)
    return

joblist =[]

for i in range(0,40,10):
    print(f'Getting page:, {i}')
    c = extract(0)
    transform(c)

df = pd.DataFrame(joblist)
print(df.head())
df.to_csv('Jobs.csv')
