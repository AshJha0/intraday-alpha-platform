"""Opt-in corrected methods: square-root impact, L1 fill cap and edge-based
capacity; the two-sample HAC drift z and the CUSUM retirement rule; the
recompute leakage probe; per-fold diagnostics and the stationary bootstrap;
blackout-aware IC and blocked rows; scale-free ICs.  Every default is the
pinned behaviour — each test group asserts that first."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from iap.adaptive.drift import (
    IC_Z_METHODS,
    ICBaseline,
    rolling_ic_z,
    rolling_ic_z_hac,
    two_sample_hac_z,
)
from iap.adaptive.lifecycle import (
    ACTIVE,
    RETIRED,
    WATCH,
    LifecycleConfig,
    LifecycleTracker,
)
from iap.alpha.base import LinearAlpha
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.core.codec import read_jsonl
from iap.labels.labels import LabelReason, MidSeries, compute_labels
from iap.validation.diagnostics import fold_diagnostics, stationary_bootstrap_ci
from iap.validation.leakage import LeakageTester, engine_frame_builder
from iap.validation.metrics import (
    bucket_ics_with_counts,
    capacity_breakeven,
    hac_mean_variance,
    ic,
    ic_with_blackout_reopen,
    instrument_ics,
    newey_west_tstat,
)
from iap.validation.validate import validate_alpha

from conftest import CONFIGS_DIR, GOLDEN_DIR
from test_validation_framework import _BackwardsAlpha, _backwards_frames

NS_S = 1_000_000_000
META = {1: {"tick_size": 0.01, "lot_size": 100, "adv": 1_000_000.0,
            "asset_class": "EQUITY", "ref_price": 25.0}}


def _cost_model(**kw) -> CostModel:
    base = dict(impact_coeff_bps_per_pct_adv=2.0, equity_taker_fee_per_share=0.003,
                fx_commission_per_million=2.5)
    base.update(kw)
    return CostModel(**base)


# ---------------------------------------------------------------------------
# 13. impact, fill cap, edge-based capacity
# ---------------------------------------------------------------------------


def test_impact_model_defaults_to_linear_and_loads_from_the_pinned_config():
    cm = CostModel.load(CONFIGS_DIR / "execution" / "execution.json")
    assert cm.impact_model == "linear" and cm.sqrt_impact_coeff_bps == 0.0
    assert cm.with_multiplier(2.0).impact_model == "linear"
    with pytest.raises(ValueError, match="unknown impact_model"):
        _cost_model(impact_model="cubic")
    with pytest.raises(ValueError):
        _cost_model(sqrt_impact_coeff_bps=-1.0)


def test_linear_costs_are_unchanged_by_the_new_fields():
    q, mid, hs = np.array([1000.0, -250.0]), np.array([25.0, 25.0]), np.array([0.01, 0.01])
    c = _cost_model().cost_components(q, mid, hs, "EQUITY", adv=1_000_000.0, lot_size=100)
    assert c["impact"][0] == pytest.approx((2.0 * 0.1) * 1e-4 * 1000 * 25.0, abs=1e-12)
    assert c["impact"][1] == pytest.approx((2.0 * 0.025) * 1e-4 * 250 * 25.0, abs=1e-12)


def test_sqrt_impact_hand_calc_and_concavity():
    cm = _cost_model().with_sqrt_impact(100.0)        # 100 bps at one ADV
    assert cm.impact_model == "sqrt" and cm.with_multiplier(2.0).impact_model == "sqrt"
    q = np.array([10_000.0, 40_000.0])                # 1 % and 4 % of ADV
    c = cm.cost_components(q, np.array([25.0, 25.0]), np.array([0.01, 0.01]),
                           "EQUITY", adv=1_000_000.0, lot_size=100)
    assert c["impact"][0] == pytest.approx(100.0 * 0.1 * 1e-4 * 10_000 * 25.0)   # 10 bps
    assert c["impact"][1] == pytest.approx(100.0 * 0.2 * 1e-4 * 40_000 * 25.0)   # 20 bps
    # 4x the size costs 2x the bps (sqrt), not 4x (linear)
    assert cm.impact_bps(np.array([0.04]))[0] == pytest.approx(
        2.0 * cm.impact_bps(np.array([0.01]))[0])
    lin = _cost_model()
    assert lin.impact_bps(np.array([0.04]))[0] == pytest.approx(
        4.0 * lin.impact_bps(np.array([0.01]))[0])
    # spread and fee are the same under both models
    lc = lin.cost_components(q, np.array([25.0, 25.0]), np.array([0.01, 0.01]),
                             "EQUITY", adv=1_000_000.0, lot_size=100)
    assert np.array_equal(lc["spread"], c["spread"]) and np.array_equal(lc["fee"], c["fee"])


def test_breakeven_size_is_where_edge_equals_cost():
    for cm in (_cost_model(), _cost_model().with_sqrt_impact(100.0),
               _cost_model(multiplier=2.0).with_sqrt_impact(60.0)):
        edge = 30e-4                                   # 30 bps per round trip
        q = cm.breakeven_size(edge, 25.0, 0.01, "EQUITY", 1_000_000.0, 100)
        assert 0.0 < q < float("inf")
        comp = cm.cost_components(np.array([q]), np.array([25.0]), np.array([0.01]),
                                  "EQUITY", 1_000_000.0, 100)
        round_trip = 2.0 * float(comp["spread"][0] + comp["fee"][0] + comp["impact"][0])
        assert round_trip == pytest.approx(edge * q * 25.0, rel=1e-9)
        # one share more loses money, one less makes it
        assert cm.breakeven_size(edge * 1.1, 25.0, 0.01, "EQUITY", 1e6, 100) > q


def test_breakeven_size_edge_cases():
    cm = _cost_model()
    fixed = (2 * 0.01 + 2 * 0.003) / 25.0
    assert cm.breakeven_size(fixed, 25.0, 0.01, "EQUITY", 1e6, 100) == 0.0
    assert cm.breakeven_size(-1e-4, 25.0, 0.01, "EQUITY", 1e6, 100) == 0.0
    free = _cost_model(impact_coeff_bps_per_pct_adv=0.0)
    assert free.breakeven_size(1e-2, 25.0, 0.01, "EQUITY", 1e6, 100) == float("inf")
    with pytest.raises(ValueError):
        cm.breakeven_size(float("nan"), 25.0, 0.01, "EQUITY", 1e6, 100)
    with pytest.raises(ValueError):
        cm.breakeven_size(1e-3, float("nan"), 0.01, "EQUITY", 1e6, 100)
    # FX sizes are in lots
    fx = cm.breakeven_size(5e-4, 1.1, 2e-5, "FX", 4e9, 1000)
    out = capacity_breakeven(cm, 5e-4, 1.1, 2e-5, "FX", 4e9, 1000)
    assert out["units"] == fx and out["notional"] == pytest.approx(fx * 1000 * 1.1)
    assert out["participation"] == pytest.approx(fx * 1000 / 4e9)


def test_capacity_breakeven_grows_with_the_edge_unlike_the_participation_proxy():
    cm = _cost_model().with_sqrt_impact(100.0)
    small = capacity_breakeven(cm, 15e-4, 25.0, 0.01, "EQUITY", 1e6)
    large = capacity_breakeven(cm, 30e-4, 25.0, 0.01, "EQUITY", 1e6)
    none = capacity_breakeven(cm, 1e-4, 25.0, 0.01, "EQUITY", 1e6)
    assert none == {"units": 0.0, "notional": 0.0, "participation": 0.0}
    assert 0.0 < small["notional"] < large["notional"]


def _l1_frame(n, bid_size, ask_size, mids=None, valid=None):
    ts = NS_S + np.arange(n, dtype=np.int64) * NS_S
    frame = pd.DataFrame({
        "exchange_ts": ts,
        "mid_price_v1": np.full(n, 25.0) if mids is None else np.asarray(mids, float),
        "spread_ticks_v1": np.full(n, 2.0),
        "depth_bid_l1_v1": np.asarray(bid_size, dtype=float),
        "depth_ask_l1_v1": np.asarray(ask_size, dtype=float),
    })
    if valid is not None:
        frame["label_valid_1s"] = np.asarray(valid, dtype=bool)
    return frame


def _scores(er):
    n = len(er)
    return pd.DataFrame({
        "exchange_ts": NS_S + np.arange(n, dtype=np.int64) * NS_S,
        "expected_return": np.asarray(er, dtype=float),
        "confidence": np.ones(n),
    })


def test_fill_cap_limits_each_fill_to_the_displayed_size():
    er = [1e-3] * 6
    frame = _l1_frame(6, bid_size=[500] * 6, ask_size=[300, 300, 300, np.nan, 300, 300])
    cfg = dict(max_pos_qty=1000, latency_rows=0)
    full = Backtester(_cost_model(), META, BacktestConfig(**cfg)).run_instrument(
        1, frame, _scores(er))
    capped = Backtester(_cost_model(), META, BacktestConfig(
        cap_fills_at_l1=True, **cfg)).run_instrument(1, frame, _scores(er))
    assert full.positions.tolist() == [1000] * 6            # fills 1000 at a 300 touch
    assert capped.positions.tolist() == [300, 600, 900, 900, 1000, 1000]
    assert capped.trade_count == 4 and capped.traded_qty == 1000
    # selling takes the BID size
    down = Backtester(_cost_model(), META, BacktestConfig(
        cap_fills_at_l1=True, **cfg)).run_instrument(1, frame, _scores([-1e-3] * 6))
    assert down.positions.tolist() == [-500, -1000, -1000, -1000, -1000, -1000]


def test_fill_cap_keeps_the_accounting_identity_and_exempts_the_session_flatten():
    day = 86_400 * NS_S
    n = 8
    frame = _l1_frame(n, bid_size=[100] * n, ask_size=[400] * n,
                      mids=25.0 + 0.01 * np.arange(n))
    ts = frame["exchange_ts"].to_numpy().copy()
    ts[4:] += day                                      # two sessions of four rows
    frame["exchange_ts"] = ts
    scores = _scores([1e-3] * n)
    scores["exchange_ts"] = ts
    res = Backtester(_cost_model(), META, BacktestConfig(
        max_pos_qty=1000, latency_rows=0, cap_fills_at_l1=True,
        flatten_at_session_end=True)).run_instrument(1, frame, scores)
    # builds 400 a row, then is flat at each close although the bid shows 100
    assert res.positions.tolist() == [400, 800, 1000, 0, 400, 800, 1000, 0]
    mark = frame["mid_price_v1"].to_numpy()
    gross = float(np.sum(res.positions[:-1] * np.diff(mark)))
    assert res.total_pnl == pytest.approx(gross - res.total_costs, abs=1e-9)
    with pytest.raises(ValueError, match="cap_fills_at_l1 needs"):
        Backtester(_cost_model(), META, BacktestConfig(cap_fills_at_l1=True)).run_instrument(
            1, frame.drop(columns=["depth_ask_l1_v1"]), scores)


# ---------------------------------------------------------------------------
# 17. blocked rows and the blackout-aware IC
# ---------------------------------------------------------------------------


def test_blocked_rows_make_no_decision_and_default_is_unchanged():
    er = [1e-3, 1e-3, -1e-3, -1e-3, 1e-3, 1e-3]
    valid = [True, True, False, False, True, True]
    frame = _l1_frame(6, [1e9] * 6, [1e9] * 6, valid=valid)
    base = Backtester(_cost_model(), META, BacktestConfig(
        max_pos_qty=100, latency_rows=0)).run_instrument(1, frame, _scores(er))
    assert base.positions.tolist() == [100, 100, -100, -100, 100, 100]
    blocked = Backtester(_cost_model(), META, BacktestConfig(
        max_pos_qty=100, latency_rows=0, block_rows_column="label_valid_1s")
    ).run_instrument(1, frame, _scores(er))
    assert blocked.positions.tolist() == [100] * 6          # the two rows are skipped
    assert blocked.trade_count == 1 and base.trade_count == 3
    with pytest.raises(ValueError, match="block_rows_column"):
        Backtester(_cost_model(), META, BacktestConfig(
            block_rows_column="label_valid_5s")).run_instrument(1, frame, _scores(er))


@pytest.mark.parametrize("latency", [
    dict(latency_rows=1), dict(latency_rows=2, max_decision_age_ns=10 * NS_S),
    dict(latency_ns=NS_S), dict(latency_ns=NS_S, max_decision_age_ns=10 * NS_S),
])
def test_blocking_applies_at_the_decision_row_under_every_latency_mode(latency):
    rng = np.random.default_rng(3)                     # test-only fixture data
    n = 60
    er = rng.standard_normal(n) * 1e-3
    valid = rng.uniform(size=n) > 0.4
    frame = _l1_frame(n, [1e9] * n, [1e9] * n, valid=valid)
    blocked = Backtester(_cost_model(), META, BacktestConfig(
        max_pos_qty=100, block_rows_column="label_valid_1s", **latency)
    ).run_instrument(1, frame, _scores(er))
    # reference: a backtest of scores whose blocked rows carry "no decision"
    # is exactly a backtest in which those rows repeat the previous target
    all_true = frame.assign(label_valid_1s=True)
    same = Backtester(_cost_model(), META, BacktestConfig(
        max_pos_qty=100, block_rows_column="label_valid_1s", **latency)
    ).run_instrument(1, all_true, _scores(er))
    plain = Backtester(_cost_model(), META, BacktestConfig(
        max_pos_qty=100, **latency)).run_instrument(1, frame, _scores(er))
    assert np.array_equal(same.positions, plain.positions)   # nothing blocked = default
    assert not np.array_equal(blocked.positions, plain.positions)
    assert blocked.trade_count <= plain.trade_count


def _halt_series():
    """1 s samples; a halt (non-tradable) at t = 10..14, reopening 2 % lower."""
    s = MidSeries()
    for t in range(30):
        if 10 <= t < 15:
            s.append(t * NS_S, float("nan"), float("nan"), False)
        else:
            s.append(t * NS_S, 100.0 if t < 10 else 98.0, 0.01, True)
    return s


def test_blackout_reopen_return_is_opt_in_and_scores_the_reopen_price():
    series = _halt_series()
    anchors = [t * NS_S for t in range(25)]
    plain = compute_labels(anchors, series, 29 * NS_S, horizons=("5s",))["5s"]
    assert plain.reopen_mid == []                            # default: untouched
    lab = compute_labels(anchors, series, 29 * NS_S, horizons=("5s",),
                         blackout_reopen=True)["5s"]
    assert (lab.mid == plain.mid or all(
        (a == b) or (a != a and b != b) for a, b in zip(lab.mid, plain.mid)))
    assert lab.valid == plain.valid and lab.reason == plain.reason
    # anchors 5..9: the 5 s horizon ends inside or just after the halt
    for t in range(5, 10):
        assert not lab.valid[t] and lab.reason[t] == LabelReason.BLACKOUT
        assert lab.reopen_mid[t] == pytest.approx(98.0 / 100.0 - 1.0)
    # anchors before the halt's reach are valid and carry no reopen value
    assert lab.valid[2] and math.isnan(lab.reopen_mid[2])
    # anchors INSIDE the halt are not tradable at the anchor: not rescued
    assert lab.reason[12] & LabelReason.ANCHOR_NOT_TRADABLE
    assert math.isnan(lab.reopen_mid[12])


def test_blackout_reopen_needs_a_reopen():
    s = MidSeries()
    for t in range(12):
        s.append(t * NS_S, 100.0 if t < 8 else float("nan"), 0.01, t < 8)
    lab = compute_labels([5 * NS_S], s, 11 * NS_S, horizons=("5s",),
                         blackout_reopen=True)["5s"]
    assert lab.reason[0] == LabelReason.BLACKOUT and math.isnan(lab.reopen_mid[0])


def test_ic_with_blackout_reopen_removes_the_survivor_selection():
    """A signal that is right on ordinary rows and wrong into every halt:
    the valid-only IC never sees the halts; the blackout-aware IC does."""
    rng = np.random.default_rng(21)
    n = 2000
    x = rng.standard_normal(n)
    label = 0.3 * x + rng.standard_normal(n)
    valid = np.ones(n, dtype=bool)
    reason = np.zeros(n, dtype=np.int64)
    reopen = np.full(n, np.nan)
    halted = np.arange(n) % 5 == 0
    valid[halted] = False
    reason[halted] = LabelReason.BLACKOUT
    reopen[halted] = -3.0 * x[halted]                   # wrong-footed by the reopen
    other = np.arange(n) % 97 == 1                      # invalid for another reason
    valid[other] = False
    reason[other] = LabelReason.BLACKOUT | LabelReason.FORWARD_STALE
    reopen[other] = 50.0
    out = ic_with_blackout_reopen(x, label, valid, reason, reopen)
    assert out["ic_valid_only"] == pytest.approx(ic(x, np.where(valid, label, np.nan)))
    assert out["ic_valid_only"] > 0.2
    assert out["ic"] < 0.0                               # the sign flips
    assert out["n_blackout_scored"] == int((halted & ~other).sum())   # BLACKOUT alone
    assert out["n_valid"] == int(valid.sum())
    # with nothing to rescue the two agree exactly
    none = ic_with_blackout_reopen(x, label, valid, reason, np.full(n, np.nan))
    assert none["ic"] == none["ic_valid_only"] and none["n_blackout_scored"] == 0
    with pytest.raises(ValueError):
        ic_with_blackout_reopen(x, label[:-1], valid, reason, reopen)


# ---------------------------------------------------------------------------
# 18. scale-free ICs
# ---------------------------------------------------------------------------


def test_instrument_ics_hand_calc():
    rng = np.random.default_rng(31)
    ids = np.repeat([1, 2, 3], 400)
    x = rng.standard_normal(1200)
    y = 0.2 * x + rng.standard_normal(1200)
    out = instrument_ics(ids, x, y)
    want = {str(i): float(np.corrcoef(x[ids == i], y[ids == i])[0, 1]) for i in (1, 2, 3)}
    assert out["by_instrument"] == pytest.approx(want)
    assert out["instrument_mean"] == pytest.approx(np.mean(list(want.values())))
    assert out["n_instruments"] == 3 and out["n_skipped"] == 0
    zx = np.concatenate([(x[ids == i] - x[ids == i].mean()) / x[ids == i].std()
                         for i in (1, 2, 3)])
    zy = np.concatenate([(y[ids == i] - y[ids == i].mean()) / y[ids == i].std()
                         for i in (1, 2, 3)])
    assert out["vol_scaled"] == pytest.approx(float(np.corrcoef(zx, zy)[0, 1]))


def test_pooled_ic_is_dominated_by_the_volatile_instrument_and_the_scaled_ones_are_not():
    """Nine quiet instruments with a real signal and one 50x more volatile
    instrument with none: the pooled IC is ~0, the scale-free ICs are not."""
    rng = np.random.default_rng(32)
    ids, xs, ys = [], [], []
    for i in range(1, 10):
        x = rng.standard_normal(500)
        ids.append(np.full(500, i))
        xs.append(x)
        ys.append((0.3 * x + rng.standard_normal(500)) * 1e-4)
    ids.append(np.full(500, 10))
    xs.append(rng.standard_normal(500))
    ys.append(rng.standard_normal(500) * 50e-4)
    ids, x, y = np.concatenate(ids), np.concatenate(xs), np.concatenate(ys)
    pooled = ic(x, y)
    out = instrument_ics(ids, x, y)
    assert abs(pooled) < 0.05
    assert out["instrument_mean"] > 0.2 and out["vol_scaled"] > 0.2


def test_instrument_ics_removes_between_instrument_level_differences():
    """Different mean score AND mean label per instrument look like signal
    in the pool; no row of any instrument carries it."""
    rng = np.random.default_rng(33)
    ids = np.repeat([1, 2], 1000)
    x = rng.standard_normal(2000) + np.where(ids == 1, 3.0, -3.0)
    y = rng.standard_normal(2000) + np.where(ids == 1, 3.0, -3.0)
    assert ic(x, y) > 0.8
    out = instrument_ics(ids, x, y)
    assert abs(out["instrument_mean"]) < 0.08 and abs(out["vol_scaled"]) < 0.08


def test_instrument_ics_degenerate_inputs():
    ids = np.repeat([1, 2], 40)
    x = np.concatenate([np.arange(40.0), np.ones(40)])       # instrument 2 is flat
    y = np.concatenate([np.arange(40.0) ** 2, np.arange(40.0)])
    out = instrument_ics(ids, x, y)
    assert out["n_instruments"] == 1 and out["n_skipped"] == 1
    empty = instrument_ics(np.array([1, 1]), np.array([1.0, 2.0]), np.array([1.0, 2.0]))
    assert math.isnan(empty["instrument_mean"]) and empty["by_instrument"] == {}
    with pytest.raises(ValueError):
        instrument_ics(ids[:-1], x, y)


def _validate_backwards(**kw):
    frames = _backwards_frames()
    meta = {1: {"tick_size": 0.01, "lot_size": 1, "adv": 1_000_000.0,
                "asset_class": "EQUITY", "ref_price": 25.0}}
    bt = Backtester(_cost_model(), meta, BacktestConfig(max_pos_qty=100, latency_rows=1))
    return frames, bt, validate_alpha(
        _BackwardsAlpha, frames, bt, {1: {"adv": 1_000_000.0, "ref_price": 25.0}},
        0.1, n_folds=4, embargo_ns=NS_S, **kw)


def test_validation_report_carries_the_scale_free_ics():
    _, _, report = _validate_backwards()
    # one instrument: both scale-free ICs are that instrument's IC = the pooled IC
    assert report["oos_ic_instrument_mean"] == pytest.approx(report["oos_ic"], rel=1e-9)
    assert report["oos_ic_vol_scaled"] == pytest.approx(report["oos_ic"], rel=1e-9)
    assert list(report["oos_ic_by_instrument"]) == ["1"]
    json.dumps(report, allow_nan=False)


# ---------------------------------------------------------------------------
# 14. drift: two-sample HAC z and the CUSUM rule
# ---------------------------------------------------------------------------


def test_hac_mean_variance_is_the_newey_west_tstat_decomposed():
    rng = np.random.default_rng(41)
    s = rng.standard_normal(60) * 0.05 + 0.02
    w = rng.integers(50, 3000, size=60).astype(float)
    for lags in (0, 2, 4):
        m, var, n = hac_mean_variance(s, lags=lags, weights=w)
        assert n == 60
        assert m / math.sqrt(var) == pytest.approx(
            newey_west_tstat(s, lags=lags, weights=w), rel=1e-12)
    m, var, n = hac_mean_variance(np.array([0.1]), lags=2)
    assert (m, n) == (0.1, 1) and math.isnan(var)
    assert hac_mean_variance(np.array([]))[2] == 0


def _ic_window(seed, n_buckets, slope, per=200, bucket_ns=300 * NS_S):
    rng = np.random.default_rng(seed)
    ts = np.repeat(np.arange(n_buckets, dtype=np.int64) * bucket_ns, per) + \
        np.tile(np.arange(per, dtype=np.int64) * NS_S, n_buckets)
    x = rng.standard_normal(n_buckets * per)
    y = slope * x + rng.standard_normal(n_buckets * per)
    return ts, x, y


def _baseline(ic_mean, ic_std, n):
    return ICBaseline(name="b", alpha_id="A", source="t", ic_mean=ic_mean, ic_std=ic_std,
                      n_buckets_baseline=n, bucket_ns=300 * NS_S, horizon="1s")


def test_two_sample_hac_z_hand_calc_and_guards():
    assert two_sample_hac_z(0.01, 4e-6, 0.03, 12e-6) == pytest.approx(-0.02 / 0.004)
    assert two_sample_hac_z(0.01, float("nan"), 0.03, 1e-6) is None
    assert two_sample_hac_z(0.01, 0.0, 0.03, 0.0) is None
    assert two_sample_hac_z(0.01, -1e-6, 0.03, 1e-6) is None
    assert IC_Z_METHODS == ("pinned", "hac")


def test_hac_z_accounts_for_the_baseline_mean_error():
    """Live and baseline drawn from the SAME process: the pinned z treats
    the baseline mean as exact and is inflated by its sampling error; the
    two-sample z is not.  Averaged over many draws the pinned z has variance
    ~ 1 + n_live / n_base, the HAC z ~ 1."""
    pinned, hac = [], []
    for seed in range(60):
        bts, bx, by = _ic_window(1000 + seed, 8, 0.05)       # a SHORT baseline
        bics, bcounts = bucket_ics_with_counts(bts, bx, by)
        baseline = _baseline(float(bics.mean()), float(bics.std()), int(bics.size))
        ts, x, y = _ic_window(5000 + seed, 24, 0.05)         # a 3x longer live window
        pinned.append(rolling_ic_z(baseline, ts, x, y).z)
        got = rolling_ic_z_hac(baseline, ts, x, y, lags=1)
        hac.append(got.z)
        assert got.n_buckets == 24 and len(got.bucket_ics) == 24
    assert np.var(pinned) > 2.5                              # ~ 1 + 24/8, and more
    assert np.var(hac) < 1.6
    assert np.mean(np.abs(np.asarray(pinned)) > 2.0) > 3 * np.mean(
        np.abs(np.asarray(hac)) > 2.0)


def test_hac_z_still_detects_a_real_shift_and_uses_the_baseline_series():
    bts, bx, by = _ic_window(7, 60, 0.20)
    bics, bcounts = bucket_ics_with_counts(bts, bx, by)
    baseline = _baseline(float(bics.mean()), float(bics.std()), int(bics.size))
    ts, x, y = _ic_window(8, 30, 0.0)                         # the signal is gone
    summary = rolling_ic_z_hac(baseline, ts, x, y)
    series = rolling_ic_z_hac(baseline, ts, x, y, baseline_bucket_ics=bics,
                              baseline_bucket_counts=bcounts)
    assert summary.z < -5.0 and series.z < -5.0
    assert summary.rolling_ic == pytest.approx(series.rolling_ic)
    # too few live buckets: silence, as in the pinned monitor
    few = rolling_ic_z_hac(baseline, ts[:400], x[:400], y[:400], min_buckets=4)
    assert few.z is None and few.rolling_ic is None and few.n_buckets == 2


def test_hac_z_widens_under_autocorrelated_bucket_ics():
    """Bucket ICs that move in long runs carry fewer independent readings
    than their count: the HAC variance must exceed the iid one."""
    rng = np.random.default_rng(43)
    n = 200
    e = rng.standard_normal(n)
    ar = np.empty(n)
    ar[0] = e[0]
    for i in range(1, n):
        ar[i] = 0.8 * ar[i - 1] + e[i]
    _, var_iid, _ = hac_mean_variance(ar, lags=0)
    _, var_hac, _ = hac_mean_variance(ar, lags=8)
    assert var_hac > 3.0 * var_iid


def _tracker(**kw):
    cfg = dict(watch_ic_gate=0.0, reactivate_ic_gate=0.01, retire_breach_evals=3,
               reactivate_evals=2)
    cfg.update(kw)
    return LifecycleTracker(alpha_id="A", config=LifecycleConfig(**cfg))


def test_lifecycle_config_defaults_to_the_consecutive_rule():
    cfg = LifecycleConfig(0.0, 0.01, 3, 2)
    assert (cfg.breach_rule, cfg.cusum_k, cfg.cusum_h) == ("consecutive", 0.0, 0.0)
    pinned = json.loads((CONFIGS_DIR / "strategies" / "strategies.json").read_text())
    block = pinned["adaptive"]["lifecycle"]
    assert "breach_rule" not in block
    assert LifecycleConfig.from_config(block).breach_rule == "consecutive"
    assert LifecycleConfig.from_config(
        {**block, "breach_rule": "cusum", "cusum_h": 0.05}).cusum_h == 0.05
    with pytest.raises(ValueError, match="unknown breach_rule"):
        LifecycleConfig(0.0, 0.01, 3, 2, breach_rule="ewma")
    with pytest.raises(ValueError, match="cusum_h"):
        LifecycleConfig(0.0, 0.01, 3, 2, breach_rule="cusum")
    with pytest.raises(ValueError, match="cusum_k"):
        LifecycleConfig(0.0, 0.01, 3, 2, cusum_k=-1.0)


def test_consecutive_rule_is_unchanged_and_ignores_new_fraction():
    a, b = _tracker(), _tracker()
    readings = [0.02, -0.01, -0.01, 0.005, -0.01, -0.01, -0.01, 0.02, 0.02, 0.02]
    for i, r in enumerate(readings):
        a.update(i, r)
        b.update(i, r, new_fraction=0.1)
    assert [t.to_dict() for t in a.transitions] == [t.to_dict() for t in b.transitions]
    assert [t.to_state for t in a.transitions] == [WATCH, RETIRED, WATCH]
    assert a.cusum == 0.0 and b.cusum == 0.0


def test_cusum_does_not_retire_on_one_bad_stretch_seen_through_overlapping_windows():
    """Three consecutive readings of a mostly shared window: the consecutive
    rule retires; the CUSUM rule, told each reading is 1/6 new, does not —
    and does retire once the same evidence is genuinely new."""
    readings = [-0.02, -0.02, -0.02]
    consecutive = _tracker()
    overlapping = _tracker(breach_rule="cusum", cusum_h=0.05)
    disjoint = _tracker(breach_rule="cusum", cusum_h=0.05)
    for i, r in enumerate(readings):
        consecutive.update(i, r)
        overlapping.update(i, r, new_fraction=1.0 / 6.0)
        disjoint.update(i, r, new_fraction=1.0)
    assert consecutive.state == RETIRED
    assert overlapping.state == WATCH
    assert overlapping.cusum == pytest.approx(3 * 0.02 / 6.0)
    assert disjoint.state == RETIRED
    assert "CUSUM" in disjoint.transitions[-1].reason
    assert disjoint.cusum == 0.0                          # reset by the verdict
    # the overlapping tracker retires after the evidence has accumulated
    for i in range(3, 20):
        if overlapping.update(i, -0.02, new_fraction=1.0 / 6.0) == RETIRED:
            break
    assert overlapping.state == RETIRED and i == 14       # 15 readings * 0.02 / 6


def test_cusum_slack_drains_and_recovery_is_unchanged():
    t = _tracker(breach_rule="cusum", cusum_h=0.05, cusum_k=0.005)
    assert t.update(0, -0.02) == WATCH                    # entering WATCH: as before
    assert t.cusum == pytest.approx(0.015)
    t.update(1, -0.004)                                   # under the gate, inside the slack
    assert t.cusum == pytest.approx(0.014) and t.state == WATCH
    t.update(2, None)                                     # silence moves nothing
    t.update(3, -0.5, informative=False)
    assert t.cusum == pytest.approx(0.014)
    t.update(4, 0.02)
    assert t.state == WATCH and t.cusum == 0.0            # drained by a good reading
    assert t.update(5, 0.02) == ACTIVE                    # two recoveries re-activate
    with pytest.raises(ValueError, match="new_fraction"):
        t.update(6, 0.0, new_fraction=0.0)
    # a retired alpha recovers through WATCH exactly as under the pinned rule
    r = _tracker(breach_rule="cusum", cusum_h=0.01)
    r.update(0, -0.05)
    assert r.update(1, -0.05) == RETIRED
    r.update(2, 0.02)
    assert r.update(3, 0.02) == WATCH and r.cusum == 0.0


# ---------------------------------------------------------------------------
# 15. recompute leakage probe
# ---------------------------------------------------------------------------


class _Ev:
    def __init__(self, ts, px):
        self.exchange_ts, self.px = ts, px


def _toy_builder(events):
    """Features from raw events: ``causal`` uses the past, ``centred`` is a
    centred 3-event mean — it reads the NEXT event (feature look-ahead)."""
    px = np.array([e.px for e in events], dtype=float)
    n = len(px)
    causal = np.array([px[max(0, i - 2): i + 1].mean() for i in range(n)])
    centred = np.array([px[max(0, i - 1): i + 2].mean() for i in range(n)])
    return {1: pd.DataFrame({
        "exchange_ts": np.array([e.exchange_ts for e in events], dtype=np.int64),
        "causal": causal - px, "centred": centred - px,
        "label_mid_1s": np.concatenate((px[1:] / px[:-1] - 1.0, [np.nan])),
        "label_valid_1s": np.concatenate((np.ones(n - 1, dtype=bool), [False])),
    })}


class _ToyAlpha(LinearAlpha):
    """Toy alpha over one engine feature.

    Economic rationale: fixture — scores a single pre-built feature column so
    the leakage probes can be pointed at a causal or a leaky feature.
    """

    alpha_id = "TSTL"
    name = "toy_feature"
    asset_class = "EQUITY"
    horizon = "1s"
    column = "causal"

    def universe(self, ids):
        return sorted(ids)

    def raw_signal(self, df):
        return df[self.column]


class _LeakyFeatureAlpha(_ToyAlpha):
    """Toy alpha over the look-ahead feature.

    Economic rationale: fixture — reads the centred (look-ahead) feature;
    its scoring path itself is causal, the FEATURE is not.
    """

    alpha_id = "TSTM"
    column = "centred"


def _toy_events(n=400, seed=51):
    rng = np.random.default_rng(seed)
    px = 100.0 + np.cumsum(rng.standard_normal(n)) * 0.05
    return [_Ev(NS_S * (i + 1), float(p)) for i, p in enumerate(px)]


def test_recompute_probe_catches_feature_lookahead_the_frame_probe_cannot_see():
    events = _toy_events()
    frames = _toy_builder(events)
    tester = LeakageTester()
    for cls in (_ToyAlpha, _LeakyFeatureAlpha):
        model = cls()
        model.fit(frames)
        # the frame-truncation probe passes BOTH: the leak is baked into the
        # precomputed feature column, and truncating the frame cannot undo it
        assert tester.truncation_probe(model, frames)
        assert tester.label_guard(model, frames)
    clean = _ToyAlpha()
    clean.fit(frames)
    leaky = _LeakyFeatureAlpha()
    leaky.fit(frames)
    # the leaky alpha is far "better" — which is the whole danger
    assert abs(ic(leaky.score(frames)[1]["expected_return"].to_numpy(),
                  frames[1]["label_mid_1s"].to_numpy())) > 0.3

    got = tester.recompute_probe(leaky, events, _toy_builder, n_probes=3)
    assert not got.ok and got.n_anchors == 3
    assert got.leaky_columns == ["centred", "confidence", "expected_return"]
    assert {m["instrument_id"] for m in got.mismatches} == {1}
    json.dumps(got.to_dict(), allow_nan=False)

    # the SAME pipeline scored by the causal alpha: the feature leak is still
    # reported (the probe checks every feature column), its score is clean
    mixed = tester.recompute_probe(clean, events, _toy_builder, n_probes=3)
    assert not mixed.ok and mixed.leaky_columns == ["centred"]

    def causal_only(evs):
        return {1: _toy_builder(evs)[1].drop(columns=["centred"])}

    ok = tester.recompute_probe(clean, events, causal_only, n_probes=3)
    assert ok.ok and ok.mismatches == [] and ok.n_anchors == 3
    assert tester.recompute_probe(None, events, causal_only, n_probes=2).ok


def test_recompute_probe_argument_checks():
    tester = LeakageTester()
    with pytest.raises(ValueError):
        tester.recompute_probe(None, _toy_events(1), _toy_builder)
    with pytest.raises(ValueError):
        tester.recompute_probe(None, _toy_events(10), _toy_builder, n_probes=0)
    assert tester.recompute_probe(None, _toy_events(2), _toy_builder, n_probes=5).n_anchors == 1


def test_reference_feature_engine_passes_the_recompute_probe():
    """The real FeatureEngine on the bundled equity vector: features rebuilt
    from truncated raw events equal the full-run features at every anchor."""
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")[:600]
    build = engine_frame_builder(CONFIGS_DIR, cadence_ns=0)
    frames = build(events)
    assert list(frames) == [1] and len(frames[1]) == 600
    assert not [c for c in frames[1].columns if c.startswith("label_")]
    got = LeakageTester().recompute_probe(None, events, build, n_probes=2)
    assert got.ok, got.leaky_columns
    assert got.n_anchors == 2


# ---------------------------------------------------------------------------
# 16. per-fold diagnostics and the stationary bootstrap
# ---------------------------------------------------------------------------


def test_stationary_bootstrap_is_seeded_and_brackets_the_estimate():
    rng = np.random.default_rng(61)
    v = rng.standard_normal(500) + 0.3
    a = stationary_bootstrap_ci(v, seed=7, n_boot=400)
    b = stationary_bootstrap_ci(v, seed=7, n_boot=400)
    c = stationary_bootstrap_ci(v, seed=8, n_boot=400)
    assert a == b and a != c
    assert a["estimate"] == pytest.approx(float(v.sum()))
    assert a["ci_low"] < a["estimate"] < a["ci_high"]
    assert a["ci_low"] > 0.0 and a["frac_resamples_le_zero"] < 0.01
    assert a["mean_block"] == 8.0 and a["n"] == 500 and a["seed"] == 7
    # iid case: the interval is close to estimate +- 1.96 * sqrt(n) * sd
    half = 1.96 * math.sqrt(500) * float(v.std())
    assert a["ci_high"] - a["ci_low"] == pytest.approx(2 * half, rel=0.25)
    json.dumps(a, allow_nan=False)


def test_stationary_bootstrap_widens_for_dependent_series():
    """Positively autocorrelated P&L: block resampling keeps the dependence
    and gives a wider interval than resampling single points."""
    rng = np.random.default_rng(62)
    e = rng.standard_normal(1500)
    ar = np.empty(1500)
    ar[0] = e[0]
    for i in range(1, 1500):
        ar[i] = 0.85 * ar[i - 1] + e[i]
    iid = stationary_bootstrap_ci(ar, seed=1, n_boot=400, mean_block=1.0)
    blocks = stationary_bootstrap_ci(ar, seed=1, n_boot=400, mean_block=40.0)
    assert (blocks["ci_high"] - blocks["ci_low"]) > 2.5 * (iid["ci_high"] - iid["ci_low"])


def test_stationary_bootstrap_degenerate_and_invalid_inputs():
    short = stationary_bootstrap_ci([1.0, 2.0, float("nan")], seed=1)
    assert short["estimate"] == 3.0 and short["ci_low"] is None and short["n"] == 2
    empty = stationary_bootstrap_ci([], seed=1)
    assert empty["estimate"] == 0.0 and empty["ci_high"] is None
    const = stationary_bootstrap_ci([2.0] * 50, seed=1, n_boot=50)
    assert const["ci_low"] == const["ci_high"] == 100.0
    for bad in (dict(n_boot=0), dict(level=1.0), dict(mean_block=0.5)):
        with pytest.raises(ValueError):
            stationary_bootstrap_ci(np.ones(20), seed=1, **bad)


def test_fold_diagnostics_reports_every_fold_and_matches_the_last_fold_of_validate():
    frames, bt, report = _validate_backwards()
    diag = fold_diagnostics(_BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S,
                            seed=20_260_919, n_boot=200)
    assert diag["n_folds_run"] == 4 == len(diag["folds"])
    assert [f["fold"] for f in diag["folds"]] == [r["fold"] for r in report["folds"]]
    for f, r in zip(diag["folds"], report["folds"]):
        assert f["n_test_pairs"] == r["n_test_pairs"] and f["degenerate"] == r["degenerate"]
        assert sorted(f["net_pnl_by_cost"]) == ["x0.5", "x1", "x2"]
        assert f["net_pnl_by_cost"]["x0.5"] > f["net_pnl_by_cost"]["x1"] > \
            f["net_pnl_by_cost"]["x2"]
        assert f["survives_1x_cost"] == (f["net_pnl_by_cost"]["x1"] > 0.0)
        assert set(f["regime"]) == {"ic_high_vol", "ic_low_vol"}
        assert f["decay_ic_by_horizon"]["1s"] < 0.0           # backwards in EVERY fold
    # the last fold is exactly what validate_alpha reports as its one view
    last = diag["folds"][-1]
    assert last["net_pnl_by_cost"]["x1"] == pytest.approx(report["net_pnl_1x_cost"])
    assert last["regime"] == pytest.approx(report["stress"]["regime"])
    assert last["decay_ic_by_horizon"]["1s"] == pytest.approx(
        report["decay_ic_by_horizon"]["1s"])
    assert diag["n_folds_survive_1x_cost"] == 0
    boot = diag["net_pnl_bootstrap"]
    assert boot["seed"] == 20_260_919 and boot["n_boot"] == 200
    assert boot["estimate"] == pytest.approx(diag["net_pnl_1x_pooled"])
    assert diag["net_pnl_1x_pooled"] == pytest.approx(
        sum(f["net_pnl_by_cost"]["x1"] for f in diag["folds"]))
    assert boot["ci_high"] < 0.0                              # loses money, reliably
    again = fold_diagnostics(_BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S,
                             seed=20_260_919, n_boot=200)
    assert again == diag
    json.dumps(diag, allow_nan=False)
    with pytest.raises(ValueError, match="multipliers must include 1.0"):
        fold_diagnostics(_BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S,
                         multipliers=(0.5, 2.0))
