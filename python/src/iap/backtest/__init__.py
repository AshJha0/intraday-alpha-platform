"""Fast vectorized research backtester (spec §18, research engine).

- ``costs``  — pinned cost model (half-spread + fees + linear impact) from
  configs/execution.json ``cost_model``.
- ``engine`` — decision-at-t / execute-at-t+latency vectorized backtester
  with exact accounting identity, per-alpha and ensemble runs.
- ``adaptive`` — walk-forward deployment backtest with refit policies,
  drift monitoring and lifecycle gating (imported lazily via
  ``iap.backtest.adaptive`` to keep the base package light).
"""

from iap.backtest.costs import CostModel  # noqa: F401
from iap.backtest.engine import (  # noqa: F401
    Backtester,
    BacktestConfig,
    BacktestResult,
    InstrumentResult,
    ensemble_scores,
)
