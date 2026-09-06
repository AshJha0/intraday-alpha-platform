//! Event-time forward mid labels + Pearson IC (validation support for the
//! alpha golden group; semantics pinned by `iap/labels/labels.py` and
//! `iap/validation/metrics.py`).
//!
//! `mid_label(t, h) = m(t+h)/m(t) - 1` where `m(x)` is the mid prevailing
//! at-or-before `x` — ONE sample per book REFRESH, each carrying a
//! `tradable` flag (two-sided merged book, no stale venue, no HALT/AUCTION).
//!
//! A label is valid only when (API_FEATURES.md §6, pinned):
//! 1. the stream was observed through `t + h`;
//! 2. the anchor sample exists, is tradable and has a positive mid;
//! 3. the forward sample exists, is tradable and is FRESH
//!    (`t + h - ts_forward <= max_age_ns`);
//! 4. every sample in `(t, t + h]` is tradable (no halt / auction / stale
//!    gap inside the horizon).
//!
//! `max_age_ns` is `max(5 s, 2 x median gap between DISTINCT sample
//! timestamps)` — see [`max_sample_age`].

/// Freshness floor for the prevailing-mid age (pinned).
pub const LABEL_MAX_AGE_FLOOR_NS: i64 = 5_000_000_000;

/// Per-instrument event-time mid series (one sample per book REFRESH).
#[derive(Debug, Default, Clone)]
pub struct MidSeries {
    ts: Vec<i64>,
    mid: Vec<f64>,
    tradable: Vec<bool>,
    /// prefix count of NON-tradable samples (`bad[i]` covers `ts[..i]`)
    bad: Vec<u32>,
}

impl MidSeries {
    /// New empty series.
    pub fn new() -> MidSeries {
        MidSeries::default()
    }

    /// Append one TRADABLE sample (timestamps non-decreasing).
    pub fn append(&mut self, ts: i64, mid: f64) {
        self.append_sample(ts, mid, true);
    }

    /// Append one sample; `tradable == false` marks a blackout refresh
    /// (one-sided book, stale venue, HALT / AUCTION).
    pub fn append_sample(&mut self, ts: i64, mid: f64, tradable: bool) {
        debug_assert!(self.ts.last().map_or(true, |&last| ts >= last));
        let prev = self.bad.last().copied().unwrap_or(0);
        self.ts.push(ts);
        self.mid.push(mid);
        self.tradable.push(tradable);
        self.bad.push(prev + u32::from(!tradable));
    }

    /// Pinned freshness bound: `max(5 s, 2 x median gap between DISTINCT
    /// sample timestamps)` — a fixed floor for dense books, scaled up for a
    /// sparse FX stream where a 15 s quote gap is the normal cadence.
    pub fn max_sample_age(&self) -> i64 {
        let mut distinct: Vec<i64> = Vec::with_capacity(self.ts.len());
        for &t in &self.ts {
            if distinct.last() != Some(&t) {
                distinct.push(t);
            }
        }
        if distinct.len() < 2 {
            return LABEL_MAX_AGE_FLOOR_NS;
        }
        let mut gaps: Vec<i64> = distinct.windows(2).map(|w| w[1] - w[0]).collect();
        gaps.sort_unstable();
        let m = gaps.len();
        let median = if m % 2 == 1 {
            gaps[m / 2]
        } else {
            (gaps[m / 2 - 1] + gaps[m / 2]) / 2
        };
        LABEL_MAX_AGE_FLOOR_NS.max(2 * median)
    }

    /// Index of the latest sample at-or-before `t`.
    fn index_at_or_before(&self, t: i64) -> Option<usize> {
        let i = self.ts.partition_point(|&x| x <= t);
        if i > 0 {
            Some(i - 1)
        } else {
            None
        }
    }

    /// Number of samples.
    pub fn len(&self) -> usize {
        self.ts.len()
    }

    /// True when empty.
    pub fn is_empty(&self) -> bool {
        self.ts.is_empty()
    }

}

/// Forward mid labels for every anchor at one horizon; NaN where invalid
/// (see the module docstring for the pinned validity rules).
pub fn mid_labels(
    anchors_ts: &[i64],
    series: &MidSeries,
    last_event_ts: i64,
    horizon_ns: i64,
) -> Vec<f64> {
    let max_age = series.max_sample_age();
    mid_labels_with_age(anchors_ts, series, last_event_ts, horizon_ns, max_age)
}

/// [`mid_labels`] with an explicit freshness bound.
pub fn mid_labels_with_age(
    anchors_ts: &[i64],
    series: &MidSeries,
    last_event_ts: i64,
    horizon_ns: i64,
    max_age_ns: i64,
) -> Vec<f64> {
    anchors_ts
        .iter()
        .map(|&t| {
            let target = t + horizon_ns;
            if last_event_ts < target {
                return f64::NAN;
            }
            let (Some(b), Some(k)) = (
                series.index_at_or_before(t),
                series.index_at_or_before(target),
            ) else {
                return f64::NAN;
            };
            if !series.tradable[b] || !(series.mid[b] > 0.0) {
                return f64::NAN;
            }
            if !series.tradable[k] || !(series.mid[k] > 0.0) {
                return f64::NAN;
            }
            if target - series.ts[k] > max_age_ns {
                return f64::NAN;
            }
            if k >= b && series.bad[k] - series.bad[b] > 0 {
                return f64::NAN; // a blackout sample inside (t, t + h]
            }
            series.mid[k] / series.mid[b] - 1.0
        })
        .collect()
}

/// Pearson information coefficient over pairwise-finite entries; NaN with
/// fewer than `min_obs` pairs or a degenerate (std <= 1e-12) side. Mirrors
/// `iap.validation.metrics.ic` (default `min_obs = 32`).
pub fn pearson_ic(scores: &[f64], labels: &[f64], min_obs: usize) -> f64 {
    assert_eq!(scores.len(), labels.len(), "score/label length mismatch");
    let pairs: Vec<(f64, f64)> = scores
        .iter()
        .zip(labels.iter())
        .filter(|(x, y)| x.is_finite() && y.is_finite())
        .map(|(&x, &y)| (x, y))
        .collect();
    let n = pairs.len();
    if n < min_obs {
        return f64::NAN;
    }
    let nf = n as f64;
    let mx = pairs.iter().map(|p| p.0).sum::<f64>() / nf;
    let my = pairs.iter().map(|p| p.1).sum::<f64>() / nf;
    let mut sxx = 0.0;
    let mut syy = 0.0;
    let mut sxy = 0.0;
    for &(x, y) in &pairs {
        let (dx, dy) = (x - mx, y - my);
        sxx += dx * dx;
        syy += dy * dy;
        sxy += dx * dy;
    }
    // population std guard, as in the reference
    if (sxx / nf).sqrt() <= 1e-12 || (syy / nf).sqrt() <= 1e-12 {
        return f64::NAN;
    }
    sxy / (sxx.sqrt() * syy.sqrt())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_use_at_or_before_and_session_end() {
        let mut s = MidSeries::new();
        s.append(0, 100.0);
        s.append(1_000, 101.0);
        s.append(3_000, 99.0);
        // anchor 500 with horizon 1000: m(500)=100 (at-or-before), m(1500)=101
        let lab = mid_labels(&[500], &s, 3_000, 1_000);
        assert!((lab[0] - (101.0 / 100.0 - 1.0)).abs() < 1e-15);
        // anchor 2500 with horizon 1000: target 3500 beyond last event -> NaN
        let lab = mid_labels(&[2_500], &s, 3_000, 1_000);
        assert!(lab[0].is_nan());
        // anchor before the first sample -> NaN
        let lab = mid_labels(&[-10], &s, 3_000, 5);
        assert!(lab[0].is_nan());
    }

    #[test]
    fn ic_matches_hand_computation_and_guards() {
        let x: Vec<f64> = (0..40).map(|i| i as f64).collect();
        let y: Vec<f64> = x.iter().map(|v| 2.0 * v + 1.0).collect();
        assert!((pearson_ic(&x, &y, 32) - 1.0).abs() < 1e-12);
        let yneg: Vec<f64> = x.iter().map(|v| -v).collect();
        assert!((pearson_ic(&x, &yneg, 32) + 1.0).abs() < 1e-12);
        // too few pairs
        assert!(pearson_ic(&x[..10], &y[..10], 32).is_nan());
        // degenerate side
        let flat = vec![1.0; 40];
        assert!(pearson_ic(&x, &flat, 32).is_nan());
        // NaNs dropped pairwise
        let mut y2 = y.clone();
        y2[3] = f64::NAN;
        assert!((pearson_ic(&x, &y2, 32) - 1.0).abs() < 1e-12);
    }
}
