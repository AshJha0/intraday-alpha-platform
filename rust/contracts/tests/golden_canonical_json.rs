//! Byte-level golden of the canonical JSON writer
//! (`tests/golden/expected_canonical_json.json`): every float repr from its
//! IEEE-754 bits, every string escape from code points, every document's
//! canonical text + sha256 (and the parse → reserialise round trip through
//! the serde_json parser), the trace-id pin, and the reject cases.

use contracts::{
    canonical_json, content_hash, float_value, format_float, make_trace_id, sha256_hex,
    write_string,
};
use serde_json::Value;

fn golden() -> Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/golden/expected_canonical_json.json");
    let text = std::fs::read_to_string(&path).expect("golden file readable");
    serde_json::from_str(&text).expect("golden parses")
}

#[test]
fn float_repr_cases() {
    let g = golden();
    let cases = g["float_repr"].as_array().expect("float_repr array");
    assert!(cases.len() > 100, "golden has {} float cases", cases.len());
    for case in cases {
        let bits = u64::from_str_radix(case["bits_hex"].as_str().expect("bits_hex"), 16)
            .expect("hex bits");
        let x = f64::from_bits(bits);
        let want = case["repr"].as_str().expect("repr");
        assert_eq!(format_float(x).expect("finite"), want, "bits {bits:016x}");
        // The layout round-trips through Rust's parser to the same bits.
        let back: f64 = want.parse().expect("repr parses");
        assert_eq!(back.to_bits(), bits, "round trip of {want}");
    }
}

#[test]
fn string_escape_cases() {
    let g = golden();
    let cases = g["string_escape"].as_array().expect("string_escape array");
    assert!(!cases.is_empty());
    for case in cases {
        let cps = case["input_codepoints"].as_array().expect("code points");
        let s: String = cps
            .iter()
            .map(|c| char::from_u32(c.as_u64().expect("u32") as u32).expect("scalar"))
            .collect();
        let mut out = String::new();
        write_string(&mut out, &s);
        assert_eq!(out, case["json"].as_str().expect("json"), "input {s:?}");
        assert_eq!(
            canonical_json(&Value::String(s.clone())).expect("finite"),
            out
        );
    }
}

#[test]
fn document_cases_canonical_sha_and_round_trip() {
    let g = golden();
    let docs = g["documents"].as_array().expect("documents array");
    assert!(docs.len() >= 9);
    for doc in docs {
        let want = doc["canonical"].as_str().expect("canonical text");
        let parsed: Value = serde_json::from_str(want).expect("canonical text parses");
        let text = canonical_json(&parsed).expect("finite");
        assert_eq!(text, want);
        assert_eq!(
            sha256_hex(text.as_bytes()),
            doc["sha256"].as_str().expect("sha")
        );
        assert_eq!(
            content_hash(&parsed).expect("finite"),
            doc["sha256"].as_str().expect("sha")
        );
        // Reserialising the reparsed text is a fixed point.
        let again: Value = serde_json::from_str(&text).expect("reparse");
        assert_eq!(canonical_json(&again).expect("finite"), want);
    }
}

#[test]
fn trace_id_pin() {
    let g = golden();
    let t = &g["trace_id"];
    let inputs = &t["inputs"];
    let got = make_trace_id(
        inputs["session_id"].as_str().expect("session"),
        inputs["instrument_id"].as_u64().expect("instrument") as u32,
        inputs["event_ts"].as_i64().expect("ts"),
        inputs["sequence"].as_u64().expect("seq"),
    );
    assert_eq!(got, t["expected"].as_str().expect("expected"));
    let preimage = t["preimage"].as_str().expect("preimage");
    assert_eq!(&sha256_hex(preimage.as_bytes())[..32], got);
}

#[test]
fn reject_cases() {
    let g = golden();
    let rejects = g["rejects"].as_array().expect("rejects array");
    let mut seen = 0;
    for r in rejects {
        match r["case"].as_str().expect("case") {
            "nan" => {
                assert!(format_float(f64::NAN).is_err());
                assert!(float_value(f64::NAN).is_err());
            }
            "inf" => {
                assert!(format_float(f64::INFINITY).is_err());
                assert!(float_value(f64::INFINITY).is_err());
            }
            "-inf" => {
                assert!(format_float(f64::NEG_INFINITY).is_err());
                assert!(float_value(f64::NEG_INFINITY).is_err());
            }
            "nested nan" => {
                // A non-finite float can never enter a document: the only
                // float constructor that could carry one refuses it.
                let nested = float_value(f64::NAN).map(|v| serde_json::json!({"a": [1, {"b": v}]}));
                assert!(nested.is_err());
            }
            "integer key" => {
                // Object keys are strings by construction; the JSON grammar
                // rejects a bare integer key on input.
                assert!(serde_json::from_str::<Value>("{1: 2}").is_err());
            }
            other => panic!("unknown reject case {other}"),
        }
        seen += 1;
    }
    assert_eq!(seen, 5);
}
