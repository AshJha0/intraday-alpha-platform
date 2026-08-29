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
