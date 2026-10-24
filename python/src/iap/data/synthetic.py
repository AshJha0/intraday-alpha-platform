"""Synthetic canary panel and its contracted features (v2.0 U2 fixtures).

A seeded panel (SplitMix64, conventions section 3) of ``n_names`` names over
``n_periods`` periods with one known causal structure::

    signal[t]  ~ N(0, 1), iid, observed at t (receive_ts, lag 0)
    ret[t + 1] = beta * signal[t] + noise[t + 1]      noise ~ N(0, 1)
    fwd_ret[t] = ret[t + 1]                            the label at t

so the only legitimate predictor of ``fwd_ret[t]`` is ``signal[t]``: the
feature shifted by one period is independent of the label (future-shift
canary), a shuffled label is independent of every feature (shuffled-label
canary), and a feature that reads ``fwd_ret`` or ``ret.shift(-1)`` reads the
future (planted-leak canary).  Three feature functions carry contracts; the
planted leak is a **fixture for the detector** and says so in its contract.

The module ends with :func:`~iap.data.contracts.enforce_module`, the pattern
every feature module follows.
"""

from __future__ import annotations

import pandas as pd

from iap.core.rng import SplitMix64
from iap.data.contracts import (
    AvailabilityRule,
    DatasetContract,
    FeatureContract,
    dataset,
    enforce_module,
    feature,
)

__all__ = ["CANARY_DATASET", "LABEL", "make_canary_panel"]

#: The label column (never an input of a contracted feature).
LABEL = "fwd_ret"

CANARY_DATASET = dataset(
    DatasetContract(
        dataset_id="synthetic.canary_panel",
        source="synthetic",
        availability=AvailabilityRule("receive_ts", 0),
        adjustment="none",
        missing_policy="ineligible",
        route="synthetic",
        leakage_test_id="LK-CAUSALITY",
        fields=("period", "name", "signal", "ret", LABEL),
        period="bar",
    )
)


def make_canary_panel(
    seed: int = 20261024, n_periods: int = 60, n_names: int = 40, beta: float = 0.3
) -> pd.DataFrame:
    """The panel of the module docstring, sorted by (period, name) with a RangeIndex."""
    if n_periods < 3 or n_names < 1:
        raise ValueError("need n_periods >= 3 and n_names >= 1")
    rng = SplitMix64(seed)
    names = [f"SYN{i:03d}" for i in range(n_names)]
    sig = {n: [rng.normal() for _ in range(n_periods)] for n in names}
    noise = {n: [rng.normal() for _ in range(n_periods)] for n in names}
    rows = []
    for t in range(n_periods):
        for n in names:
            ret = noise[n][t] + (beta * sig[n][t - 1] if t > 0 else 0.0)
            fwd = (beta * sig[n][t] + noise[n][t + 1]) if t + 1 < n_periods else float("nan")
            rows.append({"period": t, "name": n, "signal": sig[n][t], "ret": ret, LABEL: fwd})
    return pd.DataFrame(rows)


@feature(
    FeatureContract(
        feature_id="canary.signal",
        inputs=("synthetic.canary_panel",),
        availability=AvailabilityRule("inputs", 0),
        adjustment="none",
        missing_policy="ineligible",
        route="synthetic",
        leakage_test_id="LK-FUTURE-SHIFT",
        lookback_periods=0,
        description="the observed signal at t: the one legitimate predictor of fwd_ret[t]",
    )
)
def feature_canary_signal(frame: pd.DataFrame) -> pd.Series:
    return frame["signal"].astype(float)


@feature(
    FeatureContract(
        feature_id="canary.trailing_mean_ret",
        inputs=("synthetic.canary_panel",),
        availability=AvailabilityRule("inputs", 0),
        adjustment="none",
        missing_policy="ineligible",
        route="synthetic",
        leakage_test_id="LK-CAUSALITY",
        lookback_periods=3,
        description="per-name trailing mean of the last three returns up to t (causal, no information)",
    )
)
def feature_canary_trailing_mean_ret(frame: pd.DataFrame) -> pd.Series:
    return frame.groupby("name", sort=False)["ret"].transform(
        lambda s: s.rolling(3, min_periods=3).mean()
    )


@feature(
    FeatureContract(
        feature_id="canary.planted_leak",
        inputs=("synthetic.canary_panel",),
        availability=AvailabilityRule("inputs", 0),
        adjustment="none",
        missing_policy="ineligible",
        route="synthetic",
        leakage_test_id="LK-PLANTED-LEAK",
        lookback_periods=0,
        description=(
            "DELIBERATE LEAK, detector fixture only, never a research feature: the next period's return, "
            "declared causal so that the contract check (not the declaration) must catch it"
        ),
    )
)
def feature_canary_planted_leak(frame: pd.DataFrame) -> pd.Series:
    return frame.groupby("name", sort=False)["ret"].shift(-1)


enforce_module(__name__)
