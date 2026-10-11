"""v1.14 execution realism (quality review X-P1-1, X-P1-6, X-P2-7, X-P2-9, X-P2-12).

All synthetic. X-P1-1 (MODIFY size-decrease queue credit) is a pinned
simulator rule in all three simulator languages; every other option is
default-off and pinned here as such.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.backtest.maker import MakerBacktester, MakerConfig
from iap.backtest.quoting import PNL_PARTS, QuotingBacktester, QuotingConfig
from iap.core.events import EventType
from iap.execution import ExecutionSimulator, OrderState, OrderType
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.execution.markout_feedback import FeedbackConfig
from test_execution_rules import LAT, T0, Feeder, child, make_config, seed_book
from test_maker_economics import INS, TICK, _check_identity, exec_config, make_scores, synth_events
from test_quoting import make_scores as quote_scores


@pytest.fixture(scope="module")
def events():
    return synth_events(n_steps=1500)


def _rest_bid(sim, f, qty=50):
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, qty, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    return oid


# ------------------------------------------------------- X-P1-1 MODIFY credit


def test_modify_decrease_ahead_credits_and_behind_does_not():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    oid = _rest_bid(sim, f)
    sim.on_event(f.modify(T0 + 2_000_000, 0, 100, 120, 11))  # 300 -> 120, ahead of us
    assert sim.orders[oid].ahead_qty == 120
    sim.on_event(f.add(T0 + 2_100_000, 0, 100, 400, 13))  # joins behind us
    sim.on_event(f.modify(T0 + 2_200_000, 0, 100, 100, 13))  # behind: no credit
    assert sim.orders[oid].ahead_qty == 120
    # a size increase re-queues order 11 behind us; its later decrease is no credit
    sim.on_event(f.modify(T0 + 2_300_000, 0, 100, 500, 11))
    sim.on_event(f.modify(T0 + 2_400_000, 0, 100, 50, 11))
    assert sim.orders[oid].ahead_qty == 120
    assert sim.fills == []


# ------------------------------------------------------------- X-P2-7 hidden


def _hidden(on: bool):
    sim = ExecutionSimulator(make_config(), hidden_prints=on)
    f = Feeder()
    oid = _rest_bid(sim, f)
    # displayed execution: EXECUTE then its TRADE print at the same ts
    sim.on_event(f.exec(T0 + 2_000_000, 0, 100, 100, 11))
    sim.on_event(f.ev(T0 + 2_000_000, EventType.TRADE, 1, 100, 100, 0, 7))
    # a hidden (non-displayed) print: a TRADE with no EXECUTE before it
    sim.on_event(f.ev(T0 + 3_000_000, EventType.TRADE, 1, 100, 230, 0, 8))
    return sim, oid


def test_hidden_prints_are_opt_in_and_never_double_count_a_displayed_print():
    off, oid = _hidden(False)
    assert off.orders[oid].ahead_qty == 200
    assert off.fills == [] and off.hidden_print_trades == 0
    on, oid = _hidden(True)
    assert on.hidden_print_trades == 1  # the EXECUTE's own print is not hidden
    assert on.orders[oid].ahead_qty == 0
    assert [(x.qty, x.price_ticks) for x in on.fills] == [(30, 100)]


# ------------------------------------------------------- X-P2-12 amend_down


def test_amend_down_keeps_priority_after_latency():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    oid = _rest_bid(sim, f, qty=80)
    assert sim.amend_down(oid, 30, T0 + 1_000_000)
    assert sim.amend_target(oid) == 30
    assert not sim.amend_down(oid, 30, T0 + 1_000_000)  # not below the target
    sim.on_event(f.heartbeat(T0 + 1_000_000 + LAT - 1))
    assert sim.orders[oid].remaining == 80  # still in flight
    sim.on_event(f.heartbeat(T0 + 1_000_000 + LAT))
    o = sim.orders[oid]
    assert (o.remaining, o.ahead_qty, o.state) == (30, 300, OrderState.ACTIVE)
    assert sim.amends_applied == 1
    sim.on_event(f.exec(T0 + 3_000_000, 0, 100, 300, 11))
    sim.on_event(f.exec(T0 + 3_000_001, 0, 99, 1, 12))  # trade-through: we fill
    assert sum(x.qty for x in sim.fills) == 1
    with pytest.raises(ValueError):
        sim.amend_down(oid, 0, T0)
    sim.cancel(oid, T0 + 4_000_000)
    assert not sim.amend_down(oid, 10, T0 + 4_000_000)  # cancel in flight


# ------------------------------------------------- X-P1-6 maker feed / exits


def test_maker_v114_defaults_are_the_v113_run(events):
    sc = make_scores(events)
    base = MakerBacktester(exec_config(), MakerConfig(margin_bps=-1e9))
    expl = MakerBacktester(
        exec_config(),
        MakerConfig(
            margin_bps=-1e9, feed_latency_ns=0, exit_route="book", hidden_print_fills=False
        ),
    )
    a = base.run_instrument(events, sc, INS)
    b = expl.run_instrument(events, sc, INS)
    pd.testing.assert_frame_equal(a.trips, b.trips)
    assert a.counters == b.counters
    assert "exit_market_orders" not in a.counters


@pytest.mark.parametrize("exit_mode", ["taker", "passive"])
def test_maker_routed_exits_go_through_the_simulator(events, exit_mode):
    sc = make_scores(events)
    mc = MakerConfig(margin_bps=-1e9, exit=exit_mode, exit_timeout_ns=10**8, exit_route="sim")
    res = MakerBacktester(exec_config(), mc).run_instrument(events, sc, INS)
    t = res.trips
    assert len(t) > 5
    _check_identity(t)
    assert res.counters["exit_market_orders"] > 0
    crossed = t[~t["forced"] & (t["exit_passive_qty"] < t["qty"])]
    assert len(crossed) > 0
    # MARKET exits arrive after the latency path, so the exit is later than
    # the hold horizon and pays the simulator's per-fill taker fee and impact
    assert (crossed["exit_ts"] > crossed["entry_ts"]).all()
    assert (crossed["exit_impact"] > 0).all()
    rem = crossed["qty"] - crossed["exit_passive_qty"]
    np.testing.assert_allclose(
        crossed["exit_fee"], -0.002 * crossed["exit_passive_qty"] + 0.003 * rem, atol=1e-9
    )


def test_maker_feed_latency_delays_every_decision(events):
    sc = make_scores(events)
    plain = MakerBacktester(exec_config(), MakerConfig(margin_bps=-1e9))
    slow = MakerBacktester(exec_config(), MakerConfig(margin_bps=-1e9, feed_latency_ns=50_000_000))
    a = plain.run_instrument(events, sc, INS)
    b = slow.run_instrument(events, sc, INS)
    _check_identity(b.trips)
    assert not a.trips.equals(b.trips)
    again = slow.run_instrument(events, sc, INS)
    pd.testing.assert_frame_equal(b.trips, again.trips)
    with pytest.raises(ValueError):
        MakerConfig(feed_latency_ns=-1)
    with pytest.raises(ValueError):
        MakerConfig(exit_route="dark")


# ------------------------------------------------------------------- quoting


def _q_identity(res):
    p = res.pnl
    assert p["gross"] == pytest.approx(sum(p[k] for k in PNL_PARTS), abs=1e-6)
    assert p["net"] == pytest.approx(
        p["gross"] + p["rebates"] - p["taker_fees"] - p["impact"], abs=1e-9
    )


def test_quoting_v114_options(events):
    sc = quote_scores(events)
    base = QuotingBacktester(exec_config(), QuotingConfig(qty=100, max_inventory=300))
    a = base.run_instrument(events, sc, INS)
    assert "amends" not in a.counters and "flatten_market_orders" not in a.counters
    for kw in (
        {"flatten_route": "sim"},
        {
            "amend_size_down": True,
            "feedback": FeedbackConfig(
                mode="reduce", threshold_bps=-1e9, min_fills=1, horizon_ns=10**8
            ),
        },
        {"feed_latency_ns": 20_000_000},
        {"hidden_print_fills": True},
    ):
        cfg = QuotingConfig(qty=100, max_inventory=250, **kw)
        res = QuotingBacktester(exec_config(), cfg).run_instrument(events, sc, INS)
        _q_identity(res)
        f = res.fills
        d = np.where(f["side"] == 0, 1, -1) * f["qty"]
        assert d.sum() == 0, kw  # flattened
        if "flatten_route" in kw:
            assert res.counters["flatten_market_orders"] >= 1
            assert (f.loc[f["flatten"], "liquidity"] == "TAKER").all()
        if "amend_size_down" in kw:
            assert res.counters["amends"] > 0
    with pytest.raises(ValueError):
        QuotingConfig(flatten_route="x")


# ---------------------------------------------------- X-P2-9 calibration AS


def test_calibration_markout_per_touch_order_is_opt_in(events):
    plain = estimate_calibration(events, {INS: TICK})
    assert "unit" not in plain["markouts"]
    per = estimate_calibration(events, {INS: TICK}, markout_unit="touch_order")
    assert per["markouts"]["unit"] == "touch_order"
    for h, cell in per["markouts"]["all"].items():
        assert 0 < cell["n"] <= plain["markouts"]["all"][h]["n"]
    assert per["markouts"]["all"]["1s"]["n"] < plain["markouts"]["all"]["1s"]["n"]
    ExecCalibration.from_dict(per)
    with pytest.raises(ValueError):
        estimate_calibration(events, {INS: TICK}, markout_unit="trade")
