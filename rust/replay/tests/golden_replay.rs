//! Golden replay tests: determinism and checkpoint-restart equivalence over
//! the golden vectors (API_CORE.md §5-§6).

use std::path::PathBuf;

use marketdata::{read_jsonl, MarketEvent};
use replay::ReplayEngine;

fn golden_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden")
        .join(name)
}

fn eq_events() -> Vec<MarketEvent> {
    read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("golden EQ vector must decode")
}

fn fx_events() -> Vec<MarketEvent> {
    read_jsonl(golden_path("events_fx_quote.jsonl")).expect("golden FX vector must decode")
}

#[test]
fn replay_is_deterministic_across_runs() {
    for events in [eq_events(), fx_events()] {
        let mut a = ReplayEngine::new(0, 250);
        let mut b = ReplayEngine::new(0, 250);
        let sa = a.run(&events).expect("run a");
        let sb = b.run(&events).expect("run b");
        assert_eq!(sa, sb);
        assert_eq!(a.book_states(), b.book_states());
        assert_eq!(a.checkpoint(), b.checkpoint());
        assert_eq!(a.snapshots, b.snapshots);
    }
}

#[test]
fn checkpoint_restart_equals_single_pass_eq() {
    let events = eq_events();
    for split in [1, 137, 1000, 1999] {
        let mut full = ReplayEngine::new(0, 0);
        full.run(&events).expect("full run");

        let mut prefix = ReplayEngine::new(0, 0);
        prefix.run(&events[..split]).expect("prefix run");
        let cp = prefix.checkpoint();
        let mut restored = ReplayEngine::restore(&cp).expect("restore");
        restored.run(&events[split..]).expect("rest run");

        assert_eq!(restored.checkpoint(), full.checkpoint(), "split {split}");
        assert_eq!(restored.book_states(), full.book_states(), "split {split}");
    }
}

#[test]
fn checkpoint_restart_equals_single_pass_fx() {
    let events = fx_events();
    let split = 400;
    let mut full = ReplayEngine::new(0, 0);
    full.run(&events).expect("full run");

    let mut prefix = ReplayEngine::new(0, 0);
    prefix.run(&events[..split]).expect("prefix run");
    let mut restored = ReplayEngine::restore(&prefix.checkpoint()).expect("restore");
    restored.run(&events[split..]).expect("rest run");

    assert_eq!(restored.checkpoint(), full.checkpoint());
    assert_eq!(restored.book_states(), full.book_states());
}

#[test]
fn checkpoint_json_roundtrip_preserves_state() {
    let events = eq_events();
    let mut engine = ReplayEngine::new(0, 0);
    engine.run(&events[..1500]).expect("run");
    let cp = engine.checkpoint();
    let text = serde_json::to_string(&cp).expect("serialize");
    let back: replay::EngineCheckpoint = serde_json::from_str(&text).expect("deserialize");
    assert_eq!(back, cp);
    let mut restored = ReplayEngine::restore(&back).expect("restore");
    restored.run(&events[1500..]).expect("rest");
    let mut full = ReplayEngine::new(0, 0);
    full.run(&events).expect("full");
    assert_eq!(restored.book_states(), full.book_states());
}

#[test]
fn snapshot_and_checkpoint_cadence() {
    let events = eq_events();
    let mut engine = ReplayEngine::new(500, 400);
    let summary = engine.run(&events).expect("run");
    assert_eq!(summary.events_processed, 2000);
    assert_eq!(summary.instruments, 1);
    assert_eq!(engine.snapshots.len(), 5); // 400/800/1200/1600/2000
    assert_eq!(engine.snapshots[0]["index"], 400);
    assert_eq!(engine.snapshots[4]["index"], 2000);
    assert_eq!(engine.checkpoints.len(), 4); // 500..2000, keep_checkpoints=4
    assert_eq!(engine.checkpoints[3].events_processed, 2000);
    // Final snapshot state equals final book state.
    assert_eq!(
        engine.snapshots[4]["instruments"],
        engine.book_states()["instruments"]
    );
}

#[test]
fn fx_replay_routes_per_venue() {
    let events = fx_events();
    let mut engine = ReplayEngine::new(0, 0);
    let summary = engine.run(&events).expect("run");
    assert_eq!(summary.events_processed, 800);
    assert_eq!(summary.instruments, 1);
    let cons = &engine.books[&101];
    let venues: Vec<u16> = cons.books.keys().copied().collect();
    assert_eq!(venues, vec![10, 11, 12]);
    // QUOTE books are L1: at most one level per side per venue.
    for book in cons.books.values() {
        assert!(book.depth(0, 10).len() <= 1);
        assert!(book.depth(1, 10).len() <= 1);
    }
    // Consolidated depth merges the three venues' L1s.
    assert!(!cons.depth(0, 10).is_empty());
    assert!(cons.best_bid().is_some() && cons.best_ask().is_some());
}

#[test]
fn time_regressions_are_counted() {
    let mut engine = ReplayEngine::new(0, 0);
    let mk = |seq: u64, ts: i64| MarketEvent {
        event_id: seq,
        instrument_id: 1,
        venue_id: 1,
        exchange_ts: ts,
        receive_ts: ts,
        sequence: seq,
        event_type: 9, // HEARTBEAT
        side: 0,
        price_ticks: 0,
        qty: 0,
        order_id: 0,
        trade_id: 0,
    };
    let events = vec![mk(1, 100), mk(2, 90), mk(3, 95), mk(4, 94)];
    let summary = engine.run(&events).expect("run");
    assert_eq!(summary.time_regressions, 2);
}
