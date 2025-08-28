import os
import time
import json
import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait              
from selenium.webdriver.support import expected_conditions as EC     

options = Options()
options.add_argument('--headless') 
options.add_argument('--no-sandbox') 
driver = webdriver.Chrome(options=options)

load_dotenv()

PORTAL_LOGIN = os.getenv("PORTAL_LOGIN_URL")
PORTAL_FEEDS = os.getenv("PORTAL_FEEDS_URL")
PORTAL_USER = os.getenv("PORTAL_USER")
PORTAL_PASS = os.getenv("PORTAL_PASS")
WEBAPP = os.getenv("WEBAPP_URL")

try:
    # LOGIN
    print("Opening login page!")
    driver.get(PORTAL_LOGIN)
    time.sleep(2)

    driver.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
    driver.find_element(By.NAME,"data[password]").send_keys(PORTAL_PASS)
    driver.find_element(By.ID, "login-submit").click()
    time.sleep(3)

    # Feeds
    driver.get(PORTAL_FEEDS)

 
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.dataTable tbody tr"))
        )
    except Exception:
        print(" Could not find the table rows within 20 seconds")
        raise

    table = driver.find_element(By.CSS_SELECTOR, "table.dataTable")

    headers = [th.text.strip() for th in table.find_elements(By.CSS_SELECTOR, "thead th")]
    print("Extracted headers:", headers)

    code_idx = next((i for i, h in enumerate(headers) if "code" in h.lower()), -1)
    desc_idx = next((i for i, h in enumerate(headers) if "description" in h.lower()), -1)
    active_idx = next((i for i, h in enumerate(headers) if "active" in h.lower()), -1)

    if code_idx == -1 or desc_idx == -1 or active_idx == -1:
        raise Exception("Could not find required columns in table header")

    rows_data = []
    rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")

    for idx,tr in enumerate(rows, start=1):
        tds = tr.find_elements(By.TAG_NAME, "td")
        if len(tds) < max(code_idx, desc_idx, active_idx) + 1:
            continue

        code = tds[code_idx].text.strip()
        desc = tds[desc_idx].text.strip()
        active_cell = tds[active_idx]
        active_text = active_cell.text.strip().lower()

        is_active = ('✓' in active_text or "fa-check" in active_cell.get_attribute("innerHTML"))

        if code or desc:
            rows_data.append({"S.No": idx,"Code": code, "Description": desc, "Active": is_active})  # ✅ Fixed typo 'Ative' → 'Active'

    print(f"Extracted {len(rows_data)} rows")

    # Transfer to Google Sheets
    res = requests.post(WEBAPP, json=rows_data)
    print("Sheet updated!", res.text)

except Exception as e:
    print("Could not complete the scraping task")
    print("Current page URL:", driver.current_url)
    print("Page HTML snapshot:")
    print(driver.page_source[:1500])  # Print first 1500 characters of HTML
    print("Full error:")
    raise e

finally:
    driver.quit()
