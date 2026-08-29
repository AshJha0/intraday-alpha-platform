//! IAPV1 wire codec — length-prefixed fixed binary layout (normative for
//! this crate; little-endian throughout, no padding).
//!
//! ```text
//! frame    := len u32 | payload[len]        (len = payload byte count)
//! payload  := msg_type u8 | body
//! msg_type := 1 (OrderRequest) | 2 (ExecutionReport)
//!
//! OrderRequest body (variable, 50 + strategy_len bytes):
//!   order_id u64 | instrument_id u32 | venue_id u16 | side u8 |
//!   order_type u8 | qty i64 | price_ticks i64 | urgency f64 (IEEE bits) |
//!   timestamp i64 | strategy_len u16 | strategy_id UTF-8 bytes
//!
//! ExecutionReport body (fixed, 59 bytes):
//!   order_id u64 | execution_id u64 | venue_id u16 | status u8 |
//!   filled_qty i64 | fill_price_ticks i64 | exchange_ts i64 |
//!   receive_ts i64 | fees f64 (IEEE bits)
//! ```
//!
//! Decoders are strict: truncation, a length prefix that disagrees with the
//! body, unknown message types, enum-domain violations (side, order_type,
//! status), non-UTF-8 strategy ids and trailing bytes are all rejected with
//! `IapError::Codec` — malformed input never produces a message.

use marketdata::IapError;

use crate::messages::{ExecStatus, ExecutionReport, OrderRequest, OrderType};

/// Frame length prefix size (u32).
pub const LEN_PREFIX: usize = 4;
/// Message type byte for [`OrderRequest`].
pub const MSG_ORDER: u8 = 1;
/// Message type byte for [`ExecutionReport`].
pub const MSG_REPORT: u8 = 2;
/// OrderRequest body size before the strategy bytes (excl. msg_type).
pub const ORDER_FIXED_BODY: usize = 50;
/// ExecutionReport body size (excl. msg_type).
pub const REPORT_BODY: usize = 59;
/// Maximum strategy id length on the wire.
pub const MAX_STRATEGY_LEN: usize = 256;

/// A decoded wire message.
#[derive(Debug, Clone, PartialEq)]
pub enum Message {
    /// An order request frame.
    Order(OrderRequest),
    /// An execution report frame.
    Report(ExecutionReport),
}

fn put_u16(out: &mut Vec<u8>, v: u16) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn put_u32(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn put_u64(out: &mut Vec<u8>, v: u64) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn put_i64(out: &mut Vec<u8>, v: i64) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn put_f64(out: &mut Vec<u8>, v: f64) {
    out.extend_from_slice(&v.to_bits().to_le_bytes());
}

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Reader<'a> {
        Reader { buf, pos: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], IapError> {
        if self.pos + n > self.buf.len() {
            return Err(IapError::Codec(format!(
                "truncated message: need {n} bytes at offset {}, have {}",
                self.pos,
                self.buf.len() - self.pos
            )));
        }
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    fn u8(&mut self) -> Result<u8, IapError> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> Result<u16, IapError> {
        Ok(u16::from_le_bytes(self.take(2)?.try_into().unwrap()))
    }

    fn u32(&mut self) -> Result<u32, IapError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }

    fn u64(&mut self) -> Result<u64, IapError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn i64(&mut self) -> Result<i64, IapError> {
        Ok(i64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn f64(&mut self) -> Result<f64, IapError> {
        Ok(f64::from_bits(self.u64()?))
    }

    fn done(&self) -> Result<(), IapError> {
        if self.pos != self.buf.len() {
            return Err(IapError::Codec(format!(
                "{} trailing bytes after message body",
                self.buf.len() - self.pos
            )));
        }
        Ok(())
    }
}

/// Encode one order request as a length-prefixed frame.
pub fn encode_order(order: &OrderRequest) -> Result<Vec<u8>, IapError> {
    if order.strategy_id.len() > MAX_STRATEGY_LEN {
        return Err(IapError::Codec(format!(
            "strategy_id too long: {} > {MAX_STRATEGY_LEN}",
            order.strategy_id.len()
        )));
    }
    let body_len = 1 + ORDER_FIXED_BODY + order.strategy_id.len();
    let mut out = Vec::with_capacity(LEN_PREFIX + body_len);
    put_u32(&mut out, body_len as u32);
    out.push(MSG_ORDER);
    put_u64(&mut out, order.order_id);
    put_u32(&mut out, order.instrument_id);
    put_u16(&mut out, order.venue_id);
    out.push(order.side);
    out.push(order.order_type);
    put_i64(&mut out, order.qty);
    put_i64(&mut out, order.price_ticks);
    put_f64(&mut out, order.urgency);
    put_i64(&mut out, order.timestamp);
    put_u16(&mut out, order.strategy_id.len() as u16);
    out.extend_from_slice(order.strategy_id.as_bytes());
    Ok(out)
}

/// Encode one execution report as a length-prefixed frame.
pub fn encode_report(report: &ExecutionReport) -> Vec<u8> {
    let body_len = 1 + REPORT_BODY;
    let mut out = Vec::with_capacity(LEN_PREFIX + body_len);
    put_u32(&mut out, body_len as u32);
    out.push(MSG_REPORT);
    put_u64(&mut out, report.order_id);
    put_u64(&mut out, report.execution_id);
    put_u16(&mut out, report.venue_id);
    out.push(report.status);
    put_i64(&mut out, report.filled_qty);
    put_i64(&mut out, report.fill_price_ticks);
    put_i64(&mut out, report.exchange_ts);
    put_i64(&mut out, report.receive_ts);
    put_f64(&mut out, report.fees);
    out
}

fn decode_order_body(body: &[u8]) -> Result<OrderRequest, IapError> {
    let mut r = Reader::new(body);
    let order_id = r.u64()?;
    let instrument_id = r.u32()?;
    let venue_id = r.u16()?;
    let side = r.u8()?;
    let order_type = r.u8()?;
    let qty = r.i64()?;
    let price_ticks = r.i64()?;
    let urgency = r.f64()?;
    let timestamp = r.i64()?;
    let slen = r.u16()? as usize;
    if slen > MAX_STRATEGY_LEN {
        return Err(IapError::Codec(format!(
            "strategy_len {slen} exceeds cap {MAX_STRATEGY_LEN}"
        )));
    }
    let sbytes = r.take(slen)?;
    let strategy_id = std::str::from_utf8(sbytes)
        .map_err(|_| IapError::Codec("strategy_id is not valid UTF-8".to_string()))?
        .to_string();
    r.done()?;
    if side > 1 {
        return Err(IapError::Codec(format!("side out of domain: {side}")));
    }
    if OrderType::from_u8(order_type).is_none() {
        return Err(IapError::Codec(format!(
            "order_type out of domain: {order_type}"
        )));
    }
    Ok(OrderRequest {
        order_id,
        instrument_id,
        side,
        qty,
        price_ticks,
        order_type,
        venue_id,
        strategy_id,
        urgency,
        timestamp,
    })
}

fn decode_report_body(body: &[u8]) -> Result<ExecutionReport, IapError> {
    let mut r = Reader::new(body);
    let order_id = r.u64()?;
    let execution_id = r.u64()?;
    let venue_id = r.u16()?;
    let status = r.u8()?;
    let filled_qty = r.i64()?;
    let fill_price_ticks = r.i64()?;
    let exchange_ts = r.i64()?;
    let receive_ts = r.i64()?;
    let fees = r.f64()?;
    r.done()?;
    if ExecStatus::from_u8(status).is_none() {
        return Err(IapError::Codec(format!("status out of domain: {status}")));
    }
    Ok(ExecutionReport {
        order_id,
        execution_id,
        status,
        filled_qty,
        fill_price_ticks,
        venue_id,
        exchange_ts,
        receive_ts,
        fees,
    })
}

/// Decode one frame from the start of `buf`; returns the message and the
/// total bytes consumed (prefix + payload).
pub fn decode_frame(buf: &[u8]) -> Result<(Message, usize), IapError> {
    if buf.len() < LEN_PREFIX {
        return Err(IapError::Codec(format!(
            "truncated frame: {} bytes, need at least {LEN_PREFIX}",
            buf.len()
        )));
    }
    let len = u32::from_le_bytes(buf[..LEN_PREFIX].try_into().unwrap()) as usize;
    if len == 0 {
        return Err(IapError::Codec("empty frame payload".to_string()));
    }
    if buf.len() < LEN_PREFIX + len {
        return Err(IapError::Codec(format!(
            "truncated frame: payload needs {len} bytes, have {}",
            buf.len() - LEN_PREFIX
        )));
    }
    let payload = &buf[LEN_PREFIX..LEN_PREFIX + len];
    let msg = match payload[0] {
        MSG_ORDER => Message::Order(decode_order_body(&payload[1..])?),
        MSG_REPORT => Message::Report(decode_report_body(&payload[1..])?),
        other => {
            return Err(IapError::Codec(format!("unknown message type {other}")));
        }
    };
    Ok((msg, LEN_PREFIX + len))
}

/// Decode a byte stream of concatenated frames.
pub fn decode_stream(mut buf: &[u8]) -> Result<Vec<Message>, IapError> {
    let mut out = Vec::new();
    while !buf.is_empty() {
        let (msg, used) = decode_frame(buf)?;
        out.push(msg);
        buf = &buf[used..];
    }
    Ok(out)
}
