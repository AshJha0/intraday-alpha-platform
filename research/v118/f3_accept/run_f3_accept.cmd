@echo off
REM F3 acceptance study (research/v118/f3_accept/PROTOCOL.md).
REM DO NOT START until research/preregs/f3_accept_prereg.json is SIGNED by the owner, committed and merged and the
REM outside reviewer has approved the signed protocol.  Launch DETACHED (data/real/queue/launch.py), never from an agent shell,
REM from a clean checkout of the commit that holds the signed prereg.
REM run_f3_accept.py refuses (exit 2) on an unsigned prereg, identity mismatch, a signer as runner, signing keys in the
REM environment, too little RAM, or an existing DECISION.json.  Exit 0 = analysis complete; the verdict is in DECISION.json.
setlocal
set IAP_RUN_ACTOR=claude-queue-runner
set IAP_MAINTAINER_KEY=
set IAP_SIGNING_KEY=
set IAP_AGENT_PRIVATE_KEY=
set IAP_KEYFILE=
set REPO=%~dp0..\..\..
set LOG=%~dp0run_f3_accept.log
set PYTHONPATH=%REPO%\python\src
set PYTHONUTF8=1
set PY=python
if exist "%REPO%\.venv-ci\Scripts\python.exe" set PY=%REPO%\.venv-ci\Scripts\python.exe
cd /d "%REPO%"
echo START %DATE% %TIME% actor %IAP_RUN_ACTOR% >> "%LOG%"
git rev-parse HEAD >> "%LOG%" 2>&1
"%PY%" research\v118\f3_accept\run_f3_accept.py run >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo EXIT %RC% %DATE% %TIME% >> "%LOG%"
endlocal & exit /b %RC%
