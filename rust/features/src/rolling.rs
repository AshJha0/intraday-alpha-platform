//! Rolling event-time primitives (mirrors `iap/features/rolling.py`).
//!
//! All windows are half-open intervals `(t - w, t]` in event time
//! (`exchange_ts`): a sample stamped exactly `t - w` has left the window, a
//! sample stamped `t` is inside it. Integer inputs keep integer running sums
//! (window sums are *exact*); float inputs accumulate with the same
//! add/evict ordering as the Python reference, well inside the 1e-9 golden
//! tolerance.
//!
//! All buffers are preallocated (`with_capacity`) at construction and only
//! grow if a window ever holds more samples than the initial capacity.

use std::collections::VecDeque;

/// Initial per-window sample capacity (golden vectors stay far below this).
const INIT_CAPACITY: usize = 4096;

/// Rolling sums of an `N`-tuple of `i64` values over an event-time window.
///
/// Integer sums are exact — incremental state is bit-identical to a
/// brute-force recomputation over the retained samples.
#[derive(Debug, Clone)]
pub struct RollingISum<const N: usize> {
    window: i64,
    buf: VecDeque<(i64, [i64; N])>,
    /// Running sums of each component over the live window.
    pub sums: [i64; N],
    /// Number of live samples.
    pub count: usize,
}

impl<const N: usize> RollingISum<N> {
    /// New rolling sum over `window_ns` (must be > 0).
    pub fn new(window_ns: i64) -> RollingISum<N> {
        RollingISum {
            window: window_ns,
            buf: VecDeque::with_capacity(INIT_CAPACITY),
            sums: [0; N],
            count: 0,
        }
    }

    /// Append one sample at event time `ts`, then evict expired samples.
    pub fn add(&mut self, ts: i64, vals: [i64; N]) {
        self.buf.push_back((ts, vals));
        for (sum, v) in self.sums.iter_mut().zip(vals.iter()) {
            *sum += v;
        }
        self.count += 1;
        self.trim(ts);
    }

    /// Evict samples with `ts <= now - window`.
    pub fn trim(&mut self, now: i64) {
        let cutoff = now - self.window;
        while let Some(&(ts, vals)) = self.buf.front() {
            if ts > cutoff {
                break;
            }
            for (sum, v) in self.sums.iter_mut().zip(vals.iter()) {
                *sum -= v;
            }
            self.count -= 1;
            self.buf.pop_front();
        }
    }

    /// Live samples, oldest first (brute-force verification hook).
    pub fn samples(&self) -> impl Iterator<Item = &(i64, [i64; N])> {
        self.buf.iter()
    }
}

/// Rolling sum of one `f64` value over an event-time window.
///
/// Accumulation order (add on `add`, subtract on `trim`, FIFO) matches the
/// Python reference so float drift is shared, and stays far inside the
/// golden tolerance either way.
#[derive(Debug, Clone)]
pub struct RollingFSum {
    window: i64,
    buf: VecDeque<(i64, f64)>,
    /// Running sum over the live window.
    pub sum: f64,
    /// Number of live samples.
    pub count: usize,
}

impl RollingFSum {
    /// New rolling sum over `window_ns` (must be > 0).
    pub fn new(window_ns: i64) -> RollingFSum {
        RollingFSum {
            window: window_ns,
            buf: VecDeque::with_capacity(INIT_CAPACITY),
            sum: 0.0,
            count: 0,
        }
    }

    /// Append one sample at event time `ts`, then evict expired samples.
    pub fn add(&mut self, ts: i64, val: f64) {
        self.buf.push_back((ts, val));
        self.sum += val;
        self.count += 1;
        self.trim(ts);
    }

    /// Evict samples with `ts <= now - window`.
    pub fn trim(&mut self, now: i64) {
        let cutoff = now - self.window;
        while let Some(&(ts, val)) = self.buf.front() {
            if ts > cutoff {
                break;
            }
            self.sum -= val;
            self.count -= 1;
            self.buf.pop_front();
        }
    }

    /// Live samples, oldest first (brute-force verification hook).
    pub fn samples(&self) -> impl Iterator<Item = &(i64, f64)> {
        self.buf.iter()
    }
}

/// Append-only `(ts, value)` series with at-or-before lookup and trimming
/// (mirrors the Python `TimeSeries`: compaction keeps lookups cheap while
/// retaining the newest sample at-or-before the trim horizon).
#[derive(Debug, Clone)]
pub struct TimeSeries<T: Copy> {
    ts: Vec<i64>,
    vals: Vec<T>,
    start: usize,
}

const COMPACT_AT: usize = 4096;

impl<T: Copy> Default for TimeSeries<T> {
    fn default() -> TimeSeries<T> {
        TimeSeries::new()
    }
}

impl<T: Copy> TimeSeries<T> {
    /// New empty series.
    pub fn new() -> TimeSeries<T> {
        TimeSeries {
            ts: Vec::with_capacity(INIT_CAPACITY),
            vals: Vec::with_capacity(INIT_CAPACITY),
            start: 0,
        }
    }

    /// Append a sample (timestamps must be non-decreasing).
    pub fn append(&mut self, ts: i64, val: T) {
        debug_assert!(self.ts.last().map_or(true, |&last| ts >= last));
        self.ts.push(ts);
        self.vals.push(val);
    }

    /// Latest value with sample ts <= `t`, or `None`.
    pub fn at_or_before(&self, t: i64) -> Option<T> {
        let i = self.ts[self.start..].partition_point(|&x| x <= t) + self.start;
        if i > self.start {
            Some(self.vals[i - 1])
        } else {
            None
        }
    }

    /// Most recent value, or `None` when empty.
    pub fn last(&self) -> Option<T> {
        self.vals.last().copied()
    }

    /// Number of retained samples.
    pub fn len(&self) -> usize {
        self.ts.len() - self.start
    }

    /// True when no samples are retained.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Forget samples with ts < `min_ts`, keeping the newest at-or-before.
    pub fn trim(&mut self, min_ts: i64) {
        let i = self.ts.partition_point(|&x| x <= min_ts);
        if i > 0 {
            self.start = self.start.max(i - 1);
        }
        if self.start >= COMPACT_AT {
            self.ts.drain(..self.start);
            self.vals.drain(..self.start);
            self.start = 0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn window_is_half_open() {
        // A sample stamped exactly t - w is OUT; one stamped t is IN.
        let mut w: RollingISum<1> = RollingISum::new(1_000);
        w.add(0, [5]);
        w.add(1_000, [7]); // trim at t=1000 evicts the t=0 sample (0 <= 0)
        assert_eq!(w.sums[0], 7);
        assert_eq!(w.count, 1);
        w.add(1_999, [1]);
        assert_eq!(w.sums[0], 8);
        w.trim(2_000); // cutoff 1000: evicts the t=1000 sample
        assert_eq!(w.sums[0], 1);
        assert_eq!(w.count, 1);
    }

    #[test]
    fn integer_sums_are_exact_vs_brute_force() {
        let mut w: RollingISum<2> = RollingISum::new(500);
        for i in 0..200i64 {
            w.add(i * 7, [i, -2 * i]);
            let brute: (i64, i64) = w
                .samples()
                .fold((0, 0), |acc, &(_, v)| (acc.0 + v[0], acc.1 + v[1]));
            assert_eq!((w.sums[0], w.sums[1]), brute);
        }
    }

    #[test]
    fn float_sum_trims_like_int_sum() {
        let mut w = RollingFSum::new(1_000);
        w.add(0, 1.5);
        w.add(500, 2.5);
        assert_eq!(w.sum, 4.0);
        w.trim(1_400); // cutoff 400: evicts the t=0 sample only
        assert_eq!(w.sum, 2.5);
        assert_eq!(w.count, 1);
    }

    #[test]
    fn timeseries_at_or_before_semantics() {
        let mut s: TimeSeries<i64> = TimeSeries::new();
        assert_eq!(s.at_or_before(100), None);
        s.append(10, 1);
        s.append(20, 2);
        s.append(20, 3); // equal timestamps allowed; latest wins
        s.append(50, 4);
        assert_eq!(s.at_or_before(9), None);
        assert_eq!(s.at_or_before(10), Some(1));
        assert_eq!(s.at_or_before(20), Some(3));
        assert_eq!(s.at_or_before(49), Some(3));
        assert_eq!(s.at_or_before(1_000), Some(4));
        assert_eq!(s.last(), Some(4));
    }

    #[test]
    fn timeseries_trim_keeps_newest_at_or_before() {
        let mut s: TimeSeries<i64> = TimeSeries::new();
        for i in 0..10 {
            s.append(i * 10, i);
        }
        s.trim(35); // keeps the ts=30 sample (newest <= 35) onward
        assert_eq!(s.at_or_before(34), Some(3));
        assert_eq!(s.at_or_before(29), None);
        assert_eq!(s.len(), 7);
    }
}
