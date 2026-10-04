"""The MVP's optional child execution policy (``execution.child_policy``)
and the cost accounting of research/execution/run_execution_study.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from iap.contracts.types import OrderType
from iap.core.codec import read_jsonl
from iap.execution import ExecPolicy, ExecutionReplay, Liquidity, PassiveParams
from iap.mvp.config import MvpConfig
from iap.mvp.feed import generate_feed
from iap.mvp.session import run_session
from iap.tca.markout import build_gated_timeline

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "python" / "tools"))
TINY_CONFIG = REPO_ROOT / "configs" / "mvp" / "mvp_tiny.json"
PASSIVE = {
    "max_rest_ns": 400_000_000,
    "max_reprices": 1,
    "max_behind_fraction": 0.1,
    "improve_min_spread_ticks": 3,
    "end_margin_ns": 100_000_000,
}


@pytest.fixture(scope="module")
def tiny_cfg() -> MvpConfig:
    return MvpConfig.load(TINY_CONFIG, repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def runs(tiny_cfg, tmp_path_factory):
    base = tmp_path_factory.mktemp("mvp-policy")
    feed = generate_feed(tiny_cfg, base / "feed")
    cfgs = {
        "native": tiny_cfg,
        "aggressive": tiny_cfg.with_overrides(child_policy="aggressive"),
        "passive": tiny_cfg.with_overrides(child_policy="passive", passive=PASSIVE),
    }
    return {name: run_session(cfg, feed, base / name) for name, cfg in cfgs.items()}


def test_policy_keys_are_optional_and_native_by_default(tiny_cfg):
    assert "child_policy" not in tiny_cfg.document["execution"]
    assert tiny_cfg.execution.child_policy is ExecPolicy.NATIVE
    assert tiny_cfg.execution.passive == PassiveParams()
    over = tiny_cfg.with_overrides(child_policy="passive", passive=PASSIVE)
    assert over.execution.child_policy is ExecPolicy.PASSIVE
    assert over.execution.passive.max_rest_ns == 400_000_000
    # a policy run is a different run: its own id and config version
    assert over.run_id != tiny_cfg.run_id
    assert over.config_version() != tiny_cfg.config_version()
    with pytest.raises(ValueError, match="child_policy"):
        tiny_cfg.with_overrides(child_policy="patient")
    with pytest.raises(ValueError, match="passive"):
        tiny_cfg.with_overrides(passive={**PASSIVE, "max_behind_fraction": 2.0})


def test_native_run_never_touches_the_policy_machine(runs):
    eng = runs["native"].engine
    assert eng.passive_stats.to_dict() == dict.fromkeys(eng.passive_stats.to_dict(), 0)
    assert eng.sim.simulator.counters.user_cancels == 0


def test_aggressive_run_sends_only_market_children(runs):
    eng = runs["aggressive"].engine
    assert eng.counters.child_orders_submitted > 0
    assert all(o.type.name == "MARKET" for o in eng.sim.simulator.orders.values())
    assert all(f.liquidity == Liquidity.TAKER for f in eng.sim.simulator.fills)


def test_passive_run_posts_cancels_and_keeps_every_invariant(runs):
    res = runs["passive"]
    eng = res.engine
    s = eng.passive_stats
    assert s.posts > 0
    assert s.crosses_timeout + s.crosses_behind + s.reprices > 0
    assert eng.sim.simulator.counters.user_cancels > 0
    # the money identity and the risk position held after every fill (finish asserts)
    assert res.report["pnl"]["identity_abs_diff"] < 1e-6
    assert res.report["risk"]["open_orders"] == 0
    # no parent is overfilled, replacements included
    for o in eng.outcomes:
        if o.tca is not None:
            assert o.tca.filled_qty <= o.parent.qty
    # maker fills earn the rebate, taker fills pay
    for f in eng.sim.simulator.fills:
        assert (f.fee < 0.0) == (f.liquidity == Liquidity.MAKER)
    # every submitted child went through the risk engine
    assert eng.counters.risk_allowed == eng.counters.child_orders_submitted
    # traces: a posted child is a LIMIT with a price, a cross is a MARKET
    lines = (res.out_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = set()
    for line in lines:
        for c in json.loads(line)["stages"]["child_orders"]:
            kinds.add(c["order_type"])
            if c["order_type"] == OrderType.LIMIT.value and c["venue_id"] != 0:
                assert c["price_ticks"] > 0
    assert {OrderType.LIMIT.value, OrderType.MARKET.value} <= kinds


def test_passive_run_is_deterministic(runs, tiny_cfg, tmp_path):
    cfg = tiny_cfg.with_overrides(child_policy="passive", passive=PASSIVE)
    again = run_session(cfg, runs["passive"].feed, tmp_path / "again")
    assert again.trace_digest == runs["passive"].trace_digest
    assert again.report == runs["passive"].report


# ------------------------------------------------------- study accounting --


@pytest.fixture(scope="module")
def study():
    path = REPO_ROOT / "research" / "execution" / "run_execution_study.py"
    spec = importlib.util.spec_from_file_location("run_execution_study", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_study_costs_price_unfilled_quantity_and_add_up(study, golden_dir):
    import make_golden_replay_passive as tool

    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    t0 = events[0].exchange_ts
    cfg = tool.golden_config(golden_dir.parents[1] / "configs")
    timeline = build_gated_timeline(events, 1, 0.01)
    parents = [p for p in tool.golden_parents(t0) if p.parent_id in (1, 2)]
    native = [replace(p, policy=ExecPolicy.NATIVE) for p in parents]
    rows = {}
    for name, ps in (("passive", parents), ("native", native)):
        res = ExecutionReplay(cfg, ps).run(events)
        rows[name] = [
            study.parent_costs(
                p, [f for f in res.fills if f.parent_id == p.parent_id], timeline, 0.01, 0.003
            )
            for p in ps
        ]
    for name in rows:
        for r in rows[name]:
            assert r["shortfall"] == pytest.approx(r["trading"] + r["opportunity"], abs=1e-9)
            assert r["all_in"] == pytest.approx(
                r["shortfall"] + r["fees_net"] + r["sim_impact"] + r["completion"], abs=1e-9
            )
            assert r["trading"] == pytest.approx(
                r["spread"] + r["price_vs_mid_residual"] + r["timing"], abs=1e-9
            )
    # NATIVE leaves 71 shares of the VWAP parent unfilled: they are not free
    vwap = rows["native"][0]
    assert vwap["filled_qty"] == 329 and vwap["qty"] == 400
    assert vwap["completion"] > 71 * 0.003  # half-spread + taker fee per share
    assert vwap["opportunity"] != 0.0
    # the PASSIVE run completes it: nothing to complete, no opportunity cost
    done = rows["passive"][0]
    assert done["filled_qty"] == 400
    assert done["completion"] == 0.0 and done["opportunity"] == 0.0
    summary = study.summarise(rows["native"])
    assert summary["fill_rate"] == pytest.approx(929 / 1000)
    assert summary["all_in_bps"] == pytest.approx(
        1e4 * sum(r["all_in"] for r in rows["native"]) / summary["notional"]
    )
    diff = study.paired(rows["native"], rows["passive"])
    assert diff["all_in_bps"]["n"] == 2
