"""v1.17 WS0 items 0.5 / 0.6 (review X-1, X-2, X-3).

* X-1 - VWAP slices follow the forecast curve over the PARENT window
  (``curve_slice_weights(start_frac, end_frac)``, ``curve_window``); the
  pinned ``1 + x^2`` resampling is unchanged.
* X-2 - ``realism.apply()`` is the one entry point; every v3 key changes at
  least one simulator fill or config; the prereg ``mean_ns`` is a mean; a
  calibration's latency table no longer hides the prereg venue latency;
  ingest writes the assumed venue latency instead of 0 / 0.
* X-3 - the residual impact fit records its support, parents beyond it are
  flagged, and a cost under the parent rule is labelled "replay + fitted
  adder".
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from iap.execution import ChildOrder, ExecutionSimulator
from iap.execution.algos import AlgoType, ParentOrder, slice_weights
from iap.execution.calibration import ExecCalibration, apply_impact_fit, estimate_calibration
from iap.execution.realism import (
    V116_KEYS,
    RealismOptIns,
    apply,
    simulator_latency_base_ns,
)
from iap.execution.volume_curve import DEFAULT_SESSION, curve_slice_weights, curve_window, u_shape
from test_maker_economics import INS, T0, TICK, VEN, exec_config, synth_events
from test_v116_prereg_realism import TIERS

NS_S = 1_000_000_000
DAY_NS = 86_400 * NS_S

# ----------------------------------------------------------------- X-1


def test_curve_slice_weights_follow_the_parent_window():
    curve = [0.4, 0.1, 0.1, 0.4]  # a U over four bins
    assert curve_slice_weights(curve, 4) == pytest.approx(curve)  # v1.10 rule
    assert curve_slice_weights(u_shape(4), 4) == pytest.approx(u_shape(4))  # golden untouched
    # the first half of the session: the slices see the falling edge only
    assert curve_slice_weights(curve, 2, 0.0, 0.5) == pytest.approx([0.4, 0.1])
    assert curve_slice_weights(curve, 4, 0.25, 0.75) == pytest.approx([0.05] * 4)
    # a window straddling bins splits their mass proportionally
    assert curve_slice_weights(curve, 1, 0.125, 0.375) == pytest.approx([0.4 * 0.5 + 0.1 * 0.5])
    with pytest.raises(ValueError, match="start_frac"):
        curve_slice_weights(curve, 2, 0.5, 0.5)
    with pytest.raises(ValueError, match="no mass"):
        curve_slice_weights([1.0, 0.0, 0.0, 0.0], 2, 0.5, 1.0)


def test_curve_window_maps_the_parent_to_session_fractions():
    s0, s1 = DEFAULT_SESSION
    day = 3 * DAY_NS
    a, b = curve_window(day + s0, day + s1)
    assert (a, b) == (0.0, 1.0)
    mid = day + (s0 + s1) // 2
    a, b = curve_window(mid, day + s1)
    assert a == pytest.approx(0.5) and b == 1.0
    # clamped to the session, within one UTC day, must overlap it
    assert curve_window(day, day + s1) == (0.0, 1.0)
    with pytest.raises(ValueError, match="one UTC day"):
        curve_window(day + s1 - NS_S, day + DAY_NS + NS_S)
    with pytest.raises(ValueError, match="overlap"):
        curve_window(day + s1 + NS_S, day + s1 + 2 * NS_S)
    # through ParentOrder: the window changes the VWAP slices, nothing else
    curve = tuple(u_shape(8))
    whole = ParentOrder(algo=AlgoType.VWAP, qty=800, slices=4, volume_curve=curve)
    first = replace(whole, curve_window=(0.0, 0.5))
    assert slice_weights(whole) == pytest.approx(curve_slice_weights(curve, 4))
    assert slice_weights(first) == pytest.approx(curve_slice_weights(curve, 4, 0.0, 0.5))
    assert slice_weights(first)[0] > slice_weights(first)[-1]  # the morning edge falls
    pinned = ParentOrder(algo=AlgoType.VWAP, qty=800, slices=4, curve_window=(0.0, 0.5))
    assert slice_weights(pinned) == pytest.approx([1 + x * x for x in (-1, -1 / 3, 1 / 3, 1)])


# ----------------------------------------------------------------- X-2


def _fills(cfg, calibration=None, passive: bool = False, **sim_kwargs):
    """Fills of one buy child worked through the synthetic stream: marketable
    (a taker, limit far above the touch) or ``passive`` (resting at the bid,
    filled as a maker when the stream executes against the bid queue)."""
    sim = ExecutionSimulator(cfg, calibration, **sim_kwargs)
    events = synth_events(n_steps=600, seed=3)
    submitted = False
    for ev in events:
        # the passive child rests at the bid from the first event (front of
        # the queue); the taker arrives two seconds in, against a built book
        if not submitted and (passive or ev.exchange_ts >= T0 + 2 * NS_S):
            sim.submit(
                ChildOrder(
                    instrument_id=INS,
                    venue_id=VEN,
                    side=0,
                    qty=1 if passive else 40,
                    limit_ticks=10_000 if passive else 20_000,
                    decision_ts=ev.exchange_ts,
                )
            )
            submitted = True
        sim.on_event(ev)
    return [
        (f.price_ticks, f.qty, f.ts, round(f.fee, 9), round(f.impact_cost, 12)) for f in sim.fills
    ]


def _fitted_calibration():
    doc = estimate_calibration(synth_events(n_steps=4000, seed=7), {INS: TICK}, impact_fit=True)
    res = doc["impact"]["residual"]
    res["by_horizon"][res["horizon"]]["sqrt"] = {"coeff_bps_per_sqrt_pct_adv": 4.0, "se": 0.1}
    return ExecCalibration.from_dict(doc)


def test_every_v3_key_changes_a_fill_or_a_config():
    base_cfg = exec_config(jitter_ns=0)
    base = _fills(base_cfg)
    assert base, "the harness must fill"
    changed: dict[str, bool] = {}
    # venue_latency: a different arrival time, different fills
    lat = apply(RealismOptIns(venue_latency={"mean_ns": 2_000_000, "jitter_ns": 0}), base_cfg)
    assert lat.applied["venue_latency"] == "applied"
    changed["venue_latency"] = _fills(lat.exec_config) != base
    # latency_stress: the 5x config fills differently from the 1x one
    grid = apply(
        RealismOptIns(
            venue_latency={"mean_ns": 2_000_000, "jitter_ns": 0}, latency_stress=(0, 1, 5)
        ),
        base_cfg,
    )
    assert sorted(grid.stressed) == [0, 1, 5] and grid.applied["latency_stress"].startswith(
        "applied"
    )
    changed["latency_stress"] = _fills(grid.stressed[5]) != _fills(grid.stressed[1])
    # fee_tier: every tier's taker fee sits at the access-fee cap, so the
    # tiers differ in the maker REBATE: a passive (maker) fill shows it
    passive = _fills(base_cfg, passive=True)
    assert passive, "the passive harness must fill as a maker"
    tier = apply(RealismOptIns(fee_tier="base", fee_tiers=TIERS), base_cfg)
    changed["fee_tier"] = _fills(tier.exec_config, passive=True) != passive
    # fee_tier_sensitivity: the sensitivity tier's fees differ from the prereg tier's
    sens = apply(
        RealismOptIns(fee_tier="top", fee_tier_sensitivity=("base",), fee_tiers=TIERS), base_cfg
    )
    assert set(sens.fee_tiers) == {"top", "base"}
    changed["fee_tier_sensitivity"] = _fills(sens.fee_tiers["base"], passive=True) != _fills(
        sens.fee_tiers["top"], passive=True
    )
    # impact_model / impact_shape: the parent adder replaces the linear child one
    imp = apply(
        RealismOptIns(impact_model="parent", impact_shape="sqrt"),
        base_cfg,
        calibration=_fitted_calibration(),
    )
    assert imp.applied["impact_model"] == "applied" and imp.exec_config.impact_model == "parent"
    changed["impact_model"] = changed["impact_shape"] = _fills(imp.exec_config) != base
    # exec_prints: the simulator is told to fill at the printed price (the
    # mapping side is test_v115_exec_prints); the keyword must reach it
    ep = apply(RealismOptIns(exec_prints=True), base_cfg)
    assert ep.simulator_kwargs.get("exec_print_price") is True
    sim = ExecutionSimulator(ep.exec_config, **ep.simulator_kwargs)
    changed["exec_prints"] = (
        sim._exec_print_price is True and not ExecutionSimulator(base_cfg)._exec_print_price
    )
    assert set(changed) == set(V116_KEYS)
    assert all(changed.values()), changed


def test_apply_states_every_key_and_impact_not_applied_is_said():
    cfg = exec_config()
    off = apply(RealismOptIns(), cfg)
    assert off.exec_config == cfg and off.calibration is None
    assert set(off.applied) == set(V116_KEYS) and set(off.applied.values()) == {"not set"}
    assert off.stressed == {1: cfg} and off.fee_tiers == {} and off.not_applied() == {}
    r = RealismOptIns(impact_model="parent", impact_shape="sqrt")
    mvp = apply(r, cfg, impact=False)
    assert mvp.exec_config == cfg
    assert mvp.not_applied() == {
        "impact_model": "not applied: the caller runs no execution calibration (rule 6 stays)",
        "impact_shape": "not applied: the caller runs no execution calibration (rule 6 stays)",
    }
    with pytest.raises(ValueError, match="needs a calibration"):
        apply(r, cfg)


def test_prereg_mean_is_the_mean_and_the_calibration_table_carries_it():
    assert simulator_latency_base_ns(250_000, 100_000) == 200_000
    assert simulator_latency_base_ns(10, 100) == 0
    r = RealismOptIns(venue_latency={"mean_ns": 250_000, "jitter_ns": 100_000})
    cfg = r.exec_config(exec_config())
    v = cfg.venues[VEN]
    assert (v.latency_mean_ns, v.latency_jitter_ns) == (200_000, 100_000)
    assert v.latency_mean_ns + v.latency_jitter_ns / 2 == 250_000
    tail = r.latency_tail()
    assert tail["quantiles_ns"][0] == 200_000  # the body starts at mean - jitter / 2
    # a calibration with its own latency table would override the venue
    # config in the simulator: apply() puts the prereg table on every venue
    doc = {
        "schema": "iap.exec_calibration",
        "version": 1,
        "latency": {str(VEN): {"probs": [0.0, 1.0], "quantiles_ns": [5_000_000, 5_000_000]}},
    }
    cal = ExecCalibration.from_dict(doc)
    out = apply(r, exec_config(), calibration=cal)
    assert out.applied["venue_latency"] == "applied (config and calibration latency table)"
    table = out.calibration.latency_for(VEN)
    assert table is not None and table.quantiles_ns[0] == 200_000
    assert out.calibration.doc["latency"][str(VEN)]["source"] == "parametric_tail"
    assert cal.latency_for(VEN).quantiles_ns[0] == 5_000_000  # the input is untouched
    sim = ExecutionSimulator(out.exec_config, out.calibration)
    order = ChildOrder(
        instrument_id=INS, venue_id=VEN, side=0, qty=1, limit_ticks=1, decision_ts=T0
    )
    arrivals = [
        sim.orders[sim.submit(order)].arrival_ts - T0 - out.exec_config.latency.internal_ns
        for _ in range(50)
    ]
    assert min(arrivals) >= 200_000 and max(arrivals) <= 10 * 300_000


def test_ingest_reference_latency_table_is_consistent_with_the_v3_prereg():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    doc = json.loads((repo / "configs/reference/venue_latency.json").read_text(encoding="utf-8"))
    v3 = json.loads((repo / "configs/prereg/v3.json").read_text(encoding="utf-8"))
    vl = v3["execution_realism"]["venue_latency"]
    assert doc["venues"]["NASDAQ"] == {"mean_ns": vl["mean_ns"], "jitter_ns": vl["jitter_ns"]}
    assert doc["default"] == doc["venues"]["NASDAQ"]
    assert "ASSUMED" in doc["description"]


# ----------------------------------------------------------------- X-3


def test_fit_support_is_recorded_applied_flagged_and_labelled():
    cal = _fitted_calibration()
    res = cal.doc["impact"]["residual"]
    cell = res["by_horizon"][res["horizon"]]
    sup = cell["support_pct_adv"]
    assert sup is not None and sup["min"] <= sup["p05"] <= sup["p50"] <= sup["p95"] <= sup["max"]
    cfg = apply_impact_fit(exec_config(), cal, "sqrt")
    assert cfg.impact_fit_support_pct_adv == (sup["min"], sup["max"])
    label = cfg.impact_label()
    assert label.startswith("replay + fitted adder (rule 6b, exponent 0.5, fit support ")
    assert exec_config().impact_label() == "replay + linear child adder (rule 6)"
    # a parent far beyond the support is charged (extrapolated) and flagged
    tiny = replace(cfg, impact_fit_support_pct_adv=(0.0, 1e-9))
    sim = ExecutionSimulator(tiny)
    for ev in synth_events(n_steps=600, seed=3):
        if ev.exchange_ts >= T0 + 2 * NS_S and not sim.fills:
            sim.submit(
                ChildOrder(
                    instrument_id=INS,
                    venue_id=VEN,
                    side=0,
                    qty=40,
                    limit_ticks=20_000,
                    decision_ts=ev.exchange_ts,
                    parent_id=5,
                )
            )
        sim.on_event(ev)
    assert sim.fills and sim.impact_beyond_support == {(INS, 5)}
    wide = ExecutionSimulator(replace(cfg, impact_fit_support_pct_adv=(0.0, 1e9)))
    assert wide.impact_beyond_support == set()
    with pytest.raises(ValueError, match="impact_model='parent'"):
        replace(exec_config(), impact_fit_support_pct_adv=(0.0, 1.0))
    with pytest.raises(ValueError, match="lo <= hi"):
        replace(cfg, impact_fit_support_pct_adv=(2.0, 1.0))
