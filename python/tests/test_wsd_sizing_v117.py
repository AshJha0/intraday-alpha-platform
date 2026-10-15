"""v1.17 WS-D items D2 and D3: the backtester ``sizing_hook``, the
meta-label calibrate/select split (R-5), conformal sizing through the hook
and the MVP engine, and the coverage-by-regime gate.

Everything here is opt-in: with no hook, ``split_mode="legacy"`` and no
``portfolio.conformal`` block, every path is the v1.16 one (asserted below
against the unhooked backtest and the golden-config parse).
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from conftest import CONFIGS_DIR
from iap.backtest import BacktestConfig, Backtester, CostModel, SignalRow
from iap.models.conformal import (
    SplitConformal,
    conformal_sizing_hook,
    coverage_by_regime,
)
from iap.models.metalabel import meta_probability_hook, run_meta_labeling
from iap.mvp.config import ConformalSpec, MvpConfig, load_config
from iap.mvp.sizing import SizingMixin
from iap.validation.ledger import ExperimentLedger
from test_metalabel import _meta_dataset

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S
META = {
    1: {
        "tick_size": 0.01,
        "lot_size": 100,
        "adv": 1_000_000.0,
        "asset_class": "EQUITY",
        "ref_price": 25.0,
    }
}


def _cost_model():
    return CostModel(
        impact_coeff_bps_per_pct_adv=2.0,
        equity_taker_fee_per_share=0.003,
        fx_commission_per_million=2.5,
    )


def _frame(n=60, seed=3):
    rng = np.random.default_rng(seed)
    mids = 25.0 + np.cumsum(rng.normal(0.0, 0.01, n))
    return pd.DataFrame(
        {
            "exchange_ts": np.arange(n, dtype=np.int64) * NS_S + NS_S,
            "mid_price_v1": mids,
            "spread_ticks_v1": np.full(n, 2.0),
            "depth_bid_l1_v1": np.full(n, 5000.0),
            "depth_ask_l1_v1": np.full(n, 5000.0),
        }
    )


def _scores(n=60, seed=4, **extra):
    rng = np.random.default_rng(seed)
    er = rng.normal(0.0, 1e-3, n)
    er[::7] = 0.0  # some rows with no view
    return pd.DataFrame({"expected_return": er, "confidence": np.ones(n), **extra})


def _run(hook=None, scores=None):
    cfg = BacktestConfig.legacy(latency_rows=1)
    bt = Backtester(_cost_model(), META, cfg, sizing_hook=hook)
    return bt.run_instrument(1, _frame(), scores if scores is not None else _scores())


# ----------------------------------------------------------- sizing_hook


def test_no_hook_and_identity_hook_are_the_pinned_backtest():
    base = _run()
    assert base.sizing_multipliers is None
    same = _run(lambda row: 1.0)
    assert np.array_equal(base.positions, same.positions)
    assert same.total_pnl == base.total_pnl
    assert same.sizing_multipliers is not None and np.all(same.sizing_multipliers == 1.0)


def test_hook_scales_decisions_and_sees_decision_time_state_only():
    seen: list[SignalRow] = []

    def half(row: SignalRow) -> float:
        seen.append(row)
        return 0.5

    base = _run()
    res = _run(half, _scores(meta_prob=np.linspace(0.0, 1.0, 60)))
    assert seen, "hook was called"
    # called only where the policy wants a position; never on a zero target
    assert all(r.target_qty != 0.0 for r in seen)
    assert all(abs(r.target_qty) == BacktestConfig().max_pos_qty for r in seen)
    # the row's scores carry the extra columns, not the signal columns
    assert set(seen[0].scores) == {"meta_prob"}
    assert seen[0].ts == int(_frame()["exchange_ts"].iloc[seen[0].row])
    # positions are exactly half the unhooked ones (legacy rules, no fill cap)
    assert np.allclose(res.positions, 0.5 * base.positions)
    mult = res.sizing_multipliers
    assert mult is not None and set(np.unique(mult)) <= {0.5, 1.0}
    assert np.all(mult[[r.row for r in seen]] == 0.5)


def test_hook_zero_declines_and_bad_multipliers_are_errors():
    res = _run(lambda row: 0.0)
    assert np.all(res.positions == 0.0) and res.trade_count == 0
    for bad in (1.5, -0.1, float("nan"), "x"):
        with pytest.raises(ValueError):
            _run(lambda row, b=bad: b)
    with pytest.raises(TypeError):
        Backtester(_cost_model(), META, sizing_hook=3)


def test_for_horizon_carries_the_hook():
    bt = Backtester(_cost_model(), META, sizing_hook=lambda row: 0.5)
    assert bt.for_horizon("5s").sizing_hook is bt.sizing_hook


# --------------------------------------------------------- D2 meta hook


def _row(**scores) -> SignalRow:
    return SignalRow(1, 0, 0, 1e-3, 1.0, 25.0, 0.01, 1000.0, scores)


def test_meta_probability_hook_gate_linear_and_missing():
    gate = meta_probability_hook(0.6)
    assert gate(_row(meta_prob=0.6)) == 1.0 and gate(_row(meta_prob=0.59)) == 0.0
    assert gate(_row()) == 0.0 and gate(_row(meta_prob=float("nan"))) == 0.0
    assert meta_probability_hook(0.6, missing=1.0)(_row()) == 1.0
    lin = meta_probability_hook(0.5, mode="linear")
    assert lin(_row(meta_prob=0.5)) == 0.0
    assert lin(_row(meta_prob=0.75)) == pytest.approx(0.5)
    assert lin(_row(meta_prob=1.0)) == 1.0 and lin(_row(meta_prob=0.2)) == 0.0
    for bad in (dict(tau=1.0), dict(tau=0.5, mode="step"), dict(tau=0.5, missing=2.0)):
        with pytest.raises(ValueError):
            meta_probability_hook(**bad)
    # through the backtester: the gate removes the rows below tau
    p = np.linspace(0.0, 1.0, 60)
    res = _run(meta_probability_hook(0.5, column="meta_prob"), _scores(meta_prob=p))
    assert res.sizing_multipliers is not None
    called = res.sizing_multipliers != 1.0
    assert np.all(p[called] < 0.5)


# --------------------------------------------------- D2 calibrate/select


@pytest.fixture(scope="module")
def meta_cs():
    ds, pred = _meta_dataset()
    res = run_meta_labeling(ds, pred, split_mode="calibrate_select", return_model=True)
    return res, ds, pred


def test_calibrate_select_split_is_four_way_and_disjoint(meta_cs):
    res, _, _ = meta_cs
    assert res["split_mode"] == "calibrate_select"
    assert res["selection_segment"] == "select"
    seg = res["segments"]
    assert seg["train"] > seg["calibration"] > 0 and seg["select"] > 0 and seg["test"] > 0
    w = res["segment_windows"]
    # chronological and disjoint, calibration strictly before select
    assert w["train"][1] < w["calibration"][0] < w["calibration"][1] < w["select"][0]
    assert w["select"][1] < w["test"][0]
    assert res["primary_selection_window"] is None
    assert res["calibration_overlaps_primary_selection"] is None
    assert res["n_calibration_rows_purged_for_primary_selection"] == 0
    assert res["best_tau_from_calibration"] in {r["tau"] for r in res["tau_sweep_calibration"]}
    assert sum(r["n_trades"] for r in res["tau_sweep_calibration"]) <= seg["select"] * len(
        res["tau_sweep_calibration"]
    )
    # the calibrator comes back on request and scores the meta features
    assert hasattr(res["calibrator"], "predict_proba")


def test_primary_selection_window_purges_calibration_rows_and_is_recorded(meta_cs):
    res, ds, pred = meta_cs
    cal_lo, cal_hi = res["segment_windows"]["calibration"]
    window = (cal_lo, cal_lo + (cal_hi - cal_lo) // 3)
    out = run_meta_labeling(
        ds, pred, split_mode="calibrate_select", primary_selection_window=window
    )
    assert out["primary_selection_window"] == list(window)
    assert out["calibration_overlaps_primary_selection"] is False
    assert out["n_calibration_rows_purged_for_primary_selection"] > 0
    assert out["segments"]["calibration"] < res["segments"]["calibration"]
    assert out["segment_windows"]["calibration"][0] > window[1]
    # the other segments are untouched
    for k in ("train", "select", "test"):
        assert out["segments"][k] == res["segments"][k]
    with pytest.raises(ValueError):
        run_meta_labeling(ds, pred, primary_selection_window=(10, 1))
    with pytest.raises(ValueError):
        run_meta_labeling(ds, pred, split_mode="fivefold")


def test_legacy_split_is_unchanged_and_the_ledger_records_the_split(tmp_path):
    ds, pred = _meta_dataset()
    legacy = run_meta_labeling(ds, pred)
    assert legacy["split_mode"] == "legacy" and legacy["selection_segment"] == "calibration"
    assert legacy["segments"]["select"] == legacy["segments"]["calibration"]
    assert "calibrator" not in legacy
    ledger = ExperimentLedger(tmp_path / "experiments.json", dataset_version="ds1")
    run_meta_labeling(ds, pred, ledger=ledger)
    run_meta_labeling(ds, pred, ledger=ledger, split_mode="calibrate_select")
    # two identities: the split mode is part of the look's identity
    assert ledger.total_experiments == 2
    modes = {e["config"]["split_mode"] for e in ledger.entries}
    assert modes == {"legacy", "calibrate_select"}
    for e in ledger.entries:
        assert e["kind"] == "metalabel_tau" and "segments" in e["result"]
        assert e["config"]["selection"].endswith(
            "calibration segment" if e["config"]["split_mode"] == "legacy" else "select segment"
        )
    # a rerun is a rerun
    run_meta_labeling(ds, pred, ledger=ledger, split_mode="calibrate_select")
    assert ledger.total_experiments == 2


# ------------------------------------------------------- D3 conformal hook


def test_conformal_hook_maps_width_to_size():
    rng = np.random.default_rng(0)
    y_cal = rng.normal(0.0, 1e-3, 400)
    cal = SplitConformal(alpha=0.1).fit(y_cal, np.zeros(400))
    assert cal.n_cal == 400 and cal.cal_median_width == pytest.approx(2.0 * cal.q)
    # absolute score: a constant width equal to the reference -> full size
    assert conformal_sizing_hook(cal)(_row()) == pytest.approx(1.0)
    # a tighter reference shrinks size as ref / width
    hook = conformal_sizing_hook(cal, ref_width=cal.q)
    assert hook(_row()) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        conformal_sizing_hook(cal, ref_width=0.0)
    with pytest.raises(RuntimeError):
        conformal_sizing_hook(SplitConformal(alpha=0.1))
    # normalised score: the width follows the row's sigma column
    sig_cal = 0.5 + np.abs(rng.normal(size=400))
    ncal = SplitConformal(alpha=0.1, score="normalized").fit(
        y_cal * sig_cal, np.zeros(400), sig_cal
    )
    with pytest.raises(ValueError):
        conformal_sizing_hook(ncal)
    nhook = conformal_sizing_hook(ncal, ref_width=2.0 * ncal.q, sigma_column="sigma")
    assert nhook(_row(sigma=1.0)) == pytest.approx(1.0)
    assert nhook(_row(sigma=4.0)) == pytest.approx(0.25)
    assert nhook(_row(sigma=0.25)) == 1.0
    assert nhook(_row()) == 0.0 and nhook(_row(sigma=float("nan"))) == 0.0


def test_conformal_hook_through_the_backtester_shrinks_positions():
    rng = np.random.default_rng(1)
    cal = SplitConformal(alpha=0.1).fit(rng.normal(0.0, 1e-3, 500), np.zeros(500))
    base = _run()
    sigma = np.full(60, 3.0)
    res = _run(
        conformal_sizing_hook(cal, ref_width=cal.cal_median_width / 2.0), _scores(sigma=sigma)
    )
    assert res.sizing_multipliers is not None
    assert np.allclose(res.positions, 0.5 * base.positions)


# --------------------------------------------------- D3 coverage gate


def _intervals(n_days=12, per_day=300, seed=5):
    rng = np.random.default_rng(seed)
    ts = np.concatenate(
        [d * NS_DAY + np.arange(per_day, dtype=np.int64) * NS_S for d in range(n_days)]
    )
    n = ts.size
    regime = np.where(np.arange(n) % 2 == 0, "low", "high")
    cal = SplitConformal(alpha=0.1).fit(rng.normal(size=2000), np.zeros(2000))
    pred = np.zeros(n)
    y = rng.normal(size=n)
    # the "high" regime has residuals three times wider than the calibration
    y[regime == "high"] *= 3.0
    lo, hi = cal.interval(pred)
    return ts, y, lo, hi, regime


def test_coverage_by_regime_reports_and_flags_but_does_not_enforce(tmp_path):
    ts, y, lo, hi, regime = _intervals()
    out = coverage_by_regime(ts, y, lo, hi, regime, alpha=0.1)
    assert out["enforced"] is False and out["passed"] is None
    assert out["nominal"] == pytest.approx(0.9)
    low, high = out["by_regime"]["low"], out["by_regime"]["high"]
    assert low["n_days"] == high["n_days"] == 12
    assert abs(low["gap"]) < 0.05 and low["under_coverage"] is False
    assert high["coverage"] < 0.8 and high["under_coverage"] is True
    assert high["tstat_day_cluster"] < -2.0 and np.isfinite(low["tstat_day_cluster"])
    assert high["inference_df"] == 11.0 and high["inference_p_student_t"] < 0.01
    assert out["under_covered_regimes"] == ["high"]
    assert out["overall"]["n_pairs"] == ts.size
    # the same read, enforced, fails; with one regime only it passes
    assert coverage_by_regime(ts, y, lo, hi, regime, alpha=0.1, enforce=True)["passed"] is False
    m = regime == "low"
    ok = coverage_by_regime(ts[m], y[m], lo[m], hi[m], regime[m], alpha=0.1, enforce=True)
    assert ok["passed"] is True and ok["under_covered_regimes"] == []
    # a single day: the day-cluster t is undefined and nothing is flagged
    one = ts < NS_DAY
    single = coverage_by_regime(ts[one], y[one], lo[one], hi[one], regime[one], alpha=0.1)
    assert np.isnan(single["by_regime"]["high"]["tstat_day_cluster"])
    assert single["by_regime"]["high"]["under_coverage"] is False
    # the gate read is one ledger look, de-duplicated on rerun
    ledger = ExperimentLedger(tmp_path / "experiments.json", dataset_version="ds1")
    first = coverage_by_regime(ts, y, lo, hi, regime, alpha=0.1, ledger=ledger, alpha_id="EQ03")
    coverage_by_regime(ts, y, lo, hi, regime, alpha=0.1, ledger=ledger, alpha_id="EQ03")
    assert first["ledger_n"] == 1 and ledger.total_experiments == 1
    (entry,) = ledger.entries
    assert entry["kind"] == "conformal_coverage_gate" and entry["reruns"] == 1
    assert entry["result"]["under_covered_regimes"] == ["high"]
    assert entry["result"]["passed"] is None and entry["config"]["enforce"] is False
    # JSON-serialisable
    json.dumps(out)
    with pytest.raises(ValueError):
        coverage_by_regime(ts, y, lo, hi, regime[:-1], alpha=0.1)
    with pytest.raises(ValueError):
        coverage_by_regime(ts, y, lo, hi, regime, alpha=1.0)


# ------------------------------------------------------- D3 MVP opt-in

TINY_CONFIG = CONFIGS_DIR / "mvp" / "mvp_tiny.json"


def test_mvp_conformal_block_is_optional_and_parsed():
    cfg = load_config(TINY_CONFIG)
    assert cfg.portfolio.conformal is None
    on = cfg.with_overrides(conformal={"alpha": 0.2, "method": "split", "window": 50})
    assert on.portfolio.conformal == ConformalSpec(alpha=0.2, method="split", window=50)
    assert on.config_version() != cfg.config_version()
    off = cfg.with_overrides(conformal={"enabled": False, "alpha": 0.2})
    assert off.portfolio.conformal is None
    for bad in ({"method": "oracle"}, {"alpha": 0.0}, {"ref_width": -1.0}, {"window": 1}):
        with pytest.raises(ValueError):
            cfg.with_overrides(conformal=bad)


class _Stub(SizingMixin):
    """The mixin over a minimal engine: ``cfg`` is all it reads."""

    def __init__(self, cfg: MvpConfig) -> None:
        self.cfg = cfg
        self._init_conformal()


def test_mvp_conformal_scaling_is_identity_off_and_shrinks_wide_intervals_on():
    cfg = load_config(TINY_CONFIG)
    off = _Stub(cfg)
    assert off.conformal_sizing["enabled"] is False and off._conformal is None
    assert off._conformal_scale(400, 1e-3) == 400
    off._conformal_update(0.01)  # nothing to update; no error
    assert off.conformal_sizing["updates"] == 0

    on = _Stub(cfg.with_overrides(conformal={"method": "split", "window": 100, "warmup": 20}))
    assert on.conformal_sizing["enabled"] is True
    rng = np.random.default_rng(2)
    # warm-up: targets pass unscaled until ``warmup`` residuals are in
    for _ in range(19):
        assert on._conformal_scale(400, 1e-4) == 400
        on._conformal_update(float(rng.normal(0.0, 1e-3)))
    assert on.conformal_sizing["updates"] == 19
    assert on._conformal_scale(400, 1e-4) == 400  # the 20th residual not yet in
    on._conformal_update(float(rng.normal(0.0, 1e-3)))
    # calm residuals: the current width is near the median width -> near full size
    sized_calm = on._conformal_scale(400, 1e-4)
    assert 0 < sized_calm <= 400
    assert on.conformal_sizing["ref_width"] is not None and on.conformal_sizing["ref_width"] > 0
    # a burst of wide residuals widens the interval -> the size shrinks
    for _ in range(60):
        on._conformal_update(float(rng.normal(0.0, 1e-2)))
    sized_wide = on._conformal_scale(400, 1e-4)
    assert sized_wide < sized_calm
    assert on.conformal_sizing["decisions_scaled"] >= 1
    assert 0.0 < on.conformal_sizing["min_multiplier"] < 1.0
    # sign preserved, never above the unscaled target, zero stays zero
    assert on._conformal_scale(-400, -1e-4) == -sized_wide
    assert on._conformal_scale(0, 1e-4) == 0


def test_mvp_golden_config_has_no_conformal_block():
    for name in ("mvp.json", "mvp_tiny.json"):
        doc = json.loads((CONFIGS_DIR / "mvp" / name).read_text(encoding="utf-8"))
        assert "conformal" not in doc["portfolio"]
