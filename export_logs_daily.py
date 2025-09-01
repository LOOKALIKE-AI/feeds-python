# export_logs_daily.py
import os, re, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Final, Tuple, List
from urllib.parse import urlparse, urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from env_utils import load_env, require_env, get_bool

# ========== ENV & DATE ==========
load_env(".env")

PORTAL_LOGIN: Final[str] = require_env("PORTAL_LOGIN_URL")
PORTAL_USER:  Final[str] = require_env("PORTAL_USER")
PORTAL_PASS:  Final[str] = require_env("PORTAL_PASS")
LOGS_URL:     Final[str] = require_env("PORTAL_LOGS_URL")  # https://ws.lookalike.shop/gestionale/elfinder/?log
WEBAPP:       Final[str] = os.getenv("WEBAPP_URL", "")
PREVIEW_ONLY: Final[bool] = get_bool("PREVIEW_ONLY", False)

tz = ZoneInfo("Europe/Rome")
target_date = (datetime.now(tz) - timedelta(days=1)).date()
LOGS_DATE_ENV = os.getenv("LOGS_DATE")
if LOGS_DATE_ENV:
    target_date = datetime.strptime(LOGS_DATE_ENV, "%Y-%m-%d").date()
# ========== BROWSER ==========
def get_driver():
    opts = Options()
    if not get_bool("SHOW_BROWSER", False):
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    return webdriver.Chrome(options=opts)

# ========== UTILS ==========
ZERO_WIDTH = r"[\u200B\u200C\u200D\u2060\uFEFF]"
def strip_zw(s: str) -> str:
    return re.sub(ZERO_WIDTH, "", s)

def normalize_text(s: str) -> str:
    s = re.sub(ZERO_WIDTH, "", s)
    s = s.replace("\u00A0", " ")              # NBSP -> space
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)            # collapse weird spaces
    return s

def dump_dom(driver):
    try:
        with open("elfinder_debug.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot("elfinder_debug.png")
        print("[debug] Wrote elfinder_debug.html and elfinder_debug.png")
    except Exception:
        pass
    print("[debug] URL:", driver.current_url)
    try: print("[debug] Title:", driver.title)
    except Exception: pass

def _find_elfinder(driver) -> bool:
    present = bool(driver.find_elements(By.CSS_SELECTOR, "div.elfinder, #elfinder"))
    if not present: return False
    return bool(driver.execute_script("""
      var $=window.$||window.jQuery; if(!$) return false;
      var el=$('.elfinder'); if(!el.length) el=$('#elfinder'); if(!el.length) return false;
      try { return !!el.elfinder('instance'); } catch(e){ return false; }
    """))

def _switch_iframe(driver) -> bool:
    for i, fr in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
        try:
            driver.switch_to.frame(fr)
            if _find_elfinder(driver):
                print(f"[logs] Switched into iframe #{i}")
                return True
            driver.switch_to.default_content()
        except Exception:
            driver.switch_to.default_content()
    return False

def wait_for_elfinder(driver, timeout=60):
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState")=="complete")
    if ("login" in driver.current_url.lower()) or driver.find_elements(By.NAME, "data[username]"):
        dump_dom(driver); raise RuntimeError("Redirected to login.")
    if _find_elfinder(driver): return
    if _switch_iframe(driver): return
    end=time.time()+timeout
    while time.time()<end:
        if _find_elfinder(driver) or _switch_iframe(driver): return
        time.sleep(0.5)
    dump_dom(driver); raise TimeoutException("elFinder not detected.")

def _origin(u:str)->str:
    p=urlparse(u); return f"{p.scheme}://{p.netloc}"

def goto_logs_page(driver, logs_url:str, timeout=60):
    # Always absolute (avoid /elfinder/elfinder/?log)
    href = logs_url if logs_url.startswith("http") else urljoin(_origin(PORTAL_LOGIN), "/gestionale/elfinder/?log")
    driver.get(href)
    # If redirected to Dashboard, click the nav link explicitly
    try:
        WebDriverWait(driver, 5).until(EC.title_contains("Dashboard"))
        link = driver.find_elements(By.CSS_SELECTOR, "a[href*='elfinder/?log']")
        if link:
            driver.execute_script("arguments[0].click();", link[0])
    except Exception:
        pass
    wait_for_elfinder(driver, timeout=timeout)

# ========== OPEN FOLDER & LIST ==========
def open_folder_and_list_dom(driver, folder_label="allegati-log") -> tuple[dict, List[dict]]:
    """Click the folder in the left tree and scrape file tiles. Returns cwd, rows[{name,hash,href}]."""
    result = driver.execute_async_script("""
      const label = (arguments[0]||'allegati-log').toLowerCase();
      const done  = arguments[arguments.length-1];
      (async()=>{
        try{
          const $=window.$||window.jQuery; const inst=$('.elfinder').elfinder('instance');
          if(!inst) return done({ok:false,error:'no instance'});
          // Try to find the folder in left tree, by visible text
          const nav = document.querySelector('.elfinder-navbar') || document;
          function findNode() {
            const nodes = nav.querySelectorAll('a, .elfinder-navbar-dir, .elfinder-navbar-root');
            for(const n of nodes){
              const txt=(n.textContent||'').trim().toLowerCase();
              if(txt===label) return n.closest('a')||n;
            }
            for(const n of nodes){
              const txt=(n.textContent||'').trim().toLowerCase();
              if(txt.includes(label)) return n.closest('a')||n;
            }
            return null;
          }
          const clickNode = findNode();
          if(clickNode){
            clickNode.scrollIntoView({block:'center', inline:'nearest'});
            clickNode.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
            clickNode.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
            clickNode.click();
          }
          // Wait for file tiles in cwd
          const deadline=Date.now()+12000;
          while(Date.now()<deadline){
            const tiles=[...document.querySelectorAll('.elfinder-cwd .elfinder-cwd-file')];
            if(tiles.length){
                const url = inst.options?.url || inst.opts?.url;
                const mk  = h => url + (url.includes('?') ? '&' : '?') + 'cmd=file&target=' + encodeURIComponent(h);

                const cwd = inst.cwd();
                const all = Object.values(inst.files ? inst.files() : {});
                const filesInCwd = all.filter(f => f && f.phash === cwd.hash && f.mime !== 'directory');

                const rows = filesInCwd.map(f => ({
                name: String(f.name || '').trim(),
                hash: f.hash,
                href: mk(f.hash)    // no download=1 (some connectors reject it)
                }));
              const bc=document.querySelector('.elfinder-path')||document.querySelector('.elfinder-breadcrumbs');
              const cwdName=bc?(bc.textContent||'').split('/').pop().trim():null;
              return done({ok:true, cwd:{name:cwdName}, rows});
            }
            await new Promise(r=>setTimeout(r,200));
          }
          return done({ok:false,error:'timeout waiting tiles'});
        }catch(e){ done({ok:false,error:String(e)}); }
      })();
    """, folder_label)

    if not result.get("ok"):
        raise RuntimeError(f"open/list failed: {result.get('error')}")
    cwd, rows = result["cwd"], result["rows"]
    # sanitize names (strip zero-width so date matching works)
    for r in rows: r["name"] = strip_zw(r["name"])
    return cwd, rows

# ========== DOWNLOAD ==========
def fetch_texts_via_get(driver, picked: List[dict]):
    resp = driver.execute_async_script("""
      const picked=arguments[0]; const done=arguments[arguments.length-1];
      (async()=>{
        try{
          const out=[];
          for(const r of picked){
            const res=await fetch(r.href,{method:'GET',credentials:'same-origin'});
            const txt=await res.text();
            out.push({name:r.name, text: txt});
          }
          done({ok:true,texts:out});
        }catch(e){ done({ok:false,error:String(e)}); }
      })();
    """, picked)
    if not resp.get("ok"):
        raise RuntimeError(f"GET file failed: {resp.get('error')}")
    # normalize
    for t in resp["texts"]:
        t["text"] = normalize_text(t["text"])
    return resp["texts"]

# ========== PARSING ==========
def parse_file(text: str) -> Tuple[int,int,int,dict]:
    # text already normalized by fetch_texts_via_get
    dbg = {"aggiornare_lines": [], "modificati_lines": [], "cancellati_lines": []}

    # --- aggiornare / update on Google ---
    patt_agg = [
        r'Prodotti da aggiornare su Google\s*[: ]\s*(\d+)',     # ITA
        r'Products to update on Google\s*[: ]\s*(\d+)',         # ENG (translated UI)
    ]
    aggiornare = 0
    for p in patt_agg:
        for m in re.finditer(p, text, re.I):
            aggiornare += int(m.group(1)); dbg["aggiornare_lines"].append(m.group(0))

    # --- modificati / modified to send ---
    modificati = 0
    # take the RIGHT number in "fine prodotti modificati da mandare a google X/Y" -> Y
    for m in re.finditer(r'fine prodotti modificati da mandare a google\s+\d+\/(\d+)', text, re.I):
        modificati += int(m.group(1)); dbg["modificati_lines"].append(m.group(0))
    for m in re.finditer(r'end of modified products to send to google\s+\d+\/(\d+)', text, re.I):
        modificati += int(m.group(1)); dbg["modificati_lines"].append(m.group(0))
    # loose fallbacks
    for m in re.finditer(r'Preparazione JSON prodotti modificati da mandare a Google.*?(\d+)\s*(?:/|\b)$', text, re.I|re.S):
        modificati += int(m.group(1)); dbg["modificati_lines"].append(m.group(0))

    # --- cancellati / deleted to send ---
    cancellati = 0
    # standard “Prodotti da cancellare: X”
    for m in re.finditer(r'Prodotti da cancellare\s*[: ]\s*(\d+)', text, re.I):
        cancellati += int(m.group(1)); dbg["cancellati_lines"].append(m.group(0))
    # “fine prodotti cancellati da mandare a google X/Y” -> Y
    for m in re.finditer(r'fine prodotti cancellati da mandare a google\s+\d+\/(\d+)', text, re.I):
        cancellati += int(m.group(1)); dbg["cancellati_lines"].append(m.group(0))
    for m in re.finditer(r'end of deleted products to send to google\s+\d+\/(\d+)', text, re.I):
        cancellati += int(m.group(1)); dbg["cancellati_lines"].append(m.group(0))

    return aggiornare, modificati, cancellati, dbg

# ========== POST / SUMMARY ==========
def post_to_sheet(payload: dict):
    import requests
    if not WEBAPP:
        raise EnvironmentError("WEBAPP_URL is required when PREVIEW_ONLY is false.")
    r = requests.post(WEBAPP, json={"logDaily": payload}, timeout=60)
    print("[logs] Posted daily log metrics:", r.text)

def write_summary(totals: dict, per_file: List[dict]):
    print("\nPer-file parse:")
    if per_file:
        for row in per_file:
            print(f"  {row['file']}: aggiornare={row['aggiornare']}  modificati={row['modificati']}  cancellati={row['cancellati']}")
            for k in ("aggiornare_lines","modificati_lines","cancellati_lines"):
                for ln in row["debug"].get(k, [])[:3]:
                    print("      •", ln[:180])
    else:
        print("  <no files>")
    print(f"\nTOTAL {totals['date']}: aggiornare={totals['aggiornare']}, modificati={totals['modificati']}, cancellati={totals['cancellati']}\n")

# ========== MAIN ==========
def main():
    if not PREVIEW_ONLY and not WEBAPP:
        raise EnvironmentError("WEBAPP_URL is required when PREVIEW_ONLY is false.")
    print(f"[logs] Target date: {target_date}  PREVIEW_ONLY={PREVIEW_ONLY}")

    driver = get_driver()
    try:
        # login
        driver.get(PORTAL_LOGIN)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, 'data[username]')))
        driver.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
        driver.find_element(By.NAME, "data[password]").send_keys(PORTAL_PASS)
        driver.find_element(By.ID, "login-submit").click()
        WebDriverWait(driver, 20).until(EC.url_contains("gestionale"))

        # go to logs
        goto_logs_page(driver, LOGS_URL, timeout=60)

        # open folder and list files
        # (your portal shows it as "allegati-log")
        cwd, rows = open_folder_and_list_dom(driver, folder_label="allegati-log")
        print("[logs] cwd:", cwd)
        print("[logs] sample names in folder (first 50):", [r["name"] for r in rows[:50]])

        # filter today’s .log (filenames had zero-width chars; we stripped them above)
        prefix = target_date.strftime("%Y-%m-%d")
        picked = [r for r in rows if r["name"].endswith(".log") and (prefix in r["name"])]
        print(f"[logs] Found {len(picked)} log file(s) for {target_date}: {[p['name'] for p in picked]}")

        if not picked:
            totals = {"date": str(target_date), "aggiornare": 0, "modificati": 0, "cancellati": 0, "files": []}
            if PREVIEW_ONLY: write_summary(totals, [])
            else: post_to_sheet(totals)
            return

        # download RAW text
        texts = fetch_texts_via_get(driver, picked)   # [{name,text}]
        names = [t["name"] for t in texts]
        file_texts = [t["text"] for t in texts]
        print("[logs] will parse files:", names)

        # (debug) show first file snippet escaped
        if PREVIEW_ONLY and file_texts:
            snip = file_texts[0][:800]
            print("\n[debug] First file snippet (escaped):\n", snip.encode("unicode_escape").decode("ascii"), "\n")

        # parse + sum
        per_file_rows=[]; A=M=C=0
        for name, txt in zip(names, file_texts):
            a,m,c,dbg = parse_file(txt)
            A+=a; M+=m; C+=c
            per_file_rows.append({"file":name,"aggiornare":a,"modificati":m,"cancellati":c,"debug":dbg})
        totals = {"date": str(target_date), "aggiornare": A, "modificati": M, "cancellati": C, "files": names}

        if PREVIEW_ONLY: write_summary(totals, per_file_rows)
        else:
            print(f"[logs] TOTALS → aggiornare={A}, modificati={M}, cancellati={C}")
            post_to_sheet(totals)

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
