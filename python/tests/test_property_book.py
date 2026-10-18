"""Property tests (v1.18 REL-12): order-book invariants on generated streams.

Random MBO streams with duplicate events, sequence gaps, out-of-order
timestamps, malformed payloads and status changes are applied to
``iap.orderbook.OrderBook`` and, event by event, to the independent
brute-force rebuild (tests/bruteforce_book.py).  Invariants:

* the two implementations agree on the full state summary and every QC
  counter after every event (``reorder_window == 0``, the rebuild's scope);
* no level is empty or negative, each level total is the sum of its resting
  orders and the order count matches (no negative depth, quantity conserved
  inside the book);
* resting quantity never exceeds what was ever added or increased
  (no quantity is created);
* a TRADE never changes depth;
* the accounting invariant (book.py docstring): every event ends in exactly
  one of ``events_applied`` or a drop counter, or is held;
* with a reorder window, a stream permuted inside the window ends in the
  same book as the in-order stream (out-of-order arrival is recovered).

Hypothesis runs are small and derandomized; ``IAP_HYPOTHESIS_EXAMPLES``
raises the count for a local soak.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from bruteforce_book import BruteForceBook
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.orderbook.book import COUNTER_NAMES, OrderBook

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

EXAMPLES = int(os.environ.get("IAP_HYPOTHESIS_EXAMPLES", "60"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=timedelta(seconds=5),
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

INST, VENUE = 7, 1
DROP_COUNTERS = (
    "duplicates_dropped",
    "dropped_while_stale",
    "unknown_order_events",
    "invalid_side_dropped",
    "invalid_payload_dropped",
    "unknown_type_dropped",
    "modify_price_mismatch",
)

_TYPES = [
    (EventType.ADD, 8),
    (EventType.MODIFY, 2),
    (EventType.CANCEL, 2),
    (EventType.EXECUTE, 3),
    (EventType.TRADE, 2),
    (EventType.STATUS, 1),
    (EventType.HEARTBEAT, 1),
]
_TYPE_POOL = [t for t, w in _TYPES for _ in range(w)]


@st.composite
def streams(draw, max_len: int = 60, anomalies: bool = True) -> list[MarketEvent]:
    """A random MBO stream: ids drawn from a small pool so modifies, cancels
    and executes often hit a live order; optional duplicates, gaps,
    backwards timestamps and malformed fields."""
    n = draw(st.integers(1, max_len))
    events: list[MarketEvent] = []
    seq, ts = 0, 1_000
    for i in range(n):
        etype = draw(st.sampled_from(_TYPE_POOL))
        side = draw(st.sampled_from([0, 1, 0, 1, 2] if anomalies else [0, 1]))
        price = 100 + draw(st.integers(-4, 4))
        qty = draw(st.integers(-1 if anomalies else 1, 300))
        oid = draw(st.integers(0 if anomalies else 1, 12))
        if etype == EventType.STATUS:
            qty = draw(
                st.sampled_from(
                    [int(SessionStatus.TRADING)] * 3 + [int(s) for s in SessionStatus] + [9]
                )
            )
        if etype == EventType.TRADE:
            oid = 0
        step = 1
        if anomalies:
            step = draw(st.sampled_from([1] * 12 + [0, 2, -1]))  # dup, gap, replay
        seq = max(seq + step, 0)
        ts += draw(st.integers(-500 if anomalies else 0, 1_000))  # backwards stamps allowed
        events.append(
            MarketEvent(
                event_id=i,
                instrument_id=INST,
                venue_id=VENUE,
                exchange_ts=ts,
                receive_ts=ts + 10,
                sequence=seq,
                event_type=int(etype),
                side=side,
                price_ticks=price,
                qty=qty,
                order_id=oid,
                trade_id=i if etype == EventType.TRADE else 0,
            )
        )
    return events


def _check_levels(book: OrderBook) -> None:
    for side in (0, 1):
        resting: dict[int, list[int]] = {}
        for _oid, s, price, qty in book.resting_orders(side):
            assert s == side
            assert qty > 0, "a resting order with non-positive quantity"
            resting.setdefault(price, []).append(qty)
        depth = book.depth(side, levels=1_000)
        counts = book.order_count(side, levels=1_000)
        assert {p: q for p, q in depth} == {p: sum(v) for p, v in resting.items()}
        assert {p: c for p, c in counts} == {p: len(v) for p, v in resting.items()}
        assert all(q > 0 for _, q in depth), "negative or empty depth"
        prices = [p for p, _ in depth]
        assert prices == sorted(prices, reverse=(side == 0)), "levels not best-first"


@SETTINGS
@given(events=streams())
def test_book_matches_bruteforce_and_keeps_invariants(events):
    book, brute = OrderBook(INST, VENUE), BruteForceBook()
    supplied = 0  # quantity that could ever rest: adds + modify increases
    for k, ev in enumerate(events, 1):
        depth_before = (book.depth(0, 1_000), book.depth(1, 1_000))
        before = book.order(ev.order_id) if ev.order_id else None
        book.apply(ev)
        brute.apply(ev)
        assert book.state_summary() == brute.state_summary(), f"diverged at event {k}"
        assert book.counters() == brute.counters(), f"counters diverged at event {k}"
        _check_levels(book)
        if ev.event_type == EventType.ADD and ev.qty > 0:
            supplied += ev.qty
        if ev.event_type == EventType.MODIFY and before is not None and ev.qty > before[2]:
            supplied += ev.qty - before[2]
        assert sum(q for *_, q in book.resting_orders()) <= supplied, "quantity created"
        if ev.event_type == EventType.TRADE:
            assert (book.depth(0, 1_000), book.depth(1, 1_000)) == depth_before
        drops = sum(getattr(book, c) for c in DROP_COUNTERS)
        assert book.events_applied + drops + book.pending_count() == k


@SETTINGS
@given(events=streams(max_len=40, anomalies=False), window=st.integers(1, 4), data=st.data())
def test_reorder_window_recovers_permuted_arrival(events, window, data):
    """Clean stream, arrival permuted inside blocks of ``window`` events: the
    held-back events are applied in sequence order, so the end state equals
    the in-order book."""
    ordered = OrderBook(INST, VENUE)
    for ev in events:
        ordered.apply(ev)
    arrival = list(events)
    # keep the first event first (it opens the epoch whatever its sequence)
    for start in range(1, len(arrival), window):
        block = arrival[start : start + window]
        arrival[start : start + window] = data.draw(st.permutations(block))
    held = OrderBook(INST, VENUE, reorder_window=window)
    for ev in arrival:
        held.apply(ev)
    assert held.pending_count() == 0
    assert not held.stale
    assert held.state_summary() == ordered.state_summary()
    assert held.resting_orders() == ordered.resting_orders()


@SETTINGS
@given(events=streams(max_len=40, anomalies=False), at=st.integers(1, 39))
def test_feed_gap_marks_stale_and_freezes_depth(events, at):
    """Drop one event (a feed gap) with ``reorder_window == 0``: the book goes
    stale at the next event and no later order event changes its depth."""
    if at >= len(events) - 1:
        return
    book = OrderBook(INST, VENUE)
    for ev in events[:at]:
        book.apply(ev)
    frozen = None
    for ev in events[at + 1 :]:
        book.apply(ev)
        assert book.stale
        if frozen is None:
            frozen = (book.depth(0, 1_000), book.depth(1, 1_000))
        assert (book.depth(0, 1_000), book.depth(1, 1_000)) == frozen
        _check_levels(book)
    assert book.gaps_detected == 1


def test_counter_names_cover_the_accounting_set():
    assert set(DROP_COUNTERS) <= set(COUNTER_NAMES)
