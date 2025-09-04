import os, time, json, re
from typing import Final, List, Dict
import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# -------- env helpers --------
def load_env_here(filename: str = ".env") -> None:
    here = os.path.dirname(__file__)
    path = os.path.join(here, filename)
    if not os.path.exists(path): return
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and (k not in os.environ):
                os.environ[k] = v

def _env(k: str, d: str = "") -> str:
    v = os.getenv(k, d)
    return d if v is None else str(v)

load_env_here()

PORTAL_LOGIN_URL: Final[str] = _env("PORTAL_LOGIN_URL")
PORTAL_FEEDS_URL: Final[str] = _env("PORTAL_FEEDS_URL")
PORTAL_USER:      Final[str] = _env("PORTAL_USER")
PORTAL_PASS:      Final[str] = _env("PORTAL_PASS")
WEBAPP_URL:       Final[str] = _env("WEBAPP_URL")

WAIT_TIMEOUT = int(_env("WAIT_TIMEOUT","30"))
BETWEEN_STEPS_S = float(_env("BETWEEN_STEPS_S","0.3"))
ONLY_ACTIVE = True  # collect only active rows

def log(*a): print("[logids]", *a, flush=True)

def is_active_cell(td) -> bool:
    try:
        html = (td.get_attribute("innerHTML") or "").lower()
        txt  = (td.text or "").strip()
        return ("fa-check" in html) or ("✓" in txt)
    except Exception:
        return False

def extract_feed_id_from_row(tds) -> int | None:
    # primary: first hidden td
    try:
        if tds:
            text = (tds[0].text or "").strip()
            if text.isdigit():
                return int(text)
    except Exception:
        pass
    # fallback: parse onclicks in last cell
    try:
        actions_td = tds[-1]
        btns = actions_td.find_elements(By.CSS_SELECTOR, "[onclick]")
        for b in btns:
            oc = b.get_attribute("onclick") or ""
            m = re.search(r"\((\d+)\)", oc)
            if m: return int(m.group(1))
    except Exception:
        pass
    return None

def main():
    if not all([PORTAL_LOGIN_URL, PORTAL_FEEDS_URL, PORTAL_USER, PORTAL_PASS, WEBAPP_URL]):
        raise SystemExit("Missing one or more env vars: PORTAL_LOGIN_URL, PORTAL_FEEDS_URL, PORTAL_USER, PORTAL_PASS, WEBAPP_URL")

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,1000")

    driver = webdriver.Chrome(options=opts)
    rows_out: List[Dict] = []
    try:
        # Login
        driver.get(PORTAL_LOGIN_URL)
        WebDriverWait(driver, WAIT_TIMEOUT).until(EC.presence_of_element_located((By.NAME, "data[username]")))
        driver.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
        driver.find_element(By.NAME, "data[password]").send_keys(PORTAL_PASS)
        driver.find_element(By.ID, "login-submit").click()
        WebDriverWait(driver, WAIT_TIMEOUT).until(EC.url_contains("gestionale"))

        # Feeds
        driver.get(PORTAL_FEEDS_URL)
        WebDriverWait(driver, WAIT_TIMEOUT).until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.dataTable tbody tr")))
        time.sleep(BETWEEN_STEPS_S)  # small settle

        # Read headers to locate columns
        table = driver.find_element(By.CSS_SELECTOR, "table.dataTable")
        headers = [th.text.strip().lower() for th in table.find_elements(By.CSS_SELECTOR, "thead th")]
        # First <td> is hidden FeedID -> we won't rely on a header name for it
        code_idx   = next((i for i,h in enumerate(headers) if "code" in h or "codice" in h), 1)
        desc_idx   = next((i for i,h in enumerate(headers) if "description" in h or "descrizione" in h), 2)
        active_idx = next((i for i,h in enumerate(headers) if "active" in h or "attivo" in h), -1)

        rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        log(f"Rows detected: {len(rows)}")

        for r in rows:
            tds = r.find_elements(By.TAG_NAME, "td")
            if not tds: continue

            # Active filter
            if ONLY_ACTIVE and active_idx >= 0 and active_idx < len(tds):
                if not is_active_cell(tds[active_idx]):
                    continue

            feed_id = extract_feed_id_from_row(tds)
            if not feed_id: 
                continue

            code = (tds[code_idx].text or "").strip() if code_idx < len(tds) else ""
            partner = (tds[desc_idx].text or "").strip() if desc_idx < len(tds) else ""
            rows_out.append({
                "partner": partner,
                "code": code,
                "feedId": feed_id,
                "active": True
            })

        log(f"Collected {len(rows_out)} active LogIDs; posting to sheet...")
        payload = {
            "upsertLogIDs": {
                "sheetName": "LogIDs",
                "clearFirst": True,
                "rows": rows_out
            }
        }
        resp = requests.post(WEBAPP_URL, json=payload, timeout=120)
        try:
            j = resp.json()
        except Exception:
            j = {"status": resp.status_code, "text": resp.text[:200]}
        log("Upsert:", j)

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
