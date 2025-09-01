# export_feeds.py
import os, time, json, requests
from typing import Final
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from env_utils import load_env, require_env

load_env(".env")

PORTAL_LOGIN: Final[str] = require_env("PORTAL_LOGIN_URL")
PORTAL_FEEDS: Final[str] = require_env("PORTAL_FEEDS_URL")
PORTAL_USER:  Final[str] = require_env("PORTAL_USER")
PORTAL_PASS:  Final[str] = require_env("PORTAL_PASS")
WEBAPP:       Final[str] = require_env("WEBAPP_URL")

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
driver = webdriver.Chrome(options=opts)

try:
    # LOGIN
    driver.get(PORTAL_LOGIN)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, 'data[username]')))
    driver.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
    driver.find_element(By.NAME, "data[password]").send_keys(PORTAL_PASS)
    driver.find_element(By.ID, "login-submit").click()
    WebDriverWait(driver, 20).until(EC.url_contains("gestionale"))

    # FEEDS
    driver.get(PORTAL_FEEDS)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.dataTable tbody tr")))

    table = driver.find_element(By.CSS_SELECTOR, "table.dataTable")
    headers = [th.text.strip() for th in table.find_elements(By.CSS_SELECTOR, "thead th")]

    code_idx = next((i for i, h in enumerate(headers) if "code" in h.lower()), -1)
    desc_idx = next((i for i, h in enumerate(headers) if "description" in h.lower()), -1)
    active_idx = next((i for i, h in enumerate(headers) if "active" in h.lower()), -1)
    if code_idx == -1 or desc_idx == -1 or active_idx == -1:
        raise RuntimeError("Could not find required columns in table header")

    rows_data = []
    rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")

    for idx, tr in enumerate(rows, start=1):
        tds = tr.find_elements(By.TAG_NAME, "td")
        if len(tds) <= max(code_idx, desc_idx, active_idx):
            continue

        code = (tds[code_idx].text or "").strip()
        desc = (tds[desc_idx].text or "").strip()
        active_cell = tds[active_idx]
        active_text = (active_cell.text or "").strip().lower()
        inner_html = active_cell.get_attribute("innerHTML") or ""  # <-- coalesce None → ""

        is_active = ('✓' in active_text) or ("fa-check" in inner_html)

        if code or desc:
            rows_data.append({"S.No": idx, "Code": code, "Description": desc, "Active": is_active})

    print(f"Extracted {len(rows_data)} rows")

    # Transfer to Google Sheets
    res = requests.post(WEBAPP, json=rows_data, timeout=60)
    print("Sheet updated!", res.text)

except Exception as e:
    print("Could not complete the scraping task")
    print("Current page URL:", driver.current_url)
    print("Full error:")
    raise
finally:
    driver.quit()
