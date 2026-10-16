@echo off
REM v1.17 B8 real-book run (2 overlap days), code commit d5c33cf in C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-run
set PYTHONPATH=C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-run/python/src
set LOG=C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/b8_real_run.log
cd /d C:/Work/Claude/intraday-alpha-platform/wt-v117-b8-run
echo START commit d5c33cf %DATE% %TIME% >> %LOG%
python -m iap.research.execution_benchmark --data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251128 --second-data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251128 --pair-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251208 C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251208 --venues research/execution/b8/venues_xnas_iexg.json --venue-calibration research/execution/b8/calibration --policies pin_nasdaq,pin_iex,sor --baseline-policy pin_nasdaq --out research/execution/b8 --symbols AAPL --tag overlap2_AAPL >> %LOG% 2>&1
echo BENCH_AAPL_EXIT %ERRORLEVEL% %TIME% >> %LOG%
python -m iap.research.execution_benchmark --data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251128 --second-data-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251128 --pair-dir C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/r7_20251208 C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform/data/real/iex/iex_20251208 --venues research/execution/b8/venues_xnas_iexg.json --venue-calibration research/execution/b8/calibration --policies pin_nasdaq,pin_iex,sor --baseline-policy pin_nasdaq --out research/execution/b8 --symbols MSFT --tag overlap2_MSFT >> %LOG% 2>&1
echo BENCH_MSFT_EXIT %ERRORLEVEL% %TIME% >> %LOG%
echo EXIT %DATE% %TIME% >> %LOG%
