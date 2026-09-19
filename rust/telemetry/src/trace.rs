//! Decision-trace exposition (spec §25 / `store_trace.md` §4).
//!
//! Telemetry owns the exposition and trace formats: a trace is persisted as
//! one canonical JSON line ([`JsonlTraceSink`]) and a run is fingerprinted by
//! the stream digest ([`TraceDigest`], SHA-256 over `line + "\n"` per trace).
//! Both types live in the `contracts` crate and are re-exported here so a
//! component only depends on `telemetry`.
//!
//! Metric convention: every successfully emitted trace increments the
//! counter [`TRACE_RECORDS_TOTAL`] (`trace_records_total`, one per JSONL
//! line, monotone per process); a rejected (invalid) trace writes nothing and
//! counts nothing. [`emit_counted`] applies the convention in one call.

use std::io::Write;

pub use contracts::trace::{DecisionTrace, JsonlTraceSink, TraceDigest};
use contracts::IapError;

use crate::Registry;

/// Counter name: trace records written (one per canonical JSONL line).
pub const TRACE_RECORDS_TOTAL: &str = "trace_records_total";

/// Emit `trace` through `sink` and, on success, increment
/// [`TRACE_RECORDS_TOTAL`] in `registry`. An invalid trace is an error and
/// leaves both the sink and the counter untouched.
pub fn emit_counted<W: Write>(
    registry: &mut Registry,
    sink: &mut JsonlTraceSink<W>,
    trace: &DecisionTrace,
) -> Result<(), IapError> {
    sink.emit(trace)?;
    registry.counter(TRACE_RECORDS_TOTAL).inc();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use contracts::{make_trace_id, TraceStages};

    fn trace(sequence: u64) -> DecisionTrace {
        DecisionTrace {
            trace_id: make_trace_id("s1", 7, 10, sequence),
            session_id: "s1".to_string(),
            instrument_id: 7,
            event_ts: 10,
            sequence,
            data_version: "0".repeat(64),
            feature_version: "1".repeat(64),
            model_version: "2".repeat(64),
            config_version: "3".repeat(64),
            stages: TraceStages::empty(),
        }
    }

    #[test]
    fn counter_follows_successful_emits_only() {
        let mut registry = Registry::new();
        let mut sink = JsonlTraceSink::new(Vec::new());
        emit_counted(&mut registry, &mut sink, &trace(1)).expect("valid");
        emit_counted(&mut registry, &mut sink, &trace(2)).expect("valid");
        let mut bad = trace(3);
        bad.trace_id = "not-hex".to_string();
        assert!(emit_counted(&mut registry, &mut sink, &bad).is_err());
        assert_eq!(registry.counter_value(TRACE_RECORDS_TOTAL), 2);
        assert_eq!(sink.count(), 2);
        let text = String::from_utf8(sink.into_inner()).expect("ascii");
        assert_eq!(text.lines().count(), 2);
        let reread = TraceDigest::of_jsonl(&text).expect("re-read");
        let mut expected = TraceDigest::new();
        expected.update(&trace(1)).expect("valid");
        expected.update(&trace(2)).expect("valid");
        assert_eq!(reread.hexdigest(), expected.hexdigest());
        let exposition = registry.to_prometheus();
        assert!(exposition.contains("# TYPE trace_records_total counter\ntrace_records_total 2\n"));
    }
}
