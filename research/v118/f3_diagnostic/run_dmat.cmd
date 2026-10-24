@echo off
set PYTHONUTF8=1
set PYTHONPATH=C:\Work\Claude\intraday-alpha-platform\wt-f3\python\src;C:\Work\Claude\intraday-alpha-platform\wt-f3\research\v118\f3_diagnostic
cd /d C:\Work\Claude\intraday-alpha-platform\wt-f3
C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe research\v118\f3_diagnostic\d_matrix.py research\v118\f3_diagnostic\matrices.npz C:\Work\Claude\intraday-alpha-platform\wt-q-nullv3\research\v118\known_null_v3_result.json research\v118\f3_diagnostic\d_matrix_out.json > C:\Work\Claude\intraday-alpha-platform\wt-f3\f3_dmat.log 2>&1
echo %ERRORLEVEL% > C:\Work\Claude\intraday-alpha-platform\wt-f3\f3_dmat.exit
