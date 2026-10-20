@echo off
REM v1.18 REL-05 real-tape fill test (research/execution/rel05/PREDECLARATION.md).
REM DO NOT START until research/preregs/rel05_prereg.json is SIGNED and committed.
REM Launch DETACHED (tools/jobs/watchdog.py launch_detached), never from an agent shell,
REM from a clean checkout of the commit that holds the signed prereg. Runs alone on the
REM machine (one day-symbol in memory at a time). run_rel05.py refuses (exit 2) on an
REM unsigned prereg, identity mismatch, a signer as runner, a holdout-registry refusal,
REM a dataset_version mismatch or too little free RAM.
setlocal
set IAP_RUN_ACTOR=claude-queue-runner
set IAP_MAINTAINER_KEY=
set IAP_SIGNING_KEY=
set IAP_AGENT_PRIVATE_KEY=
set IAP_KEYFILE=
set REPO=%~dp0..\..\..
if "%DATA%"=="" set DATA=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real
set LOG=%DATA%\rel05_run.log
set PYTHONPATH=%REPO%\python\src
set PYTHONUTF8=1
set PY=python
if exist "%REPO%\.venv-ci\Scripts\python.exe" set PY=%REPO%\.venv-ci\Scripts\python.exe
cd /d "%REPO%"
echo START %DATE% %TIME% actor %IAP_RUN_ACTOR% >> "%LOG%"
git rev-parse HEAD >> "%LOG%" 2>&1
"%PY%" research\execution\rel05\run_rel05.py run --data-root "%DATA%" >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo EXIT %RC% %DATE% %TIME% >> "%LOG%"
endlocal & exit /b %RC%
