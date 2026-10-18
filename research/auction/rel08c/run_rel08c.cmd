@echo off
REM v1.18 REL-08c run (research/auction/rel08c/PREDECLARATION.md). DO NOT START
REM until research/preregs/rel08c_prereg.json is SIGNED and committed. Start it detached
REM (WMI / Task Scheduler), never from an agent's shell tool, and never beside an ingest.
REM Run from a clean checkout of the commit that holds the signed prereg; the vendor
REM file stays in the main checkout. run_rel08c.py refuses (exit 2) on an unsigned
REM prereg, an identity mismatch, a signer as runner, a signing key in the environment,
REM a holdout-registry state other than in_sample, a running ingest or a sha256 mismatch;
REM then it extracts NOII from data\vendor\01302019.NASDAQ_ITCH50.gz and runs the study.
setlocal
set IAP_RUN_ACTOR=rel08c-runner
set IAP_MAINTAINER_KEY=
set IAP_SIGNING_KEY=
set IAP_AGENT_PRIVATE_KEY=
set IAP_KEYFILE=
set REPO=%~dp0..\..\..
set DATA=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform
set LOG=%DATA%\data\real\rel08c_run.log
set PYTHONUTF8=1
set PYTHONPATH=%REPO%\python\src
set PY=python
if exist "%REPO%\.venv-ci\Scripts\python.exe" set PY=%REPO%\.venv-ci\Scripts\python.exe
cd /d "%REPO%"
echo START %DATE% %TIME% actor %IAP_RUN_ACTOR% >> "%LOG%"
git rev-parse HEAD >> "%LOG%" 2>&1
"%PY%" research\auction\rel08c\run_rel08c.py run --data-root "%DATA%" >> "%LOG%" 2>&1
echo EXIT %ERRORLEVEL% %DATE% %TIME% >> "%LOG%"
endlocal
