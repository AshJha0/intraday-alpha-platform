//! Golden suite for the native feature engine (API_FEATURES.md §5).
//!
//! Drives the pinned golden vectors through the engine at cadence 0 and
//! compares every checkpoint feature this engine computes natively — by
//! registry name, valid flags exact, values at abs 1e-9 / rel 1e-9. Also
//! verifies the registry contract (`registry_hash`, `registered_count`).

mod support;

use std::collections::BTreeMap;

use features::{FeatureEngine, FeatureVector, FEATURE_NAMES};
use marketdata::{read_jsonl, MarketEvent};
use support::{golden_path, sha256_hex};

const ABS_TOL: f64 = 1e-9;
const REL_TOL: f64 = 1e-9;

fn load_json(name: &str) -> serde_json::Value {
    let text = std::fs::read_to_string(golden_path(name))
        .unwrap_or_else(|e| panic!("reading golden {name}: {e}"));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("parsing golden {name}: {e}"))
}

fn engine_for(instrument_id: u32, tick: f64) -> FeatureEngine {
    let mut ticks = BTreeMap::new();
    ticks.insert(instrument_id, tick);
    FeatureEngine::new(ticks, 0).expect("engine construction")
}

/// Run `events` through a fresh engine, returning the vector emitted after
/// each event (cadence 0).
fn run_vectors(events: &[MarketEvent], instrument_id: u32, tick: f64) -> Vec<FeatureVector> {
    let mut eng = engine_for(instrument_id, tick);
    events
        .iter()
        .map(|ev| {
            eng.apply(ev)
                .expect("golden events apply cleanly")
                .expect("cadence 0 emits after every event")
        })
        .collect()
}

fn eq_vectors() -> Vec<FeatureVector> {
    let events = read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("EQ vector");
    run_vectors(&events, 1, 0.01)
}

fn fx_vectors() -> Vec<FeatureVector> {
    let events = read_jsonl(golden_path("events_fx_quote.jsonl")).expect("FX vector");
    run_vectors(&events, 101, 1e-5)
}

/// Compare one golden checkpoint against the vector emitted after its
/// (1-based) event index. Returns the number of natively-compared features.
fn check_checkpoint(golden: &serde_json::Value, key: &str, index: &str, vec: &FeatureVector) -> usize {
    let cp = &golden[key]["checkpoints"][index];
    assert!(!cp.is_null(), "{key} checkpoint {index} present");
    assert_eq!(
        vec.timestamp,
        cp["timestamp"].as_i64().expect("checkpoint timestamp"),
        "{key}@{index}: emission timestamp"
    );
    let feats = cp["features"].as_object().expect("features map");
    let mut compared = 0;
    for (name, entry) in feats {
        let Some(slot) = features::feature_index(name) else {
            continue; // feature family not ported natively
        };
        let want_valid = entry["valid"].as_bool().expect("valid flag");
        let got_valid = vec.validity[slot];
        assert_eq!(
            got_valid, want_valid,
            "{key}@{index} {name}: validity (got value {})",
            vec.values[slot]
        );
        if want_valid {
            let want = entry["value"].as_f64().expect("value");
            let got = vec.values[slot];
            let tol = ABS_TOL + REL_TOL * want.abs();
            assert!(
                (got - want).abs() <= tol,
                "{key}@{index} {name}: got {got}, want {want} (tol {tol})"
            );
        }
        compared += 1;
    }
    assert!(
        compared >= 15,
        "{key}@{index}: expected a meaningful native overlap, compared only {compared}"
    );
    compared
}

#[test]
fn eq_golden_checkpoints_match() {
    let golden = load_json("expected_features.json");
    let vectors = eq_vectors();
    for index in ["500", "1000", "1500", "2000"] {
        let row: usize = index.parse::<usize>().unwrap() - 1;
        check_checkpoint(&golden, "eq", index, &vectors[row]);
    }
}

#[test]
fn fx_golden_checkpoints_match() {
    let golden = load_json("expected_features.json");
    let vectors = fx_vectors();
    for index in ["400", "800"] {
        let row: usize = index.parse::<usize>().unwrap() - 1;
        check_checkpoint(&golden, "fx", index, &vectors[row]);
    }
}

#[test]
fn registry_contract_agrees() {
    // A port loading the registry must agree on hash and count
    // (API_FEATURES.md §5): sha256 of the canonical JSON (sorted keys,
    // compact separators) of the registry entries.
    let golden = load_json("expected_features.json");
    let reg_text = std::fs::read_to_string(
        golden_path("expected_features.json")
            .parent()
            .unwrap()
            .join("../../data/reference/feature_registry.json"),
    )
    .expect("feature_registry.json");
    let reg: serde_json::Value = serde_json::from_str(&reg_text).expect("registry JSON");
    // serde_json maps are BTreeMaps: re-serializing compactly yields
    // sorted-key canonical JSON, matching Python's json.dumps(sort_keys).
    let canonical = serde_json::to_string(&reg["features"]).expect("canonical registry");
    let hash = sha256_hex(canonical.as_bytes());
    assert_eq!(hash, reg["registry_hash"].as_str().unwrap(), "file self-hash");
    assert_eq!(hash, golden["registry_hash"].as_str().unwrap(), "golden hash");
    assert_eq!(reg["count"].as_u64(), Some(205));
    assert_eq!(golden["registered_count"].as_u64(), Some(205));
    assert_eq!(reg["features"].as_array().unwrap().len(), 205);
    // every native name (and auxiliary) must exist in the registry
    let names: Vec<&str> = reg["features"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["name"].as_str().unwrap())
        .collect();
    for name in FEATURE_NAMES {
        assert!(names.contains(&name), "registry missing {name}");
    }
}

#[test]
fn nan_never_valid_and_bits_pack() {
    for vec in eq_vectors().iter().chain(fx_vectors().iter()) {
        for (i, &v) in vec.values.iter().enumerate() {
            if vec.validity[i] {
                assert!(v.is_finite(), "valid slot {i} carries non-finite {v}");
            } else {
                assert!(v.is_nan(), "invalid slot {i} must carry NaN, got {v}");
            }
        }
        let bits = vec.validity_bits();
        for (i, &v) in vec.validity.iter().enumerate() {
            assert_eq!((bits[i >> 3] >> (i & 7)) & 1 == 1, v, "bit {i}");
        }
    }
}

#[test]
fn warmup_gates_windowed_features() {
    let events = read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("EQ vector");
    let first_ts = events[0].exchange_ts;
    let vectors = eq_vectors();
    let w30 = features::feature_index("ofi_l1_w30s_v1").unwrap();
    let sv1m = features::feature_index("signed_volume_w1m_v1").unwrap();
    for (ev, vec) in events.iter().zip(&vectors) {
        let elapsed = ev.exchange_ts - first_ts;
        if elapsed < 30_000_000_000 {
            assert!(!vec.validity[w30], "ofi_l1_w30s valid during warmup");
        }
        if elapsed < 60_000_000_000 {
            assert!(!vec.validity[sv1m], "signed_volume_w1m valid during warmup");
        } else {
            assert!(vec.validity[sv1m], "signed_volume_w1m invalid after warmup");
        }
    }
    // the 30s window becomes valid eventually
    assert!(vectors.last().unwrap().validity[w30]);
}

#[test]
fn cadence_limits_emissions() {
    let events = read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("EQ vector");
    let mut ticks = BTreeMap::new();
    ticks.insert(1u32, 0.01);
    let mut eng = FeatureEngine::new(ticks, 5_000_000_000).unwrap(); // 5s cadence
    let mut emitted = Vec::new();
    for ev in &events {
        if let Some(v) = eng.apply(ev).unwrap() {
            emitted.push(v.timestamp);
        }
    }
    assert!(!emitted.is_empty() && emitted.len() < events.len());
    for pair in emitted.windows(2) {
        assert!(
            pair[1] - pair[0] >= 5_000_000_000,
            "cadence violated: {} -> {}",
            pair[0],
            pair[1]
        );
    }
}

// ---------------------------------------------------------------------------
// Anomaly-vector golden (API_FEATURES.md §2 ingestion rules)
// ---------------------------------------------------------------------------

/// Replay one anomaly vector and check every pinned checkpoint: emission
/// timestamp, engine drop counters, stale-recovery count and the full
/// native-45 sub-vector.
fn check_anomaly_side(golden: &serde_json::Value, side: &str, tick: f64) {
    let doc = &golden[side];
    let vector = doc["vector"].as_str().expect("vector name");
    let instrument_id = doc["instrument_id"].as_u64().expect("instrument") as u32;
    let events = read_jsonl(golden_path(vector)).expect("anomaly vector");
    assert_eq!(
        events.len() as u64,
        doc["n_events"].as_u64().expect("n_events"),
        "{side}: vector length"
    );
    let mut eng = engine_for(instrument_id, tick);
    let cps = doc["checkpoints"].as_object().expect("checkpoints");
    let mut checked = 0usize;
    for (i, ev) in events.iter().enumerate() {
        let vec = eng.apply(ev).expect("anomaly events never error");
        let key = (i + 1).to_string();
        let Some(cp) = cps.get(&key) else { continue };
        let vec = vec.unwrap_or_else(|| panic!("{side}@{key}: cadence 0 must emit"));
        assert_eq!(
            vec.timestamp,
            cp["timestamp"].as_i64().unwrap(),
            "{side}@{key}: timestamp"
        );
        assert_eq!(
            eng.events_processed,
            cp["events_processed"].as_u64().unwrap(),
            "{side}@{key}: events_processed"
        );
        assert_eq!(
            eng.events_dropped,
            cp["events_dropped"].as_u64().unwrap(),
            "{side}@{key}: events_dropped (only APPLIED events feed state)"
        );
        assert_eq!(
            eng.ts_regressions_dropped,
            cp["ts_regressions_dropped"].as_u64().unwrap(),
            "{side}@{key}: ts_regressions_dropped"
        );
        assert_eq!(
            eng.oversized_qty_dropped,
            cp["oversized_qty_dropped"].as_u64().unwrap(),
            "{side}@{key}: oversized_qty_dropped"
        );
        assert_eq!(
            eng.oversized_depth_skipped,
            cp["oversized_depth_skipped"].as_u64().unwrap(),
            "{side}@{key}: oversized_depth_skipped"
        );
        assert_eq!(
            eng.recoveries(instrument_id),
            cp["recoveries"].as_u64().unwrap(),
            "{side}@{key}: stale->fresh recoveries"
        );
        assert_eq!(
            eng.warm_ts(instrument_id),
            cp["warm_ts"].as_i64(),
            "{side}@{key}: warmup anchor"
        );
        assert_eq!(
            eng.book_ok(instrument_id),
            cp["book_ok"].as_bool().unwrap(),
            "{side}@{key}: book_ok"
        );
        for (name, entry) in cp["features"].as_object().expect("features") {
            let slot = features::feature_index(name)
                .unwrap_or_else(|| panic!("{name} must be a native feature"));
            let want_valid = entry["valid"].as_bool().unwrap();
            assert_eq!(
                vec.validity[slot], want_valid,
                "{side}@{key} {name}: validity"
            );
            if want_valid {
                let want = entry["value"].as_f64().unwrap();
                let got = vec.values[slot];
                assert!(
                    (got - want).abs() <= ABS_TOL + REL_TOL * want.abs(),
                    "{side}@{key} {name}: {got} != {want}"
                );
                checked += 1;
            }
        }
    }
    assert_eq!(cps.len(), events.iter().enumerate().filter(|(i, _)| cps.contains_key(&(i + 1).to_string())).count());
    assert!(checked > 100, "{side}: too few valid features compared");
}

#[test]
fn anomaly_golden_eq_matches() {
    let golden = load_json("expected_features_anomalies.json");
    check_anomaly_side(&golden, "eq", 0.01);
}

#[test]
fn anomaly_golden_fx_matches() {
    let golden = load_json("expected_features_anomalies.json");
    check_anomaly_side(&golden, "fx", 1e-5);
}

#[test]
fn anomaly_golden_exercises_the_drop_paths() {
    let golden = load_json("expected_features_anomalies.json");
    for side in ["eq", "fx"] {
        let cps = golden[side]["checkpoints"].as_object().unwrap();
        let last = cps
            .iter()
            .max_by_key(|(k, _)| k.parse::<u64>().unwrap())
            .unwrap()
            .1;
        assert!(last["events_dropped"].as_u64().unwrap() > 0, "{side}");
        assert!(last["ts_regressions_dropped"].as_u64().unwrap() > 0, "{side}");
        assert!(last["recoveries"].as_u64().unwrap() > 0, "{side}");
    }
}
