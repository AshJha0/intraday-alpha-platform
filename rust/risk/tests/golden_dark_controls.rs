//! `tests/golden/expected_dark_controls.json`: the B10 -> F8 golden vectors
//! of the stateless dark-first pre-trade check
//! (`docs/RESEARCH_NOTES.md` §2; the Python mirror pins the same
//! rows as `iap.execution.dark.DARK_CONTROL_GOLDENS`).

use std::collections::BTreeSet;

use risk::{dark_pretrade_check, DarkControls, VenueType, DARK_CONTROL_CODES};

fn load() -> serde_json::Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/expected_dark_controls.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn controls(v: &serde_json::Value) -> DarkControls {
    let mut c = DarkControls::default();
    if let Some(x) = v["max_participation"].as_f64() {
        c.max_participation = x;
    }
    if let Some(x) = v["participation_window_ns"].as_i64() {
        c.participation_window_ns = x;
    }
    if let Some(arr) = v["allowed_venue_types"].as_array() {
        c.allowed_venue_types = arr
            .iter()
            .map(|t| VenueType::parse(t.as_str().unwrap()).unwrap())
            .collect::<BTreeSet<_>>();
    }
    if let Some(x) = v["max_dark_notional"].as_f64() {
        c.max_dark_notional = x;
    }
    assert!(c.validation_error().is_none());
    c
}

#[test]
fn golden_dark_controls_match() {
    let golden = load();
    assert_eq!(golden["x-version"].as_u64(), Some(1));
    assert_eq!(
        golden["codes"].as_array().unwrap().iter().map(|c| c.as_str().unwrap()).collect::<Vec<_>>(),
        DARK_CONTROL_CODES
    );
    let rows = golden["rows"].as_array().unwrap();
    assert!(rows.len() >= 10, "the hand-over pins ten rows");
    for (i, row) in rows.iter().enumerate() {
        let got = dark_pretrade_check(
            &controls(&row["controls"]),
            VenueType::parse(row["venue_type"].as_str().unwrap()).unwrap(),
            row["qty"].as_i64().unwrap(),
            row["trailing_lit_volume"].as_i64().unwrap(),
            row["dark_notional_so_far"].as_f64().unwrap(),
            row["child_notional"].as_f64().unwrap(),
        )
        .unwrap();
        assert_eq!(got, row["expected"].as_str().unwrap(), "row {i}");
    }
}
