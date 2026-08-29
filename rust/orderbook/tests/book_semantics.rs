//! Unit tests over the pinned book semantics and QC counters
//! (PLATFORM_CONVENTIONS.md §4 / API_CORE.md §4).

use marketdata::{EventType, MarketEvent, SessionStatus, Side};
use orderbook::{ConsolidatedBook, OrderBook};

const BID: u8 = Side::Bid as u8;
const ASK: u8 = Side::Ask as u8;

/// Event builder: auto-incrementing sequence/event_id on one venue stream.
struct Feed {
    book_venue: u16,
    seq: u64,
}

impl Feed {
    fn new(venue: u16) -> Feed {
        Feed {
            book_venue: venue,
            seq: 0,
        }
    }

    fn ev(&mut self, event_type: EventType, side: u8, price: i64, qty: i64, order_id: u64) -> MarketEvent {
        self.seq += 1;
        MarketEvent {
            event_id: self.seq,
            instrument_id: 1,
            venue_id: self.book_venue,
            exchange_ts: 1_000 + self.seq as i64,
            receive_ts: 2_000 + self.seq as i64,
            sequence: self.seq,
            event_type: event_type.as_u8(),
            side,
            price_ticks: price,
            qty,
            order_id,
            trade_id: 0,
        }
    }

    fn add(&mut self, side: u8, price: i64, qty: i64, order_id: u64) -> MarketEvent {
        self.ev(EventType::Add, side, price, qty, order_id)
    }
}

fn apply(book: &mut OrderBook, ev: MarketEvent) {
    book.apply(&ev).expect("apply must succeed");
}

fn book_with_feed() -> (OrderBook, Feed) {
    (OrderBook::new(1, 1), Feed::new(1))
}

#[test]
fn add_builds_levels_and_best() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 101, 20, 2));
    apply(&mut book, f.add(ASK, 105, 30, 3));
    apply(&mut book, f.add(ASK, 104, 5, 4));
    assert_eq!(book.best_bid(), Some((101, 20)));
    assert_eq!(book.best_ask(), Some((104, 5)));
    assert_eq!(book.depth(BID, 10), vec![(101, 20), (100, 10)]);
    assert_eq!(book.depth(ASK, 10), vec![(104, 5), (105, 30)]);
    assert_eq!(book.order_count_total(), 4);
}

#[test]
fn add_same_price_is_fifo_tail() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 100, 20, 2));
    apply(&mut book, f.add(BID, 100, 30, 3));
    assert_eq!(
        book.resting_orders(Some(BID)),
        vec![(1, BID, 100, 10), (2, BID, 100, 20), (3, BID, 100, 30)]
    );
    assert_eq!(book.order_count(BID, 10), vec![(100, 3)]);
}

#[test]
fn duplicate_order_id_add_dropped_and_counted() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 101, 20, 1)); // same order_id
    assert_eq!(book.best_bid(), Some((100, 10)));
    assert_eq!(book.counters.unknown_order_events, 1);
    assert_eq!(book.counters.events_applied, 2); // still dispatched
}

#[test]
fn crossing_add_executes_partial_head() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(ASK, 105, 100, 1));
    // Buy 40 @ 105 crosses; head reduced, no leftover posts.
    apply(&mut book, f.add(BID, 105, 40, 2));
    assert_eq!(book.best_ask(), Some((105, 60)));
    assert_eq!(book.best_bid(), None);
}

#[test]
fn crossing_add_walks_levels_and_posts_leftover() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(ASK, 105, 10, 1));
    apply(&mut book, f.add(ASK, 106, 10, 2));
    apply(&mut book, f.add(ASK, 107, 10, 3));
    // Buy 25 @ 106: clears 105 and 106, leftover 5 posts at 106.
    apply(&mut book, f.add(BID, 106, 25, 4));
    assert_eq!(book.best_ask(), Some((107, 10)));
    assert_eq!(book.best_bid(), Some((106, 5)));
    assert_eq!(book.resting_orders(Some(BID)), vec![(4, BID, 106, 5)]);
}

#[test]
fn crossing_add_respects_fifo_within_level() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(ASK, 105, 10, 1));
    apply(&mut book, f.add(ASK, 105, 10, 2));
    // Sell-side FIFO: order 1 filled first.
    apply(&mut book, f.add(BID, 105, 15, 3));
    assert_eq!(book.resting_orders(Some(ASK)), vec![(2, ASK, 105, 5)]);
}

#[test]
fn ask_crossing_bid_side_executes() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 99, 10, 2));
    // Sell 15 @ 99 clears bid 100 and half of 99.
    apply(&mut book, f.add(ASK, 99, 15, 3));
    assert_eq!(book.best_bid(), Some((99, 5)));
    assert_eq!(book.best_ask(), None);
}

#[test]
fn modify_decrease_keeps_queue_position() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 100, 20, 2));
    let m = f.ev(EventType::Modify, BID, 100, 5, 1);
    apply(&mut book, m);
    assert_eq!(
        book.resting_orders(Some(BID)),
        vec![(1, BID, 100, 5), (2, BID, 100, 20)]
    );
    assert_eq!(book.best_bid(), Some((100, 25)));
}

#[test]
fn modify_increase_moves_to_tail() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 100, 20, 2));
    let m = f.ev(EventType::Modify, BID, 100, 15, 1);
    apply(&mut book, m);
    // Level FIFO: order 1 moved to the tail of its level (checkpoint order).
    let cp = book.checkpoint();
    assert_eq!(cp.levels[0].orders, vec![(2, 20), (1, 15)]);
    // Global arrival order is untouched by MODIFY (mirrors the reference).
    assert_eq!(
        book.resting_orders(Some(BID)),
        vec![(1, BID, 100, 15), (2, BID, 100, 20)]
    );
    assert_eq!(book.best_bid(), Some((100, 35)));
}

#[test]
fn modify_ignores_event_price_and_nonpositive_removes() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    // Price field 999 ignored: order stays at level 100.
    let m = f.ev(EventType::Modify, BID, 999, 4, 1);
    apply(&mut book, m);
    assert_eq!(book.best_bid(), Some((100, 4)));
    // qty <= 0 removes the order.
    let m = f.ev(EventType::Modify, BID, 100, 0, 1);
    apply(&mut book, m);
    assert_eq!(book.best_bid(), None);
    assert_eq!(book.order_count_total(), 0);
}

#[test]
fn modify_unknown_order_counted() {
    let (mut book, mut f) = book_with_feed();
    let m = f.ev(EventType::Modify, BID, 100, 5, 42);
    apply(&mut book, m);
    assert_eq!(book.counters.unknown_order_events, 1);
}

#[test]
fn cancel_removes_and_unknown_counted() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    let c = f.ev(EventType::Cancel, BID, 0, 0, 1);
    apply(&mut book, c);
    assert_eq!(book.best_bid(), None);
    let c = f.ev(EventType::Cancel, BID, 0, 0, 99);
    apply(&mut book, c);
    assert_eq!(book.counters.unknown_order_events, 1);
}

#[test]
fn execute_partial_keeps_position_full_removes() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(ASK, 105, 10, 1));
    apply(&mut book, f.add(ASK, 105, 20, 2));
    let e = f.ev(EventType::Execute, ASK, 105, 4, 1);
    apply(&mut book, e);
    assert_eq!(
        book.resting_orders(Some(ASK)),
        vec![(1, ASK, 105, 6), (2, ASK, 105, 20)]
    );
    // Over-fill clamps to the order's qty and removes it.
    let e = f.ev(EventType::Execute, ASK, 105, 100, 1);
    apply(&mut book, e);
    assert_eq!(book.resting_orders(Some(ASK)), vec![(2, ASK, 105, 20)]);
    assert_eq!(book.trade_flow, 0); // EXECUTE never touches trade_flow
}

#[test]
fn execute_unknown_order_counted() {
    let (mut book, mut f) = book_with_feed();
    let e = f.ev(EventType::Execute, BID, 100, 5, 7);
    apply(&mut book, e);
    assert_eq!(book.counters.unknown_order_events, 1);
}

#[test]
fn trade_updates_signed_flow_only() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    let mut t = f.ev(EventType::Trade, BID, 100, 300, 0);
    t.trade_id = 1;
    apply(&mut book, t);
    let mut t = f.ev(EventType::Trade, ASK, 100, 120, 0);
    t.trade_id = 2;
    apply(&mut book, t);
    assert_eq!(book.trade_flow, 180);
    assert_eq!(book.best_bid(), Some((100, 10))); // book untouched
}

#[test]
fn quote_replaces_whole_side_at_l1() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 99, 10, 2));
    apply(&mut book, f.add(ASK, 105, 10, 3));
    let q = f.ev(EventType::Quote, BID, 101, 7, 50);
    apply(&mut book, q);
    assert_eq!(book.depth(BID, 10), vec![(101, 7)]);
    assert_eq!(book.resting_orders(Some(BID)), vec![(50, BID, 101, 7)]);
    // Ask side untouched.
    assert_eq!(book.best_ask(), Some((105, 10)));
}

#[test]
fn snapshot_burst_clears_book_and_stale() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    // Force a gap => stale.
    f.seq += 5;
    apply(&mut book, f.add(BID, 101, 10, 2));
    assert!(book.stale);
    assert_eq!(book.counters.gaps_detected, 1);
    assert_eq!(book.counters.dropped_while_stale, 1);
    assert_eq!(book.best_bid(), Some((100, 10))); // ADD dropped while stale

    // 3-record snapshot burst (trade_id = records remaining after this one).
    let mut s1 = f.ev(EventType::Snapshot, BID, 102, 5, 11);
    s1.trade_id = 2;
    let mut s2 = f.ev(EventType::Snapshot, BID, 101, 4, 12);
    s2.trade_id = 1;
    let mut s3 = f.ev(EventType::Snapshot, ASK, 103, 6, 13);
    s3.trade_id = 0;
    apply(&mut book, s1);
    assert!(book.stale); // burst not complete yet
    assert_eq!(book.best_bid(), Some((102, 5))); // old book cleared
    apply(&mut book, s2);
    apply(&mut book, s3);
    assert!(!book.stale);
    assert_eq!(book.depth(BID, 10), vec![(102, 5), (101, 4)]);
    assert_eq!(book.best_ask(), Some((103, 6)));

    // Normal flow resumes after recovery.
    apply(&mut book, f.add(ASK, 104, 9, 14));
    assert_eq!(book.depth(ASK, 10), vec![(103, 6), (104, 9)]);
}

#[test]
fn invalid_side_dropped_and_counted_never_panics() {
    // side > 1 on side-indexed types (ADD/QUOTE/SNAPSHOT/TRADE) is dropped +
    // counted (`invalid_side_dropped`) after sequence consumption. This must
    // hold in debug builds too: no `1 - side` underflow panic, no phantom
    // level, apply() returns Ok.
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(ASK, 105, 20, 2));
    let depth_before = (book.depth(BID, 10), book.depth(ASK, 10));

    book.apply(&f.add(9, 104, 100, 77)).expect("no panic, Ok");
    let q = f.ev(EventType::Quote, 5, 101, 10, 78);
    book.apply(&q).expect("no panic, Ok");
    let mut t = f.ev(EventType::Trade, 9, 102, 30, 0);
    t.trade_id = 9;
    book.apply(&t).expect("no panic, Ok");
    let mut s = f.ev(EventType::Snapshot, 3, 103, 30, 79);
    s.trade_id = 0;
    book.apply(&s).expect("no panic, Ok");

    assert_eq!(book.counters.invalid_side_dropped, 4);
    assert_eq!((book.depth(BID, 10), book.depth(ASK, 10)), depth_before);
    assert_eq!(book.trade_flow, 0); // invalid-side TRADE never signed the flow
    assert_eq!(book.order_count_total(), 2); // no phantom level/order
    assert_eq!(book.counters.events_applied, 2); // dropped events not applied

    // Sequence numbers were consumed: the next in-order event applies cleanly.
    apply(&mut book, f.add(BID, 100, 50, 80));
    assert_eq!(book.counters.gaps_detected, 0);
    assert!(!book.stale);
    assert_eq!(book.best_bid(), Some((100, 60)));

    // MODIFY/CANCEL/EXECUTE address by order_id (not side-indexed): a bogus
    // side field does not block them.
    let m = f.ev(EventType::Modify, 9, 100, 75, 80);
    apply(&mut book, m);
    assert_eq!(book.best_bid(), Some((100, 85)));
    assert_eq!(book.counters.invalid_side_dropped, 4);
}

#[test]
fn mid_burst_gap_marks_burst_broken() {
    // A gap inside a SNAPSHOT burst leaves `stale` set at burst completion;
    // only a later complete gap-free burst recovers.
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    f.seq += 2; // gap -> stale
    apply(&mut book, f.add(BID, 101, 10, 2));
    assert!(book.stale);

    // Burst of 4 records; the record with trade_id == 1 goes missing.
    let mut s = f.ev(EventType::Snapshot, BID, 102, 500, 101);
    s.trade_id = 3;
    apply(&mut book, s);
    let mut s = f.ev(EventType::Snapshot, BID, 101, 400, 102);
    s.trade_id = 2;
    apply(&mut book, s);
    f.seq += 1; // the trade_id == 1 record is lost: gap INSIDE the burst
    let mut s = f.ev(EventType::Snapshot, ASK, 104, 700, 104);
    s.trade_id = 0;
    apply(&mut book, s);
    assert_eq!(book.counters.gaps_detected, 2);
    assert!(book.stale); // broken burst must NOT clear stale

    // Book events stay blocked until a complete burst arrives.
    apply(&mut book, f.add(BID, 102, 100, 105));
    assert_eq!(book.counters.dropped_while_stale, 2);

    // A subsequent complete burst with no interior gap recovers.
    let burst = [(BID, 102i64, 500i64, 111u64), (BID, 101, 400, 112), (ASK, 103, 600, 113)];
    for (i, &(side, price, qty, oid)) in burst.iter().enumerate() {
        let mut s = f.ev(EventType::Snapshot, side, price, qty, oid);
        s.trade_id = (burst.len() - 1 - i) as u64;
        apply(&mut book, s);
    }
    assert!(!book.stale);
    assert_eq!(book.best_bid(), Some((102, 500)));
    assert_eq!(book.best_ask(), Some((103, 600)));
    assert_eq!(book.order_count_total(), 3);
}

#[test]
fn broken_burst_state_survives_checkpoint_restore() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    f.seq += 2; // gap -> stale
    let mut s = f.ev(EventType::Snapshot, BID, 102, 500, 101);
    s.trade_id = 2;
    apply(&mut book, s);
    f.seq += 1; // gap inside the burst
    let mut s = f.ev(EventType::Snapshot, BID, 101, 400, 102);
    s.trade_id = 1;
    apply(&mut book, s);

    let cp = book.checkpoint();
    assert!(cp.snapshot_active);
    assert!(cp.snapshot_broken);
    let mut restored = OrderBook::restore(&cp).expect("restore");
    let mut s = f.ev(EventType::Snapshot, ASK, 104, 600, 103);
    s.trade_id = 0;
    book.apply(&s).expect("apply");
    restored.apply(&s).expect("apply");
    assert!(book.stale && restored.stale); // broken burst completed: still stale
    assert_eq!(restored.checkpoint(), book.checkpoint());
}

#[test]
fn checkpoint_carries_new_fields_and_arrival_order_round_trips() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(ASK, 105, 20, 2));
    apply(&mut book, f.add(BID, 100, 5, 3));
    let m = f.ev(EventType::Modify, BID, 100, 30, 1); // increase: level tail
    apply(&mut book, m);
    book.apply(&f.add(7, 101, 10, 9)).expect("invalid side dropped"); // counted

    let cp = book.checkpoint();
    assert_eq!(cp.arrival_order, vec![1, 2, 3]); // MODIFY keeps arrival order
    assert!(!cp.snapshot_broken);
    assert_eq!(cp.counters.invalid_side_dropped, 1);

    // The serialized checkpoint carries the pinned schema additions.
    let js: serde_json::Value =
        serde_json::to_value(&cp).expect("checkpoint serializes");
    assert_eq!(js["arrival_order"], serde_json::json!([1, 2, 3]));
    assert_eq!(js["snapshot_broken"], serde_json::json!(false));
    assert_eq!(js["counters"]["invalid_side_dropped"], serde_json::json!(1));

    // Round-trip: counters, arrival order and iteration order all survive.
    let restored = OrderBook::restore(&cp).expect("restore");
    assert_eq!(restored.checkpoint(), cp);
    assert_eq!(restored.counters, book.counters);
    assert_eq!(restored.resting_orders(None), book.resting_orders(None));

    // A checkpoint with an inconsistent arrival_order is refused.
    let mut bad = cp.clone();
    bad.arrival_order = vec![1, 2, 4];
    assert!(OrderBook::restore(&bad).is_err());
    let mut bad = cp;
    bad.arrival_order = vec![1, 2];
    assert!(OrderBook::restore(&bad).is_err());
}

#[test]
fn duplicate_sequence_dropped_and_counted() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    let mut dup = f.add(BID, 101, 10, 2);
    dup.sequence = 1; // duplicate
    apply(&mut book, dup);
    assert_eq!(book.best_bid(), Some((100, 10)));
    assert_eq!(book.counters.duplicates_dropped, 1);
    assert_eq!(book.last_sequence, 1); // duplicates never advance the sequence
}

#[test]
fn gap_from_first_event_does_not_mark_stale() {
    // last_sequence == 0 guard: a stream may start at any sequence.
    let mut book = OrderBook::new(1, 1);
    let mut f = Feed::new(1);
    f.seq = 41; // first event carries sequence 42
    apply(&mut book, f.add(BID, 100, 10, 1));
    assert!(!book.stale);
    assert_eq!(book.counters.gaps_detected, 0);
}

#[test]
fn stale_gating_allows_trade_status_heartbeat() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    f.seq += 3; // gap
    let mut t = f.ev(EventType::Trade, BID, 100, 50, 0);
    t.trade_id = 9;
    apply(&mut book, t);
    assert!(book.stale);
    assert_eq!(book.trade_flow, 50); // TRADE applied while stale

    let s = f.ev(EventType::Status, BID, 0, SessionStatus::Halt as i64, 0);
    apply(&mut book, s);
    assert_eq!(book.status, SessionStatus::Halt as i64);

    let h = f.ev(EventType::Heartbeat, BID, 0, 0, 0);
    apply(&mut book, h);
    assert_eq!(book.last_sequence, f.seq);

    // Book-mutating events still gated.
    let c = f.ev(EventType::Cancel, BID, 0, 0, 1);
    apply(&mut book, c);
    assert_eq!(book.counters.dropped_while_stale, 1);
    assert_eq!(book.best_bid(), Some((100, 10)));
}

#[test]
fn timestamps_and_sequence_update_on_every_non_duplicate() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    f.seq += 2; // gap => stale; dropped-but-fresh event still updates seq/ts
    let ev = f.add(BID, 101, 5, 2);
    let (ex, rx, sq) = (ev.exchange_ts, ev.receive_ts, ev.sequence);
    apply(&mut book, ev);
    assert_eq!(book.last_sequence, sq);
    assert_eq!(book.exchange_ts, ex);
    assert_eq!(book.receive_ts, rx);
}

#[test]
fn wrong_routing_is_an_error_not_a_panic() {
    let mut book = OrderBook::new(1, 1);
    let mut f = Feed::new(2); // wrong venue
    assert!(book.apply(&f.add(BID, 100, 10, 1)).is_err());
    let mut f = Feed::new(1);
    let mut ev = f.add(BID, 100, 10, 1);
    ev.instrument_id = 9; // wrong instrument
    assert!(book.apply(&ev).is_err());
    // Venue 0 book accepts any venue (synthetic/consolidated input).
    let mut any = OrderBook::new(1, 0);
    let mut f = Feed::new(7);
    assert!(any.apply(&f.add(BID, 100, 10, 1)).is_ok());
}

#[test]
fn unknown_event_type_is_an_error() {
    let (mut book, mut f) = book_with_feed();
    let mut ev = f.add(BID, 100, 10, 1);
    ev.event_type = 42;
    assert!(book.apply(&ev).is_err());
    assert_eq!(book.counters.events_applied, 0);
}

#[test]
fn status_event_stores_session_status() {
    let (mut book, mut f) = book_with_feed();
    assert_eq!(book.status, SessionStatus::Trading as i64);
    let s = f.ev(EventType::Status, BID, 0, SessionStatus::Auction as i64, 0);
    apply(&mut book, s);
    assert_eq!(book.status, SessionStatus::Auction as i64);
}

#[test]
fn checkpoint_restore_is_replay_equivalent() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(BID, 100, 20, 2));
    apply(&mut book, f.add(ASK, 105, 30, 3));
    let m = f.ev(EventType::Modify, BID, 100, 25, 1); // move to tail
    apply(&mut book, m);
    let mut t = f.ev(EventType::Trade, ASK, 100, 7, 0);
    t.trade_id = 3;
    apply(&mut book, t);

    let cp = book.checkpoint();
    let mut restored = OrderBook::restore(&cp).expect("restore");
    assert_eq!(restored.checkpoint(), cp);
    assert_eq!(restored.state_summary(), book.state_summary());
    assert_eq!(restored.counters, book.counters);

    // Subsequent behavior bit-identical (FIFO order preserved through restore).
    let e1 = f.ev(EventType::Execute, BID, 100, 30, 2);
    let mut b2 = book.clone();
    b2.apply(&e1).expect("apply");
    restored.apply(&e1).expect("apply");
    assert_eq!(restored.state_summary(), b2.state_summary());
    assert_eq!(restored.resting_orders(None), b2.resting_orders(None));
}

#[test]
fn checkpoint_json_roundtrip() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    apply(&mut book, f.add(ASK, 105, 5, 2));
    let cp = book.checkpoint();
    let text = serde_json::to_string(&cp).expect("serialize");
    let back: orderbook::BookCheckpoint = serde_json::from_str(&text).expect("deserialize");
    assert_eq!(back, cp);
}

#[test]
fn restore_rejects_corrupt_checkpoint() {
    let (mut book, mut f) = book_with_feed();
    apply(&mut book, f.add(BID, 100, 10, 1));
    let mut cp = book.checkpoint();
    cp.levels.push(orderbook::LevelCheckpoint {
        side: 1,
        price_ticks: 105,
        orders: vec![(1, 5)], // duplicate order_id across levels
    });
    assert!(OrderBook::restore(&cp).is_err());
    let mut cp2 = book.checkpoint();
    cp2.levels[0].side = 3;
    assert!(OrderBook::restore(&cp2).is_err());
}

#[test]
fn consolidated_merges_sizes_and_counts_across_venues() {
    let mut cons = ConsolidatedBook::new(1);
    let mut f1 = Feed::new(1);
    let mut f2 = Feed::new(2);
    cons.apply(&f1.add(BID, 100, 10, 1)).expect("apply");
    cons.apply(&f2.add(BID, 100, 15, 2)).expect("apply");
    cons.apply(&f2.add(BID, 99, 5, 3)).expect("apply");
    cons.apply(&f1.add(ASK, 105, 8, 4)).expect("apply");
    cons.apply(&f2.add(ASK, 104, 2, 5)).expect("apply");

    assert_eq!(cons.best_bid(), Some((100, 25)));
    assert_eq!(cons.best_ask(), Some((104, 2)));
    assert_eq!(cons.depth(BID, 10), vec![(100, 25), (99, 5)]);
    assert_eq!(cons.order_count(BID, 10), vec![(100, 2), (99, 1)]);
    assert_eq!(cons.depth(ASK, 10), vec![(104, 2), (105, 8)]);

    // Per-venue sequencing is independent.
    assert_eq!(cons.books[&1].last_sequence, 2);
    assert_eq!(cons.books[&2].last_sequence, 3);
}

#[test]
fn consolidated_trade_flow_and_checkpoint() {
    let mut cons = ConsolidatedBook::new(1);
    let mut f1 = Feed::new(1);
    let mut f2 = Feed::new(2);
    let mut t = f1.ev(EventType::Trade, BID, 100, 30, 0);
    t.trade_id = 1;
    cons.apply(&t).expect("apply");
    let mut t = f2.ev(EventType::Trade, ASK, 100, 10, 0);
    t.trade_id = 2;
    cons.apply(&t).expect("apply");
    cons.apply(&f1.add(BID, 100, 10, 5)).expect("apply");
    assert_eq!(cons.trade_flow(), 20);

    let cp = cons.checkpoint();
    let restored = ConsolidatedBook::restore(&cp).expect("restore");
    assert_eq!(restored.checkpoint(), cp);
    assert_eq!(restored.trade_flow(), 20);
    assert_eq!(restored.best_bid(), cons.best_bid());
}

#[test]
fn empty_book_summary_is_zeroed() {
    let book = OrderBook::new(1, 1);
    let s = book.state_summary();
    assert_eq!(s["best_bid_ticks"], 0);
    assert_eq!(s["best_ask_size"], 0);
    assert_eq!(s["depth_bid_top5"].as_array().map(Vec::len), Some(0));
    assert_eq!(s["trade_flow"], 0);
    assert_eq!(s["sequence"], 0);
}
