"""Bars-based cross-sectional study (iap.research.breadth_bars): the universe
rule, the cost arithmetic and a planted reversal that the scores must find."""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.research import breadth_bars as bb


def _panel(mid: np.ndarray, hs: float = 0.0005, etp=None, vol=None) -> bb.Panel:
    n = mid.shape[0]
    v = np.full(mid.shape, 100.0) if vol is None else vol
    return bb.Panel(
        symbols=tuple(f"S{i:03d}" for i in range(n)),
        etp=np.zeros(n, dtype=bool) if etp is None else np.asarray(etp, dtype=bool),
        mid=mid,
        half_spread=np.where(np.isfinite(mid), hs, np.nan),
        vol=v,
        notional=v * np.nan_to_num(mid),
        valid_ns=np.where(np.isfinite(mid), float(bb.MINUTE_NS), 0.0),
    )


def test_load_panel_reads_the_itchbars_csv(tmp_path):
    p = tmp_path / "bars.csv"
    p.write_text(
        "# itchbars/1\n"
        "symbol,etp,minute,bid,ask,bid_sz,ask_sz,tw_spread,tw_valid_ns,vol,notional,trades,"
        "hidden_vol,hidden_notional\n"
        "AAA,N,0,1000000,1000200,100,100,200.0000,60000000000,10,10001000,1,0,0\n"
        "AAA,N,2,1000000,1000000,100,100,0.0000,0,0,0,0,0,0\n"
        "NA,Y,1,0,500000,0,5,0.0000,0,0,0,0,0,0\n"
        "WI#,N,3,200000,200100,1,1,100.0000,60000000000,0,0,0,0,0\n",
        encoding="utf-8",
    )
    panel = bb.load_panel(p)
    # the ticker NA is a string, not a missing value; "#" is part of a symbol, not a comment
    assert panel.symbols == ("AAA", "NA", "WI#")
    assert panel.etp.tolist() == [False, True, False]
    assert panel.mid[2, 3] == pytest.approx(20.005)
    assert panel.mid[0, 0] == pytest.approx(100.01)
    assert panel.half_spread[0, 0] == pytest.approx(200 / 2_000_200)
    assert math.isnan(panel.mid[0, 2])  # locked book
    assert math.isnan(panel.mid[1, 1])  # one-sided book
    assert math.isnan(panel.mid[0, 1])  # no row for the bar
    assert panel.notional[0, 0] == pytest.approx(1000.1)


def test_universe_rule_uses_liquidity_facts_only_and_ranks_by_notional():
    mid = np.full((6, bb.BARS), 20.0)
    mid[1] = 4.0  # below the price floor
    mid[2, :40] = np.nan  # quoted for under 95 % of the session
    vol = np.full(mid.shape, 100.0)
    vol[4] = 300.0
    vol[5] = 200.0
    panel = _panel(mid, etp=[False, False, False, True, False, False], vol=vol)
    rows = bb.select_universe(panel, top=2)
    assert [panel.symbols[i] for i in rows] == ["S004", "S005"]
    assert [panel.symbols[i] for i in bb.select_universe(panel, top=10)] == ["S004", "S005", "S000"]


def test_decision_bars_leave_room_for_entry_and_exit():
    ks = bb.decision_bars(lookback=15, step=15, delay=1, horizon=15)
    assert ks[0] == 15 and ks[-1] + 1 + 15 < bb.BARS
    assert ks[-1] + 15 + 1 + 15 >= bb.BARS
    assert len(ks) == 24
    with pytest.raises(ValueError):
        bb.decision_bars(0, 15, 1, 15)
    with pytest.raises(ValueError):
        bb.decision_bars(15, 15, -1, 15)


def test_rank_ic_edges():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert bb.rank_ic(x, x * 2) == pytest.approx(1.0)
    assert bb.rank_ic(x, -x) == pytest.approx(-1.0)
    assert math.isnan(bb.rank_ic(x[:2], x[:2]))
    assert math.isnan(bb.rank_ic(x, np.ones(4)))


def test_series_summary():
    s = bb.series_summary([1.0, 2.0, 3.0, math.nan])
    assert s["n"] == 3 and s["mean"] == pytest.approx(2.0)
    assert s["se"] == pytest.approx(1.0 / math.sqrt(3))
    assert s["t"] == pytest.approx(2.0 * math.sqrt(3))
    assert math.isnan(bb.series_summary([1.0])["t"])
    assert bb.series_summary([])["n"] == 0


def _reverting_panel(seed: int, names: int = 60, hs: float = 0.0005) -> bb.Panel:
    """Each name's mid is a base price plus a shock that lands five bars before
    every decision bar and then decays by a tenth per bar: most of what moved
    in the lookback window comes back inside the forward window."""
    rng = np.random.default_rng(seed)
    shock = np.zeros((names, bb.BARS))
    for m in range(1, bb.BARS):
        fresh = rng.normal(0.0, 0.002, names) if m % 15 == 10 else 0.0
        shock[:, m] = shock[:, m - 1] * 0.9 + fresh
    return _panel(50.0 * (1.0 + shock), hs=hs)


def test_planted_reversal_is_found_and_costs_are_subtracted_exactly():
    panel = _reverting_panel(7)
    rows = np.arange(len(panel.symbols))
    free = bb.run_study(_reverting_panel(7, hs=0.0), rows)
    paid = bb.run_study(panel, rows)
    for s in bb.SCORES:
        assert free["scores"][s]["rank_ic"]["mean"] > 0.1
        assert free["scores"][s]["gross_bps_per_rebalance"]["mean"] > 0
        g = paid["scores"][s]["gross_bps_per_rebalance"]["mean"]
        n = paid["scores"][s]["net_bps_per_rebalance"]["mean"]
        # every name pays 5 bps to enter and 5 bps to exit; return on gross pays the same
        assert g - n == pytest.approx(10.0, abs=1e-9)
        assert g == pytest.approx(free["scores"][s]["gross_bps_per_rebalance"]["mean"])
    assert paid["median_round_trip_cost_bps"] == pytest.approx(10.0)
    assert paid["rebalances"] == 24 and paid["names"] == 60


def test_scores_use_nothing_after_the_decision_bar():
    panel = _reverting_panel(11)
    rows = np.arange(len(panel.symbols))
    k = 45
    before = bb.scores_at(panel, rows, k, 15)
    mid = panel.mid.copy()
    mid[:, k + 1 :] *= 1.5
    vol = panel.vol.copy()
    vol[:, k + 1 :] = 7.0
    changed = bb.Panel(
        panel.symbols, panel.etp, mid, panel.half_spread, vol, vol * mid, panel.valid_ns
    )
    after = bb.scores_at(changed, rows, k, 15)
    for s in bb.SCORES:
        np.testing.assert_array_equal(before[s], after[s])


def test_too_few_names_gives_no_book():
    panel = _panel(np.full((6, bb.BARS), 10.0))
    res = bb.run_study(panel, np.arange(6))
    assert res["scores"]["vwap_deviation"]["net_bps_per_rebalance"]["n"] == 0
