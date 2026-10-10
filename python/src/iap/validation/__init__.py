"""Alpha validation framework (spec §13, conventions §7).

Modules:

- ``metrics``  — IC / RankIC / Newey-West-lite t-stat / hit rate / decay /
  turnover / capacity proxy (formulas documented per module).
- ``splits``   — expanding walk-forward splitter with purging + embargo.
- ``leakage``  — automatic leakage tests (label-column guard, shift-by-one,
  frame truncation, and the recompute probe on the raw events).
- ``ledger``   — persistent multiple-testing ledger (research/experiments.json)
  with Bonferroni + deflated-Sharpe-style reporting.
- ``stress``   — pinned cost/latency/regime stress grid.
- ``validate`` — per-alpha orchestrator + pinned §20 promotion gates.
- ``diagnostics`` — per-fold cost survival / decay / regime and the
  stationary-bootstrap interval for net P&L (reported in every validation
  result; no gate reads it).
- ``methods``  — the research method bundles: ``"v2"``, the defaults since
  v1.5.0, ``"legacy_v1"``, the rules up to v1.4.0, and the opt-in ``"v3"``
  (v1.9 research-validity rules).
- ``sessions`` — the FOMC / holiday-thin calendar and seeded, stratified
  session-day sampling (v1.9).

The defaults and their named legacy rules are indexed in
docs/RESEARCH_VALIDITY.md.
"""

from iap.validation.diagnostics import (  # noqa: F401
    fold_diagnostics,
    stationary_bootstrap_ci,
)
from iap.validation.leakage import (  # noqa: F401
    LeakageResult,
    LeakageTester,
    RecomputeSource,
    RecomputeSources,
)
from iap.validation.ledger import ExperimentLedger  # noqa: F401
from iap.validation.methods import (  # noqa: F401
    DEFAULT_METHODS,
    METHODS,
    METHODS_LEGACY,
    METHODS_V2,
    METHODS_V3,
    ResearchMethods,
    methods,
)
from iap.validation.metrics import (  # noqa: F401
    HORIZON_ORDER,
    HORIZONS_NS,
    bucket_ics,
    capacity_breakeven,
    capacity_proxy_usd,
    day_block_bootstrap_tstat,
    day_cluster_tstat,
    decay_curve,
    hac_mean_variance,
    hit_rate,
    ic,
    ic_with_blackout_reopen,
    instrument_ics,
    newey_west_tstat,
    nw_lags,
    pooled_slope_hac_tstat,
    rank_ic,
    signal_turnover,
)
from iap.validation.sessions import (  # noqa: F401
    FOMC_DAYS,
    HOLIDAY_THIN_DAYS,
    book_scope_for_dataset,
    day_tags,
    record_day_sampling,
    stratified_day_sample,
)
from iap.validation.splits import (  # noqa: F401
    MIN_NONDEGENERATE_FOLDS,
    MIN_TEST_PAIRS,
    SPLIT_MODES,
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
from iap.validation.validate import (  # noqa: F401
    GATES,
    looks_per_validation,
    validate_alpha,
)
