"""v1.16 P0-5: POV exact participation + catch-up; Almgren-Chriss from the residual fit."""

from __future__ import annotations

import pytest
from iap.execution import AlgoType, ExecutionReplay
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.execution.optimal import ac_params_from_calibration
from test_exec_algos import algo_config, algo_stream, parent
from test_maker_economics import INS, TICK, synth_events


def _filled(**kw) -> int:
    p = parent(AlgoType.POV, 2000, 1, **kw)
    return ExecutionReplay(algo_config(), [p]).run(algo_stream()).parents[1].filled_qty


def test_exact_participation_counts_our_fills_in_the_total():
    # Window market volume 4000 (100 TRADEs of 40).
    assert _filled(participation=0.2, max_child_qty=1000) == 800  # pinned: 20% of market
    q = _filled(participation=0.2, max_child_qty=1000, pov_exact=True)
    assert q == 1000 and q / (4000 + q) == pytest.approx(0.2)  # 20% of the total


def test_catch_up_covers_the_whole_deficit():
    # Each TRADE raises the target by 8 (pinned) / 10 (exact); child cap 5.
    lagging = _filled(participation=0.2, max_child_qty=5)
    assert lagging == 500  # pinned: one capped child per TRADE falls behind
    assert _filled(participation=0.2, max_child_qty=5, pov_exact=True) == 1000


@pytest.mark.parametrize("p", [0.0, 1.0])
def test_exact_needs_participation_strictly_inside_0_1(p):
    with pytest.raises(ValueError, match="participation"):
        _filled(participation=p, pov_exact=True)


def _cal(linear_coeff: float | None) -> ExecCalibration:
    doc = estimate_calibration(synth_events(seed=5), {INS: TICK}, impact_fit=True)
    res = doc["impact"]["residual"]
    res["by_horizon"][res["horizon"]]["linear"] = (
        None if linear_coeff is None else {"coeff_bps_per_pct_adv": linear_coeff, "se": 0.1}
    )
    return ExecCalibration.from_dict(doc)


def test_ac_params_from_the_residual_fit():
    kw = {"sigma": 0.02, "adv": 1e6, "price": 100.0}
    p = ac_params_from_calibration(calibration=_cal(3.0), impact_source="residual", **kw)
    assert p.eta == pytest.approx(3.0 * 1e-2 * 100.0 / 1e6)
    # The default still reads the v1.9 full-move slope.
    cal = _cal(3.0)
    slope = cal.impact_coeff_bps_per_pct_adv
    if slope is not None:
        d = ac_params_from_calibration(calibration=cal, **kw)
        assert d.eta == pytest.approx(slope * 1e-2 * 100.0 / 1e6)


def test_ac_residual_refusals():
    kw = {"sigma": 0.02, "adv": 1e6, "price": 100.0}
    with pytest.raises(ValueError, match="impact.residual"):
        ac_params_from_calibration(calibration=_cal(None), impact_source="residual", **kw)
    with pytest.raises(ValueError, match="impact.residual"):
        ac_params_from_calibration(impact_source="residual", **kw)
    with pytest.raises(ValueError, match="impact_source"):
        ac_params_from_calibration(impact_source="nope", **kw)
