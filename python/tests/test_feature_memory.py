"""Feature-build memory layout (v1.13, plan item E5).

The build streams events from the IAP1 file, keeps timestamps / validity /
the label mid series in typed arrays and spools the float64 row values to
disk until an instrument's row group is written.  None of that may change a
byte of output: the old-vs-new byte identity was verified on every option
combination when E5 landed (docs/REAL_DATA.md §10); these tests pin the
pieces (streamed decode == list decode, compact series == list series for
both freshness rules, the deferred Rust overlay) and a generous peak-memory
bound.
"""

from __future__ import annotations

import json
import random
import struct
import tracemalloc

import numpy as np
import pyarrow.parquet as pq
import pytest
from conftest import REPO_ROOT
from iap.core.codec import iter_iap1, read_iap1, write_iap1
from iap.features import __main__ as pipeline
from iap.labels.labels import (
    CompactMidSeries,
    MidSeries,
    compute_labels,
    max_sample_age,
)
from iap.marketdata.generator import MarketDataGenerator
from iap.marketdata.normalize import normalize_run

CFG = {
    "seed": 777,
    "sessions": 2,
    "equities": {"slots_per_stream": 150},
    "fx": {"slots_per_pair": 100},
}


@pytest.fixture(scope="module")
def normalized(tmp_path_factory):
    from iap.reference.refdata import ReferenceData

    root = tmp_path_factory.mktemp("memory")
    raw = root / "raw"
    MarketDataGenerator(ReferenceData.load(REPO_ROOT / "configs"), dict(CFG)).generate_run(raw)
    for path in sorted(raw.glob("*.jsonl")):
        if path.stat().st_size == 0:
            path.unlink()
    out = root / "normalized"
    normalize_run(raw, out)
    return root, out


def _build(root, data, tag, *extra):
    out = root / tag
    rc = pipeline.main(
        [
            "--data-dir",
            str(data),
            "--out-dir",
            str(out),
            "--configs",
            str(REPO_ROOT / "configs"),
            "--registry-out",
            str(out / "feature_registry.json"),
            "--cadence-ms",
            "50",
            "--worker-mem-gb",
            "0",
            *extra,
        ]
    )
    assert rc == 0
    return out


def _same_outputs(a, b):
    names = sorted(p.name for p in a.glob("*.parquet"))
    assert names and names == sorted(p.name for p in b.glob("*.parquet"))
    for name in names:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    s1 = json.loads((a / "features_summary.json").read_text())
    s2 = json.loads((b / "features_summary.json").read_text())
    s1.pop("runtime_seconds")
    s2.pop("runtime_seconds")
    assert s1 == s2


# ------------------------------------------------------------ streamed decode


def test_iter_iap1_matches_read_iap1(normalized):
    _, data = normalized
    path = sorted(data.glob("*.normalized.iap1"))[0]
    ref = read_iap1(path)
    assert len(ref) > 100
    for chunk in (1, 7, 65536):
        assert list(iter_iap1(path, chunk_records=chunk)) == ref


def test_iter_iap1_validates_before_first_event(normalized, tmp_path):
    _, data = normalized
    src = sorted(data.glob("*.normalized.iap1"))[0]
    events = read_iap1(src)[:50]
    good = tmp_path / "g.iap1"
    write_iap1(good, events)
    raw = bytearray(good.read_bytes())
    raw[16 + 72 * 10 + 20] ^= 0xFF  # corrupt a record: CRC must catch it
    bad = tmp_path / "b.iap1"
    bad.write_bytes(bytes(raw))
    it = iter_iap1(bad)
    with pytest.raises(ValueError, match="CRC-32 mismatch"):
        next(it)
    trunc = tmp_path / "t.iap1"
    trunc.write_bytes(good.read_bytes()[:-5])
    with pytest.raises(ValueError, match="size mismatch"):
        next(iter_iap1(trunc))
    hdr = tmp_path / "h.iap1"
    hdr.write_bytes(struct.pack("<IIQ", 0xDEAD, 2, 0))
    with pytest.raises(ValueError, match="bad IAP1 magic"):
        next(iter_iap1(hdr))


# -------------------------------------------------------- compact mid series


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_compact_mid_series_labels_identical(seed):
    rng = random.Random(seed)
    plain, compact = MidSeries(), CompactMidSeries()
    t = 1_000_000_000
    for _ in range(3000):
        t += rng.choice([0, 0, 1_000_000, 50_000_000, 900_000_000, 7_000_000_000])
        if rng.random() < 0.08:
            args = (t, float("nan"), float("nan"), False)
        else:
            args = (t, 100.0 + rng.gauss(0, 0.5), rng.choice([0.005, 0.01, 0.02]), True)
        plain.append(*args)
        compact.append(*args)
    anchors = sorted(rng.randrange(1_000_000_000, t) for _ in range(800))
    assert compact.median_gap_ns() == plain.median_gap_ns()
    assert max_sample_age(compact) == max_sample_age(plain)
    for kw in ({}, {"freshness": "trailing"}):
        a = compute_labels(anchors, plain, t, **kw)
        b = compute_labels(np.asarray(anchors, dtype=np.int64).tolist(), compact, t, **kw)
        for h in a:
            assert a[h].reason == b[h].reason
            assert a[h].valid == b[h].valid
            for x, y in ((a[h].mid, b[h].mid), (a[h].cost, b[h].cost)):
                assert np.array_equal(np.asarray(x), np.asarray(y), equal_nan=True)
            assert np.array_equal(
                np.asarray(a[h].reopen_mid), np.asarray(b[h].reopen_mid), equal_nan=True
            )


def test_compact_mid_series_edge_cases():
    s = CompactMidSeries()
    assert s.median_gap_ns() == 0
    s.append(5, 1.0, 0.1)
    s.append(5, 1.0, 0.1)
    assert s.median_gap_ns() == 0  # no distinct gap yet
    with pytest.raises(ValueError):
        s.append(4, 1.0, 0.1)


# --------------------------------------------------------- pipeline identity


@pytest.mark.parametrize(
    ("tag", "extra"),
    [
        ("evt", ("--sampling", "events:40")),
        ("ext_trail", ("--feature-set", "extended", "--label-freshness", "trailing")),
    ],
)
def test_serial_parallel_identical_more_paths(normalized, tag, extra):
    root, data = normalized
    _same_outputs(
        _build(root, data, f"{tag}_s", *extra),
        _build(root, data, f"{tag}_p", "--workers", "2", *extra),
    )


def _fake_rust(monkeypatch, shift: float):
    from iap.features import native

    real = native.compute_native_file

    def fake(path, contexts, cadence_ns=0, engine="python", coalesce=True):
        frame = real(path, contexts, cadence_ns, engine="python", coalesce=coalesce)
        frame.values = frame.values + shift
        return frame

    monkeypatch.setattr(native, "compute_native_file", fake)
    monkeypatch.setattr(native, "resolve_engine", lambda req: req)


def test_deferred_rust_overlay(normalized, monkeypatch):
    """The overlay's values are applied at flush time: an overlay equal to the
    Python values leaves every byte unchanged, a shifted one moves exactly the
    native columns."""
    from iap.features.native import NATIVE_NAMES

    root, data = normalized
    base = _build(root, data, "py")
    _fake_rust(monkeypatch, 0.0)
    same = _build(root, data, "rust_same", "--engine", "rust")
    names = sorted(p.name for p in base.glob("*.parquet"))
    for name in names:
        assert (base / name).read_bytes() == (same / name).read_bytes(), name
    _fake_rust(monkeypatch, 0.5)
    shifted = _build(root, data, "rust_shift", "--engine", "rust")
    for name in names:
        a, b = pq.read_table(base / name), pq.read_table(shifted / name)
        for col in a.column_names:
            x = a.column(col).to_numpy(zero_copy_only=False)
            y = b.column(col).to_numpy(zero_copy_only=False)
            if col in NATIVE_NAMES:
                assert np.allclose(x + 0.5, y, equal_nan=True), col
            elif x.dtype.kind == "f":
                assert np.array_equal(x, y, equal_nan=True), col
            else:
                assert np.array_equal(x, y), col


def test_no_spool_files_left(normalized):
    root, data = normalized
    out = _build(root, data, "spool_check", "--workers", "2")
    assert not [p for p in out.iterdir() if p.name.startswith(".spool")]


# --------------------------------------------------------- memory regression


def test_peak_python_memory_bound(normalized):
    """Generous regression bound on the traced (Python + numpy) peak of a
    serial build.  Before E5 the whole day's events, a boxed mid series and
    the row values were all live at once; afterwards the peak is about one
    instrument's row group.  Measured locally (v1.13): see REAL_DATA §10."""
    root, data = normalized
    rows_bytes = 0
    tracemalloc.start()
    try:
        out = _build(root, data, "mem")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    summary = json.loads((out / "features_summary.json").read_text())
    nfeat = summary["registered_features"]
    rows_total = sum(v["rows"] for v in summary["instruments"].values())
    rows_bytes = rows_total * nfeat * 8
    events_bytes = sum(p.stat().st_size for p in data.glob("*.normalized.iap1"))
    # all row values of the run, or the decoded events of the run, would
    # each blow this bound; one instrument-day of rows fits comfortably
    assert peak < 64 * 2**20 + rows_bytes // 2, (peak, rows_bytes, events_bytes)
