@echo off
rem Forward paper trade (docs/RUNBOOKS.md).
rem No argument: python -m iap.paper.daily --pending = every data\real\paper_* session without a
rem complete report. %1 = YYYY-MM-DD runs that one date. A completed date is a no-op.
rem Exit 0 done (or nothing pending), 10 done with alerts, 2 error.
setlocal
set "REPO=%~dp0.."
set PYTHONUTF8=1
set "PYTHONPATH=%REPO%\python\src"
set "ARGS=--pending"
if not "%~1"=="" set "ARGS=--date %~1"
if not exist "%REPO%\data\real\paper" mkdir "%REPO%\data\real\paper"
echo START %ARGS% %DATE% %TIME% >> "%REPO%\data\real\paper\paper_daily.log"
python -m iap.paper.daily %ARGS% >> "%REPO%\data\real\paper\paper_daily.log" 2>&1
set RC=%ERRORLEVEL%
echo EXIT %RC% >> "%REPO%\data\real\paper\paper_daily.log"
exit /b %RC%
