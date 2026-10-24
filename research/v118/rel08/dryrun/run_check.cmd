@echo off
cd /d C:\Work\Claude\intraday-alpha-platform\wt-rel08-v2
set PYTHONUTF8=1
set PYTHONPATH=python/src
"C:\Work\Claude\intraday-alpha-platform\intraday-alpha-platform\.venv-ci\Scripts\python.exe" research\v118\rel08\dryrun\rel08_v2_dryrun.py check --out research\v118\rel08\dryrun\check_raw.json --workers 2 --reps 3 --null-reps 12 --days 3 --draws 499 --alpha 20 --ra 100 --iters 20 --variants V1,V2,V5,V6,V9,V10,V11 > research\v118\rel08\dryrun\check.log 2>&1
echo %ERRORLEVEL% > research\v118\rel08\dryrun\check.exit
