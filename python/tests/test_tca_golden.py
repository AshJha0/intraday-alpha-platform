"""Golden test: pinned fill sets vs expected Perold decomposition (1e-9)."""

from __future__ import annotations

import json

import pytest

from iap.tca.tca import perold_decomposition


@pytest.fixture(scope="module")
def golden(golden_dir):
    with open(golden_dir / "expected_tca.json") as f:
        return json.load(f)


def test_golden_tca_cases_match(golden):
    tol = golden["tolerance"]
    assert tol == 1e-9
    assert len(golden["cases"]) >= 4
    for case in golden["cases"]:
        c = case["inputs"]
        got = perold_decomposition(
            c["side_sign"], c["qty_target"],
            [tuple(f) for f in c["fills"]],
            c["decision_mid"], c["arrival_mid"], c["end_mid"])
        exp = case["expected"]
        assert set(got) == set(exp), case["inputs"]["name"]
        for key, want in exp.items():
            assert abs(got[key] - want) <= tol, \
                f"{c['name']}: {key} {got[key]} != {want}"


def test_golden_tca_internal_identity(golden):
    """The stored expected values themselves satisfy the IS identity."""
    for case in golden["cases"]:
        e = case["expected"]
        assert abs(e["delay_cost"] + e["trading_cost"]
                   + e["opportunity_cost"] - e["total_is"]) <= 1e-9
        assert abs(e["delay_bps"] + e["trading_bps"]
                   + e["opportunity_bps"] - e["total_is_bps"]) <= 1e-9


def test_golden_tca_covers_key_scenarios(golden):
    names = {c["inputs"]["name"] for c in golden["cases"]}
    assert {"buy_full_fill", "sell_partial_fill", "buy_unfilled",
            "fx_sell_full"} <= names
    # the unfilled case is pure opportunity cost
    unfilled = next(c["expected"] for c in golden["cases"]
                    if c["inputs"]["name"] == "buy_unfilled")
    assert unfilled["delay_cost"] == 0.0
    assert unfilled["trading_cost"] == 0.0
    assert unfilled["total_is"] == unfilled["opportunity_cost"]
