"""v1.16 P0-1: simulator rule 6b, impact charged on the parent order."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from iap.execution.config import ExecConfig
from iap.execution.simulator import ExecutionSimulator
from iap.execution.types import OrderType
from test_execution_rules import LAT, T0, Feeder, child, make_config, seed_book


def parent_config(exponent: float = 1.0, perm: float = 0.0) -> ExecConfig:
    return replace(
        make_config(),
        impact_model="parent",
        impact_exponent=exponent,
        permanent_impact_bps_per_pct_adv=perm,
    )


def deep_book(sim: ExecutionSimulator, f: Feeder) -> None:
    """One ask level deep enough that every buy below fills at 101."""
    sim.on_event(f.add(T0, 0, 100, 10_000, 11))
    sim.on_event(f.add(T0 + 1, 1, 101, 10_000, 21))


def run_buys(cfg: ExecConfig, sizes: list[int], parent_ids: list[int] | None = None) -> float:
    sim = ExecutionSimulator(cfg)
    f = Feeder()
    deep_book(sim, f)
    ts = T0 + 10
    for i, q in enumerate(sizes):
        c = child(0, OrderType.MARKET, 0, q, ts)
        if parent_ids is not None:
            c = replace(c, parent_id=parent_ids[i])
        sim.submit(c)
        ts += 10 * LAT
        sim.on_event(f.heartbeat(ts))
    assert sum(fl.qty for fl in sim.fills) == sum(sizes)
    return sum(fl.impact_cost for fl in sim.fills)


def test_default_is_the_pinned_child_rule():
    assert make_config().impact_model == "child"


@pytest.mark.parametrize("exponent", [1.0, 0.5])
def test_splitting_a_parent_into_children_does_not_reduce_cost(exponent):
    cfg = parent_config(exponent)
    whole = run_buys(cfg, [1000])
    split = run_buys(cfg, [250, 250, 250, 250])
    assert math.isclose(whole, split, rel_tol=1e-12)
    # The pinned child rule rewards splitting (each child sized alone).
    assert run_buys(make_config(), [250] * 4) < run_buys(make_config(), [1000])


def test_parent_linear_matches_child_rule_for_a_single_child():
    # One child == its parent: rule 6b reduces to rule 6.
    assert math.isclose(
        run_buys(parent_config(), [1000]), run_buys(make_config(), [1000]), rel_tol=1e-12
    )


def test_parent_hand_calc_square_root():
    # adv 1e6, unit 1: x = q/1e4 % ADV; cost = 2 * x**0.5 * q * 1e-4 * 1.01
    q = 400
    want = 2.0 * (q / 1e4) ** 0.5 * q * 1e-4 * 1.01
    assert math.isclose(run_buys(parent_config(0.5), [q]), want, rel_tol=1e-12)


def test_distinct_parents_are_sized_separately():
    cfg = parent_config()
    assert run_buys(cfg, [500, 500], [1, 2]) < run_buys(cfg, [500, 500], [1, 1])


def test_permanent_leg_hand_calc_and_split_invariance():
    cfg = parent_config(perm=1.0)
    temp = run_buys(parent_config(), [1000])
    # perm = 1.0 * (1000/1e4) % ADV * 1000 / 2 bps-shares
    want_perm = 1.0 * (1 / 1e4) * (1000**2) / 2.0 * 1e-4 * 1.01
    assert math.isclose(run_buys(cfg, [1000]) - temp, want_perm, rel_tol=1e-12)
    # Across different parents the permanent leg still adds up.
    assert math.isclose(
        run_buys(cfg, [500, 500], [1, 2]) - run_buys(parent_config(), [500, 500], [1, 2]),
        want_perm,
        rel_tol=1e-12,
    )


def test_permanent_leg_credits_trading_against_own_flow():
    cfg = parent_config(perm=1.0)
    sim = ExecutionSimulator(cfg)
    f = Feeder()
    seed_book(sim, f)
    sim.submit(child(0, OrderType.MARKET, 0, 100, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    sim.submit(replace(child(1, OrderType.MARKET, 0, 100, T0 + 20 * LAT), parent_id=7))
    sim.on_event(f.heartbeat(T0 + 21 * LAT + 1))
    buy, sell = sim.fills
    temp_sell = 2.0 * (100 / 1e4) * 100 * 1e-4 * 1.00
    assert sell.impact_cost < temp_sell  # permanent credit on the unwind


@pytest.mark.parametrize(
    "kw",
    [
        {"impact_model": "nope"},
        {"impact_model": "parent", "impact_exponent": 0.0},
        {"impact_model": "parent", "impact_exponent": 1.5},
        {"impact_model": "parent", "permanent_impact_bps_per_pct_adv": -1.0},
        {"impact_exponent": 0.5},
        {"permanent_impact_bps_per_pct_adv": 1.0},
    ],
)
def test_config_validation(kw):
    with pytest.raises(ValueError):
        replace(make_config(), **kw)
