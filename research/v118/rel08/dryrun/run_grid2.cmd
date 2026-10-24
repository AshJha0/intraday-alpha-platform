@echo off
cd /d C:\Work\Claude\intraday-alpha-platform\wt-rel08-v2
set PYTHONUTF8=1
set PYTHONPATH=python/src
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" research\v118\rel08\dryrun\rel08_v2_dryrun.py grid2 --out C:\Work\Claude\intraday-alpha-platform\wt-rel08-v2\research\v118\rel08\dryrun\grid2_raw.json --workers 2 --reps 2 --days 2 --draws 199 --variants V1,V5,V9 > C:\Work\Claude\intraday-alpha-platform\wt-rel08-v2\research\v118\rel08\dryrun\grid2.log 2>&1
echo %ERRORLEVEL% > C:\Work\Claude\intraday-alpha-platform\wt-rel08-v2\research\v118\rel08\dryrun\grid2.exit
