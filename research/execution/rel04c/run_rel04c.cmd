@echo off
REM REL-04c (v1.18): 20-name single-venue execution universe on the three full-market
REM ITCH days in data\real\itch\r7. Resumable: every stage writes a _DONE marker under
REM data\real\rel04c\r7x20_3day.markers and is skipped on re-launch; a failed or refused
REM stage leaves none. Launch DETACHED from a clean checkout of merged main
REM (tools/jobs/watchdog.py launch_detached), never from an agent shell.
REM Launch 1 stops at "volume" (REFUSED until yahoo_volume.json is filled and
REM predeclaration.json is frozen); launch 2 resumes from there.
setlocal
if "%PY%"=="" set PY=C:\Users\ashis\AppData\Local\Programs\Python\Python312\python.exe
if "%DATA%"=="" set DATA=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real
set REPO=%~dp0..\..\..
set PYTHONPATH=%REPO%\python\src
set PYTHONUTF8=1
set IAP_RUN_ACTOR=claude-rel04c-runner
if not exist "%DATA%\rel04c" mkdir "%DATA%\rel04c"
set L=%DATA%\rel04c\run_rel04c.log
set J=%PY% -m iap.research.rel04c_job --data-root "%DATA%"
cd /d "%REPO%"
echo START %DATE% %TIME% >> "%L%"
%J% verify >> "%L%" 2>&1 || goto fail
%J% select >> "%L%" 2>&1 || goto fail
%J% ingest --day 2019-10-18 >> "%L%" 2>&1 || goto fail
%J% ingest --day 2021-07-13 >> "%L%" 2>&1 || goto fail
%J% ingest --day 2021-08-13 >> "%L%" 2>&1 || goto fail
%J% register >> "%L%" 2>&1 || goto fail
%J% volume >> "%L%" 2>&1 || goto fail
%J% bench --bucket L1 >> "%L%" 2>&1 || goto fail
%J% bench --bucket L2 >> "%L%" 2>&1 || goto fail
%J% bench --bucket L3 >> "%L%" 2>&1 || goto fail
%J% cleanup >> "%L%" 2>&1 || goto fail
echo EXIT_0 %DATE% %TIME% >> "%L%"
exit /b 0
:fail
echo EXIT_FAILED %DATE% %TIME% >> "%L%"
exit /b 1
