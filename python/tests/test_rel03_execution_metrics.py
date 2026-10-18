"""v1.18 REL-03: execution benchmark v2 metrics (cost per filled share, fill /
completion / residual, markouts, slices, MDE + ESS, 16-cell coverage).
Synthetic and golden data only."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest
from iap.core.rng import SplitMix64
from iap.execution import AlgoType
from iap.research import execution_benchmark as bench
from iap.research import execution_metrics as xm
from iap.research.study_stats import mde

REPO = Path(__file__).resolve().parents[2]
SEC = 1_000_000_000


def _row(session, qty, filled, mid=10.0, trading=0.0, fees=0.0, impact=0.0, completion=0.0):
    r = {
        "session": session,
        "qty": qty,
        "filled_qty": filled,
        "decision_mid": mid,
        "trading": trading,
        "fees_net": fees,
        "sim_impact": impact,
        "completion": completion,
        "shortfall": trading + (qty - filled) * 0.01,
    }
    for k, v in xm.parent_metrics(r).items():
        r[f"m_{k}"] = v
    return r


# ------------------------------------------------------------- per parent --


def test_cost_per_filled_share_uses_the_filled_notional_not_the_target():
    # 400 of 1000 filled at mid 10; trading cost 2.0 currency over the 400 shares
    m = xm.parent_metrics(_row("a", 1000, 400, trading=2.0, fees=0.4, impact=0.0))
    # (2.0 + 0.4) / (400 * 10) = 6 bps per filled share
    assert m["cost_per_filled_share_bps"] == pytest.approx(6.0)
    assert m["trading_cost_per_filled_share_bps"] == pytest.approx(5.0)
    assert m["residual_qty"] == 600 and m["completed"] == 0.0
    assert m["fill_rate"] == pytest.approx(0.4)
    # the target-notional denominator of v1 would have shown 2.4 / 10000 = 2.4 bps
    assert 1e4 * 2.4 / (1000 * 10.0) == pytest.approx(2.4)


def test_unfilled_parent_has_no_per_share_cost_and_a_full_residual():
    m = xm.parent_metrics(_row("a", 1000, 0, completion=3.0))
    assert m["cost_per_filled_share_bps"] is None
    assert m["residual_qty"] == 1000 and m["fill_rate"] == 0.0
    # completion charge per residual share: 3.0 / (1000 * 10)
    assert m["completion_charge_bps"] == pytest.approx(3.0)


# ------------------------------------------------------------- aggregates --


def test_ratio_stats_hand_example_cr1_ess_and_mde():
    # two sessions, two parents each: cost 1 and 3 on 100 shares each => 2.0 pooled
    num = [100.0, 300.0, 200.0, 200.0]
    den = [100.0, 100.0, 100.0, 100.0]
    s = xm.ratio_stats(num, den, ["a", "a", "b", "b"])
    assert s["estimate"] == pytest.approx(2.0)
    # residuals: a -> (-100+100)=0, b -> 0 ... equal cluster means => zero SE
    assert s["se"] == pytest.approx(0.0, abs=1e-12)
    # unequal clusters: a = 1.0, b = 3.0 pooled 2.0; residual sums -/+ 200; CR1 = 2/1 * 80000 / 400**2
    s = xm.ratio_stats([100.0, 100.0, 300.0, 300.0], [100.0] * 4, ["a", "a", "b", "b"])
    assert s["estimate"] == pytest.approx(2.0)
    assert s["se"] == pytest.approx(math.sqrt(2.0 * 80_000.0 / 400.0**2))
    assert s["df"] == 1 and s["n_clusters"] == 2 and s["n"] == 4
    assert s["mde"] == pytest.approx(mde(s["se"] * math.sqrt(2), 2))
    # Student-t at 1 df is much wider than the normal approximation
    assert s["mde_t"] > s["mde"]
    assert s["ess"] is not None and s["design_effect"] > 0


def test_one_cluster_has_a_point_estimate_and_no_interval():
    s = xm.ratio_stats([1.0, 2.0], [1.0, 1.0], ["a", "a"])
    assert s["estimate"] == pytest.approx(1.5)
    assert s["se"] is None and s["ci95"] is None and s["mde"] is None


def test_quantity_weighted_aggregate_differs_from_the_per_parent_mean():
    # a large parent that fills cheaply and a small one that fills dearly
    rows = [
        _row("a", 10_000, 10_000, trading=1.0),  # 0.1 bps on 100k notional
        _row("b", 100, 100, trading=1.0),  # 10 bps on 1k notional
    ]
    m = xm.aggregate(rows)
    pooled = m["cost_per_filled_share_bps"]["estimate"]
    per_parent_mean = m["per_parent"]["cost_per_filled_share_bps"]["mean"]
    assert pooled == pytest.approx(1e4 * 2.0 / 101_000.0)
    assert per_parent_mean == pytest.approx((1.0 + 100.0) / 2 * 0.1)
    assert pooled < per_parent_mean
    assert m["completion_ratio"]["estimate"] == 1.0
    assert m["per_parent"]["parents_with_no_fill"] == 0


def test_incomplete_parents_report_residual_and_never_a_fill():
    rows = [_row("a", 1000, 250, trading=0.5, completion=1.5), _row("b", 1000, 0, completion=3.0)]
    m = xm.aggregate(rows)
    assert m["fill_rate"]["estimate"] == pytest.approx(250 / 2000)
    assert m["completion_ratio"]["estimate"] == 0.0
    assert m["residual_qty"]["total"] == 1750 and m["residual_qty"]["share_of_target"] == 0.875
    assert m["per_parent"]["parents_with_no_fill"] == 1
    # the parent with no fill contributes no per-share cost
    assert m["per_parent"]["cost_per_filled_share_bps"]["n"] == 1


def test_post_trade_markout_is_quantity_weighted_and_excludes_undefined():
    r1, r2 = _row("a", 100, 100), _row("b", 100, 100)
    r1["markout_w"] = {"1s": [100 * 2.0, 100.0]}  # 2 bps adverse on 100 shares
    r2["markout_w"] = {"1s": [100 * 4.0, 100.0]}
    m = xm.aggregate([r1, r2])
    assert m["post_trade_markout_bps"]["1s"]["estimate"] == pytest.approx(3.0)


def test_slices_and_labels():
    assert xm.size_label(500, 100_000) == pytest.approx(0.5)
    assert xm.size_label(500, None) is None
    assert xm.liquidity_bucket(5e6, [1e6, 1e7]) == "L1"
    assert xm.spread_regime(0.5) == "tight"  # 1-tick spread
    assert xm.spread_regime(1.0) == "normal"
    assert xm.spread_regime(5.0) == "wide"
    rows = [_row("a", 100, 100), _row("b", 100, 100)]
    rows[0]["liquidity"], rows[1]["liquidity"] = "L0", "L1"
    out = xm.split_by(rows, "liquidity")
    assert sorted(out) == ["L0", "L1"] and out["L0"]["n_parents"] == 1


def test_every_metric_states_the_seven_conventions():
    keys = {"arrival_price", "target_qty", "horizon", "benchmark", "sign", "unit", "denominator"}
    for name, d in xm.METRIC_DEFINITIONS.items():
        assert set(d) == keys, name
        assert all(d[k] for k in keys), name
    assert "cost_per_filled_share_bps" in xm.definitions_markdown()


# ----------------------------------------------------------- 16-cell grid --


def _starts(passes):
    return [p.start_ts for ps in passes for p, _ in ps]


def test_the_v117_rotation_never_runs_two_cells_of_the_default_grid():
    """Review X-E: 12 windows for 16 cells leaves 4 cells unrun on a day."""
    day = int(6.5 * 3600 * SEC)
    sizes = (0.1, 0.5, 1.0, 2.0)
    algos = (AlgoType.TWAP, AlgoType.VWAP, AlgoType.POV, AlgoType.IS)
    kw = dict(window_ns=30 * 60 * SEC, warmup_ns=5 * 60 * SEC, slices=5, participation=0.1,
              max_child_qty=1000, urgency=0.5)  # fmt: skip
    one = bench.make_parent_passes(0, day, 1, 1e6, sizes, algos, 0, SplitMix64(1), **kw)
    assert len(one) == 1
    ran = {(round(pct, 3), p.algo) for p, pct in one[0]}
    assert len(one[0]) == 12 and len(ran) == 12  # 4 of 16 cells absent
    cover = bench.make_parent_passes(
        0, day, 1, 1e6, sizes, algos, 0, SplitMix64(1), cover_all_cells=True, **kw
    )
    assert len(cover) >= 2
    cells = {(round(pct, 3), p.algo) for ps in cover for p, pct in ps}
    assert len(cells) == 16
    ids = [p.parent_id for ps in cover for p, _ in ps]
    assert len(ids) == len(set(ids))  # unique across passes
    # passes overlap in time but each pass is back-to-back inside the session
    for ps in cover:
        starts = [p.start_ts for p, _ in ps]
        assert all(b - a == 30 * 60 * SEC for a, b in zip(starts, starts[1:], strict=False))
    # the default path is the v1.17 list, unchanged
    legacy = bench.make_parents(0, day, 1, 1e6, sizes, algos, 0, SplitMix64(1), **kw)
    assert [(p.start_ts, p.algo, p.qty, p.side) for p, _ in legacy] == [
        (p.start_ts, p.algo, p.qty, p.side) for p, _ in one[0]
    ]


# --------------------------------------------------------------- driver --


@pytest.fixture(scope="module")
def dataset(tmp_path_factory, golden_dir) -> Path:
    root = tmp_path_factory.mktemp("rel03ds")
    norm = root / "normalized"
    norm.mkdir()
    for day in ("20190101", "20190102"):
        shutil.copy(golden_dir / "events_eq_mbo.jsonl", norm / f"eq_{day}.normalized.jsonl")
    return root


def _run(dataset, **kw):
    return bench.run_benchmark(
        dataset,
        config_dir=REPO / "configs",
        sizes_pct=(0.0005, 0.002),
        algos=("TWAP", "POV"),
        policies=("native",),
        baseline_policy="native",
        window_min=1.5,
        warmup_min=0.5,
        timestamp=False,
        **kw,
    )


def test_cells_carry_metrics_v2_with_n_ess_and_mde_beside_every_interval(dataset):
    doc = _run(dataset)
    assert doc["metrics_schema"] == "iap.execution_metrics/v2"
    assert "cost_per_filled_share_bps" in doc["metric_definitions"]
    for key, cell in doc["cells"].items():
        m = cell["metrics_v2"]
        assert m["n_parents"] == cell["n_parents"]
        c = m["cost_per_filled_share_bps"]
        for k in ("n", "n_clusters", "ess", "mde", "mde_t", "design_effect"):
            assert k in c, (key, k)
        assert m["fill_rate"]["estimate"] == pytest.approx(cell["fill_rate"])
        assert {
            "by_spread_regime",
            "by_time_of_day",
            "by_size_pct_adv",
            "by_liquidity_bucket",
        } <= set(doc["metrics_v2_slices"]["native"])
    md = bench.render(doc)
    assert "Metrics v2" in md and "ESS" in md and "MDE" in md and "Metric definitions" in md


def test_default_run_reports_missing_cells_and_cover_all_cells_fills_them(dataset):
    base = _run(dataset)
    miss = {lab: c["cells_missing"] for lab, c in base["coverage_by_session"].items()}
    cover = _run(dataset, cover_all_cells=True)
    for lab, c in cover["coverage_by_session"].items():
        assert len(c["cells_missing"]) <= len(miss[lab])
    assert cover["grid"]["cover_all_cells"] is True
    # per-cell n is reported for every session
    for c in cover["coverage_by_session"].values():
        assert c["n_parents_by_cell"]


def test_driver_with_v2_metrics_is_deterministic(dataset):
    a = json.dumps(_run(dataset, cover_all_cells=True), sort_keys=True)
    b = json.dumps(_run(dataset, cover_all_cells=True), sort_keys=True)
    assert a == b


def test_holdout_check_is_one_function():
    with pytest.raises(SystemExit, match="holdout"):
        bench.check_data_access(Path("x/r7_holdout"))
    bench.check_data_access(Path("x/r7_holdout"), allow_holdout=True)
