@echo off
REM v1.18 REL-04b B8 v2 run (research/execution/b8v2/PREDECLARATION.md). DO NOT START
REM until research/preregs/b8v2_prereg.json is SIGNED and committed. Start it detached
REM (WMI / Task Scheduler), never from an agent's shell tool. Run from a clean checkout of
REM the commit that holds the signed prereg; the market data stays in the main checkout.
REM run_b8v2.py refuses (exit 2) on an unsigned prereg, an identity mismatch, a signer as
REM runner, a dataset_version mismatch or a registry refusal; it suspends chain B while it
REM runs and resumes it at the end.
setlocal
set IAP_RUN_ACTOR=b8v2-runner
set IAP_MAINTAINER_KEY=
set IAP_SIGNING_KEY=
set IAP_AGENT_PRIVATE_KEY=
set IAP_KEYFILE=
set REPO=%~dp0..\..\..
set DATA=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real
set LOG=%DATA%\b8v2_run.log
set PYTHONPATH=%REPO%\python\src
set PY=python
if exist "%REPO%\.venv-ci\Scripts\python.exe" set PY=%REPO%\.venv-ci\Scripts\python.exe
cd /d "%REPO%"
echo START %DATE% %TIME% actor %IAP_RUN_ACTOR% >> "%LOG%"
git rev-parse HEAD >> "%LOG%" 2>&1
"%PY%" research\execution\b8v2\run_b8v2.py run --data-root "%DATA%" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% %DATE% %TIME% >> "%LOG%"
endlocal
