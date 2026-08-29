"""Registry integrity tests (conventions §6)."""

from __future__ import annotations

import re

import pytest

from conftest import REPO_ROOT
from iap.features.registry import (
    build_registry,
    feature_index,
    feature_names,
    load_registry,
    registry_hash,
    write_registry,
)
from iap.features.spec import FAMILY_ORDER

REGISTRY_PATH = REPO_ROOT / "data" / "reference" / "feature_registry.json"


def test_count_at_least_200():
    assert len(build_registry()) >= 200


def test_unique_names():
    names = feature_names()
    assert len(names) == len(set(names))


def test_name_pattern():
    pat = re.compile(r"^[a-z][a-z0-9_]*_v\d+$")
    for name in feature_names():
        assert pat.match(name), f"bad feature name: {name}"


def test_all_families_present_and_populated():
    reg = build_registry()
    by_family = {fam: [s for s in reg if s.family == fam] for fam in FAMILY_ORDER}
    for fam in FAMILY_ORDER:
        assert len(by_family[fam]) >= 5, f"family {fam} too small"
    assert sum(len(v) for v in by_family.values()) == len(reg)


def test_registry_grouped_in_family_order():
    fams = [s.family for s in build_registry()]
    # families appear as contiguous blocks in FAMILY_ORDER order
    seen = []
    for f in fams:
        if not seen or seen[-1] != f:
            seen.append(f)
    assert seen == list(FAMILY_ORDER)


def test_depends_on_resolvable_and_no_self():
    reg = build_registry()
    names = set(feature_names())
    for s in reg:
        for dep in s.depends_on:
            assert dep in names, f"{s.name} depends on unknown {dep}"
            assert dep != s.name, f"{s.name} depends on itself"


def test_every_feature_documented():
    for s in build_registry():
        assert s.doc and len(s.doc) >= 15, f"{s.name} lacks a formula doc"
        assert s.version == 1


def test_hash_stable_and_matches_written_file(tmp_path):
    h1 = registry_hash()
    h2 = registry_hash()
    assert h1 == h2 and re.match(r"^[0-9a-f]{64}$", h1)
    doc = write_registry(tmp_path / "feature_registry.json")
    assert doc["registry_hash"] == h1
    assert doc["count"] == len(build_registry())
    loaded = load_registry(tmp_path / "feature_registry.json")
    assert loaded["features"][0]["name"] == feature_names()[0]


def test_repo_registry_file_current():
    """data/reference/feature_registry.json matches the code registry."""
    assert REGISTRY_PATH.is_file(), f"missing {REGISTRY_PATH}"
    doc = load_registry(REGISTRY_PATH)  # raises on hash mismatch
    assert doc["count"] == len(build_registry())
    assert [f["name"] for f in doc["features"]] == feature_names()


def test_feature_index_matches_order():
    idx = feature_index()
    names = feature_names()
    assert all(idx[n] == i for i, n in enumerate(names))
