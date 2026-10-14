"""v1.16 P0-2: residual-move impact calibration (``impact.residual``)."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from iap.core.events import EventType, MarketEvent
from iap.execution.calibration import (
    ExecCalibration,
    _power_fit,
    _through_origin,
    apply_impact_fit,
    estimate_calibration,
)
from test_maker_economics import INS, T0, TICK, VEN, exec_config, synth_events


def test_off_by_default_document_unchanged():
    doc = estimate_calibration(synth_events(seed=5), {INS: TICK})
    assert "residual" not in doc["impact"]
    on = estimate_calibration(synth_events(seed=5), {INS: TICK}, impact_fit=True)
    # Everything but the new block is byte-identical.
    on_impact = dict(on["impact"])
    on_impact.pop("residual")
    assert on_impact == doc["impact"]
    assert {k: v for k, v in on.items() if k != "impact"} == {
        k: v for k, v in doc.items() if k != "impact"
    }


def test_residual_block_shape():
    doc = estimate_calibration(synth_events(n_steps=4000, seed=7), {INS: TICK}, impact_fit=True)
    res = doc["impact"]["residual"]
    assert res["adv"] == "tape" and res["n_sweeps"] > 0
    cell = res["by_horizon"][res["horizon"]]
    assert cell["n"] >= 20
    assert set(cell) == {"n", "linear", "sqrt", "power"}
    assert cell["linear"]["se"] >= 0 and cell["sqrt"]["se"] >= 0
    ExecCalibration.from_dict(doc)  # still a valid v1 document


def test_through_origin_exact_and_hc0():
    xs = [1.0, 2.0, 3.0, 4.0]
    b, se = _through_origin(xs, [2.5 * x for x in xs])
    assert math.isclose(b, 2.5) and se == pytest.approx(0.0, abs=1e-12)
    ys = [2.0, 5.0, 7.0, 11.0]
    b, se = _through_origin(xs, ys)
    sxx = sum(x * x for x in xs)
    assert math.isclose(b, sum(x * y for x, y in zip(xs, ys, strict=True)) / sxx)
    meat = sum((x * (y - b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    assert math.isclose(se, math.sqrt(meat) / sxx)
    assert _through_origin([0.0, 0.0], [1.0, 2.0]) is None


def test_power_fit_recovers_square_root_law():
    xs = [0.01 * (i + 1) for i in range(200)]
    ys = [3.0 * math.sqrt(x) for x in xs]
    fit = _power_fit(xs, ys, 10)
    assert fit["bins_used"] == 10
    # bin means of sqrt(x) != sqrt of bin mean: close, not exact
    assert fit["exponent"] == pytest.approx(0.5, abs=0.02)
    assert fit["coeff"] == pytest.approx(3.0, rel=0.05)


def test_power_fit_needs_three_positive_bins():
    fit = _power_fit([1.0, 2.0, 3.0, 4.0], [-1.0, -1.0, -1.0, 1.0], 4)
    assert fit["exponent"] is None and fit["bins_used"] == 1


def test_tape_adv_counts_hidden_prints_not_execute_prints():
    def ev(i, t, et, side, px, qty, oid, tid=0):
        return MarketEvent(
            event_id=i, instrument_id=INS, venue_id=VEN, exchange_ts=t, receive_ts=t,
            sequence=i, event_type=int(et), side=side, price_ticks=px, qty=qty,
            order_id=oid, trade_id=tid,
        )  # fmt: skip

    evs = [
        ev(1, T0, EventType.ADD, 0, 100, 500, 1),
        ev(2, T0 + 1, EventType.ADD, 1, 101, 500, 2),
        ev(3, T0 + 10, EventType.EXECUTE, 1, 101, 100, 2, 7),
        ev(4, T0 + 10, EventType.TRADE, 0, 101, 100, 0, 7),  # its print: not counted
        ev(5, T0 + 20, EventType.TRADE, 0, 101, 300, 0, 8),  # hidden: counted
    ]
    from iap.execution import calibration as cal

    seen = {}
    orig = cal._residual_impact

    def spy(sweeps, timelines, tape_qty, *a):
        seen.update(tape_qty)
        return orig(sweeps, timelines, tape_qty, *a)

    cal._residual_impact = spy
    try:
        estimate_calibration(evs, {INS: TICK}, impact_fit=True)
    finally:
        cal._residual_impact = orig
    assert seen == {INS: 400}


def _cal(fit_cell: dict) -> ExecCalibration:
    doc = estimate_calibration(synth_events(seed=5), {INS: TICK}, impact_fit=True)
    res = doc["impact"]["residual"]
    res["by_horizon"][res["horizon"]] = fit_cell
    return ExecCalibration.from_dict(doc)


@pytest.mark.parametrize(
    ("shape", "coeff", "k"),
    [("linear", 1.5, 1.0), ("sqrt", 4.0, 0.5), ("power", 3.0, 0.6)],
)
def test_apply_impact_fit_sets_parent_rule(shape, coeff, k):
    cell = {
        "n": 50,
        "linear": {"coeff_bps_per_pct_adv": 1.5, "se": 0.1},
        "sqrt": {"coeff_bps_per_sqrt_pct_adv": 4.0, "se": 0.2},
        "power": {"bins_used": 10, "exponent": 0.6, "exponent_se": 0.05, "coeff": 3.0},
    }
    cfg = apply_impact_fit(exec_config(), _cal(cell), shape)
    assert cfg.impact_model == "parent"
    assert cfg.impact_coeff_bps_per_pct_adv == coeff and cfg.impact_exponent == k


def test_apply_impact_fit_refusals():
    plain = ExecCalibration.from_dict(estimate_calibration(synth_events(seed=5), {INS: TICK}))
    with pytest.raises(ValueError, match="no impact.residual"):
        apply_impact_fit(exec_config(), plain)
    with pytest.raises(ValueError, match="shape"):
        apply_impact_fit(exec_config(), plain, "cubic")
    empty = _cal({"n": 3, "linear": None, "sqrt": None, "power": None})
    with pytest.raises(ValueError, match="no sqrt"):
        apply_impact_fit(exec_config(), empty)
    neg = _cal({"n": 50, "linear": None, "sqrt": {"coeff_bps_per_sqrt_pct_adv": -1.0, "se": 1}})
    with pytest.raises(ValueError, match="not positive"):
        apply_impact_fit(exec_config(), neg)
    bad_k = _cal({"n": 50, "power": {"exponent": 1.4, "coeff": 2.0}})
    with pytest.raises(ValueError, match="impact_exponent"):
        apply_impact_fit(exec_config(), bad_k, "power")
    assert replace(exec_config()).impact_model == "child"
