//! Golden suite for the marketdata crate (API_CORE.md §6):
//! SplitMix64 known answers, IAP1 SHA-256 byte-parity, JSONL byte-identical
//! re-encode. All integer comparisons exact; uniforms bit-exact doubles.

mod support;

use marketdata::{
    decode_iap1, encode_iap1, encode_jsonl, read_jsonl, validation_error, MarketEvent, SplitMix64,
};
use support::{golden_path, sha256_hex};


fn load_json(name: &str) -> serde_json::Value {
    let text = std::fs::read_to_string(golden_path(name))
        .unwrap_or_else(|e| panic!("reading golden {name}: {e}"));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("parsing golden {name}: {e}"))
}

fn eq_events() -> Vec<MarketEvent> {
    read_jsonl(golden_path("events_eq_mbo.jsonl")).expect("golden EQ vector must decode")
}

fn fx_events() -> Vec<MarketEvent> {
    read_jsonl(golden_path("events_fx_quote.jsonl")).expect("golden FX vector must decode")
}

#[test]
fn splitmix64_known_answers() {
    let golden = load_json("splitmix64.json");
    let seed = golden["seed"].as_u64().expect("seed");
    let mut rng = SplitMix64::new(seed);
    let expected_u64: Vec<u64> = golden["first_5_u64"]
        .as_array()
        .expect("first_5_u64")
        .iter()
        .map(|v| v.as_u64().expect("u64 entry"))
        .collect();
    for (i, &exp) in expected_u64.iter().enumerate() {
        assert_eq!(rng.next_u64(), exp, "u64 draw {i}");
    }

    let mut rng = SplitMix64::new(seed);
    let expected_uniform: Vec<f64> = golden["first_5_uniform"]
        .as_array()
        .expect("first_5_uniform")
        .iter()
        .map(|v| v.as_f64().expect("f64 entry"))
        .collect();
    for (i, &exp) in expected_uniform.iter().enumerate() {
        let got = rng.uniform();
        assert_eq!(got.to_bits(), exp.to_bits(), "uniform draw {i}: {got} vs {exp}");
    }
}

#[test]
fn golden_vectors_load_and_validate() {
    let eq = eq_events();
    assert_eq!(eq.len(), 2000);
    assert_eq!(eq[0].event_id, 1);
    assert_eq!(eq[eq.len() - 1].event_id, 2000);
    for ev in &eq {
        assert_eq!(ev.instrument_id, 1);
        assert_eq!(ev.venue_id, 1);
        assert_eq!(validation_error(ev), None, "event_id {}", ev.event_id);
    }
    let fx = fx_events();
    assert_eq!(fx.len(), 800);
    for ev in &fx {
        assert_eq!(ev.instrument_id, 101);
        assert!(matches!(ev.venue_id, 10..=12));
        assert_eq!(validation_error(ev), None, "event_id {}", ev.event_id);
    }
}

#[test]
fn iap1_sha256_matches_golden_eq() {
    let expected = load_json("expected_codec_sha256.json");
    let hash = sha256_hex(&encode_iap1(&eq_events()));
    assert_eq!(hash, expected["events_eq_mbo.iap1"].as_str().expect("hash"));
}

#[test]
fn iap1_sha256_matches_golden_fx() {
    let expected = load_json("expected_codec_sha256.json");
    let hash = sha256_hex(&encode_iap1(&fx_events()));
    assert_eq!(hash, expected["events_fx_quote.iap1"].as_str().expect("hash"));
}

#[test]
fn iap1_roundtrip_both_vectors() {
    for events in [eq_events(), fx_events()] {
        let decoded = decode_iap1(&encode_iap1(&events)).expect("roundtrip");
        assert_eq!(decoded, events);
    }
}

#[test]
fn jsonl_reencode_is_byte_identical() {
    let eq_bytes = std::fs::read(golden_path("events_eq_mbo.jsonl")).expect("read eq");
    assert_eq!(encode_jsonl(&eq_events()), eq_bytes);
    let fx_bytes = std::fs::read(golden_path("events_fx_quote.jsonl")).expect("read fx");
    assert_eq!(encode_jsonl(&fx_events()), fx_bytes);
}

#[test]
fn vendored_sha256_known_answers() {
    // FIPS 180-4 test vectors, so codec parity failures implicate the codec.
    assert_eq!(
        sha256_hex(b""),
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    );
    assert_eq!(
        sha256_hex(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}

#[test]
fn jsonl_reject_cases_fixture_parity() {
    // Shared fixture (tests/golden/jsonl_reject_cases.txt): every line before
    // "# ACCEPT" must be rejected, every line after it accepted (round trip).
    let text = std::fs::read_to_string(support::golden_path("jsonl_reject_cases.txt"))
        .expect("read jsonl_reject_cases.txt");
    let (mut rejected, mut accepted, mut accept_block) = (0usize, 0usize, false);
    for line in text.lines() {
        if line == "# ACCEPT" {
            accept_block = true;
            continue;
        }
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if accept_block {
            let ev = marketdata::decode_jsonl_line(line)
                .unwrap_or_else(|e| panic!("must accept {line:?}: {e}"));
            assert_eq!(
                marketdata::decode_jsonl_line(&marketdata::encode_jsonl_line(&ev)).unwrap(),
                ev
            );
            accepted += 1;
        } else {
            assert!(marketdata::decode_jsonl_line(line).is_err(), "must reject {line:?}");
            rejected += 1;
        }
    }
    assert!(rejected >= 25 && accepted >= 5);
}

#[test]
fn iap1_v2_trailer_detects_bit_flip_in_golden_vector() {
    let events = marketdata::read_jsonl(support::golden_path("events_eq_mbo.jsonl")).unwrap();
    let mut data = marketdata::encode_iap1(&events);
    assert_eq!(data.len(), 16 + 72 * events.len() + 16);
    data[16 + 72 * 499 + 14] = 0; // event_type of record 500
    assert!(marketdata::decode_iap1(&data).is_err());
    // Legacy v1 form (no trailer) still loads; the book then drops the record.
    let n = data.len();
    data.truncate(n - 16);
    data[4] = 1;
    let legacy = marketdata::decode_iap1_ex(&data).unwrap();
    assert!(!legacy.integrity_checked);
    assert_eq!(legacy.events.len(), 2000);
    assert_eq!(legacy.events[499].event_type, 0);
}
