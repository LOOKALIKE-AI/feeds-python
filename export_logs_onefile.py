# export_logs_onefile.py
import os, re, time, pathlib, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Final, Tuple, List, Dict
from urllib.parse import urlparse, urljoin, quote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from env_utils import load_env, require_env, get_bool

# ========= ENV & DATE =========
load_env(".env")

PORTAL_LOGIN: Final[str] = require_env("PORTAL_LOGIN_URL")
PORTAL_USER:  Final[str] = require_env("PORTAL_USER")
PORTAL_PASS:  Final[str] = require_env("PORTAL_PASS")
LOGS_URL:     Final[str] = require_env("PORTAL_LOGS_URL")  # e.g. https://.../gestionale/elfinder/?log
ONE_FILE_NAME:Final[str] = os.getenv("ONE_FILE_NAME", "")  # optional: exact file name to fetch

tz = ZoneInfo("Europe/Rome")
target_date = (datetime.now(tz) - timedelta(days=1)).date()
if os.getenv("LOGS_DATE"):
    target_date = datetime.strptime(os.getenv("LOGS_DATE"), "%Y-%m-%d").date()

# ========= BROWSER =========
def get_driver():
    opts = Options()
    if not get_bool("SHOW_BROWSER", False):
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    return webdriver.Chrome(options=opts)

# ========= elFinder detection =========
def _find_elfinder(driver) -> bool:
    present = bool(driver.find_elements(By.CSS_SELECTOR, "div.elfinder, #elfinder"))
    if not present: 
        return False
    return bool(driver.execute_script("""
      var $=window.$||window.jQuery; if(!$) return false;
      var el=$('.elfinder'); if(!el.length) el=$('#elfinder'); if(!el.length) return false;
      try { return !!el.elfinder('instance'); } catch(e){ return false; }
    """))

def _switch_iframe(driver) -> bool:
    for fr in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.frame(fr)
            if _find_elfinder(driver):
                return True
            driver.switch_to.default_content()
        except Exception:
            driver.switch_to.default_content()
    return False

def wait_for_elfinder(driver, timeout=30):
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState")=="complete")
    if _find_elfinder(driver): return
    if _switch_iframe(driver): return
    end=time.time()+timeout
    while time.time()<end:
        if _find_elfinder(driver) or _switch_iframe(driver): return
        time.sleep(0.5)
    raise TimeoutException("elFinder not detected.")

def goto_logs_page(driver, logs_url:str, timeout=60):
    href = logs_url if logs_url.startswith("http") else urljoin(driver.current_url, "/gestionale/elfinder/?log")
    driver.get(href)
    wait_for_elfinder(driver, timeout=timeout)

# ========= LIST & PICK ONE =========
ZERO_WIDTH = r"[\u200B\u200C\u200D\u2060\uFEFF]"
def strip_zw(s:str)->str:
    import re
    return re.sub(ZERO_WIDTH, "", s)

def open_folder_and_list_dom(driver, folder_label="allegati-log") -> tuple[Dict, List[Dict]]:
    result = driver.execute_async_script("""
      const label = (arguments[0]||'allegati-log').toLowerCase();
      const done  = arguments[arguments.length-1];
      (async()=>{
        try{
          const $=window.$||window.jQuery; const inst=$('.elfinder').elfinder('instance');
          if(!inst) return done({ok:false,error:'no instance'});
          // click the folder in the left navbar (best-effort)
          const nav=document.querySelector('.elfinder-navbar')||document;
          const nodes=nav.querySelectorAll('a,.elfinder-navbar-dir,.elfinder-navbar-root');
          let node=null;
          for(const n of nodes){
            const t=(n.textContent||'').trim().toLowerCase();
            if(t===label || t.includes(label)){ node=n.closest('a')||n; break; }
          }
          if(node){ node.click(); }
          // wait for cwd files
          const deadline=Date.now()+12000;
          while(Date.now()<deadline){
            const tiles=[...document.querySelectorAll('.elfinder-cwd .elfinder-cwd-file')];
            if(tiles.length){
              const inst=$('.elfinder').elfinder('instance');
              const url=inst.options?.url || inst.opts?.url;
              const cwd=inst.cwd();
              const all=Object.values(inst.files ? inst.files() : {});
              const files=all.filter(f=>f && f.phash===cwd.hash && f.mime!=='directory');
              const mk=h => url + (url.includes('?')?'&':'?') + 'cmd=file&target='+encodeURIComponent(h);
              const rows=files.map(f=>({name:String(f.name||'').trim(), hash:f.hash, href:mk(f.hash)}));
              return done({ok:true, rows, connector:url});
            }
            await new Promise(r=>setTimeout(r,200));
          }
          done({ok:false,error:'timeout'});
        }catch(e){ done({ok:false,error:String(e)}); }
      })();
    """, folder_label)
    if not result.get("ok"):
        raise RuntimeError("open/list failed: "+result.get("error","unknown"))
    rows = result["rows"]
    for r in rows: r["name"] = strip_zw(r["name"])
    return result["connector"], rows

# ========= DOWNLOAD ONE (NO getfile) =========
def requests_session_from_driver(driver) -> requests.Session:
    s = requests.Session()
    for c in driver.get_cookies():
        args = {k: c[k] for k in ("domain","path","secure","expires") if k in c and c[k] is not None}
        s.cookies.set(c["name"], c["value"], **args)
    return s

def make_absolute(base_url: str, maybe_rel: str) -> str:
    # turn '../elfinder/php/connector.main.php' into an absolute URL
    return urljoin(base_url, maybe_rel)

def fetch_one_log_text(driver, row, connector_rel: str) -> str:
    sess = requests_session_from_driver(driver)
    # resolve the connector to an absolute URL using the current page URL
    connector_abs = make_absolute(driver.current_url, connector_rel)
    # try a few variants
    base = connector_abs + ('&' if '?' in connector_abs else '?')
    candidates = [
        f"{base}cmd=file&target={quote(row['hash'])}&download=1",
        f"{base}cmd=file&target={quote(row['hash'])}",
    ]
    # also try from the row href (in case it had a different base)
    href_abs = make_absolute(driver.current_url, row['href'])
    if 'download=1' not in href_abs:
        sep = '&' if '?' in href_abs else '?'
        candidates.append(href_abs + sep + 'download=1')
    candidates.append(href_abs)

    last_err = None
    for u in candidates:
        try:
            r = sess.get(u, timeout=60)
            if r.ok and r.text and "File not found" not in r.text:
                return r.text
            last_err = f"HTTP {r.status_code}, len={len(r.text)}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

    raise RuntimeError(f"Failed to fetch log via connector. Last: {last_err}")

# ========= PARSE (3 counters) =========
def normalize_text(s: str) -> str:
    import re
    s = re.sub(ZERO_WIDTH, "", s)
    s = s.replace("\u00A0", " ")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    return s

def parse_three(text: str) -> Tuple[int,int,int,dict]:
    dbg = {"aggiornare_lines": [], "modificati_lines": [], "cancellati_lines": []}

    def grab(pats, take_max=False):
        vals, lines = [], []
        for p in pats:
            for m in re.finditer(p, text, re.I|re.M|re.S):
                g = m.group(1)
                if "/" in g:
                    import re as _re
                    n = int(_re.split(r"\D+", g.strip())[-1])
                else:
                    import re as _re
                    mnum = _re.search(r"\d+", g)
                    if not mnum: 
                        continue
                    n = int(mnum.group())
                vals.append(n); lines.append(m.group(0))
        if not vals: return 0, []
        return (max(vals) if take_max else sum(vals)), lines

    aggiornare, L = grab([r'Prodotti\s+da\s+aggiornare\s+su\s+Google\s*[: ]\s*([\d\s/]+)'])
    dbg["aggiornare_lines"] += L

    modificati, L = grab([r'Preparazione\s+JSON\s+prodotti\s+modificati\s+da\s+mandare\s+a\s+Google[^\n]*?([\d\s/]+)'], take_max=True)
    dbg["modificati_lines"] += L

    cancellati, L = grab([r'Preparazione\s+JSON\s+prodotti\s+cancellati\s+da\s+mandare\s+a\s+Google[^\n]*?([\d\s/]+)'], take_max=True)
    dbg["cancellati_lines"] += L

    return aggiornare, modificati, cancellati, dbg

# ========= MAIN (one-file proof) =========
def main():
    print(f"[onefile] Target date: {target_date}")
    driver = get_driver()
    try:
        # login
        driver.get(PORTAL_LOGIN)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, 'data[username]')))
        driver.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
        driver.find_element(By.NAME, "data[password]").send_keys(PORTAL_PASS)
        driver.find_element(By.ID, "login-submit").click()
        WebDriverWait(driver, 20).until(EC.url_contains("gestionale"))

        # logs page
        goto_logs_page(driver, LOGS_URL, timeout=40)

        # list folder
        connector_rel, rows = open_folder_and_list_dom(driver, folder_label="allegati-log")
        print("[onefile] connector (raw):", connector_rel)

        # pick one file
        prefix = target_date.strftime("%Y-%m-%d")
        candidates = [r for r in rows if r["name"].endswith(".log") and prefix in r["name"]]
        if ONE_FILE_NAME:
            picked = next((r for r in candidates if r["name"] == ONE_FILE_NAME), None)
            if not picked:
                raise RuntimeError(f"Requested ONE_FILE_NAME not found: {ONE_FILE_NAME}")
        else:
            if not candidates:
                raise RuntimeError(f"No .log files for {prefix}")
            picked = candidates[0]

        print("[onefile] fetching:", picked["name"])

        # fetch raw text (no getfile, no UI)
        raw = fetch_one_log_text(driver, picked, connector_rel)
        txt = normalize_text(raw)

        # preview + parse
        print("[onefile] snippet:", (txt[:350].encode("unicode_escape").decode("ascii")).replace("\\n","\\n\n"))
        a,m,c,dbg = parse_three(txt)
        print(f"[onefile] parsed → aggiornare={a}, modificati={m}, cancellati={c}")
        for k in ("aggiornare_lines","modificati_lines","cancellati_lines"):
            for ln in dbg[k][:2]:
                print("   •", ln[:180])

    finally:
        try: driver.quit()
        except: pass

if __name__ == "__main__":
    main()
