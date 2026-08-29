"""Research backtester tests: accounting identity, no-lookahead execution,
cost-model hand calculations, determinism (spec §18)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from iap.backtest import Backtester, BacktestConfig, CostModel, ensemble_scores

from conftest import CONFIGS_DIR

NS_S = 1_000_000_000

META = {
    1: {"tick_size": 0.01, "lot_size": 100, "adv": 1_000_000.0,
        "asset_class": "EQUITY", "ref_price": 25.0},
    101: {"tick_size": 1e-05, "lot_size": 1000, "adv": 4_000_000_000.0,
          "asset_class": "FX", "ref_price": 1.1},
}


def _cost_model(multiplier=1.0):
    return CostModel(
        impact_coeff_bps_per_pct_adv=2.0,
        equity_taker_fee_per_share=0.003,
        fx_commission_per_million=2.5,
        multiplier=multiplier,
    )


def _frame(mids, spreads_ticks=2.0, iid=1, step_ns=NS_S):
    n = len(mids)
    return pd.DataFrame(
        {
            "exchange_ts": np.arange(n, dtype=np.int64) * step_ns + NS_S,
            "mid_price_v1": np.asarray(mids, dtype=float),
            "spread_ticks_v1": np.full(n, spreads_ticks, dtype=float),
        }
    )


def _scores(er, conf=None):
    n = len(er)
    return pd.DataFrame(
        {
            "exchange_ts": np.arange(n, dtype=np.int64) * NS_S + NS_S,
            "expected_return": np.asarray(er, dtype=float),
            "confidence": np.ones(n) if conf is None else np.asarray(conf, float),
        }
    )


def test_cost_model_loads_from_execution_config():
    cm = CostModel.load(CONFIGS_DIR / "execution.json")
    assert cm.impact_coeff_bps_per_pct_adv == 2.0
    assert cm.equity_taker_fee_per_share == 0.003
    assert cm.fx_commission_per_million == 2.5
    assert cm.multiplier == 1.0


def test_cost_components_hand_calc_equity():
    cm = _cost_model()
    q = np.array([1000.0])
    mid = np.array([25.0])
    hs = np.array([0.01])
    c = cm.cost_components(q, mid, hs, "EQUITY", adv=1_000_000.0, lot_size=100)
    assert abs(c["spread"][0] - 1000 * 0.01) < 1e-12
    assert abs(c["fee"][0] - 1000 * 0.003) < 1e-12
    # impact: 2 bps per 1% ADV; 1000/1e6 = 0.1% ADV -> 0.2 bps on 25000 notional
    assert abs(c["impact"][0] - (2.0 * 0.1) * 1e-4 * 1000 * 25.0) < 1e-12


def test_cost_components_hand_calc_fx_and_multiplier():
    cm = _cost_model(multiplier=2.0)
    q = np.array([-500.0])
    mid = np.array([1.1])
    hs = np.array([2e-5])
    c = cm.cost_components(q, mid, hs, "FX", adv=4e9, lot_size=1000)
    # x1 values (FX qty unit = 1000 base ccy), then doubled by the multiplier
    spread1 = 500 * 1000 * 2e-5
    fee1 = 500 * 1000 * 1.1 * 2.5 / 1e6
    impact1 = (2.0 * (500 * 1000 / 4e9 * 100)) * 1e-4 * 500 * 1000 * 1.1
    assert abs(c["spread"][0] - 2 * spread1) < 1e-12
    assert abs(c["fee"][0] - 2 * fee1) < 1e-12
    assert abs(c["impact"][0] - 2 * impact1) < 1e-15


def test_fx_unit_scaling_and_identity():
    """FX P&L is in real quote currency: 1 qty unit = lot_size base ccy."""
    mids = [1.1000, 1.1002, 1.1001, 1.1004, 1.1003, 1.1005]
    er = [1e-4, 1e-4, -1e-4, -1e-4, 1e-4, 0.0]
    f = _frame(mids, spreads_ticks=2.0, iid=101)
    s = _scores(er)
    bt = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=100))
    res = bt.run_instrument(101, f, s)
    # long 100 units from row 1 (mid 1.1002) -> real notional 100*1000*mid
    # first marked move: 1.1002 -> 1.1001 on 100 units*1000 = -10 currency
    assert res.trade_count >= 2
    assert abs(res.total_pnl - (res.gross_pnl - res.total_costs)) < 1e-9
    # gross must be lot-scaled: sum(pos * 1000 * dmid)
    pos = res.positions
    hand_gross = float(np.sum(pos[:-1] * 1000 * np.diff(np.asarray(mids))))
    assert abs(res.gross_pnl - hand_gross) < 1e-9


def test_cost_model_rejects_bad_inputs():
    cm = _cost_model()
    with pytest.raises(ValueError):
        cm.cost_components(np.ones(1), np.ones(1), np.ones(1), "BOND", 1e6, 100)
    with pytest.raises(ValueError):
        cm.cost_components(np.ones(1), np.ones(1), np.ones(1), "EQUITY", 0.0, 100)


def test_decision_t_executes_t_plus_one():
    """No-lookahead pin: a single decision at row 3 trades at row 4."""
    mids = [10.0, 10.0, 10.0, 10.0, 12.0, 12.0, 12.0]
    conf = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    er = [0.0, 0.0, 0.0, 1e-3, 0.0, 0.0, 0.0]
    bt = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=100))
    res = bt.run_instrument(1, _frame(mids), _scores(er, conf))
    pos = res.positions
    assert pos[3] == 0.0, "decision row must NOT hold the new position yet"
    assert pos[4] == 100.0, "execution must land exactly one row later"
    # buy executed at row 4 (mid 12): the 10->12 move is NOT captured
    assert res.gross_pnl == 0.0
    # follow-up decisions flat -> position exits at row 5 (mid 12): flat P&L
    assert res.trade_count == 2


def test_latency_zero_captures_what_latency_one_misses():
    mids = [10.0, 11.0, 12.0, 13.0, 14.0]
    er = [1e-3] * 5
    f, s = _frame(mids), _scores(er)
    bt0 = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=10, latency_rows=0))
    bt1 = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=10, latency_rows=1))
    g0 = bt0.run_instrument(1, f, s).gross_pnl
    g1 = bt1.run_instrument(1, f, s).gross_pnl
    assert abs(g0 - 10 * 4.0) < 1e-9   # rides 10 -> 14 from row 0
    assert abs(g1 - 10 * 3.0) < 1e-9   # enters one row late


def test_accounting_identity_exact():
    """equity_end == sum(pos * dmid) - total_costs on a churny path."""
    n = 400
    # deterministic pseudo-path (no shared-RNG requirements in tests)
    mids = 20.0 + 0.01 * np.round(10 * np.sin(np.arange(n) * 0.7))
    er = 1e-3 * np.sin(np.arange(n) * 1.3)
    conf = np.abs(np.cos(np.arange(n) * 0.9))
    bt = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=700, conf_min=0.4))
    res = bt.run_instrument(1, _frame(mids), _scores(er, conf))
    assert res.trade_count > 20
    ident = res.gross_pnl - res.total_costs
    assert abs(res.total_pnl - ident) < 1e-9
    assert abs(res.total_costs - (res.spread_cost + res.fee_cost + res.impact_cost)) < 1e-9
    # equity series consistent with its own final value
    assert abs(res.equity[-1] - res.total_pnl) < 1e-12


def test_invalid_mid_rows_carry_position():
    mids = [10.0, 10.0, np.nan, np.nan, 10.0, 10.0]
    er = [1e-3, 1e-3, -1e-3, -1e-3, -1e-3, 0.0]
    bt = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=50))
    res = bt.run_instrument(1, _frame(mids), _scores(er))
    pos = res.positions
    assert pos[1] == 50.0          # first decision executed at row 1
    assert pos[2] == 50.0 and pos[3] == 50.0  # cannot trade on NaN rows
    assert pos[4] == -50.0         # flips once the book is valid again
    ident = res.gross_pnl - res.total_costs
    assert abs(res.total_pnl - ident) < 1e-9


def test_cost_multiplier_scales_costs_linearly():
    mids = 20.0 + 0.01 * np.round(5 * np.sin(np.arange(200) * 0.8))
    er = 1e-3 * np.sin(np.arange(200) * 1.1)
    f, s = _frame(mids), _scores(er)
    cfg = BacktestConfig(max_pos_qty=100, conf_min=0.0)
    r1 = Backtester(_cost_model(1.0), META, cfg).run_instrument(1, f, s)
    r2 = Backtester(_cost_model(2.0), META, cfg).run_instrument(1, f, s)
    assert r1.trade_count == r2.trade_count
    assert abs(r2.total_costs - 2.0 * r1.total_costs) < 1e-9
    assert abs(r2.gross_pnl - r1.gross_pnl) < 1e-12


def test_backtester_deterministic():
    mids = 20.0 + 0.01 * np.round(7 * np.sin(np.arange(300) * 0.6))
    er = 1e-3 * np.cos(np.arange(300) * 0.9)
    f, s = _frame(mids), _scores(er)
    bt = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=300, conf_min=0.0))
    a = bt.run_instrument(1, f, s)
    b = bt.run_instrument(1, f, s)
    assert a.total_pnl == b.total_pnl
    assert a.total_costs == b.total_costs
    assert np.array_equal(a.equity, b.equity)


def test_run_aggregates_and_metrics():
    mids = 20.0 + 0.01 * np.round(6 * np.sin(np.arange(600) * 0.5))
    er = 1e-3 * np.sin(np.arange(600) * 0.7)
    frames = {1: _frame(mids)}
    scores = {1: _scores(er)}
    bt = Backtester(_cost_model(), META, BacktestConfig(max_pos_qty=100, conf_min=0.0))
    res = bt.run(frames, scores, "EQUITY")
    assert abs(res.total_pnl - res.per_instrument[1].total_pnl) < 1e-12
    m = res.metrics(capital=100 * 20.0)
    assert m["trade_count"] == res.trade_count
    assert m["total_costs"] > 0
    assert np.isfinite(m["max_drawdown"]) and m["max_drawdown"] >= 0
    # identity at the aggregate level too
    assert abs(m["total_pnl"] - (m["gross_pnl"] - m["total_costs"])) < 1e-9
    with pytest.raises(ValueError):
        bt.run({}, {2: _scores(er)}, "EQUITY")


def test_config_validation():
    with pytest.raises(ValueError):
        BacktestConfig(latency_rows=-1)
    with pytest.raises(ValueError):
        BacktestConfig(max_pos_qty=0)


def test_ensemble_scores_cancels_opposite_members():
    er = np.array([1e-4, -2e-4, 3e-4] * 10)
    base = {1: _scores(er)}
    # member B is member A scaled by -0.5 in beta -> z's are equal-magnitude,
    # opposite-sign; the equal-weight ensemble z must be exactly 0
    a = {1: base[1].copy()}
    b = {1: base[1].copy()}
    b[1]["expected_return"] = -b[1]["expected_return"]
    ens = ensemble_scores({"A": a, "B": b}, {"A": 1e-4, "B": 1e-4})
    assert np.allclose(ens[1]["expected_return"].to_numpy(), 0.0, atol=1e-15)
    ens2 = ensemble_scores({"A": a}, {"A": 1e-4})
    assert np.allclose(
        ens2[1]["expected_return"].to_numpy(), er, atol=1e-15
    )
    with pytest.raises(ValueError):
        ensemble_scores({"A": a}, {"A": 0.0})
