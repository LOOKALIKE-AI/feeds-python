#!/usr/bin/env python3
# get_logs_day.py — fetch all .log files for a given day (or latest day) and upload to Drive

import gzip
import os, re, time
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import base64, json, requests

TZ = ZoneInfo("Europe/Rome")

# ----- env -----
def load_env(path=".env"):
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            if not ln.strip() or ln.lstrip().startswith("#") or "=" not in ln:
                continue
            k, v = ln.rstrip().split("=", 1)
            os.environ.setdefault(k, v)
load_env()

LOGIN_URL   = os.environ["PORTAL_LOGIN_URL"]
PORTAL_USER = os.environ["PORTAL_USER"]
PORTAL_PASS = os.environ["PORTAL_PASS"]
LOGS_URL    = os.environ["PORTAL_LOGS_URL"]
WEBAPP_URL  = os.environ["WEBAPP_URL"]
ELFINDER_LABEL = os.getenv("ELFINDER_LABEL", "allegati-log").strip().lower()
LOGS_DATE   = os.getenv("LOGS_DATE")  # YYYY-MM-DD or None
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "false").lower() in ("1","true","yes")
DRY_RUN = os.getenv("PREVIEW_ONLY", "false").lower() in ("1","true","yes")

# ----- driver -----
def driver():
    opts = Options()
    if not SHOW_BROWSER:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1400,900")
    return webdriver.Chrome(options=opts)

def origin(u:str)->str:
    p=urlparse(u); return f"{p.scheme}://{p.netloc}"

def switch_into_elfinder_iframe(drv) -> bool:
    for i, fr in enumerate(drv.find_elements(By.TAG_NAME, "iframe")):
        try:
            drv.switch_to.frame(fr)
            has_container = drv.execute_script("return !!document.querySelector('.elfinder, #elfinder');")
            if has_container:
                print(f"[logs] Switched into iframe #{i} (elFinder container found)")
                return True
        except Exception:
            pass
        drv.switch_to.default_content()
    return False

def wait_for_elfinder(drv, timeout=60):
    WebDriverWait(drv, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    deadline = time.time() + timeout

    def has_container():
        try:
            return drv.execute_script("return !!document.querySelector('.elfinder, #elfinder');")
        except Exception:
            return False

    if not has_container():
        while time.time() < deadline and not switch_into_elfinder_iframe(drv):
            time.sleep(0.3)
        if not has_container():
            raise RuntimeError("elFinder container not found")

    while time.time() < deadline:
        try:
            ok = drv.execute_script("var $=window.$||window.jQuery; try{ return !!($('.elfinder').elfinder('instance')); }catch(e){ return false; }")
            if ok: return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("elFinder instance not initialized in time")

# ----- JS helpers (same logic as patches) -----
def js_list_logs_for_cwd():
    return r"""
const done = arguments[arguments.length-1];
(async () => {
  try {
    const $ = window.$ || window.jQuery;
    const inst = $('.elfinder').elfinder('instance');
    if (!inst) return done({ok:false, error:'no instance'});

    const label = %s;
    const nav = document.querySelector('.elfinder-navbar') || document;
    const nodes = nav.querySelectorAll('a, .elfinder-navbar-dir, .elfinder-navbar-root');
    for (const n of nodes) {
      const t = (n.textContent||'').trim().toLowerCase();
      if (t === label || t.includes(label)) { (n.closest('a')||n).click(); break; }
    }

    const until = Date.now()+10000;
    while(Date.now()<until){
      const cwd = inst.cwd(); if (cwd && cwd.hash) break;
      await new Promise(r=>setTimeout(r,150));
    }
    const cwd = inst.cwd(); if (!cwd || !cwd.hash) return done({ok:false, error:'no cwd'});

    await new Promise((resolve)=>{ inst.request({cmd:'open', target: cwd.hash, reload:true}).done(()=>resolve()).fail(()=>resolve()); });

    const files = Object.values(inst.files ? inst.files() : {})
      .filter(f => f && f.phash===cwd.hash && f.mime!=='directory' && /\.log$/i.test(String(f.name||'')));

    const entries = files.map(f => {
      const m = String(f.name||'').match(/^(\d{4}-\d{2}-\d{2})/);
      return { name: f.name, hash: f.hash, ts: f.ts||0, dateFromName: m? m[1]: null };
    });

    const dayOf = (e)=> e.dateFromName || (e.ts? new Date(e.ts*1000).toISOString().slice(0,10): null);
    const days = entries.map(dayOf).filter(Boolean).sort();
    const latestDay = days.length ? days[days.length-1] : null;

    done({ ok:true, latestDay, entries });
  } catch(e) { done({ ok:false, error:String(e) }); }
})();
""" % json.dumps(ELFINDER_LABEL)

def js_fetch_one_by_hash():
    return r"""
const done = arguments[arguments.length-1];
(async (hash) => {
  try {
    const $ = window.$ || window.jQuery;
    const inst = $('.elfinder').elfinder('instance');
    if (!inst) return done({ok:false, error:'no instance'});

    const base = new URL(inst.options?.url || inst.opts?.url, location.href).href;
    const file = inst.file(hash);
    if (!file) return done({ok:false, error:'hash not found'});

    // read or connector
    let txt=null, used=null, readErr='';
    // read
    try{
      const r = await fetch(base + (base.includes('?')?'&':'?') + 'cmd=read&target=' + encodeURIComponent(hash),
                            {credentials:'same-origin', headers:{'X-Requested-With':'XMLHttpRequest'}});
      const body = await r.text();
      let j=null; try{ j=JSON.parse(body) }catch(e){}
      if (j && (j.content||j.raw||j.data)) { txt=String(j.content||j.raw||j.data); used='read'; }
      else if (j && j.error) { readErr = Array.isArray(j.error)? j.error.join(','): String(j.error); }
    }catch(e){}
    if(!txt){
      const cd = (inst.options&&inst.options.customData) || (inst.opts&&inst.opts.customData)
              || (window.elFinderConfig && window.elFinderConfig.defaultOpts && window.elFinderConfig.defaultOpts.customData) || {};
      if(!cd.path||!cd.url) return done({ok:false, error:'missing customData.path/url' + (readErr? ' (read: '+readErr+')':'')});
      const q = new URLSearchParams();
      q.set('cmd','file'); q.set('target',hash); q.set('download','1'); q.set('_t', Date.now().toString());
      q.set('path', String(cd.path));
      q.set('url', new URL(String(cd.url), location.href).href);
      q.set('onetimeUrl', String(cd.onetimeUrl !== undefined ? cd.onetimeUrl : true));
      q.set('disabled', Array.isArray(cd.disabled)? cd.disabled.join(',') : (cd.disabled || 'netmount,mkfile'));
      q.set('tmbSize', String(cd.tmbSize || 315));
      const cpath = location.pathname.replace(/[^/]+$/, ''); q.set('cpath', cpath);
      const rf = await fetch(base + (base.includes('?')?'&':'?') + q.toString(),
                             {credentials:'same-origin', redirect:'follow', headers:{'X-Requested-With':'XMLHttpRequest'}});
      if(rf.status>=400) return done({ok:false, error:'download failed'+(readErr? ' (read: '+readErr+')':''), status: rf.status});
      txt = await rf.text(); used='file';
    }
    txt = txt.replace(/[\u200B\u200C\u200D\u2060\uFEFF]/g, '');
    return done({ok:true, name:String(file.name||'log.txt'), text:txt, used});
  } catch(e){ done({ok:false, error:String(e)}); }
})(arguments[0]);
"""

# ----- upload -----
def upload_log_to_drive(filename: str, content_text: str, day: str | None):
    import base64, requests
    gz_name = filename if filename.endswith(".gz") else (filename + ".gz")
    gz_bytes = gzip.compress(content_text.encode("utf-8"))
    b64 = base64.b64encode(gz_bytes).decode("ascii")
    payload = {
        "uploadLog": {
            "filename": gz_name,
            "contentBase64": b64,
            "mimeType": "application/gzip",
            "folderName": "LogsArchive",
            "useDateSubfolder": True,
            "date": day,
            "overwrite": "delete",
        }
    }
    if DRY_RUN:
        print(f"[dry-run] would upload {gz_name} → {day or '(today)'} (gzipped)")
        return {"ok": True, "dryRun": True}
    r = requests.post(WEBAPP_URL, json=payload, timeout=120)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "status": r.status_code, "text": r.text}

# ----- main -----
def main():
    drv = driver()
    try:
        # login
        drv.get(LOGIN_URL)
        WebDriverWait(drv, 20).until(EC.presence_of_element_located((By.NAME, "data[username]")))
        drv.find_element(By.NAME, "data[username]").send_keys(PORTAL_USER)
        drv.find_element(By.NAME, "data[password]").send_keys(PORTAL_PASS)
        drv.find_element(By.ID, "login-submit").click()
        WebDriverWait(drv, 20).until(EC.url_contains("gestionale"))

        # goto elFinder logs
        href = LOGS_URL if LOGS_URL.startswith("http") else urljoin(origin(LOGIN_URL), "/gestionale/elfinder/?log")
        drv.get(href)
        try:
            WebDriverWait(drv, 5).until(EC.title_contains("Dashboard"))
            links = drv.find_elements(By.CSS_SELECTOR, "a[href*='elfinder/?log']")
            if links: drv.execute_script("arguments[0].click()", links[0])
        except Exception:
            pass
        wait_for_elfinder(drv)

        # list
        res = drv.execute_async_script(js_list_logs_for_cwd())
        if not res.get("ok"): raise RuntimeError(res)
        latest_day = res["latestDay"]
        entries = res["entries"]

        day = LOGS_DATE or latest_day
        if not day:
            raise SystemExit("No logs found in the folder.")

        # pick all files for that day (prefer name prefix, else ts)
        def day_of(e):
            if e.get("dateFromName"): return e["dateFromName"]
            ts = e.get("ts") or 0
            if not ts: return None
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ).date().isoformat()

        targets = [e for e in entries if day_of(e) == day]
        targets.sort(key=lambda e: e["name"])

        print(f"[info] Day={day} files={len(targets)}")
        for i, e in enumerate(targets, 1):
            name_hash = e["hash"]
            d = drv.execute_async_script(js_fetch_one_by_hash(), name_hash)
            if not d.get("ok"):
                print(f"[warn] fetch failed for {e['name']}: {d}")
                continue
            fname = re.sub(r'[\\/:*?"<>|]+', '_', d["name"])
            resp = upload_log_to_drive(fname, d["text"], day)
            print(f"[{i}/{len(targets)}] uploaded {fname} → {resp}")
    finally:
        drv.quit()

if __name__ == "__main__":
    main()
