"""Parallel feature build (``--workers N``, v1.9.0) is byte-identical to serial."""

from __future__ import annotations

import json

import numpy as np
import pyarrow.parquet as pq
import pytest
from conftest import REPO_ROOT
from iap.features import __main__ as pipeline
from iap.marketdata.generator import MarketDataGenerator
from iap.marketdata.normalize import normalize_run

CFG = {
    "seed": 4242,
    "sessions": 3,
    "equities": {"slots_per_stream": 200},
    "fx": {"slots_per_pair": 150},
}


@pytest.fixture(scope="module")
def normalized(tmp_path_factory, refdata_module):
    root = tmp_path_factory.mktemp("parallel")
    raw = root / "raw"
    MarketDataGenerator(refdata_module, dict(CFG)).generate_run(raw)
    for path in sorted(raw.glob("*.jsonl")):
        if path.stat().st_size == 0:
            path.unlink()
    out = root / "normalized"
    normalize_run(raw, out)
    return root, out


@pytest.fixture(scope="module")
def refdata_module():
    from iap.reference.refdata import ReferenceData

    return ReferenceData.load(REPO_ROOT / "configs")


def _build(root, data, tag, workers):
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
            str(root / tag / "feature_registry.json"),
            "--cadence-ms",
            "50",
            "--workers",
            str(workers),
            "--worker-mem-gb",
            "0",
        ]
    )
    assert rc == 0
    return out


def test_parallel_build_is_byte_identical(normalized):
    root, data = normalized
    files = sorted(data.glob("*.normalized.*"))
    assert len(files) >= 3  # genuinely multi-day
    serial = _build(root, data, "serial", 1)
    parallel = _build(root, data, "parallel", 3)
    names = sorted(p.name for p in serial.glob("*.parquet"))
    assert names and names == sorted(p.name for p in parallel.glob("*.parquet"))
    for name in names:
        assert (serial / name).read_bytes() == (parallel / name).read_bytes(), name
    s1 = json.loads((serial / "features_summary.json").read_text())
    s2 = json.loads((parallel / "features_summary.json").read_text())
    s1.pop("runtime_seconds")
    s2.pop("runtime_seconds")
    assert s1 == s2
    # the cross-day profile carry is really exercised: some norm features
    # are valid, and on a day after the first
    norm_valid = 0
    for name in names:
        t = pq.read_table(serial / name, columns=["exchange_ts", "norm_spread_m5_v1"])
        v = t.column("norm_spread_m5_v1").to_numpy(zero_copy_only=False)
        ts = t.column("exchange_ts").to_numpy()
        first_day = ts.min() // 86_400_000_000_000
        norm_valid += int(np.isfinite(v[ts // 86_400_000_000_000 > first_day]).sum())
    assert norm_valid > 0


def test_effective_workers_clamps():
    assert pipeline.effective_workers(1, 10, 5.0) == 1
    assert pipeline.effective_workers(8, 2, 0) <= 2
    assert pipeline.effective_workers(0, 1, 0) == 1
    assert pipeline.effective_workers(4, 4, 1e9) == 1  # RAM cap
