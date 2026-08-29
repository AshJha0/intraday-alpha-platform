//! Event-time forward mid labels + Pearson IC (validation support for the
//! alpha golden group; semantics pinned by `iap/labels/labels.py` and
//! `iap/validation/metrics.py`).
//!
//! `mid_label(t, h) = m(t+h)/m(t) - 1` where `m(x)` is the mid prevailing
//! at-or-before `x` (one sample per `book_ok` refresh). A label is valid
//! only when the stream was observed through `t + h` and both endpoint mids
//! exist — never extrapolated past the session end.

/// Per-instrument event-time mid series (one sample per book update).
#[derive(Debug, Default, Clone)]
pub struct MidSeries {
    ts: Vec<i64>,
    mid: Vec<f64>,
}

impl MidSeries {
    /// New empty series.
    pub fn new() -> MidSeries {
        MidSeries::default()
    }

    /// Append one sample (timestamps non-decreasing).
    pub fn append(&mut self, ts: i64, mid: f64) {
        debug_assert!(self.ts.last().map_or(true, |&last| ts >= last));
        self.ts.push(ts);
        self.mid.push(mid);
    }

    /// Number of samples.
    pub fn len(&self) -> usize {
        self.ts.len()
    }

    /// True when empty.
    pub fn is_empty(&self) -> bool {
        self.ts.is_empty()
    }

    fn at_or_before(&self, t: i64) -> Option<f64> {
        let i = self.ts.partition_point(|&x| x <= t);
        if i > 0 {
            Some(self.mid[i - 1])
        } else {
            None
        }
    }
}

/// Forward mid labels for every anchor at one horizon; NaN where invalid.
pub fn mid_labels(
    anchors_ts: &[i64],
    series: &MidSeries,
    last_event_ts: i64,
    horizon_ns: i64,
) -> Vec<f64> {
    anchors_ts
        .iter()
        .map(|&t| {
            let target = t + horizon_ns;
            if last_event_ts < target {
                return f64::NAN;
            }
            match (series.at_or_before(t), series.at_or_before(target)) {
                (Some(m0), Some(m1)) if m0 > 0.0 => m1 / m0 - 1.0,
                _ => f64::NAN,
            }
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
