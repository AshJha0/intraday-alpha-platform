"""v1.14.0 statistics revision (the review's R-P0-1, R-P1-3, R-P2-1..4).

* R-P0-1 - the research backtester blocks rows on decision-time state only
  (``block_rows_column="causal"``, the default); the label-valid block of
  v1.5.0-v1.13.0 stays reachable by name and as a report diagnostic.
* R-P1-3 - automatic HAC bandwidth, no lag product across a session day,
  the ``validity`` block in ``v2``, the sign-and-replication reading of a
  holdout of fewer than four sessions.
* R-P2-1 - ``hypothesis_confirmed`` reads the share of folds that confirm.
* R-P2-2 - per-instrument scaling before any pooled statistic.
* R-P2-3 - the time-shift leakage detector (applicable where the row shift
  is vacuous; NOT APPLICABLE stated, never a silent pass).
* R-P2-4 - one look per gate decision.

The frozen ``*_v113`` bundles reproduce the v1.13.0 rules, and the bundle
names (so the experiment ids) are unchanged.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest
from iap.backtest import BacktestConfig, Backtester, CostModel
from iap.backtest.engine import BLOCK_ROWS_AUTO, BLOCK_ROWS_CAUSAL, causal_rows
from iap.labels.labels import LabelReason
from iap.validation.leakage import TIME_SHIFT_MIN_PAIRS, LeakageTester
from iap.validation.methods import (
    METHODS,
    METHODS_V2,
    METHODS_V2_V113,
    METHODS_V3,
    METHODS_V3_V113,
    METHODS_V4,
    METHODS_V4_V113,
    STATS_REVISION,
    V113_REVISION,
    ledger_bundle,
    ledger_config,
    methods,
)
from iap.validation.metrics import auto_hac_lags, ic, n_ts_buckets, nw_lags
from iap.validation.validate import (
    DECISION_LOOKS,
    GATES,
    _fold_hypothesis_share,
    _standardise_by_instrument,
    looks_per_validation,
)
from test_research_validity_v19 import _multi_day_frames, _validate

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S

META = {
    1: {"tick_size": 0.01, "lot_size": 1, "adv": 1e9, "asset_class": "EQUITY", "ref_price": 25.0}
}


def _cost_model() -> CostModel:
    return CostModel(
        impact_coeff_bps_per_pct_adv=2.0,
        equity_taker_fee_per_share=0.003,
        fx_commission_per_million=2.5,
    )


def _frame(n: int, mids=None, valid=None, reason=None) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "exchange_ts": NS_S + np.arange(n, dtype=np.int64) * NS_S,
            "mid_price_v1": np.full(n, 25.0) if mids is None else np.asarray(mids, float),
            "spread_ticks_v1": np.full(n, 2.0),
            "depth_bid_l1_v1": np.full(n, 1e9),
            "depth_ask_l1_v1": np.full(n, 1e9),
            "label_mid_1s": np.zeros(n),
            "label_valid_1s": np.ones(n, bool) if valid is None else np.asarray(valid, bool),
        }
    )
    if reason is not None:
        frame["label_reason_1s"] = np.asarray(reason, dtype=np.int64)
    return frame


def _scores(er) -> pd.DataFrame:
    n = len(er)
    return pd.DataFrame(
        {
            "exchange_ts": NS_S + np.arange(n, dtype=np.int64) * NS_S,
            "expected_return": np.asarray(er, dtype=float),
            "confidence": np.ones(n),
        }
    )


# --------------------------------------------------------------- R-P0-1


def test_causal_block_is_the_default_and_ignores_future_label_validity():
    assert BacktestConfig().block_rows_column == BLOCK_ROWS_CAUSAL
    assert methods(METHODS_V2).backtest_config().block_rows_column == BLOCK_ROWS_CAUSAL
    assert methods(METHODS_V2_V113).backtest_config().block_rows_column == BLOCK_ROWS_AUTO
    assert methods("legacy_v1").backtest_config().block_rows_column is None
    # rows 2-3 have an invalid label for BLACKOUT (a halt INSIDE the horizon,
    # unknown at t); row 4 has no tradable anchor (known at t); row 5 no mid
    blackout, anchor = LabelReason.BLACKOUT, LabelReason.ANCHOR_NOT_TRADABLE
    reason = [0, 0, blackout, blackout | LabelReason.NOT_OBSERVED, anchor, 0]
    valid = [r == 0 for r in reason]
    mids = [25.0, 25.0, 25.0, 25.0, 25.0, float("nan")]
    frame = _frame(6, mids=mids, valid=valid, reason=reason)
    assert causal_rows(frame, NS_S).tolist() == [True, True, True, True, False, False]
    # without the reason column only the mid is read
    assert causal_rows(frame.drop(columns=["label_reason_1s"]), NS_S).tolist() == [True] * 5 + [
        False
    ]
    cfg = BacktestConfig(max_pos_qty=100, latency_rows=0, position_policy="sign")
    bt = Backtester(_cost_model(), META, cfg).for_horizon("1s")
    assert bt.config.block_rows_label() == "causal"
    np.testing.assert_array_equal(
        bt.config.allowed_rows(frame), [True, True, True, True, False, False]
    )


def test_causal_block_reads_nothing_after_the_decision():
    """The causality property: rewriting every label column (validity,
    reason bits after t, values) leaves the causal-block backtest unchanged,
    while the label-valid block's positions move."""
    rng = np.random.default_rng(11)  # test-only fixture data
    n = 80
    er = rng.standard_normal(n) * 1e-3
    mids = 25.0 + np.cumsum(rng.standard_normal(n)) * 0.01
    a = _frame(n, mids=mids, valid=rng.uniform(size=n) > 0.3)
    a["label_reason_1s"] = np.where(a["label_valid_1s"], 0, LabelReason.BLACKOUT)
    b = a.copy()
    b["label_valid_1s"] = ~a["label_valid_1s"]
    b["label_mid_1s"] = rng.standard_normal(n)
    b["label_reason_1s"] = np.where(b["label_valid_1s"], 0, LabelReason.FORWARD_STALE)

    def run(frame, block):
        cfg = BacktestConfig(
            max_pos_qty=100, latency_rows=1, position_policy="sign", block_rows_column=block
        )
        bt = Backtester(_cost_model(), META, cfg).for_horizon("1s")
        return bt.run_instrument(1, frame, _scores(er)).positions

    np.testing.assert_array_equal(run(a, BLOCK_ROWS_CAUSAL), run(b, BLOCK_ROWS_CAUSAL))
    assert not np.array_equal(run(a, BLOCK_ROWS_AUTO), run(b, BLOCK_ROWS_AUTO))


# --------------------------------------------------------------- R-P1-3


def test_automatic_bandwidth_grows_with_the_sample_and_keeps_the_horizon_floor():
    h = NS_S
    assert nw_lags(h) == 2
    assert auto_hac_lags(0, h) == auto_hac_lags(1, h) == 2
    assert auto_hac_lags(3, h) == 2  # the rule (1) is below the horizon floor
    assert auto_hac_lags(30, h) == 3  # floor(4 * 0.3^(2/9)) = floor(3.06)
    assert auto_hac_lags(100, h) == 4  # floor(4 * 1^(2/9))
    assert auto_hac_lags(1000, h) == int(np.floor(4 * 10 ** (2 / 9)))  # 6
    assert auto_hac_lags(1000, 900 * NS_S) == max(nw_lags(900 * NS_S), 6)
    ts = np.array([0, 1, 299, 300, 601], dtype=np.int64) * NS_S
    assert n_ts_buckets(ts) == 3
    lags = [auto_hac_lags(n, h) for n in (10, 50, 100, 500, 5000)]
    assert lags == sorted(lags)


def test_v2_report_uses_the_auto_bandwidth_day_separation_and_validity_block():
    frames = _multi_day_frames(per_day=400)
    new = _validate(frames, **methods(METHODS_V2).validate_kwargs())
    old = _validate(frames, **methods(METHODS_V2_V113).validate_kwargs())
    assert new["nw_lags_rule"] == "auto" and old["nw_lags_rule"] == "horizon"
    assert new["nw_lags"] >= old["nw_lags"] == nw_lags(NS_S)
    assert "validity" in new and "validity" not in old
    assert new["methods"]["hac_day_separated"] is True
    # five sessions: no few-sessions reading
    assert "few_sessions" not in new["validity"]
    # three sessions: the holdout is read by sign and replication
    first3 = {
        iid: df[df["exchange_ts"] // NS_DAY < df["exchange_ts"].min() // NS_DAY + 3].reset_index(
            drop=True
        )
        for iid, df in frames.items()
    }
    short = _validate(first3, **methods(METHODS_V2).validate_kwargs())
    few = short["validity"]["few_sessions"]
    assert few["rule"] == "sign_and_replication" and few["n_days"] == 3
    assert few["days_same_sign"] <= 3
    assert few["replicated"] == (few["days_same_sign"] == 3)


# --------------------------------------------------------------- R-P2-1


def test_hypothesis_reads_the_share_of_confirming_folds():
    rows = [{"hypothesis_confirmed": f} for f in (True, False, True, True)]
    assert _fold_hypothesis_share(rows) == 0.75
    assert 0.75 >= GATES["min_fold_sign_consistency"]
    rows_bad = [{"hypothesis_confirmed": f} for f in (False, False, True, True)]
    # the last fold confirms, but only half of the folds do
    assert _fold_hypothesis_share(rows_bad) == 0.5 < GATES["min_fold_sign_consistency"]
    assert _fold_hypothesis_share([{"beta_fit": 1.0}]) is None


def test_hypothesis_rule_in_the_report():
    frames = _multi_day_frames(per_day=400)
    new = _validate(frames, **methods(METHODS_V2).validate_kwargs())
    old = _validate(frames, **methods(METHODS_V2_V113).validate_kwargs())
    assert new["hypothesis_rule"] == "sign_consistency" and old["hypothesis_rule"] == "last_fold"
    share = _fold_hypothesis_share(new["folds"])
    assert new["hypothesis_confirmed"] == (share >= GATES["min_fold_sign_consistency"])
    assert old["hypothesis_confirmed"] == old["hypothesis_confirmed_last_fold"]
    assert all("hypothesis_confirmed" not in f for f in old["folds"])


# --------------------------------------------------------------- R-P2-2


def test_per_instrument_scaling_stops_the_volatile_instrument_dominating():
    rng = np.random.default_rng(5)  # test-only fixture data
    n = 4000
    # A: quiet, strongly positive IC; B: 50x the volatility, mildly negative
    xa = rng.standard_normal(n)
    ya = 0.5 * xa + rng.standard_normal(n)
    xb = rng.standard_normal(n) * 50.0
    yb = -0.05 * xb + rng.standard_normal(n) * 50.0
    ids = np.repeat([1, 2], n)
    x, y = np.concatenate([xa, xb]), np.concatenate([ya, yb])
    assert ic(x, y) < 0.0  # the pooled IC is B's
    xs, ys, extra = _standardise_by_instrument(ids, x, y, y * 2.0)
    assert ic(xs, ys) > 0.15  # equal footing: the average of +0.45 and -0.05
    # scale only: every per-instrument IC is unchanged
    for k in (1, 2):
        m = ids == k
        assert ic(xs[m], ys[m]) == pytest.approx(ic(x[m], y[m]), abs=1e-12)
    np.testing.assert_allclose(extra, ys * 2.0)


# --------------------------------------------------------------- R-P2-3


class _StubModel:
    horizon = "1s"

    def __init__(self, column: str) -> None:
        self.column = column

    def score(self, frames):
        return {
            iid: pd.DataFrame(
                {
                    "exchange_ts": df["exchange_ts"].to_numpy(),
                    "expected_return": df[self.column].to_numpy(dtype=float),
                    "confidence": np.ones(len(df)),
                }
            )
            for iid, df in frames.items()
        }


def _dense_frames(n=4000, gap_ns=100_000_000, seed=3):
    """Rows every 100 ms (dense against a 1 s horizon).  ``leak`` is the
    row's own label plus noise (row-specific: the labels are independent
    draws); ``signal`` is a slowly varying forecast the label follows."""
    rng = np.random.default_rng(seed)  # test-only fixture data
    signal = np.zeros(n)
    for i in range(1, n):
        signal[i] = 0.99 * signal[i - 1] + rng.standard_normal() * 0.14
    label = 0.3 * signal + rng.standard_normal(n)
    leak = label + 0.5 * rng.standard_normal(n)
    frame = pd.DataFrame(
        {
            "exchange_ts": NS_S + np.arange(n, dtype=np.int64) * gap_ns,
            "label_mid_1s": label,
            "label_valid_1s": np.ones(n, bool),
            "signal": signal,
            "leak": leak,
        }
    )
    return {1: frame}


def test_time_shift_detector_fails_a_row_specific_leak_and_passes_a_signal():
    frames = _dense_frames()
    tester = LeakageTester()
    leak = tester.run(_StubModel("leak"), frames, probe_truncation=False).to_dict()
    assert leak["time_shift_pairs"] >= TIME_SHIFT_MIN_PAIRS
    assert leak["required_time_shift_ratio"] > 0.0
    assert leak["time_shift_ok"] is False and leak["passed"] is False
    good = tester.run(_StubModel("signal"), frames, probe_truncation=False).to_dict()
    assert good["time_shift_ok"] is True and good["passed"] is True
    # v1.13.0 detector set: no time-shift keys at all
    old = LeakageTester(time_shift=False).run(_StubModel("signal"), frames, probe_truncation=False)
    assert "time_shift_ok" not in old.to_dict()


def test_time_shift_detector_says_not_applicable_on_sparse_rows():
    sparse = _dense_frames(n=600, gap_ns=2 * NS_S)  # rows already span the horizon
    res = LeakageTester().run(_StubModel("leak"), sparse, probe_truncation=False).to_dict()
    assert res["required_shift_ratio"] == 0.0  # the row shift is vacuous here ...
    assert res["time_shift_ok"] is None and res["time_shift_pairs"] == 0  # ... and says so
    assert res["ic_time_shifted"] is None


# --------------------------------------------------------------- R-P2-4


def test_one_look_per_gate_decision():
    assert DECISION_LOOKS == 1
    for n_folds in (1, 4, 6):
        assert looks_per_validation(n_folds) == 1
        assert looks_per_validation(n_folds, validity_diagnostics=True, cpcv=True) == 1
    assert looks_per_validation(4, looks_rule="statistics") == 83
    with pytest.raises(ValueError, match="looks_rule"):
        looks_per_validation(4, looks_rule="everything")
    assert methods(METHODS_V2).looks(4) == 2  # the gate decision + the holdout
    assert methods(METHODS_V2_V113).looks(4) == 84


# ------------------------------------------------------- the old revision


def test_v113_bundles_are_the_old_rules_under_new_names():
    for new, old in (
        (METHODS_V2, METHODS_V2_V113),
        (METHODS_V3, METHODS_V3_V113),
        (METHODS_V4, METHODS_V4_V113),
    ):
        a, b = methods(new), methods(old)
        assert a.revision == STATS_REVISION and b.revision == V113_REVISION
        assert b.row_block == "label_valid" and a.row_block == "causal"
        assert (b.hac_bandwidth, b.hac_day_separated) == ("horizon", False)
        assert (b.hypothesis_rule, b.standardisation) == ("last_fold", "pooled")
        assert b.leakage_time_shift is False and b.looks_rule == "statistics"
        # everything else is the bundle it freezes
        keep = {f.name for f in dataclasses.fields(a)} - {
            "name",
            "causal_row_block",
            "hac_bandwidth",
            "hac_day_separated",
            "hypothesis_rule",
            "standardisation",
            "leakage_time_shift",
            "looks_rule",
            "revision",
            "validity_diagnostics",
            "feature_window",  # v1.17 statistics revision (test_research_stats_v117)
        }
        assert all(getattr(a, k) == getattr(b, k) for k in keep)
    assert methods(METHODS_V2_V113).validity_diagnostics is False
    assert METHODS["legacy_v1"].revision == V113_REVISION


def test_ledger_identity_carries_the_revision_and_old_entries_scope_to_v113():
    base = {"n_folds": 4, "methods": "v2"}
    # the current revision (v1.17 since test_research_stats_v117; "v1.14"
    # entries scope to the frozen ``*_v114`` bundles)
    assert ledger_config(base, methods(METHODS_V2)) == {**base, "stats_revision": STATS_REVISION}
    assert ledger_config(base, methods(METHODS_V2_V113)) == base
    assert ledger_config({"x": 1}, methods("legacy_v1")) == {"x": 1}
    assert ledger_bundle(None) == "legacy_v1"
    assert ledger_bundle({"methods": "legacy_v1"}) == "legacy_v1"
    assert ledger_bundle(base) == METHODS_V2_V113
    assert ledger_bundle({**base, "stats_revision": "v1.14"}) == "v2_v114"
    assert ledger_bundle({**base, "stats_revision": STATS_REVISION}) == METHODS_V2
    runner_entry = {"configuration": {"methods": "v3"}, "experiment_id": "x"}
    assert ledger_bundle(runner_entry) == METHODS_V3_V113
    assert ledger_bundle({**runner_entry, "stats_revision": "v1.14"}) == "v3_v114"
    assert ledger_bundle({**runner_entry, "stats_revision": STATS_REVISION}) == METHODS_V3
    assert ledger_bundle({"methods": "v4_v113"}) == METHODS_V4_V113
