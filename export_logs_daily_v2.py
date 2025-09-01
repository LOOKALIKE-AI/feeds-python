# export_logs_daily_v2.py
import os, re, time, pathlib, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Final, Tuple, List, Dict
from urllib.parse import urlparse, urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from env_utils import load_env, require_env, get_bool

# =========================
# ENV & DATE
# =========================
load_env(".env")

PORTAL_LOGIN: Final[str] = require_env("PORTAL_LOGIN_URL")
PORTAL_USER:  Final[str] = require_env("PORTAL_USER")
PORTAL_PASS:  Final[str] = require_env("PORTAL_PASS")
LOGS_URL:     Final[str] = require_env("PORTAL_LOGS_URL")  # e.g. https://ws.lookalike.shop/gestionale/elfinder/?log
WEBAPP:       Final[str] = os.getenv("WEBAPP_URL", "")
PREVIEW_ONLY: Final[bool] = get_bool("PREVIEW_ONLY", False)

tz = ZoneInfo("Europe/Rome")
target_date = (datetime.now(tz) - timedelta(days=1)).date()
LOGS_DATE_ENV = os.getenv("LOGS_DATE")
if LOGS_DATE_ENV:
    target_date = datetime.strptime(LOGS_DATE_ENV, "%Y-%m-%d").date()

# =========================
# BROWSER
# =========================
def get_driver():
    opts = Options()
    if not get_bool("SHOW_BROWSER", False):
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    return webdriver.Chrome(options=opts)

# =========================
# UTILS
# =========================
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
    try:
        print("[debug] Title:", driver.title)
    except Exception:
        pass

def _find_elfinder(driver) -> bool:
    present = bool(driver.find_elements(By.CSS_SELECTOR, "div.elfinder, #elfinder"))
    if not present: 
        return False
    return bool(driver.execute_script("""
      var $ = window.$ || window.jQuery; if (!$) return false;
      var el = $('.elfinder'); if (!el.length) el = $('#elfinder'); if (!el.length) return false;
      try { return !!el.elfinder('instance'); } catch(e) { return false; }
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
    if _find_elfinder(driver): 
        return
    if _switch_iframe(driver): 
        return
    end = time.time() + timeout
    while time.time() < end:
        if _find_elfinder(driver) or _switch_iframe(driver): 
            return
        time.sleep(0.5)
    dump_dom(driver); raise TimeoutException("elFinder not detected.")

def _origin(u:str)->str:
    p=urlparse(u); return f"{p.scheme}://{p.netloc}"

def _try_click_reports_logs(driver) -> bool:
    # Try to open "Reports" dropdown, then click "Log Files" (or the elfinder log link)
    try:
        driver.execute_script("""
          (function(){
            function norm(s){return (s||'').replace(/\s+/g,' ').trim().toLowerCase();}
            const as = Array.from(document.querySelectorAll('a'));
            // Find a likely "Reports" toggle
            let rep = as.find(a => /reports/.test(norm(a.textContent)) && (
              a.classList.contains('dropdown-toggle') || a.getAttribute('data-toggle')==='dropdown' ||
              (a.nextElementSibling && a.nextElementSibling.classList.contains('dropdown-menu'))
            ));
            if (rep) { rep.click(); }
            // Find the "Log Files" item or /elfinder/?log link
            let link = as.find(a => {
              const href = (a.getAttribute('href')||'').toLowerCase();
              const txt  = norm(a.textContent);
              return href.includes('elfinder/?log') || /log files/.test(txt) || /log/.test(txt) && /file/.test(txt);
            });
            if (link) { link.click(); return true; }
            return false;
          })();
        """)
        return True
    except Exception:
        return False

def goto_logs_page(driver, logs_url:str, timeout=60):
    href = logs_url if logs_url.startswith("http") else urljoin(_origin(PORTAL_LOGIN), "/gestionale/elfinder/?log")
    driver.get(href)
    try:
        wait_for_elfinder(driver, timeout=8)
        return
    except Exception:
        pass
    # if still not there, try clicking menu path
    try:
        WebDriverWait(driver, 5).until(EC.title_contains("Dashboard"))
        _try_click_reports_logs(driver)
    except Exception:
        pass
    wait_for_elfinder(driver, timeout=timeout)

# =========================
# ELFinder: list + download
# =========================
def open_folder_and_list_dom(driver, folder_label="allegati-log") -> tuple[Dict, List[Dict]]:
    """
    Click the folder in the left tree and scrape entries of the cwd via elFinder instance.
    Returns cwd, rows[{name,hash,href}].
    """
    result = driver.execute_async_script("""
      const label = (arguments[0]||'allegati-log').toLowerCase();
      const done  = arguments[arguments.length-1];
      (async()=>{
        try{
          const $=window.$||window.jQuery; const inst=$('.elfinder').elfinder('instance');
          if(!inst) return done({ok:false,error:'no instance'});
          // Find folder in navbar by visible text
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
          // Wait for cwd tiles
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
                  href: mk(f.hash)    // baseline; downloads use getfile later
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
    for r in rows: r["name"] = strip_zw(r["name"])
    return cwd, rows

def requests_session_from_driver(driver) -> requests.Session:
    """Copy Selenium cookies into a requests.Session so we can download files."""
    s = requests.Session()
    for c in driver.get_cookies():
        args = {k: c[k] for k in ("domain","path","secure","expires") if k in c and c[k] is not None}
        s.cookies.set(c["name"], c["value"], **args)
    return s

def elfinder_getfile_urls(driver, picked):
    """Ask elFinder for a real download URL for each hash via `getfile`."""
    result = driver.execute_async_script("""
      const rows = arguments[0]; const done = arguments[arguments.length-1];
      (async () => {
        try {
          const $ = window.$ || window.jQuery;
          const inst = $('.elfinder').elfinder('instance');
          if (!inst) return done({ok:false, error:'no elFinder instance'});
          const out = [];
          for (const r of rows) {
            try {
              const res = await inst.request({ data: { cmd:'getfile', target:r.hash, download:1 } });
              const f = (res && (res.files?.[0] || res)) || {};
              const url = f.url || res?.url || null;
              out.push({ name:r.name, hash:r.hash, url, fallbackHref:r.href });
            } catch (e) {
              out.push({ name:r.name, hash:r.hash, url:null, fallbackHref:r.href, err:String(e) });
            }
          }
          done({ok:true, files:out});
        } catch (e) { done({ok:false, error:String(e)}); }
      })();
    """, picked)
    if not result.get("ok"):
        raise RuntimeError("getfile failed: " + result.get("error","unknown"))
    return result["files"]

def robust_download_logs(driver, rows, out_dir: pathlib.Path) -> List[str]:
    """
    For each picked file:
      1) Try `getfile` URL (preferred).
      2) Fallback to connector `cmd=file&target=...&download=1`.
    Save as text in out_dir and return list of local paths.
    """
    sess = requests_session_from_driver(driver)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = elfinder_getfile_urls(driver, rows)
    connector = driver.execute_script("""
      const inst = (window.$||window.jQuery)('.elfinder').elfinder('instance');
      return inst ? (inst.options?.url || inst.opts?.url || null) : null;
    """)

    saved = []
    for f in files:
        name = f["name"]
        dest = out_dir / name
        text = None
        last_err = None

        # 1) signed/open URL first
        if f.get("url"):
            try:
                r = sess.get(f["url"], timeout=60)
                r.raise_for_status()
                text = r.text
            except Exception as e:
                last_err = f"GETFILE {type(e).__name__}: {e}"

        # 2) fallback to connector direct call
        if text is None and connector:
            try:
                sep = "&" if "?" in connector else "?"
                url = f"{connector}{sep}cmd=file&target={f['hash']}&download=1"
                r = sess.get(url, timeout=60)
                r.raise_for_status()
                text = r.text
            except Exception as e:
                last_err = f"CMD=file {type(e).__name__}: {e}"

        if text is None:
            print(f"[warn] Could not download {name}: {last_err or 'unknown error'}")
            continue

        dest.write_text(text, encoding="utf-8", errors="ignore")
        saved.append(str(dest))
    return saved

# =========================
# PARSING
# =========================
def parse_file(text: str) -> Tuple[int,int,int,dict]:
    # text already normalized
    dbg = {"aggiornare_lines": [], "modificati_lines": [], "cancellati_lines": []}

    def grab_all(patterns, take="sum_last"):
        """
        patterns: list of regex strings with one capturing group for the number (or X/Y).
        take:
          - "sum_last": sum all captured numbers (if X/Y, takes the last number Y)
          - "max": take max of captured numbers
        """
        vals, lines = [], []
        for p in patterns:
            for m in re.finditer(p, text, re.I|re.M|re.S):
                g = m.group(1)
                # accept either N or X/Y and take the last number for X/Y
                if "/" in g:
                    try:
                        n = int(re.split(r"\D+", g.strip())[-1])
                    except:
                        continue
                else:
                    mnum = re.search(r"\d+", g)
                    if not mnum: 
                        continue
                    n = int(mnum.group())
                vals.append(n); lines.append(m.group(0))
        if not vals: 
            return 0, []
        if take == "max": 
            return max(vals), lines
        return sum(vals), lines

    # 1) Prodotti da aggiornare su Google
    aggiornare, lines = grab_all([
        r'Prodotti\s+da\s+aggiornare\s+su\s+Google\s*[: ]\s*([\d\s/]+)'
    ])
    dbg["aggiornare_lines"] += lines
    if aggiornare == 0:
        x, lines = grab_all([r'Products\s+to\s+update\s+on\s+Google\s*[: ]\s*([\d\s/]+)'])
        aggiornare += x; dbg["aggiornare_lines"] += lines

    # 2) Preparazione JSON prodotti modificati ...
    modificati, lines = grab_all([
        r'Preparazione\s+JSON\s+prodotti\s+modificati\s+da\s+mandare\s+a\s+Google[^\n]*?([\d\s/]+)'
    ], take="max")
    dbg["modificati_lines"] += lines
    if modificati == 0:
        x, lines = grab_all([
            r'fine\s+prodotti\s+modificati\s+da\s+mandare\s+a\s+google\s+([\d\s/]+)',
            r'end\s+of\s+modified\s+products\s+to\s+send\s+to\s+google\s+([\d\s/]+)',
            r'Preparazione\s+JSON\s+prodotti\s+modificati\s+da\s+mandare\s+a\s+Google.*?(\d+)\s*(?:/|\b)'
        ], take="max")
        modificati += x; dbg["modificati_lines"] += lines

    # 3) Preparazione JSON prodotti cancellati ...
    cancellati, lines = grab_all([
        r'Preparazione\s+JSON\s+prodotti\s+cancellati\s+da\s+mandare\s+a\s+Google[^\n]*?([\d\s/]+)'
    ], take="max")
    dbg["cancellati_lines"] += lines
    if cancellati == 0:
        x, lines = grab_all([
            r'Prodotti\s+da\s+cancellare\s*[: ]\s*([\d\s/]+)',
            r'fine\s+prodotti\s+cancellati\s+da\s+mandare\s+a\s+google\s+([\d\s/]+)',
            r'end\s+of\s+deleted\s+products\s+to\s+send\s+to\s+google\s+([\d\s/]+)'
        ], take="max")
        cancellati += x; dbg["cancellati_lines"] += lines

    return aggiornare, modificati, cancellati, dbg

# =========================
# POST / SUMMARY
# =========================
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

# =========================
# MAIN
# =========================
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

        # go to logs page + ensure elFinder present
        goto_logs_page(driver, LOGS_URL, timeout=60)

        # list files in allegati-log
        cwd, rows = open_folder_and_list_dom(driver, folder_label="allegati-log")
        print("[logs] cwd:", cwd)
        print("[logs] sample names in folder (first 50):", [r["name"] for r in rows[:50]])

        prefix = target_date.strftime("%Y-%m-%d")
        picked = [r for r in rows if r["name"].endswith(".log") and prefix in r["name"]]
        print(f"[logs] Found {len(picked)} log file(s) for {target_date}: {[p['name'] for p in picked]}")

        if not picked:
            totals = {"date": str(target_date), "aggiornare": 0, "modificati": 0, "cancellati": 0, "files": []}
            if PREVIEW_ONLY: 
                write_summary(totals, [])
            else: 
                post_to_sheet(totals)
            return

        # --- download all logs locally ---
        out_dir = pathlib.Path("logs") / str(target_date)
        saved_paths = robust_download_logs(driver, picked, out_dir)
        print(f"[logs] downloaded {len(saved_paths)}/{len(picked)} files to {out_dir}")

        # parse local files
        per_file_rows=[]; A=M=C=0; names=[]
        for p in saved_paths:
            name = pathlib.Path(p).name
            txt = normalize_text(pathlib.Path(p).read_text(encoding="utf-8", errors="ignore"))
            a,m,c,dbg = parse_file(txt)
            A+=a; M+=m; C+=c
            per_file_rows.append({"file":name,"aggiornare":a,"modificati":m,"cancellati":c,"debug":dbg})
            names.append(name)

        totals = {"date": str(target_date), "aggiornare": A, "modificati": M, "cancellati": C, "files": names}
        if PREVIEW_ONLY:
            write_summary(totals, per_file_rows)
        else:
            print(f"[logs] TOTALS → aggiornare={A}, modificati={M}, cancellati={C}")
            post_to_sheet(totals)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
