@echo off
REM v1.18 REL-04 capture-ts verification (research/v118/rel04_verify/PREDECLARATION.md).
REM DO NOT START until research/preregs/rel04_capts_verify_prereg.json is SIGNED and committed,
REM the registry edit (spent_by + six capts datasets) is merged, and the REL-04 re-ingest has ended.
REM Launch DETACHED (tools/jobs/watchdog.py launch_detached), never from an agent shell, from a clean
REM checkout of the commit that holds the signed prereg, alone on the machine (features step is heavy).
REM run_rel04_verify.py refuses (exit 2) on an unsigned prereg, identity mismatch, a signer as runner,
REM an unpinned/unregistered dataset, a holdout-guard refusal, a dataset_version mismatch, too little
REM RAM or disk, or existing results. Exit 0 = the analysis completed; the verdict is in DECISION.json.
setlocal
set IAP_RUN_ACTOR=claude-queue-runner
set IAP_MAINTAINER_KEY=
set IAP_SIGNING_KEY=
set IAP_AGENT_PRIVATE_KEY=
set IAP_KEYFILE=
set REPO=%~dp0..\..\..
if "%DATA%"=="" set DATA=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\data\real
set LOG=%DATA%\rel04_verify_run.log
set PYTHONPATH=%REPO%\python\src
set PYTHONUTF8=1
set PY=python
if exist "%REPO%\.venv-ci\Scripts\python.exe" set PY=%REPO%\.venv-ci\Scripts\python.exe
cd /d "%REPO%"
echo START %DATE% %TIME% actor %IAP_RUN_ACTOR% >> "%LOG%"
git rev-parse HEAD >> "%LOG%" 2>&1
"%PY%" research\v118\rel04_verify\run_rel04_verify.py run --data-root "%DATA%" >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo EXIT %RC% %DATE% %TIME% >> "%LOG%"
endlocal & exit /b %RC%
