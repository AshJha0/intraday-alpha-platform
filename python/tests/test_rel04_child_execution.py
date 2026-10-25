"""v1.18 REL-04: venue-aware child execution, IEX fidelity (hidden prints, midpoint
peg, inbound delay, capture timestamp) and the adversarial event cases.  Every
case has a deterministic expected outcome on a hand-built book (synthetic only).

Book used throughout (``seed_book``): bids 100x300, 99x400; asks 101x200, 102x500.
"""

from __future__ import annotations

from dataclasses import replace

import iex_encoder as ix
import pytest
from iap.core.events import EventType, SessionStatus
from iap.execution import (
    AlgoType,
    CancelReason,
    ChildExecParams,
    ExecutionReplay,
    ExecutionSimulator,
    Liquidity,
    OrderState,
    OrderType,
    ParentOrder,
)
from iap.execution.child_exec import eligible_depth, participation_room
from iap.execution.types import FILL_PROVENANCES
from iap.marketdata.iex import (
    CAPTURE_TS_PROVENANCE,
    PROTOCOL_DEEP_PLUS,
    FeedLatency,
    IexReader,
)
from iap.marketdata.itch50 import session_midnight_ns
from test_execution_rules import INS, LAT, T0, VEN, Feeder, child, make_config, seed_book

MS = 1_000_000


def _parent(
    ce, *, algo=AlgoType.IS, qty=1000, start=T0 + 10, window=60_000 * MS, venue=0, **kw
) -> ParentOrder:
    return ParentOrder(
        parent_id=1,
        instrument_id=INS,
        venue_id=venue,
        side=0,
        qty=qty,
        algo=algo,
        start_ts=start,
        end_ts=start + window,
        slices=1,
        max_child_qty=1000,
        child_exec=ce,
        **kw,
    )


def _book(f):
    return [
        f.add(T0, 0, 100, 300, 11),
        f.add(T0 + 1, 0, 99, 400, 12),
        f.add(T0 + 2, 1, 101, 200, 21),
        f.add(T0 + 3, 1, 102, 500, 22),
    ]


def _beats(f, *offsets_ms):
    return [f.heartbeat(T0 + 10 + o * MS) for o in offsets_ms]


def _run(parent, events, **kw):
    # the REL-04 fixtures pin the zero-delay timing legs explicitly: since v2.0 D4
    # the replay's default is the DECLARED feed delay (test_latency_fees.py)
    kw.setdefault("md_delay_ns", 0)
    return ExecutionReplay(make_config(), [parent], **kw).run(events)


# ------------------------------------------------------------- sizing --


def test_eligible_depth_is_net_of_consumption_and_bounded_by_levels_and_band():
    ce = ChildExecParams(max_levels=2)
    levels = [(101, 200), (102, 500), (103, 900)]
    assert eligible_depth(levels, 0, ce, lambda px: 0) == (700, 102)
    assert eligible_depth(levels, 0, ce, lambda px: 150 if px == 101 else 0) == (550, 102)
    band = ChildExecParams(max_levels=3, max_slippage_ticks=1)
    assert eligible_depth(levels, 0, band, lambda px: 0) == (700, 102)  # 103 is 2 ticks away
    half = ChildExecParams(max_levels=1, depth_fraction=0.5)
    assert eligible_depth(levels, 0, half, lambda px: 0) == (100, 101)
    assert eligible_depth([], 0, ce, lambda px: 0) == (0, 0)
    assert eligible_depth(levels, 0, ce, lambda px: 10**9) == (0, 0)  # fully consumed
    assert participation_room(ChildExecParams(), 5000, 0, 777) == 777  # off
    assert participation_room(ChildExecParams(max_participation=0.1), 1000, 30, 777) == 70
    assert participation_room(ChildExecParams(max_participation=0.1), 1000, 500, 777) == 0
    for bad in (
        {"style": "x"},
        {"max_levels": 0},
        {"depth_fraction": 0.0},
        {"max_participation": 2},
    ):
        with pytest.raises(ValueError):
            ChildExecParams(**bad)


def test_default_child_is_the_pinned_market_order_walk():
    f = Feeder()
    res = _run(_parent(None), _book(f) + _beats(f, 0, 5))
    kids = res.children[1]
    assert [k.type for k in kids] == [OrderType.MARKET] and kids[0].qty == 1000
    assert [(x.qty, x.price_ticks) for x in res.fills] == [(200, 101), (500, 102)]
    assert res.parents[1].depth_exhausted == 1  # the legacy under-fill, counted
    assert res.parents[1].cap_no_depth == 0 and res.parents[1].reposts == 0


def test_capped_ioc_takes_only_eligible_depth_then_reposts_after_replenishment():
    f = Feeder()
    ce = ChildExecParams(max_levels=1, repost_interval_ns=1000 * MS)
    ev = _book(f) + _beats(f, 0, 5)  # slice at +0, IOC arrives and fills 200
    ev += [f.add(T0 + 10 + 500 * MS, 1, 101, 300, 23)]  # display 101: 200 -> 500
    ev += _beats(f, 1500, 1600)  # repost due at +1000 ms; fills the net 300
    res = _run(_parent(ce), ev)
    kids = res.children[1]
    assert kids[0].type == OrderType.IOC and kids[0].qty == 200 and kids[0].limit_ticks == 101
    assert [(x.qty, x.price_ticks) for x in res.fills] == [(200, 101), (300, 101)]
    # nothing past the cap: no 102 fill, the level was never consumed twice (200 + 300 = 500)
    assert all(x.price_ticks == 101 for x in res.fills)
    r = res.parents[1]
    assert r.filled_qty == 500 and r.residual_qty == 500 and r.reposts >= 1
    assert r.depth_exhausted == 0  # a capped child never walks off the depth
    assert r.cap_withheld_qty == 800  # the first post held back 800 of the 1000 slice


def test_displayed_liquidity_is_never_consumed_twice():
    """Two reposts against an unchanged display: the second sees nothing eligible."""
    f = Feeder()
    ce = ChildExecParams(max_levels=1, repost_interval_ns=10 * MS)
    ev = _book(f) + _beats(f, 0, 5, 30, 60, 90, 120)
    res = _run(_parent(ce), ev)
    assert sum(x.qty for x in res.fills) == 200  # the 101 level holds 200, once
    r = res.parents[1]
    assert r.cap_no_depth >= 1 and r.reposts >= 1  # the empty re-posts are COUNTED
    assert r.residual_qty == 800


def test_exhausted_depth_is_a_failure_counter_and_nothing_is_manufactured():
    f = Feeder()
    ev = [f.add(T0, 0, 100, 300, 11)] + _beats(f, 0, 5, 3000)  # no asks at all
    res = _run(_parent(ChildExecParams()), ev)
    r = res.parents[1]
    assert res.fills == [] and r.filled_qty == 0
    assert r.cap_no_depth >= 1 and r.residual_qty == 1000 and r.carry_unsent_qty == 1000
    assert r.avg_price == 0.0 and r.notional == 0.0


def test_incomplete_parent_reports_residual_at_window_end_never_a_fill():
    f = Feeder()
    ce = ChildExecParams(max_levels=2, repost_interval_ns=10 * MS)
    ev = _book(f) + _beats(f, 0, 5, 100, 70_000)  # last beat after the 60 s window
    res = _run(_parent(ce, window=1000 * MS), ev)
    r = res.parents[1]
    assert sum(x.qty for x in res.fills) == 700 == r.filled_qty  # 200 + 500 is ALL the display
    assert r.unfilled_qty == r.residual_qty == 300
    assert all(x.ts <= T0 + 10 + 1000 * MS for x in res.fills)


def test_participation_cap_uses_executing_venue_trade_volume():
    f = Feeder()
    ce = ChildExecParams(max_levels=2, max_participation=0.10)
    ev = _book(f) + [f.ev(T0 + 11, EventType.TRADE, 1, 101, 1000, 0)] + _beats(f, 5)
    res = _run(_parent(ce), ev)
    # trade volume 1000 since the window opened -> 10 % = 100 shares, not the 700 displayed
    assert res.children[1][0].qty == 100
    assert sum(x.qty for x in res.fills) == 100


def test_participation_ignores_volume_before_the_window_and_other_venues():
    f = Feeder()
    ce = ChildExecParams(max_levels=2, max_participation=0.10)
    early = [f.ev(T0 + 5, EventType.TRADE, 1, 101, 5000, 0)]  # before start_ts = T0 + 10
    ev = _book(f) + early + _beats(f, 0, 5)
    res = _run(_parent(ce), ev)
    assert res.parents[1].cap_no_depth >= 1 and res.fills == []  # window volume is 0


def test_pov_parent_carries_nothing_its_deficit_is_the_repost():
    f = Feeder()
    ce = ChildExecParams(max_levels=1)
    pov = _parent(ce, algo=AlgoType.POV, participation=0.5)
    ev = _book(f) + [f.ev(T0 + 11, EventType.TRADE, 1, 101, 1000, 0)] + _beats(f, 5, 10)
    res = _run(pov, ev)
    r = res.parents[1]
    assert res.children[1][0].qty == 200  # 500 target capped at the 200 displayed
    assert r.carry_unsent_qty == 0 and r.reposts == 0 and r.cap_withheld_qty == 300


def test_market_data_delay_orders_decision_before_arrival():
    f = Feeder()
    ev = _book(f) + _beats(f, 0, 5)
    res = _run(_parent(ChildExecParams()), ev, md_delay_ns=2 * MS)
    o = res.children[1][0]
    # seen at the event (T0 + 10), decided 2 ms later, then the pinned latency legs
    assert o.decision_ts == T0 + 10 + 2 * MS and o.arrival_ts == o.decision_ts + LAT
    zero = _run(_parent(ChildExecParams()), ev)
    assert zero.children[1][0].arrival_ts == T0 + 10 + LAT


# ------------------------------------------------ venue spec: speed bump --


def test_inbound_delay_applies_to_orders_and_cancels_and_defaults_off():
    cfg = make_config()
    assert cfg.venues[VEN].inbound_delay_ns == 0
    bumped = replace(cfg, venues={VEN: replace(cfg.venues[VEN], inbound_delay_ns=350_000)})
    sim = ExecutionSimulator(bumped)
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    assert sim.orders[oid].arrival_ts == T0 + 10 + LAT + 350_000
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))  # before the bumped arrival: still pending
    assert sim.orders[oid].state == OrderState.PENDING
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 350_001))
    assert sim.orders[oid].state == OrderState.ACTIVE
    sim.cancel(oid, T0 + 1_000_000)
    assert sim.orders[oid].cancel_arrival_ts == T0 + 1_000_000 + LAT + 350_000
    with pytest.raises(ValueError):
        replace(cfg.venues[VEN], inbound_delay_ns=-1)


def test_inbound_delay_loads_from_the_venue_json(tmp_path):
    import json

    from iap.execution.config import load_venues

    row = {
        "venue_id": 5,
        "venue": "IEXG",
        "latency": {"mean_ns": 1, "jitter_ns": 0},
        "inbound_delay_ns": 350_000,
    }
    plain = {**row, "venue_id": 6, "venue": "PLN"}
    plain.pop("inbound_delay_ns")
    p = tmp_path / "venues.json"
    p.write_text(json.dumps({"venues": [row, plain]}), encoding="utf-8")
    v = load_venues(p)
    assert v[5].inbound_delay_ns == 350_000 and v[6].inbound_delay_ns == 0


# ------------------------------------------- hidden prints, midpoint peg --


def _peg_parent(**kw):
    return _parent(ChildExecParams(style="midpoint_peg"), qty=100, **kw)


def _peg_events(f, hidden):
    ev = _book(f) + _beats(f, 0, 5)  # peg decided at +0, rests after arrival
    # sell-aggressor non-displayed prints: at the mid (100/101 -> 100 or 101), then away
    for k, (px, qty) in enumerate(hidden):
        ev.append(f.ev(T0 + 10 + (100 + 10 * k) * MS, EventType.TRADE, 1, px, qty, 0, 900 + k))
    return ev


def test_midpoint_peg_fills_only_on_hidden_prints_at_the_mid_and_never_takes_the_display():
    f = Feeder()
    ev = _peg_events(f, [(100, 60), (99, 500), (101, 100)])
    off = _run(_peg_parent(), ev)  # hidden_prints off: nothing can fill a peg
    assert off.fills == [] and off.children[1][0].peg == "midpoint"
    on = _run(_peg_parent(), ev, hidden_prints=True)
    # print 1: 60 at 100 (mid 100.5, half a tick away); print 2 at 99 is not at the mid;
    # print 3 at 101 (also half a tick) fills the remaining 40, inside the limit 101
    assert [(x.qty, x.price_ticks, x.liquidity, x.provenance) for x in on.fills] == [
        (60, 100, Liquidity.MAKER, "midpoint_peg"),
        (40, 101, Liquidity.MAKER, "midpoint_peg"),
    ]
    assert on.parents[1].filled_qty == 100 and "midpoint_peg" in FILL_PROVENANCES
    # the displayed ask 101x200 was never walked
    assert all(x.provenance != "aggressive_walk" for x in on.fills)


def test_midpoint_peg_respects_its_limit_and_the_modelled_queue():
    f = Feeder()
    ev = _peg_events(f, [(101, 50), (100, 80)])
    capped = _run(_parent(ChildExecParams(style="midpoint_peg"), qty=100), ev, hidden_prints=True)
    assert [x.qty for x in capped.fills] == [50, 50]
    queued = _run(
        _parent(ChildExecParams(style="midpoint_peg"), qty=100),
        ev,
        hidden_prints=True,
        peg_ahead_qty=70,
    )
    # 70 shares queued ahead: the first print (50) pays them down, the second (80) pays 20 + fills 60
    assert [x.qty for x in queued.fills] == [60]


def test_hidden_print_pool_is_shared_with_the_displayed_queue_once():
    """A hidden print at 100 reaches the displayed bid queue first; the peg gets only the leftover."""
    sim = ExecutionSimulator(make_config(), hidden_prints=True)
    f = Feeder()
    seed_book(sim, f)
    bid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))  # displayed, behind 300
    peg = sim.submit(child(0, OrderType.LIMIT, 101, 100, T0 + 10, peg="midpoint"))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    sim.on_event(f.ev(T0 + 2 * MS, EventType.TRADE, 1, 100, 350, 0, 77))
    # 350 = 300 ahead of the displayed bid + 50 for it; nothing is left for the peg
    assert [(x.order_id, x.qty) for x in sim.fills] == [(bid, 50)]
    sim.on_event(f.ev(T0 + 3 * MS, EventType.TRADE, 1, 100, 30, 0, 78))
    assert [(x.order_id, x.qty) for x in sim.fills][-1] == (peg, 30)
    assert sim.peg_fills == 1


# ------------------------------------------------------ IEX capture ts --


def test_iex_capture_timestamp_is_kept_as_receive_ts_with_its_qualifications(tmp_path):
    date = "2025-01-15"
    mid = session_midnight_ns(date)
    send = mid + 3_600 * 10**9
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS, nanosecond=True)
    w.segment([ix.system_event("O", send)], send_time=send, capture_ts=send + 80_000)
    w.segment(
        [ix.system_event("S", send + 1000)], send_time=send + 1000, capture_ts=send + 1500 + 20_000
    )
    r = IexReader(w.write(tmp_path / "day.pcap"), date, ["AAPL"])
    seen = []
    for _ in r:
        seen.append(r.receive_ts)
    assert seen == [3_600 * 10**9 + 80_000, 3_600 * 10**9 + 21_500]
    s = r.feed_latency.summary()
    assert s["n"] == 2 and s["min_ns"] == 20_500 and s["max_ns"] == 80_000 and s["negative"] == 0
    assert "unidentified" in CAPTURE_TS_PROVENANCE["offset_limits"]
    assert "never as a trading advantage" in CAPTURE_TS_PROVENANCE["use"]
    assert "offset" in s["qualification"]
    assert r.counters["capture_ts_missing"] == 0


def test_feed_latency_counts_negative_deltas_as_evidence_of_the_clock_offset():
    fl = FeedLatency()
    for d in (-5, 0, 1, 1000):
        fl.add(d)
    s = fl.summary()
    assert s["n"] == 4 and s["negative"] == 1 and s["min_ns"] == -5
    assert sum(s["log2_histogram"].values()) == 3


# ------------------------------------------------ adversarial sequences --


def _sim():
    return ExecutionSimulator(make_config()), Feeder()


def _rest_bid(sim, f, qty=50):
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, qty, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    return oid


def test_partial_fill_then_cancel_keeps_the_fill_and_cancels_only_the_remainder():
    sim, f = _sim()
    oid = _rest_bid(sim, f)
    sim.on_event(f.exec(T0 + 2 * MS, 0, 100, 200, 11))  # 300 ahead -> 100
    sim.on_event(f.exec(T0 + 2 * MS + 1, 0, 100, 130, 12))  # 100 ahead paid, 30 fill us
    sim.cancel(oid, T0 + 3 * MS)
    sim.on_event(f.heartbeat(T0 + 3 * MS + LAT + 1))
    o = sim.orders[oid]
    assert [x.qty for x in sim.fills] == [30]
    assert o.state == OrderState.CANCELLED and o.cancel_reason == CancelReason.USER
    assert o.remaining == 20 and sim.cancel_rejected_filled == 0


def test_cancel_arriving_after_a_full_fill_is_rejected_and_never_erases_the_fill():
    sim, f = _sim()
    oid = _rest_bid(sim, f)
    sim.cancel(oid, T0 + 1_900_000)  # decided while the order was live; lands at +2.25 ms
    sim.on_event(f.exec(T0 + 2 * MS, 0, 100, 200, 11))
    sim.on_event(f.exec(T0 + 2 * MS + 1, 0, 100, 400, 12))  # 100 ahead paid, our 50 filled
    assert sim.orders[oid].state == OrderState.FILLED
    sim.on_event(f.heartbeat(T0 + 3 * MS))
    assert sim.orders[oid].state == OrderState.FILLED and len(sim.fills) == 1
    assert sim.fills[0].qty == 50 and sim.counters.user_cancels == 0
    assert sim.cancel_rejected_filled == 1


def test_cancel_that_lands_before_the_fill_event_wins():
    sim, f = _sim()
    oid = _rest_bid(sim, f)
    sim.cancel(oid, T0 + 1 * MS)  # arrives at T0 + 1 ms + LAT
    sim.on_event(f.exec(T0 + 2 * MS, 0, 100, 200, 11))  # the cancel was effective first
    sim.on_event(f.exec(T0 + 2 * MS + 1, 0, 100, 400, 12))
    assert sim.fills == [] and sim.orders[oid].state == OrderState.CANCELLED
    assert sim.cancel_rejected_filled == 0


def test_cancel_cannot_overtake_its_own_order():
    sim, f = _sim()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.cancel(oid, T0 + 10)  # same decision time: takes effect no earlier than arrival
    assert sim.orders[oid].cancel_arrival_ts >= sim.orders[oid].arrival_ts
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].state == OrderState.CANCELLED and sim.fills == []


def test_duplicate_execute_is_dropped_and_fills_once():
    sim, f = _sim()
    oid = _rest_bid(sim, f)
    sim.on_event(f.exec(T0 + 2 * MS, 0, 100, 200, 11))
    ex = f.exec(T0 + 2 * MS + 1, 0, 100, 130, 12)
    sim.on_event(ex)
    sim.on_event(ex)  # retransmitted duplicate: the book drops it
    assert [x.qty for x in sim.fills] == [30] and sim.orders[oid].remaining == 20


def test_rejected_order_post_only_cross_is_cancelled_with_its_reason():
    sim, f = _sim()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 101, 50, T0 + 10, post_only="reject"))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    o = sim.orders[oid]
    assert o.state == OrderState.CANCELLED and o.cancel_reason == CancelReason.POST_ONLY_REJECT
    assert sim.fills == [] and sim.post_only_rejects == 1


def test_invalid_orders_are_refused_at_submit():
    sim, f = _sim()
    with pytest.raises(ValueError):
        sim.submit(child(0, OrderType.LIMIT, 100, 0, T0))
    with pytest.raises(ValueError):
        sim.submit(child(0, OrderType.LIMIT, 0, 10, T0))
    with pytest.raises(ValueError):
        sim.cancel(12345, T0)


def test_price_moves_before_arrival_leave_the_ioc_unfilled_at_its_limit():
    sim, f = _sim()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.IOC, 101, 150, T0 + 10))
    sim.on_event(f.cancel(T0 + 11, 1, 101, 200, 21))  # the 101 ask is pulled before arrival
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    o = sim.orders[oid]
    assert sim.fills == [] and o.cancel_reason == CancelReason.UNFILLED_REMAINDER
    assert o.remaining == 150 and not o.depth_exhausted  # price-bound, not depth-bound


def test_crossing_add_is_matched_by_the_book_and_the_ioc_takes_only_what_is_left():
    sim, f = _sim()
    seed_book(sim, f)
    sim.on_event(f.add(T0 + 4, 0, 101, 100, 13))  # a bid AT the ask: matched internally, 100 of 200
    book = sim.venue_book(INS, VEN)
    assert (book.best_bid(), book.best_ask()) == ((100, 300), (101, 100))
    oid = sim.submit(child(0, OrderType.IOC, 101, 150, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert [(x.qty, x.price_ticks) for x in sim.fills] == [(100, 101)]
    assert sim.orders[oid].cancel_reason == CancelReason.UNFILLED_REMAINDER


def test_crossed_display_creates_no_liquidity_the_ioc_walks_the_displayed_asks_only():
    sim, f = _sim()
    seed_book(sim, f)
    sim.on_event(f.quote(T0 + 4, 0, 102, 100))  # a quote bid ABOVE the ask: crossed display
    book = sim.venue_book(INS, VEN)
    assert book.best_bid()[0] > book.best_ask()[0]
    oid = sim.submit(child(0, OrderType.IOC, 102, 400, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    # 400 asked, 200 + 500 displayed within the limit: exactly the 400 filled, level by level
    assert [(x.qty, x.price_ticks) for x in sim.fills] == [(200, 101), (200, 102)]
    assert sim.orders[oid].state == OrderState.FILLED


def test_stale_book_blocks_every_fill_and_cancels_aggressive_arrivals():
    sim, f = _sim()
    seed_book(sim, f)
    sim.on_event(f.gap_add(T0 + 5, 1, 103, 10, 30))  # a sequence gap: the venue book is stale
    assert not sim.venue_open(sim.venue_book(INS, VEN))
    oid = sim.submit(child(0, OrderType.IOC, 105, 100, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    o = sim.orders[oid]
    assert sim.fills == [] and o.cancel_reason == CancelReason.VENUE_NOT_TRADING
    assert sim.counters.venue_not_trading_cancels == 1


def test_halted_venue_blocks_fills_like_a_gap():
    sim, f = _sim()
    seed_book(sim, f)
    sim.on_event(f.status(T0 + 5, SessionStatus.HALT))
    oid = sim.submit(child(0, OrderType.MARKET, 0, 100, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.fills == [] and sim.orders[oid].cancel_reason == CancelReason.VENUE_NOT_TRADING


def test_gapped_venue_leaves_a_capped_parent_with_owed_quantity_not_a_fill():
    f = Feeder()
    ev = _book(f) + [f.gap_add(T0 + 5, 1, 103, 10, 30)] + _beats(f, 0, 5, 3000)
    res = _run(_parent(ChildExecParams(max_levels=2, repost_interval_ns=10 * MS)), ev)
    r = res.parents[1]
    assert res.fills == [] and r.filled_qty == 0 and r.residual_qty == 1000
    # the gapped venue shows no eligible depth: counted, nothing manufactured
    assert r.cap_no_depth >= 1


def test_end_of_session_residual_is_cancelled_not_filled():
    sim, f = _sim()
    oid = _rest_bid(sim, f)
    sim.on_event(f.exec(T0 + 2 * MS, 0, 100, 200, 11))
    sim.on_event(f.exec(T0 + 2 * MS + 1, 0, 100, 110, 12))  # 100 ahead paid, 10 of our 50 fill
    sim.cancel_all()
    o = sim.orders[oid]
    assert o.cancel_reason == CancelReason.END_OF_STREAM and o.remaining == 40
    assert [x.qty for x in sim.fills] == [10]


def test_replay_window_end_expires_the_child_and_reports_the_residual():
    f = Feeder()
    ev = _book(f) + [f.add(T0 + 4, 0, 99, 1, 31)] + _beats(f, 0, 5)
    # a passive TWAP-style resting child never fills: the parent ends fully unfilled
    res = _run(
        ParentOrder(
            parent_id=1,
            instrument_id=INS,
            side=0,
            qty=100,
            algo=AlgoType.TWAP,
            start_ts=T0 + 10,
            end_ts=T0 + 10 + 20 * MS,
            slices=1,
        ),
        ev + _beats(f, 50),
    )
    r = res.parents[1]
    assert r.filled_qty == 0 and r.unfilled_qty == 100 and r.residual_qty == 100
    assert res.children[1][0].state == OrderState.CANCELLED
    assert res.children[1][0].cancel_reason in (CancelReason.EXPIRED, CancelReason.END_OF_STREAM)


# ------------------------------------------------------- the benchmark --


@pytest.fixture(scope="module")
def dataset(tmp_path_factory, golden_dir):
    import shutil

    root = tmp_path_factory.mktemp("rel04ds")
    norm = root / "normalized"
    norm.mkdir()
    for day in ("20190101", "20190102"):
        shutil.copy(golden_dir / "events_eq_mbo.jsonl", norm / f"eq_{day}.normalized.jsonl")
    return root


def _bench(dataset, **kw):
    from pathlib import Path

    from iap.research import execution_benchmark as bench

    return bench.run_benchmark(
        dataset,
        config_dir=Path(__file__).resolve().parents[2] / "configs",
        sizes_pct=(0.002,),
        algos=("IS", "POV"),
        policies=("aggressive", "aggressive+capped"),
        baseline_policy="aggressive",
        window_min=1.5,
        warmup_min=0.5,
        timestamp=False,
        **kw,
    )


def test_benchmark_capped_variant_needs_no_signal_and_pairs_with_its_base(dataset):
    doc = _bench(dataset, child_exec=ChildExecParams(max_levels=2))
    assert set(doc["cells"]) == {
        f"{p}|{a}|0.002" for p in ("aggressive", "aggressive+capped") for a in ("IS", "POV")
    }
    assert "aggressive+capped-aggressive|IS|0.002" in doc["paired_vs_baseline"]
    for cell in doc["cells"].values():
        fl = cell["failures"]
        assert {"incomplete_parents", "residual_qty", "cap_no_depth_posts", "reposts"} <= set(fl)
        assert fl["residual_qty"] >= 0 and "never filled" in fl["note"]
    cap = doc["cells"]["aggressive+capped|IS|0.002"]["failures"]
    base = doc["cells"]["aggressive|IS|0.002"]["failures"]
    # a capped child is sized to the display, so it runs out of depth no more often than
    # the 1000-share MARKET child (it can still, when the display shrinks before arrival)
    assert cap["depth_exhausted_children"] <= base["depth_exhausted_children"]
    assert cap["residual_qty"] >= 0 and base["residual_qty"] >= 0


def test_benchmark_fidelity_options_are_off_by_default_and_change_nothing_when_off(dataset):
    a = _bench(dataset)
    b = _bench(dataset, hidden_prints=False, md_delay_ns=0, peg_ahead_qty=0)
    assert a == b
    c = _bench(dataset, venue_inbound_delay_ns={"XV1": 350_000})
    assert c["grid"] == a["grid"]


def test_benchmark_peg_variant_runs_with_hidden_prints(dataset):
    from pathlib import Path

    from iap.research import execution_benchmark as bench

    doc = bench.run_benchmark(
        dataset,
        config_dir=Path(__file__).resolve().parents[2] / "configs",
        sizes_pct=(0.002,),
        algos=("IS",),
        policies=("aggressive", "aggressive+peg"),
        baseline_policy="aggressive",
        window_min=1.5,
        warmup_min=0.5,
        timestamp=False,
        hidden_prints=True,
    )
    peg = doc["cells"]["aggressive+peg|IS|0.002"]
    # a peg never takes the display: no aggressive-walk provenance in its fills
    assert "aggressive_walk" not in peg["provenance_share_of_filled"]
    assert peg["failures"]["incomplete_parents"] >= 0


def test_b8_v2_venue_file_carries_the_iex_speed_bump_and_v1_is_unchanged():
    from pathlib import Path

    from iap.execution.config import load_venues

    root = Path(__file__).resolve().parents[2] / "research" / "execution" / "b8"
    v2 = {s.name: s for s in load_venues(root / "venues_xnas_iexg_v2.json").values()}
    v1 = {s.name: s for s in load_venues(root / "venues_xnas_iexg.json").values()}
    assert v2["IEXG"].inbound_delay_ns == 350_000 and v2["XNAS"].inbound_delay_ns == 0
    assert all(s.inbound_delay_ns == 0 for s in v1.values())
    assert replace(v2["IEXG"], inbound_delay_ns=0) == v1["IEXG"]
