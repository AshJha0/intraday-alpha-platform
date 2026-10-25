"""U6: the ML protocol is frozen under a hash before the first fit; prose and block agree; delta is the documented calculation."""

from __future__ import annotations

import hashlib

import pytest
from iap.ml import protocol
from iap.ml.protocol import (
    FROZEN_SHA256,
    PROTOCOL_PATH,
    ProtocolError,
    check_block_against_prose,
    derive_delta,
    load_protocol,
    protocol_hash,
    require_frozen,
)
from stage8_fixtures import REPO_ROOT

DOC = REPO_ROOT / "research" / "v2" / "ML_PROTOCOL.md"


def test_the_protocol_file_is_the_one_the_framework_hashes() -> None:
    assert PROTOCOL_PATH.resolve() == DOC.resolve()


def test_protocol_hash_is_frozen() -> None:
    text = DOC.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == FROZEN_SHA256
    assert protocol_hash() == FROZEN_SHA256
    assert require_frozen() == FROZEN_SHA256
    assert len(FROZEN_SHA256) == 64 and "PLACEHOLDER" not in FROZEN_SHA256


def test_an_altered_protocol_is_refused(tmp_path) -> None:
    p = tmp_path / "ML_PROTOCOL.md"
    p.write_text(
        DOC.read_text(encoding="utf-8").replace('"resamples": 2000', '"resamples": 200'),
        encoding="utf-8",
    )
    with pytest.raises(ProtocolError, match="new revision"):
        require_frozen(p)


def test_block_holds_every_frozen_element() -> None:
    doc = load_protocol()
    assert doc["revision"] == 1 and doc["placeholders_filled"] is False
    assert set(doc["feature_groups"]) == {"d1", "d2", "d3", "d4"}
    for fam, g in doc["feature_groups"].items():
        assert g["features"][0].startswith(fam + ".")
        assert "common.beta_60" not in g["features"] and "common.adv_20" not in g["features"]
    assert list(doc["class_order"]) == ["M0", "M1", "M2", "M3", "M4"]
    assert (
        doc["tiers"]["M1"]["budget"] == 5
        and len(doc["grids"]["M1.ridge"]) + len(doc["grids"]["M1.elastic_net"]) == 5
    )
    assert doc["tiers"]["M3"]["budget"] == 8 == len(doc["grids"]["M3.gbt"])
    assert doc["early_stopping"]["applies_to"] == ["M3.gbt"]
    assert doc["admission"]["bootstrap"]["kind"] == "stationary block bootstrap"
    assert doc["admission"]["block_length"]["fixed_before_comparison"] is True
    assert doc["delta"]["common_across_families"] is True and doc["delta"]["unit"].startswith(
        "bps of registered capital"
    )
    assert "lexicographic" in doc["advancement"]["rule"]
    assert "no incremental value confirmed" in doc["sealed_test"]["rule"]
    assert doc["seed"] == 20261025


def test_prose_and_block_agree_and_delta_is_the_documented_calculation() -> None:
    doc = load_protocol()
    check_block_against_prose(doc)
    d = derive_delta(doc)
    assert d["delta_bps"] == max(d["delta_cost_bps"], d["delta_power_bps"])
    assert round(d["delta_bps"], 2) == doc["delta"]["value_bps"]
    assert "synthetic" in d["label"] and d["placeholders_filled"] is False


def test_prose_text_names_the_frozen_rules() -> None:
    text = DOC.read_text(encoding="utf-8")
    for phrase in (
        "CI_lower(Delta U) > delta",
        "M0 < M1 < M2 < M3 < M4",
        "Holm",
        "stationary block",
        "final evaluation only",
        "no incremental value confirmed",
        "embargo",
        "frozen under a hash",
    ):
        assert phrase in text, phrase
    assert protocol.CLASS_ORDER == ("M0", "M1", "M2", "M3", "M4")
