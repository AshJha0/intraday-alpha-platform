"""Golden: ``tests/golden/expected_canonical_json.json`` — the byte-level
canonical JSON / float repr / string escaping / digest contract shared by all
four languages (generator: ``python/tools/make_golden_canonical_json.py``)."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

import pytest

from iap.contracts.examples import example_trace
from iap.contracts.ids import make_trace_id
from iap.contracts.versions import canonical_json, content_hash
from iap.trace.digest import TraceDigest

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "golden" / "expected_canonical_json.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="ascii"))


def test_golden_header(golden: dict) -> None:
    assert golden["x-version"] == 1
    assert len(golden["float_repr"]) >= 2000
    assert golden["rules"]["separators"] == [",", ":"]


def test_golden_float_repr(golden: dict) -> None:
    for case in golden["float_repr"]:
        value = struct.unpack("<d", struct.pack("<Q", int(case["bits_hex"], 16)))[0]
        assert math.isfinite(value)
        assert canonical_json(value) == case["repr"], case
        # Round-trip: the text parses back to the identical bit pattern.
        back = json.loads(case["repr"])
        assert struct.pack("<d", back) == struct.pack("<d", value), case


def test_golden_string_escape(golden: dict) -> None:
    for case in golden["string_escape"]:
        text = "".join(chr(c) for c in case["input_codepoints"])
        assert canonical_json(text) == case["json"], case
        assert case["json"].isascii()
        assert json.loads(case["json"]) == text


def test_golden_documents(golden: dict) -> None:
    for case in golden["documents"]:
        doc = json.loads(case["canonical"])
        assert canonical_json(doc) == case["canonical"]
        assert content_hash(doc) == case["sha256"]


def test_golden_rejects(golden: dict) -> None:
    probes = {
        "nan": float("nan"), "inf": float("inf"), "-inf": float("-inf"),
        "nested nan": {"a": [1, {"b": float("nan")}]}, "integer key": {1: "x"},
    }
    assert {r["case"] for r in golden["rejects"]} == set(probes)
    for probe in probes.values():
        with pytest.raises(ValueError):
            canonical_json(probe)


def test_golden_trace_id(golden: dict) -> None:
    pin = golden["trace_id"]
    inputs = pin["inputs"]
    assert make_trace_id(inputs["session_id"], inputs["instrument_id"],
                         inputs["event_ts"], inputs["sequence"]) == pin["expected"]
    assert hashlib.sha256(pin["preimage"].encode("ascii")).hexdigest()[:32] == pin["expected"]


def test_golden_trace_digest(golden: dict) -> None:
    pin = golden["trace_digest"]
    trace = example_trace()
    line = canonical_json(trace.to_dict())
    assert len(line) == pin["line_length"]
    assert hashlib.sha256(line.encode("ascii")).hexdigest() == pin["line_sha256"]
    once = TraceDigest().update(trace).hexdigest()
    twice = TraceDigest().update(trace).update(trace).hexdigest()
    assert once == pin["digest_one_trace"]
    assert twice == pin["digest_same_trace_twice"]
    assert TraceDigest().hexdigest() == pin["digest_empty_stream"]
    assert once != twice
