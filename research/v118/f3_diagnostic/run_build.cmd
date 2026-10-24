@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3\python\src
set IAP_RUN_ACTOR=claude-f3-diagnostic
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3
C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe research\v118\f3_diagnostic\build_matrices.py C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform research\v118\f3_diagnostic\matrices.npz > C:\Work\Claude\intraday-alpha-platform\wt-f3\f3_build.log 2>&1
echo %ERRORLEVEL% > C:\Work\Claude\intraday-alpha-platform\wt-f3\f3_build.exit
