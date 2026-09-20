//! Regression tests for the verified overflow defects in the feature engine.
//!
//! Every case is reachable only with extreme (but wire-legal) inputs, so none
//! of it touches the golden vectors. Each test asserts the behaviour of the
//! Python reference (`python/src/iap/features/engine.py`), which all three
//! ports must agree on. Before the fix each of these PANICKED in a debug /
//! overflow-checked build and silently wrapped in a plain release build.

use std::collections::BTreeMap;

use features::{FeatureEngine, FeatureVector};
use marketdata::{EventType, MarketEvent};

const TICK: f64 = 0.01;
const FEATURE_MAX_QTY: i64 = 1 << 40;

// Mirrors the canonical 12-field wire record, so the argument list is wide
// on purpose.
#[allow(clippy::too_many_arguments)]
fn add(
    eid: u64,
    venue: u16,
    ts: i64,
    seq: u64,
    side: u8,
    price: i64,
    qty: i64,
    oid: u64,
) -> MarketEvent {
    MarketEvent {
        event_id: eid,
        instrument_id: 1,
        venue_id: venue,
        exchange_ts: ts,
        receive_ts: ts,
        sequence: seq,
        event_type: EventType::Add.as_u8(),
        side,
        price_ticks: price,
        qty,
        order_id: oid,
        trade_id: 0,
    }
}

fn engine(cadence_ns: i64) -> FeatureEngine {
    let mut ticks = BTreeMap::new();
    ticks.insert(1u32, TICK);
    FeatureEngine::new(ticks, cadence_ns).unwrap()
}

fn run(eng: &mut FeatureEngine, evs: &[MarketEvent]) -> Option<FeatureVector> {
    let mut last = None;
    for ev in evs {
        if let Some(v) = eng.apply(ev).unwrap() {
            last = Some(v);
        }
    }
    last
}

/// Defect 1: cross-venue merged depth was summed in `i64` BEFORE the
/// FEATURE_MAX_QTY guard, so two venues each resting 2^62 at the same price
/// overflowed the merged total (panic here, negative depth emitted as a valid
/// feature in the C++ port). Reference verdict: oversized -> view cleared,
/// `book_ok` false, `oversized_depth_skipped` counted on every refresh.
#[test]
fn merged_depth_overflow_is_oversized_not_a_panic() {
    let huge = 1i64 << 62;
    let mut eng = engine(0);
    let vec = run(
        &mut eng,
        &[
            add(1, 1, 1_000, 1, 0, 100, huge, 11),
            add(2, 1, 1_001, 2, 1, 101, 5, 12),
            add(3, 2, 1_002, 1, 0, 100, huge, 21),
            add(4, 2, 1_003, 2, 1, 101, 5, 22),
        ],
    )
    .expect("a vector is emitted per event at cadence 0");

    assert!(!eng.book_ok(1));
    assert_eq!(eng.oversized_depth_skipped, 4);
    assert_eq!(vec.get("depth_bid_l1_v1"), None);
    assert_eq!(vec.get("mid_price_v1"), None);
}

/// The guard must still pass a merged level that is large but legal: two
/// venues at FEATURE_MAX_QTY / 2 sum to exactly FEATURE_MAX_QTY.
#[test]
fn merged_depth_at_the_limit_is_still_usable() {
    let half = FEATURE_MAX_QTY / 2;
    let mut eng = engine(0);
    let vec = run(
        &mut eng,
        &[
            add(1, 1, 1_000, 1, 0, 100, half, 11),
            add(2, 1, 1_001, 2, 1, 101, 5, 12),
            add(3, 2, 1_002, 1, 0, 100, half, 21),
            add(4, 2, 1_003, 2, 1, 101, 5, 22),
        ],
    )
    .unwrap();
    assert!(eng.book_ok(1));
    assert_eq!(eng.oversized_depth_skipped, 0);
    assert_eq!(vec.get("depth_bid_l1_v1"), Some(FEATURE_MAX_QTY as f64));
}

/// Defect 2: `mid2 = bid_p + ask_p` overflowed `i64` for large but legal
/// prices. The doubled mid is now exact, so mid and microprice agree (the
/// reference computes both in arbitrary precision).
#[test]
fn doubled_mid_beyond_i64_stays_exact() {
    let bid = 1i64 << 62;
    let ask = bid + 1;
    let mut eng = engine(0);
    let vec = run(
        &mut eng,
        &[
            add(1, 1, 1_000, 1, 0, bid, 7, 11),
            add(2, 1, 1_001, 2, 1, ask, 7, 12),
        ],
    )
    .unwrap();
    let mid = vec.get("mid_price_v1").expect("mid is valid");
    let micro = vec.get("microprice_v1").expect("microprice is valid");
    let expect = (bid as f64 + ask as f64) * TICK / 2.0;
    assert!(mid > 0.0, "mid must not be negative: {mid}");
    assert_eq!(mid, expect);
    // Equal sizes on both sides put the microprice exactly at the mid.
    assert!((micro - mid).abs() <= mid.abs() * 1e-12);
    assert_eq!(vec.get("spread_ticks_v1"), Some(1.0));
}

/// Defect 4: `exchange_ts` is a signed 64-bit wire field the codec accepts
/// down to `i64::MIN`; every window / warmup / cadence computation was
/// `t - constant` on unchecked `i64`.
#[test]
fn extreme_exchange_ts_does_not_overflow_window_arithmetic() {
    let tmin = i64::MIN;
    let mut eng = engine(1_000);
    let vec = run(
        &mut eng,
        &[
            add(1, 1, tmin, 1, 0, 100, 5, 11),
            add(2, 1, tmin + 1, 2, 1, 101, 5, 12),
            add(3, 1, tmin + 2, 3, 0, 99, 5, 13),
        ],
    )
    .unwrap();
    assert_eq!(eng.events_processed, 3);
    assert_eq!(eng.warm_ts(1), Some(tmin));
    // Cadence 1 us over three events 1 ns apart: one vector, like the
    // reference (a wrapped `t - last_emit` would emit on every event).
    assert_eq!(eng.vectors_emitted, 1);
    assert_eq!(vec.get("ofi_l1_w1s_v1"), None);
}

/// The mirror case: a very negative warmup anchor with `t` at `i64::MAX`
/// overflowed `warm()` and the cadence test in `emit_if_due`.
#[test]
fn exchange_ts_spanning_the_whole_range_is_warm_not_wrapped() {
    let (tmin, tmax) = (i64::MIN, i64::MAX);
    let mut eng = engine(1_000);
    let vec = run(
        &mut eng,
        &[
            add(1, 1, tmin, 1, 0, 100, 5, 11),
            add(2, 1, tmin + 1, 2, 1, 101, 5, 12),
            add(3, 1, tmax, 3, 0, 99, 5, 13),
        ],
    )
    .unwrap();
    assert_eq!(eng.warm_ts(1), Some(tmin));
    assert_eq!(eng.vectors_emitted, 2);
    // t - warm_ts is ~2^64 ns: every window is warm.
    assert_eq!(vec.get("ofi_l1_w30s_v1"), Some(0.0));
}
