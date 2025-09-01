# export_logs_daily.py
import os, json, re, time
from typing import Final
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    # optional, for local dev; no-op on GitHub Actions if not installed
    from dotenv import load_dotenv  # pip install python-dotenv
    load_dotenv()
except Exception:
    pass

def require_env(name: str) -> str:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        raise EnvironmentError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env (local) or GitHub Actions Secrets."
        )
    return str(v)
#to avoid pylance warnings
PORTAL_LOGIN: Final[str] = require_env("PORTAL_LOGIN_URL")
PORTAL_USER:  Final[str] = require_env("PORTAL_USER")
PORTAL_PASS:  Final[str] = require_env("PORTAL_PASS")
LOGS_URL:     Final[str] = require_env("PORTAL_LOGS_URL") 
WEBAPP:       Final[str] = require_env("WEBAPP_URL")

# ---- pick the day to summarize ----
# default = "yesterday" in Europe/Rome so the day is complete
tz = ZoneInfo("Europe/Rome")
now_local = datetime.now(tz)
target_date = (now_local - timedelta(days=1)).date()
# allow override like LOGS_DATE=2025-08-25 when you run manually
override = os.getenv("LOGS_DATE")
if override:
    target_date = datetime.strptime(override, "%Y-%m-%d").date()

date_prefix = target_date.strftime("%Y-%m-%d")  # matches filenames like 2025-08-25_importDaemon_feed_86.log

def parse_metrics_from_text(text: str):
    """Return tuple: (aggiornare_total, modificati_json_total, cancellati_json_total) for ONE file."""
    aggiornare = sum(int(x) for x in re.findall(r'Prodotti da aggiornare su Google:?\s*(\d+)', text, flags=re.I))
    modificati = sum(int(x) for x in re.findall(r'fine prodotti modificati da mandare a google\s+\d+\/(\d+)', text, flags=re.I))

    # 'cancellati' needs local context around the 'Preparazione JSON prodotti cancellati...' line
    lines = text.splitlines()
    cancellati_total = 0
    for i, line in enumerate(lines):
        if 'Preparazione JSON prodotti cancellati da mandare a Google' in line:
            val = None
            # look near this line for a number
            for j in range(max(0, i-5), min(len(lines), i+6)):
                m = re.search(r'Prodotti da cancellare:?\s*(\d+)', lines[j], flags=re.I)
                if m:
                    val = int(m.group(1)); break
            if val is None:
                for j in range(i, min(len(lines), i+6)):
                    m = re.search(r'avanzamento\s+(\d+)\/\1', lines[j], flags=re.I)  # e.g., avanzamento 505/505
                    if m:
                        val = int(m.group(1)); break
            cancellati_total += (val or 0)

    return aggiornare, modificati, cancellati_total

def get_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)

def main():
    driver = get_driver()
    try:
        # ---- login ----
        driver.get(PORTAL_LOGIN)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, 'data[username]')))
        driver.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
        driver.find_element(By.NAME, "data[password]").send_keys(PORTAL_PASS)
        driver.find_element(By.ID, "login-submit").click()
        WebDriverWait(driver, 20).until(EC.url_contains("gestionale"))

        # ---- open logs (elFinder) ----
        driver.get(LOGS_URL)
        # wait until elFinder toolbar / cwd is ready
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".elfinder-toolbar, .elfinder-cwd")))
        time.sleep(1.0)

        # ---- inside the page, ask elFinder for the connector URL + current dir hash ----
        connector = driver.execute_script("""
            try {
              var $el = (window.$ || window.jQuery);
              var inst = $el('.elfinder').elfinder('instance');
              return inst && (inst.options?.url || inst.opts?.url);
            } catch(e) { return null; }
        """)
        if not connector:
            raise RuntimeError("Could not find elFinder connector URL on the page.")

        cwd_hash = driver.execute_script("""
            var $el = (window.$ || window.jQuery);
            var inst = $el('.elfinder').elfinder('instance');
            return inst.cwd().hash;
        """)

        # ---- list files in current folder via connector ----
        files = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const inst = (window.$ || window.jQuery)('.elfinder').elfinder('instance');
                const url  = inst.options?.url || inst.opts?.url;
                const cwd  = inst.cwd().hash;
                const body = new URLSearchParams({cmd:'open', target: cwd});
                const r = await fetch(url, {
                  method: 'POST',
                  credentials: 'same-origin',
                  headers: {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'},
                  body
                });
                const data = await r.json();
                done({ok:true, files: (data.files || [])});
              } catch (e) {
                done({ok:false, error:String(e)});
              }
            })();
        """)
        if not files.get("ok"):
            raise RuntimeError(f"elFinder open failed: {files.get('error')}")

        # pick all .log for the target day
        logs_today = [f for f in files["files"] if f.get("name","").endswith(".log") and f["name"].startswith(date_prefix)]
        names = [f["name"] for f in logs_today]
        hashes = [f["hash"] for f in logs_today]

        # ---- fetch file contents (only those names) ----
        texts = driver.execute_async_script("""
            const hashes = arguments[0];
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const inst = (window.$ || window.jQuery)('.elfinder').elfinder('instance');
                const url  = inst.options?.url || inst.opts?.url;
                const out = [];
                for (const h of hashes) {
                  const body = new URLSearchParams({cmd:'file', target: h});
                  const r = await fetch(url, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                              'X-Requested-With': 'XMLHttpRequest'},
                    body
                  });
                  const txt = await r.text();
                  out.push(txt);
                }
                done({ok:true, texts: out});
              } catch (e) {
                done({ok:false, error:String(e)});
              }
            })();
        """, hashes)

        if not texts.get("ok"):
            raise RuntimeError(f"elFinder file fetch failed: {texts.get('error')}")

        total_aggiornare = 0
        total_modificati = 0
        total_cancellati = 0

        for txt in texts["texts"]:
            a, m, c = parse_metrics_from_text(txt)
            total_aggiornare += a
            total_modificati += m
            total_cancellati += c

        payload = {
            "logDaily": {
                "date": target_date.strftime("%Y-%m-%d"),
                "aggiornare": total_aggiornare,    # Prodotti da aggiornare su Google (sum)
                "modificati": total_modificati,    # Preparazione JSON prodotti modificati ... (sum)
                "cancellati": total_cancellati,    # Preparazione JSON prodotti cancellati ... (sum)
                "files": names
            }
        }

        import requests
        r = requests.post(WEBAPP, json=payload, timeout=60)
        print("Posted daily log metrics:", r.text)

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
