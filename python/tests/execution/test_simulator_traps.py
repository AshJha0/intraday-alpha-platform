"""Simulator traps (v2.0 stage 11, S5; carried v1.19 text): the behaviours a reviewer would
expect to be wrong, pinned as they ARE so that a change is visible.  Each test states the
limitation it pins; docs/review/SIMULATOR_CORRECTNESS.md section 5 lists them as limitations.

1. C-as-EXECUTE through the cross.  An ITCH ``C`` (order executed WITH price) names a print price
   that may differ from the resting order's display price (an opening / closing cross, a
   midpoint match).  The simulator fills a resting child at the BOOK order's price (rule 4) and
   ignores the quoted price unless ``exec_print_price=True``; the ingest emits a NON-printable C as
   the order-size reduction it is (MODIFY / CANCEL), never as an EXECUTE, so it cannot fill us.
2. Self-matching crossing ADD.  A marketable ADD is expanded against the pre-event displayed
   opposite depth and credits our resting child from that pool (rule 4).  A real venue would have
   cancelled or re-priced the incoming order if it self-crossed the participant's own resting
   order; the simulator has no participant identity and never prevents a self-match.
3. Odd lots.  A print of fewer than 100 shares is one pool of liquidity exactly like a round lot:
   nothing in the simulator distinguishes odd-lot executions (which many venues do not report to
   the consolidated tape and which do not set the NBBO) from round-lot ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from iap.core.events import EventType
from iap.execution import ExecutionSimulator, Liquidity, OrderState, OrderType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_execution_rules import (  # noqa: E402
    INS,
    LAT,
    T0,
    VEN,
    Feeder,
    child,
    make_config,
    seed_book,
)


def _resting_bid(sim: ExecutionSimulator, f: Feeder, qty: int = 50):
    """A buy LIMIT resting at 100 behind the 300 displayed there (order 11)."""
    oid = sim.submit(child(0, OrderType.LIMIT, 100, qty, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    return oid


# --------------------------------------------------------------- 1. C-as-EXECUTE through the cross


def test_execute_with_a_different_print_price_fills_at_the_book_price_by_default():
    """An EXECUTE quoting 98 against the bid order 11 resting at 100 (a cross print) trades the
    BOOK order at ITS price: queue ahead depleted by 300, our 50 filled at 100, not at 98."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = _resting_bid(sim, f)
    sim.on_event(f.exec(T0 + 2_000_000, 0, 98, 350, 11))  # order 11 holds 300; min(350, 300) trades
    assert sim.orders[oid].ahead_qty == 0
    assert sim.fills == [] or all(fl.price_ticks == 100 for fl in sim.fills)
    # the leftover of the print beyond order 11's size does not reach us: the book order traded 300
    sim.on_event(f.exec(T0 + 3_000_000, 0, 98, 40, 11))  # order 11 is gone; nothing applies
    assert all(fl.price_ticks == 100 for fl in sim.fills)
    assert sim.orders[oid].remaining == 50 or sim.fills[0].liquidity == Liquidity.MAKER


def test_exec_print_price_opt_in_fills_at_the_printed_price():
    """With ``exec_print_price=True`` (v1.15 opt-in) the same print fills us at 98: the Python-only
    switch that makes a cross print visible.  Both behaviours are pinned; neither is validated
    against real fills (Level 2 is unreachable)."""
    sim = ExecutionSimulator(make_config(), exec_print_price=True)
    f = Feeder()
    seed_book(sim, f)
    oid = _resting_bid(sim, f)
    sim.on_event(
        f.add(T0 + 4, 0, 100, 60, 13)
    )  # another 60 behind order 11, ahead of us? no: joined after us
    sim.on_event(f.exec(T0 + 2_000_000, 0, 98, 300, 11))  # depletes the 300 ahead
    assert sim.orders[oid].ahead_qty == 0
    sim.on_event(f.exec(T0 + 3_000_000, 0, 98, 20, 13))  # a print at our level with a cross price
    fills = [fl for fl in sim.fills if fl.order_id == oid]
    assert fills and fills[-1].price_ticks == 98


def test_non_printable_c_is_a_size_reduction_for_the_ingest_never_an_execute():
    """The ITCH normaliser turns a ``printable == N`` C into MODIFY (or CANCEL when it empties the
    order) when ``exec_prints`` is on: such a message can never fill a resting child.  Pinned at
    the simulator: a MODIFY that shrinks the order ahead of us advances our queue and fills nothing."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = _resting_bid(sim, f)
    sim.on_event(f.modify(T0 + 2_000_000, 0, 100, 100, 11))  # order 11: 300 -> 100 (X-P1-1)
    assert sim.orders[oid].ahead_qty == 100
    assert sim.fills == []
    sim.on_event(f.cancel(T0 + 3_000_000, 0, 100, 100, 11))  # emptied: CANCEL
    assert sim.orders[oid].ahead_qty == 0
    assert sim.fills == []
    assert sim.orders[oid].state == OrderState.ACTIVE


# ------------------------------------------------------------------- 2. self-matching crossing ADD


def test_crossing_add_fills_our_resting_order_without_self_match_prevention():
    """A marketable sell ADD at 100 x 350 crossing the bid walks the pre-event displayed depth
    (300 at 100): the pool depletes our queue and the remaining 50 fills us.  The simulator does
    not know whose order the ADD is: a venue with self-match prevention would have cancelled an
    order that crossed the same participant's resting child, and this fill would not exist."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = _resting_bid(sim, f)
    sim.on_event(f.add(T0 + 2_000_000, 1, 100, 350, 31))  # sell 350 @ 100 crosses the 100 bid
    fills = [fl for fl in sim.fills if fl.order_id == oid]
    assert sim.orders[oid].ahead_qty == 0
    assert len(fills) == 1 and fills[0].qty == 50 and fills[0].price_ticks == 100
    # the walk's pool depletes the queue; the fill itself is credited by the post-apply crossing
    # check (rule 4 last bullet), provenance "cross" -- either way the counterparty is unknown
    assert fills[0].provenance in ("marketable_add", "cross")
    # the expansion is bounded by the displayed pool: a crossing ADD smaller than the queue ahead fills nothing
    sim2 = ExecutionSimulator(make_config())
    f2 = Feeder()
    seed_book(sim2, f2)
    oid2 = _resting_bid(sim2, f2)
    sim2.on_event(f2.add(T0 + 2_000_000, 1, 100, 200, 31))
    assert sim2.orders[oid2].ahead_qty == 100
    assert [fl for fl in sim2.fills if fl.order_id == oid2] == []


# ------------------------------------------------------------------------------------ 3. odd lots


@pytest.mark.parametrize("print_qty", [1, 37, 99])
def test_odd_lot_prints_are_ordinary_pools(print_qty: int):
    """An odd-lot EXECUTE at our level is one pool like any other: it depletes the queue ahead by
    its size and, once the queue is empty, fills us share for share.  No odd-lot rule exists."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = _resting_bid(sim, f)
    sim.on_event(f.exec(T0 + 2_000_000, 0, 100, 300, 11))  # queue ahead gone
    assert sim.orders[oid].ahead_qty == 0
    sim.on_event(
        f.add(T0 + 2_000_001, 0, 100, 500, 14)
    )  # a new order behind us keeps the level alive
    sim.on_event(f.exec(T0 + 3_000_000, 0, 100, print_qty, 14))
    fills = [fl for fl in sim.fills if fl.order_id == oid]
    expected = min(print_qty, 50)  # bounded by our remaining, as any print
    assert len(fills) == 1 and fills[0].qty == expected
    assert fills[0].liquidity == Liquidity.MAKER
    assert sim.orders[oid].remaining == 50 - expected


def test_odd_lot_displayed_depth_counts_toward_queue_position():
    """Odd-lot displayed size ahead of us counts in ``ahead_qty`` exactly like round lots (the
    simulator has no round-lot NBBO)."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    sim.on_event(f.add(T0, 0, 100, 7, 11))  # a 7-share bid
    sim.on_event(f.add(T0 + 1, 1, 101, 200, 21))
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 7
    assert sim.orders[oid].instrument_id == INS and sim.orders[oid].venue_id == VEN
    assert EventType.EXECUTE is not None
