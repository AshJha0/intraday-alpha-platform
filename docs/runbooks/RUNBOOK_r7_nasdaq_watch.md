# Runbook: R7 Nasdaq ITCH sample-directory watch

`tools/r7_watch_nasdaq.py` checks Nasdaq's free sample directory
`https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/` for new full-day ITCH 5.0 files.

- One read-only GET of the listing (30 s timeout, user-agent names the project).
  It **never downloads**; downloads stay manual with
  `tools/r7_download.py --i-have-approval` (docs/R7_DATA_PLAN.md §4-5).
- Compares to the committed known-list `research/r7/nasdaq_listing_known.json`
  (46 entries as of 2026-10-11, every file listed that day).
- New `S*-v50.txt.gz`, `*.NASDAQ_ITCH50.gz` and `itch50_MM_DD.gz` entries get a
  session date, weekday, `spent` (`r7plan.SPENT_DAYS`) and `candidate`
  (`research/r7/r7_candidates.json`) flags. A weekday that is neither spent nor a
  candidate is a *usable new day*. Size changes and removed entries are reported too.
- Exit codes: `0` nothing new, `10` usable new day(s), `1` error (network, parse).

## Run by hand

```
set PYTHONUTF8=1
set PYTHONPATH=python\src
python tools\r7_watch_nasdaq.py --json-out data\real\r7_watch\last.json
```

`--listing-file saved.html` parses a saved listing (no network). `--notify` raises
a Windows toast through `tools/jobs/watchdog.py`'s notifier when usable days appear.

## Daily schedule (not registered; the owner registers it)

`tools\run_r7_watch_nasdaq.cmd` runs one check with `--notify`, writing
`data\real\r7_watch\last.json` and appending to `data\real\r7_watch\watch.log`.

```
schtasks /Create /TN "IAP R7 Nasdaq watch" /SC DAILY /ST 09:00 /TR "\"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\run_r7_watch_nasdaq.cmd\"" /F
```

Remove with `schtasks /Delete /TN "IAP R7 Nasdaq watch" /F`.

## When it reports something

1. Read `last.json`. Check the new day against docs/R7_DATA_PLAN.md §2 (selection).
2. If it is wanted, add it to `research/r7/r7_candidates.json` (status stays DRAFT)
   and ask the owner to approve the download.
3. After review, `python tools\r7_watch_nasdaq.py --update-known` appends the new
   entries to the known-list; commit it so the next run is quiet.
