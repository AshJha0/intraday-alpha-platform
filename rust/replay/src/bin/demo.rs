//! Replay demo: loads the golden vectors, replays them through the
//! deterministic engine, and prints a book summary plus throughput.
//!
//! Usage: `cargo run --bin demo [-- <golden-dir>]`
//! (defaults to the repo's `tests/golden/` relative to this crate).
//!
//! The wall clock is used ONLY for the printed events/sec figure; nothing on
//! the deterministic replay path depends on it.

use std::path::PathBuf;
use std::time::Instant;

use marketdata::{read_jsonl, validate, IapError, MarketEvent};
use replay::ReplayEngine;

fn default_golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden")
}

fn fmt_side(side: Option<(i64, i64)>) -> String {
    match side {
        Some((price, size)) => format!("{size} @ {price}"),
        None => "-".to_string(),
    }
}

fn replay_vector(name: &str, events: &[MarketEvent]) -> Result<(), IapError> {
    for ev in events {
        validate(ev)?;
    }

    let mut engine = ReplayEngine::new(500, 500);
    let start = Instant::now();
    let summary = engine.run(events)?;
    let elapsed = start.elapsed();
    let secs = elapsed.as_secs_f64();
    let rate = if secs > 0.0 {
        summary.events_processed as f64 / secs
    } else {
        f64::INFINITY
    };

    println!("== {name} ==");
    println!(
        "  events: {}   instruments: {}   snapshots: {}   time_regressions: {}",
        summary.events_processed, summary.instruments, summary.snapshots, summary.time_regressions
    );
    println!("  throughput: {rate:.0} events/sec ({:.3} ms total)", secs * 1e3);
    for (iid, cons) in &engine.books {
        println!(
            "  instrument {iid}: best_bid [{}]  best_ask [{}]  trade_flow {}",
            fmt_side(cons.best_bid()),
            fmt_side(cons.best_ask()),
            cons.trade_flow()
        );
        for (vid, book) in &cons.books {
            println!(
                "    venue {vid}: bid [{}]  ask [{}]  seq {}  applied {}  dup {}  gaps {}",
                fmt_side(book.best_bid()),
                fmt_side(book.best_ask()),
                book.last_sequence,
                book.counters.events_applied,
                book.counters.duplicates_dropped,
                book.counters.gaps_detected,
            );
        }
    }
    println!();
    Ok(())
}

fn run() -> Result<(), IapError> {
    let golden_dir = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(default_golden_dir);

    println!("IAP Rust replay demo — golden dir: {}\n", golden_dir.display());
    for name in ["events_eq_mbo.jsonl", "events_fx_quote.jsonl"] {
        let events = read_jsonl(golden_dir.join(name))?;
        replay_vector(name, &events)?;
    }
    Ok(())
}

fn main() {
    if let Err(err) = run() {
        eprintln!("demo failed: {err}");
        std::process::exit(1);
    }
}
