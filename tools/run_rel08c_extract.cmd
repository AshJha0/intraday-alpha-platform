@echo off
rem REL-08c one-pass NOII extraction (research/auction/rel08c_PREDECLARATION_DRAFT.md).
rem Detached machine job: start it via WMI / Task Scheduler, never from an agent shell.
rem Refuses to run unless research\auction\rel08c_PREDECLARATION.md exists and holds
rem the line "Status: SIGNED". Never run beside an ingest (16 GB machine).
rem %1 = expected sha256 of the input (required).
setlocal
set "REPO=%~dp0.."
set PYTHONUTF8=1
set "PYTHONPATH=%REPO%\python\src"
set "PY=%REPO%\.venv-ci\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "ITCH=%REPO%\data\vendor\01302019.NASDAQ_ITCH50.gz"
set "OUT=%REPO%\data\real\rel08c_2019-01-30"
set "LOG=%REPO%\data\real\rel08c_extract.log"
if "%~1"=="" (echo usage: run_rel08c_extract.cmd EXPECTED_SHA256 & exit /b 2)
findstr /x /c:"Status: SIGNED" "%REPO%\research\auction\rel08c_PREDECLARATION.md" >nul 2>&1
if errorlevel 1 (echo REFUSED: no signed REL-08c pre-declaration >> "%LOG%" & exit /b 3)
echo START %DATE% %TIME% >> "%LOG%"
"%PY%" -m iap.auction.rel08c_extract --itch "%ITCH%" --universe "%REPO%\research\breadth\universe.json" --out "%OUT%" --date 2019-01-30 --expected-sha256 %1 >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo EXIT %RC% %DATE% %TIME% >> "%LOG%"
exit /b %RC%
