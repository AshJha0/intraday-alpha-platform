"""REL-06 known-null runner (iap.research.known_null) on synthetic panels."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from iap.core.rng import SplitMix64
from iap.research import known_null as kn
from iap.research.execution_benchmark import cluster_mean_stats
from iap.research.runner import render_document

SESSIONS = ["2019-01-30", "2019-03-27", "2019-07-30", "2019-08-30"]
NAMES = ["AAPL", "MSFT", "QQQ"]
ROWS = 3000  # 100 ms rows from 09:30:00 (5 minutes) per session and name


def _ar1_grid(rng: np.random.Generator, n: int, phi: float, corr: float) -> np.ndarray:
    """(n, 3) AR(1) paths with cross-name innovation correlation ``corr``."""
    c = np.full((3, 3), corr) + np.eye(3) * (1 - corr)
    e = rng.standard_normal((n, 3)) @ np.linalg.cholesky(c).T
    y = np.empty_like(e)
    y[0] = e[0]
    for k in range(1, n):
        y[k] = phi * y[k - 1] + math.sqrt(1 - phi * phi) * e[k]
    return y


def synthetic_panel(
    *,
    effect: float = 0.0,
    missing: float = 0.05,
    seed: int = 7,
    phi: float = 0.9,
    corr: float = 0.6,
    sessions=SESSIONS,
) -> kn.Panel:
    """Labels are AR(1) noise (sd 2 bps) plus ``effect`` x sign(signal 1 s
    earlier); ``missing`` of the labels are invalid."""
    rng = np.random.default_rng(seed)
    frames = []
    per_name: dict[int, dict[str, list]] = {n: {} for n in range(3)}
    for s in sessions:
        t0 = kn.session_open_ns(s)
        noise = _ar1_grid(rng, ROWS, phi, corr) * 2e-4
        for n in range(3):
            sig = rng.uniform(-1, 1, ROWS)
            # the effect pays at the entry row, one latency (10 rows) after the signal
            lab = noise[:, n] + effect * np.sign(np.roll(sig, 10))
            valid = rng.uniform(size=ROWS) > missing
            cols = {
                "exchange_ts": t0 + np.arange(ROWS, dtype=np.int64) * kn.CELL_NS,
                "sig_v1": sig,
                "mid_price_v1": np.full(ROWS, 100.0),
                "half_spread_cost_bps_v1": np.full(ROWS, 0.5),
                "depth_bid_l1_v1": np.full(ROWS, 300.0),
                "depth_ask_l1_v1": np.where(np.arange(ROWS) % 10 == 0, 40.0, 300.0),
                "label_mid_1s": lab,
                "label_cost_1s": lab - 1e-4,
                "label_valid_1s": valid,
            }
            for k, v in cols.items():
                per_name[n].setdefault(k, []).append(v)
    for n in range(3):
        frames.append((NAMES[n], n + 1, {k: np.concatenate(v) for k, v in per_name[n].items()}))
    return kn.make_panel(frames)


CONF = {
    "n_folds": 4,
    "embargo_ns": 60_000_000_000,
    "cost_multiplier": 1.0,
    "latency_ns": 1_000_000_000,
    "max_decision_age_ns": 60_000_000_000,
    "flatten_at_session_end": True,
    "methods": "v2",
}


def config(n_trials: int = 3, **over) -> dict:
    def hyp(i: int, syms: list[str]) -> dict:
        return {
            "id": f"h{i}",
            "signal": "sig_v1",
            "horizon": "1s",
            "symbols": syms,
            "sign": 1,
            "threshold": 0.5,
        }

    doc = {
        "alpha": 0.05,
        "n_trials": n_trials,
        "registered_size_shares": 100,
        "configuration": dict(CONF),
        "execution": {"impact_model": "none (synthetic test)"},
        "generators": {"A": {"base_seed": 20261018}, "B": {"base_seed": 20261019}},
        "pass_rule": {"max_point": 0.05, "max_upper": 0.07, "flag_upper_below": 0.02},
        "variants": {
            "K1": [hyp(0, ["AAPL"])],
            "K3": [hyp(1, ["AAPL"]), hyp(2, ["MSFT"]), hyp(3, NAMES)],
        },
    }
    doc.update(over)
    return doc


@pytest.fixture(scope="module")
def panel():
    return synthetic_panel()


# ------------------------------------------------------------------ RNG --
def test_vector_splitmix_matches_the_pinned_scalar_stream():
    a, b = SplitMix64(123), SplitMix64(123)
    u = kn.splitmix_uniforms(a, 1000)
    assert u.tolist() == [b.uniform() for _ in range(1000)]
    assert a.state == b.state
    z = kn.splitmix_normals(a, 500)
    assert np.allclose(z, [b.normal() for _ in range(500)], rtol=0, atol=1e-12)
    assert a.state == b.state


def test_derangements_drawn_without_replacement_and_reproducibly():
    assert len(kn.derangements(7)) == 1854
    p1 = kn.draw_permutations(4, 9, 5)
    assert p1 == kn.draw_permutations(4, 9, 5)
    perms = [tuple(p["permutation"]) for p in p1]
    assert len(set(perms)) == 9 == len(kn.derangements(4))
    assert all(all(p[i] != i for i in range(4)) for p in perms)
    assert [p["seed"] for p in p1] == list(range(5, 14))
    with pytest.raises(ValueError):
        kn.draw_permutations(4, 10, 5)


# ----------------------------------------------------------- generator A --
def test_generator_a_moves_whole_sessions_keeps_names_aligned_and_missing(panel):
    real = kn.labels_real(panel, "1s")
    perm = [1, 2, 3, 0]
    out = kn.labels_a(panel, real, perm)
    for n in range(3):
        sl = panel.name_slice(n)
        for s in range(4):
            mine = out[sl][panel.sess[sl] == s]
            src = real[sl][panel.sess[sl] == perm[s]]
            # the same cell grid, so the partner session's series arrives intact:
            # serial dependence preserved exactly, missing stays missing
            assert np.array_equal(np.isnan(mine), np.isnan(src))
            assert np.array_equal(mine[~np.isnan(mine)], src[~np.isnan(src)])

    # one permutation for all names: the cross-name correlation is unchanged
    def corr(x):
        m = np.stack([x[panel.name_slice(n)] for n in range(3)])
        ok = np.all(np.isfinite(m), axis=0)
        return np.corrcoef(m[:, ok])

    assert np.allclose(corr(out), corr(real), atol=1e-12)


def test_generator_a_partner_cell_missing_leaves_row_missing(panel):
    real = kn.labels_real(panel, "1s")
    sl = panel.name_slice(0)
    rows = np.flatnonzero(panel.sess[sl] == 1)
    real_holed = real.copy()
    real_holed[sl.start + rows[:50]] = np.nan  # partner of session 0 under perm [1,0,3,2]
    out = kn.labels_a(panel, real_holed, [1, 0, 3, 2])
    mine = np.flatnonzero(panel.sess[sl] == 0)[:50]
    assert np.all(np.isnan(out[sl.start + mine]))


# ----------------------------------------------------------- generator B --
def test_generator_b_preserves_ar1_cross_name_correlation_and_mask():
    p = synthetic_panel(phi=0.9, corr=0.6, missing=0.1)
    real = kn.labels_real(p, "1s")
    params = kn.ar1_params(p, real)
    assert all(abs(f - 0.9) < 0.03 for f in params["phi"])
    out = kn.labels_b(p, real, params, 99)
    assert np.array_equal(np.isnan(out), np.isnan(real))  # mask copied exactly
    est = kn.ar1_params(p, out)
    for a, b in zip(est["phi"], params["phi"], strict=True):
        assert abs(a - b) < 0.03
    for a, b in zip(est["sd"], params["sd"], strict=True):
        assert abs(a / b - 1) < 0.15
    assert np.allclose(est["corr"], params["corr"], atol=0.06)
    assert abs(np.nanmean(out)) < 3 * params["sd"][0] / math.sqrt(2000)
    # reproducible from its seed
    assert np.array_equal(out, kn.labels_b(p, real, params, 99), equal_nan=True)


# ------------------------------------------------------------- inference --
def _brute_p(x: np.ndarray) -> float:
    g = x.size

    def t_of(v):
        m = v.mean()
        se = math.sqrt(g / (g - 1) * ((v - m) ** 2).sum() / g**2)
        return m / se if se > 0 else -math.inf

    t0 = t_of(x)
    hits = sum(t_of(np.array(w) * x) >= t0 - 1e-12 for w in itertools.product(kn.WEBB, repeat=g))
    return hits / 6**g


@pytest.mark.parametrize("x", [[0.5, -0.2, 1.1], [2.0, 1.0, 0.3, -0.4], [-1.0, -0.5, 0.2]])
def test_webb_full_enumeration_matches_brute_force(x):
    x = np.asarray(x)
    inf = kn.cluster_inference(x)
    assert inf["p_method"] == f"wild_webb_full_enumeration_6^{x.size}"
    assert inf["p"] == pytest.approx(_brute_p(x), abs=0)
    assert kn.webb_weights(x.size)[0].shape == (6**x.size, x.size)


def test_cr1_equals_cluster_mean_stats_at_one_row_per_cluster():
    x = [0.3, -1.2, 2.5, 0.7, 0.1]
    ref = cluster_mean_stats(x, [str(i) for i in range(5)], n_boot=10)
    inf = kn.cluster_inference(x)
    assert inf["se"] == pytest.approx(ref["se"], rel=1e-12)
    assert inf["t"] == pytest.approx(ref["t"], rel=1e-12)


def test_student_t_at_ten_or_more_clusters():
    inf = kn.cluster_inference(np.linspace(-1, 2, 12))
    assert inf["p_method"] == "student_t" and inf["p_wild"] is None


# ------------------------------------------------------ economics / trips --
def test_trips_do_not_overlap_and_record_partial_fills(panel):
    h = kn.Hypothesis("h", "sig_v1", "1s", ("AAPL",), 1, 0.5)
    t = kn.build_trips(
        panel,
        h,
        size=100,
        latency_ns=10**9,
        max_decision_age_ns=60 * 10**9,
        cost_multiplier=1.0,
        pricer=None,
    )
    assert t.n > 100
    ts = panel.ts[t.entry_row]
    for s in range(4):
        e = np.sort(ts[t.sess == s])
        assert np.all(np.diff(e) >= 10**9)  # next decision only after the exit
    assert set(np.round(t.fill_frac, 2)) <= {0.4, 1.0} and (t.fill_frac < 1).any()
    # longs (buy at the thin ask, sell into 300 on the bid): spread both ends = 1 bp
    # at full fill; an entry residual pays the completion half spread (0.5 bp)
    long, full = t.side > 0, t.fill_frac == 1.0
    assert np.allclose(t.fixed_bps[long & full], 1.0)
    assert np.allclose(t.fixed_bps[long & ~full], 0.4 * 1.0 + 0.6 * 0.5)
    # shorts fill fully on the bid; an exit row with a thin ask leaves 60 shares to complete
    short = ~long
    assert np.all(t.fill_frac[short] == 1.0)
    assert set(np.round(t.fixed_bps[short], 6)) == {1.0, 1.3}


def test_simulator_pricer_adds_fees_and_impact(panel):
    from iap.execution.config import ExecConfig
    from iap.execution.types import InstrumentSpec, VenueSpec

    cfg = ExecConfig(
        venues={1: VenueSpec(venue_id=1, taker_fee_per_share=0.003)},
        instruments={
            i: InstrumentSpec(instrument_id=i, tick_size=0.01, adv=1e6) for i in (1, 2, 3)
        },
    )
    pr = kn.SimulatorPricer(cfg, 1)
    fee, imp = pr.leg(1, 0, 100, 100.0, 0)
    assert fee == pytest.approx(0.3) and imp > 0
    h = kn.Hypothesis("h", "sig_v1", "1s", ("AAPL",), 1, 0.5)
    kw = dict(size=100, latency_ns=10**9, max_decision_age_ns=60 * 10**9, cost_multiplier=1.0)
    a = kn.build_trips(panel, h, pricer=None, **kw)
    b = kn.build_trips(panel, h, pricer=kn.SimulatorPricer(cfg, 1), **kw)
    assert np.array_equal(a.entry_row, b.entry_row)
    assert np.all(b.fixed_bps > a.fixed_bps)  # 2 x 0.3 bp fees + impact on top


# --------------------------------------------------------------- the study --
def test_planted_effect_is_confirmed_and_noise_is_not():
    p = synthetic_panel(effect=1.5e-4)  # 1.5 bp per trip against 1 bp of spread
    st = kn.prepare(p, config(), pricer=None)
    rec = kn.run_trial(st.trips, st.real, st.variants["K3"], 4, alpha=0.05, eligible=True)
    assert set(rec["confirmed"]) == {"h1", "h2", "h3"}
    assert all(m > 0 for m in rec["mean_bps"])
    # the same rows with the label relationship destroyed (generator A) confirm nothing
    res = kn.run_trials(
        st,
        "A",
        [0, 1, 2],
        {i: q["permutation"] for i, q in enumerate(kn.draw_permutations(4, 3, 1))},
    )
    assert all(not r["K3"]["confirmed"] for r in res.values())


def test_ineligible_configuration_never_confirms():
    p = synthetic_panel(effect=1.5e-4)
    conf = dict(CONF, methods="v4")
    st = kn.prepare(p, config(configuration=conf), pricer=None)
    assert st.eligible["gate_eligible"] is False
    rec = kn.run_trial(
        st.trips, st.real, st.variants["K3"], 4, alpha=0.05, eligible=st.eligible["gate_eligible"]
    )
    assert rec["confirmed"] == [] and min(rec["p_holm"]) <= 0.05


def test_holm_counts_a_missing_p_as_one():
    p = synthetic_panel(effect=1.5e-4)
    st = kn.prepare(p, config(), pricer=None)
    fam = st.variants["K3"] + [kn.Hypothesis("none", "sig_v1", "1s", (), 1, 0.5)]
    st.trips["none"] = kn.TripTable(
        *(np.empty(0, dtype=t) for t in (np.int64, np.int64, float, float, float))
    )
    rec = kn.run_trial(st.trips, st.real, fam, 4, alpha=0.05, eligible=True)
    assert rec["p"][-1] == 1.0 and rec["p_holm"][-1] == 1.0
    assert rec["p_holm"][0] >= 4 * min(rec["p"][:3]) - 1e-12 or rec["p_holm"][0] == 1.0


def test_document_is_byte_reproducible_and_chunking_invariant(panel):
    cfg = config(n_trials=3)
    st = kn.prepare(panel, cfg, pricer=None)
    perms = kn.draw_permutations(4, 3, cfg["generators"]["A"]["base_seed"])
    pm = {q["trial"]: q["permutation"] for q in perms}

    def doc(chunks):
        res = {g: {} for g in ("A", "B")}
        for g in res:
            for c in chunks:
                res[g].update(kn.run_trials(st, g, c, pm if g == "A" else {}))
        return render_document(kn.study_document(st, res, perms, provenance={"commit": "x"}))

    one, two = doc([[0, 1, 2]]), doc([[2], [0, 1]])
    assert one == two
    assert one == render_document(
        kn.study_document(
            kn.prepare(panel, cfg, pricer=None),
            {g: kn.run_trials(st, g, [0, 1, 2], pm if g == "A" else {}) for g in ("A", "B")},
            perms,
            provenance={"commit": "x"},
        )
    )
    import json

    d = json.loads(one)
    assert d["complete"] is True and d["verdict"] in ("PASS", "FAIL")
    assert "G = 4 session clusters" in d["small_g_statement"]
    assert d["generators"]["A"]["permutations"] == perms
    assert d["generators"]["B"]["seeds"] == [20261019, 20261020, 20261021]
    s = d["generators"]["A"]["variants"]["K3"]["summary"]
    assert s["family"]["n"] == 3 and set(s["per_hypothesis"]) == {"h1", "h2", "h3"}


def test_pass_rule_and_wilson():
    rule = dict(max_point=0.05, max_upper=0.07, flag_upper_below=0.02)
    assert kn.evaluate(50, 1000, **rule)["pass"] is True  # 5.0 %, upper ~6.5 %
    assert kn.evaluate(51, 1000, **rule)["pass"] is False  # point above 5 %
    assert kn.evaluate(3, 40, **rule)["pass"] is False  # point ok-ish, upper too wide
    r = kn.evaluate(5, 1000, **rule)
    assert r["pass"] and r["over_conservative"] and r["wilson95"][1] < 0.02


def test_identity_args_bind_the_runner_script_and_config():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = json.loads((root / "research/v118/known_null_config.json").read_text(encoding="utf-8"))
    a = kn.identity_args(cfg)
    assert set(a["scripts"]) == set(kn.BOUND_SCRIPTS)
    assert all((root / s).is_file() for s in a["scripts"])
    assert a["data_versions"] == [
        "ee4c218738683e5bc4249aa4a9e7302f32261c5855e97b3d337862b334333fb4"
    ]
    assert {len(v) for v in cfg["variants"].values()} == {3, 6, 16}
    assert a["thresholds"]["base_seeds"] == {"A": 20261018, "B": 20261019}
    assert a["thresholds"]["pass_rule"] == {
        "max_point": 0.05,
        "max_upper": 0.07,
        "flag_upper_below": 0.02,
    }
    assert kn.eligibility(cfg["configuration"])["gate_eligible"] is True
