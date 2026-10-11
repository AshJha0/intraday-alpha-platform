@echo off
rem Job watchdog launcher (docs/runbooks/RUNBOOK_job_watchdog.md).
rem Usage: run_watchdog.cmd [registry]   default registry: data\real\jobs.json
rem Runs one tick; schedule it every 10 min with Task Scheduler (see the runbook).
setlocal
set "REPO=%~dp0..\.."
set "REG=%~1"
if "%REG%"=="" set "REG=%REPO%\data\real\jobs.json"
set PYTHONUTF8=1
python "%REPO%\tools\jobs\watchdog.py" --registry "%REG%" --once
exit /b %ERRORLEVEL%
