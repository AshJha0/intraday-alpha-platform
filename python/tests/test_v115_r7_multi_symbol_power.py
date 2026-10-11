"""v1.15 R7 multi-symbol plan: cross-sectional power functions in
iap.research.r7_power (N symbols x S sessions with cross-symbol rho)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from iap.research import r7_power as rp

REPO = Path(__file__).resolve().parents[2]
INPUTS = REPO / "research" / "r7" / "r7_power_inputs.json"
LEDGER = REPO / "research" / "experiments.json"
GATE = {"total_looks": 5000, "t_threshold": 4.4, "dsr_n_trials": 294}


def test_var_ratio_limits():
    assert rp.cross_section_var_ratio(3, 0.3) == pytest.approx(1.0)
    assert rp.cross_section_var_ratio(16, 1.0) == pytest.approx(1.0)
    assert rp.cross_section_var_ratio(16, 0.0) == pytest.approx(3 / 16)
    # monotone: more symbols never hurt, higher rho never helps
    assert rp.cross_section_var_ratio(16, 0.2) < rp.cross_section_var_ratio(8, 0.2) < 1
    assert rp.cross_section_var_ratio(16, 0.2) < rp.cross_section_var_ratio(16, 0.8)
    # floor: as N -> inf the ratio tends to rho / (rho + (1 - rho) / 3)
    assert rp.cross_section_var_ratio(10**9, 0.5) == pytest.approx(0.5 / (0.5 + 0.5 / 3))
    with pytest.raises(ValueError):
        rp.cross_section_var_ratio(3, 1.5)
    with pytest.raises(ValueError):
        rp.cross_section_var_ratio(0, 0.5)


def test_cell_at_reference_matches_single_universe_cell():
    a = rp.multi_symbol_cell(0.04, 0.016, 0.03, 3, 0.5, 7, 4.4, 294)
    b = rp._cell(0.04, 0.016, 0.03, 7, 4.4, 294)
    for k in ("power_hac", "power_dsr", "joint_upper", "mde_joint"):
        assert a[k] == pytest.approx(b[k])
    assert a["sd_scale"] == pytest.approx(1.0)


def test_power_monotone_in_n_and_rho():
    def j(n, rho):
        return rp.multi_symbol_cell(0.04, 0.016, 0.03, n, rho, 7, 4.4, 294)["joint_upper"]

    assert j(3, 0.2) <= j(8, 0.2) <= j(16, 0.2)
    assert j(16, 0.8) <= j(16, 0.5) <= j(16, 0.2)
    assert j(16, 1.0) == pytest.approx(j(3, 1.0))


def test_breakeven_rho():
    args = (0.016, 0.03, 16, 7, 4.4, 294)
    rho = rp.breakeven_rho(0.042, *args)
    assert rho is not None and 0.0 < rho < 1.0
    c = rp.multi_symbol_cell(0.042, 0.016, 0.03, 16, rho, 7, 4.4, 294)
    assert c["joint_upper"] >= rp.TARGET_POWER - 1e-9
    assert (
        rp.multi_symbol_cell(0.042, 0.016, 0.03, 16, min(1.0, rho + 0.02), 7, 4.4, 294)[
            "joint_upper"
        ]
        < rp.TARGET_POWER
    )
    assert rp.breakeven_rho(0.001, *args) is None  # tiny effect: never
    assert rp.breakeven_rho(1.0, *args) == 1.0  # huge effect: any rho


def test_symbol_dispersion():
    d = rp.symbol_dispersion({"QQQ": 0.3, "AAPL": 0.1, "MSFT": 0.2})
    assert list(d["by_symbol"]) == ["AAPL", "MSFT", "QQQ"]
    assert d["mean"] == pytest.approx(0.2)
    assert d["sd"] == pytest.approx(0.1)
    assert (d["min"], d["max"]) == (0.1, 0.3)


def test_analyse_and_render_on_committed_inputs():
    inputs = json.loads(INPUTS.read_text())
    gate = rp.ledger_gate(LEDGER) if LEDGER.exists() else GATE
    res = rp.analyse_multi_symbol(inputs, gate)
    assert set(res["alphas"]) == set(rp.MULTI_ALPHAS)
    for r in res["alphas"].values():
        assert r["dispersion"] is not None and len(r["dispersion"]["by_symbol"]) == 3
        assert len(r["insample"]["cells"]) == len(rp.MULTI_N) * len(rp.MULTI_RHO)
        # N = 3 reproduces the single-universe 7-session result
        ref = rp._cell(
            r["insample"]["effect"],
            r["sigma_session_ref"],
            r["se_hac_1session_ref"],
            7,
            gate["t_threshold"],
            gate["dsr_n_trials"],
        )
        n3 = [c for c in r["insample"]["cells"] if c["n_symbols"] == 3]
        assert all(c["joint_upper"] == pytest.approx(ref["joint_upper"]) for c in n3)
        assert math.isfinite(r["insample"]["rho0_max_n"]["joint_upper"])
    md = rp.render_multi_symbol_markdown(res)
    assert "Break-even rho" in md and "| EQ04 @5s |" in md
    json.dumps(res)  # serialisable
