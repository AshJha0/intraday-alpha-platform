//! Golden suite for the alpha crate (API_ALPHA.md §§6-7).
//!
//! Verified here, all against `tests/golden/expected_alpha.json`:
//!
//! 1. the fitted params in `configs/strategies/alpha_params.json` agree
//!    with the copies embedded in the golden file;
//! 2. every golden case (all 6 alphas, 30 cases) is reproduced from its
//!    embedded inputs — scoring math validated independently of any
//!    feature engine (FX05 additionally reproduces its raw residual from
//!    the embedded 8-pair grid cross-section);
//! 3. EQ01/EQ03/EQ06/FX01/FX09 are reproduced end-to-end: the native
//!    feature engine is driven over the golden vectors and must produce
//!    the embedded input features AND the same scores at the pinned rows;
//! 4. the pinned IC windows are reproduced end-to-end (scores from our
//!    engine, labels recomputed per the pinned label semantics).
//!
//! FX05 documented deviation: its 5 cases embed everything needed and are
//! fully verified; its pinned IC uses the bundled day-2 parquet feature
//! frames (Python-owned storage), which this port does not read — the IC
//! metadata (grid step, target pair, horizon) is still cross-checked.

use std::collections::BTreeMap;

use alpha::{
    fx05_raw_signals, load_params_file, mid_labels, pearson_ic, raw_signal, score_linear_z,
    AlphaParams, MidSeries, GRID_STEP_NS,
};
use features::{FeatureEngine, FeatureVector};
use marketdata::read_jsonl;

const ABS_TOL: f64 = 1e-9;
const REL_TOL: f64 = 1e-9;

fn repo_path(rel: &str) -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").join(rel)
}

fn load_golden() -> serde_json::Value {
    let text = std::fs::read_to_string(repo_path("tests/golden/expected_alpha.json"))
        .expect("expected_alpha.json");
    serde_json::from_str(&text).expect("valid golden JSON")
}

fn load_config_params() -> BTreeMap<String, AlphaParams> {
    load_params_file(repo_path("configs/strategies/alpha_params.json")).expect("alpha params load")
}

fn close(got: f64, want: f64) -> bool {
    (got - want).abs() <= ABS_TOL + REL_TOL * want.abs()
}

/// Drive one golden vector through the native feature engine at cadence 0.
fn engine_vectors(vector: &str, instrument_id: u32, tick: f64) -> (Vec<FeatureVector>, MidSeries, i64) {
    let events = read_jsonl(repo_path(&format!("tests/golden/{vector}"))).expect("golden vector");
    let mut ticks = BTreeMap::new();
    ticks.insert(instrument_id, tick);
    let mut eng = FeatureEngine::new(ticks, 0).unwrap();
    let mut series = MidSeries::new();
    let mut vectors = Vec::with_capacity(events.len());
    let mut last_ts = 0i64;
    for ev in &events {
        let vec = eng.apply(ev).unwrap().expect("cadence 0 emits");
        last_ts = ev.exchange_ts;
        if let Some((mid, _hs)) = eng.mid_state(instrument_id) {
            series.append(ev.exchange_ts, mid);
        }
        vectors.push(vec);
    }
    (vectors, series, last_ts)
}

// ---------------------------------------------------------------- params

#[test]
fn config_params_match_golden_embedding() {
    let golden = load_golden();
    let cfg = load_config_params();
    for aid in alpha::GOLDEN_ALPHA_IDS {
        let p = &cfg[aid];
        let g = &golden["params"][aid];
        assert_eq!(p.mu, g["mu"].as_f64().unwrap(), "{aid} mu");
        assert_eq!(p.sigma, g["sigma"].as_f64().unwrap(), "{aid} sigma");
        assert_eq!(p.beta, g["beta"].as_f64().unwrap(), "{aid} beta");
        assert_eq!(p.beta_fit, g["beta_fit"].as_f64().unwrap(), "{aid} beta_fit");
        assert_eq!(p.z_clip, g["z_clip"].as_f64().unwrap(), "{aid} z_clip");
        assert_eq!(p.conf_scale, g["conf_scale"].as_f64().unwrap(), "{aid} conf_scale");
        assert_eq!(p.horizon, g["horizon"].as_str().unwrap(), "{aid} horizon");
        // beta must equal beta_fit — ports must not "fix" signs (spec §32)
        assert_eq!(p.beta, p.beta_fit, "{aid}: beta != beta_fit");
    }
}

// ------------------------------------------------- embedded-input parity

fn check_embedded_single_frame(aid: &str) {
    let golden = load_golden();
    let cfg = load_config_params();
    let p = &cfg[aid];
    let cases = golden["alphas"][aid]["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 5, "{aid}: 5 pinned cases");
    for case in cases {
        let inputs = case["inputs"].as_object().expect("inputs");
        let get = |name: &str| -> Option<f64> { inputs.get(name).and_then(|v| v.as_f64()) };
        let raw = raw_signal(aid, &get).expect("known alpha");
        let (er, conf) = score_linear_z(p, raw);
        let want_er = case["expected_return"].as_f64().unwrap();
        let want_conf = case["confidence"].as_f64().unwrap();
        assert!(
            close(er, want_er),
            "{aid} case @{}: er {er} vs {want_er}",
            case["event_index_1based"]
        );
        assert!(
            close(conf, want_conf),
            "{aid} case @{}: conf {conf} vs {want_conf}",
            case["event_index_1based"]
        );
    }
}

#[test]
fn eq01_embedded_cases() {
    check_embedded_single_frame("EQ01");
}

#[test]
fn eq03_embedded_cases() {
    check_embedded_single_frame("EQ03");
}

#[test]
fn eq06_embedded_cases() {
    check_embedded_single_frame("EQ06");
}

#[test]
fn fx01_embedded_cases() {
    check_embedded_single_frame("FX01");
}

#[test]
fn fx09_embedded_cases() {
    check_embedded_single_frame("FX09");
}

#[test]
fn fx05_embedded_cases() {
    let golden = load_golden();
    let cfg = load_config_params();
    let p = &cfg["FX05"];
    let fx05 = &golden["alphas"]["FX05"];
    let cases = fx05["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 5);
    let icw = &fx05["ic_window"];
    // cross-check the pinned grid metadata against our constants
    assert_eq!(icw["grid_step_ns"].as_i64(), Some(GRID_STEP_NS));
    assert_eq!(icw["target_pair"].as_u64(), Some(102));
    assert_eq!(icw["horizon"].as_str(), Some("5m"));
    for case in cases {
        let target = case["target_pair"].as_u64().unwrap() as u32;
        let inputs = case["inputs"].as_object().expect("inputs");
        let mut pair_ids: Vec<u32> = inputs.keys().map(|k| k.parse().unwrap()).collect();
        pair_ids.sort_unstable();
        let returns: Vec<f64> = pair_ids
            .iter()
            .map(|pid| inputs[&pid.to_string()].as_f64().unwrap_or(f64::NAN))
            .collect();
        let signals = fx05_raw_signals(&pair_ids, &returns).expect("solve");
        let tgt = pair_ids.iter().position(|&pid| pid == target).unwrap();
        let raw = signals[tgt];
        let want_raw = case["raw_residual_signal"].as_f64().unwrap();
        assert!(
            close(raw, want_raw),
            "FX05 grid {}: raw {raw} vs {want_raw}",
            case["grid_index_0based"]
        );
        let (er, conf) = score_linear_z(p, Some(raw));
        assert!(close(er, case["expected_return"].as_f64().unwrap()), "FX05 er");
        assert!(close(conf, case["confidence"].as_f64().unwrap()), "FX05 conf");
    }
}

// -------------------------------------------- engine-driven case parity

fn check_engine_driven(aid: &str, vectors: &[FeatureVector]) {
    let golden = load_golden();
    let cfg = load_config_params();
    let p = &cfg[aid];
    for case in golden["alphas"][aid]["cases"].as_array().unwrap() {
        let row = case["event_index_1based"].as_u64().unwrap() as usize - 1;
        let vec = &vectors[row];
        assert_eq!(
            vec.timestamp,
            case["exchange_ts"].as_i64().unwrap(),
            "{aid} row {row}: timestamp"
        );
        // our engine must reproduce the embedded input features
        for (name, want) in case["inputs"].as_object().unwrap() {
            let got = vec.get(name);
            match want.as_f64() {
                Some(w) => {
                    let g = got.unwrap_or_else(|| {
                        panic!("{aid} row {row}: engine says {name} invalid, golden has {w}")
                    });
                    assert!(close(g, w), "{aid} row {row} {name}: {g} vs {w}");
                }
                None => assert!(got.is_none(), "{aid} row {row}: {name} should be invalid"),
            }
        }
        // and the same scores end-to-end
        let get = |name: &str| vec.get(name);
        let raw = raw_signal(aid, &get).expect("known alpha");
        let (er, conf) = score_linear_z(p, raw);
        assert!(close(er, case["expected_return"].as_f64().unwrap()), "{aid} row {row} er");
        assert!(close(conf, case["confidence"].as_f64().unwrap()), "{aid} row {row} conf");
    }
}

/// Scores per row (`NaN` where confidence <= 0, for the IC) plus the IC
/// reproduction for one alpha over its pinned window.
fn check_ic(
    aid: &str,
    vectors: &[FeatureVector],
    series: &MidSeries,
    last_ts: i64,
) {
    let golden = load_golden();
    let cfg = load_config_params();
    let p = &cfg[aid];
    let icw = &golden["alphas"][aid]["ic_window"];
    let lo = icw["rows_0based"][0].as_u64().unwrap() as usize;
    let hi = icw["rows_0based"][1].as_u64().unwrap() as usize;
    let horizon = icw["horizon"].as_str().unwrap();
    let h_ns: i64 = match horizon {
        "1s" => 1_000_000_000,
        "5s" => 5_000_000_000,
        "10s" => 10_000_000_000,
        "1m" => 60_000_000_000,
        other => panic!("unexpected golden IC horizon {other}"),
    };
    let anchors: Vec<i64> = vectors.iter().map(|v| v.timestamp).collect();
    let labels = mid_labels(&anchors, series, last_ts, h_ns);
    let scores: Vec<f64> = vectors
        .iter()
        .map(|vec| {
            let get = |name: &str| vec.get(name);
            let (er, conf) = score_linear_z(p, raw_signal(aid, &get).unwrap());
            if conf > 0.0 {
                er
            } else {
                f64::NAN
            }
        })
        .collect();
    let got = pearson_ic(&scores[lo..hi], &labels[lo..hi], 32);
    let want = icw["ic"].as_f64().unwrap();
    assert!(
        close(got, want),
        "{aid} IC over rows [{lo}, {hi}) at {horizon}: {got} vs {want}"
    );
}

#[test]
fn eq_alphas_engine_driven_parity_and_ic() {
    let (vectors, series, last_ts) = engine_vectors("events_eq_mbo.jsonl", 1, 0.01);
    for aid in ["EQ01", "EQ03", "EQ06"] {
        check_engine_driven(aid, &vectors);
        check_ic(aid, &vectors, &series, last_ts);
    }
}

#[test]
fn fx_alphas_engine_driven_parity_and_ic() {
    let (vectors, series, last_ts) = engine_vectors("events_fx_quote.jsonl", 101, 1e-5);
    for aid in ["FX01", "FX09"] {
        check_engine_driven(aid, &vectors);
        check_ic(aid, &vectors, &series, last_ts);
    }
}

#[test]
fn signals_respect_contract_invariants() {
    // Over every engine-driven row of both vectors: expected_return finite
    // always; conf == 0 implies er == 0.0 exactly; conf in [0, 1].
    let cfg = load_config_params();
    let (eq, _, _) = engine_vectors("events_eq_mbo.jsonl", 1, 0.01);
    let (fx, _, _) = engine_vectors("events_fx_quote.jsonl", 101, 1e-5);
    for (aids, vectors) in [
        (["EQ01", "EQ03", "EQ06"].as_slice(), &eq),
        (["FX01", "FX09"].as_slice(), &fx),
    ] {
        for &aid in aids {
            let p = &cfg[aid];
            for vec in vectors.iter() {
                let get = |name: &str| vec.get(name);
                let sig = alpha::score_row(p, vec.instrument_id, vec.timestamp, raw_signal(aid, &get).unwrap());
                assert!(sig.expected_return.is_finite());
                assert!((0.0..=1.0).contains(&sig.confidence));
                if sig.confidence == 0.0 {
                    assert_eq!(sig.expected_return, 0.0, "{aid}: conf 0 must mean er 0.0");
                }
            }
        }
    }
}
