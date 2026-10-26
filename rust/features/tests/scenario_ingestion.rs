//! Feature-engine ingestion scenarios (API_FEATURES.md §2, round-3 RESEARCH).
//!
//! Rust mirror of `python/tests/test_feature_ingestion.py`: each test is a
//! real-world event sequence whose expected behaviour is pinned by the
//! contract, so a port cannot quietly re-introduce the double-counting /
//! blow-up bugs the goldens alone would not catch.

use std::collections::BTreeMap;

use features::{FeatureEngine, FeatureVector};
use marketdata::{EventType, MarketEvent};

const NS: i64 = 1_000_000_000;
const T0: i64 = 1_787_578_200 * NS;
const TICK: f64 = 0.01;

/// Single-instrument feed with explicit per-venue sequence numbers.
struct Feed {
    eng: FeatureEngine,
    iid: u32,
    seq: BTreeMap<u16, u64>,
    id: u64,
    last: Option<FeatureVector>,
}

impl Feed {
    fn new(iid: u32, tick: f64) -> Feed {
        let mut ticks = BTreeMap::new();
        ticks.insert(iid, tick);
        Feed {
            eng: FeatureEngine::new(ticks, 0).expect("engine"),
            iid,
            seq: BTreeMap::new(),
            id: 0,
            last: None,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn send(
        &mut self,
        et: EventType,
        ts: i64,
        venue: u16,
        seq: Option<u64>,
        side: u8,
        price: i64,
        qty: i64,
        order_id: u64,
        trade_id: u64,
    ) -> Option<FeatureVector> {
        let s = seq.unwrap_or_else(|| self.seq.get(&venue).copied().unwrap_or(0) + 1);
        let cur = self.seq.entry(venue).or_insert(0);
        *cur = (*cur).max(s);
        self.id += 1;
        let ev = MarketEvent {
            event_id: self.id,
            instrument_id: self.iid,
            venue_id: venue,
            exchange_ts: ts,
            receive_ts: ts + 150_000,
            sequence: s,
            event_type: et.as_u8(),
            side,
            price_ticks: price,
            qty,
            order_id,
            trade_id,
        };
        let out = self.eng.apply(&ev).expect("engine never errors on data");
        if out.is_some() {
            self.last = out.clone();
        }
        out
    }

    fn add(&mut self, ts: i64, side: u8, price: i64, qty: i64, oid: u64) {
        self.send(EventType::Add, ts, 1, None, side, price, qty, oid, 0);
    }

    fn vec(&self) -> &FeatureVector {
        self.last.as_ref().expect("a vector was emitted")
    }

    fn get(&self, name: &str) -> Option<f64> {
        self.vec().get(name)
    }
}

fn slot(name: &str) -> usize {
    features::feature_index(name).expect("native feature")
}

#[test]
fn scenario_gateway_replay_after_reconnect() {
    // Reconnect replay: duplicate TRADE, side=2 ADD, CANCEL while stale.
    let mut f = Feed::new(1, TICK);
    let mut t = T0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    t += 2 * NS;
    f.send(EventType::Trade, t, 1, None, 0, 1001, 50, 0, 1);
    assert_eq!(f.get("signed_volume_w1s_v1"), Some(50.0));

    // the gateway replays the same message (duplicate sequence)
    let dup = *f.seq.get(&1).unwrap();
    f.send(EventType::Trade, t, 1, Some(dup), 0, 1001, 50, 0, 1);
    assert_eq!(
        f.get("signed_volume_w1s_v1"),
        Some(50.0),
        "a duplicate trade must not double-count signed volume"
    );
    assert_eq!(f.eng.events_dropped, 1);

    // malformed side on an ADD: dropped by the book, no state change
    let depth_before = f.get("depth_bid_l1_v1");
    f.send(EventType::Add, t, 1, None, 2, 1000, 70, 900, 0);
    assert_eq!(f.eng.events_dropped, 2);
    assert_eq!(f.get("depth_bid_l1_v1"), depth_before);

    // sequence gap -> stale venue; a CANCEL arriving while stale is dropped
    let seq = *f.seq.get(&1).unwrap();
    f.send(EventType::Add, t, 1, Some(seq + 10), 0, 999, 10, 901, 0);
    f.send(EventType::Cancel, t, 1, None, 0, 1000, 0, 1, 0);
    assert!(f.eng.events_dropped >= 3);
    assert!(!f.vec().validity[slot("mid_price_v1")], "stale book features");
}

#[test]
fn scenario_venue_disconnect_then_snapshot_recovery() {
    let mut f = Feed::new(1, TICK);
    let mut t = T0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    let mut bid = 1000i64;
    let mut oid = 10u64;
    for k in 0..200 {
        t += 2 * NS;
        let step = if k % 2 == 0 { 1 } else { -1 };
        f.add(t, 0, bid + step, 100, oid);
        f.send(
            EventType::Cancel,
            t,
            1,
            None,
            0,
            bid,
            0,
            if k == 0 { 1 } else { oid - 1 },
            0,
        );
        bid += step;
        oid += 1;
    }
    assert!(f.get("rvol_w1m_v1").is_some_and(|v| v > 0.0));
    assert!(f.get("ret_vol_adj_10s_v1").is_some());

    // gap, then 120 s of silence
    t += NS;
    let seq = *f.seq.get(&1).unwrap();
    f.send(EventType::Add, t, 1, Some(seq + 50), 0, 1000, 10, oid + 50, 0);
    assert!(f.get("mid_price_v1").is_none());

    // recovery: complete SNAPSHOT burst 120 s later, 1 % higher
    t += 120 * NS;
    let s = *f.seq.get(&1).unwrap() + 1;
    f.send(EventType::Snapshot, t, 1, Some(s), 0, 1010, 100, 0, 3);
    f.send(EventType::Snapshot, t, 1, Some(s + 1), 1, 1012, 100, 0, 2);
    f.send(EventType::Snapshot, t, 1, Some(s + 2), 0, 1009, 90, 0, 1);
    f.send(EventType::Snapshot, t, 1, Some(s + 3), 1, 1013, 90, 0, 0);

    assert_eq!(f.eng.recoveries(1), 1);
    assert_eq!(f.eng.warm_ts(1), Some(t));
    assert!(f.eng.book_ok(1));
    for name in [
        "rvol_w10s_v1",
        "rvol_w1m_v1",
        "rvol_w5m_v1",
        "ret_log_10s_v1",
        "ret_log_1m_v1",
        "ret_vol_adj_10s_v1",
        "vol_regime_ratio_v1",
        "ofi_l1_w1s_v1",
        "signed_volume_w1m_v1",
    ] {
        assert!(
            !f.vec().validity[slot(name)],
            "{name} valid right after a stale recovery"
        );
    }
    assert!(f.get("mid_price_v1").is_some());
    for (i, &ok) in f.vec().validity.iter().enumerate() {
        assert!(!ok || f.vec().values[i].is_finite());
    }
}

#[test]
fn stale_recovery_ratio_features_are_invalid_not_huge() {
    // 70 s with no mid change at all: rvol windows warm but exactly zero.
    let mut f = Feed::new(1, TICK);
    let mut t = T0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    for k in 0..70u64 {
        t += NS;
        f.add(t, 0, 990, 5, 500 + k);
    }
    assert_eq!(f.get("rvol_w1m_v1"), Some(0.0));
    assert!(f.get("ret_vol_adj_10s_v1").is_none());
    assert!(f.get("vol_regime_ratio_v1").is_none());
}

#[test]
fn scenario_one_sided_flicker_keeps_rvol_samples() {
    let mut f = Feed::new(1, TICK);
    let mut t = T0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    t += 11 * NS;
    f.send(EventType::Cancel, t, 1, None, 0, 1000, 0, 1, 0);
    assert!(f.get("mid_price_v1").is_none(), "one-sided");
    t += 1_000;
    f.add(t, 0, 999, 100, 3);
    assert!(
        f.get("rvol_w10s_v1").is_some_and(|v| v > 0.0),
        "the flicker mid change must enter the vol window"
    );
}

#[test]
fn scenario_lp_withdraws_with_zero_price_quote() {
    for price in [0i64, -5] {
        let mut f = Feed::new(101, 1e-5);
        let t = T0;
        f.send(EventType::Quote, t, 10, None, 0, 110_000, 1000, 0, 0);
        f.send(EventType::Quote, t, 10, None, 1, 110_002, 1000, 0, 0);
        assert!(f.get("mid_price_v1").is_some());
        f.send(EventType::Quote, t + NS, 10, None, 0, price, 1000, 0, 0);
        assert_eq!(f.eng.events_dropped, 1, "price {price} must be dropped");
        assert!(f.get("mid_price_v1").is_some(), "previous quote prevails");
        for (i, &ok) in f.vec().validity.iter().enumerate() {
            assert!(!ok || f.vec().values[i].is_finite());
        }
    }
}

#[test]
fn scenario_cross_venue_timestamp_regression() {
    let mut f = Feed::new(1, TICK);
    let t = T0;
    f.send(EventType::Add, t, 1, None, 0, 1000, 100, 1, 0);
    f.send(EventType::Add, t, 1, None, 1, 1002, 100, 2, 0);
    let mid_before = f.get("mid_price_v1").unwrap();

    // venue 2's gateway clock runs 5 ms behind venue 1's
    let out = f.send(EventType::Add, t - 5_000_000, 2, None, 0, 1001, 100, 3, 0);
    assert!(out.is_none(), "a ts regression emits no vector");
    assert_eq!(f.eng.ts_regressions_dropped, 1);
    assert_eq!(f.eng.events_dropped, 1);

    // the stream continues normally afterwards
    f.send(EventType::Add, t + NS, 2, None, 0, 1001, 100, 4, 0);
    assert!(f.get("mid_price_v1").unwrap() > mid_before);
}

#[test]
fn oversized_quantities_never_overflow_a_window_sum() {
    let mut f = Feed::new(1, TICK);
    let t = T0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    // a malformed feed sends i64::MAX quantities
    f.send(EventType::Trade, t + NS, 1, None, 0, 1001, i64::MAX, 0, 1);
    assert_eq!(f.eng.oversized_qty_dropped, 1);
    f.send(EventType::Add, t + 2 * NS, 1, None, 0, 998, i64::MAX, 7, 0);
    assert_eq!(f.eng.oversized_qty_dropped, 2);
    assert!(f.eng.oversized_depth_skipped >= 1);
    assert!(!f.eng.book_ok(1), "an oversized merged depth is unusable");
    for (i, &ok) in f.vec().validity.iter().enumerate() {
        assert!(!ok || f.vec().values[i].is_finite());
    }
}
