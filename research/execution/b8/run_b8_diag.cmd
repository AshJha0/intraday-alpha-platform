@echo off
REM v1.17 B8 depth-walk diagnosis (same parents as run_b8_real.cmd), worktree wt-v117-b8-sweep
set PYTHONPATH=C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-sweep/python/src
set LOG=C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/b8_diag.log
cd /d C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-sweep
echo START %DATE% %TIME% >> %LOG%
python research/execution/b8/diagnose_depth_walk.py --symbol AAPL --data-root C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real --out research/execution/b8/diagnosis --dump 12 >> %LOG% 2>&1
echo DIAG_AAPL_EXIT %ERRORLEVEL% %TIME% >> %LOG%
python research/execution/b8/diagnose_depth_walk.py --symbol MSFT --data-root C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real --out research/execution/b8/diagnosis --dump 12 >> %LOG% 2>&1
echo DIAG_MSFT_EXIT %ERRORLEVEL% %TIME% >> %LOG%
echo EXIT %DATE% %TIME% >> %LOG%
