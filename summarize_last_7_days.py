#!/usr/bin/env python3
# summarize_last_7_days.py — run summarize_log_counts.py for each of the last 7 days that has logs

import os, sys, json, base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import time

TZ = ZoneInfo("Europe/Rome")

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

def list_logs_for_date(day: str) -> list[str]:
    payload = {"listLogs": {"folderName": "LogsArchive", "date": day}}
    r = requests.post(WEBAPP_URL, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return []
    return [f["name"] for f in data.get("files", [])]

def run_one_day(day: str):
    # Call the summarizer module as a library, to avoid new processes
    import summarize_log_counts as S
    # ensure env is loaded there too
    S.WEBAPP_URL = WEBAPP_URL
    S.summarize_day_and_post(day)

def main():
    today = datetime.now(TZ).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(0, 7)]
    for day in days:
        files = list_logs_for_date(day)
        if files:
            print(f"[runner] {day}: {len(files)} files → summarizing")
            run_one_day(day)
            time.sleep(1.0)
        else:
            print(f"[runner] {day}: no logs; skipping")

if __name__ == "__main__":
    main()
