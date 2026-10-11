@echo off
rem Nasdaq ITCH sample-directory watch (docs/runbooks/RUNBOOK_r7_nasdaq_watch.md).
rem One read-only GET of the listing; never downloads. Exit 0 nothing new, 10 new day(s), 1 error.
setlocal
set "REPO=%~dp0.."
set PYTHONUTF8=1
set "PYTHONPATH=%REPO%\python\src"
if not exist "%REPO%\data\real\r7_watch" mkdir "%REPO%\data\real\r7_watch"
python "%REPO%\tools\r7_watch_nasdaq.py" --notify --json-out "%REPO%\data\real\r7_watch\last.json" >> "%REPO%\data\real\r7_watch\watch.log" 2>&1
exit /b %ERRORLEVEL%
