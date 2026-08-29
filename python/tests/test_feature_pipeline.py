"""Feature pipeline (python -m iap.features) integration tests."""

from __future__ import annotations

import json
import shutil

import pyarrow.parquet as pq
import pytest

from conftest import GOLDEN_DIR, REPO_ROOT
from iap.features import __main__ as pipeline
from iap.features.registry import feature_names, registry_hash
from iap.labels.labels import HORIZON_ORDER


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    """Run the pipeline over the golden vectors staged as normalized files."""
    root = tmp_path_factory.mktemp("pipeline")
    data = root / "normalized"
    data.mkdir()
    shutil.copy(GOLDEN_DIR / "events_eq_mbo.jsonl",
                data / "eq_golden.normalized.jsonl")
    shutil.copy(GOLDEN_DIR / "events_fx_quote.jsonl",
                data / "fx_golden.normalized.jsonl")
    out = root / "features"
    rc = pipeline.main([
        "--data-dir", str(data),
        "--out-dir", str(out),
        "--configs", str(REPO_ROOT / "configs"),
        "--registry-out", str(root / "reference" / "feature_registry.json"),
        "--cadence-ms", "0",
    ])
    assert rc == 0
    return root


def test_pipeline_outputs_exist(run_dir):
    out = run_dir / "features"
    assert (out / "features_1.parquet").is_file()
    assert (out / "features_101.parquet").is_file()
    assert (out / "features_summary.json").is_file()
    assert (run_dir / "reference" / "feature_registry.json").is_file()


def test_pipeline_parquet_schema_and_rows(run_dir):
    table = pq.read_table(run_dir / "features" / "features_1.parquet")
    assert table.num_rows == 2000  # cadence 0 -> one row per event
    cols = set(table.column_names)
    for name in feature_names():
        assert name in cols
    assert "validity_bits" in cols and "exchange_ts" in cols
    for h in HORIZON_ORDER:
        assert {f"label_mid_{h}", f"label_cost_{h}", f"label_valid_{h}"} <= cols
    ts = table.column("exchange_ts").to_pylist()
    assert ts == sorted(ts)


def test_pipeline_summary_contents(run_dir):
    with open(run_dir / "features" / "features_summary.json") as f:
        summary = json.load(f)
    assert summary["registry_hash"] == registry_hash()
    assert summary["registered_features"] >= 200
    assert summary["events_processed"] == 2800
    assert set(summary["instruments"]) == {"1", "101"}
    for inst in summary["instruments"].values():
        assert inst["rows"] > 0
        fracs = inst["valid_fraction_by_family"]
        assert set(fracs) == {"price", "micro", "flow", "liquidity", "vol",
                              "tod", "xasset", "venue", "regime", "exec"}
        for v in fracs.values():
            assert 0.0 <= v <= 1.0


def test_pipeline_labels_no_lookahead_tail(run_dir):
    """Labels whose horizon extends past the stream end must be invalid."""
    table = pq.read_table(run_dir / "features" / "features_1.parquet")
    ts = table.column("exchange_ts").to_pylist()
    last = ts[-1]
    for h, h_ns in (("1s", 10**9), ("1m", 60 * 10**9), ("15m", 900 * 10**9)):
        valid = table.column(f"label_valid_{h}").to_pylist()
        mids = table.column(f"label_mid_{h}").to_pylist()
        import math
        for t, v, m in zip(ts, valid, mids):
            if t + h_ns > last:
                assert not v, f"label_{h} valid past stream end"
            if not v:
                assert math.isnan(m)
        # some early labels must be valid
        assert any(valid)


def test_pipeline_deterministic(run_dir, tmp_path):
    """A second run over the same inputs produces identical tables."""
    data = run_dir / "normalized"
    out2 = tmp_path / "features2"
    rc = pipeline.main([
        "--data-dir", str(data),
        "--out-dir", str(out2),
        "--configs", str(REPO_ROOT / "configs"),
        "--registry-out", str(tmp_path / "reference" / "feature_registry.json"),
        "--cadence-ms", "0",
    ])
    assert rc == 0
    for iid in (1, 101):
        t1 = pq.read_table(run_dir / "features" / f"features_{iid}.parquet")
        t2 = pq.read_table(out2 / f"features_{iid}.parquet")
        # pandas .equals treats NaN == NaN (Arrow's .equals does not)
        assert t1.to_pandas().equals(t2.to_pandas())


def test_pipeline_cadence_reduces_rows(run_dir, tmp_path):
    out3 = tmp_path / "features3"
    rc = pipeline.main([
        "--data-dir", str(run_dir / "normalized"),
        "--files", "eq_golden.normalized.jsonl",
        "--out-dir", str(out3),
        "--configs", str(REPO_ROOT / "configs"),
        "--registry-out", str(tmp_path / "reference" / "feature_registry.json"),
        "--cadence-ms", "1000",
    ])
    assert rc == 0
    table = pq.read_table(out3 / "features_1.parquet")
    assert 0 < table.num_rows < 2000
