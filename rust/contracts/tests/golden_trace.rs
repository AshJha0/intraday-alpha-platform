//! Golden of the DecisionTrace records, sink and digest: the example trace of
//! `tests/golden/expected_contracts_examples.json` parsed strictly, serialised
//! canonically (line length + sha256 pinned in `expected_canonical_json.json`
//! `trace_digest`), the three stream digests, the pinned `explain()` text,
//! and the parse → serialise → parse round trip.

use std::collections::BTreeMap;

use contracts::{canonical_json, explain, sha256_hex, DecisionTrace, JsonlTraceSink, TraceDigest};
use serde_json::Value;

fn load(name: &str) -> Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden")
        .join(name);
    let text = std::fs::read_to_string(&path).expect("golden file readable");
    serde_json::from_str(&text).expect("golden parses")
}

fn example_trace(examples: &Value) -> (Value, DecisionTrace) {
    let doc = examples["examples"]["DecisionTrace"]["value"].clone();
    assert_eq!(examples["examples"]["DecisionTrace"]["x_version"], 1);
    let trace = DecisionTrace::from_value(&doc).expect("example parses strictly");
    (doc, trace)
}

#[test]
fn canonical_line_and_digests_are_pinned() {
    let examples = load("expected_contracts_examples.json");
    let pins = load("expected_canonical_json.json");
    let pins = &pins["trace_digest"];
    let (doc, trace) = example_trace(&examples);

    let line = trace.to_canonical_line().expect("valid");
    assert_eq!(
        line,
        canonical_json(&doc).expect("finite"),
        "typed == document canonical"
    );
    assert_eq!(
        line.len() as u64,
        pins["line_length"].as_u64().expect("len")
    );
    assert_eq!(
        sha256_hex(line.as_bytes()),
        pins["line_sha256"].as_str().expect("sha")
    );

    let mut one = TraceDigest::new();
    one.update(&trace).expect("valid");
    assert_eq!(
        one.hexdigest(),
        pins["digest_one_trace"].as_str().expect("one")
    );
    assert_eq!(one.count(), 1);

    let mut sink = JsonlTraceSink::new(Vec::new());
    sink.emit(&trace).expect("emit");
    sink.emit(&trace).expect("emit");
    assert_eq!(
        sink.digest().hexdigest(),
        pins["digest_same_trace_twice"].as_str().expect("two")
    );
    let text = String::from_utf8(sink.into_inner()).expect("ascii");
    assert_eq!(text, format!("{line}\n{line}\n"));
    let reread = TraceDigest::of_jsonl(&text).expect("re-read");
    assert_eq!(
        reread.hexdigest(),
        pins["digest_same_trace_twice"].as_str().expect("two")
    );

    assert_eq!(
        TraceDigest::new().hexdigest(),
        pins["digest_empty_stream"].as_str().expect("empty")
    );
}

#[test]
fn explain_text_is_pinned() {
    let examples = load("expected_contracts_examples.json");
    let (_, trace) = example_trace(&examples);
    let mut names: BTreeMap<u16, String> = BTreeMap::new();
    for (k, v) in examples["explain"]["venue_names"]
        .as_object()
        .expect("names")
    {
        names.insert(
            k.parse().expect("venue id"),
            v.as_str().expect("name").to_string(),
        );
    }
    assert_eq!(
        explain(&trace, &names),
        examples["explain"]["text"].as_str().expect("text")
    );
    // Unnamed venues render as their decimal id.
    let partial: BTreeMap<u16, String> = names
        .iter()
        .filter(|(k, _)| **k != 3)
        .map(|(k, v)| (*k, v.clone()))
        .collect();
    assert!(explain(&trace, &partial).contains("XV2 = 35%  3 = 20%"));
}

#[test]
fn explain_labels_the_acting_signal_and_its_components() {
    // signal[0] is the acting signal (the order's alpha id); every further
    // signal is a component labelled by its own model_version.
    let examples = load("expected_contracts_examples.json");
    let (doc, trace) = example_trace(&examples);
    let mut multi = doc.clone();
    let mut member = doc["stages"]["signal"][0].clone();
    member["model_version"] = Value::from("EQ01");
    member["expected_return"] = Value::from(0.0001);
    member["confidence"] = Value::from(0.5);
    multi["stages"]["signal"]
        .as_array_mut()
        .expect("signal array")
        .push(member);
    let multi = DecisionTrace::from_value(&multi).expect("two signals parse");
    let names = BTreeMap::new();
    let pinned: Vec<String> = explain(&trace, &names).lines().map(String::from).collect();
    let lines: Vec<String> = explain(&multi, &names).lines().map(String::from).collect();
    assert_eq!(lines[1], pinned[1]);
    assert_eq!(
        lines[2],
        "Alpha:      EQ01  expected return = +1.0 bps  confidence = 0.50"
    );
    assert_eq!(&lines[3..], &pinned[2..]);
}

#[test]
fn round_trip_equality_and_strictness() {
    let examples = load("expected_contracts_examples.json");
    let (doc, trace) = example_trace(&examples);
    let line = trace.to_canonical_line().expect("valid");
    let back = DecisionTrace::from_json_line(&line).expect("line parses");
    assert_eq!(back, trace);
    assert_eq!(back.to_value().expect("valid"), doc);

    // An unknown key anywhere is rejected.
    let mut extra = doc.clone();
    extra["stages"]["attribution"]["extra"] = Value::from(1);
    assert!(DecisionTrace::from_value(&extra).is_err());
    // A wrong wire enum is rejected.
    let mut bad_enum = doc.clone();
    bad_enum["stages"]["risk"][0]["decision"] = Value::from(7);
    assert!(DecisionTrace::from_value(&bad_enum).is_err());
    // ALLOW with a rule index is a domain violation.
    let mut bad_rule = doc.clone();
    bad_rule["stages"]["risk"][0]["rule_index"] = Value::from(3);
    assert!(DecisionTrace::from_value(&bad_rule).is_err());
    // Header / trace id consistency.
    let mut bad_id = doc;
    bad_id["sequence"] = Value::from(501);
    assert!(DecisionTrace::from_value(&bad_id).is_err());
}

#[test]
fn every_nested_example_parses_into_its_record() {
    let examples = load("expected_contracts_examples.json");
    let ex = &examples["examples"];
    type Parses = fn(&Value) -> bool;
    let checks: [(&str, Parses); 12] = [
        ("AlphaSignal", |v| {
            serde_json::from_value::<contracts::AlphaSignal>(v.clone()).is_ok()
        }),
        ("PortfolioLeg", |v| {
            serde_json::from_value::<contracts::PortfolioLeg>(v.clone()).is_ok()
        }),
        ("PortfolioTarget", |v| {
            serde_json::from_value::<contracts::PortfolioTarget>(v.clone()).is_ok()
        }),
        ("RiskDecision", |v| {
            serde_json::from_value::<contracts::RiskDecision>(v.clone()).is_ok()
        }),
        ("ParentOrder", |v| {
            serde_json::from_value::<contracts::ParentOrder>(v.clone()).is_ok()
        }),
        ("ChildOrder", |v| {
            serde_json::from_value::<contracts::ChildOrder>(v.clone()).is_ok()
        }),
        ("VenueScore", |v| {
            serde_json::from_value::<contracts::VenueScore>(v.clone()).is_ok()
        }),
        ("VenueDecision", |v| {
            serde_json::from_value::<contracts::VenueDecision>(v.clone()).is_ok()
        }),
        ("ExecutionReport", |v| {
            serde_json::from_value::<contracts::ExecutionReport>(v.clone()).is_ok()
        }),
        ("LatencyStats", |v| {
            serde_json::from_value::<contracts::LatencyStats>(v.clone()).is_ok()
        }),
        ("TCAResult", |v| {
            serde_json::from_value::<contracts::TcaResult>(v.clone()).is_ok()
        }),
        ("Attribution", |v| {
            serde_json::from_value::<contracts::Attribution>(v.clone()).is_ok()
        }),
    ];
    for (name, ok) in checks {
        let value = &ex[name]["value"];
        assert!(!value.is_null(), "example {name} present");
        assert!(ok(value), "example {name} parses into its record");
    }
}
