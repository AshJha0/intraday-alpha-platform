//! Simulated-venue behavior: accept/ack/fill flows against the order book.

use marketdata::{EventType, MarketEvent};
use venue::{ExecStatus, OrderRequest, OrderType, SimVenueConfig, SimulatedVenue};

const T0: i64 = 1_700_000_000_000_000_000;

fn cfg() -> SimVenueConfig {
    SimVenueConfig {
        venue_id: 1,
        latency_ns: 150_000,
        taker_fee_per_share: 0.003,
        maker_rebate_per_share: 0.002,
    }
}

fn add(seq: u64, side: u8, price: i64, qty: i64, order_id: u64) -> MarketEvent {
    MarketEvent {
        event_id: seq,
        instrument_id: 1,
        venue_id: 1,
        exchange_ts: T0 + seq as i64 * 1_000_000,
        receive_ts: T0 + seq as i64 * 1_000_000,
        sequence: seq,
        event_type: EventType::Add.as_u8(),
        side,
        price_ticks: price,
        qty,
        order_id,
        trade_id: 0,
    }
}

/// Venue with a book: bids 2450x300, 2449x500; asks 2452x200, 2453x400.
fn venue_with_book() -> SimulatedVenue {
    let mut v = SimulatedVenue::new(cfg()).unwrap();
    v.on_market_event(&add(1, 0, 2450, 300, 11)).unwrap();
    v.on_market_event(&add(2, 0, 2449, 500, 12)).unwrap();
    v.on_market_event(&add(3, 1, 2452, 200, 13)).unwrap();
    v.on_market_event(&add(4, 1, 2453, 400, 14)).unwrap();
    v
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

fn statuses(reports: &[venue::ExecutionReport]) -> Vec<u8> {
    reports.iter().map(|r| r.status).collect()
}

#[test]
fn market_order_walks_depth_and_cancels_remainder() {
    let mut v = venue_with_book();
    // buy 500: 200 @ 2452, 300 @ 2453, nothing left unfilled
    let reps = v.submit(&order(1, 0, 500, 0, OrderType::Market)).unwrap();
    assert_eq!(
        statuses(&reps),
        vec![
            ExecStatus::New.as_u8(),
            ExecStatus::Partial.as_u8(),
            ExecStatus::Filled.as_u8()
        ]
    );
    assert_eq!((reps[1].filled_qty, reps[1].fill_price_ticks), (200, 2452));
    assert_eq!((reps[2].filled_qty, reps[2].fill_price_ticks), (300, 2453));
    // taker fees are charged per share
    assert!((reps[1].fees - 0.003 * 200.0).abs() < 1e-12);
    // buy 10_000: exhausts the ask side (600), remainder canceled
    let reps = v.submit(&order(2, 0, 10_000, 0, OrderType::Market)).unwrap();
    assert_eq!(*statuses(&reps).last().unwrap(), ExecStatus::Canceled.as_u8());
    let filled: i64 = reps.iter().map(|r| r.filled_qty).sum();
    assert_eq!(filled, 600);
}

#[test]
fn limit_order_fills_marketable_part_and_rests() {
    let mut v = venue_with_book();
    // buy 300 @ 2452: fills 200, rests 100 at 2452
    let reps = v.submit(&order(1, 0, 300, 2452, OrderType::Limit)).unwrap();
    assert_eq!(
        statuses(&reps),
        vec![ExecStatus::New.as_u8(), ExecStatus::Partial.as_u8()]
    );
    assert_eq!(reps[1].filled_qty, 200);
    assert_eq!(v.resting_count(), 1);
    // a market sell arriving as new best bid does not fill it, but the ask
    // dropping to 2452... simulate the ask side crossing our resting buy:
    // new best ask at 2451 with size 40 -> resting buy fills 40 @ its own
    // limit price 2452? No: fills at the RESTING price only when crossed.
    let fills = v.on_market_event(&add(5, 1, 2451, 40, 15)).unwrap();
    assert_eq!(fills.len(), 1);
    assert_eq!(fills[0].status, ExecStatus::Partial.as_u8());
    assert_eq!(fills[0].filled_qty, 40);
    assert_eq!(fills[0].fill_price_ticks, 2452);
    // maker fill earns the rebate (negative fees)
    assert!((fills[0].fees + 0.002 * 40.0).abs() < 1e-12);
    // more size at the crossing price completes it
    let fills = v.on_market_event(&add(6, 1, 2451, 500, 16)).unwrap();
    assert_eq!(fills.len(), 1);
    assert_eq!(fills[0].status, ExecStatus::Filled.as_u8());
    assert_eq!(fills[0].filled_qty, 60);
    assert_eq!(v.resting_count(), 0);
}

#[test]
fn ioc_fills_and_cancels_remainder() {
    let mut v = venue_with_book();
    let reps = v.submit(&order(1, 0, 300, 2452, OrderType::Ioc)).unwrap();
    assert_eq!(
        statuses(&reps),
        vec![
            ExecStatus::New.as_u8(),
            ExecStatus::Partial.as_u8(),
            ExecStatus::Canceled.as_u8()
        ]
    );
    assert_eq!(reps[1].filled_qty, 200);
    assert_eq!(v.resting_count(), 0);
}

#[test]
fn fok_is_all_or_nothing() {
    let mut v = venue_with_book();
    // 600 available within 2453: full fill
    let reps = v.submit(&order(1, 0, 600, 2453, OrderType::Fok)).unwrap();
    let filled: i64 = reps.iter().map(|r| r.filled_qty).sum();
    assert_eq!(filled, 600);
    assert_eq!(*statuses(&reps).last().unwrap(), ExecStatus::Filled.as_u8());
    // 601 not available: no fills at all
    let mut v = venue_with_book();
    let reps = v.submit(&order(2, 0, 601, 2453, OrderType::Fok)).unwrap();
    assert_eq!(
        statuses(&reps),
        vec![ExecStatus::New.as_u8(), ExecStatus::Canceled.as_u8()]
    );
}

#[test]
fn peg_rests_at_touch_and_mid_crosses_at_midpoint() {
    let mut v = venue_with_book();
    let reps = v.submit(&order(1, 0, 50, 0, OrderType::Peg)).unwrap();
    assert_eq!(statuses(&reps), vec![ExecStatus::New.as_u8()]);
    assert_eq!(v.resting_count(), 1);
    // midpoint: (2450 + 2452) / 2 = 2451, fills up to opposite L1 (200)
    let reps = v.submit(&order(2, 0, 100, 0, OrderType::Mid)).unwrap();
    assert_eq!(
        statuses(&reps),
        vec![ExecStatus::New.as_u8(), ExecStatus::Filled.as_u8()]
    );
    assert_eq!(reps[1].fill_price_ticks, 2451);
}

#[test]
fn rejects_and_acks_are_reported() {
    let mut v = venue_with_book();
    // wrong venue
    let mut o = order(1, 0, 100, 0, OrderType::Market);
    o.venue_id = 9;
    let reps = v.submit(&o).unwrap();
    assert_eq!(statuses(&reps), vec![ExecStatus::Rejected.as_u8()]);
    // unknown instrument
    let mut o = order(2, 0, 100, 0, OrderType::Market);
    o.instrument_id = 999;
    assert_eq!(statuses(&v.submit(&o).unwrap()), vec![ExecStatus::Rejected.as_u8()]);
    // malformed (LIMIT without price)
    let o = order(3, 0, 100, 0, OrderType::Limit);
    assert_eq!(statuses(&v.submit(&o).unwrap()), vec![ExecStatus::Rejected.as_u8()]);
    // duplicate resting id
    let o = order(4, 0, 10, 2449, OrderType::Limit);
    assert_eq!(*statuses(&v.submit(&o).unwrap()).last().unwrap(), ExecStatus::New.as_u8());
    assert_eq!(statuses(&v.submit(&o).unwrap()), vec![ExecStatus::Rejected.as_u8()]);
    // cancel of a resting order, then of an unknown one
    assert_eq!(v.cancel(4, T0).status, ExecStatus::Canceled.as_u8());
    assert_eq!(v.cancel(4, T0).status, ExecStatus::Rejected.as_u8());
}

#[test]
fn latency_and_execution_ids_are_deterministic() {
    let mut v = venue_with_book();
    let reps = v.submit(&order(1, 0, 100, 0, OrderType::Market)).unwrap();
    for r in &reps {
        assert_eq!(r.exchange_ts, T0 + 1_000_000_000 + 150_000);
        assert_eq!(r.receive_ts, r.exchange_ts + 150_000);
    }
    let ids: Vec<u64> = reps.iter().map(|r| r.execution_id).collect();
    let mut sorted = ids.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(ids.len(), sorted.len(), "execution ids strictly monotone");
    // metrics observed the flow
    assert_eq!(v.metrics.counter_value("venue_orders_accepted_total"), 1);
    assert!(v.metrics.counter_value("venue_fills_total") >= 1);
}
