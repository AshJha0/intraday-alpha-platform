//! Venue-simulator gating scenarios (spec §16 "sequence-gap and stale
//! market-data protection", conventions §4): the FX golden vector fed to a
//! single-venue sim, halts, stale books, recovery.

use marketdata::{read_jsonl, EventType, MarketEvent, SessionStatus};
use venue::{ExecStatus, OrderRequest, OrderType, RejectReason, SimVenueConfig, SimulatedVenue};

const T0: i64 = 1_700_000_000_000_000_000;

fn cfg(venue_id: u16) -> SimVenueConfig {
    SimVenueConfig {
        venue_id,
        latency_ns: 150_000,
        taker_fee_per_share: 0.003,
        maker_rebate_per_share: 0.002,
    }
}

fn ev(seq: u64, et: EventType, side: u8, price: i64, qty: i64, order_id: u64, trade_id: u64) -> MarketEvent {
    MarketEvent {
        event_id: seq,
        instrument_id: 1,
        venue_id: 1,
        exchange_ts: T0 + seq as i64 * 1_000_000,
        receive_ts: T0 + seq as i64 * 1_000_000,
        sequence: seq,
        event_type: et.as_u8(),
        side,
        price_ticks: price,
        qty,
        order_id,
        trade_id,
    }
}

fn add(seq: u64, side: u8, price: i64, qty: i64, oid: u64) -> MarketEvent {
    ev(seq, EventType::Add, side, price, qty, oid, 0)
}

fn order(id: u64, side: u8, qty: i64, price: i64, otype: OrderType) -> OrderRequest {
    OrderRequest {
        order_id: id,
        instrument_id: 1,
        side,
        qty,
        price_ticks: price,
        order_type: otype.as_u8(),
        venue_id: 1,
        strategy_id: "S1".to_string(),
        urgency: 1.0,
        timestamp: T0 + 1_000_000_000,
    }
}

fn venue_with_book() -> SimulatedVenue {
    let mut v = SimulatedVenue::new(cfg(1)).unwrap();
    v.on_market_event(&add(1, 0, 2450, 300, 11)).unwrap();
    v.on_market_event(&add(2, 0, 2449, 500, 12)).unwrap();
    v.on_market_event(&add(3, 1, 2452, 200, 13)).unwrap();
    v.on_market_event(&add(4, 1, 2453, 400, 14)).unwrap();
    v
}

#[test]
fn scenario_multi_venue_feed_into_single_venue_sim_keeps_sequences_clean() {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/events_fx_quote.jsonl");
    let events = read_jsonl(path).expect("golden FX vector");
    let mut v = SimulatedVenue::new(cfg(10)).unwrap();
    for e in &events {
        v.on_market_event(e).unwrap();
    }
    let mine = events.iter().filter(|e| e.venue_id == 10).count() as u64;
    assert_eq!(v.metrics.counter("venue_market_events_total").get(), mine);
    assert_eq!(
        v.metrics.counter("venue_market_events_ignored_total").get(),
        events.len() as u64 - mine
    );
    let book = v.book(101).expect("LP1 book exists");
    assert_eq!(book.venue_id, 10);
    assert_eq!(book.counters.duplicates_dropped, 0);
    assert_eq!(book.counters.gaps_detected, 0);
    assert!(!book.stale);
    assert_eq!(book.counters.events_applied, mine);
    // the book quotes exactly this venue's L1, not an interleaving of three
    assert!(book.depth(0, 10).len() <= 1 && book.depth(1, 10).len() <= 1);
}

#[test]
fn scenario_halt_rejects_orders_and_holds_resting_fills() {
    let mut v = venue_with_book();
    // A resting buy at 2451 (between the touch) is accepted while TRADING.
    let reps = v.submit(&order(1, 0, 100, 2451, OrderType::Limit)).unwrap();
    assert_eq!(reps.last().unwrap().status, ExecStatus::New.as_u8());
    assert_eq!(v.resting_count(), 1);
    // HALT: new orders are rejected with a reason ...
    v.on_market_event(&ev(5, EventType::Status, 0, 0, SessionStatus::Halt as i64, 0, 0)).unwrap();
    let reps = v.submit(&order(2, 0, 100, 0, OrderType::Market)).unwrap();
    assert_eq!(reps.len(), 1);
    assert_eq!(reps[0].status, ExecStatus::Rejected.as_u8());
    assert_eq!(v.last_reject_reason(), Some(RejectReason::NotTrading));
    assert_eq!(v.metrics.counter(RejectReason::NotTrading.counter()).get(), 1);
    // ... and a crossing ask (rests during the halt) does NOT fill the resting buy.
    let fills = v.on_market_event(&add(6, 1, 2450, 50, 15)).unwrap();
    assert!(fills.is_empty());
    assert_eq!(v.resting_count(), 1);
    // AUCTION: still no fills, still rejected.
    v.on_market_event(&ev(7, EventType::Status, 0, 0, SessionStatus::Auction as i64, 0, 0)).unwrap();
    assert!(v.on_market_event(&ev(8, EventType::Trade, 0, 2451, 10, 0, 1)).unwrap().is_empty());
    assert_eq!(v.last_reject_reason(), Some(RejectReason::NotTrading));
    let reps = v.submit(&order(3, 1, 10, 2440, OrderType::Limit)).unwrap();
    assert_eq!(reps[0].status, ExecStatus::Rejected.as_u8());
    // The venue uncrosses (EXECUTE the crossing ask) and resumes TRADING:
    // the next market update that crosses the resting buy fills it.
    v.on_market_event(&ev(9, EventType::Execute, 1, 2450, 50, 15, 0)).unwrap();
    v.on_market_event(&ev(10, EventType::Status, 0, 0, SessionStatus::Trading as i64, 0, 0)).unwrap();
    let fills = v.on_market_event(&add(11, 1, 2451, 500, 16)).unwrap();
    assert_eq!(fills.len(), 1);
    assert_eq!(fills[0].status, ExecStatus::Filled.as_u8());
    assert_eq!((fills[0].filled_qty, fills[0].fill_price_ticks), (100, 2451));
    assert_eq!(v.resting_count(), 0);
}

#[test]
fn scenario_venue_disconnect_gap_rejects_until_snapshot_recovery() {
    let mut v = venue_with_book();
    let reps = v.submit(&order(1, 0, 100, 2451, OrderType::Limit)).unwrap();
    assert_eq!(reps.last().unwrap().status, ExecStatus::New.as_u8());
    // Sequence gap: the book is stale — orders rejected, resting orders held.
    v.on_market_event(&add(9, 1, 2451, 500, 21)).unwrap();
    assert!(v.book(1).unwrap().stale);
    let reps = v.submit(&order(2, 0, 10, 0, OrderType::Market)).unwrap();
    assert_eq!(reps[0].status, ExecStatus::Rejected.as_u8());
    assert_eq!(v.last_reject_reason(), Some(RejectReason::StaleBook));
    assert_eq!(v.metrics.counter(RejectReason::StaleBook.counter()).get(), 1);
    assert!(v.on_market_event(&add(10, 1, 2451, 500, 22)).unwrap().is_empty());
    assert_eq!(v.resting_count(), 1);
    // SNAPSHOT recovery: ask at 2451 crosses the resting buy -> fill resumes.
    let recs = [(0u8, 2450i64, 300i64, 31u64, 2u64), (0, 2449, 500, 32, 1), (1, 2451, 80, 33, 0)];
    let mut fills = Vec::new();
    for (i, &(side, price, qty, oid, remaining)) in recs.iter().enumerate() {
        fills = v.on_market_event(&ev(11 + i as u64, EventType::Snapshot, side, price, qty, oid, remaining)).unwrap();
    }
    assert!(!v.book(1).unwrap().stale);
    assert_eq!(fills.len(), 1);
    assert_eq!((fills[0].filled_qty, fills[0].fill_price_ticks), (80, 2451));
    assert_eq!(fills[0].status, ExecStatus::Partial.as_u8());
    let reps = v.submit(&order(3, 0, 10, 0, OrderType::Market)).unwrap();
    assert_eq!(reps[0].status, ExecStatus::New.as_u8());
    assert_eq!(v.last_reject_reason(), None);
}

#[test]
fn unknown_instrument_empty_book_and_wrong_venue_are_rejected_with_reasons() {
    let mut v = SimulatedVenue::new(cfg(1)).unwrap();
    let reps = v.submit(&order(1, 0, 10, 0, OrderType::Market)).unwrap();
    assert_eq!(reps[0].status, ExecStatus::Rejected.as_u8());
    assert_eq!(v.last_reject_reason(), Some(RejectReason::UnknownInstrument));
    v.on_market_event(&ev(1, EventType::Heartbeat, 0, 0, 0, 0, 0)).unwrap();
    let reps = v.submit(&order(2, 0, 10, 2450, OrderType::Limit)).unwrap();
    assert_eq!(reps[0].status, ExecStatus::Rejected.as_u8());
    assert_eq!(v.last_reject_reason(), Some(RejectReason::EmptyBook));
    let mut o = order(3, 0, 10, 2450, OrderType::Limit);
    o.venue_id = 2;
    v.submit(&o).unwrap();
    assert_eq!(v.last_reject_reason(), Some(RejectReason::WrongVenue));
    assert_eq!(v.metrics.counter("venue_orders_rejected_total").get(), 3);
}
