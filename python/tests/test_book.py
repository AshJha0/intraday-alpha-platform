"""Order-book semantics tests — pinned behavior from conventions section 4."""

import pytest

from conftest import add, mkev
from iap.core.events import EventType, SessionStatus, Side
from iap.orderbook.book import ConsolidatedBook, OrderBook


def _book():
    return OrderBook(1, 1)


def _seeded():
    """Book with bids 2449/2448 and asks 2451/2452, two orders at best bid."""
    b = _book()
    b.apply(add(1, Side.BID, 2449, 100, 11))
    b.apply(add(2, Side.BID, 2449, 200, 12))
    b.apply(add(3, Side.BID, 2448, 300, 13))
    b.apply(add(4, Side.ASK, 2451, 150, 21))
    b.apply(add(5, Side.ASK, 2452, 250, 22))
    return b


def test_add_sets_best_bid_ask():
    b = _seeded()
    assert b.best_bid() == (2449, 300)
    assert b.best_ask() == (2451, 150)


def test_fifo_execute_hits_first_order():
    b = _seeded()
    b.apply(mkev(6, EventType.EXECUTE, Side.BID, 2449, 100, order_id=11))
    # order 11 (first in) fully filled; order 12 remains alone at the level
    assert b.best_bid() == (2449, 200)
    assert b.order_count(Side.BID, 1) == [(2449, 1)]
    assert b.resting_orders(Side.BID)[0][0] in (12, 13)


def test_partial_execute_reduces_head():
    b = _seeded()
    b.apply(mkev(6, EventType.EXECUTE, Side.BID, 2449, 40, order_id=11))
    assert b.best_bid() == (2449, 260)
    assert b.order_count(Side.BID, 1) == [(2449, 2)]


def test_modify_decrease_keeps_queue_position():
    b = _seeded()
    b.apply(mkev(6, EventType.MODIFY, Side.BID, 2449, 50, order_id=11))
    assert b.best_bid() == (2449, 250)
    # Head of the FIFO at 2449 must still be order 11: execute head via FIFO
    lvl = b._best_level(Side.BID)
    assert next(iter(lvl.orders)) == 11


def test_modify_increase_moves_to_tail():
    b = _seeded()
    b.apply(mkev(6, EventType.MODIFY, Side.BID, 2449, 500, order_id=11))
    lvl = b._best_level(Side.BID)
    assert list(lvl.orders) == [12, 11]  # 11 moved behind 12
    assert b.best_bid() == (2449, 700)


def test_cancel_removes_order_and_empty_level():
    b = _seeded()
    b.apply(mkev(6, EventType.CANCEL, Side.BID, 2448, 0, order_id=13))
    assert b.depth(Side.BID) == [(2449, 300)]
    assert b.order_count_total() == 4


def test_crossing_add_executes_marketable_with_leftover():
    b = _seeded()
    # Buy 200 @ 2451 crosses the 150 ask; leftover 50 posts at 2451.
    b.apply(add(6, Side.BID, 2451, 200, 31))
    assert b.best_ask() == (2452, 250)
    assert b.best_bid() == (2451, 50)
    assert 21 not in dict((o[0], o) for o in b.resting_orders())


def test_crossing_add_fully_consumed_does_not_post():
    b = _seeded()
    b.apply(add(6, Side.ASK, 2449, 300, 31))  # sell 300 into 100+200 bids
    assert b.best_bid() == (2448, 300)
    assert b.best_ask() == (2451, 150)  # nothing posted at 2449
    assert b.order_count_total() == 3


def test_crossing_add_walks_multiple_levels():
    b = _seeded()
    b.apply(add(6, Side.ASK, 2448, 550, 31))  # consumes 2449 (300) + 2448 (250 of 300)
    assert b.best_bid() == (2448, 50)
    assert b.best_ask() == (2451, 150)


def test_trade_updates_signed_flow_only():
    b = _seeded()
    depth_before = (b.depth(Side.BID), b.depth(Side.ASK))
    b.apply(mkev(6, EventType.TRADE, Side.BID, 2451, 70, trade_id=1))
    b.apply(mkev(7, EventType.TRADE, Side.ASK, 2449, 30, trade_id=2))
    assert b.trade_flow == 40
    assert (b.depth(Side.BID), b.depth(Side.ASK)) == depth_before


def test_execute_does_not_touch_trade_flow():
    b = _seeded()
    b.apply(mkev(6, EventType.EXECUTE, Side.BID, 2449, 100, order_id=11))
    assert b.trade_flow == 0


def test_duplicate_sequence_dropped_and_counted():
    b = _seeded()
    dup = add(5, Side.ASK, 2452, 250, 99)  # sequence 5 already applied
    b.apply(dup)
    assert b.duplicates_dropped == 1
    assert b.order_count_total() == 5  # nothing changed


def test_gap_marks_stale_and_drops_book_events():
    b = _seeded()
    b.apply(add(8, Side.BID, 2449, 100, 44))  # gap: seq 6,7 missing
    assert b.stale is True
    assert b.gaps_detected == 1
    assert b.dropped_while_stale == 1
    assert b.best_bid() == (2449, 300)  # untouched
    # TRADE and STATUS still apply while stale.
    b.apply(mkev(9, EventType.TRADE, Side.BID, 2451, 10, trade_id=5))
    assert b.trade_flow == 10
    b.apply(mkev(10, EventType.STATUS, qty=int(SessionStatus.HALT)))
    assert b.status == SessionStatus.HALT


def test_snapshot_recovery_rebuilds_book_and_clears_stale():
    b = _seeded()
    b.apply(add(9, Side.BID, 2449, 100, 44))  # gap -> stale
    assert b.stale
    burst = [
        (Side.BID, 2450, 500, 101),
        (Side.BID, 2449, 400, 102),
        (Side.ASK, 2451, 600, 103),
    ]
    for i, (side, price, qty, oid) in enumerate(burst):
        b.apply(
            mkev(10 + i, EventType.SNAPSHOT, side, price, qty, order_id=oid,
                 trade_id=len(burst) - 1 - i)
        )
    assert b.stale is False
    assert b.best_bid() == (2450, 500)
    assert b.best_ask() == (2451, 600)
    assert b.order_count_total() == 3  # pre-gap orders replaced by snapshot
    # Post-recovery flow applies normally again.
    b.apply(add(13, Side.BID, 2450, 100, 105))
    assert b.best_bid() == (2450, 600)


def test_side_domain_invalid_dropped_and_counted():
    """side > 1 on side-indexed types => dropped + counted, never raised."""
    b = _seeded()
    depth_before = (b.depth(Side.BID), b.depth(Side.ASK))
    b.apply(mkev(6, EventType.ADD, 9, 2449, 100, order_id=77))
    b.apply(mkev(7, EventType.QUOTE, 5, 2450, 10, order_id=78))
    b.apply(mkev(8, EventType.TRADE, 9, 2451, 30, trade_id=9))
    b.apply(mkev(9, EventType.SNAPSHOT, 3, 2451, 30, order_id=79, trade_id=0))
    assert b.invalid_side_dropped == 4
    assert (b.depth(Side.BID), b.depth(Side.ASK)) == depth_before
    assert b.trade_flow == 0
    assert b.order_count_total() == 5
    # sequence numbers were consumed: the next in-order event applies cleanly
    b.apply(add(10, Side.BID, 2449, 50, 80))
    assert b.gaps_detected == 0 and not b.stale
    assert b.best_bid() == (2449, 350)
    # MODIFY/CANCEL/EXECUTE address by order_id (not side-indexed): a bogus
    # side field does not block them
    b.apply(mkev(11, EventType.MODIFY, 9, 2449, 75, order_id=80))
    assert b.best_bid() == (2449, 375)
    assert b.invalid_side_dropped == 4


def test_mid_burst_gap_marks_burst_broken():
    """A gap inside a SNAPSHOT burst leaves stale set at burst completion."""
    b = _seeded()
    b.apply(add(9, Side.BID, 2449, 100, 44))  # gap -> stale
    assert b.stale
    # burst of 4 records; the record with trade_id == 1 goes missing
    b.apply(mkev(10, EventType.SNAPSHOT, Side.BID, 2450, 500, order_id=101,
                 trade_id=3))
    b.apply(mkev(11, EventType.SNAPSHOT, Side.BID, 2449, 400, order_id=102,
                 trade_id=2))
    b.apply(mkev(13, EventType.SNAPSHOT, Side.ASK, 2452, 700, order_id=104,
                 trade_id=0))  # seq 12 missing: gap INSIDE the burst
    assert b.gaps_detected == 2
    assert b.stale is True  # broken burst must NOT clear stale
    # book events stay blocked until a complete burst arrives
    b.apply(add(14, Side.BID, 2450, 100, 105))
    assert b.dropped_while_stale == 2
    # a subsequent complete burst with no interior gap recovers
    burst = [
        (Side.BID, 2450, 500, 111),
        (Side.BID, 2449, 400, 112),
        (Side.ASK, 2451, 600, 113),
    ]
    for i, (side, price, qty, oid) in enumerate(burst):
        b.apply(mkev(15 + i, EventType.SNAPSHOT, side, price, qty,
                     order_id=oid, trade_id=len(burst) - 1 - i))
    assert b.stale is False
    assert b.best_bid() == (2450, 500)
    assert b.best_ask() == (2451, 600)
    assert b.order_count_total() == 3


def test_broken_burst_state_survives_checkpoint_restore():
    b = _seeded()
    b.apply(add(9, Side.BID, 2449, 100, 44))  # gap -> stale
    b.apply(mkev(10, EventType.SNAPSHOT, Side.BID, 2450, 500, order_id=101,
                 trade_id=2))
    b.apply(mkev(12, EventType.SNAPSHOT, Side.BID, 2449, 400, order_id=102,
                 trade_id=1))  # gap inside the burst
    restored = OrderBook.restore(b.checkpoint())
    for book in (b, restored):
        book.apply(mkev(13, EventType.SNAPSHOT, Side.ASK, 2451, 600,
                        order_id=103, trade_id=0))
    assert b.stale is True and restored.stale is True
    assert b.checkpoint() == restored.checkpoint()


def test_quote_replaces_whole_side_at_l1():
    b = _book()
    b.apply(mkev(1, EventType.QUOTE, Side.BID, 108650, 5, order_id=1))
    b.apply(mkev(2, EventType.QUOTE, Side.ASK, 108652, 7, order_id=2))
    b.apply(mkev(3, EventType.QUOTE, Side.BID, 108648, 9, order_id=3))
    assert b.best_bid() == (108648, 9)  # old bid fully replaced
    assert b.depth(Side.BID) == [(108648, 9)]
    assert b.best_ask() == (108652, 7)


def test_status_event_recorded():
    b = _book()
    b.apply(mkev(1, EventType.STATUS, qty=int(SessionStatus.AUCTION)))
    assert b.status == SessionStatus.AUCTION


def test_depth_top10_sorted_best_first():
    b = _book()
    for i in range(15):
        b.apply(add(i + 1, Side.BID, 2400 + i, 100, 100 + i))
    d = b.depth(Side.BID)
    assert len(d) == 10
    assert d[0][0] == 2414
    assert [p for p, _ in d] == sorted([p for p, _ in d], reverse=True)


def test_order_count_per_level():
    b = _seeded()
    assert b.order_count(Side.BID, 2) == [(2449, 2), (2448, 1)]


def test_unknown_order_events_counted_not_fatal():
    b = _seeded()
    b.apply(mkev(6, EventType.CANCEL, Side.BID, 2449, 0, order_id=777))
    b.apply(mkev(7, EventType.MODIFY, Side.BID, 2449, 100, order_id=888))
    b.apply(mkev(8, EventType.EXECUTE, Side.BID, 2449, 100, order_id=999))
    assert b.unknown_order_events == 3
    assert b.best_bid() == (2449, 300)


def test_empty_side_returns_none():
    b = _book()
    assert b.best_bid() is None and b.best_ask() is None
    assert b.depth(Side.BID) == []


def test_wrong_routing_raises():
    b = _book()
    with pytest.raises(ValueError, match="wrong book"):
        b.apply(add(1, Side.BID, 10, 10, 1, instrument_id=2))


def test_checkpoint_restore_identical_continuation():
    b = _seeded()
    b.apply(mkev(6, EventType.TRADE, Side.BID, 2451, 70, trade_id=1))
    cp = b.checkpoint()
    restored = OrderBook.restore(cp)
    assert restored.checkpoint() == cp
    follow = [
        mkev(7, EventType.EXECUTE, Side.BID, 2449, 100, order_id=11),
        mkev(8, EventType.MODIFY, Side.ASK, 2452, 400, order_id=22),
        add(9, Side.ASK, 2450, 120, 33),
    ]
    for ev in follow:
        b.apply(ev)
        restored.apply(ev)
    assert b.checkpoint() == restored.checkpoint()
    assert b.state_summary() == restored.state_summary()


def test_checkpoint_is_json_serializable():
    import json

    cp = _seeded().checkpoint()
    assert json.loads(json.dumps(cp)) == cp


def test_consolidated_merges_venues():
    cons = ConsolidatedBook(1)
    cons.apply(add(1, Side.BID, 2449, 100, 11, venue_id=1))
    cons.apply(add(1, Side.BID, 2449, 200, 21, venue_id=2))  # same price, venue 2
    cons.apply(add(2, Side.BID, 2450, 50, 22, venue_id=2))
    cons.apply(add(2, Side.ASK, 2451, 70, 12, venue_id=1))
    assert cons.best_bid() == (2450, 50)
    assert cons.depth(Side.BID) == [(2450, 50), (2449, 300)]
    assert cons.order_count(Side.BID) == [(2450, 1), (2449, 2)]
    assert cons.best_ask() == (2451, 70)
    # Per-venue books remain independent (sequences per venue).
    assert cons.venue_book(1).last_sequence == 2
    assert cons.venue_book(2).last_sequence == 2


def test_consolidated_trade_flow_and_checkpoint():
    cons = ConsolidatedBook(1)
    cons.apply(mkev(1, EventType.TRADE, Side.BID, 2450, 100, trade_id=1, venue_id=1))
    cons.apply(mkev(1, EventType.TRADE, Side.ASK, 2450, 30, trade_id=1, venue_id=2))
    assert cons.trade_flow() == 70
    cp = cons.checkpoint()
    restored = ConsolidatedBook.restore(cp)
    assert restored.checkpoint() == cp
    assert restored.trade_flow() == 70
