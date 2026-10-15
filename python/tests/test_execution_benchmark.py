"""v1.17 WS-B B1: the implementation-shortfall benchmark driver and the three
review fixes it carries (X-5 walk depth + ``depth_exhausted``, X-6 indexed
replay, X-7 fill provenance). Synthetic / golden data only."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from iap.core.codec import read_jsonl
from iap.execution import ExecConfig, ExecutionSimulator, OrderType
from iap.execution.types import FILL_PROVENANCES
from iap.research import execution_benchmark as bench
from test_execution_golden import golden_config, golden_parents
from test_execution_rules import T0, Feeder, child, make_config, seed_book

from iap.execution import ExecutionReplay  # isort: skip

REPO = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------- X-5 / X-7 --


def test_walk_depth_is_configurable_and_default_is_pinned():
    assert ExecConfig().walk_depth_levels == 10
    with pytest.raises(ValueError):
        ExecConfig(walk_depth_levels=0)


def test_market_remainder_beyond_walk_depth_is_counted_as_depth_exhausted():
    f = Feeder()
    sim = ExecutionSimulator(replace(make_config(), walk_depth_levels=1))
    seed_book(sim, f)  # asks 101x200, 102x500
    oid = sim.submit(child(0, OrderType.MARKET, 0, 500, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10_000_000))
    o = sim.orders[oid]
    assert [x.qty for x in sim.fills] == [200]  # one level only
    assert o.remaining == 300 and o.depth_exhausted
    assert sim.depth_exhausted == 1 and sim.depth_exhausted_qty == 300
    assert sim.fills[0].provenance == "aggressive_walk"


def test_limit_bound_remainder_is_not_depth_exhausted():
    f = Feeder()
    sim = ExecutionSimulator(make_config())
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.IOC, 101, 500, T0 + 10))  # 102 is outside
    sim.on_event(f.heartbeat(T0 + 10_000_000))
    o = sim.orders[oid]
    assert o.remaining == 300 and not o.depth_exhausted
    assert sim.depth_exhausted == 0


def test_golden_fills_carry_a_provenance_and_the_rows_are_unchanged(golden_dir):
    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    cfg = golden_config(golden_dir.parents[1] / "configs")
    res = ExecutionReplay(cfg, golden_parents(events[0].exchange_ts)).run(events)
    want = json.load(open(golden_dir / "expected_replay_fills.json"))
    assert [f.to_dict() for f in res.fills] == [
        {**w, "impact_cost": float(w["impact_cost"])} for w in want["fills"]
    ]
    assert all(f.provenance in FILL_PROVENANCES for f in res.fills)
    assert {f.provenance for f in res.fills if f.parent_id == 2} == {"aggressive_walk"}
    assert "provenance" not in res.fills[0].to_dict()
    for r in res.parents.values():
        assert "depth_exhausted" not in r.to_dict()
        assert sum(r.provenance_qty.values()) == r.filled_qty


# ------------------------------------------------------------------- X-6 --


def test_replay_visits_only_parents_the_event_can_change(golden_dir):
    """A parent whose slices are all in the future is never scheduled."""
    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    cfg = golden_config(golden_dir.parents[1] / "configs")
    t0 = events[0].exchange_ts
    far = [
        replace(p, start_ts=t0 + 10_000 * bench.SEC, end_ts=t0 + 20_000 * bench.SEC)
        for p in golden_parents(t0)
    ]
    replay = ExecutionReplay(cfg, golden_parents(t0) + [replace(far[0], parent_id=3)])
    visits = 0
    orig = replay._schedule

    def counting(ps, ev):
        nonlocal visits
        visits += 1
        return orig(ps, ev)

    replay._schedule = counting  # type: ignore[method-assign]
    res = replay.run(events)
    assert res.events_processed == len(events)
    assert visits < len(events)  # the old scan made 3 * len(events) visits
    assert res.parents[3].filled_qty == 0 and res.parents[3].children == 0


# ---------------------------------------------------------------- stats --


def test_cluster_mean_stats_hand_example():
    s = bench.cluster_mean_stats([1.0, 3.0, 2.0, 4.0], ["a", "a", "b", "b"], seed=1)
    assert s["n"] == 4 and s["n_clusters"] == 2 and s["df"] == 1
    assert s["mean"] == pytest.approx(2.5)
    # cluster sums of residuals: a -> -1, b -> +1; CR1 = 2/1 * 2 / 16 = 0.25
    assert s["se"] == pytest.approx(0.5)
    assert s["t"] == pytest.approx(5.0)
    assert s["ci95"][0] < s["mean"] < s["ci95"][1]
    assert s["boot_p05"] <= s["mean"] <= s["boot_p95"]
    assert s["low_power"] is True
    one = bench.cluster_mean_stats([1.0, 2.0], ["a", "a"])
    assert one["mean"] == 1.5 and one["se"] is None


def test_parent_qty_rounds_to_lots():
    assert bench.parent_qty(38_000_000, 0.1) == 38_000
    assert bench.parent_qty(1_000, 0.1) == 100


# --------------------------------------------------------------- driver --


@pytest.fixture(scope="module")
def dataset(tmp_path_factory, golden_dir) -> Path:
    root = tmp_path_factory.mktemp("bench")
    norm = root / "normalized"
    norm.mkdir()
    for day in ("20190101", "20190102"):
        shutil.copy(golden_dir / "events_eq_mbo.jsonl", norm / f"eq_{day}.normalized.jsonl")
    return root


ARGS = [
    "--sizes-pct",
    "0.0005,0.002",
    "--algos",
    "TWAP,POV",
    "--window-min",
    "1.5",
    "--warmup-min",
    "0.5",
    "--no-timestamp",
]


def test_driver_writes_labelled_clustered_report(dataset, tmp_path):
    out = tmp_path / "out"
    rc = bench.main(
        ["--data-dir", str(dataset), "--config-dir", str(REPO / "configs"), "--out", str(out)]
        + ARGS
        + ["--write-prereg-draft"]
    )
    assert rc == 0
    doc = json.loads((out / "benchmark_bench0.json").read_text())
    md = (out / "BENCHMARK_bench0.md").read_text()
    assert doc["sessions"] == ["20190101", "20190102"]
    assert doc["simulator"]["cost_label"] == "replay + linear child adder (rule 6)"
    assert "replay + linear child adder (rule 6)" in md
    assert len(doc["cells"]) == 3 * 2 * 2
    for key, cell in doc["cells"].items():
        assert cell["n_parents"] > 0 and cell["n_sessions"] == 2
        st = cell["costs_bps"]["all_in"]
        assert st["n_clusters"] == 2 and st["df"] == 1 and st["se"] is not None
        assert st["ci95"][0] <= st["mean"] <= st["ci95"][1]
        assert set(cell["provenance_share_of_filled"]) <= set(FILL_PROVENANCES)
        assert cell["depth_exhausted"]["parents"] >= 0
        if key.startswith("aggressive|"):
            assert cell["maker_share_of_filled"] == 0.0
            assert set(cell["provenance_share_of_filled"]) <= {"aggressive_walk"}
    # paired comparisons exist for every non-baseline policy
    assert len(doc["paired_vs_baseline"]) == 2 * 2 * 2
    # the draft is unsigned
    draft = json.loads((out / "prereg_DRAFT.json").read_text())
    assert draft["signed"] is False and draft["signature"] is None
    assert "DRAFT" in draft["status"]
    assert draft["gate_B7"]["alpha"] == 0.05


def test_driver_is_deterministic(dataset, tmp_path):
    outs = []
    for k in range(2):
        out = tmp_path / f"o{k}"
        bench.main(
            ["--data-dir", str(dataset), "--config-dir", str(REPO / "configs"), "--out", str(out)]
            + ARGS
        )
        outs.append((out / "benchmark_bench0.json").read_bytes())
    assert outs[0] == outs[1]


def test_driver_refuses_holdout_paths(tmp_path):
    hold = tmp_path / "r7_holdout"
    hold.mkdir()
    with pytest.raises(SystemExit, match="holdout"):
        bench.run_benchmark(hold)


def test_all_in_identity_per_parent(dataset):
    doc = bench.run_benchmark(
        dataset,
        config_dir=REPO / "configs",
        sizes_pct=(0.0005,),
        algos=("TWAP",),
        policies=("native",),
        baseline_policy="native",
        window_min=1.5,
        warmup_min=0.5,
        timestamp=False,
    )
    cell = doc["cells"]["native|TWAP|0.0005"]
    cb = cell["costs_bps"]
    parts = sum(cb[k]["mean"] for k in ("shortfall", "fees_net", "sim_impact", "completion"))
    assert cb["all_in"]["mean"] == pytest.approx(parts, abs=1e-9)


def test_committed_prereg_draft_is_unsigned():
    draft = json.loads((REPO / "research/execution/benchmark/prereg_DRAFT.json").read_text())
    assert draft["signed"] is False and draft["on_blackboard"] is False
    assert "DRAFT" in draft["status"]
    assert draft["grid"]["sizes_pct_adv"] == [0.1, 0.5, 1.0, 2.0]
