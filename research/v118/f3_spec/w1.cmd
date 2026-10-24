@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\python\src;C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\research\v118\f3_spec
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\research\v118\f3_spec
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py a0 7 6000 out\a0_G7.json >> logs\w1.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py b0 7 6000 out\b0_G7.json >> logs\w1.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py c0 7 3000 out\c0_G7.json >> logs\w1.log 2>&1
