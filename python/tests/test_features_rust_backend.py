"""Rust native-feature backend (iap_features_rs, v1.11 E2) vs the Python reference.

The parity tests need the pyo3 extension (built by the ``rust-pyo3`` CI job
with maturin); without it they skip, so a local pytest with no Rust
toolchain stays green.  The backend-independent tests always run.
"""

from __future__ import annotations

import json
import warnings

import numpy as np
import pytest
from conftest import GOLDEN_DIR, REPO_ROOT
from iap.core.codec import encode_jsonl, read_jsonl
from iap.features import native
from iap.features.context import build_contexts
from iap.features.native import (
    NATIVE_COUNT,
    NATIVE_NAMES,
    compute_native,
    compute_native_file,
    resolve_engine,
)
from iap.features.registry import feature_index

TOL = 1e-9
#: The pinned golden vectors (expected_features.json): every row must match.
PRIMARY = ("events_eq_mbo.jsonl", "events_fx_quote.jsonl")
#: The anomaly vectors (expected_features_anomalies.json): the golden
#: checkpoints and every row must match too.
ANOMALY = ("events_eq_anomalies.jsonl", "events_fx_anomalies.jsonl")
VECTORS = PRIMARY + ANOMALY
CADENCES = (0, 100_000_000)
_VRR = NATIVE_NAMES.index("vol_regime_ratio_v1")
_RV1 = NATIVE_NAMES.index("rvol_w1m_v1")
_RV5 = NATIVE_NAMES.index("rvol_w5m_v1")
#: vol_regime_ratio = rvol_w1m / (rvol_w5m + EPS). When every 1m return is 0
#: the rolling sum of squares leaves float residue (~1e-12) in one engine and
#: an exact 0 in the other -- inside rvol's own 1e-9 tolerance, but divided by
#: a small rvol_w5m it becomes ~1e-8 in the ratio. On rows where rvol_w1m is
#: within 1e-9 of 0 the ratio is compared at the input tolerance propagated
#: through the division, 1e-9 / (rvol_w5m + EPS); all other rows stay 1e-9.
RESIDUE_RV = 1e-9


def _tolerance(py_values: np.ndarray) -> np.ndarray:
    """Per-cell tolerance (abs 1e-9 + rel 1e-9; vol_regime_ratio residue rule)."""
    tol = TOL + TOL * np.abs(py_values)
    rv1 = np.abs(np.nan_to_num(py_values[:, _RV1]))
    rv5 = np.abs(np.nan_to_num(py_values[:, _RV5]))
    residue = rv1 <= RESIDUE_RV
    tol[residue, _VRR] += RESIDUE_RV / (rv5[residue] + 1e-12)
    return tol


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


@pytest.fixture(scope="module")
def python_frames(contexts):
    return {
        (v, c): compute_native(read_jsonl(GOLDEN_DIR / v), contexts, c, engine="python")
        for v in VECTORS
        for c in CADENCES
    }


def _assert_parity(py, rs, label):
    assert rs.values.shape == py.values.shape, label
    assert np.array_equal(rs.instrument_id, py.instrument_id), label
    assert np.array_equal(rs.event_index, py.event_index), label
    assert np.array_equal(rs.timestamp, py.timestamp), label
    assert rs.events_processed == py.events_processed, label
    bad = np.argwhere(rs.validity != py.validity)
    assert bad.size == 0, (
        f"{label}: validity differs at {len(bad)} cells, first "
        f"row {bad[0][0]} {NATIVE_NAMES[bad[0][1]]}"
    )
    v = py.validity
    assert np.isnan(rs.values[~v]).all(), f"{label}: invalid slot not NaN"
    a = np.where(v, py.values, 0.0)
    b = np.where(v, rs.values, 0.0)
    over = np.abs(b - a) > _tolerance(a)
    if over.any():
        report = []
        for k in np.flatnonzero(over.any(axis=0)):
            rows = np.flatnonzero(over[:, k])
            r = rows[0]
            report.append(
                f"{NATIVE_NAMES[k]}: {len(rows)} rows, first row {r} (event "
                f"{int(py.event_index[r]) + 1}) py={a[r, k]!r} rs={b[r, k]!r}, "
                f"max |d|={np.abs(b - a)[rows, k].max():.3g}"
            )
        sep = "\n  "
        pytest.fail(f"{label}: values differ{sep}" + sep.join(report))


# ---------------------------------------------------- backend-independent


def test_native_names_are_registry_features():
    idx = feature_index()
    assert len(NATIVE_NAMES) == 45 and NATIVE_COUNT == 40
    assert len(set(NATIVE_NAMES)) == 45
    missing = [n for n in NATIVE_NAMES if n not in idx]
    assert not missing


def test_python_backend_matches_golden_checkpoints(python_frames):
    golden = json.loads((GOLDEN_DIR / "expected_features.json").read_text())
    for side in ("eq", "fx"):
        doc = golden[side]
        frame = python_frames[(doc["vector"], 0)]
        for cp, exp in doc["checkpoints"].items():
            row = int(cp) - 1
            assert frame.timestamp[row] == exp["timestamp"]
            for k, name in enumerate(NATIVE_NAMES):
                if name not in exp["features"]:
                    continue
                e = exp["features"][name]
                assert bool(frame.validity[row, k]) == e["valid"], (side, cp, name)
                if e["valid"]:
                    assert abs(frame.values[row, k] - e["value"]) <= TOL + TOL * abs(e["value"])


def test_validity_bits_pack_little_endian(python_frames):
    frame = python_frames[("events_eq_mbo.jsonl", 0)]
    bits = frame.validity_bits()
    assert bits.shape == (len(frame.timestamp), 6)
    back = np.unpackbits(bits, axis=1, bitorder="little")[:, :45].astype(bool)
    assert np.array_equal(back, frame.validity)


def test_rust_request_falls_back_without_extension(monkeypatch):
    monkeypatch.setattr(native, "rust_extension", lambda: None)
    with pytest.warns(RuntimeWarning, match="falling back"):
        assert resolve_engine("rust") == "python"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_engine("python") == "python"
    with pytest.raises(ValueError):
        resolve_engine("java")
    with pytest.raises(RuntimeError):
        compute_native([], {}, engine="rust")


# ------------------------------------------------------ needs the extension


@pytest.fixture(scope="module")
def ext():
    return pytest.importorskip("iap_features_rs")


def test_extension_slot_order_matches(ext):
    assert tuple(ext.feature_names()) == NATIVE_NAMES
    assert ext.native_count() == NATIVE_COUNT


@pytest.mark.parametrize("cadence_ns", CADENCES)
@pytest.mark.parametrize("vector", PRIMARY)
def test_rust_backend_parity_on_golden_vectors(ext, contexts, python_frames, vector, cadence_ns):
    """Every emitted row, every native slot: values to 1e-9, validity exact."""
    py = python_frames[(vector, cadence_ns)]
    rs = compute_native(read_jsonl(GOLDEN_DIR / vector), contexts, cadence_ns, engine="rust")
    assert rs.engine == "rust" and rs.values.dtype == np.float64 and rs.validity.dtype == bool
    _assert_parity(py, rs, f"{vector}@{cadence_ns}")


@pytest.mark.parametrize("side", ["eq", "fx"])
def test_rust_backend_anomaly_golden_checkpoints(ext, contexts, python_frames, side):
    """Anomaly vectors: the pinned checkpoints match Python and the golden,
    with validity identical on every row."""
    golden = json.loads((GOLDEN_DIR / "expected_features_anomalies.json").read_text())
    doc = golden[side]
    py = python_frames[(doc["vector"], 0)]
    rs = compute_native(read_jsonl(GOLDEN_DIR / doc["vector"]), contexts, 0, engine="rust")
    assert np.array_equal(rs.event_index, py.event_index)
    assert np.array_equal(rs.validity, py.validity)
    for key, exp in doc["checkpoints"].items():
        (row,) = np.flatnonzero(rs.event_index == int(key) - 1)
        assert rs.timestamp[row] == exp["timestamp"], key
        for k, name in enumerate(NATIVE_NAMES):
            e = exp["features"][name]
            assert bool(rs.validity[row, k]) == e["valid"], (side, key, name)
            if e["valid"]:
                got, want = rs.values[row, k], e["value"]
                tol = TOL + TOL * abs(want)
                assert abs(got - want) <= tol, (side, key, name, got, want)


@pytest.mark.parametrize("vector", ANOMALY)
def test_rust_backend_parity_on_anomaly_vectors(ext, contexts, python_frames, vector):
    """Every row, including those inside SNAPSHOT recovery bursts (fixed in
    v1.11.0: the Rust engine used to skip the level update on a staleness
    refresh)."""
    for cadence_ns in CADENCES:
        py = python_frames[(vector, cadence_ns)]
        rs = compute_native(read_jsonl(GOLDEN_DIR / vector), contexts, cadence_ns, engine="rust")
        _assert_parity(py, rs, f"{vector}@{cadence_ns}")


def test_rust_file_and_jsonl_bytes_entry_points(ext, contexts, python_frames):
    vector = "events_eq_mbo.jsonl"
    py = python_frames[(vector, 0)]
    _assert_parity(py, compute_native_file(GOLDEN_DIR / vector, contexts, 0, "rust"), "file")
    ticks = {i: c.tick_size for i, c in contexts.items()}
    data = encode_jsonl(read_jsonl(GOLDEN_DIR / vector))
    d = ext.replay_bytes(data, ticks, 0, "jsonl")
    assert np.array_equal(d["timestamp"], py.timestamp)
    with pytest.raises(ValueError):
        ext.replay_bytes(data, ticks, 0, "csv")
    with pytest.raises(ValueError):
        ext.replay_bytes(data, {}, 0, "jsonl")  # no tick for the instrument


def test_pipeline_engine_rust_swaps_only_native_columns(ext, tmp_path):
    """`python -m iap.features --engine rust` == python on the 160 other
    columns (bit-identical) and within 1e-9 on the 45 native ones."""
    import pyarrow.parquet as pq
    from iap.features.__main__ import main

    data = tmp_path / "in"
    data.mkdir()
    (data / "eq_golden.normalized.jsonl").write_bytes(
        (GOLDEN_DIR / "events_eq_mbo.jsonl").read_bytes()
    )
    tables = {}
    for eng in ("python", "rust"):
        out = tmp_path / eng
        rc = main(
            [
                "--data-dir",
                str(data),
                "--out-dir",
                str(out),
                "--registry-out",
                str(tmp_path / f"reg_{eng}.json"),
                "--cadence-ms",
                "0",
                "--engine",
                eng,
            ]
        )
        assert rc == 0
        tables[eng] = pq.read_table(out / "features_1.parquet").to_pandas()
    summary = json.loads((tmp_path / "rust" / "features_summary.json").read_text())
    assert summary["native_engine"] == "rust"
    py, rs = tables["python"], tables["rust"]
    assert list(py.columns) == list(rs.columns)
    native_set = set(NATIVE_NAMES)
    for col in py.columns:
        a, b = py[col].to_numpy(), rs[col].to_numpy()
        if col in native_set:
            ok = ~np.isnan(a)
            assert np.array_equal(ok, ~np.isnan(b)), col
            tol = TOL + TOL * np.abs(a)
            if col == "vol_regime_ratio_v1":
                rv1 = np.abs(np.nan_to_num(py["rvol_w1m_v1"].to_numpy()))
                rv5 = np.abs(np.nan_to_num(py["rvol_w5m_v1"].to_numpy()))
                residue = rv1 <= RESIDUE_RV
                tol[residue] += RESIDUE_RV / (rv5[residue] + 1e-12)
            assert (np.abs(a[ok] - b[ok]) <= tol[ok]).all(), col
        elif a.dtype.kind == "f":
            assert np.array_equal(a, b, equal_nan=True), col
        elif col != "validity_bits":  # bits differ only in native slots (checked above)
            assert list(a) == list(b), col
