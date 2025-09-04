# -*- coding: utf-8 -*-
"""
Summarize per-partner daily log counters from Drive logs.
- Lists logs for a given date via Apps Script (listLogs)
- Fetches files in batches (getLogsBatch)
- Parses the 3 counters with your existing regexes
- Joins with LogIDs (fetched via new getLogIDs branch)
- Upserts results into DailyPartnerLogs via new partnerDailyLogs branch

ENV:
  WEBAPP_URL=...            (Apps Script web app URL)
  LOGS_DATE=YYYY-MM-DD      (optional; defaults to today Europe/Rome)
  LOGS_FOLDER=LogsArchive   (optional)
  TZ=Europe/Rome            (optional; default Europe/Rome)

CLI:
  python summarize_log_counts_by_partner.py --date 2025-09-03 --clear-first
"""
import os
import re, io, json, gzip, base64, argparse, datetime as dt
from typing import Dict, List, Tuple
import requests

# ---- env / args ----
from typing import Final


def load_env_here(filename: str = ".env") -> None:
    here = os.path.dirname(__file__)
    path = os.path.join(here, filename)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")   # tolerate quoted values
            if k and (k not in os.environ):
                os.environ[k] = v

load_env_here()

def _env(key: str, default: str = "") -> str:
    """Return environment variable as a plain string (never None)."""
    v = os.getenv(key, default)
    if v is None:
        return default
    return str(v)
 

WEBAPP_URL   = _env("WEBAPP_URL", "").strip()
LOGS_FOLDER  = _env("LOGS_FOLDER", "LogsArchive").strip()
TZ_NAME      = _env("TZ", "Europe/Rome").strip()
# NEW: writer app URL + optional root folder name + chunk size
LOGS_WRITER_URL: Final[str] = _env("LOGS_WRITER_URL").strip()
LOGS_SHEETS_ROOT: Final[str] = _env("LOGS_SHEETS_ROOT", "Logs-Sheets").strip()
UPSERT_CHUNK = int(_env("UPSERT_CHUNK", "80"))
CLEAR_FIRST=1

RX_ERRI   = r"prodotti\s+in\s+errore\s+google\s*:\s*([\d\.,]+)"
RX_ADD    = r"prodotti\s+da\s+aggiungere\s*:\s*([\d\.,]+)"
RX_UPDATE = r"prodotti\s+da\s+aggiornare\s+su\s+google\s*:\s*([\d\.,]+)"

MAX_PER_CALL = 30  # matches your Apps Script getLogsBatch cap

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (defaults to LOGS_DATE env or today in Europe/Rome)")
    ap.add_argument("--clear-first", action="store_true", help="Clear all rows for the date before upserting")
    return ap.parse_args()
if not LOGS_WRITER_URL:
    raise SystemExit("Missing LOGS_WRITER_URL env (new writer web app URL)")

# ---- time helpers ----
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def today_in_tz(tzname: str) -> dt.date:
    if ZoneInfo:
        now = dt.datetime.now(ZoneInfo(tzname))
    else:
        now = dt.datetime.utcnow()
    return now.date()
def yesterday_in_tz(tzname: str) -> dt.date:
    if ZoneInfo:
        now = dt.datetime.now(ZoneInfo(tzname))
    else:
        now = dt.datetime.utcnow()
    return (now - dt.timedelta(days=1)).date()

# ---- utils ----
def log(*a): print("[by-partner]", *a, flush=True)

def post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": f"Non-JSON response: {r.status_code}", "text": r.text[:500]}

def parse_int(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s or "") or "0")

def sum_matches(text: str, rx: str) -> int:
    total = 0
    for m in re.finditer(rx, text, flags=re.I):
        total += parse_int(m.group(1))
    return total

def decode_log_content(entry: dict) -> str:
    """
    entry: object from getLogsBatch.files[]
      { ok, name, contentBase64, mimeType, ... }
    """
    if not entry.get("ok"):
        return ""
    b64 = entry.get("contentBase64") or ""
    if not b64:
        return ""
    raw = base64.b64decode(b64)
    # Detect gzip via filename or magic header
    name = str(entry.get("name", ""))
    is_gz = name.endswith(".gz") or raw[:2] == b"\x1f\x8b"
    if is_gz:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                data = gz.read()
            return data.decode("utf-8", errors="replace")
        except Exception:
            # fallback: try raw decode
            return raw.decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")

def file_feed_id(filename: str) -> int | None:
    # e.g., 2025-09-01_importDaemon_feed_442.log or .log.gz
    m = re.search(r"feed[_-](\d+)\.log(?:\.gz)?$", filename)
    return int(m.group(1)) if m else None

# ---- main flow ----
def main():
    args = parse_args()
    if not WEBAPP_URL:
        raise SystemExit("Missing WEBAPP_URL env")

    # allow overrides from CLI or env; otherwise default to yesterday in TZ
    target_date = args.date or _env("LOGS_DATE")
    if not target_date:
        target_date = yesterday_in_tz(TZ_NAME).isoformat()

    # allow CLEAR_FIRST via env when CLI flag not provided
    clear_first_env = _env("CLEAR_FIRST").strip().lower() in ("1", "true", "yes", "y")
    args.clear_first = bool(args.clear_first or clear_first_env)

    log(f"Date: {target_date}  folder: {LOGS_FOLDER}")

    # 1) List logs for date
    res = post_json(WEBAPP_URL, {"listLogs": {"folderName": LOGS_FOLDER, "date": target_date}})
    if not res.get("ok"):
        raise SystemExit(f"listLogs failed: {res}")
    files = res.get("files", [])
    log(f"Found {len(files)} files for {target_date}")

    # 2) Pick newest by exact filename (your Apps Script listLogs already sorts by name)
    wanted_names: List[str] = []
    newest_by_name: Dict[str, dict] = {}
    for f in files:
        nm = str(f.get("name", ""))
        if not re.search(r"\.(log|log\.gz)$", nm, flags=re.I):
            continue
        prev = newest_by_name.get(nm)
        if not prev or f.get("lastUpdated", 0) > prev.get("lastUpdated", 0):
            newest_by_name[nm] = f
    wanted_names = list(newest_by_name.keys())
    if not wanted_names:
        log("No logs to fetch. Exiting.")
        return

    # 3) Fetch in batches
    results: Dict[int, Dict[str, int]] = {}  # feedId -> counters
    for i in range(0, len(wanted_names), MAX_PER_CALL):
        chunk = wanted_names[i:i+MAX_PER_CALL]
        r2 = post_json(WEBAPP_URL, {"getLogsBatch": {"folderName": LOGS_FOLDER, "date": target_date, "filenames": chunk}})
        if not r2.get("ok"):
            log("getLogsBatch failed on chunk:", r2)
            continue
        for entry in r2.get("files", []):
            if not entry.get("ok"):
                continue
            nm = entry.get("name", "")
            fid = file_feed_id(nm)
            if fid is None:
                continue
            text = decode_log_content(entry)
            if not text:
                continue
            errore     = sum_matches(text, RX_ERRI)
            aggiungere = sum_matches(text, RX_ADD)
            aggiornare = sum_matches(text, RX_UPDATE)
            results[fid] = {"errore": errore, "aggiungere": aggiungere, "aggiornare": aggiornare}

    log(f"Parsed {len(results)} feed IDs")

    if not results:
        log("No counters found; nothing to upsert.")
        return

    # 4) Fetch LogIDs mapping (onlyActive to reduce noise)
    rmap = post_json(WEBAPP_URL, {"getLogIDs": {"sheetName": "LogIDs", "onlyActive": True}})
    if not rmap.get("ok"):
        log("getLogIDs failed:", rmap)
        # proceed with unknown partner names
        mapping = {}
    else:
        # expected: { ok:true, rows:[{feedId:459, partner:"24Bottles", code:"1507", active:true}, ...] }
        mapping = { int(r.get("feedId")): r for r in rmap.get("rows", []) if "feedId" in r }

    # 5) Build rows for upsert
    # Note: we only upsert feed IDs we parsed for this date (i.e., files present)
    when_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for fid, c in results.items():
        meta = mapping.get(fid, {})
        rows.append({
            "date": target_date,
            "feedId": fid,
            "partner": meta.get("partner") or f"Feed {fid}",
            "code": meta.get("code") or "",
            "errore": c["errore"],
            "aggiungere": c["aggiungere"],
            "aggiornare": c["aggiornare"],
            "updatedAt": when_iso
        })

    log(f"Collected {len(rows)} rows for {target_date}")

    if not LOGS_WRITER_URL:
        raise SystemExit("Missing LOGS_WRITER_URL env")

    first = True
    total_written = 0
    for start in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[start:start+UPSERT_CHUNK]
        log(f"Writing chunk {start}-{start+len(chunk)-1} (size {len(chunk)}) into monthly sheet / {target_date} tab via writer...")

        payload = {
            "writeDailyPartnerLogs": {
                "date": target_date,
                "rows": chunk,
                "clearFirst": bool(args.clear_first and first),  # clear only on first chunk
                "rootFolderName": LOGS_SHEETS_ROOT
            }
        }
        rsp = post_json(LOGS_WRITER_URL, payload, timeout=300)

        if not rsp.get("ok"):
            log("Writer error:", rsp)
            continue

        if start == 0:
            log(f"Writer target: {rsp.get('spreadsheetUrl','<no url>')}  sheet={rsp.get('sheetName')}")
        total_written += len(chunk)
        first = False

    log(f"Wrote {total_written} rows total for {target_date}.")
if __name__ == "__main__":
    main()
