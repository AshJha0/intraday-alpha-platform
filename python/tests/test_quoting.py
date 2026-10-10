"""v1.10 M5: signal-skewed two-sided quoting on synthetic MBO data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.backtest.quoting import (
    PNL_PARTS,
    QuotingBacktester,
    QuotingConfig,
    sharpe_per_day,
)
from iap.execution.calibration import ExecCalibration, estimate_calibration
from test_maker_economics import INS, TICK, exec_config, synth_events


def mid_series(events):
    """Mid after each event (simple book reconstruction via the simulator)."""
    from iap.execution import ExecutionSimulator

    sim = ExecutionSimulator(exec_config())
    out_t, out_m = [], []
    for e in events:
        sim.on_event(e)
        b = sim.venue_book(INS, 1)
        bb, ba = b.best_bid(), b.best_ask()
        if bb and ba:
            out_t.append(e.exchange_ts)
            out_m.append(0.5 * (bb[0] + ba[0]) * TICK)
    return np.array(out_t), np.array(out_m)


def make_scores(events, every=10, skill=1.0, horizon_ns=2_000_000_000, seed=3):
    """er = skill * (future mid return over horizon) + noise; skill=0 is noise."""
    rng = np.random.default_rng(seed)
    mt, mm = mid_series(events)
    ts = np.array([e.exchange_ts for e in events[60::every]], dtype=np.int64)
    i0 = np.clip(np.searchsorted(mt, ts, side="right") - 1, 0, len(mm) - 1)
    i1 = np.clip(np.searchsorted(mt, ts + horizon_ns, side="right") - 1, 0, len(mm) - 1)
    fwd = mm[i1] / mm[i0] - 1.0
    er = skill * fwd + 2e-5 * rng.standard_normal(ts.size)
    return pd.DataFrame({"exchange_ts": ts, "expected_return": er, "confidence": 1.0})


@pytest.fixture(scope="module")
def events():
    return synth_events(n_steps=2500, seed=11)


def _identity(res):
    p = res.pnl
    assert p["gross"] == pytest.approx(sum(p[k] for k in PNL_PARTS), abs=1e-6)
    assert p["net"] == pytest.approx(
        p["gross"] + p["rebates"] - p["taker_fees"] - p["impact"], abs=1e-9
    )


def test_identity_limits_and_flatten(events):
    cfg = QuotingConfig(qty=100, max_inventory=300)
    res = QuotingBacktester(exec_config(), cfg).run_instrument(events, make_scores(events), INS)
    _identity(res)
    f = res.fills
    assert (~f["flatten"]).sum() > 10
    d = np.where(f["side"] == 0, 1, -1) * f["qty"]
    assert d.sum() == 0  # flattened
    assert np.abs(np.cumsum(d)).max() <= 300  # hard limit at every fill
    s = res.summary()
    assert s["max_abs_inventory"] <= 300
    assert 0 < s["bid_fill_rate"] <= 1 and 0 < s["ask_fill_rate"] <= 1
    # maker quote fills earn the venue rebate
    mk = f[f["liquidity"] == "MAKER"]
    np.testing.assert_allclose(mk["fee"], -0.002 * mk["qty"])
    assert s["as_source"] == "none"


def test_deterministic(events):
    sc = make_scores(events)
    a = QuotingBacktester(exec_config()).run_instrument(events, sc, INS)
    b = QuotingBacktester(exec_config()).run_instrument(events, sc, INS)
    assert a.pnl == b.pnl
    pd.testing.assert_frame_equal(a.fills, b.fills)


def test_tight_limit_blocks_side(events):
    cfg = QuotingConfig(qty=100, max_inventory=100)
    res = QuotingBacktester(exec_config(), cfg).run_instrument(events, make_scores(events), INS)
    _identity(res)
    d = np.where(res.fills["side"] == 0, 1, -1) * res.fills["qty"]
    assert np.abs(np.cumsum(d)).max() <= 100
    c = res.counters
    assert c["bid_blocked_by_limit"] + c["ask_blocked_by_limit"] > 0


def test_inventory_skew_mean_reverts(events):
    sc = make_scores(events, skill=0.0)
    lo = QuotingBacktester(exec_config(), QuotingConfig(gamma=1e-3, alpha_weight=0))
    hi = QuotingBacktester(exec_config(), QuotingConfig(gamma=50.0, k=5000.0, alpha_weight=0))
    a = lo.run_instrument(events, sc, INS).summary()
    b = hi.run_instrument(events, sc, INS).summary()
    assert b["mean_abs_inventory"] <= a["mean_abs_inventory"]


def test_calibrated_as_floor_widens_quotes(events):
    doc = estimate_calibration(events, {INS: TICK})
    cal = ExecCalibration.from_dict(doc)
    sc = make_scores(events)
    base = QuotingBacktester(exec_config(), QuotingConfig(as_floor=False), cal)
    wide = QuotingBacktester(exec_config(), QuotingConfig(adverse_selection_bps=50.0))
    r0 = base.run_instrument(events, sc, INS)
    r1 = wide.run_instrument(events, sc, INS)
    assert r0.as_source.startswith("calibration:")
    assert r1.as_source == "config"
    assert len(r1.fills) < len(r0.fills)
    _identity(r0)
    _identity(r1)


def test_alpha_skew_beats_no_skew_with_informative_signal():
    days = {}
    for d in range(4):
        evs = synth_events(n_steps=2000, seed=100 + d, drift=0.1 if d % 2 else -0.1)
        days[f"d{d}"] = (evs, make_scores(evs, skill=1.0, seed=d))
    skew = QuotingBacktester(exec_config(), QuotingConfig(alpha_weight=1.0)).run_days(days, INS)
    flat = QuotingBacktester(exec_config(), QuotingConfig(alpha_weight=0.0)).run_days(days, INS)
    assert skew["markout"].sum() > flat["markout"].sum()
    assert skew["net"].sum() > flat["net"].sum()
    assert sharpe_per_day(skew["net"]) is not None


def test_config_validation():
    with pytest.raises(ValueError):
        QuotingConfig(qty=100, max_inventory=50)
    with pytest.raises(ValueError):
        QuotingConfig(gamma=0)
    assert sharpe_per_day([1.0]) is None
