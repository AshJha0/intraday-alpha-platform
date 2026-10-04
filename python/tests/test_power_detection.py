"""Detection power: the statistics of the planted-signal power study
(``iap.research.power_stats``), the multi-session planted dataset, and the
study's session grid, detectors, thresholds, diagnosis and power model."""

from __future__ import annotations

import copy
import json
import math

import numpy as np
import pandas as pd
import pytest
from conftest import CONFIGS_DIR, REPO_ROOT
from iap.core.codec import encode_iap1, read_jsonl, sha256_bytes
from iap.core.rng import SplitMix64
from iap.marketdata.generator import MarketDataGenerator, load_generator_config
from iap.reference.refdata import ReferenceData
from iap.research import ResearchError, power, power_stats
from iap.validation.ledger import ExperimentLedger
from iap.validation.metrics import pooled_slope_hac_tstat

PLANTED_CONFIG = REPO_ROOT / "research" / "power" / "generator_planted.json"
NS = 1_000_000_000
DAY = 86_400 * NS
OPEN = 13 * 3600 * NS + 1800 * NS  # 13:30 UTC


def _normals(seed: int, n: int) -> np.ndarray:
    rng = SplitMix64(seed)
    return np.array([rng.normal() for _ in range(n)])


def _session_ts(n_sessions: int, rows_per_session: int, step_ns: int = 20 * NS) -> np.ndarray:
    """Row timestamps of ``n_sessions`` consecutive UTC days, evenly spaced."""
    one = OPEN + step_ns * np.arange(rows_per_session, dtype=np.int64)
    return np.concatenate([one + (20_000 + d) * DAY for d in range(n_sessions)])


# ---------------------------------------------------------------------------
# binomial interval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (1, 3, 0.0615, 0.7923),  # the v1.4.0 headline: 1 of 3 says almost nothing
        (0, 20, 0.0, 0.1611),
        (20, 20, 0.8389, 1.0),
        (16, 20, 0.5840, 0.9193),
        (10, 20, 0.2993, 0.7007),
    ],
)
def test_wilson_interval_matches_the_closed_form(k, n, lo, hi):
    got = power_stats.wilson_interval(k, n)
    assert got == pytest.approx((lo, hi), abs=5e-4)
    assert got[0] <= k / n <= got[1]


def test_wilson_interval_edges_and_errors():
    assert power_stats.wilson_interval(0, 0) == (0.0, 1.0)
    narrow = power_stats.wilson_interval(50, 100)
    wide = power_stats.wilson_interval(5, 10)
    assert narrow[1] - narrow[0] < wide[1] - wide[0]
    # three seeds cannot separate one in three from four in five; twenty can
    assert power_stats.wilson_interval(1, 3)[1] > 0.75
    assert power_stats.wilson_interval(16, 20)[0] > 1.0 / 3.0
    for bad in ((-1, 3), (4, 3), (1, -1)):
        with pytest.raises(ValueError):
            power_stats.wilson_interval(*bad)
    with pytest.raises(ValueError):
        power_stats.wilson_interval(1, 3, z=0.0)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_session_index_and_run_fraction():
    ts = _session_ts(4, 5, step_ns=3600 * NS)
    sess = power_stats.session_index(ts)
    assert sess.tolist() == [s for s in range(4) for _ in range(5)]
    frac = power_stats.run_fraction(ts)
    assert frac[0] == 0.0 and frac[-1] == 1.0
    assert np.all(np.diff(frac) >= 0.0)
    # sessions are equal slices; the first row of session 2 is at exactly 1/2
    assert frac[10] == 0.5 and frac[5] == 0.25
    assert frac[7] == pytest.approx(0.25 + 0.25 * 0.5)
    assert power_stats.run_fraction(np.empty(0, dtype=np.int64)).size == 0
    # order does not matter
    perm = np.array([3, 17, 0, 9, 12])
    assert power_stats.session_index(ts[perm]).tolist() == [0, 3, 0, 1, 2]


# ---------------------------------------------------------------------------
# session-clustered pooled-slope HAC
# ---------------------------------------------------------------------------


def _signal_pairs(seed: int, ts: np.ndarray, slope: float) -> tuple[np.ndarray, np.ndarray]:
    z = _normals(seed, 2 * ts.size)
    x = z[: ts.size]
    return x, slope * x + z[ts.size :]


def test_session_hac_equals_the_gate_statistic_on_one_session():
    ts = _session_ts(1, 900)
    x, y = _signal_pairs(11, ts, 0.1)
    for lags in (0, 2, 4):
        res = power_stats.pooled_slope_session_hac(ts, x, y, lags=lags)
        assert res["t"] == pytest.approx(pooled_slope_hac_tstat(ts, x, y, lags=lags), rel=1e-12)
    assert res["n_sessions"] == 1 and res["n_pairs"] == 900
    assert res["n_buckets"] == len(np.unique(ts // (300 * NS)))
    assert res["ic"] == pytest.approx(float(np.corrcoef(x, y)[0, 1]), abs=1e-12)


def test_session_hac_drops_exactly_the_cross_session_products():
    """Two sessions: the long-run variance is the sum of the two sessions'
    own long-run variances (same global means), i.e. the gate statistic
    minus the products that pair the end of one day with the start of the
    next."""
    ts = _session_ts(2, 600)
    x, y = _signal_pairs(5, ts, 0.05)
    lags = 2
    res = power_stats.pooled_slope_session_hac(ts, x, y, lags=lags)
    dx, dy = x - x.mean(), y - y.mean()
    sxx = float(np.sum(dx * dx))
    slope = float(np.sum(dx * dy)) / sxx
    moment = dx * (dy - slope * dx)
    lrv = 0.0
    for day in np.unique(ts // DAY):
        mask = ts // DAY == day
        _, inv = np.unique(ts[mask] // (300 * NS), return_inverse=True)
        s = np.bincount(inv, weights=moment[mask])
        lrv += float(np.sum(s * s))
        for lag in range(1, lags + 1):
            lrv += 2.0 * (1.0 - lag / (lags + 1.0)) * float(np.sum(s[lag:] * s[:-lag]))
    assert res["t"] == pytest.approx(slope * sxx / math.sqrt(lrv), rel=1e-12)
    assert res["n_sessions"] == 2
    # it differs from the statistic that treats the two days as adjacent
    assert res["t"] != pytest.approx(pooled_slope_hac_tstat(ts, x, y, lags=lags), rel=1e-9)
    # with no lags there is nothing to drop
    assert power_stats.pooled_slope_session_hac(ts, x, y, lags=0)["t"] == pytest.approx(
        pooled_slope_hac_tstat(ts, x, y, lags=0), rel=1e-12
    )


def test_session_hac_effective_sample_accounting():
    ts = _session_ts(4, 1500)
    x, y = _signal_pairs(3, ts, 0.08)
    res = power_stats.pooled_slope_session_hac(ts, x, y)
    assert res["n_eff"] == pytest.approx((res["t"] / res["ic"]) ** 2)
    assert res["design_effect"] == pytest.approx(res["n_pairs"] / res["n_eff"])
    # independent rows: one row is about one observation
    assert 0.6 < res["design_effect"] < 1.6
    assert res["t"] == pytest.approx(res["ic"] * math.sqrt(res["n_pairs"]), rel=0.3)
    # every row repeated 4 times: four rows per observation, same t
    rep = power_stats.pooled_slope_session_hac(np.repeat(ts, 4), np.repeat(x, 4), np.repeat(y, 4))
    assert rep["t"] == pytest.approx(res["t"], rel=1e-9)
    assert rep["design_effect"] == pytest.approx(4.0 * res["design_effect"], rel=1e-9)


def test_session_hac_grows_with_the_square_root_of_sessions():
    ts = _session_ts(8, 800)
    x, y = _signal_pairs(21, ts, 0.06)
    t8 = power_stats.pooled_slope_session_hac(ts, x, y)["t"]
    first2 = power_stats.session_index(ts) < 2
    t2 = power_stats.pooled_slope_session_hac(ts[first2], x[first2], y[first2])["t"]
    assert t8 > t2 > 0.0
    assert t8 / t2 == pytest.approx(2.0, rel=0.45)  # sqrt(8 / 2), sampling noise


def test_session_hac_degenerate_inputs():
    ts = _session_ts(1, 100)
    x = _normals(1, 100)
    flat = power_stats.pooled_slope_session_hac(ts, x, np.zeros(100))
    assert flat["t"] is None and flat["ic"] is None and flat["n_pairs"] == 100
    few = power_stats.pooled_slope_session_hac(ts[:20], x[:20], x[:20] + 1.0, min_buckets=8)
    assert few["t"] is None and few["n_buckets"] < 8 and few["ic"] is not None
    nan = power_stats.pooled_slope_session_hac(ts, np.full(100, np.nan), x)
    assert nan["n_pairs"] == 0 and nan["t"] is None
    with pytest.raises(ValueError, match="length mismatch"):
        power_stats.pooled_slope_session_hac(ts, x[:5], x)
    with pytest.raises(ValueError, match="lags"):
        power_stats.pooled_slope_session_hac(ts, x, x, lags=-1)


# ---------------------------------------------------------------------------
# break test
# ---------------------------------------------------------------------------


def test_slope_break_z_detects_a_reversal_and_not_a_stable_effect():
    ts = _session_ts(4, 1500)
    x, noise = _signal_pairs(8, ts, 0.0)
    stable = power_stats.slope_break_z(ts, x, 0.1 * x + noise)
    assert abs(stable["z"]) < 3.0
    assert stable["slope_pre"] > 0.0 and stable["slope_post"] > 0.0
    post = power_stats.run_fraction(ts) >= 0.5
    reversed_ = power_stats.slope_break_z(ts, x, np.where(post, -0.1, 0.1) * x + noise)
    assert reversed_["z"] > 6.0
    assert reversed_["slope_pre"] > 0.0 > reversed_["slope_post"]
    assert reversed_["t_pre"] > 3.0 and reversed_["t_post"] < -3.0
    # z is the difference over the root of the summed variances
    se2 = (reversed_["slope_pre"] / reversed_["t_pre"]) ** 2 + (
        reversed_["slope_post"] / reversed_["t_post"]
    ) ** 2
    assert reversed_["z"] == pytest.approx(
        (reversed_["slope_pre"] - reversed_["slope_post"]) / math.sqrt(se2)
    )
    # a split elsewhere sees a mixed second part: weaker
    early = power_stats.slope_break_z(
        ts, x, np.where(post, -0.1, 0.1) * x + noise, at_fraction=0.25
    )
    assert 0.0 < early["z"] < reversed_["z"]
    null = power_stats.slope_break_z(ts, x, noise)
    assert abs(null["z"]) < 3.0


def test_slope_break_z_degenerate_and_errors():
    ts = _session_ts(1, 40)
    x = _normals(2, 40)
    out = power_stats.slope_break_z(ts, x, x + 1.0)  # too few buckets per half
    assert out["z"] is None
    assert power_stats.slope_break_z(ts[:0], x[:0], x[:0])["z"] is None
    for bad in (0.0, 1.0, -0.1):
        with pytest.raises(ValueError, match="at_fraction"):
            power_stats.slope_break_z(ts, x, x, at_fraction=bad)
    with pytest.raises(ValueError, match="length mismatch"):
        power_stats.slope_break_z(ts, x[:3], x)


# ---------------------------------------------------------------------------
# analytical ICs of the planted mechanisms
# ---------------------------------------------------------------------------


def test_expected_z_tanh():
    assert power_stats.expected_z_tanh(1.0) == pytest.approx(0.6057, abs=2e-4)
    assert power_stats.expected_z_tanh(0.0) == 0.0
    assert power_stats.expected_z_tanh(50.0) == pytest.approx(math.sqrt(2.0 / math.pi), abs=1e-3)
    assert power_stats.expected_z_tanh(0.5) < power_stats.expected_z_tanh(2.0)
    with pytest.raises(ValueError):
        power_stats.expected_z_tanh(-1.0)


def test_ideal_ic_order_flow_matches_a_simulation_of_the_generator_rule():
    """Simulate the planted rule on a unit-variance random walk: one trade
    per step, a buy with probability 0.5 + 0.5 * s * tanh(g / |w|); a row
    reads the sign of ONE trade aged 1..W steps against the next h moves."""
    strength, decay, kernel, window, horizon = 0.4, 0.9, 20, 10, 5
    n = 60_000
    rng = SplitMix64(20261004)
    moves = np.array([rng.normal() for _ in range(n + kernel + horizon)])
    w = decay ** np.arange(kernel)
    g = np.convolve(moves, w[::-1], mode="valid")[:n] / math.sqrt(float(np.sum(w * w)))
    signs = np.array(
        [1.0 if rng.uniform() < 0.5 + 0.5 * strength * math.tanh(g[k]) else -1.0 for k in range(n)]
    )
    csum = np.concatenate([[0.0], np.cumsum(moves)])
    rows = np.arange(window, n)
    age = np.array([rng.randint(1, window) for _ in rows])
    x = signs[rows - age]
    y = csum[rows + horizon] - csum[rows]
    simulated = float(np.corrcoef(x, y)[0, 1])
    ideal = power_stats.ideal_ic_order_flow(strength, decay, kernel, window, horizon)
    assert simulated == pytest.approx(ideal, abs=0.012)
    assert 0.08 < ideal < 0.16


def test_ideal_ic_order_flow_properties():
    base = power_stats.ideal_ic_order_flow(0.4, 0.9, 20, 10, 5)
    assert power_stats.ideal_ic_order_flow(0.2, 0.9, 20, 10, 5) == pytest.approx(base / 2.0)
    assert power_stats.ideal_ic_order_flow(0.0, 0.9, 20, 10, 5) == 0.0
    # a label that covers more of the 20-step kernel reads more of it
    assert power_stats.ideal_ic_order_flow(0.4, 0.9, 20, 10, 10) > base
    # a one-step kernel has finished before any row can use the trade
    assert power_stats.ideal_ic_order_flow(0.4, 0.9, 1, 10, 5) == 0.0
    # a high-volatility regime saturates the tanh: more informative flow
    assert power_stats.ideal_ic_order_flow(0.4, 0.9, 20, 10, 5, (1.0, 0.4 / 0.15)) > base
    for bad in (
        (1.0, 0.9, 20, 10, 5),
        (0.4, 0.0, 20, 10, 5),
        (0.4, 0.9, 0, 10, 5),
        (0.4, 0.9, 20, 0, 5),
        (0.4, 0.9, 20, 10, 0),
    ):
        with pytest.raises(ValueError):
            power_stats.ideal_ic_order_flow(*bad)


def test_ideal_ic_lead_lag_formula_and_simulation():
    beta, lag = 0.4, 2
    assert power_stats.ideal_ic_lead_lag(beta, lag, 1) == 0.0  # a 1 s label is blind to a 2 s lag
    assert power_stats.ideal_ic_lead_lag(beta, lag, 2) == pytest.approx(
        beta / math.sqrt(1 + beta**2) / math.sqrt(2)
    )
    assert power_stats.ideal_ic_lead_lag(beta, 1, 1) == pytest.approx(0.371391, abs=1e-6)
    n, horizon = 60_000, 5
    z = _normals(77, 2 * n)
    leader, own = z[:n], z[n:]
    follower = own.copy()
    follower[lag:] += beta * leader[:-lag]
    csum = np.concatenate([[0.0], np.cumsum(follower)])
    rows = np.arange(1, n - horizon)
    x = leader[rows - 1]  # the leader's last one-second move
    for h in (1, horizon):
        y = csum[rows + h] - csum[rows]
        assert float(np.corrcoef(x, y)[0, 1]) == pytest.approx(
            power_stats.ideal_ic_lead_lag(beta, lag, h), abs=0.012
        )
    with pytest.raises(ValueError):
        power_stats.ideal_ic_lead_lag(beta, 0, 5)


# ---------------------------------------------------------------------------
# power model
# ---------------------------------------------------------------------------


def test_fit_t_model_recovers_kappa_and_sd():
    noise = _normals(9, 400)
    points = []
    for i, e in enumerate(noise):
        level = (0.5, 1.0, 2.0)[i % 3]
        sessions = (1, 2, 4, 8)[i % 4]
        points.append((level, sessions, 2.0 * level * math.sqrt(sessions) + e))
    fit = power_stats.fit_t_model(points)
    assert fit["n"] == 400
    assert fit["kappa"] == pytest.approx(2.0, abs=0.05)
    assert fit["sd"] == pytest.approx(1.0, abs=0.1)
    exact = power_stats.fit_t_model([(1.0, 4, 6.0), (2.0, 1, 6.0), (0.5, 16, 6.0)])
    assert exact["kappa"] == pytest.approx(3.0) and exact["sd"] == pytest.approx(0.0, abs=1e-12)
    # null rows, missing t and too few points are not fitted
    assert power_stats.fit_t_model([(0.0, 2, 1.0), (1.0, 2, None), (1.0, 2, 3.0)]) == {
        "kappa": None,
        "sd": None,
        "n": 1,
    }


def test_power_mde_and_sessions_needed_are_consistent():
    kappa, sd, thr = 2.0, 1.0, 4.365
    assert power_stats.normal_power(thr, sd, thr) == pytest.approx(0.5)
    assert power_stats.normal_power(thr + 0.8416, sd, thr) == pytest.approx(0.8, abs=1e-4)
    need = power_stats.sessions_needed(kappa, sd, thr)
    assert need == pytest.approx(((thr + 0.8416) / kappa) ** 2, rel=1e-4)
    assert power_stats.normal_power(kappa * math.sqrt(need), sd, thr) == pytest.approx(0.8)
    mde = power_stats.minimum_detectable_level(kappa, sd, thr, sessions=8)
    assert power_stats.normal_power(kappa * mde * math.sqrt(8), sd, thr) == pytest.approx(0.8)
    # the MDE at the needed session count is the reference effect itself
    assert power_stats.minimum_detectable_level(kappa, sd, thr, need) == pytest.approx(1.0)
    # a stricter threshold needs more sessions; half the effect needs four times as many
    assert power_stats.sessions_needed(kappa, sd, 3.0) < need
    assert power_stats.sessions_needed(kappa, sd, thr, level=0.5) == pytest.approx(4.0 * need)
    # a detector that does not respond has no MDE
    assert power_stats.sessions_needed(0.0, sd, thr) is None
    assert power_stats.minimum_detectable_level(-0.3, sd, thr, 8) is None
    for call in (
        lambda: power_stats.normal_power(1.0, 0.0, 3.0),
        lambda: power_stats.sessions_needed(kappa, sd, thr, level=0.0),
        lambda: power_stats.sessions_needed(kappa, sd, thr, power=1.0),
        lambda: power_stats.minimum_detectable_level(kappa, sd, thr, 0),
    ):
        with pytest.raises(ValueError):
            call()


# ---------------------------------------------------------------------------
# session grid, calendar, detectors, thresholds
# ---------------------------------------------------------------------------


def test_default_session_grid_and_extended_calendar():
    assert power.default_session_grid(8) == [1, 2, 4, 8]
    assert power.default_session_grid(6) == [1, 2, 4, 6]
    assert power.default_session_grid(1) == [1]
    with pytest.raises(ResearchError):
        power.default_session_grid(0)
    days = json.loads((CONFIGS_DIR / "instruments" / "instruments.json").read_text())["calendar"][
        "trading_days"
    ]
    assert power.extended_trading_days(days, 3) == days[:3]
    ten = power.extended_trading_days(days, 10)
    assert ten[: len(days)] == days and len(ten) == 10 == len(set(ten))
    assert ten == sorted(ten)
    # Friday 2026-08-28 is followed by Monday 2026-08-31: weekends are skipped
    assert ten[5] == "2026-08-31" and ten[-1] == "2026-09-04"
    with pytest.raises(ResearchError):
        power.extended_trading_days([], 3)


def test_matched_horizon_is_a_rule_on_the_planted_block():
    ref = load_generator_config(PLANTED_CONFIG)["planted"]
    # half of a 0.9-decay, 20-step kernel has arrived after 6 steps -> 10 s
    assert power.matched_horizon("order_flow", ref) == "10s"
    # a 2-step lag after a 1 s signal window -> 3 s -> the 5 s label
    assert power.matched_horizon("lead_lag", ref) == "5s"
    fast = copy.deepcopy(ref)
    fast["order_flow"].update(kernel_decay=0.7, kernel_steps=5)
    fast["lead_lag"]["lag_steps"] = 1
    assert power.matched_horizon("order_flow", fast) == "5s"
    assert power.matched_horizon("lead_lag", fast) == "5s"
    slow = copy.deepcopy(ref)
    slow["lead_lag"]["lag_steps"] = 12
    assert power.matched_horizon("lead_lag", slow) == "30s"
    with pytest.raises(ResearchError, match="unknown effect"):
        power.matched_horizon("wobble", ref)

    detectors = power.detectors_for(ref)
    assert [d["id"] for d in detectors] == [
        "lead_lag:EQ10@1s",
        "lead_lag:EQ10@5s",
        "order_flow:EQ04@5s",
        "order_flow:EQ04@10s",
    ]
    assert [d["role"] for d in detectors] == ["declared", "matched", "declared", "matched"]
    # a mechanism the declared horizon already covers gets no second detector
    assert [d["id"] for d in power.detectors_for(fast)] == [
        "lead_lag:EQ10@1s",
        "lead_lag:EQ10@5s",
        "order_flow:EQ04@5s",
    ]


def test_gate_looks_in_force_reads_the_committed_promotion_reports(tmp_path):
    reports = REPO_ROOT / "research" / "alpha_reports"
    looks = power.gate_looks_in_force(reports)
    eq04 = json.loads((reports / "EQ04.json").read_text(encoding="utf-8"))
    assert looks == eq04["ledger_looks"]
    # the threshold the study calls `gate` is the one those reports were judged at
    assert ExperimentLedger.bonferroni_t_threshold_at(looks) == pytest.approx(
        eq04["gates"]["min_nw_tstat"]
    )
    with pytest.raises(ResearchError, match="no promotion report"):
        power.gate_looks_in_force(tmp_path)
    (tmp_path / "EQ04.json").write_text("{}")
    (tmp_path / "EQ10.json").write_text("{}")
    with pytest.raises(ResearchError, match="ledger_looks"):
        power.gate_looks_in_force(tmp_path)


def test_first_sessions_slices_whole_sessions():
    ts = _session_ts(3, 4, step_ns=3600 * NS)
    frames = {
        1: pd.DataFrame({"exchange_ts": ts, "v": np.arange(12.0)}),
        2: pd.DataFrame({"exchange_ts": ts[4:], "v": np.arange(8.0)}),  # no first day
    }
    two = power.first_sessions(frames, 2)
    assert two[1]["v"].tolist() == list(np.arange(8.0))
    assert two[2]["v"].tolist() == list(np.arange(4.0))  # sessions are days of the RUN
    assert power.first_sessions(frames, 3)[1] is frames[1]
    assert power.first_sessions(frames, 9)[2] is frames[2]
    with pytest.raises(ResearchError):
        power.first_sessions(frames, 0)


# ---------------------------------------------------------------------------
# the multi-session planted dataset
# ---------------------------------------------------------------------------

TINY = {
    "seed": 1234,
    "equities": {"slots_per_stream": 150},
    "planted": {
        "order_flow": {"strength": 0.4, "kernel_decay": 0.9, "kernel_steps": 20},
        "lead_lag": {"beta": 0.4, "leader": "SYN.ETF.IDX", "lag_steps": 2},
    },
}


def _generate(tmp_path, sessions: int, sub: str, calendar_sessions: int | None = None):
    cfg_dir = power._reduced_configs(
        CONFIGS_DIR, power.DEFAULT_UNIVERSE, tmp_path / f"cfg-{sub}", calendar_sessions or sessions
    )
    refdata = ReferenceData.load(cfg_dir)
    stats = MarketDataGenerator(
        refdata, {**copy.deepcopy(TINY), "sessions": sessions}
    ).generate_run(tmp_path / sub)
    files = sorted(n for n in stats["files"] if n.startswith("eq_"))
    return refdata, {
        name: sha256_bytes(encode_iap1(read_jsonl(tmp_path / sub / name))) for name in files
    }


def test_generator_runs_more_sessions_than_the_bundled_calendar(tmp_path):
    bundled = ReferenceData.load(CONFIGS_DIR)
    assert len(bundled.trading_days) == 5
    with pytest.raises(ValueError, match="calendar has 5 trading days"):
        MarketDataGenerator(bundled, {**copy.deepcopy(TINY), "sessions": 7}).generate_run(
            tmp_path / "short"
        )
    refdata, seven = _generate(tmp_path, 7, "seven")
    assert len(refdata.trading_days) == 7 and len(seven) == 7
    assert sorted(seven) == [f"eq_{d.replace('-', '')}.jsonl" for d in refdata.trading_days]
    assert len(set(seven.values())) == 7  # seven different sessions
    _, again = _generate(tmp_path, 7, "again")
    assert again == seven  # deterministic
    # the study's reference tree leaves configs/ alone
    assert len(ReferenceData.load(CONFIGS_DIR).trading_days) == 5


def test_first_n_sessions_of_a_run_are_the_n_session_run(tmp_path):
    """What lets the study generate once at the largest session count: with
    no break, session k does not depend on how many sessions follow."""
    _, seven = _generate(tmp_path, 7, "seven")
    _, three = _generate(tmp_path, 3, "three", calendar_sessions=7)
    assert len(three) == 3
    assert {name: seven[name] for name in three} == three
    # a break is a fraction of the RUN, so there the prefix property fails —
    # the study evaluates break runs at the full session count only
    brk = {"at_fraction": 0.5, "post_multiplier": -1.0}
    digests = []
    for sessions, sub in ((4, "b4"), (2, "b2")):
        cfg_dir = power._reduced_configs(
            CONFIGS_DIR, power.DEFAULT_UNIVERSE, tmp_path / f"cfg-{sub}", 4
        )
        cfg = {**copy.deepcopy(TINY), "sessions": sessions}
        cfg["planted"]["break"] = brk
        MarketDataGenerator(ReferenceData.load(cfg_dir), cfg).generate_run(tmp_path / sub)
        digests.append(sha256_bytes(encode_iap1(read_jsonl(tmp_path / sub / "eq_20260825.jsonl"))))
    assert digests[0] != digests[1]


def test_cell_config_sets_the_session_count():
    base = load_generator_config(PLANTED_CONFIG)
    assert power.cell_config(base, 1.0, "stable", 7)["sessions"] == base["sessions"]
    assert power.cell_config(base, 1.0, "stable", 7, sessions=11)["sessions"] == 11
    with pytest.raises(ResearchError, match="sessions"):
        power.cell_config(base, 1.0, "stable", 7, sessions=0)


def test_reference_config_generates_enough_sessions_for_the_committed_grid():
    base = load_generator_config(PLANTED_CONFIG)
    assert base["sessions"] >= 4
    assert power.default_session_grid(base["sessions"])[-1] == base["sessions"]
    # the reference effect itself is the one v1.3.0 defined
    assert base["planted"]["order_flow"] == {
        "strength": 0.4,
        "kernel_decay": 0.9,
        "kernel_steps": 20,
    }
    assert base["planted"]["lead_lag"] == {"beta": 0.4, "leader": "SYN.ETF.IDX", "lag_steps": 2}


@pytest.fixture(scope="module")
def tiny_study(tmp_path_factory):
    """A real (tiny) study: 3 sessions beyond... of a 400-slot dataset, one
    seed per cell, run in-process and through the worker pool."""
    tmp = tmp_path_factory.mktemp("power")
    cfg = json.loads(PLANTED_CONFIG.read_text(encoding="utf-8"))
    cfg["equities"]["slots_per_stream"] = 400
    cfg["sessions"] = 6
    path = tmp / "generator_planted.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    kwargs = dict(levels=(1.0,), n_seeds=2, sessions=[3, 6], break_levels=(1.0,), gate_looks=3936)
    serial = power.run_power_study(path, CONFIGS_DIR, jobs=1, scratch_dir=tmp / "s1", **kwargs)
    pooled = power.run_power_study(path, CONFIGS_DIR, jobs=2, scratch_dir=tmp / "s2", **kwargs)
    return serial, pooled, tmp


def test_study_end_to_end_on_six_tiny_sessions(tiny_study):
    doc, pooled, tmp = tiny_study
    assert doc == pooled  # the document does not depend on the number of workers
    assert doc["x-version"] == power.POWER_VERSION == 3
    assert doc["sessions"] == [3, 6]
    assert [(r["scenario"], r["level"]) for r in doc["runs"]] == [
        ("stable", 1.0),
        ("stable", 1.0),
        ("break", 1.0),
        ("break", 1.0),
    ]
    stable, brk = doc["runs"][0], doc["runs"][2]
    assert [e["sessions"] for e in stable["evaluations"]] == [3, 6]
    assert [e["sessions"] for e in brk["evaluations"]] == [6]  # break: full run only
    assert sorted(stable["rows"]) == ["1", "11", "2"]
    ids = [d["id"] for d in doc["detectors"]]
    for run in doc["runs"]:
        for evaluation in run["evaluations"]:
            assert sorted(evaluation["detectors"]) == sorted(ids)
    three, six = (e["detectors"]["order_flow:EQ04@5s"] for e in stable["evaluations"])
    for row in (three, six):
        assert "error" in row or row["verdict"] in ("REJECT", "ITERATE", "PROMOTE")
    if "error" not in three and "error" not in six:
        # six sessions hold about twice the pairs of the first three
        assert six["n_pairs"] == pytest.approx(2.0 * three["n_pairs"], rel=0.2)
        assert 0.0 < six["signal_coverage"] < 1.0
        assert 0.9 < six["label_scored_frac"] <= 1.0
        assert 0.0 <= six["label_zero_frac"] <= 1.0
        # the recompute probe ran on the first seed at the full session count only
        assert six["recompute_ok"] is True and three["recompute_ok"] is None
        second = doc["runs"][1]["evaluations"][1]["detectors"]["order_flow:EQ04@5s"]
        assert second.get("recompute_ok") is None
    # the study charges itself: every chain t and every break z is a test
    assert doc["protocol"]["n_tests"] == 2 * len(ids) * 2 * (2 + 1)
    thr = doc["protocol"]["thresholds"]
    assert thr["fixed"] == 3.0
    assert thr["gate"] == pytest.approx(ExperimentLedger.bonferroni_t_threshold_at(3936), abs=1e-6)
    assert thr["study"] == pytest.approx(
        ExperimentLedger.bonferroni_t_threshold_at(doc["protocol"]["n_tests"]), abs=1e-6
    )
    assert doc["protocol"]["promote_t_threshold"] == max(thr.values())
    assert doc["cells"] == power.summarise(doc["runs"], thr)
    json.dumps(doc, allow_nan=False)
    md = power.render_markdown(doc)
    assert "## Where the power goes" in md and "## What the platform can and cannot detect" in md
    # nothing is left in the scratch directories
    assert not list((tmp / "s1").iterdir()) and not list((tmp / "s2").iterdir())


def test_direct_statistics_agree_with_a_hand_computation(tmp_path):
    base = load_generator_config(PLANTED_CONFIG)
    base["equities"]["slots_per_stream"] = 400
    cfg = power.cell_config(base, 1.0, "stable", 31337, sessions=2)
    frames = power.build_planted_frames(cfg, CONFIGS_DIR, tmp_path / "work")
    stats = power.direct_statistics(frames, "EQ04", "5s")
    ts, xs, ys, rows = [], [], [], 0
    for iid in sorted(frames):  # EQ04 trades the whole reduced universe
        df = frames[iid]
        y = df["label_mid_5s"].to_numpy(dtype=float).copy()
        y[~df["label_valid_5s"].to_numpy(dtype=bool)] = np.nan
        reopen = df["label_reopen_5s"].to_numpy(dtype=float)
        y[np.isfinite(reopen)] = reopen[np.isfinite(reopen)]
        ts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        xs.append(df["trade_imbalance_w10s_v1"].to_numpy(dtype=float))
        ys.append(y)
        rows += len(df)
    ts, x, y = np.concatenate(ts), np.concatenate(xs), np.concatenate(ys)
    pair = np.isfinite(x) & np.isfinite(y)
    assert stats["n_pairs"] == int(pair.sum())
    assert stats["signal_coverage"] == pytest.approx(np.isfinite(x).sum() / rows)
    assert stats["label_scored_frac"] == pytest.approx(np.isfinite(y).sum() / rows)
    assert stats["label_zero_frac"] == pytest.approx(float((y[pair] == 0.0).mean()))
    assert stats["ic_direct"] == pytest.approx(float(np.corrcoef(x[pair], y[pair])[0, 1]))
    expected = power_stats.pooled_slope_session_hac(ts, x, y, lags=2)
    assert stats["t_direct"] == pytest.approx(expected["t"])
    assert stats["break_z"] == pytest.approx(power_stats.slope_break_z(ts, x, y, lags=2)["z"])
    # EQ10 at the matched horizon reads the constituents only
    lead = power.direct_statistics(frames, "EQ10", "5s")
    assert lead["n_pairs"] < stats["n_pairs"] / stats["signal_coverage"]
    assert lead["signal_coverage"] > 0.9
