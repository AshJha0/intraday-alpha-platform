//! Event-driven incremental feature engine (API_FEATURES.md, pinned).
//!
//! Rust port of the reference `iap/features/engine.py` restricted to the 40
//! native features (+5 auxiliary alpha inputs, see [`crate::names`]). The
//! pinned state-update semantics are mirrored exactly:
//!
//! - An `exchange_ts` below the instrument's last seen `exchange_ts`
//!   (cross-venue clock skew) is dropped + counted before the book sees it.
//! - Events are routed to a per-instrument [`ConsolidatedBook`]; only events
//!   the book reports `Applied` feed rolling state (a duplicate, an invalid
//!   side, a malformed payload or an event dropped while stale contributes
//!   nothing).  The merged top-10 view is refreshed after applied
//!   book-touching events (ADD, MODIFY, CANCEL, EXECUTE, QUOTE, and the
//!   FINAL record of a SNAPSHOT burst — `trade_id == 0`) and after any event
//!   that changed the set of stale venues (*staleness refresh*: view only,
//!   no samples), merging non-stale venue books only (same price => sizes
//!   summed; venues iterated in ascending venue_id).
//! - A stale -> fresh recovery CLEARS every rolling window and history and
//!   re-anchors warmup at the recovery timestamp (`warmup_after_recovery`).
//! - Every `x / (y + EPS)` ratio is invalid when `y <= 0`.
//! - `book_ok` means both sides quoted after the merge.
//! - Windows are half-open event-time intervals `(t - w, t]`.
//! - Mid-derived samples (returns, realized vol) are recorded whenever the
//!   merged mid differs from the last RECORDED mid sample; depth samples at
//!   every two-sided refresh; OFI at every refresh. History lookups
//!   `x(t - h)` are at-or-before, no interpolation.
//! - Warmup: a windowed feature is invalid until `t - first_event_ts >= w`.
//! - Cadence 0 emits one vector after every event of the instrument;
//!   otherwise at most one vector per cadence interval (pure event time).
//!
//! Validity: NaN never appears with `valid == true` — every value flows
//! through a single funnel that forces invalid on non-finite input.

use std::collections::BTreeMap;

use marketdata::{EventType, IapError, MarketEvent};
use orderbook::{ApplyStatus, ConsolidatedBook};

use crate::names::{feature_index, FEATURE_COUNT, FEATURE_NAMES};
use crate::rolling::{RollingFSum, RollingISum, TimeSeries};

const NS: i64 = 1_000_000_000;
/// History retention for the mid series (matches the reference: 2x the
/// longest lookback plus margin).
const HIST_KEEP_NS: i64 = 660 * NS;
/// Pinned epsilon for guarded divisions (conventions / API_FEATURES §3).
pub const EPS: f64 = 1e-12;
/// Largest quantity folded into a rolling window (API_FEATURES.md §2.2):
/// beyond this a feed is malformed, not a market, and an i64 window sum
/// could no longer be exact in every port.
pub const FEATURE_MAX_QTY: i64 = 1 << 40;

const W1S: i64 = NS;
const W5S: i64 = 5 * NS;
const W10S: i64 = 10 * NS;
const W30S: i64 = 30 * NS;
const W1M: i64 = 60 * NS;
const W5M: i64 = 300 * NS;

/// One emitted feature vector (documented 45-slot sub-vector of the
/// registry, indexed by `crate::names::FEATURE_NAMES`).
#[derive(Debug, Clone)]
pub struct FeatureVector {
    /// Instrument the vector belongs to.
    pub instrument_id: u32,
    /// `exchange_ts` of the emission event (event time, ns).
    pub timestamp: i64,
    /// Values in `FEATURE_NAMES` order; NaN where invalid.
    pub values: [f64; FEATURE_COUNT],
    /// Parallel validity mask.
    pub validity: [bool; FEATURE_COUNT],
}

impl FeatureVector {
    /// Value by registry name: `Some(v)` when computed here and valid.
    pub fn get(&self, name: &str) -> Option<f64> {
        let i = feature_index(name)?;
        if self.validity[i] {
            Some(self.values[i])
        } else {
            None
        }
    }

    /// True when `name` is computed by this engine (valid or not).
    pub fn has(name: &str) -> bool {
        feature_index(name).is_some()
    }

    /// Validity as a little-endian-bit-packed bitset (bit i = feature i),
    /// per the FeatureVector contract.
    pub fn validity_bits(&self) -> Vec<u8> {
        let mut out = vec![0u8; FEATURE_COUNT.div_ceil(8)];
        for (i, &v) in self.validity.iter().enumerate() {
            if v {
                out[i >> 3] |= 1 << (i & 7);
            }
        }
        out
    }
}

/// One side's cached top-10 depth: `(price_ticks, size)` best-first.
type Depth = Vec<(i64, i64)>;

/// Per-instrument rolling state (mirrors `_InstState` in the reference).
struct InstState {
    tick: f64,
    cons: ConsolidatedBook,
    first_ts: Option<i64>,
    /// warmup anchor: first event, or the last stale->fresh recovery
    warm_ts: Option<i64>,
    /// stale->fresh recoveries so far (rolling-state resets)
    recoveries: u64,
    /// sorted ids of this instrument's venues whose book is stale
    stale_venues: Vec<u16>,
    /// monotone count of merged-view refreshes (label sampling hook)
    refresh_seq: u64,
    /// last seen exchange_ts (timestamp-regression guard)
    last_ts: i64,
    t: i64,
    last_emit: Option<i64>,
    // current merged book view
    book_ok: bool,
    depth_bid: Vec<(i64, i64)>,
    depth_ask: Vec<(i64, i64)>,
    bid_p: i64,
    bid_q: i64,
    ask_p: i64,
    ask_q: i64,
    db: [i64; 4], // depth sums at k = 1, 3, 5, 10
    da: [i64; 4],
    mid2: i64,
    mid: f64,
    logmid: f64,
    spread_ticks: i64,
    spread_bps: f64,
    // per-venue cached top-10 depth (ascending venue id)
    venue_cache: BTreeMap<u16, (Depth, Depth)>,
    // rolling structures
    hist2: TimeSeries<i64>,
    histlog: TimeSeries<f64>,
    rv: [RollingFSum; 3],       // dlm^2 over 10s / 1m / 5m
    ofi: [RollingISum<4>; 3],   // e(1|3|5|10) over 1s / 5s / 30s
    trades: [RollingISum<3>; 3], // (signed, buy, sell) over 1s / 10s / 1m
    depthavg: RollingISum<4>,   // (db1, da1, db5, da5) over 10s
    // scratch buffers reused across refreshes (no steady-state allocation)
    agg_b: BTreeMap<i64, i64>,
    agg_a: BTreeMap<i64, i64>,
}

impl InstState {
    fn new(instrument_id: u32, tick: f64) -> InstState {
        InstState {
            tick,
            cons: ConsolidatedBook::new(instrument_id),
            first_ts: None,
            warm_ts: None,
            recoveries: 0,
            stale_venues: Vec::new(),
            refresh_seq: 0,
            last_ts: 0,
            t: 0,
            last_emit: None,
            book_ok: false,
            depth_bid: Vec::with_capacity(10),
            depth_ask: Vec::with_capacity(10),
            bid_p: 0,
            bid_q: 0,
            ask_p: 0,
            ask_q: 0,
            db: [0; 4],
            da: [0; 4],
            mid2: 0,
            mid: 0.0,
            logmid: 0.0,
            spread_ticks: 0,
            spread_bps: 0.0,
            venue_cache: BTreeMap::new(),
            hist2: TimeSeries::new(),
            histlog: TimeSeries::new(),
            rv: [
                RollingFSum::new(W10S),
                RollingFSum::new(W1M),
                RollingFSum::new(W5M),
            ],
            ofi: [
                RollingISum::new(W1S),
                RollingISum::new(W5S),
                RollingISum::new(W30S),
            ],
            trades: [
                RollingISum::new(W1S),
                RollingISum::new(W10S),
                RollingISum::new(W1M),
            ],
            depthavg: RollingISum::new(W10S),
            agg_b: BTreeMap::new(),
            agg_a: BTreeMap::new(),
        }
    }

    /// True when the window has fully elapsed since the warmup anchor (the
    /// first event, or the last stale->fresh recovery).
    fn warm(&self, w_ns: i64) -> bool {
        matches!(self.warm_ts, Some(f) if self.t - f >= w_ns)
    }

    /// Clear every rolling window and history; re-anchor warmup at `t`
    /// (stale->fresh recovery, API_FEATURES.md §2.1).
    fn reset_rolling(&mut self, t: i64) {
        self.warm_ts = Some(t);
        self.recoveries += 1;
        self.hist2 = TimeSeries::new();
        self.histlog = TimeSeries::new();
        self.rv = [
            RollingFSum::new(W10S),
            RollingFSum::new(W1M),
            RollingFSum::new(W5M),
        ];
        self.ofi = [
            RollingISum::new(W1S),
            RollingISum::new(W5S),
            RollingISum::new(W30S),
        ];
        self.trades = [
            RollingISum::new(W1S),
            RollingISum::new(W10S),
            RollingISum::new(W1M),
        ];
        self.depthavg = RollingISum::new(W10S);
        self.depth_bid.clear();
        self.depth_ask.clear();
        self.book_ok = false;
    }

    /// Realized vol over window index (0 = 10s, 1 = 1m, 2 = 5m); `None`
    /// until the window is warm. The `max()` guards float drain-drift.
    fn rvol(&self, wi: usize) -> Option<f64> {
        let w_ns = [W10S, W1M, W5M][wi];
        if !self.warm(w_ns) {
            return None;
        }
        Some((self.rv[wi].sum.max(0.0) / (w_ns as f64 / 1e9)).sqrt())
    }

    fn trim_all(&mut self, t: i64) {
        for w in &mut self.rv {
            w.trim(t);
        }
        for w in &mut self.ofi {
            w.trim(t);
        }
        for w in &mut self.trades {
            w.trim(t);
        }
        self.depthavg.trim(t);
        self.hist2.trim(t - HIST_KEEP_NS);
        self.histlog.trim(t - HIST_KEEP_NS);
    }
}

/// Signed depth change within the best-k levels (the OFI building block,
/// pinned): sum over the union of prev/curr prices of `curr[p] - prev[p]`.
fn depth_delta(prev: &[(i64, i64)], curr: &[(i64, i64)], k: usize) -> i64 {
    let prev_k = &prev[..prev.len().min(k)];
    let curr_k = &curr[..curr.len().min(k)];
    let mut d = 0i64;
    for &(p, q) in curr_k {
        let pq = prev_k
            .iter()
            .find(|&&(pp, _)| pp == p)
            .map_or(0, |&(_, q0)| q0);
        d += q - pq;
    }
    for &(p, q) in prev_k {
        if !curr_k.iter().any(|&(cp, _)| cp == p) {
            d -= q;
        }
    }
    d
}

/// Incremental event-driven feature computation over a replay stream.
pub struct FeatureEngine {
    ticks: BTreeMap<u32, f64>,
    cadence_ns: i64,
    states: BTreeMap<u32, InstState>,
    /// Events consumed so far.
    pub events_processed: u64,
    /// Events the book dropped/held — never folded into rolling state.
    pub events_dropped: u64,
    /// Events dropped for an exchange_ts regression (fail closed).
    pub ts_regressions_dropped: u64,
    /// Applied events whose qty exceeded [`FEATURE_MAX_QTY`] (not folded).
    pub oversized_qty_dropped: u64,
    /// Refreshes whose merged depth exceeded [`FEATURE_MAX_QTY`].
    pub oversized_depth_skipped: u64,
    /// Vectors emitted so far.
    pub vectors_emitted: u64,
}

impl FeatureEngine {
    /// New engine. `ticks` maps instrument_id -> tick_size; `cadence_ns = 0`
    /// emits after every event.
    pub fn new(ticks: BTreeMap<u32, f64>, cadence_ns: i64) -> Result<FeatureEngine, IapError> {
        if cadence_ns < 0 {
            return Err(IapError::InvalidArgument(
                "cadence_ns must be >= 0".to_string(),
            ));
        }
        for (&iid, &tick) in &ticks {
            if !(tick.is_finite() && tick > 0.0) {
                return Err(IapError::InvalidArgument(format!(
                    "instrument {iid}: tick_size must be finite and > 0"
                )));
            }
        }
        Ok(FeatureEngine {
            ticks,
            cadence_ns,
            states: BTreeMap::new(),
            events_processed: 0,
            events_dropped: 0,
            ts_regressions_dropped: 0,
            oversized_qty_dropped: 0,
            oversized_depth_skipped: 0,
            vectors_emitted: 0,
        })
    }

    /// Apply one event; returns the emitted vector, if any.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<Option<FeatureVector>, IapError> {
        let tick = *self.ticks.get(&ev.instrument_id).ok_or_else(|| {
            IapError::InvalidArgument(format!(
                "no tick_size configured for instrument {}",
                ev.instrument_id
            ))
        })?;
        let st = self
            .states
            .entry(ev.instrument_id)
            .or_insert_with(|| InstState::new(ev.instrument_id, tick));
        let t = ev.exchange_ts;
        if st.first_ts.is_some() && t < st.last_ts {
            // Cross-venue exchange_ts regression: dropped + counted before
            // the book sees it (pinned, API_FEATURES.md §2). Never an error.
            self.ts_regressions_dropped += 1;
            self.events_dropped += 1;
            self.events_processed += 1;
            return Ok(None);
        }
        let status = st.cons.apply(ev)?;
        if st.first_ts.is_none() {
            st.first_ts = Some(t);
            st.warm_ts = Some(t);
        }
        st.last_ts = t;
        st.t = t;
        self.events_processed += 1;

        // The merged view is a function of WHICH venues are stale, so the
        // trigger is a change of the stale SET (a second venue going stale
        // must leave the view too), not of "any venue is stale".
        let stale_now: Vec<u16> = st
            .cons
            .books
            .iter()
            .filter(|(_, b)| b.stale)
            .map(|(&v, _)| v)
            .collect();
        let stale_changed = stale_now != st.stale_venues;
        let just_recovered = !st.stale_venues.is_empty() && stale_now.is_empty();
        st.stale_venues = stale_now;

        if status != ApplyStatus::Applied {
            self.events_dropped += 1;
            if stale_changed {
                if Self::refresh_book(st, ev.venue_id, t, just_recovered, false) {
                    self.oversized_depth_skipped += 1;
                }
            }
            return Ok(self.emit_if_due(ev.instrument_id, t));
        }

        let et = EventType::from_u8(ev.event_type);
        if ev.qty > FEATURE_MAX_QTY {
            // Oversized quantity (pinned §2.2): the book may hold it, but no
            // rolling window folds it in — an i64 window sum stays exact.
            // The merged view is still refreshed (book state changed).
            self.oversized_qty_dropped += 1;
        } else if et == Some(EventType::Trade) {
            let buy = if ev.side == 0 { ev.qty } else { 0 };
            let sell = ev.qty - buy;
            let vals = [buy - sell, buy, sell];
            for w in &mut st.trades {
                w.add(t, vals);
            }
        }
        let touches = matches!(
            et,
            Some(EventType::Add)
                | Some(EventType::Modify)
                | Some(EventType::Cancel)
                | Some(EventType::Execute)
                | Some(EventType::Quote)
        ) || (et == Some(EventType::Snapshot) && ev.trade_id == 0);
        let oversized_depth = if touches {
            Self::refresh_book(st, ev.venue_id, t, just_recovered, true)
        } else if stale_changed {
            Self::refresh_book(st, ev.venue_id, t, just_recovered, false)
        } else {
            false
        };
        if oversized_depth {
            self.oversized_depth_skipped += 1;
        }

        Ok(self.emit_if_due(ev.instrument_id, t))
    }

    /// Cadence check (pure event time); emits at most one vector.
    fn emit_if_due(&mut self, instrument_id: u32, t: i64) -> Option<FeatureVector> {
        let st = self.states.get_mut(&instrument_id)?;
        let emit = self.cadence_ns == 0
            || match st.last_emit {
                None => true,
                Some(le) => t - le >= self.cadence_ns,
            };
        if emit {
            let vec = Self::emit(st, instrument_id, t);
            st.last_emit = Some(t);
            self.vectors_emitted += 1;
            Some(vec)
        } else {
            None
        }
    }

    /// Recompute the merged non-stale top-10 view after a book-touching
    /// event; record OFI / depth / mid-change samples (pinned semantics).
    /// `samples == false` is a *staleness refresh*: the merged view and
    /// `book_ok` are recomputed because the stale-venue set changed, but no
    /// OFI / depth / mid sample is recorded.
    fn refresh_book(
        st: &mut InstState,
        venue_id: u16,
        t: i64,
        just_recovered: bool,
        samples: bool,
    ) -> bool {
        st.refresh_seq += 1;
        if just_recovered {
            st.reset_rolling(t);
        }
        if let Some(vb) = st.cons.books.get(&venue_id) {
            st.venue_cache
                .insert(venue_id, (vb.depth(0, 10), vb.depth(1, 10)));
        }
        st.agg_b.clear();
        st.agg_a.clear();
        for (vid, (b10, a10)) in &st.venue_cache {
            if st.cons.books.get(vid).is_some_and(|b| b.stale) {
                continue;
            }
            for &(p, q) in b10 {
                *st.agg_b.entry(p).or_insert(0) += q;
            }
            for &(p, q) in a10 {
                *st.agg_a.entry(p).or_insert(0) += q;
            }
        }
        let bid: Vec<(i64, i64)> = st.agg_b.iter().rev().take(10).map(|(&p, &q)| (p, q)).collect();
        let ask: Vec<(i64, i64)> = st.agg_a.iter().take(10).map(|(&p, &q)| (p, q)).collect();

        // Oversized merged depth (pinned §2.2): a level above
        // FEATURE_MAX_QTY makes the merged view unusable — clear it, record
        // nothing, and let the next clean refresh re-baseline.
        if bid.iter().chain(ask.iter()).any(|&(_, q)| q > FEATURE_MAX_QTY) {
            st.depth_bid.clear();
            st.depth_ask.clear();
            st.book_ok = false;
            return true;
        }

        // OFI contributions (defined per side, book_ok or not).  The first
        // refresh after a recovery has no previous depth: it contributes
        // nothing, exactly like the very first refresh.
        let sample_flow = samples
            && !just_recovered
            && !(st.depth_bid.is_empty()
                && st.depth_ask.is_empty()
                && bid.is_empty()
                && ask.is_empty());
        if sample_flow {
            let mut contribs = [0i64; 4];
            for (i, k) in [1usize, 3, 5, 10].into_iter().enumerate() {
                contribs[i] =
                    depth_delta(&st.depth_bid, &bid, k) - depth_delta(&st.depth_ask, &ask, k);
            }
            for w in &mut st.ofi {
                w.add(t, contribs);
            }
        }
        st.depth_bid = bid;
        st.depth_ask = ask;

        st.book_ok = !st.depth_bid.is_empty() && !st.depth_ask.is_empty();
        if !st.book_ok || !samples {
            return false;
        }
        (st.bid_p, st.bid_q) = st.depth_bid[0];
        (st.ask_p, st.ask_q) = st.depth_ask[0];
        for (i, k) in [1usize, 3, 5, 10].into_iter().enumerate() {
            st.db[i] = st.depth_bid.iter().take(k).map(|&(_, q)| q).sum();
            st.da[i] = st.depth_ask.iter().take(k).map(|&(_, q)| q).sum();
        }
        st.mid2 = st.bid_p + st.ask_p;
        st.mid = st.mid2 as f64 * st.tick / 2.0;
        st.logmid = (st.mid2 as f64).ln();
        st.spread_ticks = st.ask_p - st.bid_p;
        st.spread_bps = st.spread_ticks as f64 * st.tick / st.mid * 1e4;

        // depth sample at every two-sided refresh
        st.depthavg.add(t, [st.db[0], st.da[0], st.db[2], st.da[2]]);

        // Mid-change samples, compared against the last RECORDED sample
        // (pinned): a one-sided flicker that moves the mid still yields a
        // vol sample; a flicker back to the same mid yields none.
        let last_mid2 = st.hist2.last();
        if last_mid2 != Some(st.mid2) {
            if last_mid2.is_some() {
                let dlm = st.logmid - st.histlog.last().unwrap_or(st.logmid);
                let sq = dlm * dlm;
                for w in &mut st.rv {
                    w.add(t, sq);
                }
            }
            st.hist2.append(t, st.mid2);
            st.histlog.append(t, st.logmid);
        }
        false
    }

    fn emit(st: &mut InstState, instrument_id: u32, t: i64) -> FeatureVector {
        st.trim_all(t);
        let mut values = [f64::NAN; FEATURE_COUNT];
        let mut validity = [false; FEATURE_COUNT];
        let put = |slot: &mut usize,
                   values: &mut [f64; FEATURE_COUNT],
                   validity: &mut [bool; FEATURE_COUNT],
                   v: Option<f64>| {
            if let Some(x) = v {
                if x.is_finite() {
                    values[*slot] = x;
                    validity[*slot] = true;
                }
            }
            *slot += 1;
        };
        let mut slot = 0usize;
        let ok = st.book_ok;
        let warm = |w: i64| st.warm(w);

        // OFI (12): levels 1/3/5/10 x windows 1s/5s/30s
        for ki in 0..4 {
            for wi in 0..3 {
                let w_ns = [W1S, W5S, W30S][wi];
                let v = if warm(w_ns) {
                    Some(st.ofi[wi].sums[ki] as f64)
                } else {
                    None
                };
                put(&mut slot, &mut values, &mut validity, v);
            }
        }
        // imbalance (4)
        for ki in 0..4 {
            let (b, a) = (st.db[ki], st.da[ki]);
            let v = if ok && b + a > 0 {
                Some((b - a) as f64 / (b + a) as f64)
            } else {
                None
            };
            put(&mut slot, &mut values, &mut validity, v);
        }
        // microprice family (5)
        put(&mut slot, &mut values, &mut validity, ok.then_some(st.mid));
        let micro = if ok && st.bid_q + st.ask_q > 0 {
            Some(
                (st.bid_p * st.ask_q + st.ask_p * st.bid_q) as f64
                    / (st.bid_q + st.ask_q) as f64
                    * st.tick,
            )
        } else {
            None
        };
        put(&mut slot, &mut values, &mut validity, micro);
        let dev = match micro {
            Some(m) if st.mid != 0.0 => Some((m - st.mid) / st.mid * 1e4),
            _ => None,
        };
        put(&mut slot, &mut values, &mut validity, dev);
        put(
            &mut slot,
            &mut values,
            &mut validity,
            ok.then_some(st.spread_ticks as f64),
        );
        put(&mut slot, &mut values, &mut validity, ok.then_some(st.spread_bps));
        // depth (6): bid/ask at k = 1, 5, 10 (db/da indices 0, 2, 3)
        for ki in [0usize, 2, 3] {
            put(&mut slot, &mut values, &mut validity, ok.then_some(st.db[ki] as f64));
            put(&mut slot, &mut values, &mut validity, ok.then_some(st.da[ki] as f64));
        }
        // signed volume (3)
        for wi in 0..3 {
            let w_ns = [W1S, W10S, W1M][wi];
            let v = warm(w_ns).then_some(st.trades[wi].sums[0] as f64);
            put(&mut slot, &mut values, &mut validity, v);
        }
        // trade imbalance (3)
        for wi in 0..3 {
            let w_ns = [W1S, W10S, W1M][wi];
            let (buy, sell) = (st.trades[wi].sums[1], st.trades[wi].sums[2]);
            let v = if warm(w_ns) && buy + sell > 0 {
                Some((buy - sell) as f64 / (buy + sell) as f64)
            } else {
                None
            };
            put(&mut slot, &mut values, &mut validity, v);
        }
        // realized vol (3)
        let rvols = [st.rvol(0), st.rvol(1), st.rvol(2)];
        for rv in rvols {
            put(&mut slot, &mut values, &mut validity, rv);
        }
        // returns (4): ret_simple_1s, ret_log_1s, ret_log_10s, ret_log_1m
        let ret_log = |h_ns: i64| -> Option<f64> {
            if !ok {
                return None;
            }
            st.histlog.at_or_before(t - h_ns).map(|p| st.logmid - p)
        };
        let simple_1s = if ok {
            st.hist2
                .at_or_before(t - W1S)
                .map(|p| st.mid2 as f64 / p as f64 - 1.0)
        } else {
            None
        };
        put(&mut slot, &mut values, &mut validity, simple_1s);
        let log_1s = ret_log(W1S);
        let log_10s = ret_log(W10S);
        let log_1m = ret_log(W1M);
        put(&mut slot, &mut values, &mut validity, log_1s);
        put(&mut slot, &mut values, &mut validity, log_10s);
        put(&mut slot, &mut values, &mut validity, log_1m);
        // auxiliary: ofi_norm_l1_w1s, ofi_norm_l5_w1s, ofi_norm_l5_w5s
        let ofi_norm = |ki: usize, wi: usize, bi: usize, ai: usize| -> Option<f64> {
            let w_ns = [W1S, W5S, W30S][wi];
            if warm(w_ns) && warm(W10S) && st.depthavg.count > 0 {
                let denom = (st.depthavg.sums[bi] + st.depthavg.sums[ai]) as f64
                    / st.depthavg.count as f64;
                // Exact INTEGER guard (API_FEATURES.md §4): a float `> 0`
                // test would flip between languages on accumulation drift.
                if st.depthavg.sums[bi] + st.depthavg.sums[ai] > 0 {
                    Some(st.ofi[wi].sums[ki] as f64 / (denom + EPS))
                } else {
                    None
                }
            } else {
                None
            }
        };
        put(&mut slot, &mut values, &mut validity, ofi_norm(0, 0, 0, 1));
        put(&mut slot, &mut values, &mut validity, ofi_norm(2, 0, 2, 3));
        put(&mut slot, &mut values, &mut validity, ofi_norm(2, 1, 2, 3));
        // ret_vol_adj_10s = ret_log_10s / (rvol_w1m + EPS)
        // EPS guard: the denominator is undefined when the vol window holds
        // no mid-change SAMPLE (exact integer count, not `rvol > 0`).
        let rva = match (log_10s, rvols[1]) {
            (Some(r), Some(v)) if st.rv[1].count > 0 => Some(r / (v + EPS)),
            _ => None,
        };
        put(&mut slot, &mut values, &mut validity, rva);
        // vol_regime_ratio = rvol_w1m / (rvol_w5m + EPS)
        let vrr = match (rvols[1], rvols[2]) {
            (Some(a), Some(b)) if st.rv[2].count > 0 => Some(a / (b + EPS)),
            _ => None,
        };
        put(&mut slot, &mut values, &mut validity, vrr);

        debug_assert_eq!(slot, FEATURE_COUNT);
        debug_assert_eq!(FEATURE_NAMES.len(), FEATURE_COUNT);
        FeatureVector {
            instrument_id,
            timestamp: t,
            values,
            validity,
        }
    }

    /// Current consolidated mid and half-spread (price units) for an
    /// instrument, when its merged book is two-sided. Used by label
    /// construction (API_FEATURES.md §6: one mid sample per `book_ok`
    /// refresh).
    pub fn mid_state(&self, instrument_id: u32) -> Option<(f64, f64)> {
        let st = self.states.get(&instrument_id)?;
        if st.book_ok {
            Some((st.mid, st.spread_ticks as f64 * st.tick / 2.0))
        } else {
            None
        }
    }

    /// Monotone count of merged-view refreshes for an instrument: the label
    /// layer samples the mid series once per refresh (API_FEATURES.md §6).
    pub fn refresh_seq(&self, instrument_id: u32) -> u64 {
        self.states
            .get(&instrument_id)
            .map_or(0, |st| st.refresh_seq)
    }

    /// True when this refresh is a TRADABLE market state (API_FEATURES.md
    /// §6): two-sided merged book, no stale venue.  (Session status is not
    /// tracked by this port; a caller that observes STATUS events must add
    /// the HALT / AUCTION condition itself.)
    pub fn label_tradable(&self, instrument_id: u32) -> bool {
        self.states
            .get(&instrument_id)
            .is_some_and(|st| st.book_ok && st.stale_venues.is_empty())
    }

    /// Stale->fresh recoveries seen for an instrument (rolling-state resets).
    pub fn recoveries(&self, instrument_id: u32) -> u64 {
        self.states.get(&instrument_id).map_or(0, |st| st.recoveries)
    }

    /// Warmup anchor of an instrument (first event, or last recovery).
    pub fn warm_ts(&self, instrument_id: u32) -> Option<i64> {
        self.states.get(&instrument_id).and_then(|st| st.warm_ts)
    }

    /// True when the instrument's merged book is currently two-sided.
    pub fn book_ok(&self, instrument_id: u32) -> bool {
        self.states.get(&instrument_id).is_some_and(|st| st.book_ok)
    }

    /// Brute-force recomputation hooks: live OFI window samples for a
    /// window index (0 = 1s, 1 = 5s, 2 = 30s).
    pub fn ofi_samples(&self, instrument_id: u32, wi: usize) -> Vec<(i64, [i64; 4])> {
        self.states
            .get(&instrument_id)
            .map(|st| st.ofi[wi].samples().copied().collect())
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn depth_delta_matches_pinned_cases() {
        // new level appears within top-k
        assert_eq!(depth_delta(&[(100, 5)], &[(101, 3), (100, 5)], 2), 3);
        // level leaves top-k
        assert_eq!(depth_delta(&[(101, 3), (100, 5)], &[(100, 5)], 2), -3);
        // size change on a shared price
        assert_eq!(depth_delta(&[(100, 5)], &[(100, 9)], 1), 4);
        // truncation at k
        assert_eq!(depth_delta(&[(100, 5), (99, 7)], &[(100, 5), (99, 8)], 1), 0);
        // empty prev: full new depth counts
        assert_eq!(depth_delta(&[], &[(100, 5), (99, 7)], 10), 12);
    }

    #[test]
    fn unknown_instrument_is_an_error() {
        let mut eng = FeatureEngine::new(BTreeMap::new(), 0).unwrap();
        let ev = MarketEvent {
            event_id: 1,
            instrument_id: 42,
            venue_id: 1,
            exchange_ts: 10,
            receive_ts: 10,
            sequence: 1,
            event_type: EventType::Add.as_u8(),
            side: 0,
            price_ticks: 100,
            qty: 1,
            order_id: 1,
            trade_id: 0,
        };
        assert!(eng.apply(&ev).is_err());
    }

    #[test]
    fn bad_cadence_and_bad_tick_rejected() {
        assert!(FeatureEngine::new(BTreeMap::new(), -1).is_err());
        let mut ticks = BTreeMap::new();
        ticks.insert(1u32, 0.0f64);
        assert!(FeatureEngine::new(ticks, 0).is_err());
    }
}
