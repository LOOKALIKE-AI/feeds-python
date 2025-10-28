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

WAIT_TIMEOUT     = int(_env("WAIT_TIMEOUT","30"))
BETWEEN_STEPS_S  = float(_env("BETWEEN_STEPS_S","0.3"))
ONLY_ACTIVE      = True  # collect only active rows
STRICT_MATCH     = _env("STRICT_MATCH", "1").lower() in ("1","true","yes","y")

def log(*a): print("[logids]", *a, flush=True)

def fetch_active_list() -> List[Dict]:
    """Fetch active partners from the 'Active' sheet via Apps Script."""
    try:
        payload = {"getActiveList": {"sheetName": "Active"}}
        r = requests.post(WEBAPP_URL, json=payload, timeout=60)
        j = r.json()
        if not j.get("ok"):
            log("WARN: getActiveList returned not-ok:", j)
            return []
        rows = j.get("rows", [])
        out = []
        for x in rows:
            code = str(x.get("code", "")).strip()
            partner = str(x.get("partner", "")).strip()
            if code or partner:
                out.append({"code": code, "partner": partner})
        return out
    except Exception as e:
        log("WARN: getActiveList failed:", e)
        return []

def is_active_cell(td) -> bool:
    try:
        html = (td.get_attribute("innerHTML") or "").lower()
        txt  = (td.text or "").strip()
        return ("fa-check" in html) or ("✓" in txt)
    except Exception:
        return False

def extract_feed_id_from_row(tds) -> int | None:
    # 1) try first cell's textContent (hidden cells may have empty .text)
    try:
        if tds:
            for attr in ("textContent", "data-id", "data-feedid", "data-feed-id"):
                v = (tds[0].get_attribute(attr) or "").strip()
                if v.isdigit():
                    return int(v)
    except Exception:
        pass

    # 2) inspect last cell + its children
    try:
        actions_td = tds[-1] if tds else None
        if actions_td:
            for attr in ("data-id", "data-feedid", "data-feed-id"):
                v = (actions_td.get_attribute(attr) or "").strip()
                if v.isdigit():
                    return int(v)

            els = actions_td.find_elements(By.CSS_SELECTOR, "a,button,[onclick],[href],[data-id],[data-feedid],[data-feed-id]")
            for el in els:
                # data-*
                for attr in ("data-id", "data-feedid", "data-feed-id"):
                    v = (el.get_attribute(attr) or "").strip()
                    if v.isdigit():
                        return int(v)

                # href patterns
                href = (el.get_attribute("href") or "")
                m = re.search(r"[?&#](?:id|feed(?:_|)id)=(\d+)", href, re.I) or \
                    re.search(r"/(?:feed|feeds?)/(\d+)(?:\D|$)", href, re.I)
                if m:
                    return int(m.group(1))

                # onclick patterns
                oc = (el.get_attribute("onclick") or "")
                m = re.search(r"\((\d+)\)", oc) or \
                    re.search(r"\bid\s*[:=]\s*(\d+)\b", oc, re.I) or \
                    re.search(r"feed(?:_|)id\s*[:=]\s*(\d+)\b", oc, re.I)
                if m:
                    return int(m.group(1))
    except Exception:
        pass

    return None

def switch_into_iframe_with_table(driver) -> None:
    """If the feeds table lives inside an iframe, switch into it."""
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for fr in frames:
            driver.switch_to.frame(fr)
            if driver.find_elements(By.CSS_SELECTOR, "table.dataTable"):
                log("Switched into iframe with DataTable")
                return
            driver.switch_to.default_content()
    except Exception as e:
        log("Iframe scan skipped/failed:", e)
    # leave in default content if none found

def wait_for_table_ready(driver) -> None:
    """Wait until the table is present and either rows exist or processing overlay is gone."""
    # ensure DOM ready
    WebDriverWait(driver, WAIT_TIMEOUT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    # if table is inside an iframe, switch now
    switch_into_iframe_with_table(driver)

    # wait for the table element to exist
    WebDriverWait(driver, WAIT_TIMEOUT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "table.dataTable"))
    )

    # then wait for either rows to appear OR processing overlay to disappear
    WebDriverWait(driver, WAIT_TIMEOUT).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, "table.dataTable tbody tr")
                  or not d.find_elements(By.CSS_SELECTOR, ".dataTables_processing")
    )

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
        log("After nav to FEEDS => URL:", driver.current_url, "Title:", driver.title)
        time.sleep(0.5)  # small settle

        # Robust wait for table readiness (handles iframe + AJAX)
        wait_for_table_ready(driver)
        time.sleep(BETWEEN_STEPS_S)  # tiny settle

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
            if not tds:
                continue

            # Active filter
            if ONLY_ACTIVE and 0 <= active_idx < len(tds):
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

        # --- strict count guard vs Active sheet ---
        active_list = fetch_active_list()
        expected = len(active_list)
        actual = len(rows_out)

        if STRICT_MATCH and expected > 0 and actual != expected:
            # Prefer matching by Code; fall back to Partner names
            act_codes = {x["code"] for x in active_list if x.get("code")}
            scr_codes = {x["code"] for x in rows_out if x.get("code")}
            missing_codes = sorted(act_codes - scr_codes)

            # Also compute partner-based diff as a fallback (name normalized)
            norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())
            act_partners = {norm(x["partner"]) for x in active_list if x.get("partner")}
            scr_partners = {norm(x["partner"]) for x in rows_out if x.get("partner")}
            missing_partners = sorted(p for p in (act_partners - scr_partners) if p)

            log(f"STRICT MISMATCH: Active={expected} vs Scraped={actual}. Aborting write.")
            if missing_codes:
                log(f"Missing by CODE (first 10): {missing_codes[:10]}")
            elif missing_partners:
                log(f"Missing by PARTNER (first 10): {missing_partners[:10]}")
            raise SystemExit(2)
        # --- end strict guard ---

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
