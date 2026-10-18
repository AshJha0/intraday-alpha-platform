"""Outside review finding 5 (2026-10-18): the simulator questions that had no
direct test. Synthetic, deterministic, hand-built books; the full map of
question -> test is docs/review/SIMULATOR_CORRECTNESS.md.

Book (``seed_book``): bids 100x300 (order 11), 99x400 (12); asks 101x200 (21),
102x500 (22).
"""

from __future__ import annotations

from iap.core.events import EventType
from iap.execution import ExecutionSimulator, OrderState, OrderType
from test_execution_rules import INS, LAT, T0, VEN, Feeder, child, make_config, seed_book

MS = 1_000_000


def _rest_bid(qty=50):
    sim, f = ExecutionSimulator(make_config()), Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, qty, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    return sim, f, oid


def _gap(sim, f):
    sim.on_event(f.gap_add(T0 + 2 * MS, 1, 103, 10, 30))  # skipped sequence: stale
    assert not sim.venue_open(sim.venue_book(INS, VEN))


def test_trade_print_through_our_price_while_stale_never_fills_a_resting_order():
    sim, f, oid = _rest_bid()
    _gap(sim, f)
    # TRADE prints still reach the book while stale; one trades through our bid.
    sim.on_event(f.ev(T0 + 3 * MS, EventType.TRADE, 0, 99, 1000, 0, tid=7))
    sim.on_event(f.exec(T0 + 3 * MS + 1, 0, 100, 300, 11))  # dropped by the stale book
    assert sim.fills == []
    assert sim.orders[oid].state == OrderState.ACTIVE and sim.orders[oid].remaining == 50
    assert sim.orders[oid].ahead_qty == 300  # nothing seen while stale advanced us


def test_snapshot_recovery_keeps_the_pre_gap_queue_and_treats_snapshot_orders_as_ahead():
    sim, f, oid = _rest_bid()
    _gap(sim, f)
    burst = [(0, 100, 250, 901), (0, 99, 400, 902), (1, 101, 200, 903)]
    for i, (side, px, qty, sid) in enumerate(burst):
        sim.on_event(f.snapshot(T0 + 4 * MS + i, side, px, qty, sid, len(burst) - 1 - i))
    assert sim.venue_open(sim.venue_book(INS, VEN))
    # Our pre-gap position is kept: no credit for what may have traded in the gap,
    # even though the recovered level shows only 250 (conservative).
    assert sim.orders[oid].ahead_qty == 300
    # Snapshot orders carry real MBO ids and did not join after us, so they are
    # assumed AHEAD: executing all 250 advances us to 50 ahead, still no fill.
    sim.on_event(f.exec(T0 + 5 * MS, 0, 100, 250, 901))
    assert sim.fills == [] and sim.orders[oid].ahead_qty == 50
    assert sim.orders[oid].state == OrderState.ACTIVE and sim.orders[oid].remaining == 50


def test_fill_quantity_never_exceeds_the_print_across_two_of_our_resting_orders():
    sim, f, a = _rest_bid(qty=50)
    b = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 20))
    sim.on_event(f.heartbeat(T0 + 20 + LAT + 1))
    # one 330-share execute of the order ahead: 300 ahead of a, then 30 for a, none for b
    sim.on_event(f.exec(T0 + 2 * MS, 0, 100, 300, 11))
    sim.on_event(f.ev(T0 + 2 * MS + 1, EventType.TRADE, 0, 99, 30, 0, tid=8))
    total = sum(x.qty for x in sim.fills)
    assert total <= 30
    assert all(x.order_id == a for x in sim.fills)
    assert sim.orders[b].remaining == 50
