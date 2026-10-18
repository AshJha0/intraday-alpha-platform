"""v1.18 REL-07: cross-asset reference ETF by symbol (feature identity xasset_v2)."""

from __future__ import annotations

import json
import math

import pytest
from bruteforce_features import approx
from iap.alpha.base import configure_universe, universe_ids
from iap.alpha.universe_v2 import configure_universe_many_etfs
from iap.core.codec import write_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.reference_map import (
    DEFAULT_MAP,
    FEATURE_IDENTITY,
    apply_reference_map,
    build_contexts_v2,
    load_reference_map,
)
from iap.features.registry import feature_index
from rel07_fixture import tape, write_configs

AAPL, AMLP, QQQ = 1, 2, 3


def test_committed_map_is_versioned_and_point_in_time():
    doc = json.loads(DEFAULT_MAP.read_text())
    assert doc["as_of"] == "2019-01-30" and doc["map_version"] == "reference_etf_map_v1"
    assert doc["feature_identity"] == FEATURE_IDENTITY
    assert doc["source"] and len(doc["limitations"]) >= 3
    rmap = load_reference_map()
    assert len(rmap.references) == 71  # the EQUITY rows of the 100-name universe
    assert rmap.references["AAPL"] == "QQQ" and rmap.references["JPM"] == "SPY"
    assert set(rmap.references.values()) == {"QQQ", "SPY"}
    assert rmap.identity()["map_sha256"] == rmap.sha256 and len(rmap.sha256) == 64


def _beta(contexts, events):
    j = feature_index()["beta_w5m_v1"]
    eng = FeatureEngine(contexts, cadence_ns=0)
    out = {}
    for ev in events:
        vec = eng.apply(ev)
        if vec is not None and vec.instrument_id == AAPL and vec.validity[j]:
            out[vec.timestamp] = vec.values[j]
    return out


def test_v2_reference_reproduces_qqq_relative_beta(tmp_path):
    """The map picks QQQ for AAPL although AMLP is the first ETF row, and the
    v2 beta_w5m equals the v1 beta of a universe whose first ETF is QQQ."""
    events = tape()
    v1_amlp = build_contexts(write_configs(tmp_path / "a", (AAPL, AMLP, QQQ)))
    v1_qqq = build_contexts(write_configs(tmp_path / "q", (AAPL, QQQ, AMLP)))
    v2 = build_contexts_v2(tmp_path / "a")
    assert v1_amlp[AAPL].ref_instrument_id == AMLP  # the defect: "first ETF row"
    assert v1_qqq[AAPL].ref_instrument_id == QQQ
    assert v2[AAPL].ref_instrument_id == QQQ
    assert v2[AMLP].ref_instrument_id == AMLP and v2[QQQ].ref_instrument_id == QQQ

    b_qqq, b_v2, b_amlp = _beta(v1_qqq, events), _beta(v2, events), _beta(v1_amlp, events)
    assert len(b_v2) > 100 and b_v2.keys() == b_qqq.keys()
    assert all(approx(b_v2[t], b_qqq[t]) for t in b_qqq)  # v1 golden tolerance 1e-9
    last = max(b_v2)
    assert 1.5 < b_v2[last] < 2.5  # AAPL moves 2x QQQ in the fixture
    assert abs(b_amlp.get(last, math.nan)) < 0.5  # vs AMLP: no relation


def test_map_fails_closed(tmp_path):
    rmap = load_reference_map()
    ctxs = build_contexts(write_configs(tmp_path / "a", (AAPL, AMLP)))  # no QQQ row
    with pytest.raises(ValueError, match="not an ETF row"):
        apply_reference_map(ctxs, rmap)
    bad = json.loads(DEFAULT_MAP.read_text())
    bad["symbols"]["AAPL"]["reference_etf"] = "AMLP"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="reference_etf"):
        load_reference_map(p)
    unknown = dict(bad, symbols={"MSFT": {"reference_etf": "QQQ"}})
    p.write_text(json.dumps(unknown))
    with pytest.raises(ValueError, match="not in reference map"):
        apply_reference_map(build_contexts(tmp_path / "a"), load_reference_map(p))


def test_configure_universe_many_etfs_is_opt_in(tmp_path):
    cfg = write_configs(tmp_path / "a", (AAPL, AMLP, QQQ)) / "instruments" / "instruments.json"
    with pytest.raises(ValueError, match="more than one ETF"):
        configure_universe(cfg)
    configure_universe_many_etfs(cfg)
    assert universe_ids("etf") == (AMLP, QQQ) and universe_ids("constituents") == (AAPL,)
    configure_universe(None)


def test_store_cli_records_the_identity(tmp_path):
    from iap.features.__main__ import main

    data = tmp_path / "norm"
    data.mkdir()
    write_jsonl(data / "eq_20190130.normalized.jsonl", tape(seconds=60))
    cfg = write_configs(tmp_path / "cfg", (AAPL, AMLP, QQQ))
    base = ["--data-dir", str(data), "--configs", str(cfg)]
    for name, extra in (("v1", []), ("v2", ["--xasset-reference", "symbol_map"])):
        out = tmp_path / name
        assert (
            main([*base, "--out-dir", str(out), "--registry-out", str(out / "r.json"), *extra]) == 0
        )
        summary = json.loads((out / "features_summary.json").read_text())
        if name == "v1":
            assert "xasset_reference" not in summary and "labels_v2" not in summary
        else:
            assert summary["xasset_reference"] == load_reference_map().identity()
