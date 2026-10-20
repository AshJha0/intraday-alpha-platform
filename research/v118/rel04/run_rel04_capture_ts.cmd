@echo off
REM REL-04 (v1.18): re-ingest the six approved IEX overlap days WITH the capture timestamp as receive_ts.
REM Output: data\real\iex_capts (never data\real\iex: --capture-ts refuses that root and refuses to
REM overwrite a dataset of the other mode). Re-downloads TOPS + DEEP+ for each day (one vendor file on
REM disk at a time, deleted after use). Resumable via the markers; a failed day is logged and the chain goes on.
REM Launch DETACHED from a clean checkout of merged main (tools/jobs/watchdog.py launch_detached), one heavy job at a time.
setlocal
if "%PY%"=="" set PY=C:\Users\ashis\AppData\Local\Programs\Python\Python312\python.exe
if "%DATA%"=="" set DATA=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data
set REPO=%~dp0..\..\..
set PYTHONPATH=%REPO%\python\src
set PYTHONUTF8=1
set IAP_RUN_ACTOR=claude-rel04-capture-ts-runner
set L=%DATA%\real\run_rel04_capture_ts.log
set FAILED=0
cd /d "%REPO%"
echo START %DATE% %TIME% >> %L%
echo DAY 2025-11-28 %DATE% %TIME% >> %L%
%PY% -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json --date 2025-11-28 --vendor-dir "%DATA%\vendor" --out-root "%DATA%\real\iex_capts" --symbols AAPL,MSFT,QQQ --capture-ts --download --i-have-approval --partition-instruments auto --min-free-gb 30 >> %L% 2>&1
if errorlevel 1 (set /a FAILED+=1 & echo EXIT_2025-11-28 FAILED >> %L%) else (echo EXIT_2025-11-28 0 >> %L%)
echo DAY 2025-12-08 %DATE% %TIME% >> %L%
%PY% -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json --date 2025-12-08 --vendor-dir "%DATA%\vendor" --out-root "%DATA%\real\iex_capts" --symbols AAPL,MSFT,QQQ --capture-ts --download --i-have-approval --partition-instruments auto --min-free-gb 30 >> %L% 2>&1
if errorlevel 1 (set /a FAILED+=1 & echo EXIT_2025-12-08 FAILED >> %L%) else (echo EXIT_2025-12-08 0 >> %L%)
echo DAY 2025-12-09 %DATE% %TIME% >> %L%
%PY% -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json --date 2025-12-09 --vendor-dir "%DATA%\vendor" --out-root "%DATA%\real\iex_capts" --symbols AAPL,MSFT,QQQ --capture-ts --download --i-have-approval --partition-instruments auto --min-free-gb 30 >> %L% 2>&1
if errorlevel 1 (set /a FAILED+=1 & echo EXIT_2025-12-09 FAILED >> %L%) else (echo EXIT_2025-12-09 0 >> %L%)
echo DAY 2025-12-10 %DATE% %TIME% >> %L%
%PY% -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json --date 2025-12-10 --vendor-dir "%DATA%\vendor" --out-root "%DATA%\real\iex_capts" --symbols AAPL,MSFT,QQQ --capture-ts --download --i-have-approval --partition-instruments auto --min-free-gb 30 >> %L% 2>&1
if errorlevel 1 (set /a FAILED+=1 & echo EXIT_2025-12-10 FAILED >> %L%) else (echo EXIT_2025-12-10 0 >> %L%)
echo DAY 2026-05-15 %DATE% %TIME% >> %L%
%PY% -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json --date 2026-05-15 --vendor-dir "%DATA%\vendor" --out-root "%DATA%\real\iex_capts" --symbols AAPL,MSFT,QQQ --capture-ts --download --i-have-approval --partition-instruments auto --min-free-gb 30 >> %L% 2>&1
if errorlevel 1 (set /a FAILED+=1 & echo EXIT_2026-05-15 FAILED >> %L%) else (echo EXIT_2026-05-15 0 >> %L%)
echo DAY 2026-05-18 %DATE% %TIME% >> %L%
%PY% -m iap.marketdata process-iex-day --manifest research\v117\iex_overlap_manifest.json --date 2026-05-18 --vendor-dir "%DATA%\vendor" --out-root "%DATA%\real\iex_capts" --symbols AAPL,MSFT,QQQ --capture-ts --download --i-have-approval --partition-instruments auto --min-free-gb 30 >> %L% 2>&1
if errorlevel 1 (set /a FAILED+=1 & echo EXIT_2026-05-18 FAILED >> %L%) else (echo EXIT_2026-05-18 0 >> %L%)
echo EXIT %FAILED% >> %L%
exit /b %FAILED%
