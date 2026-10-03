"""Adaptability-layer tests (iap.adaptive + iap.backtest.adaptive).

Covers: PSI/KS correctness vs independent brute force and vs the pinned
goldens (tests/golden/expected_adaptive.json), baseline serialization
round-trips, refit-policy trigger boundaries, the lifecycle state machine
(incl. re-activation and log completeness), and the adaptive walk-forward
backtest: accounting identity, determinism, no-lookahead (shift test),
warmup/retirement gating and config validation.

The defaults are the v1.5.0 rules — the CUSUM retirement rule, the
two-sample HAC drift z with the pair-count-weighted rolling IC, and the
research backtester's cost-aware / fill-capped / row-blocked configuration.
Every rule up to v1.4.0 is exercised under its explicit legacy name
(``LifecycleConfig.legacy``, ``rolling_ic_z`` / ``ic_z_method="legacy"``,
``BacktestConfig.legacy``, ``CostModel.with_linear_impact``); a test that
pins the old numbers says "legacy" in its name.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
from conftest import CONFIGS_DIR, GOLDEN_DIR
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
    rolling_ic_z_hac,
    validate_adaptive_config,
)
from iap.adaptive.drift import BASELINE_VERSION, DEFAULT_IC_Z_METHOD
from iap.adaptive.lifecycle import DEFAULT_BREACH_RULE, DEFAULT_CUSUM_H, DEFAULT_CUSUM_K
from iap.alpha.base import LinearAlpha, col
from iap.backtest import BacktestConfig, Backtester, CostModel
from iap.backtest.adaptive import AdaptiveDeployment
from iap.core.rng import SplitMix64
from iap.features.registry import registry_hash

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
    return load_adaptive_config(CONFIGS_DIR / "strategies" / "strategies.json")


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
    correlated with the next-second return label.  The displayed L1 sizes
    (which the default fill cap of the research backtester reads) are ten
    times the default ``max_pos_qty``, so the cap never binds here."""
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
        frames[iid] = pd.DataFrame(
            {
                "exchange_ts": ts,
                "x_v1": sig,
                "mid_price_v1": mid,
                "spread_ticks_v1": np.ones(n_rows),
                "depth_bid_l1_v1": np.full(n_rows, 10_000.0),
                "depth_ask_l1_v1": np.full(n_rows, 10_000.0),
                "label_mid_1s": label,
                "label_valid_1s": valid,
            }
        )
    return frames


_META = {
    1: {"asset_class": "EQUITY", "tick_size": 0.01, "lot_size": 1, "adv": 1e6, "ref_price": 100.0},
    2: {"asset_class": "EQUITY", "tick_size": 0.01, "lot_size": 1, "adv": 1e6, "ref_price": 100.0},
}


@pytest.fixture(scope="module")
def toy_frames():
    return _make_frames()


@pytest.fixture(scope="module")
def toy_deployment(toy_frames, adaptive_cfg):
    bt = Backtester(
        CostModel.load(CONFIGS_DIR / "execution" / "execution.json"), _META, BacktestConfig()
    )
    return AdaptiveDeployment(
        _ToyAlpha,
        toy_frames,
        adaptive_cfg,
        bt,
        psi_threshold=float(adaptive_cfg["policies"]["drift_triggered"]["psi_threshold"]),
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
    assert list(synth_baseline.expected_frac) == pytest.approx(gb["expected_frac"], abs=TOL)


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
    bf = brute_psi(synth_baseline.edges, synth_baseline.expected_frac, np.full(500, 99.0))
    assert abs(v - bf) <= TOL
    assert v > 2.0  # total shift is a huge PSI


def test_psi_monotone_under_growing_shift(synth_baseline, synth_samples):
    base, _, _ = synth_samples
    vals = [psi(synth_baseline, base[:2000] + s) for s in (0.0, 0.1, 0.3, 0.6)]
    assert all(b > a for a, b in zip(vals, vals[1:], strict=False))


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
    assert d["x-version"] == BASELINE_VERSION == 2
    # round-3 provenance: a baseline records the registry it was captured
    # against, so a loader can refuse one whose feature semantics moved.
    assert d["feature_version"] == registry_hash()
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
    assert all(e2 >= e1 for e1, e2 in zip(b.edges, b.edges[1:], strict=False))


def test_rolling_ic_matches_golden(golden):
    """The `rolling_ic` block of expected_adaptive.json, re-derived here.

    Java asserts this section (`AdaptiveGoldenTest.
    rollingIcMatchesThePythonResearchLabel`), but the vector was PRODUCED by
    the Python reference (`python/tools/make_golden_adaptive.py`) — so
    without this test a regression in `rolling_ic_z_hac` / `rolling_ic_z` /
    `bucket_ics` would leave the committed golden stale and Java would
    happily keep passing against the stale file.  Everything below is rebuilt
    from the golden's own embedded `mid_series` / `signals`; nothing is read
    from the frame.

    x-version 2 (v1.5.0): `rolling_ic` is the PAIR-COUNT-WEIGHTED mean bucket
    IC of the default monitor (`rolling_ic_z_hac`); the legacy unweighted
    mean (`rolling_ic_z`) is pinned beside it as `rolling_ic_unweighted`.
    Both are asserted.
    """
    assert golden["x-version"] == 2
    g = golden["rolling_ic"]
    assert g["weighting"] == "pair_count"
    h_ns = int(g["horizon_ns"])
    window_ns = int(g["window_ns"])
    bucket_ns = int(g["bucket_ns"])
    min_buckets = int(g["min_buckets"])

    mids = np.asarray(g["mid_series"], dtype=np.float64)
    mid_ts = mids[:, 0].astype(np.int64)
    mid_v = mids[:, 1]
    sigs = np.asarray(g["signals"], dtype=np.float64)
    sig_ts = sigs[:, 0].astype(np.int64)
    sig_v, sig_mid = sigs[:, 1], sigs[:, 2]
    assert np.all(np.diff(mid_ts) >= 0), "mid stream must be event-time sorted"

    # research label: the mid prevailing AT OR BEFORE t + h, and only once
    # the mid stream has actually been seen through t + h (no lookahead).
    idx = np.searchsorted(mid_ts, sig_ts + h_ns, side="right") - 1
    have = idx >= 0
    fwd = np.where(have, mid_v[np.maximum(idx, 0)], np.nan)
    observed = (sig_ts + h_ns) <= mid_ts[-1]
    ret = np.where(have & observed, fwd / sig_mid - 1.0, np.nan)

    base = ICBaseline(
        name="golden",
        alpha_id=g["alpha_id"],
        source="golden",
        ic_mean=0.0,
        ic_std=1.0,
        n_buckets_baseline=8,
        bucket_ns=bucket_ns,
        horizon=g["horizon"],
    )
    evals = g["evaluations"]
    assert len(evals) >= 5, "the golden must pin several evaluation times"
    for i, ev in enumerate(evals):
        t_eval = int(ev["t"])
        m = (sig_ts >= t_eval - window_ns) & (sig_ts + h_ns <= t_eval)
        res = rolling_ic_z_hac(base, sig_ts[m], sig_v[m], ret[m], min_buckets)
        legacy = rolling_ic_z(base, sig_ts[m], sig_v[m], ret[m], min_buckets)
        assert int(np.sum(m & np.isfinite(ret))) == ev["n_matured"], (
            f"eval {i}: matured-row count drifted from the golden"
        )
        assert res.n_buckets == ev["n_buckets"], f"eval {i}: bucket count"
        assert legacy.n_buckets == ev["n_buckets"], f"eval {i}: legacy bucket count"
        if ev["rolling_ic"] is None:
            assert res.rolling_ic is None, f"eval {i}: expected a null IC"
        else:
            assert res.rolling_ic == pytest.approx(ev["rolling_ic"], abs=1e-10), (
                f"eval {i}: rolling_ic (pair-count weighted, the default)"
            )
        if ev["rolling_ic_unweighted"] is None:
            assert legacy.rolling_ic is None, f"eval {i}: expected a null legacy IC"
        else:
            assert legacy.rolling_ic == pytest.approx(ev["rolling_ic_unweighted"], abs=1e-10), (
                f"eval {i}: rolling_ic_unweighted (the legacy mean)"
            )
    # the two means are different statistics on this frame, not one number twice
    assert all(abs(ev["rolling_ic"] - ev["rolling_ic_unweighted"]) > 1e-3 for ev in evals)


def test_ic_baseline_roundtrip(tmp_path):
    b = ICBaseline(
        "run_test_ic",
        "ZZ99",
        "unit test",
        0.05,
        0.02,
        30,
        300 * NS_S,
        "1s",
        feature_version=registry_hash(),
    )
    p = tmp_path / "ic.json"
    b.save(p)
    assert ICBaseline.load(p) == b
    with pytest.raises(ValueError):
        ICBaseline.from_dict({"x-version": BASELINE_VERSION, "kind": "feature"})
    with pytest.raises(ValueError):  # superseded schema
        ICBaseline.from_dict({**b.to_dict(), "x-version": 1})


def test_baseline_loader_rejects_a_foreign_feature_registry(tmp_path):
    """Pinned (API_ADAPTIVE section 4): a baseline captured against another
    feature registry describes a feature whose semantics may have changed
    under the same name, so PSI/IC against it is meaningless."""
    b = ICBaseline(
        "run_test_ic",
        "ZZ99",
        "unit test",
        0.05,
        0.02,
        30,
        300 * NS_S,
        "1s",
        feature_version=registry_hash(),
    )
    p = tmp_path / "ic.json"
    b.save(p)
    blob = json.loads(p.read_text())
    blob["feature_version"] = "0" * 64
    p.write_text(json.dumps(blob))
    with pytest.raises(ValueError, match="feature_version"):
        ICBaseline.load(p)
    # explicit opt-out for tooling that inspects a historic file
    assert ICBaseline.load(p, expected_feature_version=None).ic_mean == 0.05


def test_drift_baseline_carries_the_registry_hash(synth_baseline, tmp_path):
    p = tmp_path / "b.json"
    synth_baseline.save(p)
    assert DriftBaseline.load(p).feature_version == registry_hash()
    blob = json.loads(p.read_text())
    del blob["feature_version"]
    p.write_text(json.dumps(blob))
    with pytest.raises(ValueError, match="feature_version"):
        DriftBaseline.load(p)


# ---------------------------------------------------------------------------
# refit policies
# ---------------------------------------------------------------------------


def _ctx(now, last, psi_by=None, ic_z=None):
    return RefitContext(now_ns=now, last_fit_ns=last, psi_by_series=psi_by or {}, ic_z=ic_z)


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
    assert not p.should_refit(_ctx(1, 0, {"s": 0.25})).refit  # == : no
    assert p.should_refit(_ctx(1, 0, {"s": 0.25 + 1e-9})).refit  # > : yes


def test_drift_policy_icz_threshold_edge():
    p = DriftTriggeredPolicy(0.25, -2.0, 0)
    assert not p.should_refit(_ctx(1, 0, ic_z=-2.0)).refit  # == : no
    assert p.should_refit(_ctx(1, 0, ic_z=-2.0 - 1e-9)).refit  # < : yes


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
        g["policy"]["psi_threshold"],
        g["policy"]["ic_z_threshold"],
        g["policy"]["min_refit_gap_ns"],
    )
    last_fit = int(g["initial_last_fit_ns"])
    got = []
    for step in g["steps"]:
        dec = p.should_refit(
            RefitContext(
                now_ns=int(step["now_ns"]),
                last_fit_ns=last_fit,
                psi_by_series=step["psi"],
                ic_z=step["ic_z"],
            )
        )
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
        (lambda c: c["policies"]["drift_triggered"].update(psi_threshold=-1), "psi_threshold"),
        (lambda c: c["lifecycle"].pop("watch_ic_gate"), "lifecycle"),
        (lambda c: c["lifecycle"].update(retire_breach_evals=0), "retire_breach_evals"),
        (lambda c: c["lifecycle"].update(reactivate_ic_gate=-9.0), "reactivate_ic_gate"),
        # x-version 2 (v1.5.0): the block must NAME its z and its retirement
        # rule; a v1.4.0 block is rejected, not read under the other rule
        (lambda c: c.update({"x-version": 1}), "x-version must be 2"),
        (lambda c: c.pop("x-version"), "x-version must be 2"),
        (lambda c: c.pop("ic_z_method"), "ic_z_method"),
        (lambda c: c.update(ic_z_method="pinned"), "ic_z_method"),
        (lambda c: c["lifecycle"].pop("breach_rule"), "breach_rule"),
        (lambda c: c["lifecycle"].update(breach_rule="ewma"), "unknown breach_rule"),
        (lambda c: c["lifecycle"].pop("cusum_h"), "cusum_h"),
        (lambda c: c["lifecycle"].pop("cusum_k"), "cusum_k"),
    ):
        bad = copy.deepcopy(adaptive_cfg)
        mutate(bad)
        with pytest.raises(ValueError, match=match):
            validate_adaptive_config(bad)


def test_shipped_adaptive_config_names_the_default_rules_and_legacy_validates(adaptive_cfg):
    """The pinned block names the v1.5.0 defaults; the same block naming the
    legacy z and the legacy retirement rule is still a valid configuration
    (and may then omit the CUSUM parameters)."""
    import copy

    assert adaptive_cfg["x-version"] == 2
    assert adaptive_cfg["ic_z_method"] == DEFAULT_IC_Z_METHOD == "hac"
    lc = LifecycleConfig.from_config(adaptive_cfg["lifecycle"])
    assert lc.breach_rule == DEFAULT_BREACH_RULE == "cusum"
    assert (lc.cusum_k, lc.cusum_h) == (DEFAULT_CUSUM_K, DEFAULT_CUSUM_H) == (0.0025, 0.01)
    assert lc.to_dict() == adaptive_cfg["lifecycle"]

    old = copy.deepcopy(adaptive_cfg)
    old["ic_z_method"] = "legacy"
    old["lifecycle"]["breach_rule"] = "consecutive"
    del old["lifecycle"]["cusum_k"], old["lifecycle"]["cusum_h"]
    validate_adaptive_config(old)
    assert LifecycleConfig.from_config(old["lifecycle"]) == LifecycleConfig.legacy(
        lc.watch_ic_gate, lc.reactivate_ic_gate, lc.retire_breach_evals, lc.reactivate_evals
    )


# ---------------------------------------------------------------------------
# lifecycle state machine
# ---------------------------------------------------------------------------

# The default retirement rule (CUSUM, cusum_k 0.0025, cusum_h 0.01) ...
_LC = LifecycleConfig(
    watch_ic_gate=0.0, reactivate_ic_gate=0.005, retire_breach_evals=3, reactivate_evals=2
)
# ... and the same gates under the rule up to v1.4.0, named.
_LC_LEGACY = LifecycleConfig.legacy(
    watch_ic_gate=0.0, reactivate_ic_gate=0.005, retire_breach_evals=3, reactivate_evals=2
)


def test_lifecycle_default_rule_is_cusum_and_legacy_names_the_consecutive_rule():
    assert _LC.breach_rule == "cusum" and (_LC.cusum_k, _LC.cusum_h) == (0.0025, 0.01)
    assert _LC_LEGACY.breach_rule == "consecutive"
    assert _LC_LEGACY == LifecycleConfig(0.0, 0.005, 3, 2, breach_rule="consecutive")
    assert (_LC_LEGACY.watch_ic_gate, _LC_LEGACY.reactivate_ic_gate) == (0.0, 0.005)
    assert (_LC_LEGACY.retire_breach_evals, _LC_LEGACY.reactivate_evals) == (3, 2)


def _walk(path, cfg=_LC, log=None):
    t = LifecycleTracker(alpha_id="ZZ99", config=cfg, policy="test", log=log)
    states = [t.update((k + 1) * NS_S, v) for k, v in enumerate(path)]
    return t, states


@pytest.mark.parametrize("cfg", [_LC, _LC_LEGACY], ids=["cusum", "legacy_consecutive"])
def test_lifecycle_active_to_watch(cfg):
    t, states = _walk([0.02, -0.01], cfg=cfg)
    assert states == [ACTIVE, WATCH]
    assert t.transitions[0].reason.startswith("rolling_ic")


@pytest.mark.parametrize("cfg", [_LC, _LC_LEGACY], ids=["cusum", "legacy_consecutive"])
def test_lifecycle_reactivation_from_watch(cfg):
    t, states = _walk([-0.01, 0.01, 0.01], cfg=cfg)
    assert states == [WATCH, WATCH, ACTIVE]
    assert [tr.to_state for tr in t.transitions] == [WATCH, ACTIVE]


def test_lifecycle_legacy_consecutive_persistent_breach_retires():
    t, states = _walk([-0.01, -0.01, -0.01], cfg=_LC_LEGACY)
    assert states == [WATCH, WATCH, RETIRED]
    assert not t.allocatable
    assert t.cusum == 0.0  # the legacy rule never moves the CUSUM statistic


def test_lifecycle_cusum_persistent_breach_retires():
    """Default rule, disjoint readings (new_fraction 1), gate 0, k 0.0025,
    h 0.01: S = 0 + (0 - (-0.01) - 0.0025) = 0.0075 on the reading that
    enters WATCH (S is kept); the second breach takes S to 0.015 >= 0.01 and
    retires — one reading earlier than three consecutive breaches."""
    t = LifecycleTracker(alpha_id="ZZ99", config=_LC, policy="test")
    assert t.update(1 * NS_S, -0.01) == WATCH
    assert t.cusum == pytest.approx(0.0075, abs=1e-15)
    assert t.update(2 * NS_S, -0.01) == RETIRED
    assert not t.allocatable
    assert t.cusum == 0.0  # reset by the verdict
    assert "CUSUM 0.015000 >= 0.01" in t.transitions[-1].reason
    assert t.update(3 * NS_S, -0.01) == RETIRED
    assert t.breach_count == 0  # not used by the CUSUM rule
    # a shallow breach inside the slack adds nothing: 0 - (-0.002) - 0.0025 < 0
    shallow = LifecycleTracker(alpha_id="ZZ99", config=_LC, policy="test")
    assert [shallow.update(k * NS_S, -0.002) for k in range(1, 40)] == [WATCH] * 39
    assert shallow.cusum == 0.0


def test_lifecycle_legacy_consecutive_retired_recovers_to_watch_then_active():
    t, states = _walk([-0.01, -0.01, -0.01, 0.01, 0.01, 0.01, 0.01], cfg=_LC_LEGACY)
    assert states == [WATCH, WATCH, RETIRED, RETIRED, WATCH, WATCH, ACTIVE]


def test_lifecycle_cusum_retired_recovers_to_watch_then_active():
    """Default rule on the same path: retired on the second breach (S 0.015),
    the third breach lands in RETIRED, and the recovery rule is the legacy
    one — two readings >= 0.005 to WATCH, two more to ACTIVE."""
    t, states = _walk([-0.01, -0.01, -0.01, 0.01, 0.01, 0.01, 0.01])
    assert states == [WATCH, RETIRED, RETIRED, RETIRED, WATCH, WATCH, ACTIVE]
    assert [(tr.from_state, tr.to_state) for tr in t.transitions] == [
        (ACTIVE, WATCH),
        (WATCH, RETIRED),
        (RETIRED, WATCH),
        (WATCH, ACTIVE),
    ]
    assert t.cusum == 0.0


def test_lifecycle_legacy_consecutive_neutral_zone_resets_counters():
    # breaches interrupted by a neutral reading never accumulate to retire
    t, states = _walk([-0.01, -0.01, 0.002, -0.01, -0.01, 0.002, -0.01], cfg=_LC_LEGACY)
    assert RETIRED not in states
    assert states[-1] == WATCH


def test_lifecycle_cusum_neutral_zone_drains_but_does_not_reset():
    """Default rule: a neutral reading (gate <= ic < reactivate gate) drains
    S by ``ic + k`` instead of resetting a counter.  Gate 0, k 0.0025,
    h 0.01, new_fraction 1; each -0.005 adds 0.0025, each 0.002 takes 0.0045:

        reading  -0.005  0.002  -0.005  -0.005  0.002   -0.005
        S        0.0025  0      0.0025  0.005   0.0005  0.003

    then three more -0.005 readings: 0.0055, 0.008, 0.0105 >= 0.01 -> RETIRED.
    """
    t = LifecycleTracker(alpha_id="ZZ99", config=_LC, policy="test")
    path = [-0.005, 0.002, -0.005, -0.005, 0.002, -0.005]
    want = [0.0025, 0.0, 0.0025, 0.005, 0.0005, 0.003]
    for k, (v, s) in enumerate(zip(path, want, strict=True)):
        assert t.update((k + 1) * NS_S, v) == WATCH
        assert t.cusum == pytest.approx(s, abs=1e-15)
    assert t.update(7 * NS_S, -0.005) == WATCH
    assert t.update(8 * NS_S, -0.005) == WATCH
    assert t.cusum == pytest.approx(0.008, abs=1e-15)
    assert t.update(9 * NS_S, -0.005) == RETIRED
    # the legacy path of the test above, under the default rule, retires on
    # its second breach: -0.01 twice is S = 0.015 whatever follows
    _, states = _walk([-0.01, -0.01, 0.002, -0.01, -0.01, 0.002, -0.01])
    assert states == [WATCH, RETIRED] + [RETIRED] * 5


def test_lifecycle_legacy_consecutive_none_is_no_evidence():
    t, states = _walk([-0.01, None, -0.01, None, -0.01], cfg=_LC_LEGACY)
    # Nones neither breach nor recover: breaches accumulate to retirement
    assert states == [WATCH, WATCH, WATCH, WATCH, RETIRED]


def test_lifecycle_cusum_none_is_no_evidence():
    """Default rule: a missing reading moves neither the state nor S, so the
    evidence accumulates across it — S 0.0075, (None), 0.015 -> RETIRED."""
    t = LifecycleTracker(alpha_id="ZZ99", config=_LC, policy="test")
    assert t.update(1 * NS_S, -0.01) == WATCH
    assert t.update(2 * NS_S, None) == WATCH
    assert t.cusum == pytest.approx(0.0075, abs=1e-15)
    assert t.update(3 * NS_S, -0.01) == RETIRED
    assert t.update(4 * NS_S, None) == RETIRED
    assert t.update(5 * NS_S, -0.01) == RETIRED
    assert t.eval_index == 5 and len(t.transitions) == 2


def test_lifecycle_cusum_weights_a_reading_by_its_new_fraction():
    """The replay passes block / window = 1/8: eight readings of one bad
    stretch are worth one disjoint reading.  -0.01 at new_fraction 0.125
    adds 0.125 * 0.0075 = 0.0009375 per reading, so S reaches 0.01 on the
    11th (0.0103125) — and the reading must itself be a breach to retire."""
    t = LifecycleTracker(alpha_id="ZZ99", config=_LC, policy="test")
    states = [t.update(k * NS_S, -0.01, new_fraction=0.125) for k in range(1, 11)]
    assert states == [WATCH] * 10
    assert t.cusum == pytest.approx(10 * 0.0009375, abs=1e-15)
    assert t.update(11 * NS_S, -0.01, new_fraction=0.125) == RETIRED
    with pytest.raises(ValueError, match="new_fraction"):
        LifecycleTracker(alpha_id="ZZ99", config=_LC).update(1, -0.01, new_fraction=1.5)
    # the legacy rule does not read new_fraction: three breaches retire
    legacy = LifecycleTracker(alpha_id="ZZ99", config=_LC_LEGACY, policy="test")
    assert [legacy.update(k * NS_S, -0.01, new_fraction=0.125) for k in range(1, 4)] == [
        WATCH,
        WATCH,
        RETIRED,
    ]


def test_lifecycle_bad_config_raises():
    with pytest.raises(ValueError):
        LifecycleConfig(0.0, -0.5, 3, 2)  # reactivate below watch gate
    with pytest.raises(ValueError):
        LifecycleConfig(0.0, 0.0, 0, 1)
    with pytest.raises(ValueError, match="unknown breach_rule"):
        LifecycleConfig(0.0, 0.005, 3, 2, breach_rule="ewma")
    with pytest.raises(ValueError, match="cusum_h"):
        LifecycleConfig(0.0, 0.005, 3, 2, cusum_h=0.0)
    with pytest.raises(ValueError, match="cusum_k"):
        LifecycleConfig(0.0, 0.005, 3, 2, cusum_k=-1e-9)
    with pytest.raises(ValueError, match="names no 'breach_rule'"):  # a v1.4.0 block
        LifecycleConfig.from_config(
            {
                "watch_ic_gate": 0.0,
                "reactivate_ic_gate": 0.005,
                "retire_breach_evals": 3,
                "reactivate_evals": 2,
            }
        )


@pytest.mark.parametrize(
    ("section", "rule"),
    [("lifecycle", "cusum"), ("lifecycle_legacy_consecutive", "consecutive")],
)
def test_golden_lifecycle_sequence(golden, adaptive_cfg, section, rule):
    """x-version 2 (v1.5.0): ``lifecycle`` is the default rule of the pinned
    config (CUSUM), ``lifecycle_legacy_consecutive`` the rule up to v1.4.0 on
    the same path; states, transitions and the CUSUM statistic after every
    reading are pinned for both."""
    g = golden[section]
    cfg = LifecycleConfig.from_config(g["config"])
    assert cfg.to_dict() == g["config"] and cfg.breach_rule == rule
    shipped = LifecycleConfig.from_config(adaptive_cfg["lifecycle"])
    if section == "lifecycle":
        assert cfg == shipped, "the default golden runs the shipped adaptive.lifecycle block"
    else:
        assert cfg == LifecycleConfig.legacy(
            shipped.watch_ic_gate,
            shipped.reactivate_ic_gate,
            shipped.retire_breach_evals,
            shipped.reactivate_evals,
        )
    # the replay's share of new rows per reading: block / IC window
    assert g["new_fraction"] == adaptive_cfg["block_ns"] / adaptive_cfg["ic_window_ns"] == 0.125
    assert g["ic_path"] == golden["lifecycle"]["ic_path"], "both rules read the same path"
    t = LifecycleTracker(alpha_id="GOLDEN", config=cfg, policy="golden")
    states, cusum = [], []
    informative = g["ic_informative"]
    assert len(informative) == len(g["ic_path"]) == len(g["expected_cusum"])
    for k, v in enumerate(g["ic_path"]):
        states.append(
            t.update(
                (k + 1) * int(g["ts_step_ns"]),
                v,
                informative=bool(informative[k]),
                new_fraction=float(g["new_fraction"]),
            )
        )
        cusum.append(t.cusum)
    assert states == g["expected_states"]
    assert cusum == pytest.approx(g["expected_cusum"], abs=1e-12)
    # the golden path holds a block of six UNINFORMATIVE breaches (readings
    # 22-27, a frozen IC window re-read) that move nothing: neither the
    # state nor the CUSUM statistic
    quiet = [k for k, flag in enumerate(informative) if not flag]
    assert quiet == [21, 22, 23, 24, 25, 26]
    assert all(v is not None and v < cfg.watch_ic_gate for v in g["ic_path"][21:27])
    assert all(states[k] == states[20] == "WATCH" for k in quiet)
    assert all(cusum[k] == cusum[20] for k in quiet)
    if rule == "consecutive":
        assert all(c == 0.0 for c in cusum)
    assert len(t.transitions) == g["expected_transition_count"]
    got = [
        {"from": tr.from_state, "to": tr.to_state, "eval_index": tr.eval_index}
        for tr in t.transitions
    ]
    assert got == g["expected_transitions"]


@pytest.mark.parametrize("cfg", [_LC, _LC_LEGACY], ids=["cusum", "legacy_consecutive"])
def test_lifecycle_log_completeness(tmp_path, cfg):
    # three transitions under either rule: WATCH, RETIRED (on the second
    # breach under CUSUM, the third under the legacy count), back to WATCH
    log = LifecycleLog(tmp_path / "lc.jsonl", truncate=True)
    t, _ = _walk([-0.01, -0.01, -0.01, 0.01, 0.01], cfg=cfg, log=log)
    rows = log.read_all()
    assert len(rows) == len(t.transitions) == 3
    assert [r["to"] for r in rows] == [WATCH, RETIRED, WATCH]
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
    bt = Backtester(
        CostModel.load(CONFIGS_DIR / "execution" / "execution.json"), _META, BacktestConfig()
    )
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
    for _iid, sc in res.scores.items():
        warm = sc["exchange_ts"].to_numpy() < toy_deployment.deploy_start
        assert (sc["confidence"].to_numpy()[warm] == 0.0).all()
        assert (sc["expected_return"].to_numpy()[warm] == 0.0).all()


@pytest.mark.parametrize("rule", ["cusum", "legacy_consecutive"])
def test_adaptive_retirement_halts_allocation(toy_deployment, adaptive_cfg, tmp_path, rule):
    # gates the toy alpha can never satisfy: every eval breaches
    gates = dict(
        watch_ic_gate=0.9, reactivate_ic_gate=0.95, retire_breach_evals=3, reactivate_evals=2
    )
    lc = LifecycleConfig(**gates) if rule == "cusum" else LifecycleConfig.legacy(**gates)
    assert lc.breach_rule == ("cusum" if rule == "cusum" else "consecutive")
    log = LifecycleLog(tmp_path / "lc.jsonl", truncate=True)
    res = toy_deployment.run(build_policy("static", adaptive_cfg), lc, log)
    assert res.final_state == RETIRED
    retire_ts = next(t["event_ts"] for t in res.transitions if t["to"] == RETIRED)
    # Every reading is ~0.9 under the gate.  Legacy: the third consecutive
    # breach retires.  CUSUM: each reading is 1/8 new, so the first adds
    # about 0.125 * 0.9 >> cusum_h = 0.01 on entering WATCH, and the second
    # breach — the first WATCH reading — retires.
    informative = [r["block"] for r in res.eval_rows if r["informative"]]
    retire_block = next(r["block"] for r in res.eval_rows if r["ts"] == retire_ts)
    assert retire_block == informative[1 if rule == "cusum" else 2]
    for _iid, sc in res.scores.items():
        after = sc["exchange_ts"].to_numpy() >= retire_ts
        assert (sc["confidence"].to_numpy()[after] == 0.0).all()
    # log carries exactly the run's transitions
    assert [r["event_ts"] for r in log.read_all()] == [t["event_ts"] for t in res.transitions]


def test_adaptive_static_matches_manual_deployment(toy_deployment, adaptive_cfg, toy_frames):
    """StaticPolicy == fit once on the purged warmup, score everything,
    zero out warmup rows, run the standard backtester."""
    res = toy_deployment.run(build_policy("static", adaptive_cfg))
    assert res.refit_count == 1  # the initial deployment fit only

    model = _ToyAlpha()
    model.fit(toy_deployment._train_window(toy_deployment.deploy_start))
    scores = model.score(toy_frames)
    # the default position policy and row block need the label horizon
    bt = Backtester(
        CostModel.load(CONFIGS_DIR / "execution" / "execution.json"), _META, BacktestConfig()
    ).for_horizon(_ToyAlpha.horizon)
    manual = {}
    for iid, sc in scores.items():
        ts = sc["exchange_ts"].to_numpy()
        live = ts >= toy_deployment.deploy_start
        manual[iid] = pd.DataFrame(
            {
                "exchange_ts": ts,
                "expected_return": np.where(live, sc["expected_return"], 0.0),
                "confidence": np.where(live, sc["confidence"], 0.0),
            }
        )
    ref = bt.run(frames=toy_frames, scores=manual, asset_class="EQUITY")
    assert res.backtest.total_pnl == pytest.approx(ref.total_pnl, abs=1e-9)
    assert res.backtest.trade_count == ref.trade_count > 0


def test_adaptive_backtest_runs_the_default_research_rules(toy_deployment, adaptive_cfg):
    """The deployment hands its scores to the research backtester under the
    v1.5.0 defaults, with the alpha's label horizon set."""
    cfg = toy_deployment.backtester.config
    assert cfg.position_policy == "cost_aware" and cfg.cap_fills_at_l1
    assert cfg.block_rows_label() == "scored_rows:1s" and cfg.horizon_ns == NS_S
    assert toy_deployment.backtester.cost_model.impact_model == "sqrt"
    res = toy_deployment.run(build_policy("static", adaptive_cfg))
    for r in res.backtest.per_instrument.values():
        # cost-aware: flat or the full position (the 10 000 displayed never caps)
        assert set(np.unique(r.positions)) <= {-1000.0, 0.0, 1000.0}
        assert r.trade_count > 0
        # the last row has no valid label: it is blocked and makes no decision
        assert r.positions[-1] == r.positions[-2]


def test_adaptive_legacy_rules_are_selectable_by_name(toy_frames, adaptive_cfg, toy_deployment):
    """Every rule up to v1.4.0, named: sign positions / uncapped fills /
    every row traded, linear impact, the legacy z, the consecutive rule."""
    bt = Backtester(
        CostModel.load(CONFIGS_DIR / "execution" / "execution.json").with_linear_impact(),
        _META,
        BacktestConfig.legacy(),
    )
    legacy_cfg = {**adaptive_cfg, "ic_z_method": "legacy"}
    validate_adaptive_config(legacy_cfg)
    dep = AdaptiveDeployment(_ToyAlpha, toy_frames, legacy_cfg, bt, 0.25)
    assert dep.backtester.config.position_policy == "sign"
    assert dep.backtester.cost_model.impact_model == "linear"
    shipped = LifecycleConfig.from_config(adaptive_cfg["lifecycle"])
    lc = LifecycleConfig.legacy(
        shipped.watch_ic_gate,
        shipped.reactivate_ic_gate,
        shipped.retire_breach_evals,
        shipped.reactivate_evals,
    )
    pol = build_policy("static", adaptive_cfg)
    old = dep.run(pol, lc)
    new = toy_deployment.run(pol, shipped)
    bt_old = old.backtest
    assert abs(bt_old.total_pnl - (bt_old.gross_pnl - bt_old.total_costs)) < 1e-9
    # the sign rule re-decides on every row; the cost-aware rule trades only
    # forecasts that clear the round-trip cost
    assert bt_old.trade_count > 10 * new.backtest.trade_count > 0
    # the deployed (shadow) scores do not depend on the backtest rules
    for i in old.scores:
        assert np.array_equal(
            old.scores[i]["expected_return"].to_numpy(),
            new.scores[i]["expected_return"].to_numpy(),
        )
    # the two z's: same windows and bucket counts; on these uniform 1 s rows
    # every full bucket holds the same number of pairs, so the legacy
    # unweighted rolling IC and the pair-weighted one agree closely while
    # the z statistics differ (the HAC z adds the baseline's own variance)
    rows_old = [r for r in old.eval_rows if r["ic_z"] is not None]
    rows_new = [r for r in new.eval_rows if r["ic_z"] is not None]
    assert len(rows_old) == len(rows_new) > 0
    for a, b in zip(rows_old, rows_new, strict=True):
        assert a["ts"] == b["ts"] and a["n_ic_buckets"] == b["n_ic_buckets"]
        assert a["rolling_ic"] == pytest.approx(b["rolling_ic"], abs=5e-3)
    assert any(abs(a["ic_z"] - b["ic_z"]) > 1e-6 for a, b in zip(rows_old, rows_new, strict=True))


def test_adaptive_config_must_name_the_ic_z_method(toy_frames, adaptive_cfg):
    bt = Backtester(
        CostModel.load(CONFIGS_DIR / "execution" / "execution.json"), _META, BacktestConfig()
    )
    pol = build_policy("static", adaptive_cfg)
    unnamed = {k: v for k, v in adaptive_cfg.items() if k != "ic_z_method"}
    with pytest.raises(ValueError, match="names no 'ic_z_method'"):
        AdaptiveDeployment(_ToyAlpha, toy_frames, unnamed, bt, 0.25).run(pol)
    with pytest.raises(ValueError, match="ic_z_method 'pinned' unknown"):
        AdaptiveDeployment(
            _ToyAlpha, toy_frames, {**adaptive_cfg, "ic_z_method": "pinned"}, bt, 0.25
        ).run(pol)


def test_adaptive_scheduled_fires_on_day_boundary(adaptive_cfg):
    # frames straddling a UTC day boundary: daily policy refits exactly once
    day = 86_400_000_000_000
    t0 = (1_700_000_000_000_000_000 // day) * day + day - 4 * 3600 * NS_S
    frames = _make_frames(n_rows=6 * 3600, t0=t0, seed=11)
    bt = Backtester(
        CostModel.load(CONFIGS_DIR / "execution" / "execution.json"), _META, BacktestConfig()
    )
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


def test_rolling_ic_z_legacy_hand_computed():
    """``rolling_ic_z`` is the LEGACY monitor (``ic_z_method="legacy"``): the
    unweighted mean bucket IC and a z that treats the baseline as exact."""
    base = ICBaseline(
        "b",
        "ZZ99",
        "",
        ic_mean=0.2,
        ic_std=0.1,
        n_buckets_baseline=30,
        bucket_ns=10 * NS_S,
        horizon="1s",
    )
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


def test_rolling_ic_z_hac_hand_computed():
    """The DEFAULT monitor (``ic_z_method="hac"``): the pair-count-weighted
    mean bucket IC and the two-sample z.

    Two buckets: 10 pairs with IC +1 and 30 pairs with IC -1.
      weighted mean  m = (10 * 1 + 30 * -1) / 40 = -0.5   (unweighted: 0)
      deviations     d = [1.5, -0.5]
      lag-0 variance sum(w^2 d^2) / sum(w^2) = (100 * 2.25 + 900 * 0.25) / 1000 = 0.45
      n_eff          (sum w)^2 / sum(w^2) = 1600 / 1000 = 1.6
      var_live       0.45 / 1.6 = 0.28125
      var_base       ic_std^2 / n_buckets_baseline = 0.01 / 30
      z              (-0.5 - 0.2) / sqrt(0.28125 + 0.01 / 30)
    """
    base = ICBaseline(
        "b",
        "ZZ99",
        "",
        ic_mean=0.2,
        ic_std=0.1,
        n_buckets_baseline=30,
        bucket_ns=100 * NS_S,
        horizon="1s",
    )
    ts = np.concatenate([np.arange(10), 100 * NS_S + np.arange(30)]).astype(np.int64)
    x = np.concatenate([np.arange(10.0), np.arange(30.0)])
    y = np.concatenate([2.0 * np.arange(10.0) + 1.0, -np.arange(30.0)])
    r = rolling_ic_z_hac(base, ts, x, y, min_buckets=2, lags=0)
    assert r.n_buckets == 2 and r.bucket_ics == pytest.approx([1.0, -1.0], abs=1e-12)
    assert r.rolling_ic == pytest.approx(-0.5, abs=1e-12)
    assert r.z == pytest.approx(-0.7 / math.sqrt(0.28125 + 0.01 / 30.0), abs=1e-9)
    # the legacy monitor on the same rows: unweighted mean 0, and a z that
    # ignores both the pair counts and the baseline's sampling error
    legacy = rolling_ic_z(base, ts, x, y, min_buckets=2)
    assert legacy.rolling_ic == pytest.approx(0.0, abs=1e-12)
    assert legacy.z == pytest.approx((0.0 - 0.2) / (0.1 / math.sqrt(2)), abs=1e-9)
    assert abs(r.z) < abs(legacy.z)
    # default lags (nw_lags of a 1 s horizon in 100 s buckets = 2): with two
    # buckets the lag-1 term is 2 * (1 - 1/3) * (1.5 * -0.5) = -1.0, the
    # long-run variance 0.45 - 1.0 is not positive, and the monitor reports
    # the rolling IC without fabricating a z
    r_default = rolling_ic_z_hac(base, ts, x, y, min_buckets=2)
    assert r_default.rolling_ic == pytest.approx(-0.5, abs=1e-12) and r_default.z is None
    # below min_buckets: silent
    r2 = rolling_ic_z_hac(base, ts, x, y, min_buckets=3)
    assert r2.rolling_ic is None and r2.z is None and r2.n_buckets == 2


# -- round-3: informative evaluations, OOS IC baseline ---------------------


def test_lifecycle_legacy_consecutive_ignores_uninformative_evaluations():
    """Six re-reads of the SAME matured window are one reading, not six
    consecutive breaches (API_ADAPTIVE section 6) — the legacy rule."""
    from iap.adaptive.lifecycle import ACTIVE, RETIRED, WATCH

    cfg = LifecycleConfig.legacy(
        watch_ic_gate=0.0, reactivate_ic_gate=0.005, retire_breach_evals=6, reactivate_evals=3
    )
    tr = LifecycleTracker(alpha_id="EQ03", config=cfg)
    # one genuine breach enters WATCH, then the feed goes quiet
    assert tr.update(1, -0.04) == WATCH
    for k in range(2, 9):
        assert tr.update(k, -0.04, informative=False) == WATCH
    assert tr.state == WATCH
    assert tr.breach_count == 1

    # the same readings WITH new evidence retire the alpha
    tr2 = LifecycleTracker(alpha_id="EQ03", config=cfg)
    states = [tr2.update(k, -0.04) for k in range(1, 8)]
    assert states[-1] == RETIRED
    assert tr2.state == RETIRED
    assert ACTIVE not in states[1:]


def test_lifecycle_cusum_ignores_uninformative_evaluations():
    """The default rule: re-reads of an unchanged matured window add nothing
    to the CUSUM statistic.  One -0.04 reading at new_fraction 1/8 enters
    WATCH with S = 0.125 * (0.04 - 0.0025) = 0.0046875 < 0.01; seven
    uninformative re-reads leave S there.  With new evidence the same
    readings reach 0.009375 and then 0.0140625 >= 0.01 on the third."""
    cfg = LifecycleConfig(
        watch_ic_gate=0.0, reactivate_ic_gate=0.005, retire_breach_evals=6, reactivate_evals=3
    )
    assert cfg.breach_rule == "cusum"
    tr = LifecycleTracker(alpha_id="EQ03", config=cfg)
    assert tr.update(1, -0.04, new_fraction=0.125) == WATCH
    for k in range(2, 9):
        assert tr.update(k, -0.04, informative=False, new_fraction=0.125) == WATCH
    assert tr.state == WATCH
    assert tr.cusum == pytest.approx(0.0046875, abs=1e-15)
    assert tr.eval_index == 8 and len(tr.transitions) == 1

    tr2 = LifecycleTracker(alpha_id="EQ03", config=cfg)
    states = [tr2.update(k, -0.04, new_fraction=0.125) for k in range(1, 4)]
    assert states == [WATCH, WATCH, RETIRED]


def test_drift_refit_requires_new_evidence():
    """drift_triggered must not fire twice on the same ic_z recomputed from
    an unchanged matured set."""
    pol = DriftTriggeredPolicy(
        psi_threshold=0.25, ic_z_threshold=-2.0, min_refit_gap_ns=3600 * NS_S
    )
    hour = 3600 * NS_S
    ctx = RefitContext(
        now_ns=10 * hour, last_fit_ns=1 * hour, psi_by_series={"signal": 0.01}, ic_z=-7.5
    )
    assert pol.should_refit(ctx).refit is True
    # the deployment passes ic_z=None for an uninformative evaluation
    stale = RefitContext(
        now_ns=11 * hour, last_fit_ns=10 * hour, psi_by_series={"signal": 0.01}, ic_z=None
    )
    assert pol.should_refit(stale).refit is False


def test_ic_baseline_must_be_out_of_sample():
    from iap.adaptive.drift import ICBaseline, capture_ic_baseline

    ts = np.arange(400, dtype=np.int64) * 10 * NS_S
    rng = np.random.default_rng(4)
    x = rng.standard_normal(400)
    y = 0.1 * x + rng.standard_normal(400)
    base = capture_ic_baseline(ts, x, y, name="t", alpha_id="EQ01", horizon="1m", source="unit")
    assert base.baseline_kind == "oos"
    assert base.to_dict()["baseline_kind"] == "oos"
    # a file claiming an in-sample baseline is rejected at load
    blob = base.to_dict()
    blob["baseline_kind"] = "is"
    with pytest.raises(ValueError, match="must be 'oos'"):
        ICBaseline.from_dict(blob)
    blob.pop("baseline_kind")
    with pytest.raises(ValueError, match="must be 'oos'"):
        ICBaseline.from_dict(blob)
    with pytest.raises(ValueError, match="out-of-sample"):
        capture_ic_baseline(ts, x, y, name="t", alpha_id="EQ01", horizon="1m", baseline_kind="is")
