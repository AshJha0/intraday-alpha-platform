"""Alpha validation framework (spec §13, conventions §7).

Modules:

- ``metrics``  — IC / RankIC / Newey-West-lite t-stat / hit rate / decay /
  turnover / capacity proxy (formulas documented per module).
- ``splits``   — expanding walk-forward splitter with purging + embargo.
- ``leakage``  — automatic leakage tests (label-column guard, shift-by-one).
- ``ledger``   — persistent multiple-testing ledger (research/experiments.json)
  with Bonferroni + deflated-Sharpe-style reporting.
- ``stress``   — pinned cost/latency/regime stress grid.
- ``validate`` — per-alpha orchestrator + pinned §20 promotion gates.
- ``diagnostics`` — per-fold cost survival / decay / regime and the
  stationary-bootstrap interval for net P&L (additive, no gate reads it).

The opt-in corrected methods are indexed in docs/RESEARCH_VALIDITY.md.
"""

from iap.validation.leakage import LeakageResult, LeakageTester  # noqa: F401
from iap.validation.ledger import ExperimentLedger  # noqa: F401
from iap.validation.metrics import (  # noqa: F401
    HORIZON_ORDER,
    HORIZONS_NS,
    bucket_ics,
    capacity_proxy_usd,
    decay_curve,
    hit_rate,
    ic,
    capacity_breakeven,
    hac_mean_variance,
    ic_with_blackout_reopen,
    instrument_ics,
    newey_west_tstat,
    nw_lags,
    pooled_slope_hac_tstat,
    rank_ic,
    signal_turnover,
)
from iap.validation.splits import (  # noqa: F401
    MIN_NONDEGENERATE_FOLDS,
    MIN_TEST_PAIRS,
    Fold,
    WalkForwardSplitter,
)
from iap.validation.stress import (  # noqa: F401
    COST_MULTIPLIERS,
    LATENCY_SHIFTS,
    LATENCY_TIMES_NS,
    cost_stress,
    latency_stress,
    latency_stress_time,
    regime_split,
)
from iap.validation.diagnostics import (  # noqa: F401
    fold_diagnostics,
    stationary_bootstrap_ci,
)
from iap.validation.validate import GATES, validate_alpha  # noqa: F401
