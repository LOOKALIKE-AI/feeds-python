#!/usr/bin/env python3
# summarize_log_counts.py
# Pull latest log from Google Drive via Apps Script, sum counters, POST results.

import gzip
import os, re, sys, base64
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
import time, random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import requests  # pip install requests

TZ = ZoneInfo("Europe/Rome")
def is_valid_day(s: str | None) -> bool:
    return bool(s and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) and "MM" not in s and "DD" not in s)

def today_rome_str() -> str:
    return datetime.now(TZ).date().isoformat()

def yesterday_rome_str() -> str:
    return (datetime.now(TZ) - timedelta(days=1)).date().isoformat()


# --- tiny .env loader ---
def load_env(path=".env"):
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            if not ln.strip() or ln.lstrip().startswith("#") or "=" not in ln:
                continue
            k, v = ln.rstrip().split("=", 1)
            os.environ.setdefault(k, v)

load_env()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
if not WEBAPP_URL:
    sys.exit("ERROR: set WEBAPP_URL in .env")

DATE_FOR_FOLDER = os.getenv("LOGS_DATE")  # e.g. 2025-09-01

def normalize_text(s: str) -> str:
    s = re.sub(r"[\u200B\u200C\u200D\u2060\uFEFF]", "", s)
    s = s.replace("\u00A0", " ")
    return s

def sum_matches(text: str, pattern: str) -> int:
    total = 0
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        try:
            total += int(m.group(1))
        except Exception:
            pass
    return total

def latest_timestamp(text: str) -> datetime | None:
    ts_pat = re.compile(
        r"^([A-Z][a-z]{2},\s\d{2}\s[A-Z][a-z]{2}\s\d{4}\s\d{2}:\d{2}:\d{2}\s[+-]\d{4})\b",
        re.MULTILINE
    )
    best = None
    for m in ts_pat.finditer(text):
        ts_str = m.group(1)
        try:
            dt = datetime.strptime(ts_str, "%a, %d %b %Y %H:%M:%S %z")
            if (best is None) or (dt > best):
                best = dt
        except Exception:
            continue
    return best

def bytes_to_text_maybe_gzip(b: bytes) -> str:
    # gzip magic: 1F 8B
    if len(b) >= 2 and b[0] == 0x1F and b[1] == 0x8B:
        try:
            return gzip.decompress(b).decode("utf-8", errors="replace")
        except Exception:
            # fallback if something odd
            return gzip.decompress(b).decode("latin-1", errors="replace")
    return b.decode("utf-8", errors="replace")
def list_logs_for_date(day: str) -> list[str]:
    """Return a sorted list of filenames for the given date folder, or [] if none."""
    payload = {"listLogs": {"folderName": "LogsArchive", "date": day}}
    r = requests.post(WEBAPP_URL, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return []
    files = data.get("files", [])
    # accept .log and .log.gz
    names = [f["name"] for f in files if isinstance(f.get("name"), str) and (f["name"].endswith(".log") or f["name"].endswith(".log.gz"))]
    return sorted(names)

def fetch_log_text_by_filename(day: str, filename: str) -> str:
    """Get one file's text by exact filename under the date folder."""
    payload = {"getLatestLog": {"folderName": "LogsArchive", "date": day, "filename": filename}}
    r = requests.post(WEBAPP_URL, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Drive fetch failed for {day}/{filename}: {data}")
    raw = base64.b64decode(data["contentBase64"])
    return bytes_to_text_maybe_gzip(raw)
def summarize_day_and_post(day: str):
    files = list_logs_for_date(day)
    if not files:
        print(f"[summarize] No logs found for {day}; skipping.")
        return {"ok": False, "day": day, "files": 0}

    # Batch fetch all files' text
    texts_by_name, misses = fetch_logs_batch(day, files, batch_size=20, base_sleep=0.25)
    if misses:
        print(f"[warn] {day}: {len(misses)} files could not be fetched (will be excluded). Example: {misses[:3]}")

    patt_err    = r"Prodotti in errore Google\s*:\s*(\d+)"
    patt_add    = r"Prodotti da aggiungere\s*:\s*(\d+)"
    patt_update = r"Prodotti da aggiornare su Google\s*:\s*(\d+)"

    tot_err = tot_add = tot_update = 0
    latest_dt = None

    for name in files:
        text = texts_by_name.get(name)
        if not text:
            continue
        tot_err    += sum_matches(text, patt_err)
        tot_add    += sum_matches(text, patt_add)
        tot_update += sum_matches(text, patt_update)

        dt = latest_timestamp(text)
        if dt and (latest_dt is None or dt > latest_dt):
            latest_dt = dt

    if latest_dt is None:
        latest_dt = datetime.fromisoformat(day).replace(tzinfo=TZ)

    payload = {
        "logCounters": {
            "date": day,
            "errore": tot_err,
            "aggiungere": tot_add,
            "aggiornare": tot_update
        }
    }
    r = requests.post(WEBAPP_URL, json=payload, timeout=60)
    print(f"[summarize] {day}: files={len(files)} used={len(texts_by_name)} miss={len(misses)} "
          f"errore={tot_err} aggiungere={tot_add} aggiornare={tot_update} → {r.status_code} {r.text.strip()}")
    return {"ok": True, "day": day, "files": len(files), "used": len(texts_by_name),
            "miss": len(misses), "errore": tot_err, "aggiungere": tot_add, "aggiornare": tot_update}
def make_session() -> requests.Session:
    retry = Retry(
        total=5, connect=5, read=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods={"POST", "GET"},
        raise_on_status=False,
    )
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s
def fetch_logs_batch(day: str, filenames: list[str], batch_size: int = 20,
                     base_sleep: float = 0.25) -> tuple[dict[str, str], list[str]]:
    """
    Returns (texts_by_name, missing_names).
    texts_by_name[name] = UTF-8 text (auto-gunzip if needed).
    """
    session = make_session()
    texts: dict[str, str] = {}
    missing: list[str] = []

    def chunks(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i:i+n]

    # First pass
    for group in chunks(filenames, batch_size):
        payload = {"getLogsBatch": {"folderName": "LogsArchive", "date": day, "filenames": group}}
        r = session.post(WEBAPP_URL, json=payload, timeout=(15, 180))
        try:
            data = r.json()
        except Exception:
            # retry once more slowly by splitting
            for single in group:
                payload1 = {"getLogsBatch": {"folderName": "LogsArchive", "date": day, "filenames": [single]}}
                r1 = session.post(WEBAPP_URL, json=payload1, timeout=(15, 180))
                try:
                    d1 = r1.json()
                except Exception:
                    missing.append(single)
                    continue
                items = d1.get("files", [])
                if not items:
                    missing.append(single); continue
                item = items[0]
                if item.get("ok"):
                    raw = base64.b64decode(item["contentBase64"])
                    texts[single] = bytes_to_text_maybe_gzip(raw)
                else:
                    missing.append(single)
            time.sleep(base_sleep + random.uniform(0, 0.15))
            continue

        for item in data.get("files", []):
            name = item.get("name")
            if not name:
                continue
            if item.get("ok"):
                raw = base64.b64decode(item["contentBase64"])
                texts[name] = bytes_to_text_maybe_gzip(raw)
            else:
                missing.append(name)
        time.sleep(base_sleep + random.uniform(0, 0.15))

    # Second pass for misses (slower, smaller batches)
    if missing:
        retry_these = missing
        missing = []
        for group in chunks(retry_these, max(1, batch_size // 3)):
            payload = {"getLogsBatch": {"folderName": "LogsArchive", "date": day, "filenames": group}}
            r = session.post(WEBAPP_URL, json=payload, timeout=(15, 180))
            ok_data = {}
            try:
                ok_data = r.json()
            except Exception:
                # last resort: mark all in this group missing
                missing.extend(group)
                continue

            for item in ok_data.get("files", []):
                name = item.get("name")
                if not name:
                    continue
                if item.get("ok"):
                    raw = base64.b64decode(item["contentBase64"])
                    texts[name] = bytes_to_text_maybe_gzip(raw)
                else:
                    missing.append(name)
            time.sleep(base_sleep*2 + random.uniform(0, 0.3))

    return texts, missing

def main():
    # Prefer explicit LOGS_DATE if valid; else fallback
    day_env = DATE_FOR_FOLDER if is_valid_day(DATE_FOR_FOLDER) else None
    if day_env is None and DATE_FOR_FOLDER:
        print(f"[summarize] Ignoring invalid LOGS_DATE='{DATE_FOR_FOLDER}' (expect YYYY-MM-DD)")

    if day_env:
        summarize_day_and_post(day_env)
        return

    # Otherwise: try today, else yesterday (Europe/Rome)
    for d in (today_rome_str(), yesterday_rome_str()):
        res = summarize_day_and_post(d)
        if res.get("ok") and res.get("files", 0) > 0:
            return
    raise RuntimeError("No logs found for today or yesterday in LogsArchive")

if __name__ == "__main__":
    main()
