//! Cross-language parity of the JSONL whitespace set (schemas/FORMAT.md §1).
//!
//! The pinned set is ASCII space, tab, CR and LF — exactly the `[ \t\r\n]*`
//! of the reference decoder's line regex and of this crate's `Scanner`.
//! `decode_jsonl` used `str::trim`, which strips every Unicode `White_Space`
//! char, so a line prefixed with VT (`\x0b`), FF (`\x0c`) or NBSP (`\u{a0}`)
//! was ingested here and rejected by the C++ port: one port reading a file
//! another refuses, in a codec whose whole contract is byte-identical
//! cross-language behaviour.

use marketdata::codec::decode_jsonl;
use marketdata::decode_jsonl_line;

const BODY: &str = concat!(
    r#"{"event_id":1,"instrument_id":2,"venue_id":3,"exchange_ts":4,"#,
    r#""receive_ts":5,"sequence":6,"event_type":1,"side":0,"#,
    r#""price_ticks":7,"qty":8,"order_id":9,"trade_id":0}"#
);

#[test]
fn ascii_whitespace_is_accepted_around_a_line() {
    for ws in [" ", "\t", "\r"] {
        let line = format!("{ws}{BODY}{ws}");
        let ev = decode_jsonl_line(&line)
            .unwrap_or_else(|e| panic!("{ws:?} rejected by decode_jsonl_line: {e}"));
        assert_eq!(ev.event_id, 1);
        assert_eq!(ev.price_ticks, 7);
        // decode_jsonl splits on LF, so feed it the same line as a document.
        let evs = decode_jsonl(&format!("{line}\n")).unwrap();
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].qty, 8);
    }
    // LF is whitespace INSIDE a line too (the reference regex allows
    // `[ \t\r\n]*` between every token).
    let split = BODY.replacen(',', ",\n", 1);
    assert_eq!(decode_jsonl_line(&split).unwrap().instrument_id, 2);
}

#[test]
fn non_ascii_whitespace_is_content_and_is_rejected() {
    for ws in ["\u{0b}", "\u{0c}", "\u{a0}"] {
        let line = format!("{ws}{BODY}");
        assert!(
            decode_jsonl_line(&line).is_err(),
            "{ws:?} must not be treated as JSONL whitespace"
        );
        assert!(
            decode_jsonl(&format!("{line}\n")).is_err(),
            "{ws:?} must not be trimmed by decode_jsonl"
        );
    }
}

#[test]
fn blank_lines_are_still_skipped() {
    let text = format!("\n  \n\t\n{BODY}\n\r\n");
    let evs = decode_jsonl(&text).unwrap();
    assert_eq!(evs.len(), 1);
    assert_eq!(evs[0].event_id, 1);
}
