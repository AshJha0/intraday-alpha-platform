"""Golden feature-vector reproduction (tests/golden/expected_features.json)."""

from __future__ import annotations

import json

import pytest

from conftest import GOLDEN_DIR, REPO_ROOT
from iap.core.codec import read_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import build_registry, feature_index, registry_hash

TOL = 1e-9


@pytest.fixture(scope="module")
def golden():
    with open(GOLDEN_DIR / "expected_features.json") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


def _run_checkpoints(vector_file, checkpoints, contexts):
    events = read_jsonl(GOLDEN_DIR / vector_file)
    engine = FeatureEngine(contexts, cadence_ns=0)
    out = {}
    for i, ev in enumerate(events, start=1):
        vec = engine.apply(ev)
        if i in checkpoints:
            out[i] = vec
    return out


def _check_side(side_doc, contexts):
    idx = feature_index()
    checkpoints = sorted(int(k) for k in side_doc["checkpoints"])
    vecs = _run_checkpoints(side_doc["vector"], set(checkpoints), contexts)
    for cp in checkpoints:
        expected = side_doc["checkpoints"][str(cp)]
        vec = vecs[cp]
        assert vec.timestamp == expected["timestamp"]
        assert vec.instrument_id == side_doc["instrument_id"]
        for name, exp in expected["features"].items():
            i = idx[name]
            assert vec.validity[i] == exp["valid"], (cp, name)
            if exp["valid"]:
                got, want = vec.values[i], exp["value"]
                assert abs(got - want) <= TOL + TOL * abs(want), (
                    f"event {cp} {name}: {got!r} != {want!r}")


def test_golden_eq_checkpoints(golden, contexts):
    _check_side(golden["eq"], contexts)


def test_golden_fx_checkpoints(golden, contexts):
    _check_side(golden["fx"], contexts)


def test_golden_registry_hash_and_count(golden):
    assert golden["registry_hash"] == registry_hash()
    assert golden["registered_count"] == len(build_registry())
    assert golden["registered_count"] >= 200


def test_golden_covers_every_family(golden):
    reg = {s.name: s.family for s in build_registry()}
    covered = set()
    for side in ("eq", "fx"):
        for cp in golden[side]["checkpoints"].values():
            for name, entry in cp["features"].items():
                if entry["valid"]:
                    covered.add(reg[name])
    from iap.features.spec import FAMILY_ORDER
    assert covered == set(FAMILY_ORDER), (
        f"families without a valid golden value: {set(FAMILY_ORDER) - covered}")


def test_engine_determinism(golden, contexts):
    """Two independent runs produce bit-identical vectors at the checkpoints."""
    cps = {500, 2000}
    a = _run_checkpoints("events_eq_mbo.jsonl", cps, contexts)
    b = _run_checkpoints("events_eq_mbo.jsonl", cps, contexts)
    for cp in cps:
        assert a[cp].values == b[cp].values  # exact float equality
        assert a[cp].validity == b[cp].validity
        assert a[cp].feature_version == b[cp].feature_version


# --------------------------------------------------------------------------
# Anomaly-vector golden (API_FEATURES §2 ingestion rules)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_anomalies():
    with open(GOLDEN_DIR / "expected_features_anomalies.json") as f:
        return json.load(f)


def _run_anomaly_side(side_doc, contexts):
    """Replay one anomaly vector; return {checkpoint: (vector, counters)}."""
    events = read_jsonl(GOLDEN_DIR / side_doc["vector"])
    engine = FeatureEngine(contexts, cadence_ns=0)
    want = {int(k) for k in side_doc["checkpoints"]}
    out = {}
    for i, ev in enumerate(events, start=1):
        vec = engine.apply(ev)
        if i in want:
            st = engine.states[side_doc["instrument_id"]]
            out[i] = (
                vec,
                {
                    "events_processed": engine.events_processed,
                    "events_dropped": engine.events_dropped,
                    "ts_regressions_dropped": engine.ts_regressions_dropped,
                    "oversized_qty_dropped": engine.oversized_qty_dropped,
                    "oversized_depth_skipped": engine.oversized_depth_skipped,
                    "recoveries": st.recoveries,
                    "warm_ts": st.warm_ts,
                    "book_ok": st.book_ok,
                },
            )
    assert len(events) == side_doc["n_events"]
    return out


def _check_anomaly_side(side_doc, contexts):
    idx = feature_index()
    got = _run_anomaly_side(side_doc, contexts)
    for key in sorted(side_doc["checkpoints"], key=int):
        exp = side_doc["checkpoints"][key]
        vec, counters = got[int(key)]
        assert vec.timestamp == exp["timestamp"], key
        for cname, want in counters.items():
            assert want == exp[cname], (key, cname)
        for name, e in exp["features"].items():
            i = idx[name]
            assert vec.validity[i] == e["valid"], (key, name)
            if e["valid"]:
                v, w = vec.values[i], e["value"]
                assert abs(v - w) <= TOL + TOL * abs(w), (key, name, v, w)


def test_golden_anomaly_eq_checkpoints(golden_anomalies, contexts):
    _check_anomaly_side(golden_anomalies["eq"], contexts)


def test_golden_anomaly_fx_checkpoints(golden_anomalies, contexts):
    _check_anomaly_side(golden_anomalies["fx"], contexts)


def test_golden_anomaly_vector_exercises_the_drop_paths(golden_anomalies):
    """The vectors must actually contain drops, ts regressions and recoveries
    (otherwise the golden would silently stop testing the ingestion rules)."""
    for side in ("eq", "fx"):
        last_key = sorted(golden_anomalies[side]["checkpoints"], key=int)[-1]
        last = golden_anomalies[side]["checkpoints"][last_key]
        assert last["events_dropped"] > 0, side
        assert last["ts_regressions_dropped"] > 0, side
        assert last["recoveries"] > 0, side
