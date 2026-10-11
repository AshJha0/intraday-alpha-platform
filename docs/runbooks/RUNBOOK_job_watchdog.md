# Runbook: long-job watchdog

Long real-data jobs run detached through WMI-launched `.cmd` wrappers in
`data/real/` (each appends `START ...` and finally `EXIT n` to its log). A job
that dies without writing `EXIT` is otherwise silent. `tools/jobs/watchdog.py`
checks every registered job on each tick and tells you.

## What it does per tick

| state | meaning | action |
|---|---|---|
| `running` | matching process alive, log grew within `stall_minutes` | none |
| `stalled` | process alive, log unchanged for `stall_minutes` | log + toast (never kills) |
| `done` | no process, last marker matches `done_regex` (`EXIT 0`) | log + toast |
| `failed` | no process, last marker `EXIT n`, n != 0 | log + toast |
| `died` | no process, no `EXIT` after the last `START` | restart if `restart_safe` and under `max_restarts`; log + toast |
| `missing` | log file does not exist | log only |

Notifications fire on state *changes* (and on every restart), so a finished job
toasts once. Toast route: BurntToast module if installed, else the built-in
`Windows.UI.Notifications` API via PowerShell, else `msg.exe`.

The watchdog **never kills or signals a process**. Restarts use the same
`Win32_Process.Create` (WMI) detached launch as the manual flow, so restarted
jobs survive the watchdog exiting. Only mark a job `restart_safe` when its
wrapper is idempotent / resumable — a non-resumable study should stay `false`
(you get a `died` toast instead).

## Registry

Copy `configs/jobs/jobs.example.json` to `data/real/jobs.json` (git-ignored)
and edit. Relative paths resolve against the registry file's directory.
Per job: `name`, `wrapper`, `log`, `process_pattern` (regex over process
command lines), `done_regex`, `restart_safe`, `max_restarts`, `stall_minutes`,
optional `enabled`, `exit_regex`, `start_regex`. Restart counts and last state
live in `state_file` (`watchdog_state.json`); delete a job's entry to reset its
restart budget. Transitions are appended to `watchdog_log`.

## Running

```
python tools\jobs\watchdog.py --registry data\real\jobs.json --once --dry-run   # look only
python tools\jobs\watchdog.py --registry data\real\jobs.json --once
python tools\jobs\watchdog.py --registry data\real\jobs.json --loop --interval 600
tools\jobs\run_watchdog.cmd [registry]                                         # one tick
```

## Enabling it with Task Scheduler (owner does this; nothing registers it automatically)

Every 10 minutes, as the logged-in user (needed for toasts), only while logged on:

```
schtasks /Create /TN "IAP Job Watchdog" /SC MINUTE /MO 10 /IT ^
  /TR "\"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\tools\jobs\run_watchdog.cmd\""
```

Check / run now / disable / remove:

```
schtasks /Query /TN "IAP Job Watchdog" /V /FO LIST
schtasks /Run /TN "IAP Job Watchdog"
schtasks /Change /TN "IAP Job Watchdog" /DISABLE
schtasks /Delete /TN "IAP Job Watchdog" /F
```

(Or Task Scheduler GUI: Create Task -> Triggers: Daily, repeat every 10 minutes
indefinitely -> Actions: Start a program, `run_watchdog.cmd` -> "Run only when
user is logged on".) A cmd window flashes each tick; to avoid that, point the
action at `pythonw.exe` with arguments
`tools\jobs\watchdog.py --registry data\real\jobs.json --once` and the repo as
"Start in".
