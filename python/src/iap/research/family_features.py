"""Contracted features of the U3 daily families on the synthetic fixture route (v2.0 stage 7, RL1).

Every feature the research loop consumes is declared through
:mod:`iap.data.contracts` before any fit: the dataset contract of the daily
panel (``synthetic.daily_panel``, ``period_end`` availability with lag 1: a
close is not observable at the close), the common factor-model features
(trailing 60-session beta and residual dispersion, 20-session ADV) and one
signal feature per family as the specification files under
``research/v2/families/`` define them.  A feature function reads rows up to and
including the current row of its name and never a later one; the planted-leak
detector :func:`iap.data.contracts.check_causality` is run on each of them by
``python/tests/research/test_family_features.py``.

Route note.  The contracts here are on route ``synthetic`` because the daily
point-in-time set does not exist yet (ingestion waits for the isolation service).
When it does, the same functions are registered under ``daily_pit`` contracts
whose source, adjustment (``split_dividend``) and availability come from the
ingested dataset's contract; the registration is one declaration per feature,
never a change to the arithmetic (defect register, stage 7 row).

Labels are not features: :func:`forward_label` lives in :mod:`iap.research.loop`
and reads the future by definition.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from iap.data.contracts import (
    AvailabilityRule,
    DatasetContract,
    FeatureContract,
    dataset,
    enforce_module,
    feature,
)

__all__ = [
    "DAILY_PANEL",
    "FAMILY_SIGNAL",
    "REQUIRED_COLUMNS",
    "feature_common_adv_20",
    "feature_common_beta_60",
    "feature_common_sigma_60",
    "feature_d1_reversal_z",
    "feature_d1_reversal_z1",
    "feature_d2_residual_momentum",
    "feature_d3_overnight_spread",
    "feature_d4_filing_reaction_z",
    "feature_d4_filing_reaction_z_live5",
    "residual_returns",
]

REQUIRED_COLUMNS = (
    "session",
    "name",
    "open",
    "close",
    "volume",
    "auction_volume",
    "open_volume",
    "sector",
    "filing",
    "mkt_ret",
    "mkt_c2o",
)

DAILY_PANEL = dataset(
    DatasetContract(
        dataset_id="synthetic.daily_panel",
        source="synthetic",
        availability=AvailabilityRule("period_end", 1),
        adjustment="none",
        missing_policy="ineligible",
        route="synthetic",
        leakage_test_id="LK-CAUSALITY",
        fields=REQUIRED_COLUMNS,
        period="session",
    )
)

#: The signal feature of each family that runs on the daily route.
FAMILY_SIGNAL = {
    "d1": "d1.reversal_z",
    "d2": "d2.residual_momentum",
    "d3": "d3.overnight_spread",
    "d4": "d4.filing_reaction_z",
}

BETA_WINDOW = 60
Z_CAP = 4.0


def _contract(feature_id: str, lookback: int, description: str) -> FeatureContract:
    return FeatureContract(
        feature_id=feature_id,
        inputs=("synthetic.daily_panel",),
        availability=AvailabilityRule("inputs", 0),
        adjustment="none",
        missing_policy="ineligible",
        route="synthetic",
        leakage_test_id="LK-CAUSALITY",
        lookback_periods=lookback,
        description=description,
    )


def _by_name(frame: pd.DataFrame):
    return frame.groupby("name", sort=False)


def _roll(values: pd.Series, group: pd.Series, window: int, stat: str) -> pd.Series:
    """Per-group trailing window statistic (rows up to and including t), aligned to ``values``."""
    r = values.groupby(group, sort=False).rolling(window, min_periods=window)
    out = r.std(ddof=1) if stat == "std" else getattr(r, stat)()
    return out.reset_index(level=0, drop=True).sort_index()


def _log_returns(frame: pd.DataFrame) -> pd.Series:
    close = frame["close"].astype(float)
    prev = _by_name(frame)["close"].shift(1).astype(float)
    return np.log(close / prev)


def _rolling_beta(frame: pd.DataFrame, window: int = BETA_WINDOW) -> pd.Series:
    """Trailing OLS beta of the name's log return on the market return, rows up to and including t."""
    r = _log_returns(frame)
    m = np.log1p(frame["mkt_ret"].astype(float))
    g = frame["name"]
    rm = _roll(r * m, g, window, "mean")
    r_mean = _roll(r, g, window, "mean")
    m_mean = _roll(m, g, window, "mean")
    mm = _roll(m * m, g, window, "mean")
    cov = rm - r_mean * m_mean
    var = mm - m_mean * m_mean
    return cov / var.where(var > 0)


def residual_returns(frame: pd.DataFrame) -> pd.Series:
    """Log return minus the previous session's trailing beta times the market log return (causal)."""
    r = _log_returns(frame)
    m = np.log1p(frame["mkt_ret"].astype(float))
    beta_prev = _by_name(frame.assign(_b=_rolling_beta(frame)))["_b"].shift(1)
    return r - beta_prev * m


def _z(num: pd.Series, sd: pd.Series) -> pd.Series:
    return (num / sd.where(sd > 0)).clip(-Z_CAP, Z_CAP)


@feature(
    _contract("common.beta_60", BETA_WINDOW + 1, "trailing 60-session market beta of the name")
)
def feature_common_beta_60(frame: pd.DataFrame) -> pd.Series:
    return _rolling_beta(frame)


@feature(
    _contract(
        "common.sigma_60",
        2 * BETA_WINDOW + 1,
        "trailing 60-session standard deviation of the residual log return",
    )
)
def feature_common_sigma_60(frame: pd.DataFrame) -> pd.Series:
    return _roll(residual_returns(frame), frame["name"], BETA_WINDOW, "std")


@feature(_contract("common.adv_20", 20, "20-session average full-session volume in shares"))
def feature_common_adv_20(frame: pd.DataFrame) -> pd.Series:
    return _roll(frame["volume"].astype(float), frame["name"], 20, "mean")


@feature(
    _contract(
        "d1.reversal_z",
        2 * BETA_WINDOW + 6,
        "D1: minus the five-session residual return over the residual dispersion, |z| <= 4",
    )
)
def feature_d1_reversal_z(frame: pd.DataFrame) -> pd.Series:
    res = residual_returns(frame)
    g = frame["name"]
    s5 = _roll(res, g, 5, "sum")
    sd = _roll(res, g, BETA_WINDOW, "std")
    return _z(-s5, sd * np.sqrt(5.0))


@feature(
    _contract(
        "d1.reversal_z1",
        2 * BETA_WINDOW + 2,
        "D1 companion: minus the one-session residual return over the residual dispersion",
    )
)
def feature_d1_reversal_z1(frame: pd.DataFrame) -> pd.Series:
    res = residual_returns(frame)
    sd = _roll(res, frame["name"], BETA_WINDOW, "std")
    return _z(-res, sd)


@feature(
    _contract(
        "d2.residual_momentum",
        2 * BETA_WINDOW + 253,
        "D2: residual return summed over sessions t-252..t-22 (skip 21) over its dispersion, |z| <= 4",
    )
)
def feature_d2_residual_momentum(frame: pd.DataFrame) -> pd.Series:
    res = residual_returns(frame)
    g = frame["name"]
    window = 231
    skipped = res.groupby(g, sort=False).shift(21)
    total = _roll(skipped, g, window, "sum")
    sd = _roll(skipped, g, window, "std")
    return _z(total, sd * np.sqrt(float(window)))


@feature(
    _contract(
        "d3.overnight_spread",
        BETA_WINDOW + 1,
        "D3: 20-session mean of (close-to-open minus open-to-close return) over its 60-session dispersion",
    )
)
def feature_d3_overnight_spread(frame: pd.DataFrame) -> pd.Series:
    g = frame["name"]
    prev_close = _by_name(frame)["close"].shift(1).astype(float)
    c2o = np.log(frame["open"].astype(float) / prev_close)
    o2c = np.log(frame["close"].astype(float) / frame["open"].astype(float))
    spread = c2o - o2c
    mean20 = _roll(spread, g, 20, "mean")
    sd60 = _roll(spread, g, BETA_WINDOW, "std")
    return _z(mean20, sd60 / np.sqrt(20.0))


@feature(
    _contract(
        "d4.filing_reaction_z",
        2 * BETA_WINDOW + 2,
        "D4: the residual return of the first usable session after a financial filing over the dispersion; NaN otherwise",
    )
)
def feature_d4_filing_reaction_z(frame: pd.DataFrame) -> pd.Series:
    res = residual_returns(frame)
    g = frame["name"]
    sd = _roll(res, g, BETA_WINDOW, "std")
    filed_prev = _by_name(frame)["filing"].shift(1)
    z = _z(res, sd)
    return z.where(filed_prev == 1)


#: Sessions a filing reaction stays tradable in the exploratory D4 variant (D-107).
D4_LIVE_SESSIONS = 5


@feature(
    _contract(
        "d4.filing_reaction_z_live5",
        2 * BETA_WINDOW + 2 + D4_LIVE_SESSIONS,
        "D4 exploratory variant: the latest filing reaction z, held for 5 sessions after it is first usable; NaN otherwise",
    )
)
def feature_d4_filing_reaction_z_live5(frame: pd.DataFrame) -> pd.Series:
    """D-107: the registered signal is finite on about two names a session on a real universe, too
    few for a cohort; holding each reaction for five sessions pools the week's filers (exploratory only)."""
    z = feature_d4_filing_reaction_z(frame)
    held = z.groupby(frame["name"], sort=False).ffill(limit=D4_LIVE_SESSIONS - 1)
    return held


enforce_module(__name__)
