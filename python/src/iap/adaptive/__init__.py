"""Adaptability layer (spec §20 steps 12-13): drift monitoring, refit
policies and the alpha lifecycle state machine.

Markets shift; models decay.  This package makes evolution a first-class,
*deterministic* mechanism instead of an ad-hoc rescue:

- ``drift``     — PSI / two-sample KS distribution monitors + rolling
  realized-vs-research IC, with serialized baselines
  (research/baselines/*.json, schema in the module docstring and
  /API_ADAPTIVE.md — the cross-language contract).
- ``refit``     — refit policies as objects: StaticPolicy,
  ScheduledPolicy, DriftTriggeredPolicy (pinned thresholds from
  configs/strategies.json ``adaptive`` block).
- ``lifecycle`` — ACTIVE -> WATCH -> RETIRED state machine with a pinned
  re-activation rule; every transition logged to
  research/lifecycle_log.jsonl.

The adaptive walk-forward deployment backtest that ties these together
lives in :mod:`iap.backtest.adaptive`.
"""

from iap.adaptive.drift import (  # noqa: F401
    KS_PVALUE_TERMS,
    MIN_BASELINE_N,
    PSI_BUCKETS,
    PSI_EPS,
    DriftBaseline,
    ICBaseline,
    ICWindowResult,
    bucket_counts,
    capture_baseline,
    capture_ic_baseline,
    ks_pvalue,
    ks_statistic,
    ks_test,
    psi,
    rolling_ic_z,
)
from iap.adaptive.lifecycle import (  # noqa: F401
    ACTIVE,
    RETIRED,
    STATES,
    WATCH,
    LifecycleConfig,
    LifecycleLog,
    LifecycleTracker,
    Transition,
)
from iap.adaptive.refit import (  # noqa: F401
    DriftTriggeredPolicy,
    RefitContext,
    RefitDecision,
    RefitPolicy,
    ScheduledPolicy,
    StaticPolicy,
    build_policy,
    load_adaptive_config,
    validate_adaptive_config,
)
