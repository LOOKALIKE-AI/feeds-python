# export_logs_daily.py
import os, re, time, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Final, Tuple, List
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from env_utils import load_env, require_env, get_bool
load_env(".env")

# --- env (typed) ---
PORTAL_LOGIN: Final[str] = require_env("PORTAL_LOGIN_URL")
PORTAL_USER:  Final[str] = require_env("PORTAL_USER")
PORTAL_PASS:  Final[str] = require_env("PORTAL_PASS")
LOGS_URL:     Final[str] = require_env("PORTAL_LOGS_URL")
WEBAPP:       Final[str] = os.getenv("WEBAPP_URL", "")  # required only when not previewing
PREVIEW_ONLY: Final[bool] = get_bool("PREVIEW_ONLY", False)

# --- date selection ---
tz = ZoneInfo("Europe/Rome")
now_local = datetime.now(tz)
target_date = (now_local - timedelta(days=1)).date()
logs_date_env = os.getenv("LOGS_DATE", "").strip()
if logs_date_env:
    try:
        target_date = datetime.strptime(logs_date_env, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"LOGS_DATE must be YYYY-MM-DD, got '{logs_date_env}': {e}")

def get_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    return webdriver.Chrome(options=opts)

# ---------- parsing ----------
def _sum_ints(pattern: str, text: str, flags=re.I) -> int:
    return sum(int(m) for m in re.findall(pattern, text, flags))

def parse_file(text: str) -> Tuple[int,int,int,dict]:
    """
    Returns (aggiornare, modificati, cancellati, debug) for ONE log file.
    debug has matched lines to help you see what was captured.
    """
    dbg = {"aggiornare_lines": [], "modificati_lines": [], "cancellati_lines": []}

    # 1) "Prodotti da aggiornare su Google"
    aggiornare = 0
    for m in re.finditer(r'(Prodotti da aggiornare su Google)[:\s]*([0-9]+)', text, flags=re.I):
        aggiornare += int(m.group(2))
        dbg["aggiornare_lines"].append(m.group(0))

    # 2) "Preparazione JSON prodotti modificati da mandare a Google"
    # Try multiple nearby patterns for robustness
    modificati = 0
    # a) explicit count on same/next lines
    for m in re.finditer(
        r'(Preparazione JSON prodotti modificati da mandare a Google.*?)(?:\n|.){0,180}?'
        r'(?:Prodotti da modificare|Totale|Count|avanzamento|fine.*?da mandare a google)\s*[: ]\s*(\d+)(?:/\d+)?',
        text, flags=re.I):
        modificati += int(m.group(2))
        dbg["modificati_lines"].append(m.group(0))
    # b) fallback to "fine ... 0/N" and take N (right side)
    for m in re.finditer(r'fine prodotti modificati da mandare a google\s+\d+\/(\d+)', text, flags=re.I):
        modificati += int(m.group(1))
        dbg["modificati_lines"].append(m.group(0))

    # 3) "Preparazione JSON prodotti cancellati da mandare a Google"
    cancellati = 0
    # a) explicit count near the header
    for m in re.finditer(
        r'(Preparazione JSON prodotti cancellati da mandare a Google.*?)(?:\n|.){0,180}?'
        r'(?:Prodotti da cancellare|Totale|Count|avanzamento|fine.*?da mandare a google)\s*[: ]\s*(\d+)(?:/\d+)?',
        text, flags=re.I):
        cancellati += int(m.group(2))
        dbg["cancellati_lines"].append(m.group(0))
    # b) generic fallbacks often seen in your logs: "Prodotti da cancellare: X" or "avanzamento X/X"
    for m in re.finditer(r'Prodotti da cancellare\s*[: ]\s*(\d+)', text, flags=re.I):
        cancellati += int(m.group(1)); dbg["cancellati_lines"].append(m.group(0))
    for m in re.finditer(r'avanzamento\s+(\d+)\/\1', text, flags=re.I):
        cancellati += int(m.group(1)); dbg["cancellati_lines"].append(m.group(0))

    return aggiornare, modificati, cancellati, dbg

# ---------- main ----------
def main():
    if not PREVIEW_ONLY and not WEBAPP:
        raise EnvironmentError("WEBAPP_URL is required when PREVIEW_ONLY is false.")

    prefix = target_date.strftime("%Y-%m-%d")
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

        # open logs (elFinder)
        driver.get(LOGS_URL)
        goto_logs_page(driver, LOGS_URL, timeout=60)

        # connector + list files
        files = driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const $ = (window.$ || window.jQuery);
                const inst = $('.elfinder').elfinder('instance');
                const url  = inst.options?.url || inst.opts?.url;
                const cwd  = inst.cwd().hash;
                const body = new URLSearchParams({cmd:'open', target: cwd});
                const r = await fetch(url, {
                  method: 'POST',
                  credentials: 'same-origin',
                  headers: {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','X-Requested-With':'XMLHttpRequest'},
                  body
                });
                const data = await r.json();
                done({ok:true, url, cwd, files: (data.files || [])});
              } catch (e) { done({ok:false, error:String(e)}); }
            })();
        """)
        if not files.get("ok"):
            raise RuntimeError(f"elFinder open failed: {files.get('error')}")

        all_files = files["files"]
        sample_names = [f.get("name","") for f in all_files][:50]
        print("[logs] sample names in folder (first 50):", sample_names)
        logs_today = [
            f for f in files["files"]
            if f.get("name", "").endswith(".log") and f["name"].startswith(prefix)
        ]
        names = [f["name"] for f in logs_today]
        print(f"[logs] Found {len(names)} log file(s) for {target_date}: {names}")

        if not logs_today:
            msg = f"No .log files found for date {target_date}. Will {'skip posting' if PREVIEW_ONLY else 'post zeros'}."
            print("[logs]", msg)
            totals = {"date": str(target_date), "aggiornare": 0, "modificati": 0, "cancellati": 0, "files": []}
            if PREVIEW_ONLY:
                write_summary(totals, per_file=[])
                return
            else:
                post_to_sheet(totals); return

        # fetch contents
        texts = driver.execute_async_script("""
            const hashes = arguments[0];
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const $ = (window.$ || window.jQuery);
                const inst = $('.elfinder').elfinder('instance');
                const url  = inst.options?.url || inst.opts?.url;
                const out = [];
                for (const h of hashes) {
                  const body = new URLSearchParams({cmd:'file', target: h});
                  const r = await fetch(url, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','X-Requested-With':'XMLHttpRequest'},
                    body
                  });
                  out.push(await r.text());
                }
                done({ok:true, texts: out});
              } catch (e) { done({ok:false, error:String(e)}); }
            })();
        """, [f["hash"] for f in logs_today])
        if not texts.get("ok"):
            raise RuntimeError(f"elFinder file fetch failed: {texts.get('error')}")

        # parse each file
        per_file_rows = []
        total_a = total_m = total_c = 0
        for name, txt in zip(names, texts["texts"]):
            a, m, c, dbg = parse_file(txt)
            total_a += a; total_m += m; total_c += c
            per_file_rows.append({
                "file": name, "aggiornare": a, "modificati": m, "cancellati": c, "debug": dbg
            })

        totals = {"date": str(target_date), "aggiornare": total_a, "modificati": total_m, "cancellati": total_c, "files": names}

        # preview or post
        if PREVIEW_ONLY:
            write_summary(totals, per_file_rows)
        else:
            print(f"[logs] TOTALS → aggiornare={total_a}, modificati={total_m}, cancellati={total_c}")
            post_to_sheet(totals)

    finally:
        driver.quit()


def goto_logs_page(driver, logs_url: str, timeout: int = 60):
    # Try direct URL first
    driver.get(logs_url)
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    # If elFinder already present, we’re done
    if _find_elfinder_in_current_context(driver):
        wait_for_elfinder(driver, timeout=timeout)
        return

    # If we’re on the dashboard (or still no elFinder), click through the menu.
    on_dashboard = ("Dashboard" in (driver.title or "")) or ("/gestionale/" in (driver.current_url or ""))
    if on_dashboard or not _find_elfinder_in_current_context(driver):
        # 1) Expand the hamburger so #navbarSupportedContent gets .show
        try:
            toggler = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button.navbar-toggler"))
            )
            driver.execute_script("arguments[0].click();", toggler)
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#navbarSupportedContent.show"))
            )
        except Exception:
            # If it's already expanded (desktop width), ignore.
            pass

        # 2) Open Reports dropdown
        reports_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "li.menu_item_41 > a.nav-link.dropdown-toggle")  # "Reports"
            )
        )
        driver.execute_script("arguments[0].click();", reports_btn)

        # 3) Click "Log Files"
        logs_link = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "li.menu_item_41 .dropdown-menu a[href*='elfinder/?log']")
            )
        )
        driver.execute_script("arguments[0].click();", logs_link)

    # 4) Now wait until elFinder is actually ready (handles iframe too)
    wait_for_elfinder(driver, timeout=timeout)

    
def post_to_sheet(payload: dict):
    import requests
    r = requests.post(WEBAPP, json={"logDaily": payload}, timeout=60)
    print("[logs] Posted daily log metrics:", r.text)

def write_summary(totals: dict, per_file: List[dict]):
    # Console table
    print("\nPer-file parse:")
    if per_file:
        for row in per_file:
            print(f"  {row['file']}: aggiornare={row['aggiornare']}  modificati={row['modificati']}  cancellati={row['cancellati']}")
            # show first matched lines (if any)
            for k in ("aggiornare_lines","modificati_lines","cancellati_lines"):
                if row["debug"].get(k):
                    print(f"    {k}:")
                    for ln in row["debug"][k][:3]:
                        print("      •", ln[:180])
    else:
        print("  <no files>")

    print(f"\nTOTAL {totals['date']}: aggiornare={totals['aggiornare']}, modificati={totals['modificati']}, cancellati={totals['cancellati']}\n")

    # GitHub Actions job summary (nice Markdown block)
    gh_sum = os.getenv("GITHUB_STEP_SUMMARY")
    if gh_sum:
        with open(gh_sum, "a", encoding="utf-8") as f:
            f.write(f"### Daily logs summary for {totals['date']}\n\n")
            if per_file:
                f.write("| File | Aggiornare | Modificati | Cancellati |\n|---|---:|---:|---:|\n")
                for row in per_file:
                    f.write(f"| {row['file']} | {row['aggiornare']} | {row['modificati']} | {row['cancellati']} |\n")
            else:
                f.write("_No files found_\n")
            f.write(f"\n**TOTALS:** {totals['aggiornare']} / {totals['modificati']} / {totals['cancellati']}\n")
def dump_dom(driver):
    try:
        with open("elfinder_debug.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        driver.save_screenshot("elfinder_debug.png")
        print("[debug] Wrote elfinder_debug.html and elfinder_debug.png")
    except Exception as e:
        print("[debug] Failed to write debug artifacts:", e)
    print("[debug] URL:", driver.current_url)
    try:
        print("[debug] Title:", driver.title)
    except Exception:
        pass

def _find_elfinder_in_current_context(driver) -> bool:
    # Check for the container
    has_container = bool(driver.find_elements(By.CSS_SELECTOR, "div.elfinder, #elfinder"))
    if not has_container:
        return False
    # Check for the JS instance
    return bool(driver.execute_script("""
        var $ = window.$ || window.jQuery;
        if (!$) return false;
        var el = $('.elfinder'); if (!el.length) el = $('#elfinder');
        if (!el.length) return false;
        try {
          var inst = el.elfinder('instance');
          return !!(inst && inst.cwd());
        } catch (e) { return false; }
    """))

def _switch_into_elfinder_iframe(driver) -> bool:
    # Look for an iframe that contains elFinder and switch into it
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for i, frame in enumerate(iframes):
        try:
            driver.switch_to.frame(frame)
            if _find_elfinder_in_current_context(driver):
                print(f"[logs] Switched into iframe #{i} for elFinder.")
                return True
            # Not here; pop back out and try next
            driver.switch_to.default_content()
        except Exception:
            driver.switch_to.default_content()
    return False

def wait_for_elfinder(driver, timeout=45):
    # Wait for HTML to be ready
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    # If redirected to login, bail early with a clear message
    if ("login" in driver.current_url.lower()) or driver.find_elements(By.NAME, "data[username]"):
        dump_dom(driver)
        raise RuntimeError("Redirected to login while opening logs page. Session not kept or credentials/URL issue.")

    # Try to find elFinder in the main document
    if _find_elfinder_in_current_context(driver):
        return

    # If not found, try iframes
    if _switch_into_elfinder_iframe(driver):
        return

    # Poll a bit (some installs initialize slowly)
    end = time.time() + timeout
    while time.time() < end:
        if _find_elfinder_in_current_context(driver):
            return
        if _switch_into_elfinder_iframe(driver):
            return
        time.sleep(0.5)

    dump_dom(driver)
    raise TimeoutException("elFinder not detected (no container/instance). See debug artifacts for details.")
if __name__ == "__main__":
    main()