//! Canonical codecs: JSONL and IAP1 binary (normative layout: schemas/FORMAT.md).
//!
//! Both encoders are byte-exact: the same event vector must produce
//! byte-identical files in every language (verified via SHA-256 golden tests).
//!
//! The JSONL decoder is deliberately strict per FORMAT.md §1: exact keys, in
//! exactly the canonical order, integer values only (no floats, exponents,
//! leading `+`, leading zeros, bools, or strings).

use std::fs;
use std::path::Path;

use crate::error::IapError;
use crate::events::{MarketEvent, FIELDS};

// ------------------------------------------------------------------- JSONL

/// Encode one event as a canonical JSONL line (no trailing newline).
pub fn encode_jsonl_line(ev: &MarketEvent) -> String {
    format!(
        "{{\"event_id\":{},\"instrument_id\":{},\"venue_id\":{},\"exchange_ts\":{},\
         \"receive_ts\":{},\"sequence\":{},\"event_type\":{},\"side\":{},\
         \"price_ticks\":{},\"qty\":{},\"order_id\":{},\"trade_id\":{}}}",
        ev.event_id,
        ev.instrument_id,
        ev.venue_id,
        ev.exchange_ts,
        ev.receive_ts,
        ev.sequence,
        ev.event_type,
        ev.side,
        ev.price_ticks,
        ev.qty,
        ev.order_id,
        ev.trade_id,
    )
}

/// Encode events to canonical JSONL bytes (LF after every line).
pub fn encode_jsonl(events: &[MarketEvent]) -> Vec<u8> {
    let mut out = Vec::with_capacity(events.len() * 200);
    for ev in events {
        out.extend_from_slice(encode_jsonl_line(ev).as_bytes());
        out.push(b'\n');
    }
    out
}

/// Strict scanner over one JSONL line.
struct Scanner<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Scanner<'a> {
    fn new(line: &'a str) -> Scanner<'a> {
        Scanner {
            bytes: line.as_bytes(),
            pos: 0,
        }
    }

    fn err(&self, msg: &str) -> IapError {
        IapError::Codec(format!("malformed JSONL line at byte {}: {}", self.pos, msg))
    }

    fn skip_ws(&mut self) {
        while let Some(&b) = self.bytes.get(self.pos) {
            if b == b' ' || b == b'\t' || b == b'\r' || b == b'\n' {
                self.pos += 1;
            } else {
                break;
            }
        }
    }

    fn expect(&mut self, ch: u8) -> Result<(), IapError> {
        if self.bytes.get(self.pos) == Some(&ch) {
            self.pos += 1;
            Ok(())
        } else {
            Err(self.err(&format!("expected {:?}", ch as char)))
        }
    }

    fn expect_key(&mut self, key: &str) -> Result<(), IapError> {
        self.expect(b'"')?;
        let start = self.pos;
        while let Some(&b) = self.bytes.get(self.pos) {
            if b == b'"' {
                break;
            }
            self.pos += 1;
        }
        let found = &self.bytes[start..self.pos];
        self.expect(b'"')?;
        if found != key.as_bytes() {
            return Err(IapError::Codec(format!(
                "JSONL keys mismatch: expected {:?}, found {:?} (order must be {:?})",
                key,
                String::from_utf8_lossy(found),
                FIELDS
            )));
        }
        Ok(())
    }

    /// Parse a strict JSON integer token; rejects floats/exponents/leading
    /// zeros/'+'; returns the raw token text.
    fn integer_token(&mut self) -> Result<&'a str, IapError> {
        let start = self.pos;
        if self.bytes.get(self.pos) == Some(&b'-') {
            self.pos += 1;
        }
        let digits_start = self.pos;
        while let Some(&b) = self.bytes.get(self.pos) {
            if b.is_ascii_digit() {
                self.pos += 1;
            } else {
                break;
            }
        }
        if self.pos == digits_start {
            return Err(self.err("expected an integer value"));
        }
        let digits = &self.bytes[digits_start..self.pos];
        if digits.len() > 1 && digits[0] == b'0' {
            return Err(self.err("integer with leading zeros"));
        }
        match self.bytes.get(self.pos) {
            Some(&b'.') | Some(&b'e') | Some(&b'E') => {
                return Err(self.err("field values must be integers, not floats"));
            }
            _ => {}
        }
        // Slicing on ASCII boundaries of a str-backed buffer is valid UTF-8.
        std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("invalid UTF-8 in integer"))
    }

    fn at_end(&mut self) -> bool {
        self.skip_ws();
        self.pos == self.bytes.len()
    }
}

fn parse_u64(tok: &str, key: &str) -> Result<u64, IapError> {
    tok.parse::<u64>().map_err(|_| {
        IapError::Codec(format!("JSONL field {key:?} out of range for its type: {tok}"))
    })
}

fn parse_i64(tok: &str, key: &str) -> Result<i64, IapError> {
    tok.parse::<i64>().map_err(|_| {
        IapError::Codec(format!("JSONL field {key:?} out of range for its type: {tok}"))
    })
}

fn parse_u32(tok: &str, key: &str) -> Result<u32, IapError> {
    tok.parse::<u32>().map_err(|_| {
        IapError::Codec(format!("JSONL field {key:?} out of range for its type: {tok}"))
    })
}

fn parse_u16(tok: &str, key: &str) -> Result<u16, IapError> {
    tok.parse::<u16>().map_err(|_| {
        IapError::Codec(format!("JSONL field {key:?} out of range for its type: {tok}"))
    })
}

fn parse_u8(tok: &str, key: &str) -> Result<u8, IapError> {
    tok.parse::<u8>().map_err(|_| {
        IapError::Codec(format!("JSONL field {key:?} out of range for its type: {tok}"))
    })
}

/// Decode one canonical JSONL line. Strict: exact keys in exact order,
/// integer values only.
pub fn decode_jsonl_line(line: &str) -> Result<MarketEvent, IapError> {
    let mut s = Scanner::new(line);
    s.skip_ws();
    s.expect(b'{')?;
    let mut toks: [&str; 12] = [""; 12];
    for (i, key) in FIELDS.iter().enumerate() {
        s.skip_ws();
        s.expect_key(key)?;
        s.skip_ws();
        s.expect(b':')?;
        s.skip_ws();
        toks[i] = s.integer_token()?;
        s.skip_ws();
        if i + 1 < FIELDS.len() {
            s.expect(b',')?;
        }
    }
    s.expect(b'}')?;
    if !s.at_end() {
        return Err(s.err("trailing content after object"));
    }
    Ok(MarketEvent {
        event_id: parse_u64(toks[0], FIELDS[0])?,
        instrument_id: parse_u32(toks[1], FIELDS[1])?,
        venue_id: parse_u16(toks[2], FIELDS[2])?,
        exchange_ts: parse_i64(toks[3], FIELDS[3])?,
        receive_ts: parse_i64(toks[4], FIELDS[4])?,
        sequence: parse_u64(toks[5], FIELDS[5])?,
        event_type: parse_u8(toks[6], FIELDS[6])?,
        side: parse_u8(toks[7], FIELDS[7])?,
        price_ticks: parse_i64(toks[8], FIELDS[8])?,
        qty: parse_i64(toks[9], FIELDS[9])?,
        order_id: parse_u64(toks[10], FIELDS[10])?,
        trade_id: parse_u64(toks[11], FIELDS[11])?,
    })
}

/// Decode canonical JSONL text (skipping blank lines, like the reference).
pub fn decode_jsonl(text: &str) -> Result<Vec<MarketEvent>, IapError> {
    let mut events = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if !line.is_empty() {
            events.push(decode_jsonl_line(line)?);
        }
    }
    Ok(events)
}

/// Read all events from a canonical JSONL file.
pub fn read_jsonl<P: AsRef<Path>>(path: P) -> Result<Vec<MarketEvent>, IapError> {
    let text = fs::read_to_string(path.as_ref()).map_err(|e| {
        IapError::Io(format!("reading {}: {}", path.as_ref().display(), e))
    })?;
    decode_jsonl(&text)
}

/// Write a canonical JSONL file; return the number of events written.
pub fn write_jsonl<P: AsRef<Path>>(path: P, events: &[MarketEvent]) -> Result<usize, IapError> {
    fs::write(path.as_ref(), encode_jsonl(events)).map_err(|e| {
        IapError::Io(format!("writing {}: {}", path.as_ref().display(), e))
    })?;
    Ok(events.len())
}

// -------------------------------------------------------------------- IAP1

pub const IAP1_MAGIC: u32 = 0x4941_5031;
pub const IAP1_VERSION: u32 = 1;
pub const IAP1_HEADER_SIZE: usize = 16;
pub const IAP1_RECORD_SIZE: usize = 72;

/// Encode events to IAP1 bytes (16-byte header + fixed 72-byte LE records).
pub fn encode_iap1(events: &[MarketEvent]) -> Vec<u8> {
    let mut out = Vec::with_capacity(IAP1_HEADER_SIZE + IAP1_RECORD_SIZE * events.len());
    out.extend_from_slice(&IAP1_MAGIC.to_le_bytes());
    out.extend_from_slice(&IAP1_VERSION.to_le_bytes());
    out.extend_from_slice(&(events.len() as u64).to_le_bytes());
    for ev in events {
        out.extend_from_slice(&ev.event_id.to_le_bytes());
        out.extend_from_slice(&ev.instrument_id.to_le_bytes());
        out.extend_from_slice(&ev.venue_id.to_le_bytes());
        out.push(ev.event_type);
        out.push(ev.side);
        out.extend_from_slice(&ev.exchange_ts.to_le_bytes());
        out.extend_from_slice(&ev.receive_ts.to_le_bytes());
        out.extend_from_slice(&ev.sequence.to_le_bytes());
        out.extend_from_slice(&ev.price_ticks.to_le_bytes());
        out.extend_from_slice(&ev.qty.to_le_bytes());
        out.extend_from_slice(&ev.order_id.to_le_bytes());
        out.extend_from_slice(&ev.trade_id.to_le_bytes());
    }
    out
}

fn le_u16(b: &[u8]) -> u16 {
    u16::from_le_bytes([b[0], b[1]])
}

fn le_u32(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

fn le_u64(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

fn le_i64(b: &[u8]) -> i64 {
    le_u64(b) as i64
}

/// Decode IAP1 bytes. Rejects bad magic/version, truncation, count mismatch.
pub fn decode_iap1(data: &[u8]) -> Result<Vec<MarketEvent>, IapError> {
    if data.len() < IAP1_HEADER_SIZE {
        return Err(IapError::Codec(format!(
            "IAP1 file truncated: {} bytes < 16-byte header",
            data.len()
        )));
    }
    let magic = le_u32(&data[0..4]);
    if magic != IAP1_MAGIC {
        return Err(IapError::Codec(format!(
            "bad IAP1 magic: 0x{magic:08X} (expected 0x{IAP1_MAGIC:08X})"
        )));
    }
    let version = le_u32(&data[4..8]);
    if version != IAP1_VERSION {
        return Err(IapError::Codec(format!("unsupported IAP1 version: {version}")));
    }
    let count = le_u64(&data[8..16]);
    let expected = (count as u128) * (IAP1_RECORD_SIZE as u128) + IAP1_HEADER_SIZE as u128;
    if data.len() as u128 != expected {
        return Err(IapError::Codec(format!(
            "IAP1 size mismatch: {} bytes, header count={} implies {}",
            data.len(),
            count,
            expected
        )));
    }
    let count = count as usize;
    let mut events = Vec::with_capacity(count);
    let mut off = IAP1_HEADER_SIZE;
    for _ in 0..count {
        let r = &data[off..off + IAP1_RECORD_SIZE];
        events.push(MarketEvent {
            event_id: le_u64(&r[0..8]),
            instrument_id: le_u32(&r[8..12]),
            venue_id: le_u16(&r[12..14]),
            event_type: r[14],
            side: r[15],
            exchange_ts: le_i64(&r[16..24]),
            receive_ts: le_i64(&r[24..32]),
            sequence: le_u64(&r[32..40]),
            price_ticks: le_i64(&r[40..48]),
            qty: le_i64(&r[48..56]),
            order_id: le_u64(&r[56..64]),
            trade_id: le_u64(&r[64..72]),
        });
        off += IAP1_RECORD_SIZE;
    }
    Ok(events)
}

/// Write an IAP1 file; return the number of events written.
pub fn write_iap1<P: AsRef<Path>>(path: P, events: &[MarketEvent]) -> Result<usize, IapError> {
    fs::write(path.as_ref(), encode_iap1(events)).map_err(|e| {
        IapError::Io(format!("writing {}: {}", path.as_ref().display(), e))
    })?;
    Ok(events.len())
}

/// Read an IAP1 file.
pub fn read_iap1<P: AsRef<Path>>(path: P) -> Result<Vec<MarketEvent>, IapError> {
    let data = fs::read(path.as_ref()).map_err(|e| {
        IapError::Io(format!("reading {}: {}", path.as_ref().display(), e))
    })?;
    decode_iap1(&data)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> MarketEvent {
        MarketEvent {
            event_id: 1,
            instrument_id: 2,
            venue_id: 3,
            exchange_ts: -4,
            receive_ts: 5,
            sequence: 6,
            event_type: 1,
            side: 0,
            price_ticks: 2450,
            qty: 100,
            order_id: u64::MAX,
            trade_id: 0,
        }
    }

    #[test]
    fn jsonl_roundtrip() {
        let ev = sample();
        let line = encode_jsonl_line(&ev);
        assert_eq!(decode_jsonl_line(&line).expect("roundtrip"), ev);
    }

    #[test]
    fn jsonl_rejects_misordered_keys() {
        let line = encode_jsonl_line(&sample()).replace(
            "\"event_id\":1,\"instrument_id\":2",
            "\"instrument_id\":2,\"event_id\":1",
        );
        assert!(decode_jsonl_line(&line).is_err());
    }

    #[test]
    fn jsonl_rejects_missing_and_extra_keys() {
        let full = encode_jsonl_line(&sample());
        let missing = full.replace(",\"trade_id\":0", "");
        assert!(decode_jsonl_line(&missing).is_err());
        let extra = full.replace("}", ",\"extra\":1}");
        assert!(decode_jsonl_line(&extra).is_err());
    }

    #[test]
    fn jsonl_rejects_non_integer_values() {
        let full = encode_jsonl_line(&sample());
        for bad in ["1.5", "1e3", "true", "\"1\"", "01", "+1"] {
            let line = full.replace("\"qty\":100", &format!("\"qty\":{bad}"));
            assert!(decode_jsonl_line(&line).is_err(), "accepted qty={bad}");
        }
    }

    #[test]
    fn jsonl_rejects_out_of_range_and_garbage() {
        let full = encode_jsonl_line(&sample());
        // venue_id is u16 on the wire.
        let line = full.replace("\"venue_id\":3", "\"venue_id\":70000");
        assert!(decode_jsonl_line(&line).is_err());
        let line = full.replace("\"event_id\":1", "\"event_id\":-1");
        assert!(decode_jsonl_line(&line).is_err());
        assert!(decode_jsonl_line("not json").is_err());
        assert!(decode_jsonl_line(&format!("{full} garbage")).is_err());
    }

    #[test]
    fn iap1_roundtrip_and_layout() {
        let events = vec![sample(), MarketEvent::default()];
        let bytes = encode_iap1(&events);
        assert_eq!(bytes.len(), IAP1_HEADER_SIZE + 2 * IAP1_RECORD_SIZE);
        assert_eq!(&bytes[0..4], &[0x31, 0x50, 0x41, 0x49]); // "1PAI" LE
        assert_eq!(decode_iap1(&bytes).expect("roundtrip"), events);
    }

    #[test]
    fn iap1_rejects_bad_magic_version_truncation_count() {
        let good = encode_iap1(&[sample()]);

        let mut bad_magic = good.clone();
        bad_magic[0] ^= 0xFF;
        assert!(decode_iap1(&bad_magic).is_err());

        let mut bad_version = good.clone();
        bad_version[4] = 9;
        assert!(decode_iap1(&bad_version).is_err());

        assert!(decode_iap1(&good[..10]).is_err()); // truncated header
        assert!(decode_iap1(&good[..good.len() - 1]).is_err()); // truncated record

        let mut bad_count = good.clone();
        bad_count[8] = 2; // header claims 2 records, file has 1
        assert!(decode_iap1(&bad_count).is_err());
    }
}
