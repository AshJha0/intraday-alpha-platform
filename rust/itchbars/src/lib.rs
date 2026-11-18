//! Nasdaq TotalView-ITCH 5.0 -> one-minute bars for every symbol of the day.
//!
//! The plain idea: one streaming pass over the day's messages keeps each
//! symbol's displayed book and, for each minute of the regular session,
//! records the quote at the minute's end, the time-weighted quoted spread and
//! the traded volume.  A cross-sectional study at 5 to 30 minute horizons
//! needs nothing more, and the pass takes minutes instead of the hours the
//! full feature pipeline takes.
//!
//! Input: the vendor file's framing, a big-endian `u16` length followed by
//! the message, repeated.  Timestamps are nanoseconds since midnight in the
//! exchange's local time.  Prices are integers in units of 1e-4 dollars.
//!
//! Bar `m` covers `[open + m min, open + (m + 1) min)` with `open` 09:30:00
//! and 390 bars to 16:00:00.  Definitions:
//!
//! * `bid`, `ask`, `bid_sz`, `ask_sz`: the displayed best quote and its size
//!   at the end of the bar (0 when that side is empty);
//! * `tw_spread`: time-weighted `ask - bid` over the part of the bar in which
//!   both sides were present and `ask > bid` (`tw_valid_ns` is that part);
//! * `vol`, `notional`, `trades`: executions against displayed orders (`E`,
//!   and `C` when printable), `notional` in 1e-4 dollars times shares;
//! * `hidden_vol`, `hidden_notional`: non-displayed prints (`P`);
//! * crosses (`Q`) are not counted: the bars describe continuous trading.
//!
//! What it does not do: halts and LULD states are not applied (a halted
//! symbol shows its last quote), and the session clock is fixed rather than
//! read from system events.  Both are stated in the output header.

use std::collections::{BTreeMap, HashMap};
use std::io::{self, Read, Write};

pub const OPEN_NS: u64 = (9 * 3600 + 30 * 60) * 1_000_000_000;
pub const MINUTE_NS: u64 = 60 * 1_000_000_000;
pub const BARS: usize = 390;
pub const CLOSE_NS: u64 = OPEN_NS + BARS as u64 * MINUTE_NS;

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub struct Bar {
    pub bid: u32,
    pub ask: u32,
    pub bid_sz: u64,
    pub ask_sz: u64,
    /// sum over the bar of (ask - bid) * ns, valid time only
    pub tw_spread_ns: u128,
    pub tw_valid_ns: u64,
    pub vol: u64,
    pub notional: u128,
    pub trades: u32,
    pub hidden_vol: u64,
    pub hidden_notional: u128,
}

#[derive(Clone, Copy)]
struct Order {
    locate: u16,
    buy: bool,
    price: u32,
    shares: u32,
}

#[derive(Default)]
struct Book {
    bids: BTreeMap<u32, u64>,
    asks: BTreeMap<u32, u64>,
    /// time up to which the current quote has been accrued into the bars
    last_ts: u64,
    bars: Option<Box<[Bar]>>,
    messages: u64,
}

impl Book {
    fn best(&self) -> (u32, u64, u32, u64) {
        let (bp, bq) = self
            .bids
            .iter()
            .next_back()
            .map_or((0, 0), |(p, q)| (*p, *q));
        let (ap, aq) = self.asks.iter().next().map_or((0, 0), |(p, q)| (*p, *q));
        (bp, bq, ap, aq)
    }

    fn bars_mut(&mut self) -> &mut [Bar] {
        self.bars
            .get_or_insert_with(|| vec![Bar::default(); BARS].into_boxed_slice())
    }

    /// Accrue the current quote from `last_ts` to `t` (both clipped to the
    /// session), stamping the end-of-bar quote at every bar boundary passed.
    fn accrue(&mut self, t: u64) {
        let from = self.last_ts.max(OPEN_NS);
        let to = t.min(CLOSE_NS);
        if t > self.last_ts {
            self.last_ts = t;
        }
        if to <= from {
            return;
        }
        let (bp, bq, ap, aq) = self.best();
        let valid = bp > 0 && ap > bp;
        let bars = self.bars_mut();
        let mut cur = from;
        while cur < to {
            let m = ((cur - OPEN_NS) / MINUTE_NS) as usize;
            let bar_end = OPEN_NS + (m as u64 + 1) * MINUTE_NS;
            let seg_end = to.min(bar_end);
            let b = &mut bars[m];
            if valid {
                b.tw_spread_ns += u128::from(ap - bp) * u128::from(seg_end - cur);
                b.tw_valid_ns += seg_end - cur;
            }
            if seg_end == bar_end {
                b.bid = bp;
                b.ask = ap;
                b.bid_sz = bq;
                b.ask_sz = aq;
            }
            cur = seg_end;
        }
    }

    fn add(&mut self, buy: bool, price: u32, shares: u32) {
        let side = if buy { &mut self.bids } else { &mut self.asks };
        *side.entry(price).or_insert(0) += u64::from(shares);
    }

    fn remove(&mut self, buy: bool, price: u32, shares: u32) {
        let side = if buy { &mut self.bids } else { &mut self.asks };
        if let Some(q) = side.get_mut(&price) {
            *q = q.saturating_sub(u64::from(shares));
            if *q == 0 {
                side.remove(&price);
            }
        }
    }
}

#[derive(Default, Debug, Clone, PartialEq, Eq)]
pub struct Stats {
    pub messages: u64,
    pub unknown_order_refs: u64,
    pub short_messages: u64,
}

pub struct Builder {
    books: Vec<Book>,
    symbols: Vec<Option<[u8; 8]>>,
    etp: Vec<u8>,
    orders: HashMap<u64, Order>,
    pub stats: Stats,
}

impl Default for Builder {
    fn default() -> Self {
        Self::new()
    }
}

fn be16(b: &[u8], o: usize) -> u16 {
    u16::from_be_bytes([b[o], b[o + 1]])
}
fn be32(b: &[u8], o: usize) -> u32 {
    u32::from_be_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}
fn be48(b: &[u8], o: usize) -> u64 {
    let mut x = [0u8; 8];
    x[2..].copy_from_slice(&b[o..o + 6]);
    u64::from_be_bytes(x)
}
fn be64(b: &[u8], o: usize) -> u64 {
    let mut x = [0u8; 8];
    x.copy_from_slice(&b[o..o + 8]);
    u64::from_be_bytes(x)
}

/// Minimum length of each handled message type (type byte included).
fn min_len(kind: u8) -> usize {
    match kind {
        b'R' => 39,
        b'A' => 36,
        b'F' => 40,
        b'E' => 31,
        b'C' => 36,
        b'X' => 23,
        b'D' => 19,
        b'U' => 35,
        b'P' => 44,
        _ => 11,
    }
}

impl Builder {
    pub fn new() -> Self {
        let mut books = Vec::with_capacity(1 << 16);
        books.resize_with(1 << 16, Book::default);
        Builder {
            books,
            symbols: vec![None; 1 << 16],
            etp: vec![b' '; 1 << 16],
            orders: HashMap::with_capacity(1 << 22),
            stats: Stats::default(),
        }
    }

    fn trade(&mut self, locate: u16, ts: u64, price: u32, shares: u32, hidden: bool) {
        if !(OPEN_NS..CLOSE_NS).contains(&ts) {
            return;
        }
        let m = ((ts - OPEN_NS) / MINUTE_NS) as usize;
        let b = &mut self.books[locate as usize].bars_mut()[m];
        let n = u128::from(price) * u128::from(shares);
        if hidden {
            b.hidden_vol += u64::from(shares);
            b.hidden_notional += n;
        } else {
            b.vol += u64::from(shares);
            b.notional += n;
            b.trades += 1;
        }
    }

    /// Apply one message (without its length prefix).
    pub fn apply(&mut self, msg: &[u8]) {
        if msg.is_empty() {
            return;
        }
        let kind = msg[0];
        if msg.len() < min_len(kind) {
            self.stats.short_messages += 1;
            return;
        }
        self.stats.messages += 1;
        let locate = be16(msg, 1);
        let ts = be48(msg, 5);
        match kind {
            b'R' => {
                let mut s = [0u8; 8];
                s.copy_from_slice(&msg[11..19]);
                self.symbols[locate as usize] = Some(s);
                self.etp[locate as usize] = msg[33];
            }
            b'A' | b'F' => {
                let oref = be64(msg, 11);
                let buy = msg[19] == b'B';
                let shares = be32(msg, 20);
                let price = be32(msg, 32);
                let book = &mut self.books[locate as usize];
                book.messages += 1;
                book.accrue(ts);
                book.add(buy, price, shares);
                self.orders.insert(
                    oref,
                    Order {
                        locate,
                        buy,
                        price,
                        shares,
                    },
                );
            }
            b'E' | b'C' | b'X' | b'D' => {
                let oref = be64(msg, 11);
                let Some(o) = self.orders.get(&oref).copied() else {
                    self.stats.unknown_order_refs += 1;
                    return;
                };
                let qty = match kind {
                    b'D' => o.shares,
                    _ => be32(msg, 19).min(o.shares),
                };
                let book = &mut self.books[o.locate as usize];
                book.messages += 1;
                book.accrue(ts);
                book.remove(o.buy, o.price, qty);
                if qty >= o.shares {
                    self.orders.remove(&oref);
                } else if let Some(live) = self.orders.get_mut(&oref) {
                    live.shares -= qty;
                }
                match kind {
                    b'E' => self.trade(o.locate, ts, o.price, qty, false),
                    b'C' if msg[31] == b'Y' => self.trade(o.locate, ts, be32(msg, 32), qty, false),
                    _ => {}
                }
            }
            b'U' => {
                let old = be64(msg, 11);
                let new = be64(msg, 19);
                let shares = be32(msg, 27);
                let price = be32(msg, 31);
                let Some(o) = self.orders.remove(&old) else {
                    self.stats.unknown_order_refs += 1;
                    return;
                };
                let book = &mut self.books[o.locate as usize];
                book.messages += 1;
                book.accrue(ts);
                book.remove(o.buy, o.price, o.shares);
                book.add(o.buy, price, shares);
                self.orders.insert(
                    new,
                    Order {
                        locate: o.locate,
                        buy: o.buy,
                        price,
                        shares,
                    },
                );
            }
            b'P' => {
                let shares = be32(msg, 20);
                let price = be32(msg, 32);
                self.trade(locate, ts, price, shares, true);
            }
            _ => {}
        }
    }

    /// Read the length-prefixed stream to its end.
    pub fn read_stream<R: Read>(&mut self, mut r: R) -> io::Result<()> {
        let mut len = [0u8; 2];
        let mut buf = vec![0u8; 1 << 16];
        loop {
            match r.read_exact(&mut len) {
                Ok(()) => {}
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
                Err(e) => return Err(e),
            }
            let n = u16::from_be_bytes(len) as usize;
            match r.read_exact(&mut buf[..n]) {
                Ok(()) => {}
                // a file cut inside a message: stop at the last whole one
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
                Err(e) => return Err(e),
            }
            self.apply(&buf[..n]);
        }
    }

    /// Close every book at the session end and return `(symbol, etp flag,
    /// book messages, bars)` for each symbol that had a directory row and at
    /// least one bar touched, ordered by symbol.
    pub fn finish(mut self) -> Vec<(String, char, u64, Box<[Bar]>)> {
        let mut out = Vec::new();
        for (i, book) in self.books.iter_mut().enumerate() {
            let Some(sym) = self.symbols[i] else { continue };
            if book.bars.is_none() && book.messages == 0 {
                continue;
            }
            book.accrue(CLOSE_NS);
            let name = String::from_utf8_lossy(&sym).trim_end().to_string();
            if let Some(bars) = book.bars.take() {
                out.push((name, self.etp[i] as char, book.messages, bars));
            }
        }
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

pub const HEADER: &str = "symbol,etp,minute,bid,ask,bid_sz,ask_sz,tw_spread,tw_valid_ns,vol,notional,trades,hidden_vol,hidden_notional";

/// Write the bars as CSV.  Prices and `tw_spread` are in 1e-4 dollars;
/// `tw_spread` is printed with four decimals; an all-empty bar is skipped.
pub fn write_csv<W: Write>(mut w: W, rows: &[(String, char, u64, Box<[Bar]>)]) -> io::Result<()> {
    writeln!(
        w,
        "# itchbars/1: session 09:30:00-16:00:00 fixed clock; halts not applied; crosses excluded"
    )?;
    writeln!(w, "{HEADER}")?;
    for (sym, etp, _, bars) in rows {
        for (m, b) in bars.iter().enumerate() {
            if *b == Bar::default() {
                continue;
            }
            let tw = if b.tw_valid_ns > 0 {
                // exact integer division to 1e-4 of a price unit, half up
                let scaled = b.tw_spread_ns * 10_000 + u128::from(b.tw_valid_ns) / 2;
                scaled / u128::from(b.tw_valid_ns)
            } else {
                0
            };
            writeln!(
                w,
                "{sym},{etp},{m},{},{},{},{},{}.{:04},{},{},{},{},{},{}",
                b.bid,
                b.ask,
                b.bid_sz,
                b.ask_sz,
                tw / 10_000,
                tw % 10_000,
                b.tw_valid_ns,
                b.vol,
                b.notional,
                b.trades,
                b.hidden_vol,
                b.hidden_notional
            )?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn head(kind: u8, locate: u16, ts: u64) -> Vec<u8> {
        let mut v = vec![kind];
        v.extend_from_slice(&locate.to_be_bytes());
        v.extend_from_slice(&[0, 0]);
        v.extend_from_slice(&ts.to_be_bytes()[2..]);
        v
    }
    fn directory(locate: u16, sym: &str, etp: u8) -> Vec<u8> {
        let mut v = head(b'R', locate, 0);
        let mut s = [b' '; 8];
        s[..sym.len()].copy_from_slice(sym.as_bytes());
        v.extend_from_slice(&s);
        v.resize(39, b' ');
        v[33] = etp;
        v
    }
    fn add(locate: u16, ts: u64, oref: u64, buy: bool, shares: u32, price: u32) -> Vec<u8> {
        let mut v = head(b'A', locate, ts);
        v.extend_from_slice(&oref.to_be_bytes());
        v.push(if buy { b'B' } else { b'S' });
        v.extend_from_slice(&shares.to_be_bytes());
        v.extend_from_slice(b"TEST    ");
        v.extend_from_slice(&price.to_be_bytes());
        v
    }
    fn exec(ts: u64, oref: u64, shares: u32) -> Vec<u8> {
        let mut v = head(b'E', 1, ts);
        v.extend_from_slice(&oref.to_be_bytes());
        v.extend_from_slice(&shares.to_be_bytes());
        v.extend_from_slice(&7u64.to_be_bytes());
        v
    }
    fn exec_px(ts: u64, oref: u64, shares: u32, printable: u8, price: u32) -> Vec<u8> {
        let mut v = head(b'C', 1, ts);
        v.extend_from_slice(&oref.to_be_bytes());
        v.extend_from_slice(&shares.to_be_bytes());
        v.extend_from_slice(&7u64.to_be_bytes());
        v.push(printable);
        v.extend_from_slice(&price.to_be_bytes());
        v
    }
    fn cancel(ts: u64, oref: u64, shares: u32) -> Vec<u8> {
        let mut v = head(b'X', 1, ts);
        v.extend_from_slice(&oref.to_be_bytes());
        v.extend_from_slice(&shares.to_be_bytes());
        v
    }
    fn delete(ts: u64, oref: u64) -> Vec<u8> {
        let mut v = head(b'D', 1, ts);
        v.extend_from_slice(&oref.to_be_bytes());
        v
    }
    fn replace(ts: u64, old: u64, new: u64, shares: u32, price: u32) -> Vec<u8> {
        let mut v = head(b'U', 1, ts);
        v.extend_from_slice(&old.to_be_bytes());
        v.extend_from_slice(&new.to_be_bytes());
        v.extend_from_slice(&shares.to_be_bytes());
        v.extend_from_slice(&price.to_be_bytes());
        v
    }
    fn hidden(locate: u16, ts: u64, shares: u32, price: u32) -> Vec<u8> {
        let mut v = head(b'P', locate, ts);
        v.extend_from_slice(&0u64.to_be_bytes());
        v.push(b'B');
        v.extend_from_slice(&shares.to_be_bytes());
        v.extend_from_slice(b"TEST    ");
        v.extend_from_slice(&price.to_be_bytes());
        v.extend_from_slice(&9u64.to_be_bytes());
        v
    }
    const S: u64 = 1_000_000_000;

    fn run(msgs: &[Vec<u8>]) -> (Vec<(String, char, u64, Box<[Bar]>)>, Stats) {
        let mut b = Builder::new();
        for m in msgs {
            b.apply(m);
        }
        let st = b.stats.clone();
        (b.finish(), st)
    }

    #[test]
    fn quote_set_before_the_open_is_carried_through_every_bar() {
        let (rows, _) = run(&[
            directory(1, "TEST", b'N'),
            add(1, OPEN_NS - 5 * S, 1, true, 100, 1_000_000),
            add(1, OPEN_NS - 4 * S, 2, false, 200, 1_000_200),
        ]);
        assert_eq!(rows.len(), 1);
        let bars = &rows[0].3;
        for b in bars.iter() {
            assert_eq!(
                (b.bid, b.ask, b.bid_sz, b.ask_sz),
                (1_000_000, 1_000_200, 100, 200)
            );
            assert_eq!(b.tw_valid_ns, MINUTE_NS);
            assert_eq!(b.tw_spread_ns, 200 * u128::from(MINUTE_NS));
        }
    }

    #[test]
    fn time_weighted_spread_splits_at_the_change_and_at_the_bar_boundary() {
        // spread 200 for the first 15 s of bar 0, then 100 for the rest of the day
        let (rows, _) = run(&[
            directory(1, "TEST", b'N'),
            add(1, OPEN_NS - S, 1, true, 100, 1_000_000),
            add(1, OPEN_NS - S, 2, false, 100, 1_000_200),
            add(1, OPEN_NS + 15 * S, 3, false, 50, 1_000_100),
        ]);
        let bars = &rows[0].3;
        assert_eq!(
            bars[0].tw_spread_ns,
            u128::from(200 * 15 * S + 100 * 45 * S)
        );
        assert_eq!(bars[0].ask, 1_000_100);
        assert_eq!(bars[0].ask_sz, 50);
        assert_eq!(bars[1].tw_spread_ns, 100 * u128::from(MINUTE_NS));
    }

    #[test]
    fn one_sided_and_locked_books_carry_no_valid_spread_time() {
        let (rows, _) = run(&[
            directory(1, "TEST", b'N'),
            add(1, OPEN_NS - S, 1, true, 100, 1_000_000),
            add(1, OPEN_NS + 60 * S, 2, false, 100, 1_000_000), // locked from bar 1
        ]);
        let bars = &rows[0].3;
        assert_eq!(bars[0].tw_valid_ns, 0);
        assert_eq!((bars[0].bid, bars[0].ask), (1_000_000, 0));
        assert_eq!(bars[1].tw_valid_ns, 0);
        assert_eq!((bars[1].bid, bars[1].ask), (1_000_000, 1_000_000));
    }

    #[test]
    fn executions_cancels_deletes_and_replaces_move_the_book_and_count_volume() {
        let t = OPEN_NS + 10 * S;
        let (rows, st) = run(&[
            directory(1, "TEST", b'Y'),
            add(1, OPEN_NS - S, 1, true, 300, 1_000_000),
            add(1, OPEN_NS - S, 2, false, 300, 1_000_200),
            exec(t, 1, 100),                        // displayed, at the order's price
            exec_px(t + S, 2, 50, b'Y', 1_000_150), // printable, at its own price
            exec_px(t + 2 * S, 2, 50, b'N', 1_000_150), // not printable: book only
            cancel(t + 3 * S, 1, 50),
            replace(t + 4 * S, 2, 3, 80, 1_000_300),
            hidden(1, t + 5 * S, 500, 1_000_100),
            delete(t + 6 * S, 1),
            exec(t + 7 * S, 99, 10), // unknown order
        ]);
        assert_eq!(rows[0].1, 'Y');
        let b = rows[0].3[0];
        assert_eq!(b.vol, 150);
        assert_eq!(b.trades, 2);
        assert_eq!(b.notional, 100 * 1_000_000 + 50 * 1_000_150);
        assert_eq!((b.hidden_vol, b.hidden_notional), (500, 500 * 1_000_100));
        assert_eq!((b.bid, b.bid_sz, b.ask, b.ask_sz), (0, 0, 1_000_300, 80));
        assert_eq!(st.unknown_order_refs, 1);
    }

    #[test]
    fn trades_outside_the_session_are_not_counted() {
        let (rows, _) = run(&[
            directory(1, "TEST", b'N'),
            add(1, OPEN_NS - 10 * S, 1, true, 300, 1_000_000),
            exec(OPEN_NS - S, 1, 100),
            exec(CLOSE_NS, 1, 100),
            exec(CLOSE_NS - 1, 1, 50),
        ]);
        let bars = &rows[0].3;
        assert_eq!(bars.iter().map(|b| b.vol).sum::<u64>(), 50);
        assert_eq!(bars[BARS - 1].vol, 50);
    }

    #[test]
    fn stream_framing_stops_cleanly_on_a_cut_file_and_skips_short_messages() {
        let mut bytes = Vec::new();
        for m in [
            directory(1, "TEST", b'N'),
            add(1, OPEN_NS, 1, true, 10, 5_000),
        ] {
            bytes.extend_from_slice(&(m.len() as u16).to_be_bytes());
            bytes.extend_from_slice(&m);
        }
        bytes.extend_from_slice(&[0, 3, b'A', 0, 1]); // a short 'A'
        bytes.extend_from_slice(&[0, 36, b'A']); // cut inside a message
        let mut b = Builder::new();
        b.read_stream(&bytes[..]).unwrap();
        assert_eq!(b.stats.messages, 2);
        assert_eq!(b.stats.short_messages, 1);
    }

    #[test]
    fn csv_has_the_header_and_skips_empty_bars() {
        let (rows, _) = run(&[
            directory(1, "TEST", b'N'),
            add(1, OPEN_NS - S, 1, true, 100, 1_000_000),
            add(1, OPEN_NS - S, 2, false, 100, 1_000_150),
        ]);
        let mut out = Vec::new();
        write_csv(&mut out, &rows).unwrap();
        let text = String::from_utf8(out).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines[1], HEADER);
        assert_eq!(lines.len(), 2 + BARS);
        assert_eq!(
            lines[2],
            "TEST,N,0,1000000,1000150,100,100,150.0000,60000000000,0,0,0,0,0"
        );
    }
}
