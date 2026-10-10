//! pyo3 bindings for the IAP native feature engine (v1.11 E2).
//!
//! Replays a stream of normalized events (IAP1 or canonical JSONL, as bytes
//! or a file path) through [`features::FeatureEngine`] and returns the
//! emitted rows as numpy arrays. The row buffers are built in Rust and
//! handed to numpy without a copy (`into_pyarray_bound` moves the Vec).
//!
//! Parity with the Python reference is asserted by
//! `python/tests/test_features_rust_backend.py` on the golden vectors.
#![allow(clippy::useless_conversion)] // pyo3 0.22 macro expansion

use std::collections::BTreeMap;

use features::{FeatureEngine, FEATURE_COUNT, FEATURE_NAMES, NATIVE_COUNT};
use marketdata::codec::decode_jsonl;
use marketdata::{decode_iap1, read_iap1, read_jsonl, IapError, MarketEvent};
use numpy::ndarray::Array2;
use numpy::IntoPyArray;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Row-major replay output (owned buffers, moved into numpy).
#[derive(Default)]
struct Replay {
    instrument_id: Vec<u32>,
    timestamp: Vec<i64>,
    values: Vec<f64>,
    validity: Vec<bool>,
    events_processed: u64,
    vectors_emitted: u64,
    events_dropped: u64,
    ts_regressions_dropped: u64,
}

fn err(e: IapError) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn replay(
    events: &[MarketEvent],
    ticks: BTreeMap<u32, f64>,
    cadence_ns: i64,
) -> Result<Replay, IapError> {
    let mut eng = FeatureEngine::new(ticks, cadence_ns)?;
    let mut out = Replay::default();
    for ev in events {
        if let Some(v) = eng.apply(ev)? {
            out.instrument_id.push(v.instrument_id);
            out.timestamp.push(v.timestamp);
            out.values.extend_from_slice(&v.values);
            out.validity.extend_from_slice(&v.validity);
        }
    }
    out.events_processed = eng.events_processed;
    out.vectors_emitted = eng.vectors_emitted;
    out.events_dropped = eng.events_dropped;
    out.ts_regressions_dropped = eng.ts_regressions_dropped;
    Ok(out)
}

fn to_dict(py: Python<'_>, r: Replay) -> PyResult<Bound<'_, PyDict>> {
    let n = r.timestamp.len();
    let d = PyDict::new_bound(py);
    let values = Array2::from_shape_vec((n, FEATURE_COUNT), r.values)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let validity = Array2::from_shape_vec((n, FEATURE_COUNT), r.validity)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    d.set_item("instrument_id", r.instrument_id.into_pyarray_bound(py))?;
    d.set_item("timestamp", r.timestamp.into_pyarray_bound(py))?;
    d.set_item("values", values.into_pyarray_bound(py))?;
    d.set_item("validity", validity.into_pyarray_bound(py))?;
    d.set_item("events_processed", r.events_processed)?;
    d.set_item("vectors_emitted", r.vectors_emitted)?;
    d.set_item("events_dropped", r.events_dropped)?;
    d.set_item("ts_regressions_dropped", r.ts_regressions_dropped)?;
    Ok(d)
}

fn decode(data: &[u8], format: &str) -> Result<Vec<MarketEvent>, IapError> {
    match format {
        "iap1" => decode_iap1(data),
        "jsonl" => {
            let text = std::str::from_utf8(data)
                .map_err(|e| IapError::Codec(format!("JSONL is not UTF-8: {e}")))?;
            decode_jsonl(text)
        }
        other => Err(IapError::InvalidArgument(format!(
            "format must be 'iap1' or 'jsonl', got {other:?}"
        ))),
    }
}

/// Registry names of the 45 slots, in slot order.
#[pyfunction]
fn feature_names() -> Vec<&'static str> {
    FEATURE_NAMES.to_vec()
}

/// Number of pinned native features (the first slots).
#[pyfunction]
fn native_count() -> usize {
    NATIVE_COUNT
}

/// Replay encoded events (`format` = "iap1" | "jsonl").
#[pyfunction]
#[pyo3(signature = (data, ticks, cadence_ns=0, format="iap1"))]
fn replay_bytes<'py>(
    py: Python<'py>,
    data: &[u8],
    ticks: BTreeMap<u32, f64>,
    cadence_ns: i64,
    format: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let r = py
        .allow_threads(|| decode(data, format).and_then(|ev| replay(&ev, ticks, cadence_ns)))
        .map_err(err)?;
    to_dict(py, r)
}

/// Replay a normalized file (`.iap1`, else canonical JSONL).
#[pyfunction]
#[pyo3(signature = (path, ticks, cadence_ns=0))]
fn replay_file<'py>(
    py: Python<'py>,
    path: String,
    ticks: BTreeMap<u32, f64>,
    cadence_ns: i64,
) -> PyResult<Bound<'py, PyDict>> {
    let r = py
        .allow_threads(|| {
            let events = if path.ends_with(".iap1") {
                read_iap1(&path)?
            } else {
                read_jsonl(&path)?
            };
            replay(&events, ticks, cadence_ns)
        })
        .map_err(err)?;
    to_dict(py, r)
}

#[pymodule]
fn iap_features_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(feature_names, m)?)?;
    m.add_function(wrap_pyfunction!(native_count, m)?)?;
    m.add_function(wrap_pyfunction!(replay_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(replay_file, m)?)?;
    m.add("FEATURE_COUNT", FEATURE_COUNT)?;
    Ok(())
}
