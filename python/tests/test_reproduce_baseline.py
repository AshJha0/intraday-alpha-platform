"""Review finding 6: the reproduction script's 9.6 comparison (stdlib, no clone)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "reproduce_baseline", ROOT / "tools" / "reproduce_baseline.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_identical_documents_have_no_differences():
    t = _tool()
    d = {"a": [1, 2.5, {"x_bps": 1.234}], "s": "v"}
    assert t.compare(d, d) == []


def test_counts_and_strings_are_exact_and_bps_floats_round_to_two_decimals():
    t = _tool()
    diffs = t.compare(
        {"n": 3, "s": "a", "x": {"shortfall_bps": {"mean": 1.231}}, "f": 0.5},
        {"n": 4, "s": "b", "x": {"shortfall_bps": {"mean": 1.229}}, "f": 0.5000001},
    )
    kinds = {d["path"]: d["kind"] for d in diffs}
    assert kinds == {
        "n": "exact",
        "s": "exact",
        "x.shortfall_bps.mean": "float_within_tolerance",
        "f": "float",
    }


def test_missing_keys_and_length_changes_are_listed():
    t = _tool()
    diffs = t.compare({"a": 1, "l": [1, 2]}, {"b": 1, "l": [1]})
    assert {(d["path"], d["kind"]) for d in diffs} == {
        ("a", "key"),
        ("b", "key"),
        ("l", "length"),
    }
