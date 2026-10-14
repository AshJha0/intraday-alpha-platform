//! Micro-benchmark of the pre-trade hot path, `RiskEngine::check_order`
//! (v1.16 item 13). Dependency-free (`harness = false`, no criterion: the
//! lockfile stays unchanged): best-of-runs ns/call for an allowed and a
//! size-denied order. CI only compiles it (`cargo bench --no-run`); run it
//! with `cargo bench -p risk --bench check_order`.

use std::collections::BTreeMap;
use std::hint::black_box;
use std::time::Instant;

use risk::RiskEngine;
use venue::{OrderRequest, OrderType};

const T0: i64 = 1_700_000_000_000_000_000;
const CALLS: usize = 2_000;
const RUNS: usize = 20;

fn engine() -> RiskEngine {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../configs/risk/risk.json");
    let doc: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut ticks = BTreeMap::new();
    ticks.insert(1u32, 0.01);
    let mut eng = RiskEngine::from_config_ticks(&doc, ticks);
    eng.on_market(1, 2450, 2452, T0);
    eng
}

fn order(qty: i64) -> OrderRequest {
    OrderRequest {
        order_id: 1,
        instrument_id: 1,
        side: 0,
        qty,
        price_ticks: 2451,
        order_type: OrderType::Limit.as_u8(),
        venue_id: 1,
        strategy_id: "S1".to_string(),
        urgency: 0.5,
        timestamp: T0 + 1,
    }
}

/// Best-of-RUNS mean ns per check_order. A fresh engine per call (built
/// outside the timed region): check_order appends to the audit log and
/// feeds the throttle, so reusing one engine would drift the path taken.
fn bench(name: &str, o: &OrderRequest) {
    let mut best = f64::INFINITY;
    for _ in 0..RUNS {
        let mut engines: Vec<RiskEngine> = (0..CALLS).map(|_| engine()).collect();
        let start = Instant::now();
        for e in engines.iter_mut() {
            black_box(e.check_order(black_box(o)));
        }
        let ns = start.elapsed().as_nanos() as f64 / CALLS as f64;
        best = best.min(ns);
    }
    println!("risk_check_order/{name}: {best:.0} ns/call (best of {RUNS} x {CALLS})");
}

fn main() {
    bench("allow", &order(100));
    bench("deny_size", &order(i64::MAX / 4)); // breaches the per-order size limit
}
