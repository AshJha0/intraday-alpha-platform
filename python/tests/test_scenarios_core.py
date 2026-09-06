"""Real-life market-data scenarios for the core layer (docs/SCENARIOS.md, CORE).

Each test is named after the scenario, constructs the real-world event
sequence, and pins the behaviour from PLATFORM_CONVENTIONS.md section 4 /
API_CORE.md sections 3-5, 7. The same scenarios exist in C++, Rust and Java.
"""

import json
import struct

import pytest

from conftest import CONFIGS_DIR, add, mkev
from iap.core import codec
from iap.core.events import (
    I64_MAX,
    I64_MIN,
    SYNTHETIC_ID_BASE,
    EventType,
    SessionStatus,
    Side,
    validation_error,
)
from iap.marketdata.generator import MarketDataGenerator
from iap.marketdata.normalize import DEDUP_WINDOW, _StreamQC, normalize_run
from iap.orderbook.book import (
    MAX_REORDER_WINDOW,
    ConsolidatedBook,
    OrderBook,
    synthetic_order_id,
)
from iap.reference.refdata import ReferenceData
from iap.replay.replay import ReplayEngine

BID, ASK = int(Side.BID), int(Side.ASK)


def _seeded(reorder_window: int = 0) -> OrderBook:
    """Bids 2449 (11: 100, 12: 200) / 2448 (13: 300); asks 2451 (21: 150) / 2452 (22: 250)."""
    b = OrderBook(1, 1, reorder_window)
    b.apply(add(1, BID, 2449, 100, 11))
    b.apply(add(2, BID, 2449, 200, 12))
    b.apply(add(3, BID, 2448, 300, 13))
    b.apply(add(4, ASK, 2451, 150, 21))
    b.apply(add(5, ASK, 2452, 250, 22))
    return b


def _burst(book: OrderBook, seq: int, records, ids=True, **kw) -> int:
    """Apply a complete SNAPSHOT burst starting at ``seq``; returns next seq."""
    n = len(records)
    for i, (side, price, qty, oid) in enumerate(records):
        book.apply(mkev(seq + i, EventType.SNAPSHOT, side, price, qty,
                        order_id=oid if ids else 0, trade_id=n - 1 - i, **kw))
    return seq + n


BURST = [(BID, 2450, 500, 101), (BID, 2449, 400, 102), (ASK, 2451, 600, 103)]


def _drops(book: OrderBook) -> int:
    c = book.counters()
    return sum(c[k] for k in (
        "duplicates_dropped", "dropped_while_stale", "unknown_order_events",
        "invalid_side_dropped", "invalid_payload_dropped", "unknown_type_dropped",
        "modify_price_mismatch"))


# ------------------------------------------------ #2 snapshot-after-gap variants


def test_snapshot_gap_on_first_burst_record_recovers():
    """(a) The gap lands ON the first SNAPSHOT record: burst not broken, stale clears."""
    b = _seeded()
    nxt = _burst(b, 9, BURST)  # seq 6..8 missing -> gap on the burst start
    assert b.gaps_detected == 1 and b.stale is False
    assert b.best_bid() == (2450, 500) and b.best_ask() == (2451, 600)
    b.apply(add(nxt, BID, 2450, 100, 105))
    assert b.best_bid() == (2450, 600)


def test_snapshot_gap_between_two_bursts():
    """(b) A gap between two complete bursts: the second burst recovers."""
    b = _seeded()
    nxt = _burst(b, 6, BURST)
    assert not b.stale
    nxt = _burst(b, nxt + 3, [(BID, 2447, 50, 201), (ASK, 2453, 60, 202)])
    assert b.gaps_detected == 1 and b.stale is False
    assert b.best_bid() == (2447, 50) and b.order_count_total() == 2


def test_snapshot_duplicate_inside_burst_is_ignored():
    """(c) An exact duplicate of a burst record does not disturb the burst."""
    b = _seeded()
    b.apply(add(9, BID, 2449, 100, 44))  # gap -> stale
    rec = mkev(10, EventType.SNAPSHOT, BID, 2450, 500, order_id=101, trade_id=2)
    b.apply(rec)
    b.apply(rec)  # duplicate
    b.apply(mkev(11, EventType.SNAPSHOT, BID, 2449, 400, order_id=102, trade_id=1))
    b.apply(mkev(12, EventType.SNAPSHOT, ASK, 2451, 600, order_id=103, trade_id=0))
    assert b.duplicates_dropped == 1 and b.stale is False
    assert b.order_count_total() == 3 and b.snapshot_restarts == 0


def test_snapshot_heartbeat_and_trade_interleaved_inside_burst():
    """(d) HEARTBEAT / TRADE inside a burst are applied; the burst completes."""
    b = _seeded()
    b.apply(add(9, BID, 2449, 100, 44))  # gap -> stale
    b.apply(mkev(10, EventType.SNAPSHOT, BID, 2450, 500, order_id=101, trade_id=1))
    b.apply(mkev(11, EventType.HEARTBEAT))
    b.apply(mkev(12, EventType.TRADE, BID, 2451, 30, trade_id=7))
    b.apply(mkev(13, EventType.SNAPSHOT, ASK, 2451, 600, order_id=103, trade_id=0))
    assert b.stale is False and b.trade_flow == 30
    assert b.order_count_total() == 2


# ------------------------------------------------------- #3 venue sequence reset


def test_scenario_venue_sequence_reset_daily_restart():
    """LSE/Xetra-style daily reset: seqs 1..500, then a burst from seq 1 and 2.."""
    b = OrderBook(1, 1)
    for s in range(1, 501):
        b.apply(add(s, BID if s % 2 else ASK, 2400 - (s % 7) if s % 2 else 2410 + (s % 7),
                    100, 1000 + s))
    assert b.last_sequence == 500 and not b.stale
    _burst(b, 1, BURST)  # sequence reset + recovery
    assert b.sequence_resets == 1 and b.sequence_epoch == 1
    assert b.stale is False and b.duplicates_dropped == 0
    assert b.best_bid() == (2450, 500) and b.best_ask() == (2451, 600)
    assert b.order_count_total() == 3  # pre-reset book fully replaced
    b.apply(add(4, BID, 2450, 100, 5001))
    assert b.best_bid() == (2450, 600) and b.last_sequence == 4
    # a plain duplicate is still a duplicate (not a reset)
    b.apply(add(4, BID, 2450, 100, 5002))
    assert b.duplicates_dropped == 1


def test_scenario_partition_failover_reset_marks_stale_until_burst_completes():
    """A reset burst that is itself broken leaves the book stale."""
    b = _seeded()
    b.apply(mkev(1, EventType.SNAPSHOT, BID, 2450, 500, order_id=101, trade_id=2))
    assert b.sequence_resets == 1 and b.stale is True
    b.apply(mkev(3, EventType.SNAPSHOT, ASK, 2451, 600, order_id=103, trade_id=0))  # gap inside
    assert b.stale is True and b.gaps_detected == 1
    _burst(b, 4, BURST)
    assert b.stale is False


def test_explicit_reset_sequence_api():
    b = _seeded()
    b.reset_sequence()
    assert b.stale and not b.has_sequence and b.sequence_resets == 1
    b.apply(add(1, BID, 2449, 100, 44))  # accepted (new epoch) but dropped while stale
    assert b.duplicates_dropped == 0 and b.dropped_while_stale == 1
    _burst(b, 2, BURST)
    assert not b.stale and b.last_sequence == 4


def test_scenario_two_day_capture_with_daily_reset_in_normaliser(tmp_path):
    """Day 2 restarts sequences at 1 with later timestamps: kept, not duplicates."""
    raw = tmp_path / "raw"
    raw.mkdir()
    day1 = [add(s, BID, 100, 10, s, ts=10**18 + s * 10**6) for s in range(1, 51)]
    day2 = [add(s, BID, 100, 10, 100 + s, ts=10**18 + 86400 * 10**9 + s * 10**6)
            for s in range(1, 41)]
    for evs, name in ((day1, "eq_day1.jsonl"), (day2, "eq_day2.jsonl")):
        for i, e in enumerate(evs):
            e.event_id = i + 1
        codec.write_jsonl(raw / name, evs)
    report = normalize_run(raw, tmp_path / "norm")
    c = report["per_stream"]["1:1"]
    assert c["duplicates"] == 0 and c["sequence_resets"] == 1
    assert c["out_of_order"] == 0 and c["events_out"] == 90
    out2 = codec.read_jsonl(tmp_path / "norm" / "eq_day2.normalized.jsonl")
    assert [e.sequence for e in out2] == list(range(1, 41))


# ------------------------------------- #4 in-stream timestamp regression (normaliser)


def test_scenario_matching_engine_clock_step_in_stream(tmp_path):
    """seqs 1,2,3(ts 400),4(ts 300),5: seq 4 dropped + counted, order preserved."""
    raw = tmp_path / "raw"
    raw.mkdir()
    ts = [100, 200, 400, 300, 500]
    evs = [add(s, BID, 100 - s, 10, s, ts=10**18 + t * 10**6) for s, t in zip(range(1, 6), ts)]
    for i, e in enumerate(evs):
        e.event_id = i + 1
    codec.write_jsonl(raw / "s.jsonl", evs)
    report = normalize_run(raw, tmp_path / "norm")
    c = report["per_stream"]["1:1"]
    assert c["ts_regression_dropped"] == 1 and c["events_out"] == 4
    out = codec.read_jsonl(tmp_path / "norm" / "s.normalized.jsonl")
    assert [e.sequence for e in out] == [1, 2, 3, 5]
    # the dropped record is a visible gap for the book, healed by the next burst
    book = OrderBook(1, 1)
    for e in out:
        book.apply(e)
    assert book.duplicates_dropped == 0 and book.gaps_detected == 1 and book.stale


def test_clock_skew_receive_before_exchange_is_clamped_and_counted(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    evs = [add(s, BID, 100, 10, s) for s in range(1, 4)]
    evs[1].receive_ts = evs[1].exchange_ts - 5_000
    for i, e in enumerate(evs):
        e.event_id = i + 1
    assert validation_error(evs[1]) is not None  # validator rejects the raw record
    codec.write_jsonl(raw / "s.jsonl", evs)
    report = normalize_run(raw, tmp_path / "norm")
    c = report["per_stream"]["1:1"]
    assert c["ts_clamped"] == 1 and c["events_out"] == 3 and c["invalid"] == 0
    out = codec.read_jsonl(tmp_path / "norm" / "s.normalized.jsonl")
    assert out[1].receive_ts == out[1].exchange_ts


# --------------------------------------------------------- #5 auction call phase


def test_scenario_halt_then_reopen_auction():
    """STATUS AUCTION -> crossed ADD rests -> EXECUTE uncross -> STATUS TRADING."""
    b = _seeded()
    b.apply(mkev(6, EventType.STATUS, qty=int(SessionStatus.HALT)))
    b.apply(mkev(7, EventType.STATUS, qty=int(SessionStatus.AUCTION)))
    b.apply(add(8, BID, 2452, 100, 31))  # crosses both asks: rests (call phase)
    assert b.best_bid() == (2452, 100) and b.best_ask() == (2451, 150)
    assert b.is_crossed() and b.order_count_total() == 6
    b.apply(mkev(9, EventType.EXECUTE, BID, 2452, 100, order_id=31))
    b.apply(mkev(10, EventType.EXECUTE, ASK, 2451, 100, order_id=21))
    assert not b.is_crossed() and b.unknown_order_events == 0
    assert b.best_ask() == (2451, 50) and b.best_bid() == (2449, 300)
    b.apply(mkev(11, EventType.STATUS, qty=int(SessionStatus.TRADING)))
    b.apply(add(12, BID, 2452, 100, 32))  # same ADD during TRADING executes
    assert b.best_ask() == (2452, 200) and b.best_bid() == (2449, 300)
    assert b.order_count_total() == 4 and not b.is_crossed()


def test_no_matching_while_halted_or_closed():
    for status in (SessionStatus.HALT, SessionStatus.CLOSE):
        b = _seeded()
        b.apply(mkev(6, EventType.STATUS, qty=int(status)))
        b.apply(add(7, ASK, 2448, 300, 31))
        assert b.is_crossed() and b.order_count_total() == 6
        assert b.best_bid() == (2449, 300)  # nothing consumed


# ------------------------------------------------ #6 payload-domain malformed events


def test_payload_domain_malformed_events_dropped_and_counted():
    b = _seeded()
    before = b.checkpoint()
    bad = [
        mkev(6, EventType.EXECUTE, BID, 2449, -50, order_id=11),
        mkev(7, EventType.EXECUTE, BID, 2449, 0, order_id=11),
        mkev(8, EventType.QUOTE, BID, 2449, 0, order_id=77),
        mkev(9, EventType.SNAPSHOT, BID, 0, 10, order_id=78, trade_id=0),
        mkev(10, EventType.TRADE, ASK, 2449, -1, trade_id=9),
        mkev(11, EventType.ADD, BID, 2447, 100, order_id=0),
        mkev(12, EventType.ADD, BID, 2447, 100, order_id=SYNTHETIC_ID_BASE + 1),
        mkev(13, EventType.STATUS, qty=7),
        mkev(14, EventType.CANCEL, BID, 0, 0, order_id=0),
        mkev(15, EventType.ADD, BID, -5, 100, order_id=79),
    ]
    for ev in bad:
        b.apply(ev)
    assert b.invalid_payload_dropped == len(bad)
    after = b.checkpoint()
    assert after["levels"] == before["levels"] and after["trade_flow"] == 0
    assert after["last_sequence"] == 15 and b.status == SessionStatus.TRADING
    assert b.best_bid() == (2449, 300)  # the -50 EXECUTE did not inflate anything


def test_unknown_event_type_dropped_not_raised():
    b = _seeded()
    b.apply(mkev(6, 0, BID, 2449, 100, order_id=55))
    b.apply(mkev(7, 42, BID, 2449, 100, order_id=56))
    assert b.unknown_type_dropped == 2 and b.last_sequence == 7
    b.apply(add(8, BID, 2449, 50, 57))
    assert b.gaps_detected == 0 and b.best_bid() == (2449, 350)


def test_modify_price_mismatch_dropped_and_counted():
    b = _seeded()
    b.apply(mkev(6, EventType.MODIFY, BID, 777, 10, order_id=11))
    assert b.modify_price_mismatch == 1 and b.best_bid() == (2449, 300)
    b.apply(mkev(7, EventType.MODIFY, BID, 0, 10, order_id=11))  # price 0: unchanged
    b.apply(mkev(8, EventType.MODIFY, BID, 2449, 20, order_id=11))  # matching price
    assert b.best_bid() == (2449, 220) and b.modify_price_mismatch == 1


# ------------------------------------------- #7 interrupted SNAPSHOT burst restart


def test_interrupted_snapshot_burst_restart():
    b = _seeded()
    b.apply(mkev(6, EventType.SNAPSHOT, BID, 2430, 10, order_id=301, trade_id=3))
    b.apply(mkev(7, EventType.SNAPSHOT, BID, 2429, 10, order_id=302, trade_id=2))
    _burst(b, 8, [(BID, 2440, 1, 401), (ASK, 2441, 2, 402), (ASK, 2442, 3, 403)])
    assert b.snapshot_restarts == 1 and not b.stale
    assert [o[0] for o in b.resting_orders()] == [401, 402, 403]


def test_snapshot_countdown_skip_marks_burst_broken():
    b = _seeded()
    b.apply(add(9, BID, 2449, 100, 44))  # gap -> stale
    b.apply(mkev(10, EventType.SNAPSHOT, BID, 2450, 500, order_id=101, trade_id=3))
    b.apply(mkev(11, EventType.SNAPSHOT, ASK, 2451, 600, order_id=103, trade_id=0))  # skipped 2,1
    assert b.stale is True and b.snapshot_restarts == 0
    _burst(b, 12, BURST)
    assert b.stale is False


# ----------------------------------------- #8 consolidated excludes stale / crossed


def test_scenario_bzx_stall_consolidated_nbbo_excludes_stale_venue():
    """Venue B gaps with a better (stale) bid: excluded until it recovers."""
    cons = ConsolidatedBook(1)
    cons.apply(add(1, BID, 100, 10, 11, venue_id=1))
    cons.apply(add(2, ASK, 101, 10, 12, venue_id=1))
    cons.apply(add(1, BID, 105, 10, 21, venue_id=2))
    cons.apply(add(2, ASK, 106, 10, 22, venue_id=2))
    assert cons.best_bid() == (105, 10) and cons.is_crossed()  # both fresh (crossed)
    cons.apply(add(9, BID, 107, 10, 23, venue_id=2))  # gap on venue 2 -> stale
    assert cons.stale_venues() == [2] and cons.active_venues() == [1]
    assert cons.best_bid() == (100, 10) and cons.best_ask() == (101, 10)
    assert not cons.is_crossed() and not cons.is_locked()
    assert cons.depth(BID) == [(100, 10)] and cons.order_count(ASK) == [(101, 1)]
    assert cons.venue_status(2) == SessionStatus.TRADING
    # recovery via SNAPSHOT re-admits the venue
    b2 = cons.venue_book(2)
    _burst(b2, 10, [(BID, 100, 5, 31), (ASK, 101, 5, 32)], venue_id=2)
    assert cons.active_venues() == [1, 2]
    assert cons.best_bid() == (100, 15) and cons.order_count(BID) == [(100, 2)]


def test_consolidated_locked_and_crossed_predicates():
    cons = ConsolidatedBook(1)
    cons.apply(add(1, BID, 100, 10, 11, venue_id=1))
    cons.apply(add(1, ASK, 100, 10, 21, venue_id=2))
    assert cons.is_locked() and not cons.is_crossed()
    cons.apply(add(2, ASK, 99, 10, 22, venue_id=2))
    assert cons.is_crossed()


def test_is_fresh_time_based_staleness():
    b = OrderBook(1, 1)
    assert not b.is_fresh(0, 10**9)  # no event yet
    b.apply(add(1, BID, 100, 10, 1, ts=10**18))
    assert b.is_fresh(10**18 + 150_000 + 10**9, 10**9)
    assert not b.is_fresh(10**18 + 150_000 + 2 * 10**9, 10**9)


# --------------------------------------------- #9 late retransmission (reorder window)


def test_scenario_ab_feed_retransmission_with_reorder_window():
    """seqs 1,2,3,6,4,5,7 with window 3: applied in order, never stale."""
    b = OrderBook(1, 1, reorder_window=3)
    for s in (1, 2, 3, 6, 4, 5, 7):
        b.apply(add(s, BID, 2400 + s, 10, 100 + s))
    assert b.gaps_detected == 0 and not b.stale and b.late_recovered == 2
    assert b.pending_count() == 0 and b.last_sequence == 7
    assert [o[0] for o in b.resting_orders()] == [101, 102, 103, 104, 105, 106, 107]
    assert b.events_applied == 7 and _drops(b) == 0


def test_reorder_window_zero_reproduces_stale_behaviour():
    b = OrderBook(1, 1)
    for s in (1, 2, 3, 6, 4, 5, 7):
        b.apply(add(s, BID, 2400 + s, 10, 100 + s))
    assert b.gaps_detected == 1 and b.stale and b.duplicates_dropped == 2
    assert b.dropped_while_stale == 2 and b.late_recovered == 0


def test_reorder_window_overflow_declares_gap_and_flushes_in_order():
    b = OrderBook(1, 1, reorder_window=2)
    b.apply(add(1, BID, 2401, 10, 101))
    for s in (5, 3, 6):  # 2 missing; buffer holds 5 and 3, 6 overflows it
        b.apply(add(s, BID, 2400 + s, 10, 100 + s))
    assert b.gaps_detected == 2 and b.stale and b.pending_count() == 0  # 2 and 4 missing
    assert b.last_sequence == 6 and b.dropped_while_stale == 3
    assert b.duplicates_dropped == 0


def test_reorder_window_duplicate_in_buffer_and_checkpoint_round_trip():
    b = OrderBook(1, 1, reorder_window=4)
    b.apply(add(1, BID, 2401, 10, 101))
    b.apply(add(4, BID, 2404, 10, 104))
    b.apply(add(4, BID, 2404, 10, 104))  # duplicate of a buffered event
    assert b.duplicates_dropped == 1 and b.pending_count() == 1
    cp = json.loads(json.dumps(b.checkpoint()))
    assert cp["reorder_window"] == 4 and len(cp["reorder_pending"]) == 1
    r = OrderBook.restore(cp)
    for book in (b, r):
        book.apply(add(2, BID, 2402, 10, 102))
        book.apply(add(3, BID, 2403, 10, 103))
    assert b.checkpoint() == r.checkpoint()
    assert r.pending_count() == 0 and r.late_recovered == 2 and r.last_sequence == 4


def test_reorder_window_bounds_pinned():
    OrderBook(1, 1, MAX_REORDER_WINDOW)
    with pytest.raises(ValueError):
        OrderBook(1, 1, MAX_REORDER_WINDOW + 1)
    with pytest.raises(ValueError):
        OrderBook(1, 1, -1)


# ----------------------------------------------------------- #10 first sequence 0


def test_first_sequence_zero_bootstraps():
    b = OrderBook(1, 1)
    b.apply(add(0, BID, 100, 10, 1))
    b.apply(add(1, BID, 101, 10, 2))
    b.apply(add(0, BID, 102, 10, 3))  # second 0 is a duplicate
    assert b.order_count_total() == 2 and b.duplicates_dropped == 1
    assert b.gaps_detected == 0 and not b.stale and b.has_sequence


def test_first_sequence_large_bootstraps_without_gap():
    b = OrderBook(1, 1)
    b.apply(add(1 << 63, BID, 100, 10, 1, ts=10**18))
    assert b.gaps_detected == 0 and b.last_sequence == 1 << 63


# ------------------------------------------------------------ #11 arithmetic limits


def test_arithmetic_limits_no_wrap_events_dropped_and_counted():
    b = OrderBook(1, 1)
    b.apply(mkev(1, EventType.TRADE, BID, 100, I64_MAX, trade_id=1))
    assert b.trade_flow == I64_MAX
    b.apply(mkev(2, EventType.TRADE, BID, 100, 1, trade_id=2))  # overflow: dropped
    assert b.trade_flow == I64_MAX and b.invalid_payload_dropped == 1
    b.apply(mkev(3, EventType.TRADE, ASK, 100, I64_MAX, trade_id=3))
    b.apply(mkev(4, EventType.TRADE, ASK, 100, I64_MAX, trade_id=4))
    b.apply(mkev(5, EventType.TRADE, ASK, 100, 2, trade_id=5))  # would underflow
    assert b.trade_flow == -I64_MAX and b.invalid_payload_dropped == 2
    assert I64_MIN <= b.trade_flow <= I64_MAX
    b.apply(add(6, BID, 100, I64_MAX, 1))
    b.apply(add(7, BID, 100, I64_MAX, 2))  # level total overflow: dropped
    assert b.best_bid() == (100, I64_MAX) and b.invalid_payload_dropped == 3
    b.apply(mkev(8, EventType.MODIFY, BID, 100, 1, order_id=1))
    b.apply(add(9, BID, 100, I64_MAX - 1, 3))
    assert b.best_bid() == (100, I64_MAX)
    b.apply(mkev(10, EventType.MODIFY, BID, 100, 2, order_id=1))  # +1 overflows
    assert b.invalid_payload_dropped == 4 and b.best_bid() == (100, I64_MAX)
    # sequence u64::MAX followed by another event: no wrap
    c = OrderBook(1, 1)
    c.apply(add((1 << 64) - 1, BID, 100, 10, 1, ts=10**18))
    c.apply(add(0, BID, 101, 10, 2, ts=10**18))
    assert c.duplicates_dropped == 1 and c.last_sequence == (1 << 64) - 1
    restored = OrderBook.restore(json.loads(json.dumps(b.checkpoint())))
    assert restored.checkpoint() == b.checkpoint()


# --------------------------------------------------------- #13 ids >= 2^63 in the book


def test_book_with_order_ids_above_2_63():
    base = (1 << 63) + 1
    b = OrderBook(1, 1)
    b.apply(add(1 << 63, BID, 100, 10, base, ts=10**18))
    b.apply(add((1 << 63) + 1, BID, 100, 20, base + 1, ts=10**18))
    b.apply(mkev((1 << 63) + 2, EventType.MODIFY, BID, 100, 30, order_id=base, ts=10**18))
    b.apply(mkev((1 << 63) + 3, EventType.EXECUTE, BID, 100, 5, order_id=base + 1, ts=10**18))
    assert [o[0] for o in b.resting_orders()] == [base, base + 1]
    lvl = b._best_level(BID)
    assert list(lvl.orders.items()) == [(base + 1, 15), (base, 30)]
    b.apply(mkev((1 << 63) + 4, EventType.CANCEL, BID, 100, 0, order_id=base + 1, ts=10**18))
    cp = json.loads(json.dumps(b.checkpoint()))
    assert OrderBook.restore(cp).checkpoint() == b.checkpoint()
    assert b.unknown_order_events == 0 and b.gaps_detected == 0
    # codec round trip keeps the ids exact
    ev = add((1 << 64) - 1, BID, 100, 10, (1 << 64) - 2, ts=10**18)
    assert codec.decode_jsonl_line(codec.encode_jsonl_line(ev)) == ev
    assert codec.decode_iap1(codec.encode_iap1([ev])) == [ev]


# ------------------------------------------------------- #14 reference-data fail-fast


def _cfgs():
    with open(CONFIGS_DIR / "instruments.json") as f:
        inst = json.load(f)
    with open(CONFIGS_DIR / "venues.json") as f:
        ven = json.load(f)
    return inst, ven


@pytest.mark.parametrize("patch,match", [
    ({"tick_size": 0}, "tick_size"),
    ({"tick_size": -0.01}, "tick_size"),
    ({"lot_size": 0}, "lot_size"),
    ({"lot_size": 1.5}, "lot_size"),
    ({"venues": ["XV9"]}, "not in venues.json"),
    ({"venues": ["LP1"]}, "FX venue"),
    ({"venues": []}, "at least one venue"),
    ({"ref_price": 0}, "ref_price"),
    ({"adv": -1}, "adv"),
])
def test_refdata_instrument_validation_fail_fast(patch, match):
    inst, ven = _cfgs()
    inst["instruments"][0].update(patch)
    with pytest.raises(ValueError, match=match):
        ReferenceData(inst, ven)


def test_refdata_venue_validation_fail_fast():
    inst, ven = _cfgs()
    dup = dict(ven["venues"][0])
    dup["venue"] = "XV3"
    ven["venues"].append(dup)  # duplicate venue_id 1
    with pytest.raises(ValueError, match="duplicate venue id"):
        ReferenceData(inst, ven)
    inst, ven = _cfgs()
    ven["venues"][0]["venue_id"] = 0
    with pytest.raises(ValueError, match="venue_id"):
        ReferenceData(inst, ven)
    inst, ven = _cfgs()
    inst["sessions"]["EQUITY"]["timezone"] = "Mars/Olympus"
    with pytest.raises(ValueError, match="timezone"):
        ReferenceData(inst, ven)


def test_replay_rejects_unknown_instrument_and_venue(refdata, golden_dir):
    eq = codec.read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    engine = ReplayEngine(refdata=refdata)
    engine.run(eq[:50])
    engine.apply(add(51, BID, 100, 10, 9, instrument_id=9999, venue_id=1))
    engine.apply(add(51, BID, 100, 10, 9, instrument_id=1, venue_id=10))  # FX venue
    engine.apply(add(51, BID, 100, 10, 9, instrument_id=1, venue_id=999))
    assert engine.unknown_instrument_dropped == 1 and engine.unknown_venue_dropped == 2
    assert sorted(engine.books) == [1] and sorted(engine.books[1].books) == [1]
    assert engine.events_processed == 53
    cp = json.loads(json.dumps(engine.checkpoint()))
    assert cp["universe"]["1"] == [1, 2]
    resumed = ReplayEngine.restore(cp)
    resumed.apply(add(52, BID, 100, 10, 9, instrument_id=7777, venue_id=1))
    assert resumed.unknown_instrument_dropped == 2
    with pytest.raises(ValueError):
        ReplayEngine(refdata=refdata, universe={1: {1}})


# ------------------------------------------------------ #15 corrupt record mid-file


def test_scenario_bit_flip_in_iap1_record_is_detected(golden_dir):
    eq = codec.read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    data = bytearray(codec.encode_iap1(eq))
    off = 16 + 72 * 499
    data[off + 14] = 0  # event_type of record 500 -> 0
    data[off + 55] ^= 0x80  # qty sign
    with pytest.raises(ValueError, match="CRC-32"):
        codec.decode_iap1(bytes(data))
    # a legacy v1 file cannot be verified: the book still never throws
    struct.pack_into("<I", data, 4, 1)
    legacy = codec.decode_iap1_ex(bytes(data[:-16]))
    assert legacy.integrity_checked is False
    book = OrderBook(1, 1)
    for ev in legacy.events:
        book.apply(ev)
    assert book.unknown_type_dropped == 1 and book.events_applied == 1999


# ------------------------------------------------------------ #16 memory bounds


def test_long_session_snapshot_retention_bounded(golden_dir):
    eq = codec.read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    engine = ReplayEngine(snapshot_every=1, keep_snapshots=3)
    seen = 0

    def cb(i, snap):
        nonlocal seen
        seen += 1

    for _ in range(5):
        engine.run(eq, on_snapshot=cb)  # replays: duplicates after the first pass
    assert seen == 10_000 and engine.snapshots_emitted == 10_000
    assert len(engine.snapshots) == 3
    assert [s["index"] for s in engine.snapshots] == [9998, 9999, 10_000]


def test_normaliser_dedup_state_is_bounded():
    qc = _StreamQC()
    for s in range(1, 3 * DEDUP_WINDOW + 1):
        qc.remember(s)
    assert len(qc.recent) == DEDUP_WINDOW and len(qc.recent_set) == DEDUP_WINDOW
    assert 1 not in qc.recent_set and 3 * DEDUP_WINDOW in qc.recent_set


# ------------------------------------- #18 generator: halt with re-opening auction


def test_scenario_generator_halt_with_reopening_auction(refdata):
    cfg = {
        "seed": 777, "sessions": 1,
        "equities": {"slots_per_stream": 400,
                     "halt": {"instrument": "SYN.EQ.001", "session_index": 0,
                              "duration_s": 120, "reopen_auction": True, "reopen_call_s": 30}},
        "fx": {"slots_per_pair": 10},
    }
    gen = MarketDataGenerator(refdata, cfg)
    inst = refdata.instrument("SYN.EQ.001")
    from iap.marketdata.generator import _EffPrice, _Stream
    from iap.core.rng import SplitMix64

    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", date)
    price = _EffPrice(inst, gen._rng("eqmid", 1, 0))
    price.new_session(open_ns, close_ns, gen.cfg["equities"]["vol_regimes"])
    stream = _Stream(inst, refdata.venue("XV1"))
    halt_at = open_ns + 20 * 10**9
    events = gen._eq_session_stream(stream, price, SplitMix64(1), open_ns, close_ns, 400,
                                    (halt_at, halt_at + 120 * 10**9), None)
    statuses = [(e.exchange_ts, e.qty) for e in events if e.event_type == EventType.STATUS]
    codes = [q for _, q in statuses]
    assert codes[:2] == [SessionStatus.AUCTION, SessionStatus.TRADING]
    i = codes.index(SessionStatus.HALT)
    assert codes[i:i + 3] == [SessionStatus.HALT, SessionStatus.AUCTION, SessionStatus.TRADING]
    t_halt, t_auc, t_trd = (t for t, _ in statuses[i:i + 3])
    assert t_trd - t_halt == 120 * 10**9 and t_trd - t_auc == 30 * 10**9
    call = [e for e in events if t_auc < e.exchange_ts < t_trd]
    types = [e.event_type for e in call]
    assert types[:4] == [EventType.ADD, EventType.ADD, EventType.EXECUTE, EventType.EXECUTE]
    assert all(t == EventType.TRADE for t in types[4:])
    # replaying the stream through a fresh book: the call phase is crossed,
    # nothing is matched during it, and the uncross leaves no unknown orders
    book = OrderBook(1, 1)
    crossed_during_call = False
    for e in events:
        book.apply(e)
        if t_auc < e.exchange_ts < t_trd and e.event_type == EventType.ADD:
            crossed_during_call = crossed_during_call or book.is_crossed()
    assert crossed_during_call and book.unknown_order_events == 0
    assert book.invalid_payload_dropped == 0 and not book.is_crossed()


# ------------------------------------------------------ #20 QUOTE feed without ids


def test_scenario_fx_lp_quote_feed_without_ids():
    cons = ConsolidatedBook(101)
    seqs = {10: 0, 11: 0, 12: 0}
    for k in range(10_000):
        vid = 10 + k % 3
        seqs[vid] += 1
        side = BID if (k // 3) % 2 == 0 else ASK
        price = 108650 - 3 + (k % 5) if side == BID else 108650 + 3 + (k % 5)
        cons.apply(mkev(seqs[vid], EventType.QUOTE, side, price, 5 + (k % 7),
                        order_id=0, instrument_id=101, venue_id=vid))
    for vid, book in cons.books.items():
        assert book.order_count(BID) == [(book.best_bid()[0], 1)]
        assert book.order_count(ASK) == [(book.best_ask()[0], 1)]
        ids = {o[0] for o in book.resting_orders()}
        assert ids == {synthetic_order_id(BID, 0), synthetic_order_id(ASK, 0)}
        assert _drops(book) == 0 and book.events_applied == seqs[vid]
    assert cons.best_bid()[0] == max(b.best_bid()[0] for b in cons.books.values())
    assert cons.best_ask()[0] == min(b.best_ask()[0] for b in cons.books.values())
    cp = json.loads(json.dumps(cons.checkpoint()))
    assert ConsolidatedBook.restore(cp).checkpoint() == cp


def test_quote_explicit_id_rules():
    b = OrderBook(1, 1)
    b.apply(mkev(1, EventType.QUOTE, BID, 100, 5, order_id=7))
    b.apply(mkev(2, EventType.QUOTE, ASK, 101, 7, order_id=7))  # rests on BID: dropped
    assert b.unknown_order_events == 1 and b.best_ask() is None
    b.apply(mkev(3, EventType.QUOTE, BID, 99, 4, order_id=7))  # same side: replace
    assert b.best_bid() == (99, 4) and b.order_count_total() == 1
    b.apply(mkev(4, EventType.QUOTE, ASK, 101, 7, order_id=0))
    b.apply(mkev(5, EventType.QUOTE, BID, 98, 3, order_id=0))  # replaces explicit 7
    assert b.resting_orders() == [(synthetic_order_id(ASK, 0), ASK, 101, 7),
                                  (synthetic_order_id(BID, 0), BID, 98, 3)]


def test_snapshot_with_zero_ids_assigns_deterministic_synthetic_ids():
    b = OrderBook(1, 1)
    _burst(b, 1, [(BID, 100, 5, 0), (BID, 99, 6, 0), (ASK, 101, 7, 0)], ids=False)
    assert b.resting_orders() == [
        (synthetic_order_id(BID, 0), BID, 100, 5),
        (synthetic_order_id(BID, 1), BID, 99, 6),
        (synthetic_order_id(ASK, 0), ASK, 101, 7),
    ]
    b.apply(mkev(4, EventType.EXECUTE, BID, 100, 2, order_id=synthetic_order_id(BID, 0)))
    assert b.best_bid() == (100, 3)
    # repeated explicit id inside a burst is malformed
    b.apply(mkev(5, EventType.SNAPSHOT, BID, 100, 5, order_id=9, trade_id=1))
    b.apply(mkev(6, EventType.SNAPSHOT, ASK, 101, 5, order_id=9, trade_id=0))
    assert b.unknown_order_events == 1 and b.order_count_total() == 1


# ------------------------------------------------- #21 STATUS while stale, ADD after CLOSE


def test_status_while_stale_and_add_after_close():
    b = _seeded()
    b.apply(add(9, BID, 2449, 100, 44))  # gap -> stale
    for code in (SessionStatus.HALT, SessionStatus.AUCTION, SessionStatus.CLOSE):
        b.apply(mkev(b.last_sequence + 1, EventType.STATUS, qty=int(code)))
        assert b.status == code
    _burst(b, b.last_sequence + 1, BURST)
    assert not b.stale and b.status == SessionStatus.CLOSE
    b.apply(add(b.last_sequence + 1, BID, 2452, 100, 45))  # after CLOSE: rests, no match
    assert b.best_bid() == (2452, 100) and b.is_crossed()
    assert b.order_count_total() == 4


# ---------------------------------------------------------------- #22 DST / sessions


def test_dst_session_bounds_shift_in_utc():
    inst, ven = _cfgs()
    inst["calendar"]["trading_days"] = ["2026-10-30", "2026-11-02"]
    inst["sessions"]["EQUITY"] = {"timezone": "America/New_York",
                                  "open": "09:30:00", "close": "16:00:00"}
    ref = ReferenceData(inst, ven)
    before = ref.session_bounds_ns("EQUITY", "2026-10-30")
    after = ref.session_bounds_ns("EQUITY", "2026-11-02")
    day = 3 * 86400 * 10**9
    assert after[0] - before[0] == day + 3600 * 10**9  # EDT -> EST: 1 h later in UTC
    assert after[1] - before[1] == day + 3600 * 10**9
    assert (before[1] - before[0]) == (after[1] - after[0]) == 6 * 3600 * 10**9 + 1800 * 10**9
    assert ref.session_timezone("EQUITY") == "America/New_York"
    assert ref.is_open("EQUITY", before[0]) and not ref.is_open("EQUITY", before[1])
    assert not ref.is_open("EQUITY", before[0] - 1)


def test_fx_week_sunday_open_friday_close(refdata):
    import datetime as dt

    def ts(s):
        return int(dt.datetime.fromisoformat(s).timestamp()) * 10**9

    assert not refdata.is_open("FX", ts("2026-01-03T12:00:00+00:00"))  # Saturday
    assert not refdata.is_open("FX", ts("2026-01-04T21:30:00+00:00"))  # Sun 16:30 NY
    assert refdata.is_open("FX", ts("2026-01-04T22:00:00+00:00"))      # Sun 17:00 NY (EST)
    assert refdata.is_open("FX", ts("2026-01-07T03:00:00+00:00"))      # mid-week
    assert refdata.is_open("FX", ts("2026-01-09T21:59:59+00:00"))      # Fri 16:59:59 NY
    assert not refdata.is_open("FX", ts("2026-01-09T22:00:00+00:00"))  # Fri 17:00 NY
    assert refdata.is_open("FX", ts("2026-08-23T21:00:00+00:00"))      # Sun 17:00 NY (EDT)
    assert not refdata.is_open("FX", ts("2026-08-23T20:59:59+00:00"))
    # the synthetic FX daily session stays inside the week
    o, c = refdata.session_bounds_ns("FX", "2026-08-24")
    assert refdata.is_open("FX", o) and refdata.is_open("FX", c - 1)


# ------------------------------------------------ scenario: silent venue disconnect


def test_scenario_venue_disconnect_silent_feed():
    """No sequence gap, the feed just stops: time-based freshness flags it."""
    cons = ConsolidatedBook(1)
    t0 = 10**18
    cons.apply(add(1, BID, 100, 10, 11, venue_id=1, ts=t0))
    cons.apply(add(1, BID, 99, 10, 21, venue_id=2, ts=t0))
    now = t0 + 5 * 10**9
    cons.apply(add(2, ASK, 101, 10, 12, venue_id=1, ts=now))
    fresh = [vid for vid, b in sorted(cons.books.items()) if b.is_fresh(now + 150_000, 10**9)]
    assert fresh == [1]  # venue 2 silent for 5 s
    assert cons.active_venues() == [1, 2]  # sequence-based staleness is separate
