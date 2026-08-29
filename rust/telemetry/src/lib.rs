//! Lightweight observability primitives (spec §25).
//!
//! - [`Counter`] / [`Gauge`] — monotone counts and last-value gauges.
//! - [`Histogram`] — fixed-bucket log2 histogram of non-negative integer
//!   samples (latency in ns, sizes, ...). 65 buckets: bucket 0 holds the
//!   value 0, bucket `i >= 1` holds values in `[2^(i-1), 2^i)`. Quantiles
//!   (p50/p99/p999) are answered from the cumulative counts: the reported
//!   value is the *inclusive upper bound* of the bucket containing the
//!   requested rank (conservative for latency), so a reported quantile is
//!   always >= the exact order-statistic and within one power of two of it.
//! - [`Registry`] — named metrics with deterministic (sorted) iteration and
//!   a Prometheus text exposition writer.
//! - [`JsonlLogger`] — structured JSONL log writer (sorted keys, one event
//!   per line), used for audit-style component logs.
//!
//! Everything is single-threaded by design (the control plane wraps a
//! registry in its own synchronization if it needs sharing); no wall clock
//! is read anywhere — callers stamp event time explicitly.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::io::Write;

/// Number of histogram buckets: value 0 plus one bucket per power of two.
pub const HISTOGRAM_BUCKETS: usize = 65;

/// Monotone counter.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Counter {
    value: u64,
}

impl Counter {
    /// New counter at zero.
    pub fn new() -> Counter {
        Counter { value: 0 }
    }

    /// Add one.
    pub fn inc(&mut self) {
        self.value += 1;
    }

    /// Add `n`.
    pub fn add(&mut self, n: u64) {
        self.value += n;
    }

    /// Current value.
    pub fn get(&self) -> u64 {
        self.value
    }
}

/// Last-value gauge (f64).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Gauge {
    value: f64,
}

impl Gauge {
    /// New gauge at zero.
    pub fn new() -> Gauge {
        Gauge { value: 0.0 }
    }

    /// Set the gauge.
    pub fn set(&mut self, v: f64) {
        self.value = v;
    }

    /// Current value.
    pub fn get(&self) -> f64 {
        self.value
    }
}

/// Fixed-bucket log2 histogram of `u64` samples.
///
/// Bucket 0 counts the value 0; bucket `i >= 1` counts values in
/// `[2^(i-1), 2^i)`. The inclusive upper bound of bucket `i >= 1` is
/// `2^i - 1` (saturating at `u64::MAX` for the last bucket).
#[derive(Debug, Clone)]
pub struct Histogram {
    counts: [u64; HISTOGRAM_BUCKETS],
    total: u64,
    sum: u128,
    max: u64,
}

impl Default for Histogram {
    fn default() -> Histogram {
        Histogram::new()
    }
}

impl Histogram {
    /// New empty histogram.
    pub fn new() -> Histogram {
        Histogram {
            counts: [0; HISTOGRAM_BUCKETS],
            total: 0,
            sum: 0,
            max: 0,
        }
    }

    /// Bucket index for a value: 0 for 0, else `64 - leading_zeros`.
    pub fn bucket_of(value: u64) -> usize {
        if value == 0 {
            0
        } else {
            64 - value.leading_zeros() as usize
        }
    }

    /// Inclusive upper bound of a bucket.
    pub fn bucket_upper(index: usize) -> u64 {
        match index {
            0 => 0,
            64 => u64::MAX,
            i => (1u64 << i) - 1,
        }
    }

    /// Record one sample.
    pub fn record(&mut self, value: u64) {
        self.counts[Self::bucket_of(value)] += 1;
        self.total += 1;
        self.sum += value as u128;
        if value > self.max {
            self.max = value;
        }
    }

    /// Number of recorded samples.
    pub fn count(&self) -> u64 {
        self.total
    }

    /// Sum of recorded samples.
    pub fn sum(&self) -> u128 {
        self.sum
    }

    /// Largest recorded sample (0 when empty).
    pub fn max(&self) -> u64 {
        self.max
    }

    /// Quantile estimate for `q` in [0, 1]: the inclusive upper bound of the
    /// bucket holding the rank-`ceil(q * count)` sample (1-based). `None`
    /// when the histogram is empty or `q` is not a finite value in [0, 1].
    pub fn quantile(&self, q: f64) -> Option<u64> {
        if self.total == 0 || !q.is_finite() || !(0.0..=1.0).contains(&q) {
            return None;
        }
        let rank = ((q * self.total as f64).ceil() as u64).max(1);
        let mut cum = 0u64;
        for (i, &c) in self.counts.iter().enumerate() {
            cum += c;
            if cum >= rank {
                return Some(Self::bucket_upper(i));
            }
        }
        Some(Self::bucket_upper(HISTOGRAM_BUCKETS - 1))
    }

    /// p50 / p99 / p999 convenience triple (`None` when empty).
    pub fn p50_p99_p999(&self) -> Option<(u64, u64, u64)> {
        Some((
            self.quantile(0.50)?,
            self.quantile(0.99)?,
            self.quantile(0.999)?,
        ))
    }

    /// Raw bucket counts.
    pub fn buckets(&self) -> &[u64; HISTOGRAM_BUCKETS] {
        &self.counts
    }
}

/// Named metrics with deterministic iteration and Prometheus text output.
#[derive(Debug, Default)]
pub struct Registry {
    counters: BTreeMap<String, Counter>,
    gauges: BTreeMap<String, Gauge>,
    histograms: BTreeMap<String, Histogram>,
}

impl Registry {
    /// New empty registry.
    pub fn new() -> Registry {
        Registry::default()
    }

    /// Mutable named counter (created at zero on first use).
    pub fn counter(&mut self, name: &str) -> &mut Counter {
        self.counters.entry(name.to_string()).or_default()
    }

    /// Mutable named gauge (created at zero on first use).
    pub fn gauge(&mut self, name: &str) -> &mut Gauge {
        self.gauges.entry(name.to_string()).or_default()
    }

    /// Mutable named histogram (created empty on first use).
    pub fn histogram(&mut self, name: &str) -> &mut Histogram {
        self.histograms.entry(name.to_string()).or_default()
    }

    /// Read a counter value (0 when absent).
    pub fn counter_value(&self, name: &str) -> u64 {
        self.counters.get(name).map_or(0, Counter::get)
    }

    /// Read a gauge value (`None` when absent).
    pub fn gauge_value(&self, name: &str) -> Option<f64> {
        self.gauges.get(name).map(Gauge::get)
    }

    /// Read-only named histogram.
    pub fn histogram_ref(&self, name: &str) -> Option<&Histogram> {
        self.histograms.get(name)
    }

    /// Prometheus text exposition format (metrics sorted by name;
    /// histograms as cumulative `_bucket{le=...}` series plus `_sum`/`_count`).
    pub fn to_prometheus(&self) -> String {
        let mut out = String::new();
        for (name, c) in &self.counters {
            let _ = writeln!(out, "# TYPE {name} counter");
            let _ = writeln!(out, "{name} {}", c.get());
        }
        for (name, g) in &self.gauges {
            let _ = writeln!(out, "# TYPE {name} gauge");
            let _ = writeln!(out, "{name} {}", g.get());
        }
        for (name, h) in &self.histograms {
            let _ = writeln!(out, "# TYPE {name} histogram");
            let mut cum = 0u64;
            for (i, &c) in h.buckets().iter().enumerate() {
                if c == 0 {
                    continue;
                }
                cum += c;
                let _ = writeln!(
                    out,
                    "{name}_bucket{{le=\"{}\"}} {cum}",
                    Histogram::bucket_upper(i)
                );
            }
            let _ = writeln!(out, "{name}_bucket{{le=\"+Inf\"}} {}", h.count());
            let _ = writeln!(out, "{name}_sum {}", h.sum());
            let _ = writeln!(out, "{name}_count {}", h.count());
        }
        out
    }
}

/// Structured JSONL logger: one JSON object per line, keys sorted
/// (`serde_json` maps iterate in key order), byte-deterministic for a given
/// event sequence.
#[derive(Debug)]
pub struct JsonlLogger<W: Write> {
    sink: W,
    lines: u64,
}

impl<W: Write> JsonlLogger<W> {
    /// Wrap a sink (file, `Vec<u8>`, ...).
    pub fn new(sink: W) -> JsonlLogger<W> {
        JsonlLogger { sink, lines: 0 }
    }

    /// Write one structured event. `ts` is event time (ns); `fields` are
    /// extra key/value pairs (values already JSON).
    pub fn log(
        &mut self,
        ts: i64,
        level: &str,
        component: &str,
        msg: &str,
        fields: &[(&str, serde_json::Value)],
    ) -> std::io::Result<()> {
        let mut map = serde_json::Map::new();
        map.insert("ts".to_string(), serde_json::json!(ts));
        map.insert("level".to_string(), serde_json::json!(level));
        map.insert("component".to_string(), serde_json::json!(component));
        map.insert("msg".to_string(), serde_json::json!(msg));
        for (k, v) in fields {
            map.insert((*k).to_string(), v.clone());
        }
        let line = serde_json::Value::Object(map).to_string();
        self.sink.write_all(line.as_bytes())?;
        self.sink.write_all(b"\n")?;
        self.lines += 1;
        Ok(())
    }

    /// Number of lines written.
    pub fn lines(&self) -> u64 {
        self.lines
    }

    /// Consume the logger, returning the sink.
    pub fn into_sink(self) -> W {
        self.sink
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counter_and_gauge_basics() {
        let mut r = Registry::new();
        r.counter("orders_total").inc();
        r.counter("orders_total").add(4);
        r.gauge("position").set(-12.5);
        assert_eq!(r.counter_value("orders_total"), 5);
        assert_eq!(r.gauge_value("position"), Some(-12.5));
        assert_eq!(r.counter_value("missing"), 0);
    }

    #[test]
    fn histogram_bucket_boundaries() {
        assert_eq!(Histogram::bucket_of(0), 0);
        assert_eq!(Histogram::bucket_of(1), 1);
        assert_eq!(Histogram::bucket_of(2), 2);
        assert_eq!(Histogram::bucket_of(3), 2);
        assert_eq!(Histogram::bucket_of(4), 3);
        assert_eq!(Histogram::bucket_of(u64::MAX), 64);
        assert_eq!(Histogram::bucket_upper(0), 0);
        assert_eq!(Histogram::bucket_upper(2), 3);
        assert_eq!(Histogram::bucket_upper(64), u64::MAX);
    }

    #[test]
    fn empty_histogram_has_no_quantiles() {
        let h = Histogram::new();
        assert_eq!(h.quantile(0.5), None);
        assert_eq!(h.p50_p99_p999(), None);
    }
}
