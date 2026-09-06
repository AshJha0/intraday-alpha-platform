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

// ------------------------------------------------------- anomaly goldens (round 3)

fn venue_state(book: &OrderBook) -> serde_json::Value {
    serde_json::json!({
        "summary": book.state_summary(),
        "counters": book.counters,
        "stale": book.stale,
        "status": book.status,
        "has_sequence": book.has_sequence,
        "sequence_epoch": book.sequence_epoch,
        "pending_count": book.pending_count(),
    })
}

fn check_anomaly_vector(name: &str) {
    let expected: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(golden_path("expected_anomaly_states.json"))
            .expect("read expected_anomaly_states.json"),
    )
    .expect("parse expected_anomaly_states.json");
    let spec = &expected["vectors"][name];
    let events = read_jsonl(golden_path(name)).expect("anomaly vector must decode");
    assert_eq!(events.len() as u64, spec["events"].as_u64().unwrap());
    let instrument_id = spec["instrument_id"].as_u64().unwrap() as u32;
    for run in spec["runs"].as_array().unwrap() {
        let window = run["reorder_window"].as_u64().unwrap() as usize;
        let mut cons =
            orderbook::ConsolidatedBook::with_reorder_window(instrument_id, window).unwrap();
        let states = run["states"].as_object().unwrap();
        for (i, ev) in events.iter().enumerate() {
            cons.apply(ev).unwrap_or_else(|e| panic!("{name} event {}: {e}", i + 1));
            let key = (i + 1).to_string();
            let Some(exp) = states.get(&key) else { continue };
            let mut venues = serde_json::Map::new();
            for (vid, book) in &cons.books {
                venues.insert(vid.to_string(), venue_state(book));
            }
            let got = serde_json::json!({
                "venues": venues,
                "consolidated": cons.consolidated_summary(),
            });
            assert_eq!(&got, exp, "{name} window={window} index {key}");
        }
        // Accounting invariant: applied + drops + pending == events fed.
        let mut fed = std::collections::BTreeMap::new();
        for ev in &events {
            *fed.entry(ev.venue_id).or_insert(0u64) += 1;
        }
        for (vid, book) in &cons.books {
            assert_eq!(
                book.counters.events_applied + book.counters.drops() + book.pending_count() as u64,
                fed[vid],
                "{name} venue {vid}"
            );
        }
    }
}

#[test]
fn anomaly_eq_vector_states_and_counters_match_golden() {
    check_anomaly_vector("events_eq_anomalies.jsonl");
}

#[test]
fn anomaly_fx_vector_states_and_counters_match_golden() {
    check_anomaly_vector("events_fx_anomalies.jsonl");
}

#[test]
fn anomaly_vectors_byte_stable_and_checkpoint_round_trip() {
    for name in ["events_eq_anomalies.jsonl", "events_fx_anomalies.jsonl"] {
        let events = read_jsonl(golden_path(name)).expect("decode");
        let text = std::fs::read(golden_path(name)).expect("read");
        assert_eq!(marketdata::encode_jsonl(&events), text);
        for window in [0usize, 4] {
            let mut full =
                orderbook::ConsolidatedBook::with_reorder_window(events[0].instrument_id, window)
                    .unwrap();
            for ev in &events {
                full.apply(ev).unwrap();
            }
            for split in [137usize, 500, events.len() - 20] {
                let mut part = orderbook::ConsolidatedBook::with_reorder_window(
                    events[0].instrument_id,
                    window,
                )
                .unwrap();
                for ev in &events[..split] {
                    part.apply(ev).unwrap();
                }
                let json = serde_json::to_string(&part.checkpoint()).unwrap();
                let back: orderbook::ConsolidatedCheckpoint = serde_json::from_str(&json).unwrap();
                let mut resumed = orderbook::ConsolidatedBook::restore(&back).unwrap();
                for ev in &events[split..] {
                    resumed.apply(ev).unwrap();
                }
                assert_eq!(resumed.checkpoint(), full.checkpoint(), "{name} w={window} split={split}");
            }
        }
    }
}
