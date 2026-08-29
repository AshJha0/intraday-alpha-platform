//! Telemetry correctness: histogram quantiles vs an exact sorted
//! reference, Prometheus exposition shape, JSONL logger determinism.

use marketdata_free::sorted_reference_quantile;
use telemetry::{Histogram, JsonlLogger, Registry};

/// Exact order-statistic quantile on a sorted copy (the reference the
/// histogram is compared against): rank = max(1, ceil(q * n)), 1-based.
mod marketdata_free {
    pub fn sorted_reference_quantile(values: &[u64], q: f64) -> u64 {
        let mut v = values.to_vec();
        v.sort_unstable();
        let rank = ((q * v.len() as f64).ceil() as usize).max(1);
        v[rank - 1]
    }
}

/// Deterministic pseudo-random u64s (SplitMix64 constants).
fn pseudo_random(n: usize, seed: u64) -> Vec<u64> {
    let mut state = seed;
    (0..n)
        .map(|_| {
            state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = state;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        })
        .collect()
}

/// The histogram's quantile must bracket the exact order statistic within
/// its own bucket: `upper/2 < exact <= upper` (log2 buckets), i.e. the
/// reported value is never below the exact quantile and never more than
/// one power of two above it.
fn assert_quantile_brackets(values: &[u64], q: f64) {
    let mut h = Histogram::new();
    for &v in values {
        h.record(v);
    }
    let got = h.quantile(q).expect("non-empty");
    let exact = sorted_reference_quantile(values, q);
    assert!(
        got >= exact,
        "q={q}: reported {got} below exact order statistic {exact}"
    );
    assert_eq!(
        Histogram::bucket_of(got.min(u64::MAX - 1)),
        Histogram::bucket_of(exact),
        "q={q}: exact {exact} not in the reported bucket (upper {got})"
    );
}

#[test]
fn p50_p99_p999_vs_sorted_reference_uniform() {
    // latency-like values spanning many buckets
    let values: Vec<u64> = pseudo_random(10_000, 42)
        .into_iter()
        .map(|v| v % 5_000_000) // 0 .. 5ms in ns
        .collect();
    for q in [0.50, 0.99, 0.999] {
        assert_quantile_brackets(&values, q);
    }
}

#[test]
fn p50_p99_p999_vs_sorted_reference_heavy_tail() {
    // bimodal: fast path ~1us, slow tail ~1s
    let mut values: Vec<u64> = pseudo_random(9_900, 7).iter().map(|v| 800 + v % 400).collect();
    values.extend(pseudo_random(100, 8).iter().map(|v| 900_000_000 + v % 200_000_000));
    for q in [0.50, 0.99, 0.999] {
        assert_quantile_brackets(&values, q);
    }
    // the p999 must land in the slow tail, p50 in the fast path
    let mut h = Histogram::new();
    for &v in &values {
        h.record(v);
    }
    let (p50, p99, p999) = h.p50_p99_p999().unwrap();
    assert!(p50 < 4096);
    assert!(p999 >= 900_000_000);
    assert!(p50 <= p99 && p99 <= p999);
}

#[test]
fn exact_small_cases() {
    let mut h = Histogram::new();
    h.record(0);
    assert_eq!(h.quantile(0.5), Some(0));
    let mut h = Histogram::new();
    for v in [1u64, 2, 3, 4] {
        h.record(v);
    }
    // rank(0.5) = 2 -> value 2 lives in bucket [2,4) upper 3
    assert_eq!(h.quantile(0.5), Some(3));
    // rank(1.0) = 4 -> bucket [4,8) upper 7
    assert_eq!(h.quantile(1.0), Some(7));
    assert_eq!(h.quantile(f64::NAN), None);
    assert_eq!(h.quantile(1.5), None);
    assert_eq!(h.count(), 4);
    assert_eq!(h.sum(), 10);
    assert_eq!(h.max(), 4);
}

#[test]
fn prometheus_exposition_shape() {
    let mut r = Registry::new();
    r.counter("orders_total").add(3);
    r.gauge("position_qty").set(-42.5);
    let h = r.histogram("latency_ns");
    h.record(1);
    h.record(3);
    h.record(1_000);
    let text = r.to_prometheus();
    assert!(text.contains("# TYPE orders_total counter\norders_total 3\n"));
    assert!(text.contains("# TYPE position_qty gauge\nposition_qty -42.5\n"));
    assert!(text.contains("# TYPE latency_ns histogram\n"));
    assert!(text.contains("latency_ns_bucket{le=\"1\"} 1\n"));
    assert!(text.contains("latency_ns_bucket{le=\"3\"} 2\n"));
    assert!(text.contains("latency_ns_bucket{le=\"1023\"} 3\n"));
    assert!(text.contains("latency_ns_bucket{le=\"+Inf\"} 3\n"));
    assert!(text.contains("latency_ns_sum 1004\n"));
    assert!(text.contains("latency_ns_count 3\n"));
    // cumulative buckets are non-decreasing in the exposition order
    let counts: Vec<u64> = text
        .lines()
        .filter(|l| l.starts_with("latency_ns_bucket"))
        .map(|l| l.rsplit(' ').next().unwrap().parse().unwrap())
        .collect();
    assert!(counts.windows(2).all(|w| w[0] <= w[1]));
}

#[test]
fn jsonl_logger_is_deterministic_and_structured() {
    let write = || {
        let mut log = JsonlLogger::new(Vec::new());
        log.log(100, "INFO", "risk", "engine armed", &[]).unwrap();
        log.log(
            200,
            "WARN",
            "venue",
            "reject",
            &[
                ("order_id", serde_json::json!(7)),
                ("reason", serde_json::json!("fat finger")),
            ],
        )
        .unwrap();
        assert_eq!(log.lines(), 2);
        String::from_utf8(log.into_sink()).unwrap()
    };
    let a = write();
    assert_eq!(a, write(), "byte-identical across runs");
    let lines: Vec<&str> = a.lines().collect();
    assert_eq!(lines.len(), 2);
    for line in &lines {
        let v: serde_json::Value = serde_json::from_str(line).unwrap();
        assert!(v["ts"].is_i64());
        assert!(v["level"].is_string());
        assert!(v["component"].is_string());
        assert!(v["msg"].is_string());
    }
    let v: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
    assert_eq!(v["order_id"], 7);
}
