//! Incremental-vs-brute-force verification for six windowed/history
//! features (task: feature engine correctness beyond golden checkpoints).
//!
//! The brute-force side maintains *full, never-evicted* sample histories
//! built independently of the engine's rolling state:
//!
//! - merged non-stale top-10 depth recomputed from a second
//!   `ConsolidatedBook` after every book-touching event (independent merge
//!   code), feeding an OFI contribution log and a mid-change log;
//! - a trade log taken straight from the raw events.
//!
//! At every 100th event the half-open window `(t - w, t]` is re-evaluated
//! by scanning the full logs and compared with the engine's incremental
//! value: exact for integer features, 1e-12 relative for floats.

mod support;

use std::collections::{BTreeMap, HashMap};

use features::{feature_index, FeatureEngine, FeatureVector};
use marketdata::{read_jsonl, EventType, MarketEvent};
use orderbook::ConsolidatedBook;
use support::golden_path;

const NS: i64 = 1_000_000_000;

/// One side's top-10 depth: `(price_ticks, size)` best-first.
type Depth = Vec<(i64, i64)>;

/// Independent merged top-10 (bid, ask) over non-stale venue books.
fn merge(cons: &ConsolidatedBook, cache: &BTreeMap<u16, (Depth, Depth)>) -> (Depth, Depth) {
    let mut bid: HashMap<i64, i64> = HashMap::new();
    let mut ask: HashMap<i64, i64> = HashMap::new();
    for (vid, (b10, a10)) in cache {
        if cons.books.get(vid).map(|b| b.stale).unwrap_or(false) {
            continue;
        }
        for &(p, q) in b10 {
            *bid.entry(p).or_insert(0) += q;
        }
        for &(p, q) in a10 {
            *ask.entry(p).or_insert(0) += q;
        }
    }
    let mut b: Vec<(i64, i64)> = bid.into_iter().collect();
    let mut a: Vec<(i64, i64)> = ask.into_iter().collect();
    b.sort_by_key(|&(p, _)| std::cmp::Reverse(p));
    a.sort_by_key(|&(p, _)| p);
    b.truncate(10);
    a.truncate(10);
    (b, a)
}

/// Union-based signed depth delta over the best k levels (independent of
/// the engine's implementation).
fn delta_union(prev: &[(i64, i64)], curr: &[(i64, i64)], k: usize) -> i64 {
    let mut union: BTreeMap<i64, (i64, i64)> = BTreeMap::new();
    for &(p, q) in prev.iter().take(k) {
        union.entry(p).or_insert((0, 0)).0 = q;
    }
    for &(p, q) in curr.iter().take(k) {
        union.entry(p).or_insert((0, 0)).1 = q;
    }
    union.values().map(|&(pq, cq)| cq - pq).sum()
}

struct BruteState {
    cons: ConsolidatedBook,
    cache: BTreeMap<u16, (Depth, Depth)>,
    bid: Depth,
    ask: Depth,
    book_ok: bool,
    mid2: i64,
    /// full OFI contribution log: (ts, e1, e5)
    ofi_log: Vec<(i64, i64, i64)>,
    /// full mid-change sample log: (ts, mid2)
    mid_log: Vec<(i64, i64)>,
    /// full squared-log-mid-change log: (ts, dlm^2). Per the pinned
    /// reference, a change is sampled only when the PREVIOUS refresh also
    /// had a two-sided book (`prev_ok`): changes across a one-sided /
    /// empty-book stretch contribute no realized-vol sample.
    rv_log: Vec<(i64, f64)>,
    /// full trade log: (ts, signed, buy, sell)
    trade_log: Vec<(i64, i64, i64, i64)>,
    first_ts: Option<i64>,
}

impl BruteState {
    fn new(instrument_id: u32) -> BruteState {
        BruteState {
            cons: ConsolidatedBook::new(instrument_id),
            cache: BTreeMap::new(),
            bid: Vec::new(),
            ask: Vec::new(),
            book_ok: false,
            mid2: 0,
            ofi_log: Vec::new(),
            mid_log: Vec::new(),
            rv_log: Vec::new(),
            trade_log: Vec::new(),
            first_ts: None,
        }
    }

    fn apply(&mut self, ev: &MarketEvent) {
        self.cons.apply(ev).expect("golden events apply");
        let t = ev.exchange_ts;
        self.first_ts.get_or_insert(t);
        let et = EventType::from_u8(ev.event_type);
        if et == Some(EventType::Trade) {
            let buy = if ev.side == 0 { ev.qty } else { 0 };
            let sell = ev.qty - buy;
            self.trade_log.push((t, buy - sell, buy, sell));
        }
        let touches = matches!(
            et,
            Some(EventType::Add)
                | Some(EventType::Modify)
                | Some(EventType::Cancel)
                | Some(EventType::Execute)
                | Some(EventType::Quote)
        ) || (et == Some(EventType::Snapshot) && ev.trade_id == 0);
        if !touches {
            return;
        }
        if let Some(vb) = self.cons.books.get(&ev.venue_id) {
            self.cache
                .insert(ev.venue_id, (vb.depth(0, 10), vb.depth(1, 10)));
        }
        let (bid, ask) = merge(&self.cons, &self.cache);
        if !(self.bid.is_empty() && self.ask.is_empty() && bid.is_empty() && ask.is_empty()) {
            let e1 = delta_union(&self.bid, &bid, 1) - delta_union(&self.ask, &ask, 1);
            let e5 = delta_union(&self.bid, &bid, 5) - delta_union(&self.ask, &ask, 5);
            self.ofi_log.push((t, e1, e5));
        }
        let prev_ok = self.book_ok;
        let prev_mid2 = self.mid2;
        self.bid = bid;
        self.ask = ask;
        self.book_ok = !self.bid.is_empty() && !self.ask.is_empty();
        if self.book_ok {
            self.mid2 = self.bid[0].0 + self.ask[0].0;
            if !prev_ok || self.mid2 != prev_mid2 {
                if prev_ok {
                    if let Some(&(_, m0)) = self.mid_log.last() {
                        let dlm = (self.mid2 as f64).ln() - (m0 as f64).ln();
                        self.rv_log.push((t, dlm * dlm));
                    }
                }
                self.mid_log.push((t, self.mid2));
            }
        }
    }

    fn warm(&self, t: i64, w: i64) -> bool {
        matches!(self.first_ts, Some(f) if t - f >= w)
    }
}

/// Drive both sides over a golden vector, comparing at every 100th event.
fn run_comparison(vector: &str, instrument_id: u32, tick: f64, check: impl Fn(&BruteState, i64, &FeatureVector)) {
    let events = read_jsonl(golden_path(vector)).expect("golden vector");
    let mut ticks = BTreeMap::new();
    ticks.insert(instrument_id, tick);
    let mut eng = FeatureEngine::new(ticks, 0).unwrap();
    let mut brute = BruteState::new(instrument_id);
    let mut checks = 0;
    for (i, ev) in events.iter().enumerate() {
        let vec = eng.apply(ev).unwrap().expect("cadence 0");
        brute.apply(ev);
        if (i + 1) % 100 == 0 {
            check(&brute, ev.exchange_ts, &vec);
            checks += 1;
        }
    }
    assert!(checks >= 8, "too few comparison points: {checks}");
}

fn slot(name: &str) -> usize {
    feature_index(name).unwrap_or_else(|| panic!("{name} not native"))
}

#[test]
fn ofi_l1_w1s_matches_brute_force() {
    let s = slot("ofi_l1_w1s_v1");
    run_comparison("events_eq_mbo.jsonl", 1, 0.01, |b, t, vec| {
        if !b.warm(t, NS) {
            assert!(!vec.validity[s]);
            return;
        }
        let want: i64 = b
            .ofi_log
            .iter()
            .filter(|&&(ts, _, _)| ts > t - NS && ts <= t)
            .map(|&(_, e1, _)| e1)
            .sum();
        assert!(vec.validity[s]);
        assert_eq!(vec.values[s], want as f64, "ofi_l1_w1s at t={t}");
    });
}

#[test]
fn ofi_l5_w5s_matches_brute_force() {
    let s = slot("ofi_l5_w5s_v1");
    run_comparison("events_eq_mbo.jsonl", 1, 0.01, |b, t, vec| {
        if !b.warm(t, 5 * NS) {
            assert!(!vec.validity[s]);
            return;
        }
        let want: i64 = b
            .ofi_log
            .iter()
            .filter(|&&(ts, _, _)| ts > t - 5 * NS && ts <= t)
            .map(|&(_, _, e5)| e5)
            .sum();
        assert!(vec.validity[s]);
        assert_eq!(vec.values[s], want as f64, "ofi_l5_w5s at t={t}");
    });
}

#[test]
fn signed_volume_w1m_matches_brute_force() {
    let s = slot("signed_volume_w1m_v1");
    run_comparison("events_eq_mbo.jsonl", 1, 0.01, |b, t, vec| {
        if !b.warm(t, 60 * NS) {
            assert!(!vec.validity[s]);
            return;
        }
        let want: i64 = b
            .trade_log
            .iter()
            .filter(|&&(ts, ..)| ts > t - 60 * NS && ts <= t)
            .map(|&(_, signed, ..)| signed)
            .sum();
        assert!(vec.validity[s]);
        assert_eq!(vec.values[s], want as f64, "signed_volume_w1m at t={t}");
    });
}

#[test]
fn trade_imbalance_w10s_matches_brute_force() {
    let s = slot("trade_imbalance_w10s_v1");
    run_comparison("events_eq_mbo.jsonl", 1, 0.01, |b, t, vec| {
        let (buy, sell) = b
            .trade_log
            .iter()
            .filter(|&&(ts, ..)| ts > t - 10 * NS && ts <= t)
            .fold((0i64, 0i64), |acc, &(_, _, bq, sq)| (acc.0 + bq, acc.1 + sq));
        if !b.warm(t, 10 * NS) || buy + sell == 0 {
            assert!(!vec.validity[s]);
            return;
        }
        let want = (buy - sell) as f64 / (buy + sell) as f64;
        assert!(vec.validity[s]);
        assert_eq!(vec.values[s], want, "trade_imbalance_w10s at t={t}");
    });
}

#[test]
fn rvol_w1m_matches_brute_force() {
    let s = slot("rvol_w1m_v1");
    run_comparison("events_eq_mbo.jsonl", 1, 0.01, |b, t, vec| {
        if !b.warm(t, 60 * NS) {
            assert!(!vec.validity[s]);
            return;
        }
        // squared log mid changes over (t-1m, t] from the full sample log
        let sum: f64 = b
            .rv_log
            .iter()
            .filter(|&&(ts, _)| ts > t - 60 * NS && ts <= t)
            .map(|&(_, sq)| sq)
            .sum();
        let want = (sum.max(0.0) / 60.0).sqrt();
        assert!(vec.validity[s]);
        let got = vec.values[s];
        // Pinned float tolerance (abs 1e-9 + rel 1e-9, as in the Python
        // brute-force validation): the engine's rolling sum drains samples
        // incrementally, so a fully-drained window carries ~1e-21 residual
        // sum (~1e-11 after sqrt) where the full rescan is exactly 0.
        assert!(
            (got - want).abs() <= 1e-9 + 1e-9 * want.abs(),
            "rvol_w1m at t={t}: {got} vs {want}"
        );
    });
}

#[test]
fn ret_log_10s_matches_brute_force() {
    let s = slot("ret_log_10s_v1");
    run_comparison("events_eq_mbo.jsonl", 1, 0.01, |b, t, vec| {
        // latest mid sample at-or-before t - 10s, by full scan
        let past = b
            .mid_log
            .iter()
            .rev()
            .find(|&&(ts, _)| ts <= t - 10 * NS)
            .map(|&(_, m)| m);
        match past {
            Some(m0) if b.book_ok => {
                assert!(vec.validity[s], "ret_log_10s should be valid at t={t}");
                let want = (b.mid2 as f64).ln() - (m0 as f64).ln();
                let got = vec.values[s];
                assert!(
                    (got - want).abs() <= 1e-12 * (1.0 + want.abs()),
                    "ret_log_10s at t={t}: {got} vs {want}"
                );
            }
            _ => assert!(!vec.validity[s], "ret_log_10s should be invalid at t={t}"),
        }
    });
}
