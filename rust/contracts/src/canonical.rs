//! Canonical JSON with byte parity to Python's
//! `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
//! allow_nan=False)` (`iap.contracts.versions.canonical_json`), plus the
//! `indent=2` layout used by research artefacts such as
//! `research/alpha_registry.json`.
//!
//! The tree is `serde_json::Value`; the *writer* is this module's own
//! (serde_json's serializer lays floats out differently — `1e16` instead
//! of `1e+16`, `1e-5` instead of `1e-05` — and does not escape non-ASCII).
//!
//! Pinned rules (`tests/golden/expected_canonical_json.json` `rules`):
//!
//! * object keys sorted by Unicode code point of the raw key, recursively;
//! * separators `,` and `:` (no whitespace);
//! * strings: `"` `\` and the C0 controls escaped (`\n \r \t \b \f`, else
//!   `\u00XX`), every code point outside `0x20..=0x7E` escaped as `\uXXXX`
//!   (lowercase hex; UTF-16 surrogate pairs above U+FFFF); `/` untouched;
//! * integers exact in the i64 / u64 domain (a number that was parsed or
//!   built as an integer prints as an integer);
//! * floats laid out like Python `float.__repr__`: the shortest digit
//!   string that round-trips, in exponent form iff the decimal exponent is
//!   `< -4` or `>= 16` (`1e-05`, `1e+16`: sign and at least two exponent
//!   digits), else positional with `.0` on integral values; `-0.0` stays;
//! * NaN / ±Inf are an error (`IapError::InvalidArgument`).

use marketdata::IapError;
use serde_json::Value;

use crate::sha256::sha256_hex;

/// Lay a finite `f64` out exactly like Python's `repr(float)`.
///
/// Algorithm: the shortest round-trip digit string comes from serde_json's
/// own `f64` serializer (ryu's `format_finite`, which rounds exact decimal
/// midpoints half-to-even like Python `repr`, `std::to_chars` and JDK-19+
/// `Double.toString`). `core::fmt`'s `{:e}` was used before 2026-09-20 and
/// rounds those ties half-up (`1059438285926254.25` → `…254.3` instead of
/// `…254.2`), which broke content hashes and trace digests on that class of
/// doubles. The digits and the decimal exponent are then re-laid out under
/// Python's rule (exponent form iff `exp < -4 || exp >= 16`, written with a
/// sign and at least two digits; positional otherwise with a mandatory
/// fraction part).
pub fn format_float(x: f64) -> Result<String, IapError> {
    if !x.is_finite() {
        return Err(IapError::InvalidArgument(format!(
            "canonical JSON: non-finite float {x}"
        )));
    }
    let (negative, digits, exp) = shortest_digits(x)?;
    Ok(layout_python_repr(negative, &digits, exp))
}

/// `(negative, significant digits without leading/trailing zeros, decimal
/// exponent of the first digit)` from serde_json's (ryu) shortest
/// round-trip text of `x`. Zero yields `("0", 0)`.
fn shortest_digits(x: f64) -> Result<(bool, String, i32), IapError> {
    let text = serde_json::to_string(&x).map_err(|e| {
        IapError::InvalidArgument(format!("canonical JSON: cannot serialise float {x}: {e}"))
    })?;
    let (mantissa, exp_part) = match text.split_once(['e', 'E']) {
        Some((m, e)) => (m, e),
        None => (text.as_str(), "0"),
    };
    let sci_exp: i32 = exp_part.parse().map_err(|_| {
        IapError::InvalidArgument(format!("canonical JSON: unexpected float layout {text}"))
    })?;
    let (negative, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, mantissa),
    };
    let (int_part, frac_part) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if !int_part.bytes().all(|b| b.is_ascii_digit()) || !frac_part.bytes().all(|b| b.is_ascii_digit()) {
        return Err(IapError::InvalidArgument(format!(
            "canonical JSON: unexpected float layout {text}"
        )));
    }
    let all: String = format!("{int_part}{frac_part}");
    let leading = all.bytes().take_while(|b| *b == b'0').count();
    if leading == all.len() {
        return Ok((negative, "0".to_string(), 0));
    }
    let trimmed = all[leading..].trim_end_matches('0');
    let exp = int_part.len() as i32 - 1 - leading as i32 + sci_exp;
    Ok((negative, trimmed.to_string(), exp))
}

/// Python `float.__repr__` layout of a digit string and its decimal exponent.
fn layout_python_repr(negative: bool, digits: &str, exp: i32) -> String {
    let mut out = String::with_capacity(32);
    if negative {
        out.push('-');
    }
    if !(-4..16).contains(&exp) {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        let abs = exp.unsigned_abs();
        if abs < 10 {
            out.push('0');
        }
        out.push_str(&abs.to_string());
    } else if exp < 0 {
        out.push_str("0.");
        for _ in 0..(-exp - 1) {
            out.push('0');
        }
        out.push_str(digits);
    } else {
        let int_len = exp as usize + 1;
        if digits.len() <= int_len {
            out.push_str(digits);
            for _ in digits.len()..int_len {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..int_len]);
            out.push('.');
            out.push_str(&digits[int_len..]);
        }
    }
    out
}

/// Append `s` as a JSON string literal (quotes included) under the
/// `ensure_ascii` escaping rules.
pub fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// Append one number in canonical layout (integers exact, floats per
/// [`format_float`]).
fn write_number(out: &mut String, n: &serde_json::Number) -> Result<(), IapError> {
    if let Some(u) = n.as_u64() {
        out.push_str(&u.to_string());
    } else if let Some(i) = n.as_i64() {
        out.push_str(&i.to_string());
    } else if let Some(f) = n.as_f64() {
        out.push_str(&format_float(f)?);
    } else {
        return Err(IapError::InvalidArgument(format!(
            "canonical JSON: unrepresentable number {n}"
        )));
    }
    Ok(())
}

/// Object keys in Unicode code-point order (`String`'s byte order on UTF-8
/// is the same order; sorted explicitly so the result never depends on the
/// map type behind `serde_json::Value`).
fn sorted_keys(map: &serde_json::Map<String, Value>) -> Vec<&String> {
    let mut keys: Vec<&String> = map.keys().collect();
    keys.sort_unstable_by(|a, b| a.chars().cmp(b.chars()));
    keys
}

/// Append the canonical (compact, sorted) form of `v`.
pub fn write_canonical(out: &mut String, v: &Value) -> Result<(), IapError> {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => write_number(out, n)?,
        Value::String(s) => write_string(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(out, item)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, key) in sorted_keys(map).into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, key);
                out.push(':');
                write_canonical(out, &map[key])?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// Canonical JSON text of `v` (one line, no trailing newline).
pub fn canonical_json(v: &Value) -> Result<String, IapError> {
    let mut out = String::new();
    write_canonical(&mut out, v)?;
    Ok(out)
}

fn write_indented(
    out: &mut String,
    v: &Value,
    indent: usize,
    depth: usize,
) -> Result<(), IapError> {
    match v {
        Value::Array(items) if !items.is_empty() => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                out.push_str(if i > 0 { ",\n" } else { "\n" });
                push_spaces(out, indent * (depth + 1));
                write_indented(out, item, indent, depth + 1)?;
            }
            out.push('\n');
            push_spaces(out, indent * depth);
            out.push(']');
        }
        Value::Object(map) if !map.is_empty() => {
            out.push('{');
            for (i, key) in sorted_keys(map).into_iter().enumerate() {
                out.push_str(if i > 0 { ",\n" } else { "\n" });
                push_spaces(out, indent * (depth + 1));
                write_string(out, key);
                out.push_str(": ");
                write_indented(out, &map[key], indent, depth + 1)?;
            }
            out.push('\n');
            push_spaces(out, indent * depth);
            out.push('}');
        }
        other => write_canonical(out, other)?,
    }
    Ok(())
}

fn push_spaces(out: &mut String, n: usize) {
    for _ in 0..n {
        out.push(' ');
    }
}

/// Python `json.dumps(v, sort_keys=True, indent=<indent>, ensure_ascii=True)`
/// layout (item separator `,` + newline, key separator `: `, empty
/// containers as `[]` / `{}`), without a trailing newline.
pub fn indented_json(v: &Value, indent: usize) -> Result<String, IapError> {
    let mut out = String::new();
    write_indented(&mut out, v, indent, 0)?;
    Ok(out)
}

/// Lowercase SHA-256 hex of the canonical JSON text of `v`
/// (`iap.contracts.versions.content_hash`).
pub fn content_hash(v: &Value) -> Result<String, IapError> {
    Ok(sha256_hex(canonical_json(v)?.as_bytes()))
}

/// A `serde_json::Value` float that rejects NaN / ±Inf instead of silently
/// becoming `null` (what `serde_json::to_value` does with them).
pub fn float_value(x: f64) -> Result<Value, IapError> {
    let n = serde_json::Number::from_f64(x).ok_or_else(|| {
        IapError::InvalidArgument(format!("canonical JSON: non-finite float {x}"))
    })?;
    Ok(Value::Number(n))
}

/// Pinned trace id: the first 32 hex chars of
/// `sha256("<session_id>|<instrument_id>|<event_ts>|<sequence>")`
/// (`iap.contracts.ids.make_trace_id`).
pub fn make_trace_id(session_id: &str, instrument_id: u32, event_ts: i64, sequence: u64) -> String {
    let preimage = format!("{session_id}|{instrument_id}|{event_ts}|{sequence}");
    let mut hex = sha256_hex(preimage.as_bytes());
    hex.truncate(32);
    hex
}

/// True when `s` is a lowercase 64-hex SHA-256 digest.
pub fn is_sha256_hex(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// True when `s` is a 32-hex trace id.
pub fn is_trace_id(s: &str) -> bool {
    s.len() == 32
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// True when `s` matches the generic id alphabet
/// `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` (strategy / alpha / session ids).
pub fn is_generic_id(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.is_empty() || bytes.len() > 128 || !bytes[0].is_ascii_alphanumeric() {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn float_layout_matches_python_repr() {
        let cases: [(f64, &str); 18] = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (0.1, "0.1"),
            (1e16, "1e+16"),
            (9999999999999998.0, "9999999999999998.0"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1.5e-10, "1.5e-10"),
            (5e-324, "5e-324"),
            (1.2345678901234568e17, "1.2345678901234568e+17"),
            (12345.6789, "12345.6789"),
            (4200.0, "4200.0"),
            (-4.2e-5, "-4.2e-05"),
            // exact decimal midpoints: half-to-even, like Python / C++ / Java
            (1059438285926254.25, "1059438285926254.2"),
            (26363981746409.3125, "26363981746409.312"),
            (1000000000000000.25, "1000000000000000.2"),
            (2251799813685248.5, "2251799813685248.5"),
        ];
        for (x, want) in cases {
            assert_eq!(format_float(x).expect("finite"), want);
        }
        assert!(format_float(f64::NAN).is_err());
        assert!(format_float(f64::INFINITY).is_err());
        assert!(format_float(f64::NEG_INFINITY).is_err());
    }

    #[test]
    fn documents_sort_and_escape() {
        let v = json!({"b": [1, 2.5, null, true], "a": {"z": "\u{e9}", "y": -0.0}});
        assert_eq!(
            canonical_json(&v).expect("finite"),
            "{\"a\":{\"y\":-0.0,\"z\":\"\\u00e9\"},\"b\":[1,2.5,null,true]}"
        );
        assert_eq!(
            content_hash(&v).expect("finite"),
            "3e56e3e089f9a8f3ad883f0230a81867c8a7782e45361756982f95a04390a595"
        );
        let mut s = String::new();
        write_string(&mut s, "emoji \u{1F600}/\u{7f}\u{0}");
        assert_eq!(s, "\"emoji \\ud83d\\ude00/\\u007f\\u0000\"");
    }

    #[test]
    fn integers_stay_integers() {
        let v = json!({"u": u64::MAX, "i": i64::MIN, "f": 100.0, "z": 0});
        assert_eq!(
            canonical_json(&v).expect("finite"),
            "{\"f\":100.0,\"i\":-9223372036854775808,\"u\":18446744073709551615,\"z\":0}"
        );
    }

    #[test]
    fn indented_layout_matches_python_indent_2() {
        let v = json!({"b": [1, {"x": []}], "a": {}, "c": "s"});
        let want = "{\n  \"a\": {},\n  \"b\": [\n    1,\n    {\n      \"x\": []\n    }\n  ],\n  \"c\": \"s\"\n}";
        assert_eq!(indented_json(&v, 2).expect("finite"), want);
    }

    #[test]
    fn trace_id_pin() {
        assert_eq!(
            make_trace_id("golden-session-2026-09-19", 1, 1787578700000000000, 500),
            "8b9fed6896d01463e64c4de915b0614b"
        );
        assert!(float_value(f64::NAN).is_err());
        assert!(is_generic_id("EQ-INTRADAY-1"));
        assert!(!is_generic_id("-bad"));
        assert!(!is_generic_id("a|b"));
    }
}
