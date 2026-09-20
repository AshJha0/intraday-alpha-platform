//! `OrderBook::restore` must reject what `apply` rejects.
//!
//! A checkpoint is a trust boundary like the wire. restore() validated side,
//! duplicate ids and arrival-order consistency but not qty or price_ticks,
//! while `payload_ok` requires both > 0 on the live path: a hand-written
//! checkpoint was accepted and `best_bid()` returned `(-5, -1000000)` — a
//! state unreachable through `apply()` that then feeds negative sizes into
//! the feature engine's merged depth and rolling windows.

use marketdata::{EventType, MarketEvent};
use orderbook::{BookCheckpoint, OrderBook};

fn seed() -> BookCheckpoint {
    let mut book = OrderBook::new(1, 1);
    let ev = MarketEvent {
        event_id: 1,
        instrument_id: 1,
        venue_id: 1,
        exchange_ts: 1_000,
        receive_ts: 1_000,
        sequence: 1,
        event_type: EventType::Add.as_u8(),
        side: 0,
        price_ticks: 100,
        qty: 10,
        order_id: 7,
        trade_id: 0,
    };
    book.apply(&ev).unwrap();
    book.checkpoint()
}

#[test]
fn a_clean_checkpoint_still_round_trips() {
    let cp = seed();
    let book = OrderBook::restore(&cp).unwrap();
    assert_eq!(book.best_bid(), Some((100, 10)));
}

#[test]
fn restore_rejects_non_positive_price_ticks() {
    let mut cp = seed();
    cp.levels[0].price_ticks = -5;
    assert!(OrderBook::restore(&cp).is_err());
    cp.levels[0].price_ticks = 0;
    assert!(OrderBook::restore(&cp).is_err());
}

#[test]
fn restore_rejects_non_positive_qty() {
    let mut cp = seed();
    cp.levels[0].orders[0].1 = -1_000_000;
    assert!(OrderBook::restore(&cp).is_err());
    cp.levels[0].orders[0].1 = 0;
    assert!(OrderBook::restore(&cp).is_err());
}

#[test]
fn restore_rejects_the_reported_unreachable_state() {
    let mut cp = seed();
    cp.levels[0].price_ticks = -5;
    cp.levels[0].orders[0].1 = -1_000_000;
    assert!(
        OrderBook::restore(&cp).is_err(),
        "best_bid() of (-5, -1000000) must be unreachable"
    );
}
