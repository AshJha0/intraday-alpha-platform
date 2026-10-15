"""v1.17 WS-C: C2 impact term, C1 exposure builder + cross-sectional book, C7 report."""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from iap.backtest.costs import CostModel
from iap.portfolio.exposures import (
    SECTOR_UNKNOWN,
    build_exposure_matrix,
    load_sectors,
)
from iap.portfolio.optimizer import Constraints, ImpactTerm, objective, prox_power, solve
from iap.research import breadth_book as bb
from iap.research.breadth_report import (
    breadth_report,
    breakeven_notional,
    cluster_mean,
    render_markdown,
    sanitise,
)
from iap.validation.ledger import ExperimentLedger

NS_S = 1_000_000_000

# ------------------------------------------------------------- C2 --


def test_prox_power_first_order_condition_and_closed_forms():
    s = np.array([0.0, 0.3, 1.0, 2.5])
    for k in (0.5, 1.0, 0.7):
        c = np.array([0.0, 0.2, 1.0, 3.0])
        x = prox_power(s, c, k)
        assert np.all(x >= 0) and np.all(x <= s + 1e-15)
        # x + c (1 + k) x^k = s wherever c > 0 (the stationarity condition)
        res = x + c * (1 + k) * x**k - s
        np.testing.assert_allclose(res, 0.0, atol=1e-9)
        assert x[0] == 0.0
    # c = 0 is the identity
    np.testing.assert_array_equal(prox_power(s, np.zeros(4), 0.5), s)
    # closed forms agree with the bisection route (force it with k != 0.5, 1)
    x_bis = prox_power(s, np.full(4, 0.4), 0.5 + 1e-12)
    np.testing.assert_allclose(prox_power(s, np.full(4, 0.4), 0.5), x_bis, atol=1e-8)


def test_impact_term_units_from_cost_model_and_fit():
    cm = CostModel(2.0, 0.003, 2.5)  # sqrt default, 100 bps at one ADV
    adv_notional = np.array([1e8, 4e8])
    term = ImpactTerm.from_cost_model(cm, 1e6, adv_notional)
    assert term.exponent == 0.5
    # kappa * d^1.5 == impact_bps(participation) * 1e-4 * d, participation = d * N / ADV
    d = 0.05
    for i in range(2):
        part = d * 1e6 / adv_notional[i]
        expect = cm.impact_bps(np.array([part]))[0] * 1e-4 * d
        assert term.kappa[i] * d**1.5 == pytest.approx(expect, rel=1e-12)
    lin = ImpactTerm.from_cost_model(cm.with_linear_impact(), 1e6, adv_notional)
    assert lin.exponent == 1.0
    part = d * 1e6 / adv_notional[0]
    assert lin.kappa[0] * d**2 == pytest.approx(2.0 * part * 100 * 1e-4 * d, rel=1e-12)
    # calibration units: impact_bps = coeff * (% ADV) ** k
    fit = ImpactTerm.from_impact_fit(3.0, 0.6, 1e6, adv_notional)
    pct = 100 * d * 1e6 / adv_notional[1]
    assert fit.kappa[1] * d**1.6 == pytest.approx(3.0 * pct**0.6 * 1e-4 * d, rel=1e-12)
    with pytest.raises(ValueError):
        ImpactTerm.from_cost_model(cm, 0.0, adv_notional)
    with pytest.raises(ValueError):
        ImpactTerm(kappa=np.array([1.0]), exponent=1.5).validate(1)
    with pytest.raises(ValueError):
        ImpactTerm(kappa=np.array([-1.0]), exponent=0.5).validate(1)


def _three_asset():
    alpha = np.array([0.02, -0.01, 0.005])
    Sigma = np.array([[0.04, 0.01, 0.0], [0.01, 0.03, 0.005], [0.0, 0.005, 0.02]])
    w_prev = np.array([0.1, 0.0, -0.1])
    tc = np.full(3, 1e-3)
    cons = Constraints(w_min=np.full(3, -0.5), w_max=np.full(3, 0.5))
    return alpha, Sigma, w_prev, tc, cons


def test_impact_term_lowers_turnover_and_pinned_path_unchanged():
    alpha, Sigma, w_prev, tc, cons = _three_asset()
    base = solve(alpha, Sigma, w_prev, 1.0, tc, cons)
    again = solve(alpha, Sigma, w_prev, 1.0, tc, cons, impact=None)
    np.testing.assert_array_equal(base.weights, again.weights)
    term = ImpactTerm(kappa=np.full(3, 0.05), exponent=0.5)
    with_imp = solve(alpha, Sigma, w_prev, 1.0, tc, cons, impact=term)
    assert with_imp.feasible
    t0 = np.abs(base.weights - w_prev).sum()
    t1 = np.abs(with_imp.weights - w_prev).sum()
    assert t1 < t0
    # the reported objective includes the term
    assert with_imp.objective == pytest.approx(
        objective(with_imp.weights, alpha, Sigma, w_prev, 1.0, tc, term)
    )
    bigger = solve(alpha, Sigma, w_prev, 1.0, tc, cons, impact=ImpactTerm(np.full(3, 0.5), 0.5))
    assert np.abs(bigger.weights - w_prev).sum() < t1


@pytest.mark.parametrize("exponent", [0.5, 1.0, 0.7])
def test_impact_optimum_matches_brute_force_grid(exponent):
    alpha, Sigma, w_prev, tc, cons = _three_asset()
    term = ImpactTerm(kappa=np.array([0.05, 0.02, 0.08]), exponent=exponent)
    res = solve(alpha, Sigma, w_prev, 1.0, tc, cons, iters=2000, impact=term)
    assert res.feasible
    g = np.linspace(-0.5, 0.5, 101)
    W = np.array(np.meshgrid(g, g, g, indexing="ij")).reshape(3, -1).T  # (M, 3)
    D = W - w_prev
    f = (
        W @ alpha
        - np.einsum("ij,jk,ik->i", W, Sigma, W)
        - np.abs(D) @ tc
        - np.abs(D) ** (1.0 + exponent) @ term.kappa
    )
    best = W[np.argmax(f)]
    assert res.objective >= f.max() - 1e-9
    assert np.max(np.abs(res.weights - best)) <= 0.01 + 1e-9  # within one grid cell


# --------------------------------------------------------- exposures --


def test_exposure_builder_rows_and_buckets():
    ids = [1, 2, 3, 4, 5]
    betas = {1: 1.2, 2: float("nan"), 3: 0.8, 4: 1.0}
    sectors = {1: "Tech", 2: "Tech", 3: "Energy", 4: "Fin", 5: "Fin"}
    e = build_exposure_matrix(ids, betas, sectors)
    assert e.labels == ["beta", "sector:Fin", "sector:Tech", f"sector:{SECTOR_UNKNOWN}"]
    np.testing.assert_array_equal(e.matrix[0], [1.2, 1.0, 0.8, 1.0, 1.0])  # missing -> 1
    assert e.missing_beta == [2, 5]
    assert e.sector_of[3] == SECTOR_UNKNOWN  # singleton Energy merged
    np.testing.assert_array_equal(e.matrix[1], [0, 0, 0, 1, 1])
    np.testing.assert_array_equal(e.bounds, 0.0)
    w = np.array([0.1, -0.1, 0.0, 0.05, -0.05])
    assert e.exposures(w)["sector:Fin"] == pytest.approx(0.0)
    assert e.exposures(w)["beta"] == pytest.approx(0.12 - 0.1 + 0.05 - 0.05)
    only_sectors = build_exposure_matrix(ids, None, sectors, beta_row=False, bound=0.02)
    assert only_sectors.labels[0].startswith("sector:") and only_sectors.bounds[0] == 0.02
    none = build_exposure_matrix(ids, None, None, beta_row=False, sector_rows=False)
    assert none.n_rows == 0 and none.matrix.shape == (0, 5)
    with pytest.raises(ValueError):
        build_exposure_matrix([1, 1], None, None)
    with pytest.raises(ValueError):
        build_exposure_matrix(ids, None, None, bound=-1.0)


def test_sector_taxonomy_covers_the_breadth_universe():
    doc = load_sectors()
    universe = json.loads(
        (bb._REPO / "research" / "breadth" / "universe.json").read_text(encoding="utf-8")
    )
    symbols = [r["symbol"] for r in universe["universe"]]
    assert set(symbols) <= set(doc["sectors"])
    assert doc["source"] and doc["as_of"] == universe["selection_date"]
    for r in universe["universe"]:
        if r["etp"]:
            assert doc["sectors"][r["symbol"]] == "ETF"


# ------------------------------------------------------------ book --


def _planted(
    seed: int = 7,
    n_names: int = 8,
    days: int = 2,
    strength: float = 1.5e-3,
    blocks_per_day: int = 30,
):
    """Per-name 10 s rows over ``days`` sessions; a planted cross-sectional
    signal in ``ret_log_1m_v1`` (constant over each 5-minute block) drives
    the NEXT block's return: r_block = strength * z + common + noise."""
    rng = np.random.default_rng(seed)
    step = 10 * NS_S
    block = 300 * NS_S
    rows_per_block = block // step
    day0 = 1548858600 * NS_S  # 2019-01-30 14:30 UTC
    frames = {}
    for i in range(1, n_names + 1):
        ts_all, mid_all, sig_all = [], [], []
        price = 50.0 + 5.0 * i
        for d in range(days):
            t0 = day0 + d * 86_400 * NS_S
            z_prev = 0.0
            for b in range(blocks_per_day):
                z = float(rng.standard_normal())
                # the grid point is the block's first row: its increment
                # belongs to the previous interval, the other rows' to this one
                drift = (strength * z + 2e-4 * rng.standard_normal()) / rows_per_block
                drift_prev = strength * z_prev / rows_per_block
                for j in range(rows_per_block):
                    t = t0 + b * block + j * step
                    price *= np.exp(
                        (drift_prev if j == 0 else drift) + 3e-5 * rng.standard_normal()
                    )
                    ts_all.append(t)
                    mid_all.append(price)
                    sig_all.append(z)
                z_prev = z
        n = len(ts_all)
        frames[i] = pd.DataFrame(
            {
                "exchange_ts": np.array(ts_all, dtype=np.int64),
                "mid_price_v1": np.array(mid_all),
                "spread_bps_v1": np.full(n, 2.0),
                "ret_log_1m_v1": np.array(sig_all),
                "ref_ret_1m_v1": np.zeros(n),
                "beta_w5m_v1": np.full(n, 0.8 + 0.05 * i),
            }
        )
    instruments = {
        i: bb.Instrument(i, f"N{i}", "EQUITY", 5e6, 100, 0.01) for i in range(1, n_names + 1)
    }
    return frames, instruments


def _planted_score(inputs, min_names):
    return bb.demean_studentise(inputs["ret_log_1m_v1"], min_names)


_SECTORS = {1: "A", 2: "A", 3: "A", 4: "A", 5: "B", 6: "B", 7: "B", 8: "B"}


def _cfg(**kw):
    base = dict(alpha_bps=10.0, notional=1e6, cov_init=6, participation_cap=0.05)
    base.update(kw)
    return bb.BookConfig(**base)


def test_planted_book_profitable_gross_neutral_and_identity_exact():
    frames, instruments = _planted()
    cm = CostModel(2.0, 0.003, 2.5)
    res = bb.run_book(frames, instruments, cm, _cfg(), sectors=_SECTORS, score_fn=_planted_score)
    s = res.summary()
    assert s["n_days"] == 2 and s["n_traded"] > 50
    assert s["n_infeasible"] == 0
    assert res.gross_pnl > 0.0, s
    assert res.gross_pnl > res.total_costs, s  # profitable net of the cost model it saw
    assert s["mean_rank_ic"] > 0.5
    # neutrality: dollar, beta, sector within tolerance at every rebalance
    assert s["max_abs_net_exposure"] < 1e-6
    assert s["max_abs_beta_exposure"] < 1e-6
    assert s["max_abs_sector_exposure"] < 1e-6
    assert res.exposure_labels == ["beta", "sector:A", "sector:B"]
    # the identity, two ways: totals, and reconstructed from positions/prices
    assert res.net_pnl == pytest.approx(res.gross_pnl - res.total_costs, abs=1e-9)
    q, px = res.positions, res.prices
    gross_recon = float(np.sum(q[:-1] * (px[1:] - px[:-1])))
    assert gross_recon == pytest.approx(res.gross_pnl, abs=1e-6)
    # flattened at every session end: the last rebalance of each day holds nothing
    for k, r in enumerate(res.records):
        if r.flattened:
            assert np.all(q[k] == 0.0)
    # every rebalance record satisfies the per-row identity
    for r in res.records:
        assert r.net_pnl == pytest.approx(
            r.gross_pnl - r.spread_cost - r.fee_cost - r.impact_cost, abs=1e-12
        )


def test_book_costs_scale_and_impact_lowers_turnover_at_size():
    frames, instruments = _planted()
    cm = CostModel(2.0, 0.003, 2.5)
    small = bb.run_book(frames, instruments, cm, _cfg(notional=1e6), score_fn=_planted_score)
    big = bb.run_book(frames, instruments, cm, _cfg(notional=8e6), score_fn=_planted_score)
    # sqrt impact grows faster than linearly in size; the optimizer trades less
    assert big.impact_cost / small.impact_cost > 8.0
    assert big.summary()["mean_turnover"] < small.summary()["mean_turnover"]
    none = bb.run_book(frames, instruments, cm, _cfg(impact="none"), score_fn=_planted_score)
    assert none.impact_cost == 0.0
    cal = bb.run_book(
        frames,
        instruments,
        cm,
        _cfg(impact="calibration", impact_coeff=5.0, impact_exponent=0.5),
        score_fn=_planted_score,
    )
    assert cal.impact_cost > 0.0
    with pytest.raises(ValueError):
        _cfg(impact="calibration").validate()


def test_rank_to_weights_maps():
    s = np.array([0.3, np.nan, -1.0, 2.0, 0.0])
    w = bb.rank_to_weights(s, "linear")
    assert w[1] == 0.0 and abs(np.abs(w).sum() - 1.0) < 1e-12 and abs(w.sum()) < 1e-12
    assert w[3] > w[0] > w[4] > w[2]
    d = bb.rank_to_weights(s, "deciles", decile_q=0.1)  # 4 names -> 1 a side
    assert d[3] == 0.5 and d[2] == -0.5 and d[0] == 0.0 and d[4] == 0.0
    assert np.all(bb.rank_to_weights(np.array([1.0, np.nan]), "linear") == 0.0)
    with pytest.raises(ValueError):
        bb.rank_to_weights(s, "nope")


def test_builtin_scores_shapes():
    inputs = {
        "ret_log_1m_v1": np.array([[1.0, 2.0], [0.0, 1.0], [-1.0, np.nan], [2.0, 0.0]]),
        "ref_ret_1m_v1": np.array([[0.5, 0.5]] * 4),
        "beta_w5m_v1": np.array([[1.0, np.nan]] * 4),
    }
    e11 = bb.score_eq11(inputs, 3)
    assert e11.shape == (4, 2) and np.isfinite(e11[:, 0]).all()
    assert e11[0, 0] < e11[2, 0]  # reversal: the winner is shorted
    gap = bb.score_eq10_beta_gap(inputs, 3)
    assert gap.shape == (4, 2) and np.isnan(gap[2, 1])
    assert gap[2, 0] > gap[3, 0]  # the laggard is long


# ---------------------------------------------------------- report --


def test_report_arithmetic_breakeven_and_cluster_mean():
    assert breakeven_notional([1, 2, 4], [10.0, 2.0, -6.0]) == {
        "notional": 2 + 2 * 2.0 / 8.0,
        "status": "crossed",
        "at": None,
    }
    assert breakeven_notional([1, 2], [-1.0, -2.0])["status"] == "below"
    assert breakeven_notional([1, 2], [1.0, 2.0])["status"] == "above"
    with pytest.raises(ValueError):
        breakeven_notional([2, 1], [1.0, -1.0])
    # CR1 by hand: two clusters
    x = [1.0, 3.0, 2.0, 6.0]
    c = [0, 0, 1, 1]
    r = cluster_mean(x, c)
    mean = 3.0
    s = np.array([(1 - mean) + (3 - mean), (2 - mean) + (6 - mean)])
    se = np.sqrt(2 / 1 * np.sum(s**2) / 16)
    assert r["mean"] == mean and r["se"] == pytest.approx(se) and r["df"] == 1
    assert r["t"] == pytest.approx(mean / se)
    one = cluster_mean([1.0, 2.0], [0, 0])
    assert one["se"] is None and one["n_clusters"] == 1
    assert sanitise({"a": float("nan"), "b": np.float64(1.5), "c": [np.int64(2)]}) == {
        "a": None,
        "b": 1.5,
        "c": [2],
    }


def test_breadth_report_joins_book_and_sizes():
    frames, instruments = _planted()
    cm = CostModel(2.0, 0.003, 2.5)
    cfg = _cfg()
    base = bb.run_book(frames, instruments, cm, cfg, sectors=_SECTORS, score_fn=_planted_score)

    def runner(notional):
        return bb.run_book(
            frames,
            instruments,
            cm,
            replace(cfg, notional=notional),
            sectors=_SECTORS,
            score_fn=_planted_score,
        )

    rep = breadth_report(base, runner, (0.5, 1.0, 2.0), cm, instruments)
    assert [a["size"] for a in rep["at_size"]] == [0.5, 1.0, 2.0]
    assert rep["at_size"][1]["net_pnl"] == base.net_pnl  # size 1 is the base run
    assert rep["identity"]["net_minus_gross_plus_costs"] == pytest.approx(0.0, abs=1e-9)
    b = rep["breadth"]
    assert b["n_names"] == 8 and 1.0 <= b["effective_bets"]["n_effective"] <= 8.0
    assert b["arithmetic"]["ir_implied"] > 0
    assert rep["sharpe"]["n_days"] == 2 and rep["sharpe"]["net_daily_annualised"] is not None
    assert rep["net_pnl_day_clustered"]["df"] == 1
    assert rep["breakeven"]["status"] in ("crossed", "above", "below")
    assert rep["capacity"]["breakeven"] is not None
    md = render_markdown({**rep, "tag": "test", "dataset": {"smoke_test": True}})
    assert "Smoke test" in md and "Net Sharpe at size" in md
    json.dumps(sanitise(rep), allow_nan=False)


def test_cli_end_to_end_records_one_ledger_look(tmp_path):
    frames, instruments = _planted(n_names=6)
    fdir = tmp_path / "features"
    fdir.mkdir()
    for i, df in frames.items():
        df.to_parquet(fdir / f"features_{i}.parquet", index=False)
    cdir = tmp_path / "configs"
    (cdir / "instruments").mkdir(parents=True)
    (cdir / "instruments" / "instruments.json").write_text(
        json.dumps(
            {
                "instruments": [
                    {
                        "instrument_id": i,
                        "symbol": inst.symbol,
                        "asset_class": "EQUITY",
                        "adv": inst.adv_shares,
                        "lot_size": 100,
                        "tick_size": 0.01,
                    }
                    for i, inst in instruments.items()
                ]
                + [{"instrument_id": 101, "symbol": "EUR/USD", "asset_class": "FX", "adv": 1e9}]
            }
        )
    )
    ledger = tmp_path / "ledger.json"
    out = tmp_path / "out"
    args = [
        "--features-dir",
        str(fdir),
        "--config-dir",
        str(cdir),
        "--out",
        str(out),
        "--tag",
        "t",
        "--no-sector-neutral",
        "--sizes",
        "1,2",
        "--ledger",
        str(ledger),
        "--no-timestamp",
        "--max-rebalances",
        "40",
    ]
    assert bb.main(args) == 0
    doc = json.loads((out / "book_t.json").read_text())
    assert doc["book"]["n_names"] == 6 and doc["ledger"]["kind"] == "breadth_book"
    led = ExperimentLedger(ledger)
    assert led.total_experiments == 1 and led.entries[0]["kind"] == "breadth_book"
    assert bb.main(args) == 0  # a rerun of the same identity is not a new look
    assert ExperimentLedger(ledger).total_experiments == 1
    assert (out / "BOOK_t.md").read_text().startswith("# Breadth book")
    # byte-identical reruns
    first = (out / "book_t.json").read_bytes()
    assert bb.main(args) == 0
    assert (out / "book_t.json").read_bytes() == first
    assert bb.main(["--dataset-dir", str(tmp_path / "x_holdout_y")]) == 2
