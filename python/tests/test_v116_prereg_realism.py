"""v1.16 P0-3: net-of-cost prereg realism keys (configs/prereg/v3.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.execution.realism import RealismOptIns, load_fee_tiers, load_prereg
from test_maker_economics import INS, TICK, exec_config, synth_events

REPO = Path(__file__).resolve().parents[2]
V2 = REPO / "configs" / "prereg" / "v2.json"
V3 = REPO / "configs" / "prereg" / "v3.json"
TIERS = {
    "top": {"taker_fee_per_share": 0.003, "maker_rebate_per_share": 0.002},
    "base": {"taker_fee_per_share": 0.003, "maker_rebate_per_share": 0.001},
}


def test_v2_is_unchanged_by_the_new_keys():
    r = load_prereg(V2)
    assert r.venue_latency is None and r.latency_stress == () and r.fee_tier is None
    assert r.exec_prints is False and r.impact_model == "child"
    cfg = exec_config()
    assert r.exec_config(cfg) == cfg
    assert r.simulator_kwargs() == {"hidden_prints": True}
    assert r.calibration_kwargs() == {"markout_unit": "touch_order"}


def test_v3_loads_and_states_every_key():
    r = load_prereg(V3)
    assert r.latency_stress == (0, 1, 5)
    assert r.fee_tier == "mid" and r.fee_tier_sensitivity == ("base",)
    assert r.exec_prints and r.impact_model == "parent" and r.impact_shape == "sqrt"
    assert r.simulator_kwargs()["exec_print_price"] is True
    assert r.calibration_kwargs()["impact_fit"] is True
    doc = json.loads(V3.read_text(encoding="utf-8"))
    assert doc["methods"] == "v3"


def test_shipped_fee_tier_table_is_valid():
    tiers = load_fee_tiers()
    assert {"top", "mid", "base"} <= set(tiers)
    # lower tiers never rebate more
    assert (
        tiers["top"]["maker_rebate_per_share"]
        >= tiers["mid"]["maker_rebate_per_share"]
        >= tiers["base"]["maker_rebate_per_share"]
    )


def test_venue_latency_fee_tier_and_stress_grid():
    r = RealismOptIns(
        venue_latency={"mean_ns": 200_000, "jitter_ns": 40_000},
        latency_stress=(0, 1, 5),
        fee_tier="top",
        fee_tier_sensitivity=("base",),
        fee_tiers=TIERS,
    )
    cfg = r.exec_config(exec_config())
    for v in cfg.venues.values():
        assert (v.latency_mean_ns, v.latency_jitter_ns) == (200_000, 40_000)
        assert v.maker_rebate_per_share == 0.002
    grid = r.stressed_exec_configs(exec_config())
    assert sorted(grid) == [0, 1, 5]
    assert all(v.latency_mean_ns == 0 for v in grid[0].venues.values())
    assert all(v.latency_mean_ns == 1_000_000 for v in grid[5].venues.values())
    assert grid[1] == cfg
    tiers = r.fee_tier_configs(exec_config())
    assert [v.maker_rebate_per_share for v in tiers["base"].venues.values()] == [0.001] * len(
        cfg.venues
    )
    tail = r.latency_tail()
    assert tail["source"] == "parametric_tail"
    assert tail["quantiles_ns"][-1] > 240_000


def test_parent_impact_needs_a_fitted_calibration():
    r = RealismOptIns(impact_model="parent", impact_shape="sqrt")
    with pytest.raises(ValueError, match="needs a calibration"):
        r.exec_config(exec_config())
    assert r.exec_config(exec_config(), impact=False) == exec_config()
    doc = estimate_calibration(synth_events(n_steps=4000, seed=7), {INS: TICK}, impact_fit=True)
    res = doc["impact"]["residual"]
    res["by_horizon"][res["horizon"]]["sqrt"] = {"coeff_bps_per_sqrt_pct_adv": 4.0, "se": 0.1}
    cfg = r.exec_config(exec_config(), ExecCalibration.from_dict(doc))
    assert (cfg.impact_model, cfg.impact_exponent, cfg.impact_coeff_bps_per_pct_adv) == (
        "parent",
        0.5,
        4.0,
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"venue_latency": {"mean_ns": 1}},
        {"venue_latency": {"mean_ns": 1, "jitter_ns": -1}},
        {"venue_latency": {"mean_ns": 1, "jitter_ns": 1, "bogus": 2}},
        {"venue_latency": {"mean_ns": 1, "jitter_ns": 1, "tail_mult": 0.5}},
        {"latency_stress": (0, 5)},
        {"latency_stress": (1, -1)},
        {"exec_prints": 1},
        {"impact_model": "parent"},
        {"impact_shape": "sqrt"},
        {"impact_model": "x", "impact_shape": "sqrt"},
        {"fee_tier_sensitivity": ("base",)},
        {"fee_tier": "platinum", "fee_tiers": TIERS},
        {"fee_tier": "top", "fee_tier_sensitivity": ("nope",), "fee_tiers": TIERS},
    ],
)
def test_refusals(kw):
    with pytest.raises(ValueError):
        RealismOptIns(**kw)


def test_unknown_prereg_key_still_refused():
    with pytest.raises(ValueError, match="unknown keys"):
        RealismOptIns.from_prereg({"execution_realism": {"fee_tiers": {}}})
