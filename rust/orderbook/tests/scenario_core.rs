//! Real-life market-data scenarios for the core layer (docs/SCENARIOS.md,
//! CORE). Mirrors `python/tests/test_scenarios_core.py`: every test is named
//! after the scenario and pins PLATFORM_CONVENTIONS.md §4 / API_CORE.md §4.

use marketdata::{EventType, MarketEvent, SessionStatus, Side, SYNTHETIC_ID_BASE};
use orderbook::{synthetic_order_id, ConsolidatedBook, OrderBook, MAX_REORDER_WINDOW};

const BID: u8 = Side::Bid as u8;
const ASK: u8 = Side::Ask as u8;
const TS0: i64 = 1_700_000_000_000_000_000;

#[allow(clippy::too_many_arguments)]
fn mk(
    seq: u64,
    et: u8,
    side: u8,
    price: i64,
    qty: i64,
    order_id: u64,
    trade_id: u64,
    venue: u16,
    ts: i64,
) -> MarketEvent {
    let ts = if ts == 0 {
        TS0 + (seq % 1_000_000) as i64 * 1_000_000
    } else {
        ts
    };
    MarketEvent {
        event_id: seq,
        instrument_id: 1,
        venue_id: venue,
        exchange_ts: ts,
        receive_ts: ts + 150_000,
        sequence: seq,
        event_type: et,
        side,
        price_ticks: price,
        qty,
        order_id,
        trade_id,
    }
}

fn ev(seq: u64, et: EventType, side: u8, price: i64, qty: i64, oid: u64, tid: u64) -> MarketEvent {
    mk(seq, et.as_u8(), side, price, qty, oid, tid, 1, 0)
}

fn add(seq: u64, side: u8, price: i64, qty: i64, oid: u64) -> MarketEvent {
    ev(seq, EventType::Add, side, price, qty, oid, 0)
}

fn status(seq: u64, code: SessionStatus) -> MarketEvent {
    ev(seq, EventType::Status, 0, 0, code as i64, 0, 0)
}

const BURST: [(u8, i64, i64, u64); 3] = [(BID, 2450, 500, 101), (BID, 2449, 400, 102), (ASK, 2451, 600, 103)];

fn burst(b: &mut OrderBook, seq: u64, recs: &[(u8, i64, i64, u64)], ids: bool, venue: u16) -> u64 {
    let n = recs.len() as u64;
    for (i, &(side, price, qty, oid)) in recs.iter().enumerate() {
        let e = mk(seq + i as u64, EventType::Snapshot.as_u8(), side, price, qty,
                   if ids { oid } else { 0 }, n - 1 - i as u64, venue, 0);
        b.apply(&e).unwrap();
    }
    seq + n
}

/// Bids 2449 (11: 100, 12: 200) / 2448 (13: 300); asks 2451 (21: 150) / 2452 (22: 250).
fn seeded(window: usize) -> OrderBook {
    let mut b = OrderBook::with_reorder_window(1, 1, window).unwrap();
    b.apply(&add(1, BID, 2449, 100, 11)).unwrap();
    b.apply(&add(2, BID, 2449, 200, 12)).unwrap();
    b.apply(&add(3, BID, 2448, 300, 13)).unwrap();
    b.apply(&add(4, ASK, 2451, 150, 21)).unwrap();
    b.apply(&add(5, ASK, 2452, 250, 22)).unwrap();
    b
}

fn ids(b: &OrderBook) -> Vec<u64> {
    b.resting_orders(None).into_iter().map(|o| o.0).collect()
}

// ------------------------------------------------ #2 snapshot-after-gap variants

#[test]
fn snapshot_gap_on_first_burst_record_recovers() {
    let mut b = seeded(0);
    let nxt = burst(&mut b, 9, &BURST, true, 1);
    assert_eq!(b.counters.gaps_detected, 1);
    assert!(!b.stale);
    assert_eq!(b.best_bid(), Some((2450, 500)));
    assert_eq!(b.best_ask(), Some((2451, 600)));
    b.apply(&add(nxt, BID, 2450, 100, 105)).unwrap();
    assert_eq!(b.best_bid(), Some((2450, 600)));
}

#[test]
fn snapshot_gap_between_two_bursts() {
    let mut b = seeded(0);
    let nxt = burst(&mut b, 6, &BURST, true, 1);
    assert!(!b.stale);
    burst(&mut b, nxt + 3, &[(BID, 2447, 50, 201), (ASK, 2453, 60, 202)], true, 1);
    assert_eq!(b.counters.gaps_detected, 1);
    assert!(!b.stale);
    assert_eq!(b.best_bid(), Some((2447, 50)));
    assert_eq!(b.order_count_total(), 2);
}

#[test]
fn snapshot_duplicate_inside_burst_is_ignored() {
    let mut b = seeded(0);
    b.apply(&add(9, BID, 2449, 100, 44)).unwrap();
    let rec = ev(10, EventType::Snapshot, BID, 2450, 500, 101, 2);
    b.apply(&rec).unwrap();
    b.apply(&rec).unwrap();
    b.apply(&ev(11, EventType::Snapshot, BID, 2449, 400, 102, 1)).unwrap();
    b.apply(&ev(12, EventType::Snapshot, ASK, 2451, 600, 103, 0)).unwrap();
    assert_eq!(b.counters.duplicates_dropped, 1);
    assert!(!b.stale);
    assert_eq!(b.order_count_total(), 3);
    assert_eq!(b.counters.snapshot_restarts, 0);
}

#[test]
fn snapshot_heartbeat_and_trade_interleaved_inside_burst() {
    let mut b = seeded(0);
    b.apply(&add(9, BID, 2449, 100, 44)).unwrap();
    b.apply(&ev(10, EventType::Snapshot, BID, 2450, 500, 101, 1)).unwrap();
    b.apply(&ev(11, EventType::Heartbeat, 0, 0, 0, 0, 0)).unwrap();
    b.apply(&ev(12, EventType::Trade, BID, 2451, 30, 0, 7)).unwrap();
    b.apply(&ev(13, EventType::Snapshot, ASK, 2451, 600, 103, 0)).unwrap();
    assert!(!b.stale);
    assert_eq!(b.trade_flow, 30);
    assert_eq!(b.order_count_total(), 2);
}

// ------------------------------------------------------- #3 venue sequence reset

#[test]
fn scenario_venue_sequence_reset_daily_restart() {
    let mut b = OrderBook::new(1, 1);
    for s in 1..=500u64 {
        let bid = s % 2 == 1;
        let price = if bid { 2400 - (s % 7) as i64 } else { 2410 + (s % 7) as i64 };
        b.apply(&add(s, if bid { BID } else { ASK }, price, 100, 1000 + s)).unwrap();
    }
    assert_eq!(b.last_sequence, 500);
    assert!(!b.stale);
    burst(&mut b, 1, &BURST, true, 1);
    assert_eq!(b.counters.sequence_resets, 1);
    assert_eq!(b.sequence_epoch, 1);
    assert!(!b.stale);
    assert_eq!(b.counters.duplicates_dropped, 0);
    assert_eq!(b.best_bid(), Some((2450, 500)));
    assert_eq!(b.order_count_total(), 3);
    b.apply(&add(4, BID, 2450, 100, 5001)).unwrap();
    assert_eq!(b.best_bid(), Some((2450, 600)));
    assert_eq!(b.last_sequence, 4);
    b.apply(&add(4, BID, 2450, 100, 5002)).unwrap();
    assert_eq!(b.counters.duplicates_dropped, 1);
}

#[test]
fn scenario_partition_failover_reset_stays_stale_until_burst_completes() {
    let mut b = seeded(0);
    b.apply(&ev(1, EventType::Snapshot, BID, 2450, 500, 101, 2)).unwrap();
    assert_eq!(b.counters.sequence_resets, 1);
    assert!(b.stale);
    b.apply(&ev(3, EventType::Snapshot, ASK, 2451, 600, 103, 0)).unwrap(); // gap inside
    assert!(b.stale);
    assert_eq!(b.counters.gaps_detected, 1);
    burst(&mut b, 4, &BURST, true, 1);
    assert!(!b.stale);
}

#[test]
fn scenario_sequence_reset_explicit_api() {
    let mut b = seeded(0);
    b.reset_sequence();
    assert!(b.stale && !b.has_sequence);
    assert_eq!(b.counters.sequence_resets, 1);
    b.apply(&add(1, BID, 2449, 100, 44)).unwrap();
    assert_eq!(b.counters.duplicates_dropped, 0);
    assert_eq!(b.counters.dropped_while_stale, 1);
    burst(&mut b, 2, &BURST, true, 1);
    assert!(!b.stale);
    assert_eq!(b.last_sequence, 4);
}

// --------------------------------------------------------- #5 auction call phase

#[test]
fn scenario_halt_then_reopen_auction() {
    let mut b = seeded(0);
    b.apply(&status(6, SessionStatus::Halt)).unwrap();
    b.apply(&status(7, SessionStatus::Auction)).unwrap();
    b.apply(&add(8, BID, 2452, 100, 31)).unwrap(); // crosses both asks: rests
    assert_eq!(b.best_bid(), Some((2452, 100)));
    assert_eq!(b.best_ask(), Some((2451, 150)));
    assert!(b.is_crossed());
    assert_eq!(b.order_count_total(), 6);
    b.apply(&ev(9, EventType::Execute, BID, 2452, 100, 31, 0)).unwrap();
    b.apply(&ev(10, EventType::Execute, ASK, 2451, 100, 21, 0)).unwrap();
    assert!(!b.is_crossed());
    assert_eq!(b.counters.unknown_order_events, 0);
    assert_eq!(b.best_ask(), Some((2451, 50)));
    assert_eq!(b.best_bid(), Some((2449, 300)));
    b.apply(&status(11, SessionStatus::Trading)).unwrap();
    b.apply(&add(12, BID, 2452, 100, 32)).unwrap(); // executes during TRADING
    assert_eq!(b.best_ask(), Some((2452, 200)));
    assert_eq!(b.best_bid(), Some((2449, 300)));
    assert_eq!(b.order_count_total(), 4);
}

#[test]
fn no_matching_while_halted_or_closed() {
    for code in [SessionStatus::Halt, SessionStatus::Close] {
        let mut b = seeded(0);
        b.apply(&status(6, code)).unwrap();
        b.apply(&add(7, ASK, 2448, 300, 31)).unwrap();
        assert!(b.is_crossed());
        assert_eq!(b.order_count_total(), 6);
        assert_eq!(b.best_bid(), Some((2449, 300)));
    }
}

// ------------------------------------------------ #6 payload-domain malformed events

#[test]
fn payload_domain_malformed_events_dropped_and_counted() {
    let mut b = seeded(0);
    let before = b.checkpoint();
    let bad = [
        ev(6, EventType::Execute, BID, 2449, -50, 11, 0),
        ev(7, EventType::Execute, BID, 2449, 0, 11, 0),
        ev(8, EventType::Quote, BID, 2449, 0, 77, 0),
        ev(9, EventType::Snapshot, BID, 0, 10, 78, 0),
        ev(10, EventType::Trade, ASK, 2449, -1, 0, 9),
        ev(11, EventType::Add, BID, 2447, 100, 0, 0),
        ev(12, EventType::Add, BID, 2447, 100, SYNTHETIC_ID_BASE + 1, 0),
        ev(13, EventType::Status, 0, 0, 7, 0, 0),
        ev(14, EventType::Cancel, BID, 0, 0, 0, 0),
        ev(15, EventType::Add, BID, -5, 100, 79, 0),
    ];
    for e in &bad {
        b.apply(e).unwrap();
    }
    assert_eq!(b.counters.invalid_payload_dropped, bad.len() as u64);
    let after = b.checkpoint();
    assert_eq!(after.levels, before.levels);
    assert_eq!(after.trade_flow, 0);
    assert_eq!(after.last_sequence, 15);
    assert_eq!(b.status, SessionStatus::Trading as i64);
    assert_eq!(b.best_bid(), Some((2449, 300)));
}

#[test]
fn modify_price_mismatch_dropped_and_counted() {
    let mut b = seeded(0);
    b.apply(&ev(6, EventType::Modify, BID, 777, 10, 11, 0)).unwrap();
    assert_eq!(b.counters.modify_price_mismatch, 1);
    assert_eq!(b.best_bid(), Some((2449, 300)));
    b.apply(&ev(7, EventType::Modify, BID, 0, 10, 11, 0)).unwrap();
    b.apply(&ev(8, EventType::Modify, BID, 2449, 20, 11, 0)).unwrap();
    assert_eq!(b.best_bid(), Some((2449, 220)));
}

// ------------------------------------------- #7 interrupted SNAPSHOT burst restart

#[test]
fn interrupted_snapshot_burst_restart() {
    let mut b = seeded(0);
    b.apply(&ev(6, EventType::Snapshot, BID, 2430, 10, 301, 3)).unwrap();
    b.apply(&ev(7, EventType::Snapshot, BID, 2429, 10, 302, 2)).unwrap();
    burst(&mut b, 8, &[(BID, 2440, 1, 401), (ASK, 2441, 2, 402), (ASK, 2442, 3, 403)], true, 1);
    assert_eq!(b.counters.snapshot_restarts, 1);
    assert!(!b.stale);
    assert_eq!(ids(&b), vec![401, 402, 403]);
}

#[test]
fn snapshot_countdown_skip_marks_burst_broken() {
    let mut b = seeded(0);
    b.apply(&add(9, BID, 2449, 100, 44)).unwrap();
    b.apply(&ev(10, EventType::Snapshot, BID, 2450, 500, 101, 3)).unwrap();
    b.apply(&ev(11, EventType::Snapshot, ASK, 2451, 600, 103, 0)).unwrap();
    assert!(b.stale);
    assert_eq!(b.counters.snapshot_restarts, 0);
    burst(&mut b, 12, &BURST, true, 1);
    assert!(!b.stale);
}

// ----------------------------------------- #8 consolidated excludes stale / crossed

#[test]
fn scenario_bzx_stall_consolidated_nbbo_excludes_stale_venue() {
    let mut cons = ConsolidatedBook::new(1);
    cons.apply(&mk(1, 1, BID, 100, 10, 11, 0, 1, 0)).unwrap();
    cons.apply(&mk(2, 1, ASK, 101, 10, 12, 0, 1, 0)).unwrap();
    cons.apply(&mk(1, 1, BID, 105, 10, 21, 0, 2, 0)).unwrap();
    cons.apply(&mk(2, 1, ASK, 106, 10, 22, 0, 2, 0)).unwrap();
    assert_eq!(cons.best_bid(), Some((105, 10)));
    assert!(cons.is_crossed());
    cons.apply(&mk(9, 1, BID, 107, 10, 23, 0, 2, 0)).unwrap(); // gap on venue 2
    assert_eq!(cons.stale_venues(), vec![2]);
    assert_eq!(cons.active_venues(), vec![1]);
    assert_eq!(cons.best_bid(), Some((100, 10)));
    assert_eq!(cons.best_ask(), Some((101, 10)));
    assert!(!cons.is_crossed() && !cons.is_locked());
    assert_eq!(cons.depth(BID, 10), vec![(100, 10)]);
    assert_eq!(cons.order_count(ASK, 10), vec![(101, 1)]);
    assert_eq!(cons.venue_status(2), Some(SessionStatus::Trading as i64));
    assert_eq!(cons.venue_status(9), None);
    burst(cons.venue_book(2), 10, &[(BID, 100, 5, 31), (ASK, 101, 5, 32)], true, 2);
    assert_eq!(cons.active_venues(), vec![1, 2]);
    assert_eq!(cons.best_bid(), Some((100, 15)));
    assert_eq!(cons.order_count(BID, 10), vec![(100, 2)]);
}

#[test]
fn scenario_venue_disconnect_silent_feed() {
    let mut cons = ConsolidatedBook::new(1);
    cons.apply(&mk(1, 1, BID, 100, 10, 11, 0, 1, TS0)).unwrap();
    cons.apply(&mk(1, 1, BID, 99, 10, 21, 0, 2, TS0)).unwrap();
    let now = TS0 + 5_000_000_000;
    cons.apply(&mk(2, 1, ASK, 101, 10, 12, 0, 1, now)).unwrap();
    let fresh: Vec<u16> = cons
        .books
        .iter()
        .filter(|(_, b)| b.is_fresh(now + 150_000, 1_000_000_000))
        .map(|(&v, _)| v)
        .collect();
    assert_eq!(fresh, vec![1]);
    assert_eq!(cons.active_venues(), vec![1, 2]);
}

// --------------------------------------------- #9 late retransmission (reorder window)

#[test]
fn scenario_ab_feed_retransmission_with_reorder_window() {
    let mut b = OrderBook::with_reorder_window(1, 1, 3).unwrap();
    for s in [1u64, 2, 3, 6, 4, 5, 7] {
        b.apply(&add(s, BID, 2400 + s as i64, 10, 100 + s)).unwrap();
    }
    assert_eq!(b.counters.gaps_detected, 0);
    assert!(!b.stale);
    assert_eq!(b.counters.late_recovered, 2);
    assert_eq!(b.pending_count(), 0);
    assert_eq!(b.last_sequence, 7);
    assert_eq!(ids(&b), vec![101, 102, 103, 104, 105, 106, 107]);
    assert_eq!(b.counters.events_applied, 7);
    assert_eq!(b.counters.drops(), 0);
}

#[test]
fn reorder_window_zero_reproduces_stale_behaviour() {
    let mut b = OrderBook::new(1, 1);
    for s in [1u64, 2, 3, 6, 4, 5, 7] {
        b.apply(&add(s, BID, 2400 + s as i64, 10, 100 + s)).unwrap();
    }
    assert_eq!(b.counters.gaps_detected, 1);
    assert!(b.stale);
    assert_eq!(b.counters.duplicates_dropped, 2);
    assert_eq!(b.counters.dropped_while_stale, 2);
    assert_eq!(b.counters.late_recovered, 0);
}

#[test]
fn reorder_window_overflow_declares_gap_and_flushes_in_order() {
    let mut b = OrderBook::with_reorder_window(1, 1, 2).unwrap();
    b.apply(&add(1, BID, 2401, 10, 101)).unwrap();
    for s in [5u64, 3, 6] {
        b.apply(&add(s, BID, 2400 + s as i64, 10, 100 + s)).unwrap();
    }
    assert_eq!(b.counters.gaps_detected, 2);
    assert!(b.stale);
    assert_eq!(b.pending_count(), 0);
    assert_eq!(b.last_sequence, 6);
    assert_eq!(b.counters.dropped_while_stale, 3);
}

#[test]
fn reorder_window_duplicate_in_buffer_and_checkpoint_round_trip() {
    let mut b = OrderBook::with_reorder_window(1, 1, 4).unwrap();
    b.apply(&add(1, BID, 2401, 10, 101)).unwrap();
    b.apply(&add(4, BID, 2404, 10, 104)).unwrap();
    b.apply(&add(4, BID, 2404, 10, 104)).unwrap();
    assert_eq!(b.counters.duplicates_dropped, 1);
    assert_eq!(b.pending_count(), 1);
    let json = serde_json::to_string(&b.checkpoint()).unwrap();
    let cp: orderbook::BookCheckpoint = serde_json::from_str(&json).unwrap();
    assert_eq!(cp.reorder_pending.len(), 1);
    let mut r = OrderBook::restore(&cp).unwrap();
    for book in [&mut b, &mut r] {
        book.apply(&add(2, BID, 2402, 10, 102)).unwrap();
        book.apply(&add(3, BID, 2403, 10, 103)).unwrap();
    }
    assert_eq!(b.checkpoint(), r.checkpoint());
    assert_eq!(r.pending_count(), 0);
    assert_eq!(r.counters.late_recovered, 2);
    assert_eq!(r.last_sequence, 4);
}

#[test]
fn reorder_window_bounds_pinned() {
    assert!(OrderBook::with_reorder_window(1, 1, MAX_REORDER_WINDOW).is_ok());
    assert!(OrderBook::with_reorder_window(1, 1, MAX_REORDER_WINDOW + 1).is_err());
}

// ----------------------------------------------------------- #10 first sequence 0

#[test]
fn first_sequence_zero_bootstraps() {
    let mut b = OrderBook::new(1, 1);
    b.apply(&add(0, BID, 100, 10, 1)).unwrap();
    b.apply(&add(1, BID, 101, 10, 2)).unwrap();
    b.apply(&add(0, BID, 102, 10, 3)).unwrap();
    assert_eq!(b.order_count_total(), 2);
    assert_eq!(b.counters.duplicates_dropped, 1);
    assert_eq!(b.counters.gaps_detected, 0);
    assert!(!b.stale && b.has_sequence);
}

// ------------------------------------------------------------ #11 arithmetic limits

#[test]
fn arithmetic_limits_no_panic_events_dropped_and_counted() {
    let mut b = OrderBook::new(1, 1);
    b.apply(&ev(1, EventType::Trade, BID, 100, i64::MAX, 0, 1)).unwrap();
    assert_eq!(b.trade_flow, i64::MAX);
    b.apply(&ev(2, EventType::Trade, BID, 100, 1, 0, 2)).unwrap();
    assert_eq!(b.trade_flow, i64::MAX);
    assert_eq!(b.counters.invalid_payload_dropped, 1);
    b.apply(&ev(3, EventType::Trade, ASK, 100, i64::MAX, 0, 3)).unwrap();
    b.apply(&ev(4, EventType::Trade, ASK, 100, i64::MAX, 0, 4)).unwrap();
    b.apply(&ev(5, EventType::Trade, ASK, 100, 2, 0, 5)).unwrap();
    assert_eq!(b.trade_flow, -i64::MAX);
    assert_eq!(b.counters.invalid_payload_dropped, 2);
    b.apply(&add(6, BID, 100, i64::MAX, 1)).unwrap();
    b.apply(&add(7, BID, 100, i64::MAX, 2)).unwrap();
    assert_eq!(b.best_bid(), Some((100, i64::MAX)));
    assert_eq!(b.counters.invalid_payload_dropped, 3);
    b.apply(&ev(8, EventType::Modify, BID, 100, 1, 1, 0)).unwrap();
    b.apply(&add(9, BID, 100, i64::MAX - 1, 3)).unwrap();
    b.apply(&ev(10, EventType::Modify, BID, 100, 2, 1, 0)).unwrap();
    assert_eq!(b.counters.invalid_payload_dropped, 4);
    assert_eq!(b.best_bid(), Some((100, i64::MAX)));
    // TRADE with qty i64::MIN never panics (qty <= 0 is invalid payload).
    b.apply(&ev(11, EventType::Trade, ASK, 100, i64::MIN, 0, 11)).unwrap();
    assert_eq!(b.counters.invalid_payload_dropped, 5);
    let mut c = OrderBook::new(1, 1);
    c.apply(&mk(u64::MAX, 1, BID, 100, 10, 1, 0, 1, TS0)).unwrap();
    c.apply(&mk(0, 1, BID, 101, 10, 2, 0, 1, TS0)).unwrap();
    assert_eq!(c.counters.duplicates_dropped, 1);
    assert_eq!(c.last_sequence, u64::MAX);
    assert_eq!(OrderBook::restore(&b.checkpoint()).unwrap().checkpoint(), b.checkpoint());
}

#[test]
fn consolidated_trade_flow_saturates() {
    let mut cons = ConsolidatedBook::new(1);
    cons.apply(&mk(1, 5, BID, 100, i64::MAX, 0, 1, 1, 0)).unwrap();
    cons.apply(&mk(1, 5, BID, 100, i64::MAX, 0, 1, 2, 0)).unwrap();
    assert_eq!(cons.trade_flow(), i64::MAX);
    cons.apply(&mk(2, 5, ASK, 100, i64::MAX, 0, 2, 2, 0)).unwrap();
    cons.apply(&mk(3, 5, ASK, 100, 5, 0, 3, 2, 0)).unwrap();
    assert_eq!(cons.trade_flow(), i64::MAX - 5);
}

// --------------------------------------------------------- #13 ids >= 2^63 in the book

#[test]
fn book_with_order_ids_above_2_pow_63() {
    let base = (1u64 << 63) + 1;
    let s0 = 1u64 << 63;
    let mut b = OrderBook::new(1, 1);
    b.apply(&mk(s0, 1, BID, 100, 10, base, 0, 1, TS0)).unwrap();
    b.apply(&mk(s0 + 1, 1, BID, 100, 20, base + 1, 0, 1, TS0)).unwrap();
    b.apply(&mk(s0 + 2, 2, BID, 100, 30, base, 0, 1, TS0)).unwrap();
    b.apply(&mk(s0 + 3, 4, BID, 100, 5, base + 1, 0, 1, TS0)).unwrap();
    assert_eq!(ids(&b), vec![base, base + 1]);
    let cp = b.checkpoint();
    assert_eq!(cp.levels[0].orders, vec![(base + 1, 15), (base, 30)]);
    b.apply(&mk(s0 + 4, 3, BID, 100, 0, base + 1, 0, 1, TS0)).unwrap();
    let json = serde_json::to_string(&b.checkpoint()).unwrap();
    let back: orderbook::BookCheckpoint = serde_json::from_str(&json).unwrap();
    assert_eq!(OrderBook::restore(&back).unwrap().checkpoint(), b.checkpoint());
    assert_eq!(b.counters.unknown_order_events, 0);
    assert_eq!(b.counters.gaps_detected, 0);
}

// ------------------------------------------------------ #20 QUOTE feed without ids

#[test]
fn scenario_fx_lp_quote_feed_without_ids() {
    let mut cons = ConsolidatedBook::new(1);
    let mut seqs = [0u64; 3];
    for k in 0..10_000usize {
        let vi = k % 3;
        let vid = 10 + vi as u16;
        seqs[vi] += 1;
        let side = if (k / 3) % 2 == 0 { BID } else { ASK };
        let price = if side == BID { 108650 - 3 + (k % 5) as i64 } else { 108650 + 3 + (k % 5) as i64 };
        cons.apply(&mk(seqs[vi], 6, side, price, 5 + (k % 7) as i64, 0, 0, vid, 0)).unwrap();
    }
    for (vid, book) in &cons.books {
        assert_eq!(book.order_count(BID, 10), vec![(book.best_bid().unwrap().0, 1)]);
        assert_eq!(book.order_count(ASK, 10), vec![(book.best_ask().unwrap().0, 1)]);
        let mut got = ids(book);
        got.sort_unstable();
        assert_eq!(got, vec![synthetic_order_id(BID, 0), synthetic_order_id(ASK, 0)]);
        assert_eq!(book.counters.drops(), 0);
        assert_eq!(book.counters.events_applied, seqs[(*vid - 10) as usize]);
    }
    let cp = cons.checkpoint();
    assert_eq!(ConsolidatedBook::restore(&cp).unwrap().checkpoint(), cp);
}

#[test]
fn quote_explicit_id_rules() {
    let mut b = OrderBook::new(1, 1);
    b.apply(&ev(1, EventType::Quote, BID, 100, 5, 7, 0)).unwrap();
    b.apply(&ev(2, EventType::Quote, ASK, 101, 7, 7, 0)).unwrap(); // rests on BID: dropped
    assert_eq!(b.counters.unknown_order_events, 1);
    assert_eq!(b.best_ask(), None);
    b.apply(&ev(3, EventType::Quote, BID, 99, 4, 7, 0)).unwrap(); // same side: replace
    assert_eq!(b.best_bid(), Some((99, 4)));
    assert_eq!(b.order_count_total(), 1);
    b.apply(&ev(4, EventType::Quote, ASK, 101, 7, 0, 0)).unwrap();
    b.apply(&ev(5, EventType::Quote, BID, 98, 3, 0, 0)).unwrap();
    assert_eq!(
        b.resting_orders(None),
        vec![(synthetic_order_id(ASK, 0), ASK, 101, 7), (synthetic_order_id(BID, 0), BID, 98, 3)]
    );
}

#[test]
fn snapshot_with_zero_ids_assigns_deterministic_synthetic_ids() {
    let mut b = OrderBook::new(1, 1);
    burst(&mut b, 1, &[(BID, 100, 5, 0), (BID, 99, 6, 0), (ASK, 101, 7, 0)], false, 1);
    assert_eq!(
        b.resting_orders(None),
        vec![
            (synthetic_order_id(BID, 0), BID, 100, 5),
            (synthetic_order_id(BID, 1), BID, 99, 6),
            (synthetic_order_id(ASK, 0), ASK, 101, 7),
        ]
    );
    b.apply(&ev(4, EventType::Execute, BID, 100, 2, synthetic_order_id(BID, 0), 0)).unwrap();
    assert_eq!(b.best_bid(), Some((100, 3)));
    b.apply(&ev(5, EventType::Snapshot, BID, 100, 5, 9, 1)).unwrap();
    b.apply(&ev(6, EventType::Snapshot, ASK, 101, 5, 9, 0)).unwrap();
    assert_eq!(b.counters.unknown_order_events, 1);
    assert_eq!(b.order_count_total(), 1);
}

// ------------------------------------------------- #21 STATUS while stale, ADD after CLOSE

#[test]
fn status_while_stale_and_add_after_close() {
    let mut b = seeded(0);
    b.apply(&add(9, BID, 2449, 100, 44)).unwrap();
    for code in [SessionStatus::Halt, SessionStatus::Auction, SessionStatus::Close] {
        b.apply(&status(b.last_sequence + 1, code)).unwrap();
        assert_eq!(b.status, code as i64);
    }
    let nxt = b.last_sequence + 1;
    burst(&mut b, nxt, &BURST, true, 1);
    assert!(!b.stale);
    assert_eq!(b.status, SessionStatus::Close as i64);
    let nxt = b.last_sequence + 1;
    b.apply(&add(nxt, BID, 2452, 100, 45)).unwrap();
    assert_eq!(b.best_bid(), Some((2452, 100)));
    assert!(b.is_crossed());
    assert_eq!(b.order_count_total(), 4);
}

// ------------------------------------------------------------- hot-path complexity

#[test]
fn remove_order_is_constant_time_on_large_books() {
    // 50k resting orders, then 50k cancels of the OLDEST orders (worst case
    // for a Vec-based arrival list): must run comfortably fast.
    let mut b = OrderBook::new(1, 1);
    let n = 50_000u64;
    for s in 1..=n {
        b.apply(&add(s, BID, 1000 + (s % 200) as i64, 10, s)).unwrap();
    }
    let start = std::time::Instant::now();
    for s in 1..=n {
        b.apply(&ev(n + s, EventType::Cancel, BID, 0, 0, s, 0)).unwrap();
    }
    assert_eq!(b.order_count_total(), 0);
    assert!(start.elapsed().as_secs_f64() < 2.0, "cancels took {:?}", start.elapsed());
}

// ------------------------------------------------------ #15 corrupt record mid-file

#[test]
fn scenario_bit_flip_in_iap1_record_is_detected_and_book_never_panics() {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/events_eq_mbo.jsonl");
    let events = marketdata::read_jsonl(path).unwrap();
    let mut data = marketdata::encode_iap1(&events);
    let off = 16 + 72 * 499;
    data[off + 14] = 0; // event_type of record 500 -> 0
    data[off + 55] ^= 0x80; // qty sign
    assert!(marketdata::decode_iap1(&data).is_err());
    // A legacy v1 file cannot be verified: the book still never errors.
    let n = data.len();
    data.truncate(n - 16);
    data[4] = 1;
    let legacy = marketdata::decode_iap1_ex(&data).unwrap();
    assert!(!legacy.integrity_checked);
    let mut book = OrderBook::new(1, 1);
    for e in &legacy.events {
        book.apply(e).unwrap();
    }
    assert_eq!(book.counters.unknown_type_dropped, 1);
    assert_eq!(book.counters.events_applied, 1999);
}
