"""Adaptability-layer tests (iap.adaptive + iap.backtest.adaptive).

Covers: PSI/KS correctness vs independent brute force and vs the pinned
goldens (tests/golden/expected_adaptive.json), baseline serialization
round-trips, refit-policy trigger boundaries, the lifecycle state machine
(incl. re-activation and log completeness), and the adaptive walk-forward
backtest: accounting identity, determinism, no-lookahead (shift test),
warmup/retirement gating and config validation.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from iap.adaptive import (
    ACTIVE,
    PSI_EPS,
    RETIRED,
    STATES,
    WATCH,
    DriftBaseline,
    DriftTriggeredPolicy,
    ICBaseline,
    LifecycleConfig,
    LifecycleLog,
    LifecycleTracker,
    RefitContext,
    ScheduledPolicy,
    StaticPolicy,
    build_policy,
    capture_baseline,
    ks_pvalue,
    ks_statistic,
    ks_test,
    load_adaptive_config,
    psi,
    rolling_ic_z,
    validate_adaptive_config,
)
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.backtest.adaptive import AdaptiveDeployment
from iap.alpha.base import LinearAlpha, col
from iap.core.rng import SplitMix64

from conftest import CONFIGS_DIR, GOLDEN_DIR

NS_S = 1_000_000_000
TOL = 1e-10


# ---------------------------------------------------------------------------
# independent brute-force implementations (no iap.adaptive code)
# ---------------------------------------------------------------------------


def brute_psi(edges, expected_frac, values):
    counts = [0] * 10
    n = 0
    for v in values:
        if not math.isfinite(v):
            continue
        n += 1
        b = 0
        for e in edges:
            if e < v:
                b += 1
        counts[b] += 1
    total = 0.0
    for i in range(10):
        a = max(counts[i] / n, PSI_EPS)
        e = max(expected_frac[i], PSI_EPS)
        total += (a - e) * math.log(a / e)
    return total


def brute_ks_d(a, b):
    a = [v for v in a if math.isfinite(v)]
    b = [v for v in b if math.isfinite(v)]
    d = 0.0
    for v in list(a) + list(b):
        fa = sum(1 for x in a if x <= v) / len(a)
        fb = sum(1 for x in b if x <= v) / len(b)
        d = max(d, abs(fa - fb))
    return d


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden():
    return json.loads((GOLDEN_DIR / "expected_adaptive.json").read_text())


@pytest.fixture(scope="module")
def synth_samples(golden):
    """Regenerate the pinned synthetic samples from the golden recipe."""
    rec = golden["psi_ks"]["recipe"]
    rng = SplitMix64(int(rec["seed"]))
    base = np.array([rng.uniform() for _ in range(4000)])
    same = np.array([rng.uniform() for _ in range(2000)])
    shifted = np.array([0.25 + 0.75 * rng.uniform() for _ in range(2000)])
    return base, same, shifted


@pytest.fixture(scope="module")
def synth_baseline(synth_samples):
    base, _, _ = synth_samples
    return capture_baseline(base, "feature", "golden_synthetic_uniform")


@pytest.fixture(scope="module")
def adaptive_cfg():
    return load_adaptive_config(CONFIGS_DIR / "strategies.json")


class _ToyAlpha(LinearAlpha):
    """Test-only alpha over a synthetic feature column.

    Economic rationale: none — this is a machinery fixture; the signal is
    constructed to correlate with the 1s label so the adaptive engine has
    a live IC to monitor.
    """

    alpha_id = "ZZ99"
    name = "toy_adaptive_fixture"
    asset_class = "EQUITY"
    horizon = "1s"
    features = ("x_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "x_v1")


def _make_frames(n_rows=21_600, t0=1_700_000_000_000_000_000, seed=7):
    """Two-instrument synthetic frames: 1s rows, causal-ish signal x_v1
    correlated with the next-second return label."""
    frames = {}
    for iid in (1, 2):
        rng = SplitMix64(seed + iid)
        ts = t0 + NS_S * np.arange(n_rows, dtype=np.int64)
        steps = np.array([rng.normal() for _ in range(n_rows)])
        mid = 100.0 + 0.01 * np.cumsum(steps)
        label = np.empty(n_rows)
        label[:-1] = mid[1:] / mid[:-1] - 1.0
        label[-1] = np.nan
        valid = np.ones(n_rows, dtype=bool)
        valid[-1] = False
        noise = np.array([rng.normal() for _ in range(n_rows)])
        sig = label * 5e3 + noise  # correlated with the label, plus noise
        sig[-1] = noise[-1]
        frames[iid] = pd.DataFrame({
            "exchange_ts": ts,
            "x_v1": sig,
            "mid_price_v1": mid,
            "spread_ticks_v1": np.ones(n_rows),
            "label_mid_1s": label,
            "label_valid_1s": valid,
        })
    return frames


_META = {
    1: {"asset_class": "EQUITY", "tick_size": 0.01, "lot_size": 1,
        "adv": 1e6, "ref_price": 100.0},
    2: {"asset_class": "EQUITY", "tick_size": 0.01, "lot_size": 1,
        "adv": 1e6, "ref_price": 100.0},
}


@pytest.fixture(scope="module")
def toy_frames():
    return _make_frames()


@pytest.fixture(scope="module")
def toy_deployment(toy_frames, adaptive_cfg):
    bt = Backtester(CostModel.load(CONFIGS_DIR / "execution.json"), _META,
                    BacktestConfig())
    return AdaptiveDeployment(
        _ToyAlpha, toy_frames, adaptive_cfg, bt,
        psi_threshold=float(
            adaptive_cfg["policies"]["drift_triggered"]["psi_threshold"]
        ),
    )


# ---------------------------------------------------------------------------
# PSI / KS correctness
# ---------------------------------------------------------------------------


def test_psi_matches_bruteforce_same_dist(synth_baseline, synth_samples):
    _, same, _ = synth_samples
    got = psi(synth_baseline, same)
    bf = brute_psi(synth_baseline.edges, synth_baseline.expected_frac, same)
    assert abs(got - bf) <= TOL


def test_psi_matches_bruteforce_shifted(synth_baseline, synth_samples):
    _, _, shifted = synth_samples
    got = psi(synth_baseline, shifted)
    bf = brute_psi(synth_baseline.edges, synth_baseline.expected_frac, shifted)
    assert abs(got - bf) <= TOL


def test_psi_ks_match_golden(golden, synth_baseline, synth_samples):
    base, same, shifted = synth_samples
    cases = golden["psi_ks"]["cases"]
    for name, cur in (("same_dist", same), ("shifted", shifted)):
        assert abs(psi(synth_baseline, cur) - cases[name]["psi"]) <= TOL
        d, p = ks_test(base, cur)
        assert abs(d - cases[name]["ks_d"]) <= TOL
        assert abs(p - cases[name]["ks_p"]) <= TOL
    gb = golden["psi_ks"]["baseline"]
    assert list(synth_baseline.edges) == pytest.approx(gb["edges"], abs=TOL)
    assert list(synth_baseline.expected_frac) == pytest.approx(
        gb["expected_frac"], abs=TOL
    )


def test_ks_matches_bruteforce(synth_samples):
    base, _, shifted = synth_samples
    # brute force is O(n^2): subsample deterministically
    a, b = base[:300], shifted[:300]
    assert abs(ks_statistic(a, b) - brute_ks_d(a, b)) <= TOL


def test_psi_identical_sample_is_exactly_zero(synth_baseline, synth_samples):
    base, _, _ = synth_samples
    assert psi(synth_baseline, base) == 0.0


def test_psi_epsilon_guard_disjoint_sample(synth_baseline):
    # every current value beyond the top edge: 9 empty buckets, no nan/inf
    v = psi(synth_baseline, np.full(500, 99.0))
    assert math.isfinite(v)
    bf = brute_psi(synth_baseline.edges, synth_baseline.expected_frac,
                   np.full(500, 99.0))
    assert abs(v - bf) <= TOL
    assert v > 2.0  # total shift is a huge PSI


def test_psi_monotone_under_growing_shift(synth_baseline, synth_samples):
    base, _, _ = synth_samples
    vals = [psi(synth_baseline, base[:2000] + s) for s in (0.0, 0.1, 0.3, 0.6)]
    assert all(b > a for a, b in zip(vals, vals[1:]))


def test_ks_bounds():
    a = np.arange(100, dtype=float)
    assert ks_statistic(a, a) == 0.0
    assert ks_statistic(a, a + 1000.0) == 1.0
    with pytest.raises(ValueError):
        ks_statistic(a, np.array([np.nan]))


def test_ks_pvalue_range_and_direction():
    p_small = ks_pvalue(0.3, 1000, 1000)
    p_big = ks_pvalue(0.01, 1000, 1000)
    assert 0.0 <= p_small < p_big <= 1.0
    with pytest.raises(ValueError):
        ks_pvalue(1.5, 10, 10)


def test_psi_none_with_too_few_samples(synth_baseline):
    assert psi(synth_baseline, np.arange(10.0), min_samples=200) is None
    assert psi(synth_baseline, np.full(5, np.nan), min_samples=1) is None


def test_capture_baseline_requires_min_n():
    with pytest.raises(ValueError, match="finite values"):
        capture_baseline(np.arange(50.0), "feature", "too_small")


def test_bucket_edge_value_falls_in_lower_bucket():
    # baseline 0..999: edges at deciles; a value exactly on edge k must
    # count in bucket k-1's mass (side='left', pinned)
    base = np.arange(1000, dtype=float)
    b = capture_baseline(base, "feature", "edge_check")
    edge = b.edges[4]  # the median edge
    from iap.adaptive import bucket_counts
    counts = bucket_counts(np.array([edge]), b.edges)
    assert counts[4] == 1 and counts.sum() == 1


# ---------------------------------------------------------------------------
# baseline serialization
# ---------------------------------------------------------------------------


def test_baseline_roundtrip(tmp_path, synth_baseline, synth_samples):
    _, same, _ = synth_samples
    p = tmp_path / "b.json"
    synth_baseline.save(p)
    loaded = DriftBaseline.load(p)
    assert loaded == synth_baseline
    assert psi(loaded, same) == psi(synth_baseline, same)


def test_baseline_schema_fields(synth_baseline):
    d = synth_baseline.to_dict()
    assert d["x-version"] == 1
    assert d["kind"] in ("signal", "feature")
    assert d["psi_eps"] == PSI_EPS and d["n_buckets"] == 10
    assert len(d["edges"]) == 9 and len(d["expected_frac"]) == 10
    assert abs(sum(d["expected_frac"]) - 1.0) < 1e-12
    assert d["n"] >= 100


def test_signal_eq01_baseline_matches_golden(golden):
    from conftest import REPO_ROOT
    path = REPO_ROOT / "research" / "baselines" / "signal_eq01.json"
    b = DriftBaseline.load(path)
    g = golden["signal_eq01"]
    assert b.n == g["n"] and b.kind == "signal" and b.alpha_id == "EQ01"
    assert g["psi_self"] == 0.0  # same sample => identical fractions
    # the pinned second-half PSI must be reproducible from the baseline's
    # own bucket geometry via brute force once the sample is rebuilt; here
    # we verify the serialized geometry is internally consistent instead
    # (full sample reconstruction is covered by test_alpha_golden's frame)
    assert abs(sum(b.expected_frac) - 1.0) < 1e-12
    assert all(e2 >= e1 for e1, e2 in zip(b.edges, b.edges[1:]))


def test_ic_baseline_roundtrip(tmp_path):
    b = ICBaseline("run_test_ic", "ZZ99", "unit test", 0.05, 0.02, 30,
                   300 * NS_S, "1s")
    p = tmp_path / "ic.json"
    b.save(p)
    assert ICBaseline.load(p) == b
    with pytest.raises(ValueError):
        ICBaseline.from_dict({"x-version": 1, "kind": "feature"})


# ---------------------------------------------------------------------------
# refit policies
# ---------------------------------------------------------------------------


def _ctx(now, last, psi_by=None, ic_z=None):
    return RefitContext(now_ns=now, last_fit_ns=last,
                        psi_by_series=psi_by or {}, ic_z=ic_z)


def test_static_never_refits():
    p = StaticPolicy()
    assert not p.should_refit(_ctx(10**18, 0, {"s": 99.0}, -99.0)).refit


def test_scheduled_period_boundary_edges():
    day = 86_400_000_000_000
    p = ScheduledPolicy(day)
    # same epoch-day: no refit even after 23h59m
    assert not p.should_refit(_ctx(day - 1, 1)).refit
    # boundary exactly on the evaluation time: fires
    assert p.should_refit(_ctx(day, 1)).refit
    # crossing anywhere within the next day: fires once, then not again
    assert p.should_refit(_ctx(day + 5, day - 5)).refit
    assert not p.should_refit(_ctx(day + 10, day + 5)).refit


def test_scheduled_invalid_period_raises():
    with pytest.raises(ValueError):
        ScheduledPolicy(0)


def test_drift_policy_psi_threshold_edge():
    p = DriftTriggeredPolicy(0.25, -2.0, 0)
    assert not p.should_refit(_ctx(1, 0, {"s": 0.25})).refit      # == : no
    assert p.should_refit(_ctx(1, 0, {"s": 0.25 + 1e-9})).refit   # > : yes


def test_drift_policy_icz_threshold_edge():
    p = DriftTriggeredPolicy(0.25, -2.0, 0)
    assert not p.should_refit(_ctx(1, 0, ic_z=-2.0)).refit        # == : no
    assert p.should_refit(_ctx(1, 0, ic_z=-2.0 - 1e-9)).refit     # < : yes


def test_drift_policy_min_gap_blocks_trigger():
    p = DriftTriggeredPolicy(0.25, -2.0, min_refit_gap_ns=3600 * NS_S)
    assert not p.should_refit(_ctx(3599 * NS_S, 0, {"s": 9.9}, -9.9)).refit
    assert p.should_refit(_ctx(3600 * NS_S, 0, {"s": 9.9})).refit


def test_drift_policy_none_monitors_are_silent():
    p = DriftTriggeredPolicy(0.25, -2.0, 0)
    d = p.should_refit(_ctx(1, 0, {"s": None, "t": None}, None))
    assert not d.refit and d.reasons == []


def test_drift_policy_reasons_name_all_breaches():
    p = DriftTriggeredPolicy(0.25, -2.0, 0)
    d = p.should_refit(_ctx(1, 0, {"a": 0.3, "b": 0.9}, -3.0))
    assert d.refit and len(d.reasons) == 3


def test_golden_trigger_sequence(golden):
    g = golden["drift_trigger"]
    p = DriftTriggeredPolicy(
        g["policy"]["psi_threshold"], g["policy"]["ic_z_threshold"],
        g["policy"]["min_refit_gap_ns"],
    )
    last_fit = int(g["initial_last_fit_ns"])
    got = []
    for step in g["steps"]:
        dec = p.should_refit(RefitContext(
            now_ns=int(step["now_ns"]), last_fit_ns=last_fit,
            psi_by_series=step["psi"], ic_z=step["ic_z"],
        ))
        got.append(bool(dec.refit))
        if dec.refit:
            last_fit = int(step["now_ns"])
    assert got == g["expected"]


def test_validate_and_build_policies(adaptive_cfg):
    assert isinstance(build_policy("static", adaptive_cfg), StaticPolicy)
    w = build_policy("scheduled_weekly", adaptive_cfg)
    assert isinstance(w, ScheduledPolicy) and w.period_ns == 604_800_000_000_000
    d = build_policy("drift_triggered", adaptive_cfg)
    assert isinstance(d, DriftTriggeredPolicy)
    with pytest.raises(ValueError):
        build_policy("annealed", adaptive_cfg)


def test_config_validation_rejects_bad_blocks(adaptive_cfg):
    import copy
    ok = copy.deepcopy(adaptive_cfg)
    validate_adaptive_config(ok)  # the shipped config must validate
    for mutate, match in (
        (lambda c: c.pop("block_ns"), "missing field"),
        (lambda c: c.update(block_ns=0), "positive integer"),
        (lambda c: c.update(warmup_ns=1), "warmup_ns"),
        (lambda c: c["policies"].pop("drift_triggered"), "drift_triggered"),
        (lambda c: c["policies"]["drift_triggered"].update(psi_threshold=-1),
         "psi_threshold"),
        (lambda c: c["lifecycle"].pop("watch_ic_gate"), "lifecycle"),
        (lambda c: c["lifecycle"].update(retire_breach_evals=0),
         "retire_breach_evals"),
        (lambda c: c["lifecycle"].update(reactivate_ic_gate=-9.0),
         "reactivate_ic_gate"),
    ):
        bad = copy.deepcopy(adaptive_cfg)
        mutate(bad)
        with pytest.raises(ValueError, match=match):
            validate_adaptive_config(bad)


# ---------------------------------------------------------------------------
# lifecycle state machine
# ---------------------------------------------------------------------------

_LC = LifecycleConfig(watch_ic_gate=0.0, reactivate_ic_gate=0.005,
                      retire_breach_evals=3, reactivate_evals=2)


def _walk(path, cfg=_LC, log=None):
    t = LifecycleTracker(alpha_id="ZZ99", config=cfg, policy="test", log=log)
    states = [t.update((k + 1) * NS_S, v) for k, v in enumerate(path)]
    return t, states


def test_lifecycle_active_to_watch():
    t, states = _walk([0.02, -0.01])
    assert states == [ACTIVE, WATCH]
    assert t.transitions[0].reason.startswith("rolling_ic")


def test_lifecycle_reactivation_from_watch():
    t, states = _walk([-0.01, 0.01, 0.01])
    assert states == [WATCH, WATCH, ACTIVE]
    assert [tr.to_state for tr in t.transitions] == [WATCH, ACTIVE]


def test_lifecycle_persistent_breach_retires():
    t, states = _walk([-0.01, -0.01, -0.01])
    assert states == [WATCH, WATCH, RETIRED]
    assert not t.allocatable


def test_lifecycle_retired_recovers_to_watch_then_active():
    t, states = _walk([-0.01, -0.01, -0.01, 0.01, 0.01, 0.01, 0.01])
    assert states == [WATCH, WATCH, RETIRED, RETIRED, WATCH, WATCH, ACTIVE]


def test_lifecycle_neutral_zone_resets_counters():
    # breaches interrupted by a neutral reading never accumulate to retire
    t, states = _walk([-0.01, -0.01, 0.002, -0.01, -0.01, 0.002, -0.01])
    assert RETIRED not in states
    assert states[-1] == WATCH


def test_lifecycle_none_is_no_evidence():
    t, states = _walk([-0.01, None, -0.01, None, -0.01])
    # Nones neither breach nor recover: breaches accumulate to retirement
    assert states == [WATCH, WATCH, WATCH, WATCH, RETIRED]


def test_lifecycle_bad_config_raises():
    with pytest.raises(ValueError):
        LifecycleConfig(0.0, -0.5, 3, 2)   # reactivate below watch gate
    with pytest.raises(ValueError):
        LifecycleConfig(0.0, 0.0, 0, 1)


def test_golden_lifecycle_sequence(golden):
    g = golden["lifecycle"]
    cfg = LifecycleConfig(**{k: g["config"][k] for k in (
        "watch_ic_gate", "reactivate_ic_gate", "retire_breach_evals",
        "reactivate_evals")})
    t = LifecycleTracker(alpha_id="GOLDEN", config=cfg, policy="golden")
    states = []
    for k, v in enumerate(g["ic_path"]):
        states.append(t.update((k + 1) * int(g["ts_step_ns"]), v))
    assert states == g["expected_states"]
    assert len(t.transitions) == g["expected_transition_count"]
    got = [{"from": tr.from_state, "to": tr.to_state,
            "eval_index": tr.eval_index} for tr in t.transitions]
    assert got == g["expected_transitions"]


def test_lifecycle_log_completeness(tmp_path):
    log = LifecycleLog(tmp_path / "lc.jsonl", truncate=True)
    t, _ = _walk([-0.01, -0.01, -0.01, 0.01, 0.01], log=log)
    rows = log.read_all()
    assert len(rows) == len(t.transitions) == 3
    last_ts = -1
    for r in rows:
        assert r["from"] in STATES and r["to"] in STATES
        assert r["from"] != r["to"]
        assert r["reason"]
        assert r["alpha_id"] == "ZZ99" and r["policy"] == "test"
        assert r["event_ts"] > last_ts
        last_ts = r["event_ts"]


# ---------------------------------------------------------------------------
# adaptive walk-forward backtest
# ---------------------------------------------------------------------------


def test_adaptive_accounting_identity(toy_deployment, adaptive_cfg):
    res = toy_deployment.run(build_policy("drift_triggered", adaptive_cfg))
    bt = res.backtest
    assert abs(bt.total_pnl - (bt.gross_pnl - bt.total_costs)) < 1e-9
    for r in bt.per_instrument.values():
        assert abs(r.total_pnl - (r.gross_pnl - r.total_costs)) < 1e-9


def test_adaptive_determinism(toy_deployment, adaptive_cfg):
    a = toy_deployment.run(build_policy("drift_triggered", adaptive_cfg))
    b = toy_deployment.run(build_policy("drift_triggered", adaptive_cfg))
    assert a.backtest.total_pnl == b.backtest.total_pnl
    assert [e["ts"] for e in a.refit_events] == [e["ts"] for e in b.refit_events]
    for i in a.scores:
        assert np.array_equal(
            a.scores[i]["expected_return"].to_numpy(),
            b.scores[i]["expected_return"].to_numpy(),
        )


def test_adaptive_no_lookahead_shift(toy_frames, adaptive_cfg):
    """Mutating every row at or after a cutoff leaves all deployed scores
    strictly before the cutoff bit-identical (row-level no-lookahead)."""
    bt = Backtester(CostModel.load(CONFIGS_DIR / "execution.json"), _META,
                    BacktestConfig())
    dep = AdaptiveDeployment(_ToyAlpha, toy_frames, adaptive_cfg, bt, 0.25)
    cutoff = dep.block_bounds[len(dep.block_bounds) // 2]

    mutated = {}
    for iid, df in toy_frames.items():
        df2 = df.copy()
        m = df2["exchange_ts"].to_numpy() >= cutoff
        for c in ("x_v1", "mid_price_v1", "label_mid_1s"):
            df2.loc[m, c] = -3.0 * df2.loc[m, c] + 1.0
        mutated[iid] = df2
    dep2 = AdaptiveDeployment(_ToyAlpha, mutated, adaptive_cfg, bt, 0.25)

    pol = build_policy("drift_triggered", adaptive_cfg)
    r1 = dep.run(pol)
    r2 = dep2.run(pol)
    for iid in r1.scores:
        ts = toy_frames[iid]["exchange_ts"].to_numpy()
        past = ts < cutoff
        a = r1.scores[iid]["expected_return"].to_numpy()[past]
        b = r2.scores[iid]["expected_return"].to_numpy()[past]
        assert np.array_equal(a, b)


def test_adaptive_refits_train_only_on_purged_past(toy_deployment):
    from iap.validation.metrics import HORIZONS_NS
    h = HORIZONS_NS["1s"] + toy_deployment.embargo_ns
    for tb in toy_deployment.block_bounds:
        train = toy_deployment._train_window(tb)
        for df in train.values():
            if len(df):
                assert int(df["exchange_ts"].max()) + h < tb


def test_adaptive_warmup_never_trades(toy_deployment, adaptive_cfg):
    res = toy_deployment.run(build_policy("static", adaptive_cfg))
    for iid, sc in res.scores.items():
        warm = sc["exchange_ts"].to_numpy() < toy_deployment.deploy_start
        assert (sc["confidence"].to_numpy()[warm] == 0.0).all()
        assert (sc["expected_return"].to_numpy()[warm] == 0.0).all()


def test_adaptive_retirement_halts_allocation(toy_deployment, adaptive_cfg,
                                              tmp_path):
    # gates the toy alpha can never satisfy: every eval breaches
    lc = LifecycleConfig(watch_ic_gate=0.9, reactivate_ic_gate=0.95,
                         retire_breach_evals=3, reactivate_evals=2)
    log = LifecycleLog(tmp_path / "lc.jsonl", truncate=True)
    res = toy_deployment.run(build_policy("static", adaptive_cfg), lc, log)
    assert res.final_state == RETIRED
    retire_ts = next(t["event_ts"] for t in res.transitions
                     if t["to"] == RETIRED)
    for iid, sc in res.scores.items():
        after = sc["exchange_ts"].to_numpy() >= retire_ts
        assert (sc["confidence"].to_numpy()[after] == 0.0).all()
    # log carries exactly the run's transitions
    assert [r["event_ts"] for r in log.read_all()] == [
        t["event_ts"] for t in res.transitions
    ]


def test_adaptive_static_matches_manual_deployment(toy_deployment,
                                                   adaptive_cfg, toy_frames):
    """StaticPolicy == fit once on the purged warmup, score everything,
    zero out warmup rows, run the standard backtester."""
    res = toy_deployment.run(build_policy("static", adaptive_cfg))
    assert res.refit_count == 1  # the initial deployment fit only

    model = _ToyAlpha()
    model.fit(toy_deployment._train_window(toy_deployment.deploy_start))
    scores = model.score(toy_frames)
    bt = Backtester(CostModel.load(CONFIGS_DIR / "execution.json"), _META,
                    BacktestConfig())
    manual = {}
    for iid, sc in scores.items():
        ts = sc["exchange_ts"].to_numpy()
        live = ts >= toy_deployment.deploy_start
        manual[iid] = pd.DataFrame({
            "exchange_ts": ts,
            "expected_return": np.where(live, sc["expected_return"], 0.0),
            "confidence": np.where(live, sc["confidence"], 0.0),
        })
    ref = bt.run(frames=toy_frames, scores=manual, asset_class="EQUITY")
    assert res.backtest.total_pnl == pytest.approx(ref.total_pnl, abs=1e-9)


def test_adaptive_scheduled_fires_on_day_boundary(adaptive_cfg):
    # frames straddling a UTC day boundary: daily policy refits exactly once
    day = 86_400_000_000_000
    t0 = (1_700_000_000_000_000_000 // day) * day + day - 4 * 3600 * NS_S
    frames = _make_frames(n_rows=6 * 3600, t0=t0, seed=11)
    bt = Backtester(CostModel.load(CONFIGS_DIR / "execution.json"), _META,
                    BacktestConfig())
    dep = AdaptiveDeployment(_ToyAlpha, frames, adaptive_cfg, bt, 0.25)
    res = dep.run(build_policy("scheduled_daily", adaptive_cfg))
    assert res.refit_count == 2  # initial fit + one day-boundary refit
    boundary = ((t0 // day) + 1) * day
    refit_ts = res.refit_events[1]["ts"]
    assert refit_ts >= boundary
    assert refit_ts - boundary < int(adaptive_cfg["block_ns"])
    # weekly never fires inside a two-session sample that crosses no week
    if (t0 // (7 * day)) == ((t0 + 6 * 3600 * NS_S) // (7 * day)):
        res_w = dep.run(build_policy("scheduled_weekly", adaptive_cfg))
        assert res_w.refit_count == 1


def test_adaptive_eval_rows_are_complete(toy_deployment, adaptive_cfg):
    res = toy_deployment.run(build_policy("drift_triggered", adaptive_cfg))
    assert res.n_evals == len(toy_deployment.block_bounds) - 1
    for row in res.eval_rows:
        assert row["state"] in STATES
        assert set(row) >= {"ts", "psi", "ks", "rolling_ic", "ic_z", "refit"}


def test_rolling_ic_z_hand_computed():
    base = ICBaseline("b", "ZZ99", "", ic_mean=0.2, ic_std=0.1,
                      n_buckets_baseline=30, bucket_ns=10 * NS_S, horizon="1s")
    # two buckets of 10 perfectly correlated pairs => bucket ICs [1, 1]
    ts = np.concatenate([np.arange(10), 10 * NS_S + np.arange(10)]).astype(np.int64)
    x = np.concatenate([np.arange(10.0), np.arange(10.0)])
    y = 2.0 * x + 1.0
    r = rolling_ic_z(base, ts, x, y, min_buckets=2)
    assert r.rolling_ic == pytest.approx(1.0, abs=1e-12)
    assert r.z == pytest.approx((1.0 - 0.2) / (0.1 / math.sqrt(2)), abs=1e-9)
    # below min_buckets: silent
    r2 = rolling_ic_z(base, ts, x, y, min_buckets=3)
    assert r2.rolling_ic is None and r2.z is None
