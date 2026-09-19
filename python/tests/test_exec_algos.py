"""Parent-algo schedule tests (TWAP/VWAP/POV/IS) and replay-driven scenarios.

Ports cpp/tests/test_exec_algos.cpp (AlgoSchedule / AlgoReplay / Algo*
scenario groups) and the Java AlgosTest / ExecutionScenarioTest algo rows.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.execution import (
    AlgoType,
    CancelReason,
    ExecConfig,
    ExecutionReplay,
    InstrumentSpec,
    Liquidity,
    OrderState,
    OrderType,
    ParentOrder,
    VenueSpec,
    slice_quantities,
    slice_times,
    slice_weights,
)

T0 = 1_700_000_000_000_000_000
SEC = 1_000_000_000


def parent(algo: AlgoType, qty: int, slices: int, **kw) -> ParentOrder:
    return ParentOrder(
        parent_id=1, instrument_id=7, venue_id=1, side=0, qty=qty, algo=algo,
        start_ts=T0, end_ts=T0 + 100 * SEC, slices=slices, **kw,
    )


# ------------------------------------------------------------- schedules --


def test_twap_uniform_slices_sum_exactly():
    q = slice_quantities(parent(AlgoType.TWAP, 1000, 8))
    assert q == [125] * 8
    # Non-divisible: remainder goes to the earliest slices (pinned).
    r = slice_quantities(parent(AlgoType.TWAP, 1002, 8))
    assert sum(r) == 1002
    assert r == [126, 126, 125, 125, 125, 125, 125, 125]
    assert slice_weights(parent(AlgoType.TWAP, 1000, 5)) == [1.0] * 5


def test_vwap_u_shaped_curve():
    q = slice_quantities(parent(AlgoType.VWAP, 1000, 9))
    assert len(q) == 9 and sum(q) == 1000
    assert q[0] > q[4] and q[8] > q[4]  # edge > middle
    for i in range(4):
        assert q[i] >= q[i + 1]  # front half decays toward the middle
        assert abs(q[i] - q[8 - i]) <= 1  # symmetric up to rounding
    # Exact weights for N=4: w = {2, 10/9, 10/9, 2} => {129, 71, 71, 129}
    # by largest remainder (golden-scenario apportionment).
    w = slice_weights(parent(AlgoType.VWAP, 400, 4))
    assert w == [2.0, 1.0 + (1.0 / 3.0) ** 2, 1.0 + (1.0 / 3.0) ** 2, 2.0]
    assert slice_quantities(parent(AlgoType.VWAP, 400, 4)) == [129, 71, 71, 129]


def test_is_front_loaded_by_risk_aversion():
    p = parent(AlgoType.IS, 900, 6, risk_aversion=1.0)
    q = slice_quantities(p)
    assert sum(q) == 900
    for i in range(1, len(q)):
        assert q[i] <= q[i - 1]  # non-increasing
    assert q[0] > q[-1]
    # Higher urgency shifts more quantity into the first slice.
    qh = slice_quantities(replace(p, risk_aversion=3.0))
    assert qh[0] > q[0]
    # Golden-scenario apportionment (600 over 3 slices, lambda 1.0):
    # w = {1, e^-0.5, e^-1} => {304, 184, 112}.
    assert slice_quantities(parent(AlgoType.IS, 600, 3, risk_aversion=1.0)) == [304, 184, 112]
    # risk_aversion 0 degenerates to TWAP.
    assert slice_quantities(parent(AlgoType.IS, 800, 4, risk_aversion=0.0)) == [200] * 4


def test_single_slice_takes_all():
    for algo in (AlgoType.TWAP, AlgoType.VWAP, AlgoType.IS):
        p = parent(algo, 777, 1)
        assert slice_weights(p) == [1.0]
        assert slice_quantities(p) == [777]


@pytest.mark.parametrize("algo", [AlgoType.TWAP, AlgoType.VWAP, AlgoType.IS])
@pytest.mark.parametrize(
    "qty,slices", [(1, 7), (5, 7), (97, 13), (400, 4), (600, 3), (999_983, 11), (10, 4)]
)
def test_largest_remainder_sums_exactly(algo, qty, slices):
    q = slice_quantities(parent(algo, qty, slices))
    assert all(x >= 0 for x in q)
    assert sum(q) == qty


def test_largest_remainder_stays_within_one_share():
    p = parent(AlgoType.VWAP, 12_345, 9)
    w = slice_weights(p)
    q = slice_quantities(p)
    wsum = sum(w)
    for i in range(len(q)):
        assert abs(q[i] - p.qty * w[i] / wsum) < 1.0


def test_remainder_ties_go_to_earlier_slice():
    # TWAP 10 over 4: targets 2.5 each; the 2 leftovers land on slices 0, 1.
    assert slice_quantities(parent(AlgoType.TWAP, 10, 4)) == [3, 3, 2, 2]


def test_slice_times_evenly_spaced_with_integer_division():
    t = slice_times(parent(AlgoType.TWAP, 100, 4))
    assert t == [T0, T0 + 25 * SEC, T0 + 50 * SEC, T0 + 75 * SEC]
    p = replace(parent(AlgoType.TWAP, 100, 3), start_ts=1000, end_ts=2000)
    assert slice_times(p) == [1000, 1333, 1666]  # floor, never rounded
    assert slice_times(p)[-1] < p.end_ts


def test_invalid_parents_rejected():
    with pytest.raises(ValueError):
        slice_times(replace(parent(AlgoType.TWAP, 100, 4), end_ts=T0))
    with pytest.raises(ValueError):
        slice_weights(parent(AlgoType.POV, 100, 4))  # POV is event-driven
    with pytest.raises(ValueError):
        slice_quantities(parent(AlgoType.POV, 100, 4))
    with pytest.raises(ValueError):
        slice_weights(parent(AlgoType.TWAP, 100, 0))
    with pytest.raises(ValueError):
        slice_times(parent(AlgoType.TWAP, 100, 0))
    with pytest.raises(ValueError):
        slice_quantities(parent(AlgoType.TWAP, 0, 4))
    with pytest.raises(ValueError):
        ExecutionReplay(algo_config(), [parent(AlgoType.TWAP, 0, 4)])
    with pytest.raises(ValueError):
        ExecutionReplay(algo_config(), [parent(AlgoType.TWAP, 10, 4, max_child_qty=0)])
    with pytest.raises(ValueError):
        ExecutionReplay(algo_config(), [replace(parent(AlgoType.POV, 10, 4), end_ts=T0)])


# --------------------------------------------------------- replay-driven --


def algo_config() -> ExecConfig:
    v1 = VenueSpec(
        venue_id=1, name="TST", taker_fee_per_share=0.003,
        maker_rebate_per_share=0.002, latency_mean_ns=100_000, latency_jitter_ns=0,
    )
    v2 = replace(v1, venue_id=2, taker_fee_per_share=0.001, maker_rebate_per_share=0.0025)
    return ExecConfig(
        seed=7, venues={1: v1, 2: v2},
        instruments={7: InstrumentSpec(7, 0.01, 1.0, 1_000_000.0)},
    )


def _ev(seq, ts, etype, side, px, qty, oid, tid=0, venue=1) -> MarketEvent:
    return MarketEvent(
        event_id=seq, instrument_id=7, venue_id=venue, exchange_ts=ts,
        receive_ts=ts, sequence=seq, event_type=int(etype), side=side,
        price_ticks=px, qty=qty, order_id=oid, trade_id=tid,
    )


def algo_stream():
    """A two-sided venue-1 book plus a 40-lot TRADE every second for 200 s."""
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, 100, 100_000, 1, 0)
    push(T0 - SEC + 1, EventType.ADD, 1, 101, 100_000, 2, 0)
    for i in range(200):
        ts = T0 + i * SEC
        push(ts, EventType.TRADE, 0 if i % 2 == 0 else 1, 100, 40, 0, 1000 + i)
        push(ts + SEC // 2, EventType.HEARTBEAT, 0, 0, 0, 0, 0)
    return evs


def test_pov_tracks_participation_cap():
    p = parent(AlgoType.POV, 500, 1, participation=0.10, max_child_qty=25)
    res = ExecutionReplay(algo_config(), [p]).run(algo_stream())
    rep = res.parents[1]
    # Window volume = 100 TRADEs of 40 inside [T0, T0+100s) = 4000;
    # 10% participation = 400 <= parent 500 and every child <= 25.
    assert rep.filled_qty == 400
    assert rep.children >= 400 // 25
    sent = vol_seen = 0
    fi = 0
    for i in range(100):
        vol_seen += 40
        while fi < len(res.fills) and res.fills[fi].ts <= T0 + i * SEC + SEC // 2:
            sent += res.fills[fi].qty
            fi += 1
        assert sent <= int(0.10 * vol_seen) + 25  # never ahead of the tape
    for f in res.fills:
        assert f.qty <= 25
        assert f.liquidity == Liquidity.TAKER  # POV children: MARKET


def test_twap_children_spread_across_window():
    res_replay = ExecutionReplay(algo_config(), [parent(AlgoType.TWAP, 400, 4)])
    res = res_replay.run(algo_stream())
    assert res.parents[1].children == 4
    due = [T0, T0 + 25 * SEC, T0 + 50 * SEC, T0 + 75 * SEC]
    decisions = sorted(o.decision_ts for o in res_replay.simulator.orders.values())
    assert len(decisions) == 4
    for i in range(4):
        assert due[i] <= decisions[i] < due[i] + SEC  # stream ticks every 0.5 s
    for o in res_replay.simulator.orders.values():
        assert o.type == OrderType.LIMIT  # passive join of the best bid
        assert o.limit_ticks == 100
        assert o.expire_ts == T0 + 100 * SEC


def test_vwap_follows_the_curve_in_replay():
    replay = ExecutionReplay(algo_config(), [parent(AlgoType.VWAP, 400, 4)])
    replay.run(algo_stream())
    qtys = [o.qty for o in sorted(replay.simulator.orders.values(), key=lambda o: o.order_id)]
    assert qtys == [129, 71, 71, 129]


def test_is_schedule_in_replay_is_front_loaded_market_children():
    replay = ExecutionReplay(algo_config(), [parent(AlgoType.IS, 600, 3, risk_aversion=1.0)])
    res = replay.run(algo_stream())
    orders = sorted(replay.simulator.orders.values(), key=lambda o: o.order_id)
    assert [o.qty for o in orders] == [304, 184, 112]
    assert all(o.type == OrderType.MARKET for o in orders)
    assert res.parents[1].filled_qty == 600
    assert all(f.liquidity == Liquidity.TAKER for f in res.fills)


def test_accounting_identity_and_determinism():
    p1 = parent(AlgoType.VWAP, 300, 3)
    p2 = replace(parent(AlgoType.IS, 200, 2), parent_id=2, side=1)
    events = algo_stream()

    def run():
        return ExecutionReplay(algo_config(), [p1, p2]).run(events)

    a, b = run(), run()
    assert [f.to_dict() for f in a.fills] == [f.to_dict() for f in b.fills]
    for pid, rep in a.parents.items():
        fees = rebates = impact = notional = 0.0
        qty = 0
        for f in a.fills:
            if f.parent_id != pid:
                continue
            if f.fee >= 0.0:
                fees += f.fee
            else:
                rebates += -f.fee
            impact += f.impact_cost
            notional += float(f.qty) * float(f.price_ticks) * 0.01
            qty += f.qty
        assert rep.filled_qty == qty
        assert rep.fees == fees
        assert rep.rebates == rebates
        assert rep.impact == impact
        assert rep.notional == notional
        assert rep.total_cost == fees - rebates + impact
        assert rep.filled_qty + rep.unfilled_qty == (p1.qty if pid == 1 else p2.qty)
    assert list(a.parents) == [1, 2]


def test_no_route_children_are_skipped_and_counted():
    p = replace(parent(AlgoType.IS, 300, 3), venue_id=0)  # SOR-routed
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, gap):
        nonlocal seq
        if gap:
            seq += 1
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid))

    push(T0 - SEC, EventType.ADD, 0, 100, 100_000, 1, False)
    push(T0 - SEC + 1, EventType.ADD, 1, 101, 100_000, 2, False)
    push(T0 - SEC + 2, EventType.ADD, 1, 102, 100, 3, True)  # gap: venue 1 stale
    for i in range(200):
        push(T0 + i * SEC, EventType.HEARTBEAT, 0, 0, 0, 0, False)
    res = ExecutionReplay(algo_config(), [p]).run(evs)
    assert res.sor_no_route == 3
    assert res.parents[1].children == 0
    assert res.parents[1].unfilled_qty == 300
    assert res.fills == []


def test_sor_routed_parent_uses_the_router_per_child_style():
    # Equal displayed quotes on both venues: venue 2 wins the aggressive
    # tie (lower taker fee) and the passive choice (higher rebate), so every
    # child of both parents is submitted with venue_id 2.
    evs = []
    seq = {1: 0, 2: 0}

    def push(venue, ts, etype, side, px, qty, oid):
        seq[venue] += 1
        evs.append(_ev(seq[venue], ts, etype, side, px, qty, oid, venue=venue))

    push(1, T0 - SEC, EventType.ADD, 0, 100, 1000, 1)
    push(1, T0 - SEC + 1, EventType.ADD, 1, 101, 1000, 2)
    push(2, T0 - SEC + 2, EventType.ADD, 0, 100, 1000, 3)
    push(2, T0 - SEC + 3, EventType.ADD, 1, 101, 1000, 4)
    for i in range(120):
        push(1, T0 + i * SEC, EventType.HEARTBEAT, 0, 0, 0, 0)
    p_pass = replace(parent(AlgoType.TWAP, 40, 2), venue_id=0)
    p_aggr = replace(parent(AlgoType.IS, 40, 2), venue_id=0, parent_id=2, side=1)
    replay = ExecutionReplay(algo_config(), [p_pass, p_aggr])
    res = replay.run(evs)
    assert res.sor_no_route == 0
    assert all(o.venue_id == 2 for o in replay.simulator.orders.values())
    assert all(f.venue_id == 2 for f in res.fills)
    assert res.parents[2].filled_qty == 40


def test_slice_larger_than_max_child_is_split():
    p = parent(AlgoType.IS, 20_000, 8, max_child_qty=1000, risk_aversion=0.0)
    replay = ExecutionReplay(algo_config(), [p])
    res = replay.run(algo_stream())
    rep = res.parents[1]
    assert rep.children == 24  # 8 slices of 2500 -> 1000 + 1000 + 500 each
    assert rep.filled_qty == 20_000  # the 100000 displayed ask absorbs it
    assert rep.unfilled_qty == 0
    for o in replay.simulator.orders.values():
        assert o.qty <= 1000
        assert o.expire_ts == p.end_ts
    # The three children of a slice share one decision time, in order.
    orders = sorted(replay.simulator.orders.values(), key=lambda o: o.order_id)
    assert [o.qty for o in orders[:3]] == [1000, 1000, 500]
    assert len({o.decision_ts for o in orders[:3]}) == 1


def test_children_cancelled_at_end_ts_late_crossing_print_does_not_fill():
    p = parent(AlgoType.VWAP, 400, 4)
    evs = algo_stream()  # ends at T0 + 199.5 s, window ends T0+100s
    seq = len(evs) + 1
    # A crossing ask well after the window (limit 99 < our 100 bids).
    evs.append(_ev(seq, T0 + 150 * SEC, EventType.ADD, 1, 99, 100_000, 77))
    replay = ExecutionReplay(algo_config(), [p])
    res = replay.run(evs)
    rep = res.parents[1]
    assert rep.children == 4
    assert rep.filled_qty == 0  # no fills after end_ts
    assert rep.unfilled_qty == 400
    for o in replay.simulator.orders.values():
        assert o.state == OrderState.CANCELLED
        assert o.cancel_reason == CancelReason.EXPIRED
    assert replay.simulator.counters.expired_orders == 4


def test_no_child_outlives_its_window_property():
    # Every child of every algo carries expire_ts == end_ts and every fill
    # lies inside [arrival_ts, end_ts].
    parents = [
        parent(AlgoType.TWAP, 300, 3),
        replace(parent(AlgoType.VWAP, 300, 3), parent_id=2),
        replace(parent(AlgoType.IS, 300, 3), parent_id=3, side=1),
        replace(parent(AlgoType.POV, 300, 1), parent_id=4, participation=0.5, max_child_qty=30),
    ]
    replay = ExecutionReplay(algo_config(), parents)
    res = replay.run(algo_stream())
    assert res.fills
    by_pid = {p.parent_id: p for p in parents}
    for o in replay.simulator.orders.values():
        assert o.expire_ts == by_pid[o.parent_id].end_ts
        assert o.is_terminal
    for f in res.fills:
        o = replay.simulator.orders[f.order_id]
        assert o.arrival_ts <= f.ts <= by_pid[f.parent_id].end_ts
        assert by_pid[f.parent_id].start_ts <= f.ts


def test_pov_resends_after_cancelled_remainder():
    p = parent(AlgoType.POV, 500, 1, participation=0.10, max_child_qty=25)
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, 100, 100_000, 1, 0)
    push(T0 - SEC + 1, EventType.ADD, 1, 101, 30, 2, 0)  # thin ask: 30 displayed
    for i in range(200):
        ts = T0 + i * SEC
        if i == 30:
            push(ts - 1, EventType.ADD, 1, 101, 100_000, 3, 0)  # liquidity back
        push(ts, EventType.TRADE, 0, 100, 40, 0, 1000 + i)
        push(ts + SEC // 2, EventType.HEARTBEAT, 0, 0, 0, 0, 0)
    replay = ExecutionReplay(algo_config(), [p])
    res = replay.run(evs)
    rep = res.parents[1]
    # The first children hit the 30-lot display (25 filled, then a 5-lot
    # partial with a 20-lot cancelled remainder); the deficit is re-sent
    # against filled qty, so the 10% target (400) is still reached.
    assert rep.filled_qty == 400
    assert rep.children > 16  # re-sent remainders add children
    saw_partial = any(
        o.state == OrderState.CANCELLED and 0 < o.remaining < o.qty
        for o in replay.simulator.orders.values()
    )
    assert saw_partial


def test_pov_deficit_counts_in_flight_quantity():
    # With a slow venue (100 ms) and prints every 1 s, the in-flight child
    # counts as committed: no second child is sent for the same deficit.
    cfg = algo_config()
    cfg = replace(cfg, venues={1: replace(cfg.venues[1], latency_mean_ns=100_000_000)})
    p = parent(AlgoType.POV, 500, 1, participation=0.5, max_child_qty=1000)
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, 100, 100_000, 1, 0)
    push(T0 - SEC + 1, EventType.ADD, 1, 101, 100_000, 2, 0)
    push(T0, EventType.TRADE, 0, 100, 100, 0, 1)  # target 50 -> child 50
    push(T0 + 1_000, EventType.HEARTBEAT, 0, 0, 0, 0, 0)  # child still in flight
    push(T0 + 2_000, EventType.TRADE, 0, 100, 20, 0, 2)  # target 60 -> deficit 10
    push(T0 + SEC, EventType.HEARTBEAT, 0, 0, 0, 0, 0)  # both arrive
    push(T0 + 2 * SEC, EventType.HEARTBEAT, 0, 0, 0, 0, 0)
    replay = ExecutionReplay(cfg, [p])
    res = replay.run(evs)
    qtys = [o.qty for o in sorted(replay.simulator.orders.values(), key=lambda o: o.order_id)]
    assert qtys == [50, 10]
    assert res.parents[1].filled_qty == 60


def test_replay_gated_venue_status_is_respected_by_scheduler():
    # A halt over the whole window: passive TWAP children rest (LIMIT) and
    # expire unfilled; IS MARKET children are VENUE_NOT_TRADING cancels.
    evs = algo_stream()
    seq = len(evs)
    halt = _ev(seq + 1, T0 - SEC + 2, EventType.STATUS, 0, 0, int(SessionStatus.HALT), 0)
    evs.insert(2, halt)
    for i, ev in enumerate(evs[3:], start=seq + 2):
        ev.sequence = i
        ev.event_id = i
    p1 = parent(AlgoType.TWAP, 40, 2)
    p2 = replace(parent(AlgoType.IS, 40, 2), parent_id=2, side=1)
    replay = ExecutionReplay(algo_config(), [p1, p2])
    res = replay.run(evs)
    assert res.fills == []
    orders = replay.simulator.orders.values()
    reasons = {
        pid: {o.cancel_reason for o in orders if o.parent_id == pid} for pid in (1, 2)
    }
    assert reasons[1] == {CancelReason.EXPIRED}
    assert reasons[2] == {CancelReason.VENUE_NOT_TRADING}
    assert replay.simulator.counters.venue_not_trading_cancels == 2
