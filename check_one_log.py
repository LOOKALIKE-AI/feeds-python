# check_one_log.py  — posts one-partner snapshot to Google Sheet (LogsPartners)
import os, re, time, json
from typing import Final

import requests
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
WEBAPP:       Final[str] = require_env("WEBAPP_URL")  # Apps Script web app URL

TARGET_PARTNER = os.getenv("TARGET_PARTNER", "").strip()
PREVIEW_ONLY = os.getenv("PREVIEW_ONLY", "").strip().lower() in ("1","true","yes","y")

def sum_matches(text: str, rx: str) -> int:
    total = 0
    for m in re.finditer(rx, text, flags=re.I):
        num = int(re.sub(r"[^\d]", "", m.group(1)) or "0")
        total += num
    return total

RX_ERRI   = r"prodotti\s+in\s+errore\s+google\s*:\s*([\d\.,]+)"
RX_ADD    = r"prodotti\s+da\s+aggiungere\s*:\s*([\d\.,]+)"
RX_UPDATE = r"prodotti\s+da\s+aggiornare\s+su\s+google\s*:\s*([\d\.,]+)"

def get_log_text_from_current_window(driver) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "pre").text
    except Exception:
        pass
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        pass
    try:
        iframe = driver.find_element(By.TAG_NAME, "iframe")
        driver.switch_to.frame(iframe)
        txt = driver.find_element(By.TAG_NAME, "body").text
        driver.switch_to.default_content()
        return txt
    except Exception:
        pass
    return driver.page_source

def find_log_button(actions_td):
    for css in ["button[title*='log' i]", "button[onclick*='_view_log']", "i.fa-file-text-o"]:
        try:
            el = actions_td.find_element(By.CSS_SELECTOR, css)
            if el.tag_name.lower() == "i":
                return el.find_element(By.XPATH, "./ancestor::button")
            return el
        except Exception:
            continue
    return None

opts = Options()
if os.getenv("SHOW_BROWSER", "").lower() not in ("1","true","yes","y"):
    opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--disable-popup-blocking")
opts.add_argument("--window-size=1400,1000")

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

    try:
        driver.execute_script("""
            if (window.jQuery && jQuery.fn.dataTable) {
              var t = jQuery('table.dataTable').DataTable();
              t.page.len(1000).draw(false);
            }
        """)
        time.sleep(0.5)
    except Exception:
        pass

    table = driver.find_element(By.CSS_SELECTOR, "table.dataTable")
    headers = [th.text.strip().lower() for th in table.find_elements(By.CSS_SELECTOR, "thead th")]
    desc_idx   = next((i for i,h in enumerate(headers) if "description" in h), 1)
    code_idx   = next((i for i,h in enumerate(headers) if "code" in h), 0)
    active_idx = next((i for i,h in enumerate(headers) if "active" in h), -1)

    chosen_row = None
    chosen_name = ""
    chosen_code = ""

    for tr in table.find_elements(By.CSS_SELECTOR, "tbody tr"):
        tds = tr.find_elements(By.TAG_NAME, "td")
        if not tds: 
            continue
        # active?
        is_active = False
        if 0 <= active_idx < len(tds):
            a = tds[active_idx]
            if "fa-check" in (a.get_attribute("innerHTML") or "") or "✓" in (a.text or ""):
                is_active = True
        if not is_active:
            continue

        code = (tds[code_idx].text or "").strip() if code_idx < len(tds) else ""
        name = (tds[desc_idx].text or "").strip() if desc_idx < len(tds) else ""

        if TARGET_PARTNER:
            if TARGET_PARTNER.lower() in (name + " " + code).lower():
                chosen_row, chosen_name, chosen_code = tr, name, code
                break
        else:
            chosen_row, chosen_name, chosen_code = tr, name, code
            break

    if not chosen_row:
        raise RuntimeError("No matching ACTIVE partner row found" + (f" for '{TARGET_PARTNER}'" if TARGET_PARTNER else ""))

    # open log
    actions_td = chosen_row.find_elements(By.TAG_NAME, "td")[-1]
    log_btn = find_log_button(actions_td)
    if not log_btn:
        raise RuntimeError("Couldn't locate the Log button in actions column for the chosen partner")

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", log_btn)
    time.sleep(0.1)

    before = set(driver.window_handles)
    log_btn.click()
    time.sleep(0.3)
    after = set(driver.window_handles)
    switched = False
    if len(after) > len(before):
        new_handle = list(after - before)[0]
        driver.switch_to.window(new_handle)
        switched = True

    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    text = get_log_text_from_current_window(driver)

    # parse & print
    prodotti_in_errore   = sum_matches(text, RX_ERRI)
    prodotti_da_aggiungere = sum_matches(text, RX_ADD)
    prodotti_da_aggiornare = sum_matches(text, RX_UPDATE)

    print("Partner:", f"{chosen_name} ({chosen_code})")
    print("Prodotti in errore Google:", prodotti_in_errore)
    print("Prodotti da aggiungere:", prodotti_da_aggiungere)
    print("Prodotti da aggiornare su Google:", prodotti_da_aggiornare)

    # POST to Apps Script -> LogsPartners
    payload = {
        "partnerLog": {
            "partner": chosen_name,
            "errore": prodotti_in_errore,
            "aggiungere": prodotti_da_aggiungere,
            "aggiornare": prodotti_da_aggiornare,
        }
    }

    if PREVIEW_ONLY:
        print("PREVIEW_ONLY on — not posting. Payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        r = requests.post(WEBAPP, json=payload, timeout=60)
        print("Sheet response:", r.text)

    if switched:
        driver.close()
        driver.switch_to.window(list(before)[0])

except Exception as e:
    print("Failed while checking one log.")
    print("URL:", driver.current_url)
    raise
finally:
    driver.quit()
