@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3\python\src
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3
C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe research\v118\f3_diagnostic\d32_synthetic_size.py research\v118\f3_diagnostic\d32_out.json 20000 > C:\Work\Claude\intraday-alpha-platform\wt-f3\f3_d32.log 2>&1
echo %ERRORLEVEL% > C:\Work\Claude\intraday-alpha-platform\wt-f3\f3_d32.exit
