"""v1.15 report-only statistical additions (no verdict rule reads these)."""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
import pytest
from iap.alpha.base import beta_stability
from iap.backtest.quoting import sharpe_per_day
from iap.features.engine import _InstState
from iap.features.rolling import TimeSeries
from iap.models.splits import WalkForwardSplitter, embargo_check
from iap.validation.cpcv import probability_of_backtest_overfitting
from iap.validation.deflated import DAYS_PER_YEAR, deflated_sharpe_block, pnl_sharpe
from iap.validation.ledger import ExperimentLedger
from iap.validation.metrics import (
    BOOTSTRAP_LOW_POWER_DAYS,
    bucket_ics_demeaned,
    bucket_ics_with_counts,
    day_block_bootstrap_tstat,
    day_cluster_inference,
)

NS = 1_000_000_000
DAY = 86_400 * NS


def test_day_cluster_inference_student_t_wider_than_normal():
    r = day_cluster_inference(2.5, 5)
    assert r["df"] == 4
    assert r["crit_normal"] == pytest.approx(1.959964, abs=1e-5)
    assert r["crit_student_t"] == pytest.approx(2.776445, abs=1e-5)
    assert r["p_student_t"] > r["p_normal"]
    nan = day_cluster_inference(float("nan"), 1)
    assert math.isnan(nan["p_normal"]) and math.isnan(nan["crit_student_t"])


def test_ledger_student_threshold_report_only():
    normal = ExperimentLedger.bonferroni_t_threshold_at(10)
    student = ExperimentLedger.bonferroni_t_threshold_student_at(10, 6)
    assert student > normal
    assert ExperimentLedger.bonferroni_t_threshold_student_at(10, 1000) == pytest.approx(
        normal, rel=1e-2
    )
    assert math.isnan(ExperimentLedger.bonferroni_t_threshold_student_at(10, 0))


def _days(n_days: int, per_day: int = 200, seed: int = 0):
    rng = np.random.default_rng(seed)
    ts = np.concatenate([d * DAY + np.arange(per_day) * NS for d in range(n_days)])
    x = rng.standard_normal(ts.size)
    y = 0.3 * x + rng.standard_normal(ts.size)
    return ts, x, y


def test_bootstrap_low_power_flag():
    assert day_block_bootstrap_tstat(*_days(4), n_boot=50)["low_power"] is True
    assert (
        day_block_bootstrap_tstat(*_days(BOOTSTRAP_LOW_POWER_DAYS), n_boot=50)["low_power"] is False
    )


def test_bucket_ic_demeaned_removes_instrument_level():
    rng = np.random.default_rng(1)
    n = 400
    ts = np.zeros(n, dtype=np.int64)
    inst = np.repeat([1, 2], n // 2)
    # instrument level drives a spurious pooled IC; within-instrument noise
    x = np.where(inst == 1, 5.0, -5.0) + rng.standard_normal(n)
    y = np.where(inst == 1, 1.0, -1.0) + rng.standard_normal(n)
    raw, _ = bucket_ics_with_counts(ts, x, y)
    dm, counts = bucket_ics_demeaned(ts, inst, x, y)
    assert raw[0] > 0.5
    assert abs(dm[0]) < 0.2
    assert counts[0] == n


def test_dsr_and_psr_errors_recorded(monkeypatch):
    import iap.validation.deflated as d

    def boom(*a, **k):
        raise ValueError("bad trials")

    monkeypatch.setattr(d, "deflated_sharpe_ratio", boom)
    out = deflated_sharpe_block([1.0, 2.0, 0.5, 1.5, 3.0], 5)
    assert out["dsr"] is None and out["dsr_error"] == "bad trials"
    assert out["psr"] is not None and "psr_error" not in out


def test_shared_pnl_sharpe():
    x = [1.0, 2.0, -0.5, 1.5]
    assert sharpe_per_day(x) == pytest.approx(pnl_sharpe(x))
    assert pnl_sharpe(x) == pytest.approx(pnl_sharpe(x, None) * math.sqrt(DAYS_PER_YEAR))
    assert pnl_sharpe([1.0]) is None and pnl_sharpe([1.0, 1.0]) is None


def _state(t: int) -> _InstState:
    me = object.__new__(_InstState)
    ref = object.__new__(_InstState)
    ref.histlog = TimeSeries()
    me.ref = ref
    me.t = t
    return me


def test_ref_ret_log_same_nanosecond_tie_is_included():
    """Pins CURRENT behaviour: a reference update stamped at the same
    nanosecond as the event IS read (at-or-before)."""
    st = _state(100 * NS)
    st.ref.histlog.append(0, 0.0)
    st.ref.histlog.append(50 * NS, 0.1)
    st.ref.histlog.append(100 * NS, 0.5)  # same-ns tie
    assert st.ref_ret_log(60 * NS) == pytest.approx(0.5)
    # strict-before sensitivity excludes the tie
    assert st.ref_ret_log_strict(60 * NS) == pytest.approx(0.1)


def test_embargo_check_warns():
    assert embargo_check(60 * NS, 5 * NS) == []
    assert len(embargo_check(1 * NS, 5 * NS, 120 * NS)) == 2
    with pytest.warns(UserWarning, match="max_feature_lookback_ns"):
        WalkForwardSplitter(embargo_ns=60 * NS, max_feature_lookback_ns=300 * NS)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        WalkForwardSplitter()


def test_pbo_drops_constant_candidates():
    rng = np.random.default_rng(3)
    m = rng.standard_normal((80, 4))
    with_const = np.column_stack([m, np.zeros(80), np.ones(80)])
    a = probability_of_backtest_overfitting(m)
    b = probability_of_backtest_overfitting(with_const)
    assert a["n_constant_dropped"] == 0
    assert b["n_constant_dropped"] == 2
    assert b["n_candidates"] == 4
    assert b["pbo"] == a["pbo"]


def test_beta_stability_diagnostic():
    rng = np.random.default_rng(4)
    items = []
    for iid, beta in ((1, 0.5), (2, 0.4), (3, -0.2)):
        n = 400
        ts = np.concatenate([np.arange(200) * NS, DAY + np.arange(200) * NS])
        sig = pd.Series(rng.standard_normal(n))
        df = pd.DataFrame(
            {
                "exchange_ts": ts,
                "label_mid_5s": beta * sig.to_numpy() + 0.1 * rng.standard_normal(n),
                "label_valid_5s": True,
            }
        )
        items.append((iid, df, sig))
    r = beta_stability(items, "label_mid_5s", "label_valid_5s")
    assert r["by_instrument"]["n"] == 3
    assert r["by_instrument"]["sign_agreement"] == pytest.approx(2 / 3)
    assert r["by_instrument_day"]["n"] == 6
    assert beta_stability([], "a", "b")["by_instrument"]["n"] == 0
