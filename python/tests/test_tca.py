"""TCA metrics: Perold identity, benchmarks, impact, adverse selection (§19)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.tca.fills import Fill, MarketTimeline, ParentOrder
from iap.tca.simulator import bundled_order_set
from iap.tca.tca import (
    adverse_selection,
    arrival_slippage_bps,
    impact_regression,
    interval_twap,
    interval_vwap,
    order_tca,
    perold_decomposition,
    spread_and_impact_cost,
)


def _mini_timeline() -> MarketTimeline:
    tl = MarketTimeline()
    #                ts,   bid,    ask,  bid_sz, ask_sz
    tl.append(1_000_000_000, 99.98, 100.02, 500, 400)
    tl.append(2_000_000_000, 99.99, 100.03, 300, 200)
    tl.append(4_000_000_000, 100.01, 100.05, 600, 100)
    tl.append(10_000_000_000, 100.06, 100.10, 200, 300)
    tl.add_trade(3_000_000_000, 100.00, 100)
    tl.add_trade(5_000_000_000, 100.04, 300)
    return tl


def test_perold_hand_case_buy():
    # buy 1000, fills 600@100.05 after arrival mid 100.01 (decision 100.00),
    # end mid 100.20
    dec = perold_decomposition(1, 1000, [(100.05, 600)],
                               100.00, 100.01, 100.20)
    assert abs(dec["delay_cost"] - 600 * 0.01) < 1e-9
    assert abs(dec["trading_cost"] - 600 * 0.04) < 1e-9
    assert abs(dec["opportunity_cost"] - 400 * 0.20) < 1e-9
    assert abs(dec["total_is"] - (6.0 + 24.0 + 80.0)) < 1e-9
    assert abs(dec["total_is_bps"] - 1e4 * 110.0 / 100000.0) < 1e-9
    assert dec["fill_rate"] == 0.6


def test_perold_hand_case_sell():
    # sell 500, fully filled at 99.95 with arrival mid 100.00,
    # decision 100.02: delay = -1*500*(100.00-100.02) = +10 (cost: price
    # fell before we arrived); trading = -1*500*(99.95-100.00) = +25
    dec = perold_decomposition(-1, 500, [(99.95, 500)],
                               100.02, 100.00, 99.80)
    assert abs(dec["delay_cost"] - 10.0) < 1e-9
    assert abs(dec["trading_cost"] - 25.0) < 1e-9
    assert dec["opportunity_cost"] == 0.0
    assert abs(dec["total_is"] - 35.0) < 1e-9


def test_perold_identity_components_sum_to_total():
    """delay + trading + opportunity == total, exactly (1e-9)."""
    for iid, (tl, orders, _) in bundled_order_set().items():
        for o in orders:
            r = order_tca(o, tl)["perold"]
            assert abs(r["delay_cost"] + r["trading_cost"]
                       + r["opportunity_cost"] - r["total_is"]) < 1e-9
            assert abs(r["delay_bps"] + r["trading_bps"]
                       + r["opportunity_bps"] - r["total_is_bps"]) < 1e-9


def test_perold_validation():
    with pytest.raises(ValueError):
        perold_decomposition(0, 100, [], 100.0, 100.0, 100.0)
    with pytest.raises(ValueError):
        perold_decomposition(1, 0, [], 100.0, 100.0, 100.0)
    with pytest.raises(ValueError):
        perold_decomposition(1, 100, [(100.0, 200)], 100.0, 100.0, 100.0)
    with pytest.raises(ValueError):
        perold_decomposition(1, 100, [], 0.0, 100.0, 100.0)


def test_interval_vwap_and_twap_hand():
    tl = _mini_timeline()
    # trades in [1s, 6s]: 100@100.00 + 300@100.04 -> vwap = 100.03
    vwap = interval_vwap(tl, 1_000_000_000, 6_000_000_000)
    assert abs(vwap - (100.00 * 100 + 100.04 * 300) / 400) < 1e-12
    assert interval_vwap(tl, 6_000_000_000, 9_000_000_000) is None
    # twap over [2s, 4s): mid state 2s..4s is 100.01 -> exactly 100.01
    twap = interval_twap(tl, 2_000_000_000, 4_000_000_000)
    assert abs(twap - 100.01) < 1e-12
    # twap over [1s, 3s]: 1s of mid 100.00 + 1s of mid 100.01
    twap2 = interval_twap(tl, 1_000_000_000, 3_000_000_000)
    assert abs(twap2 - 100.005) < 1e-12
    with pytest.raises(ValueError):
        interval_twap(tl, 5, 5)


def test_prevailing_no_lookahead():
    tl = _mini_timeline()
    assert tl.prevailing(999_999_999) is None       # before first state
    assert tl.prevailing(1_000_000_000) == 0        # inclusive
    assert tl.prevailing(3_999_999_999) == 1        # not yet the 4s state
    assert tl.prevailing(4_000_000_000) == 2
    assert np.isnan(tl.mid_at(0))


def test_spread_and_impact_split_hand():
    order = ParentOrder(order_id=1, instrument_id=1, side=0, qty_target=200,
                        decision_ts=1, arrival_ts=1, end_ts=2)
    # buy fill at mid+0.03 when half-spread was 0.02: spread 0.02, impact 0.01
    order.fills.append(Fill(ts=1, price=100.03, qty=200, mid_at_fill=100.00,
                            half_spread_at_fill=0.02, opp_depth_at_fill=100))
    split = spread_and_impact_cost(order)
    assert abs(split["spread_cost"] - 200 * 0.02) < 1e-12
    assert abs(split["impact_cost"] - 200 * 0.01) < 1e-12
    assert abs(split["exec_cost_vs_mid"] - 200 * 0.03) < 1e-12


def test_adverse_selection_hand():
    tl = _mini_timeline()
    order = ParentOrder(order_id=1, instrument_id=1, side=0, qty_target=100,
                        decision_ts=1_000_000_000,
                        arrival_ts=2_000_000_000, end_ts=9_000_000_000)
    order.fills.append(Fill(ts=2_000_000_000, price=100.03, qty=100,
                            mid_at_fill=100.01, half_spread_at_fill=0.02,
                            opp_depth_at_fill=200))
    adv = adverse_selection(order, tl)
    # +100ms: still state@2s (mid 100.01) -> (100.01-100.03)/100.03
    assert abs(adv["100ms"] - 1e4 * (100.01 - 100.03) / 100.03) < 1e-9
    # +10s = 12s: state@10s mid = 100.08
    assert abs(adv["10s"] - 1e4 * (100.08 - 100.03) / 100.03) < 1e-9


def test_arrival_slippage_hand():
    order = ParentOrder(order_id=1, instrument_id=1, side=1, qty_target=100,
                        decision_ts=1, arrival_ts=1, end_ts=2)
    assert arrival_slippage_bps(order, 100.0) is None  # unfilled
    order.fills.append(Fill(ts=1, price=99.95, qty=100, mid_at_fill=100.0,
                            half_spread_at_fill=0.02, opp_depth_at_fill=10))
    # sell below arrival mid -> positive slippage (cost)
    assert abs(arrival_slippage_bps(order, 100.0) - 5.0) < 1e-9


def test_impact_regression_recovers_exact_slope():
    part = [0.1, 0.2, 0.3, 0.4]
    cost = [2.0 + 30.0 * x for x in part]
    reg = impact_regression(part, cost)
    assert abs(reg["slope_bps_per_participation"] - 30.0) < 1e-9
    assert abs(reg["intercept_bps"] - 2.0) < 1e-9
    assert abs(reg["r2"] - 1.0) < 1e-12
    with pytest.raises(ValueError):
        impact_regression([0.1], [1.0])


def test_timeline_validation():
    tl = MarketTimeline()
    tl.append(10, 99.0, 101.0, 1, 1)
    with pytest.raises(ValueError):
        tl.append(5, 99.0, 101.0, 1, 1)  # time regression
    with pytest.raises(ValueError):
        tl.append(20, 101.0, 99.0, 1, 1)  # crossed


def test_simulator_deterministic():
    a = bundled_order_set()
    b = bundled_order_set()
    assert sorted(a) == sorted(b) == [1, 101]
    for iid in a:
        oa, ob = a[iid][1], b[iid][1]
        assert len(oa) == len(ob)
        for x, y in zip(oa, ob):
            assert x.order_id == y.order_id
            assert x.qty_target == y.qty_target
            assert x.decision_ts == y.decision_ts
            assert [(f.ts, f.price, f.qty) for f in x.fills] == \
                   [(f.ts, f.price, f.qty) for f in y.fills]


def test_simulated_orders_are_sane():
    for iid, (tl, orders, tick) in bundled_order_set().items():
        for o in orders:
            assert 0 < o.qty_filled <= o.qty_target or o.qty_filled == 0
            assert o.decision_ts < o.arrival_ts < o.end_ts
            for f in o.fills:
                assert o.arrival_ts <= f.ts <= o.end_ts
                assert f.qty > 0 and f.price > 0
                # marketable fills never improve on the touch
                if o.side == 0:
                    assert f.price >= f.mid_at_fill
                else:
                    assert f.price <= f.mid_at_fill
