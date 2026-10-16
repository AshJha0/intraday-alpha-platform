@echo off
REM v1.17.1 B8 re-run with sor_sweep (same parents as run_b8_real.cmd), worktree wt-v117-b8-sweep
set PYTHONPATH=C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-sweep/python/src
set LOG=C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/b8_sweep_run.log
cd /d C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-sweep
echo START %DATE% %TIME% >> %LOG%
python -m iap.research.execution_benchmark --data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251128 --second-data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251128 --pair-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251208 C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251208 --venues research/execution/b8/venues_xnas_iexg.json --venue-calibration research/execution/b8/calibration --policies pin_nasdaq,pin_iex,sor,sor_sweep --baseline-policy pin_nasdaq --out research/execution/b8 --symbols AAPL --tag sweep_AAPL >> %LOG% 2>&1
echo BENCH_AAPL_EXIT %ERRORLEVEL% %TIME% >> %LOG%
python -m iap.research.execution_benchmark --data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251128 --second-data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251128 --pair-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251208 C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251208 --venues research/execution/b8/venues_xnas_iexg.json --venue-calibration research/execution/b8/calibration --policies pin_nasdaq,pin_iex,sor,sor_sweep --baseline-policy pin_nasdaq --out research/execution/b8 --symbols MSFT --tag sweep_MSFT >> %LOG% 2>&1
echo BENCH_MSFT_EXIT %ERRORLEVEL% %TIME% >> %LOG%
echo EXIT %DATE% %TIME% >> %LOG%
