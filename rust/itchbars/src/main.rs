//! `gzip -dc 01302019.NASDAQ_ITCH50.gz | itchbars > bars_20190130.csv`
//!
//! Reads the decompressed ITCH 5.0 stream from stdin, writes the one-minute
//! bars of every symbol to stdout and a one-line summary to stderr.

use std::io::{self, BufReader, BufWriter, Write};

fn main() -> io::Result<()> {
    let mut b = itchbars::Builder::new();
    b.read_stream(BufReader::with_capacity(1 << 22, io::stdin().lock()))?;
    let stats = b.stats.clone();
    let rows = b.finish();
    let mut out = BufWriter::with_capacity(1 << 22, io::stdout().lock());
    itchbars::write_csv(&mut out, &rows)?;
    out.flush()?;
    eprintln!(
        "itchbars: messages {} symbols {} unknown_order_refs {} short_messages {}",
        stats.messages,
        rows.len(),
        stats.unknown_order_refs,
        stats.short_messages
    );
    Ok(())
}
