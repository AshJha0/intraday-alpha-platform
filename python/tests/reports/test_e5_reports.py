"""EN5 (v2.0 stage 5): the E5 blocks carry units, counts and uncertainty; the prohibited-inflation rules are tests."""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.contracts.versions import canonical_json
from iap.reports import (
    InflationError,
    SessionRow,
    Table,
    block_a_signal,
    block_b_portfolio,
    block_c_execution,
    block_d_risk_adjusted,
    block_e_sensitivity,
    check_no_inflation,
    decision_tree,
    economic_screen,
    retention,
    session_series,
)

C = 1_000_000.0


def rows(n=30, seed=3):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        if i == 10:
            out.append(SessionRow(f"s{i}", "coverage_failure"))
        elif i % 7 == 3:
            out.append(SessionRow(f"s{i}", "usable_no_trades"))
        elif i == 20:
            out.append(
                SessionRow(f"s{i}", "unpriced_at_close", -300.0, 100.0, ("halt through the close",))
            )
        else:
            g = float(rng.normal(250, 400))
            out.append(SessionRow(f"s{i}", "traded", g - 150.0, g))
    return out


def test_session_series_counts_the_three_kinds_and_divides_by_C():
    rs = rows()
    s = session_series("h1", "15m", rs, C)
    assert s.counts["coverage_failure"] == 1 and s.dropped == ["s10"]
    assert s.counts["usable_no_trades"] == 3 and s.counts["unpriced_at_close"] == 1
    assert s.n == 29 and all(
        r == 0.0
        for r, sid in zip(s.r_bps, s.session_ids, strict=True)
        if sid in {"s3", "s17", "s24"}
    )
    assert s.flags["s20"] == ("halt through the close",)
    assert s.r_bps[0] == pytest.approx(1e4 * rs[0].net_pnl / C)
    check_no_inflation(s, all_session_ids=[r.session_id for r in rs], denominators=[C, C])


def test_prohibited_inflation_checks():
    rs = rows()
    s = session_series("h1", "15m", rs, C)
    with pytest.raises(InflationError, match="omitted"):
        check_no_inflation(s, all_session_ids=[r.session_id for r in rs] + ["s99"])
    with pytest.raises(InflationError, match="credited"):
        check_no_inflation(
            s, all_session_ids=[r.session_id for r in rs], refused_orders_credited=[object()]
        )
    with pytest.raises(InflationError, match="denominator"):
        check_no_inflation(s, all_session_ids=[r.session_id for r in rs], denominators=[C, 2 * C])
    with pytest.raises(InflationError, match="annualisation"):
        check_no_inflation(
            s, all_session_ids=[r.session_id for r in rs], annualised_over_active_only=True
        )
    with pytest.raises(InflationError):
        SessionRow("x", "usable_no_trades", net_pnl=5.0)  # a no-trade session cannot carry P&L
    with pytest.raises(InflationError):
        SessionRow("x", "unpriced_at_close", net_pnl=-5.0)  # needs its flag
    with pytest.raises(InflationError):
        SessionRow("x", "active_only")
    with pytest.raises(InflationError):
        session_series("h1", "15m", [SessionRow("a", "traded"), SessionRow("a", "traded")], C)
    with pytest.raises(InflationError):
        session_series("h1", "15m", rs, 0.0)
    # retention is undefined (the screen fails) when the aggregate gross is not positive
    assert (
        retention(10.0, 0.0) is None
        and retention(10.0, -5.0) is None
        and retention(5.0, 10.0) == 0.5
    )
    neg = session_series("h1", "15m", [SessionRow("a", "traded", 10.0, -20.0)], C)
    scr = economic_screen(neg)
    assert scr["retention"] is None and not scr["passed"] and not scr["retention_defined"]


def test_blocks_carry_units_counts_and_uncertainty_and_serialise():
    rs = rows()
    s = session_series("h1", "15m", rs, C)
    rng = np.random.default_rng(1)
    z = rng.standard_normal(500) * 1.5
    fwd = {"15m": -1.5 * z + rng.standard_normal(500), "30m": -0.5 * z + rng.standard_normal(500)}
    a = block_a_signal(
        z,
        fwd,
        decay_bps={5: [0.3], 15: [0.8], 30: [0.4]},
        mu_hat_bps={"15m": -1.5 * z},
        coverage=0.91,
        quote_age_buckets={"<1s": [1.0], "1-5s": [0.5]},
    )
    assert a.rows[0]["ic"] > 0.5 and a.rows[0]["rank_ic"] > 0.5 and a.rows[0]["n"] == 500
    assert a.rows[0]["calibration"]["slope"] == pytest.approx(1.0, abs=0.1)
    assert a.rows[0]["long_leg_alpha_bps"] > 0 and a.rows[0]["short_leg_alpha_bps"] > 0
    assert set(a.rows[0]["by_tail_bin_bps"]) == {"2-2.5", "2.5-3", ">3"}
    names = [f"N{i}" for i in range(6)]
    w = {n: (0.01 if i < 3 else -0.01) for i, n in enumerate(names)}
    stage_books = {
        "raw": {n: 2 * x for n, x in w.items()},
        "name_and_gross_capped": w,
        "participation_limited": {**w, "N0": 0.005},
        "executable": {**w, "N0": 0.005},
    }
    b = block_b_portfolio(
        stage_books,
        {n: 5.0 for n in names},
        {n: 2.0 for n in names},
        {n: 1.0 for n in names},
        {n: ("a" if i < 3 else "b") for i, n in enumerate(names)},
        series=s,
        deployed_gross=[0.3 * C] * 10,
        turnover_per_rebalance=[0.1] * 10,
        refusals={"deployed gross below 50 %": 2},
    )
    summary = b.rows[-1]["summary"]
    assert summary["return_lost_to_liquidity_bps_of_C"] == pytest.approx(5.0 * 0.005)
    assert (
        summary["capital_utilisation_mean"] == pytest.approx(0.3)
        and summary["R_gross_bps_of_deployed"] is not None
    )
    assert [r["stage"] for r in b.rows[:-1]] == [
        "raw",
        "name_and_gross_capped",
        "participation_limited",
        "executable",
    ]
    wfs = [
        {
            "midpoint_reference_pnl": 300.0,
            "latency_drift": -10.0,
            "half_spread": 60.0,
            "impact": 40.0,
            "fees": 20.0,
            "execution_residual": 5.0,
            "financing": 0.0,
        }
    ] * 5
    c = block_c_execution(
        wfs,
        capital=C,
        exit_cost=[50.0] * 5,
        refusals={"liquidity": (1000, 1.5)},
        cushions_bps=[0.2, 1.0, 3.0],
        columns_pnl={"base": 900.0, "conservative": 875.0, "stress": 800.0},
        capacity={0.5: {"refused_share": 0.0}, 5.0: {"refused_share": 0.3}},
    )
    three = c.rows[0]["three_return_measurements_bps_of_C"]
    assert three["mid_to_mid"] == pytest.approx(15.0) and three[
        "executable_conservative"
    ] == pytest.approx(8.75)
    assert three["statistical_alpha_lost_in_execution"] == pytest.approx(6.25)
    assert c.rows[1]["waterfall_lines_bps_of_C"]["slippage"].startswith("no separate term")
    assert c.rows[4]["cost_cushion_B"]["share_below_0_5_bps"] == pytest.approx(1 / 3)
    d = block_d_risk_adjusted(s, bootstrap=200)
    row = d.rows[0]
    assert math.isfinite(row["net_session_sharpe"]) and len(row["sharpe_block_bootstrap_95"]) == 2
    assert row["sessions"] == 29 and "block bootstrap" in row["dependence_assumption"]
    assert d.units["sharpe"] == "per session (not annualised)"
    assert (
        block_d_risk_adjusted(s, bootstrap=200).rows[0]["sharpe_block_bootstrap_95"]
        == row["sharpe_block_bootstrap_95"]
    )  # seeded
    e = block_e_sensitivity(wfs, capital=C, series=s)
    assert e.rows[0]["gross_to_net_waterfall_bps_per_session"]["net"] == pytest.approx(1.75)
    assert e.rows[1]["arithmetic_scenarios"]["break_even_gross_alpha_change_bps"] == pytest.approx(
        -1.75
    )
    for t in (a, b, c, d, e):
        assert isinstance(t, Table) and t.units and t.counts
        assert "uncertainty" in t.as_dict()
        canonical_json(_clean(t.as_dict()))  # serialisable (NaN-free after cleaning)


def _clean(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    return obj


def test_economic_screen_and_the_six_branch_decision_tree():
    good = session_series(
        "h1",
        "15m",
        [SessionRow(f"s{i}", "traded", 200.0 + (i % 3) * 10, 400.0) for i in range(40)],
        C,
    )
    scr = economic_screen(good)
    assert (
        scr["passed"]
        and scr["mean_bps"] >= 1.0
        and scr["retention"] >= 0.5
        and scr["interval_95_bps"] is not None
    )
    weak = session_series(
        "h1", "15m", [SessionRow(f"s{i}", "traded", 50.0, 400.0) for i in range(40)], C
    )
    assert not economic_screen(weak)["passed"]
    assert (
        decision_tree(
            e3_red=True, predictability=True, screen_passed=True, powered=True, x0_eligible=True
        )["branch"]
        == 1
    )
    assert (
        decision_tree(
            e3_red=False, predictability=False, screen_passed=True, powered=True, x0_eligible=True
        )["verdict"]
        == "not viable"
    )
    b3 = decision_tree(
        e3_red=False,
        predictability=True,
        screen_passed=False,
        powered=True,
        x0_eligible=True,
        attribution="spread",
    )
    assert b3["branch"] == 3 and b3["loss_attributed_to"] == "spread"
    assert (
        "GREEN-REGISTER-ONLY"
        in decision_tree(
            e3_red=False, predictability=True, screen_passed=True, powered=False, x0_eligible=None
        )["outcome"]
    )
    assert (
        decision_tree(
            e3_red=False, predictability=True, screen_passed=True, powered=True, x0_eligible=False
        )["branch"]
        == 5
    )
    assert (
        decision_tree(
            e3_red=False, predictability=True, screen_passed=True, powered=True, x0_eligible=True
        )["branch"]
        == 6
    )
