import os, re, time, sys, json
from datetime import datetime
from urllib.parse import urljoin, urlparse
import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

ELFINDER_URL = os.environ.get("ELFINDER_URL", "https://ws.lookalike.shop/gestionale/elfinder/?log")
TARGET_DATE = os.environ.get("LOGS_DATE", datetime.now().strftime("%Y-%m-%d"))
SHOW_BROWSER = os.environ.get("SHOW_BROWSER", "false").lower() == "true"

# ---------- parsing ----------
LINE_PATTERNS = [
    ("aggiornare", r"Prodotti\s+da\s+aggiornare\s+su\s+Google\s*:\s*([\d\.,]+)"),
    ("modificati", r"Preparazione\s+JSON\s+prodotti\s+modificati\s+da\s+mandare\s+a\s+Google\s*:\s*([\d\.,]+)"),
    ("cancellati", r"Preparazione\s+JSON\s+prodotti\s+cancellati\s+da\s+mandare\s+a\s+Google\s*:\s*([\d\.,]+)"),
]
def to_int(s):
    if s is None: return 0
    return int(re.sub(r"[^\d]", "", s)) if re.search(r"\d", s or "") else 0

def parse_three_numbers(text):
    results = {}
    for key, pat in LINE_PATTERNS:
        m = re.search(pat, text, flags=re.I)
        results[key] = to_int(m.group(1) if m else None)
    return results

# ---------- selenium helpers ----------
def make_driver(headless=not SHOW_BROWSER):
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1050")
    return webdriver.Chrome(options=opts)

def wait_for_elfinder_ready(driver, timeout=60):
    driver.get(ELFINDER_URL)
    # elFinder creates a global instance on #elfinder
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script(
            "var n = document.querySelector('#elfinder');"
            "if(!n) return false;"
            "try{var fm = $('#elfinder').elfinder('instance'); return !!(fm && fm.cwd());}catch(e){return false;}"
        ) is True
    )

def list_files_from_instance(driver):
    script = """
        var fm = $('#elfinder').elfinder('instance');
        var list = [];
        var files = fm ? fm.files() : {};
        for (var k in files){
            var f = files[k];
            if (f && f.name && f.mime && f.hash) {
                list.push({name:f.name, mime:f.mime, hash:f.hash});
            }
        }
        return list;
    """
    return driver.execute_script(script)

def find_one_log_for_date(file_list, ymd):
    # exact names are like: 2025-08-29_importDaemon_feed_156.log
    candidates = [f for f in file_list if f["name"].startswith(ymd+"_") and f["name"].endswith(".log")]
    # deterministically pick first by name
    return sorted(candidates, key=lambda x: x["name"])[0] if candidates else None

def get_connector_absolute(driver):
    # elFinder option 'url' gives the connector path (may be relative)
    rel = driver.execute_script("var fm=$('#elfinder').elfinder('instance'); return fm && fm.options ? fm.options.url : null;")
    if not rel: return None
    return urljoin(driver.current_url, rel)

def transfer_cookies_to_requests(driver, session):
    for c in driver.get_cookies():
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

# ---------- HTTP fetch via connector with robust fallbacks ----------
def try_fetch_via_connector(sess, connector_abs, target_hash, referer, ua):
    # headers to mimic in-page XHR
    base_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Referer": referer,
        "User-Agent": ua,
        "Origin": f"{urlparse(referer).scheme}://{urlparse(referer).netloc}",
        "Cache-Control": "no-cache",
    }
    params = {"cmd":"file", "target":target_hash}  # let server decide download vs inline
    tried = []

    def attempt(url):
        # 1) GET
        r = sess.get(url, params=params, headers=base_headers, allow_redirects=True, timeout=40)
        tried.append((url, "GET", r.status_code, len(r.content)))
        if r.ok and r.content:
            return r
        # 2) POST
        r = sess.post(url, data=params, headers=base_headers, allow_redirects=True, timeout=40)
        tried.append((url, "POST", r.status_code, len(r.content)))
        return r if r.ok and r.content else None

    # try the declared connector first
    r = attempt(connector_abs)
    if r: 
        return r.text, tried

    # try a few common alternatives if the declared one 404s
    candidates = [
        "php/connector.php",
        "php/connector.minimal.php",
        "php/connector.main.php",
        "../elfinder/php/connector.php",
        "../elfinder/php/connector.minimal.php",
        "../elfinder/php/connector.main.php",
    ]
    for rel in candidates:
        url = urljoin(referer, rel)
        r = attempt(url)
        if r:
            return r.text, tried

    return None, tried

# ---------- UI quicklook fallback (no direct HTTP) ----------
def quicklook_read_text(driver, filename):
    # Double-click the tile whose filename title equals the exact name
    # Works both in list and icons view because we target the inner filename span's title attribute.
    tile = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.XPATH,
            f"//div[contains(@class,'elfinder-cwd-file')][.//span[contains(@class,'elfinder-cwd-filename')]/span[@title={json.dumps(filename)}]]"
        ))
    )
    ActionChains(driver).move_to_element(tile).double_click(tile).perform()

    # Quicklook overlay should appear
    # For text files, elFinder renders a <div class='elfinder-quicklook-preview-text'> or wraps in <pre>
    WebDriverWait(driver, 30).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, ".elfinder-quicklook"))
    )

    # Wait a bit for content fetch
    time.sleep(1.5)

    # Try dedicated text preview container first
    js = """
      var root = document.querySelector('.elfinder-quicklook');
      if (!root || getComputedStyle(root).display === 'none') return null;
      var pre = root.querySelector('.elfinder-quicklook-preview-text, pre, .elfinder-quicklook-preview'); 
      return pre ? pre.innerText : root.innerText;
    """
    txt = driver.execute_script(js)
    return txt or ""

# ---------- main ----------
def main():
    print(f"[onefile v3] Target date: {TARGET_DATE}")
    driver = make_driver()
    try:
        wait_for_elfinder_ready(driver)
        print("[onefile v3] elFinder ready")

        files = list_files_from_instance(driver)
        if not files:
            raise RuntimeError("Could not enumerate files from elFinder instance (files() empty).")

        picked = find_one_log_for_date(files, TARGET_DATE)
        if not picked:
            raise SystemExit(f"No .log found for {TARGET_DATE}")

        print(f"[onefile v3] Picking: {picked['name']} (hash={picked['hash']})")

        connector_abs = get_connector_absolute(driver)
        if connector_abs:
            print(f"[onefile v3] Connector reported by elFinder: {connector_abs}")
        else:
            print("[onefile v3] Could not read connector from elFinder; will jump to Quicklook fallback.")

        session = requests.Session()
        transfer_cookies_to_requests(driver, session)
        ua = driver.execute_script("return navigator.userAgent") or "Mozilla/5.0"

        raw_text = None
        tries = []
        if connector_abs:
            raw_text, tries = try_fetch_via_connector(
                session, connector_abs, picked["hash"], driver.current_url, ua
            )

        if not raw_text:
            print("[onefile v3] Direct connector fetch failed; trying Quicklook UI fallback…")
            raw_text = quicklook_read_text(driver, picked["name"])

        if not raw_text or not raw_text.strip():
            print("[onefile v3] All strategies failed.")
            if tries:
                print("Tried:")
                for url, m, code, ln in tries:
                    print(f"  {m} {url} -> {code}, len={ln}")
            raise SystemExit(2)

        # Parse the three numbers
        got = parse_three_numbers(raw_text)
        print("\nParsed counters:")
        print(f"  Prodotti da aggiornare su Google: {got['aggiornare']}")
        print(f"  Preparazione JSON prodotti modificati da mandare a Google: {got['modificati']}")
        print(f"  Preparazione JSON prodotti cancellati da mandare a Google: {got['cancellati']}")

    finally:
        if not SHOW_BROWSER:
            try: driver.quit()
            except: pass

if __name__ == "__main__":
    main()
