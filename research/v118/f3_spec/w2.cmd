@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\python\src;C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\research\v118\f3_spec
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\research\v118\f3_spec
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py a1 7 6000 out\a1_G7.json >> logs\w2.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py b1 7 6000 out\b1_G7.json >> logs\w2.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py c1 7 3000 out\c1_G7.json >> logs\w2.log 2>&1
