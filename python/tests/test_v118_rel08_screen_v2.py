"""REL-08 screen v2 evaluation path: sessions, coverage, within-session gap and resampling.

Synthetic data only; no real session is opened.  The v1 path is checked unchanged at the end.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from iap.research import rel08_screen as rs
from iap.research import rel08_screen_v2 as v2

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "rel08_v2_dryrun", REPO / "research" / "v118" / "rel08" / "dryrun" / "rel08_v2_dryrun.py"
)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

NS_MIN, NS_DAY = v2.NS_MIN, v2.NS_DAY
P = {"min_rebalances_per_session": 2, "sessions_min_qualifying": 5, "sessions_total": 6}


def _grid(days=6, n=26, step=15):
    return np.concatenate([d * NS_DAY + (570 + step * np.arange(n)) * NS_MIN for d in range(days)])


def test_single_rebalance_day_does_not_qualify_and_gives_no_zero_gap():
    g = _grid(days=3)
    u = np.zeros(g.size, bool)
    u[:26] = True  # day 0 fully usable
    u[26] = True  # day 1: a single usable rebalance
    u[52:54] = True  # day 2: two
    q = v2.qualifying_mask(g, u, 2)
    assert q[:26].all() and not q[26] and q[52:54].all()
    assert v2.within_session_max_gap_min(g, q) == 15.0
    only_single = np.zeros(g.size, bool)
    only_single[26] = True
    assert v2.within_session_max_gap_min(g, v2.qualifying_mask(g, only_single, 2)) == float("inf")


def test_leading_gap_and_session_boundary_excluded_midday_hole_counted():
    g = _grid(days=2)
    u = np.ones(g.size, bool)
    u[:20] = False  # late first usable rebalance on day 0: not a gap
    u[26 + 3 : 26 + 8] = False  # 6 x 15 = 90 min hole inside day 1
    q = v2.qualifying_mask(g, u, 2)
    assert v2.within_session_max_gap_min(g, q) == 90.0


def test_missing_days_and_coverage_rule():
    g = _grid(days=6)
    u = np.ones(g.size, bool)
    assert v2.coverage(g, v2.qualifying_mask(g, u, 2), P)["coverage_ok"]
    u5 = u.copy()
    u5[26:52] = False  # one session dead: 5 of 6 still ok
    c = v2.coverage(g, v2.qualifying_mask(g, u5, 2), P)
    assert c["sessions_qualifying"] == 5 and c["coverage_ok"]
    u4 = u5.copy()
    u4[:26] = False  # two dead
    assert not v2.coverage(g, v2.qualifying_mask(g, u4, 2), P)["coverage_ok"]
    g_missing = _grid(days=4)  # two registered sessions have no rows at all
    c = v2.coverage(g_missing, v2.qualifying_mask(g_missing, np.ones(g_missing.size, bool), 2), P)
    assert c["sessions_qualifying"] == 4 and not c["coverage_ok"]
    # a day with a single usable rebalance does not count towards coverage
    us = u.copy()
    us[26:52] = False
    us[26] = True
    assert v2.coverage(g, v2.qualifying_mask(g, us, 2), P)["sessions_qualifying"] == 5


def test_no_usable_rebalance_is_inf_and_not_covered():
    g = _grid(days=2)
    q = v2.qualifying_mask(g, np.zeros(g.size, bool), 2)
    assert not q.any() and v2.within_session_max_gap_min(g, q) == float("inf")
    assert not v2.coverage(g, q, P)["coverage_ok"]


def test_bootstrap_stays_within_sessions():
    # session A all +1, session B all -1: every draw must keep the pooled mean exactly at the sample mean
    x = np.concatenate([np.ones(30), -np.ones(30)])
    s = np.repeat([0, 1], 30)
    m = v2.mbb_means_within(x, s, block=4, draws=200, seed=1)
    assert np.allclose(m, 0.0)
    # a cross-session v1 bootstrap does move the pooled mean (blocks straddle the boundary)
    assert rs.mbb_means(x, 4, 200, 1).std() > 0.0


def test_bootstrap_deterministic_and_size_preserving():
    rng = np.random.default_rng(0)
    x = rng.normal(size=70)
    s = np.repeat([0, 1, 2], [20, 30, 20])
    a = v2.mbb_means_within(x, s, 5, 50, 7)
    b = v2.mbb_means_within(x, s, 5, 50, 7)
    assert np.array_equal(a, b) and a.shape == (50,)
    assert v2.mbb_means_within(np.array([]), np.array([]), 5, 10, 1).shape == (10,)


def test_check_sessions_refuses_unregistered_day():
    class D:
        def session_close(self):
            return {1: 0, 2: 0, 9: 0}

    v2.check_sessions(D(), [1, 2, 3, 9])  # a registered day without rows is allowed
    with pytest.raises(v2.SessionError):
        v2.check_sessions(D(), [1, 2, 3])


def _rule(sessions: int):
    rule = dr.base_rule({"alpha_bps": 20.0, "risk_aversion": 100.0}, draws=99)
    rule.doc["sufficiency_v2"] = {"sessions_total": sessions, "sessions_min_qualifying": sessions}
    return rule


def test_integration_two_session_screen_and_incomplete_features():
    data = dr.make_data("null", 0.0, 3, days=2)
    rule = _rule(2)
    days = sorted(data.session_close())
    out = v2.run_screen_v2(data, rule, days, variant_ids=("V1",))
    v = out["variants"][0]
    assert v["sessions_qualifying"] == 2 and v["coverage_ok"]
    assert v["n_usable"] <= v["n_usable_all_sessions"]
    assert np.isfinite(v["max_gap_min"]) and v["max_gap_min"] <= 30.0
    assert out["decisions"][0]["outcome"] in rs.OUTCOMES
    assert "sessions_qualifying" in v and out["label"].endswith("no edge claim")
    # incomplete features: blank every score input of session 1 -> that session cannot qualify,
    # coverage fails, the variant is underpowered by session coverage
    for df in data.frames.values():
        mask = (df["exchange_ts"].to_numpy() // NS_DAY) == days[1]
        for col in ("mid_price_v1", "traded_volume_w10s_v1"):
            df.loc[mask, col] = np.nan
    out2 = v2.run_screen_v2(data, rule, days, variant_ids=("V1",))
    w = out2["variants"][0]
    assert w["sessions_qualifying"] == 1 and not w["coverage_ok"]
    d = out2["decisions"][0]
    assert d["outcome"] == "underpowered" and "C1" in d["reason"]


def test_v1_path_unchanged():
    data = dr.make_data("null", 0.0, 3, days=1)
    rule = dr.base_rule({"alpha_bps": 20.0, "risk_aversion": 100.0}, draws=99)
    v = dict(next(x for x in rule.variants if x["id"] == "V1"))
    out = rs.evaluate_variant(data, rule, v)
    assert out["id"] == "V1" and "sessions_qualifying" not in out
    # v1 still counts the leading idle time from the grid origin as a gap (the documented v1 formula)
    assert out["max_gap_min"] >= 0.0


def _same(a, b):
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    return a == b


@pytest.mark.parametrize(
    ("mode", "amp", "seed"), [("null", 0.0, 20269201), ("revert", 60.0, 20269202)]
)
def test_short_circuit_changes_nothing_but_the_skipped_books(mode, amp, seed):
    """Pure-speed option: decisions and every other statistic are bit-identical to the full evaluation."""
    data = dr.make_data(mode, amp, seed, days=1)
    rule = dr.base_rule({"alpha_bps": 20.0, "risk_aversion": 100.0}, draws=99)
    rule.doc["sufficiency_v2"] = {"sessions_total": 1, "sessions_min_qualifying": 1}
    days = sorted(data.session_close())
    full = v2.run_screen_v2(data, rule, days, variant_ids=("V1", "V5"))
    fast = v2.run_screen_v2(data, rule, days, variant_ids=("V1", "V5"), short_circuit=True)
    for a, b in zip(full["variants"], fast["variants"], strict=True):
        skipped = not b["robustness_books_computed"]
        for k in a:
            if k in ("robustness_books_computed",):
                continue
            if skipped and k in ("influential_removed_net_bps", "alt_normalisation_net_bps"):
                continue
            assert _same(a[k], b[k]), k
    for a, b in zip(full["decisions"], fast["decisions"], strict=True):
        assert a["outcome"] == b["outcome"] and a["reason"] == b["reason"]
        for c in ("C1", "C2", "C3", "C4", "C8"):
            assert a["criteria"][c] == b["criteria"][c]
