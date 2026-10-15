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
- **IEX HIST (v1.17 WS0).** The same run also reads the IEX HIST index
  `https://iextrading.com/api/1.0/hist?date=YYYYMMDD` for UTC today minus 1..7
  (`--iex-days`, `--no-iex` to skip): one small JSON GET per day, same
  user-agent and timeout, never following a `link`. It reports which days have
  DEEP / TOPS files and their sizes (`iex` in the JSON report, `IEX HIST:` lines
  in the text). A day with data that the known-list's `iex_days` has not seen sets
  `iex_new` and goes into the toast; it is informational and does not change the
  exit code, which is Nasdaq's alone. A failed IEX read is recorded per day, not
  an error exit.

## Run by hand

```
set PYTHONUTF8=1
set PYTHONPATH=python\src
python tools\r7_watch_nasdaq.py --json-out data\real\r7_watch\last.json
```

`--listing-file saved.html` parses a saved listing (no network for Nasdaq; add
`--no-iex` for none at all). `--notify` raises a Windows toast through
`tools/jobs/watchdog.py`'s notifier when usable Nasdaq days or new IEX days appear.

## Daily schedule (not registered; the owner registers it)

`tools\run_r7_watch_nasdaq.cmd` runs one check with `--notify`, writing
`data\real\r7_watch\last.json` and appending to `data\real\r7_watch\watch.log`.
`data\real\` is git-ignored (`.gitignore`: `/data/real/`), so the directory is not
committed; the `.cmd` creates `data\real\r7_watch` on its first run.

The task registered with the earlier `schtasks /Create` line never ran: its Last
Result was `-2147020576` (`0x800710E0`, "the operator or administrator has refused
the request") because the task was interactive-only and set not to start on
battery, and this is a laptop. Register it from an **elevated PowerShell** with the
settings below instead (runs whether the user is logged on or not, no password
stored, starts on battery, catches up a missed 09:00, 20 min limit). Replace the
existing task first (`Unregister-ScheduledTask` or the `schtasks /Delete` line).

```powershell
$cmd = 'C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\run_r7_watch_nasdaq.cmd'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "' + $cmd + '"')
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName 'IAP R7 Nasdaq watch' -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force
```

Remove with `schtasks /Delete /TN "IAP R7 Nasdaq watch" /F`.

Check it ran (expect `Last Result: 0` nothing new, or `10` usable new day(s);
anything else is a failure, see `data\real\r7_watch\watch.log`):

```
schtasks /query /tn "IAP R7 Nasdaq watch" /v /fo LIST | findstr /c:"Last Result"
```

## When it reports something

1. Read `last.json`. Check the new day against docs/R7_DATA_PLAN.md §2 (selection).
   An IEX day (`iex.new_days`) is a second, free venue: note it in the plan; nothing
   is downloaded by the watcher.
2. If it is wanted, add it to `research/r7/r7_candidates.json` (status stays DRAFT)
   and ask the owner to approve the download.
3. After review, `python tools\r7_watch_nasdaq.py --update-known` appends the new
   entries (and the IEX days seen, under `iex_days`) to the known-list; commit it so
   the next run is quiet.
