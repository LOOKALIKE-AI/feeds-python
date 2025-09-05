#Feeds & Logs Automation

Automations to collect feed/log data from the gestionale portal, store raw logs in Google Drive, and write daily partner-level summaries into monthly Google Sheets. Built for macOS / Python + Selenium, with two Apps Scripts (old utility API + new monthly writer) and two GitHub Actions workflows.
TL;DR

Raw logs → Drive: LogsArchive/YYYY-MM-DD/*.log(.gz)

Daily totals (whole day, not per partner) → old sheet via existing Apps Script

Per-partner daily rows → new monthly Google Sheets:

Logs-Sheets/
  2025 logs/
    2025-09 Partner Logs   (Spreadsheet)
      2025-09-01  (sheet tab)
      2025-09-02
      ...

feeds-python/
├─ export_feeds.py                    # Scrape Feeds table → post to sheet (Feeds + Active)
├─ export_partner_logs_all.py         # (legacy) feeds/log links collector (Selenium)
├─ collect_log_ids.py                 # Build FeedID → (Partner, Code, Active) map (Selenium)
├─ get_logs_day.py                    # Upload all .log files for a day to Drive
├─ summarize_log_counts.py            # Parse one day (global totals), post to old sheet
├─ summarize_last_7_days.py           # Run summarize_log_counts.py over the last 7 days
├─ summarize_log_counts_by_partner.py # Parse one day per-partner, write monthly sheet/tab
├─ env_utils.py                       # Small env loader helpers
├─ requirements.txt
└─ .github/workflows/
   ├─ logs_summarize.yml              # Daily totals @ ~06:00 Europe/Rome
   └─ partner-logs-monthly.yml        # Collector + per-partner writer @ ~07:10 Europe/Rome

##Environment & prerequisites

Python 3.10+

macOS (collector uses Chrome headless)

Chrome installed (only needed for Selenium scripts)

pip install -r requirements.txt

##How the scripts fit together

###Feed list to sheet
export_feeds.py → posts Feeds → Apps Script mirrors Active.

###Raw logs to Drive
get_logs_day.py → uploads .log(.gz) to LogsArchive/YYYY-MM-DD/.

###Daily totals
summarize_log_counts.py → parses all logs for a day → posts totals to old sheet (logCounters).

###Per-partner daily rows
summarize_log_counts_by_partner.py:

lists file names for a date (listLogs)

fetches base64 contents in batches (getLogsBatch)

parses 3 counters via regex across each log

looks up FeedID→(Partner,Code) via getLogIDs (from old sheet; unmapped IDs show as “Feed N”)

writes all rows to the monthly spreadsheet/day tab via LOGS_WRITER_URL
first chunk clears; subsequent chunks append

###Refresh mapping
collect_log_ids.py (Selenium) scrapes the portal Feeds page and upserts LogIDs in the old sheet. Run daily or weekly.

##GitHub Actions
###logs_summarize.yml — daily totals

Triggers around 06:00 Europe/Rome (DST-safe)

Decides mode: daily (default) or last7 (Mon)

Manual run supports optional date: YYYY-MM-DD

Secrets needed:

WEBAPP_URL

###partner-logs-monthly.yml — per-partner writer

Runs ~07:10 Europe/Rome (DST-safe)

Steps:

Collect LogIDs (Selenium; optional but recommended)

Write per-partner rows into monthly file/day tab

Secrets needed:

WEBAPP_URL (listLogs/getLogsBatch/getLogIDs)

LOGS_WRITER_URL (writer)

PORTAL_LOGIN_URL, PORTAL_FEEDS_URL, PORTAL_USER, PORTAL_PASS (collector)

Manual backfill:

Run workflow → input date: YYYY-MM-DD
If omitted, defaults to yesterday (Europe/Rome).

If you don’t care about fixed local time: use a single UTC cron and remove the “gate” step. Cron in Actions is UTC.

##Conventions & headers

Daily tab headers (per-partner):

Partner | Code | FeedID | Prodotti in errore Google | Prodotti da aggiungere | Prodotti da aggiornare su Google | Updated at


Monthly file naming:

YYYY-MM Partner Logs


Year folder naming:

YYYY logs


Root Drive folder:

Logs-Sheets

##Credits

Built as part of the Lookalike project automation suite (macOS, Selenium, Google Apps Script, GitHub Actions).
