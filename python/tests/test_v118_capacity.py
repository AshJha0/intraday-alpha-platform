"""REL-17 capacity curves and the scorecard capacity section."""

from __future__ import annotations

import math

import pytest
from iap.portfolio.capacity import CapacityInput, capacity_curve, capacity_section
from iap.research.scorecard_capacity import (
    append_capacity,
    rel08_inputs,
    rel08c_inputs,
    rel09_inputs,
    rel10_inputs,
)


def test_curve_closed_form() -> None:
    # gross-after-linear-costs a = 3 bps, impact 1 bps at base -> net 2 bps
    c = CapacityInput("x", 2.0, 0.5, 1e6, impact_bps_at_base=1.0, base_participation=0.01)
    cv = capacity_curve(c, multipliers=(1.0, 4.0, 9.0))
    assert cv["status"] == "ok"
    r1, r4, r9 = cv["rows"]
    assert r1["net_bps"] == pytest.approx(2.0)
    assert r4["net_bps"] == pytest.approx(1.0) and r4["participation"] == pytest.approx(0.04)
    assert r9["net_bps"] == pytest.approx(0.0)
    assert cv["breakeven_multiplier"] == pytest.approx(9.0)  # (3/1)^2
    assert cv["best_multiplier"] == pytest.approx(4.0)  # (3/1.5)^2
    assert r4["net_usd"] == pytest.approx(1.0 * 1e-4 * 4e6)
    assert r1["net_bps_lo"] < r1["net_bps"] < r1["net_bps_hi"]
    assert cv["breakeven_multiplier_lo"] < 9.0 < cv["breakeven_multiplier_hi"]


def test_impact_from_cost_model_when_study_has_none() -> None:
    cv = capacity_curve(CapacityInput("x", 1.0, None, None, base_participation=1e-4))
    assert cv["impact_bps_at_base"] == pytest.approx(100.0 * 0.01)
    assert "cost model" in cv["impact_source"]


def test_unscalable_and_passive() -> None:
    assert capacity_curve(CapacityInput("x", 1.0, 0.1, 1e6))["status"] == "not_computable"
    cv = capacity_curve(CapacityInput("m", 1.0, 0.1, 1e6, passive=True))
    assert cv["breakeven_multiplier"] == math.inf
    assert all(r["net_bps"] == 1.0 for r in cv["rows"])


def test_empty_section_is_labelled_not_applicable() -> None:
    md = capacity_section("REL-08", [], "no variant passed")
    assert "Capacity (REL-17)" in md and "Not applicable" in md and "no variant passed" in md


def _rel08_raw(outcome: str) -> dict:
    return {
        "variants": [
            {
                "id": "v1",
                "n_rebalances": 100,
                "net_bps": {"mean": 2.0, "se": 0.5, "lower": 1.1},
                "book": {"impact_cost": 50.0, "mean_turnover": 0.5},
            }
        ],
        "decisions": [{"id": "v1", "outcome": outcome}],
    }


def test_rel08_adapter_and_scorecard() -> None:
    inputs, why = rel08_inputs(_rel08_raw("continue"), base_notional=100_000.0)
    assert why == "" and len(inputs) == 1
    c = inputs[0]
    assert c.impact_bps_at_base == pytest.approx(50.0 / (100 * 1e5) * 1e4)  # 0.05
    assert c.base_participation == pytest.approx((0.05 / 0.5 / 100.0) ** 2)
    md = append_capacity("rel08", "# REL-08 scorecard\n", inputs)
    assert md.startswith("# REL-08 scorecard") and "| x base |" in md
    none, why = rel08_inputs(_rel08_raw("stop"), base_notional=1e5)
    assert none == [] and "Not applicable" in append_capacity("rel08", "#", none, why)


def test_rel08c_rel09_rel10_adapters() -> None:
    ins, _ = rel08c_inputs(
        {"verdict": "continue", "mean_incremental_bps": 3.0, "ci_low_bps": 1.0, "ci_high_bps": 5.0},
        1e4,
        1e-4,
    )
    assert capacity_curve(ins[0])["status"] == "ok"
    assert rel08c_inputs({"verdict": "reject"})[0] == []
    ins, _ = rel09_inputs({"ci95_usd": [1.0, 3.0]})
    assert capacity_curve(ins[0])["status"] == "not_computable"
    assert rel09_inputs({"ci95_usd": [-1.0, 3.0]})[0] == []
    s = {"ci95_usd": [10.0, 30.0], "mean_usd": 20.0, "per_trip": {"mean_net_bps": 2.0}}
    ins, _ = rel10_inputs({"taker": s, "maker": s}, 1e4, 1e-4)
    assert [i.passive for i in ins] == [False, True]
    assert ins[0].net_bps_ci == pytest.approx((1.0, 3.0))
    assert rel10_inputs({"taker": {**s, "ci95_usd": [-1.0, 2.0]}})[0] == []
