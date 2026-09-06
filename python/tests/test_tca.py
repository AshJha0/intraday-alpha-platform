"""TCA metrics: Perold identity, benchmarks, impact, adverse selection (§19)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.tca.fills import Fill, MarketTimeline, ParentOrder
from iap.tca.simulator import bundled_order_set
from iap.tca.tca import (
    adverse_selection_with_counts,
    validate_order_window,
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
    # +10s = 12s is BEYOND the timeline end (10s): undefined, never the
    # stale last mid (pinned §2.5)
    assert adv["10s"] is None
    _, n = adverse_selection_with_counts(order, tl)
    assert n == {"100ms": 1, "1s": 1, "10s": 0}
    # extend the timeline to 12s: the 10s markout becomes defined
    tl.append(12_000_000_000, 100.07, 100.09, 100, 100)
    adv = adverse_selection(order, tl)
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


# ------------------------------------------------------ round-3 scenarios


def test_tca_markout_undefined_past_timeline_end():
    """A fill at the last state: every markout undefined (n_defined 0); a
    fill 20 s before the end: only the 10 s markout is defined (pinned §2.5)."""
    tl = MarketTimeline()
    t0 = 1_000_000_000_000
    for k in range(0, 61):
        tl.append(t0 + k * 1_000_000_000, 99.99, 100.01, 100, 100)
    last = ParentOrder(order_id=1, instrument_id=1, side=0, qty_target=100,
                       decision_ts=t0, arrival_ts=t0, end_ts=tl.last_ts)
    last.fills.append(Fill(ts=tl.last_ts, price=100.01, qty=100,
                           mid_at_fill=100.0, half_spread_at_fill=0.01,
                           opp_depth_at_fill=100))
    adv, n = adverse_selection_with_counts(last, tl)
    assert adv == {"100ms": None, "1s": None, "10s": None}
    assert n == {"100ms": 0, "1s": 0, "10s": 0}
    rec = order_tca(last, tl)
    assert rec["adverse_selection_bps"]["10s"] is None
    assert rec["adverse_selection_n"] == {"100ms": 0, "1s": 0, "10s": 0}
    # 20 s before the end: 100ms / 1s / 10s all inside -> all defined; a
    # fill 5 s before the end: 10 s undefined, the others defined
    early = ParentOrder(order_id=2, instrument_id=1, side=0, qty_target=100,
                        decision_ts=t0, arrival_ts=t0, end_ts=tl.last_ts)
    early.fills.append(Fill(ts=tl.last_ts - 5_000_000_000, price=100.01,
                            qty=100, mid_at_fill=100.0,
                            half_spread_at_fill=0.01, opp_depth_at_fill=100))
    adv, n = adverse_selection_with_counts(early, tl)
    assert n == {"100ms": 1, "1s": 1, "10s": 0}
    assert adv["10s"] is None and adv["1s"] is not None
    # a HALT inside (t_f, t_f + 10s] makes the 10 s markout undefined too
    tl2 = MarketTimeline()
    for k in range(0, 61):
        tl2.append(t0 + k * 1_000_000_000, 99.99, 100.01, 100, 100)
    tl2.add_halt(t0 + 3_000_000_000)
    halted = ParentOrder(order_id=3, instrument_id=1, side=0, qty_target=100,
                         decision_ts=t0, arrival_ts=t0, end_ts=tl2.last_ts)
    halted.fills.append(Fill(ts=t0, price=100.01, qty=100, mid_at_fill=100.0,
                             half_spread_at_fill=0.01, opp_depth_at_fill=100))
    _, n = adverse_selection_with_counts(halted, tl2)
    assert n == {"100ms": 1, "1s": 1, "10s": 0}


def test_tca_passive_fill_uses_pre_event_mid():
    """MAKER fill by a trade-through: reference state strictly before the
    event -> spread cost == -q*hs (we provided liquidity), impact 0."""
    from iap.tca.fills import MAKER, TAKER, stamp_fill
    tl = MarketTimeline()
    t0 = 1_000_000_000_000
    tl.append(t0, 99.98, 100.02, 500, 400)        # hs 0.02, mid 100.00
    # a sell sweeps through our 99.98 bid: post-event state 99.90 / 100.02
    tl.append(t0 + 1_000, 99.90, 100.02, 500, 400)  # mid 99.96
    f = stamp_fill(tl, t0 + 1_000, 99.98, 100, side=0, liquidity=MAKER)
    assert abs(f.mid_at_fill - 100.00) < 1e-9
    assert abs(f.half_spread_at_fill - 0.02) < 1e-9
    order = ParentOrder(order_id=1, instrument_id=1, side=0, qty_target=100,
                        decision_ts=t0, arrival_ts=t0, end_ts=t0 + 1_000)
    order.fills.append(f)
    split = spread_and_impact_cost(order)
    assert abs(split["spread_cost"] - 100 * 0.02) < 1e-12
    assert abs(split["exec_cost_vs_mid"] - (-100 * 0.02)) < 1e-12
    assert abs(split["impact_cost"] - (-100 * 0.04)) < 1e-12  # -2 * q * hs
    # the same fill stamped as TAKER (post-event state) would read as cost
    g = stamp_fill(tl, t0 + 1_000, 99.98, 100, side=0, liquidity=TAKER)
    assert abs(g.mid_at_fill - 99.96) < 1e-9
    with pytest.raises(ValueError):
        stamp_fill(tl, t0, 99.98, 100, side=0, liquidity=MAKER)  # no prior state
    with pytest.raises(ValueError):
        stamp_fill(tl, t0, 99.98, 100, side=0, liquidity="ODD")


def test_tca_locked_and_crossed_states_pinned(tmp_path):
    """Timeline builder: crossed consolidated states skipped + counted,
    locked states kept with half-spread 0 (pinned §2.1)."""
    import json
    from iap.tca.simulator import build_timeline
    t0 = 1_000_000_000_000
    rows = []
    seq = {1: 0, 2: 0}

    def add(vid, side, px, qty, oid, ts, etype=1):
        seq[vid] += 1
        rows.append({"event_id": len(rows) + 1, "instrument_id": 1,
                     "venue_id": vid, "exchange_ts": ts, "receive_ts": ts,
                     "sequence": seq[vid], "event_type": etype, "side": side,
                     "price_ticks": px, "qty": qty, "order_id": oid,
                     "trade_id": 0})
    add(1, 0, 100, 100, 11, t0)          # v1 bid 100
    add(1, 1, 102, 100, 12, t0 + 1)      # v1 ask 102 -> mid 101
    add(2, 0, 102, 50, 21, t0 + 2)       # v2 bid 102 -> LOCKED (102/102), hs 0
    add(2, 0, 103, 50, 22, t0 + 3)       # v2 bid 103 -> CROSSED (103/102): skipped
    add(2, 0, 104, 50, 23, t0 + 4)       # still crossed (best bid 104)
    add(2, 0, 104, 50, 23, t0 + 5, 3)    # cancel 104: still crossed (103)
    add(2, 0, 103, 50, 22, t0 + 6, 3)    # cancel 103: locked again
    for k in range(60):                  # pad to a long enough timeline
        add(1, 0, 90, 10, 100 + k, t0 + 10 + k)
    path = tmp_path / "ev.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    tl = build_timeline(path, 1, 0.01)
    assert tl.crossed_states_skipped == 3
    i = tl.prevailing(t0 + 2)
    assert tl.half_spread(i) == 0.0 and abs(tl.mid(i) - 1.02) < 1e-12  # locked kept
    assert tl.prevailing(t0 + 5) == i                        # crossed skipped
    assert tl.prevailing(t0 + 6) == i + 1                    # locked 102/102 again
    assert tl.half_spread(i + 1) == 0.0
    with pytest.raises(ValueError):
        tl.append(t0 + 100, 1.01, 1.00, 1, 1)                # crossed never appended


def test_tca_fill_outside_window_rejected():
    tl = _mini_timeline()
    order = ParentOrder(order_id=1, instrument_id=1, side=0, qty_target=100,
                        decision_ts=1_000_000_000, arrival_ts=2_000_000_000,
                        end_ts=9_000_000_000)
    order.fills.append(Fill(ts=9_000_000_001, price=100.03, qty=100,
                            mid_at_fill=100.01, half_spread_at_fill=0.02,
                            opp_depth_at_fill=200))
    with pytest.raises(ValueError, match="outside"):
        order_tca(order, tl)
    with pytest.raises(ValueError, match="outside"):
        validate_order_window(order, tl)
    # end_ts beyond the last state: end_mid would be fabricated -> reject
    late = ParentOrder(order_id=2, instrument_id=1, side=0, qty_target=100,
                       decision_ts=1_000_000_000, arrival_ts=2_000_000_000,
                       end_ts=11_000_000_000)
    with pytest.raises(ValueError, match="beyond"):
        order_tca(late, tl)
    # inverted window
    bad = ParentOrder(order_id=3, instrument_id=1, side=0, qty_target=100,
                      decision_ts=3_000_000_000, arrival_ts=2_000_000_000,
                      end_ts=9_000_000_000)
    with pytest.raises(ValueError):
        order_tca(bad, tl)
