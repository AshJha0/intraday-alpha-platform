@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3\python\src;C:\Work\Claude\intraday-alpha-platform\wt-f3\research\v118\f3_diagnostic
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3
set PY=C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe
set D=research\v118\f3_diagnostic
%PY% %D%\d13_null_matrices.py c 40 %D%\d13_c.json %D%\matrices.npz "imbalance_l1@10s/AAPL+MSFT+QQQ" > f3_d13c.log 2>&1
%PY% %D%\d13_null_matrices.py b 40 %D%\d13_b.json > f3_d13b.log 2>&1
%PY% %D%\d13_null_matrices.py a 40 %D%\d13_a.json > f3_d13a.log 2>&1
echo done > f3_d13.exit
