//! Golden book-state parity: exact-integer state after events
//! 100/500/1000/1500/2000 of the EQ MBO vector (API_CORE.md §6).

use std::path::PathBuf;

use marketdata::read_jsonl;
use orderbook::OrderBook;

fn golden_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden")
        .join(name)
}

#[test]
fn book_states_at_pinned_indices_match_golden() {
    let text = std::fs::read_to_string(golden_path("expected_book_states.json"))
        .expect("read expected_book_states.json");
    let expected: serde_json::Value =
        serde_json::from_str(&text).expect("parse expected_book_states.json");
    assert_eq!(expected["vector"], "events_eq_mbo.jsonl");

    let events =
        read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("golden EQ vector must decode");
    let instrument_id = expected["instrument_id"].as_u64().expect("instrument_id") as u32;
    let venue_id = expected["venue_id"].as_u64().expect("venue_id") as u16;
    let mut book = OrderBook::new(instrument_id, venue_id);

    let states = expected["states"].as_object().expect("states object");
    let mut pins: Vec<usize> = states
        .keys()
        .map(|k| k.parse::<usize>().expect("numeric state key"))
        .collect();
    pins.sort_unstable();
    assert_eq!(pins.len(), 5);

    let mut applied = 0usize;
    for n in pins {
        assert!(n <= events.len());
        while applied < n {
            book.apply(&events[applied])
                .unwrap_or_else(|e| panic!("apply event {}: {}", applied + 1, e));
            applied += 1;
        }
        let got = book.state_summary();
        assert_eq!(got, states[&n.to_string()], "state after event {n}");
    }
}

#[test]
fn golden_eq_replay_has_clean_qc_counters() {
    // The golden vector is a valid gapless stream: no duplicates, no gaps,
    // nothing dropped, every event applied.
    let events =
        read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("golden EQ vector must decode");
    let mut book = OrderBook::new(1, 1);
    for ev in &events {
        book.apply(ev).expect("golden events apply cleanly");
    }
    assert_eq!(book.counters.events_applied, events.len() as u64);
    assert_eq!(book.counters.duplicates_dropped, 0);
    assert_eq!(book.counters.gaps_detected, 0);
    assert_eq!(book.counters.dropped_while_stale, 0);
    assert!(!book.stale);
    assert_eq!(book.last_sequence, events.len() as u64);
}

#[test]
fn golden_checkpoint_midstream_restores_to_identical_final_state() {
    let events =
        read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("golden EQ vector must decode");
    let mut full = OrderBook::new(1, 1);
    for ev in &events {
        full.apply(ev).expect("apply");
    }
    let mut prefix = OrderBook::new(1, 1);
    for ev in &events[..1234] {
        prefix.apply(ev).expect("apply");
    }
    let cp = prefix.checkpoint();
    let mut restored = OrderBook::restore(&cp).expect("restore");
    for ev in &events[1234..] {
        restored.apply(ev).expect("apply");
    }
    assert_eq!(restored.checkpoint(), full.checkpoint());
    assert_eq!(restored.state_summary(), full.state_summary());
}
