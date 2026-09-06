//! Replay demo: loads the golden vectors, streams them from a decoder
//! thread through the bounded SPSC event bus (`eventbus::channel`) into the
//! deterministic replay engine on the consumer thread, and prints a book
//! summary plus throughput.
//!
//! Usage: `cargo run --bin demo [-- <golden-dir>]`
//! (defaults to the repo's `tests/golden/` relative to this crate).
//!
//! Backpressure policy (API_CORE §8, pinned): the producer BLOCKS (spins,
//! then yields) when the ring is full — events are never dropped between
//! the decoder and the book; a full ring is counted (`bus_full_spins`) so
//! the operator can size `BUS_CAPACITY` for the live feed rate.
//!
//! The wall clock is used ONLY for the printed events/sec figure; nothing on
//! the deterministic replay path depends on it.

use std::path::PathBuf;
use std::thread;
use std::time::Instant;

use marketdata::{read_jsonl, validate, IapError, MarketEvent};
use replay::ReplayEngine;

/// Ring capacity between the decoder and the replay engine (events).
const BUS_CAPACITY: usize = 4096;

/// Stream `events` through the SPSC ring into `engine` on this thread; the
/// producer runs on a helper thread and blocks (never drops) when full.
fn stream_through_bus(
    engine: &mut ReplayEngine,
    events: Vec<MarketEvent>,
) -> Result<(u64, u64), IapError> {
    let (mut tx, mut rx) = eventbus::channel::<Option<MarketEvent>>(BUS_CAPACITY)?;
    let producer = thread::spawn(move || -> u64 {
        let mut full_spins = 0u64;
        for ev in events {
            let mut item = Some(ev);
            loop {
                match tx.push(item) {
                    Ok(()) => break,
                    Err(back) => {
                        item = back;
                        full_spins += 1;
                        thread::yield_now();
                    }
                }
            }
        }
        let mut done = None; // end-of-stream marker
        while let Err(back) = tx.push(done) {
            done = back;
            thread::yield_now();
        }
        full_spins
    });
    let mut consumed = 0u64;
    loop {
        match rx.pop() {
            Some(Some(ev)) => {
                engine.apply(&ev)?;
                consumed += 1;
            }
            Some(None) => break,
            None => thread::yield_now(),
        }
    }
    let full_spins = producer
        .join()
        .map_err(|_| IapError::Io("producer thread panicked".to_string()))?;
    Ok((consumed, full_spins))
}

fn default_golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden")
}

fn fmt_side(side: Option<(i64, i64)>) -> String {
    match side {
        Some((price, size)) => format!("{size} @ {price}"),
        None => "-".to_string(),
    }
}

fn replay_vector(name: &str, events: Vec<MarketEvent>) -> Result<(), IapError> {
    for ev in &events {
        validate(ev)?;
    }

    let mut engine = ReplayEngine::new(0, 0);
    let n = events.len();
    let start = Instant::now();
    let (consumed, full_spins) = stream_through_bus(&mut engine, events)?;
    let elapsed = start.elapsed();
    let secs = elapsed.as_secs_f64();
    let rate = if secs > 0.0 {
        consumed as f64 / secs
    } else {
        f64::INFINITY
    };
    if consumed != n as u64 || engine.events_processed != n as u64 {
        return Err(IapError::Io(format!(
            "bus delivered {consumed} of {n} events (engine saw {})",
            engine.events_processed
        )));
    }

    println!("== {name} ==");
    println!(
        "  events: {}   instruments: {}   time_regressions: {}   bus_full_spins: {}",
        engine.events_processed,
        engine.books.len(),
        engine.time_regressions,
        full_spins
    );
    println!("  throughput: {rate:.0} events/sec through the SPSC bus ({:.3} ms total)", secs * 1e3);
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
        replay_vector(name, events)?;
    }
    Ok(())
}

fn main() {
    if let Err(err) = run() {
        eprintln!("demo failed: {err}");
        std::process::exit(1);
    }
}
