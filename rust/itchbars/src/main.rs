//! `gzip -dc 01302019.NASDAQ_ITCH50.gz | itchbars [--auction auction.csv] > bars_20190130.csv`
//!
//! Reads the decompressed ITCH 5.0 stream from stdin, writes the one-minute
//! bars of every symbol to stdout and a one-line summary to stderr.  With
//! `--auction <path>` it also writes the closing imbalance messages and
//! closing cross prints to `<path>`; the bars on stdout are unchanged.

use std::fs::File;
use std::io::{self, BufReader, BufWriter, Write};

fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let auction_path = match args.as_slice() {
        [] => None,
        [flag, path] if flag == "--auction" => Some(path.clone()),
        _ => {
            eprintln!("usage: itchbars [--auction <path>] < itch50_stream > bars.csv");
            std::process::exit(2);
        }
    };
    let mut b = itchbars::Builder::new();
    if auction_path.is_some() {
        b = b.with_auction();
    }
    b.read_stream(BufReader::with_capacity(1 << 22, io::stdin().lock()))?;
    if let Some(path) = &auction_path {
        let mut a = BufWriter::new(File::create(path)?);
        itchbars::write_auction_csv(&mut a, b.auction_rows())?;
        a.flush()?;
    }
    let auction_rows = b.auction_rows().len();
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
    if auction_path.is_some() {
        eprintln!(
            "itchbars: auction rows {} short_auction_messages {}",
            auction_rows, stats.short_auction_messages
        );
    }
    Ok(())
}
