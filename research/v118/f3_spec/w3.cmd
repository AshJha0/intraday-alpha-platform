@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\python\src;C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\research\v118\f3_spec
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3-spec\research\v118\f3_spec
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py a0 5 6000 out\a0_G5.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py b0 5 6000 out\b0_G5.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py b1 5 6000 out\b1_G5.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py a1 5 6000 out\a1_G5.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py a0 10 6000 out\a0_G10.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py b0 10 6000 out\b0_G10.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py b1 10 6000 out\b1_G10.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py a1 10 6000 out\a1_G10.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py c0 5 3000 out\c0_G5.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py c1 5 3000 out\c1_G5.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py c0 10 3000 out\c0_G10.json >> logs\w3.log 2>&1
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" run_cell.py c1 10 3000 out\c1_G10.json >> logs\w3.log 2>&1
