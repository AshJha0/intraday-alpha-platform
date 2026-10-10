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
VECTORS = (
    "events_eq_mbo.jsonl",
    "events_fx_quote.jsonl",
    "events_eq_anomalies.jsonl",
    "events_fx_anomalies.jsonl",
)


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


@pytest.fixture(scope="module")
def python_frames(contexts):
    return {
        (v, c): compute_native(read_jsonl(GOLDEN_DIR / v), contexts, c, engine="python")
        for v in VECTORS
        for c in (0, 100_000_000)
    }


def _assert_parity(py, rs, label):
    assert rs.values.shape == py.values.shape, label
    assert np.array_equal(rs.instrument_id, py.instrument_id), label
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
    over = np.abs(b - a) > TOL + TOL * np.abs(a)
    if over.any():
        report = []
        for k in np.flatnonzero(over.any(axis=0)):
            rows = np.flatnonzero(over[:, k])
            r = rows[0]
            report.append(
                f"{NATIVE_NAMES[k]}: {len(rows)} rows, first row {r} "
                f"py={a[r, k]!r} rs={b[r, k]!r}, max |d|={np.abs(b - a)[rows, k].max():.3g}"
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


@pytest.mark.parametrize("cadence_ns", [0, 100_000_000])
@pytest.mark.parametrize("vector", VECTORS)
def test_rust_backend_parity_on_golden_vectors(ext, contexts, python_frames, vector, cadence_ns):
    """Every emitted row, every native slot: values to 1e-9, validity exact."""
    py = python_frames[(vector, cadence_ns)]
    rs = compute_native(read_jsonl(GOLDEN_DIR / vector), contexts, cadence_ns, engine="rust")
    assert rs.engine == "rust" and rs.values.dtype == np.float64 and rs.validity.dtype == bool
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
            assert (np.abs(a[ok] - b[ok]) <= TOL + TOL * np.abs(a[ok])).all(), col
        elif a.dtype.kind == "f":
            assert np.array_equal(a, b, equal_nan=True), col
        elif col != "validity_bits":  # bits differ only in native slots (checked above)
            assert list(a) == list(b), col
