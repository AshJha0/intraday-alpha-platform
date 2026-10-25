"""Confirmation profiles PF-1 / PF-2 (U13): reference cases, the block-length rule, the
refusal fixtures (a)-(z) of ``CONFIRMATION_PROFILES.md`` section 7, the single-read and
identity rules through ``iap.core.guard``, a planted effect that promotes, and the fast
smoke subset of the synthetic study (n = 200).  The synthetic evidence itself is
``test_profiles_evidence.py`` (slow).

Reference values: the Newey-West long-run variance against an explicit textbook loop and,
when importable, ``statsmodels`` (D-40: not in the CI lock); the circular block bootstrap
p-value against an explicit resample-by-resample reference on a fixed seed.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from iap.core import guard
from iap.validation import confirmatory as c
from iap.validation import profiles as pf

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "python" / "tests" / "spec"
sys.path.insert(0, str(SPEC))
import pf_generators as gen  # noqa: E402
import pf_suite as suite  # noqa: E402


def _normals(seed: int, *shape: int) -> np.ndarray:
    return gen.Stream(seed).normals(*shape)


# ----------------------------------------------------------- reference cases --
def _textbook_lrv(x: np.ndarray, lag: int) -> float:
    n = x.size
    d = x - x.mean()
    gamma = [sum(d[t] * d[t - h] for t in range(h, n)) / n for h in range(lag + 1)]
    return gamma[0] + 2.0 * sum((1.0 - h / (lag + 1.0)) * gamma[h] for h in range(1, lag + 1))


@pytest.mark.parametrize("lag", [0, 2, 5, 20])
def test_newey_west_lrv_against_textbook_loop(lag):
    x = _normals(11, 250)
    x = np.cumsum(x) * 0.1 + x  # some dependence
    ref = _textbook_lrv(x, lag)
    assert pf.newey_west_lrv(x, lag, correction=False) == pytest.approx(ref, rel=1e-10)
    assert pf.newey_west_lrv(x, lag) == pytest.approx(ref * 250 / (250 - lag - 1), rel=1e-10)


def test_newey_west_lrv_against_statsmodels():
    sm = pytest.importorskip("statsmodels.api", reason="D-40: statsmodels is not in the CI lock")
    x = _normals(12, 300)
    x = 0.4 * np.roll(x, 1) + x
    for lag in (2, 5, 20):
        fit = sm.OLS(x, np.ones(x.size)).fit(
            cov_type="HAC", cov_kwds={"maxlags": lag, "use_correction": False}
        )
        ref = float(fit.bse[0]) ** 2 * x.size
        assert pf.newey_west_lrv(x, lag, correction=False) == pytest.approx(ref, rel=1e-10)
        t_ref = float(fit.tvalues[0])
        mine = x.mean() / math.sqrt(pf.newey_west_lrv(x, lag, correction=False) / x.size)
        assert mine == pytest.approx(t_ref, rel=1e-10)


def test_hac_pvalue_matches_vectorised_and_reference_df():
    x = _normals(13, 60, 250, 4)
    p = pf.hac_pvalues_many(x, 5)
    for i in (0, 7, 59):
        for j in range(4):
            st = pf.hac_pvalue(x[i, :, j], 5)
            assert st["p"] == pytest.approx(p[i, j], abs=1e-12) and st["df"] == 75
    assert pf.hac_reference_df(120, 5) == 36
    assert pf.hac_reference_df(250, 20) == 18
    assert pf.hac_reference_df(500, 60) == 12
    assert pf.hac_reference_df(250, 0) == 249
    assert pf.hac_reference_df(30, 5) == 9
    assert pf.hac_reference_df(10, 60) == 1


def _reference_cbb(x: np.ndarray, L: int, B: int, seed: int) -> float:
    T = x.size
    nb = -(-T // L)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, T, size=(1, 1, B * nb)).reshape(B, nb)
    m = x.mean()
    scale = math.sqrt(T / (T - L))
    count = 0
    for b in range(B):
        vals = np.concatenate([x[(s + np.arange(L)) % T] for s in starts[b]])
        assert vals.size == nb * L
        if (vals.mean() - m) * scale >= m:
            count += 1
    return (1.0 + count) / (B + 1.0)


@pytest.mark.parametrize("L,T", [(1, 40), (5, 120), (7, 250), (20, 250)])
def test_cbb_pvalue_against_explicit_resample_reference(L, T):
    x = _normals(21 + L, T) + 0.15
    st = pf.cbb_pvalue(x, L, resamples=199, seed=77)
    assert st["p"] == pytest.approx(_reference_cbb(x, L, 199, 77), abs=1e-12)
    assert st["block_length"] == L and st["T"] == T


def test_cbb_vectorised_equals_per_series_on_the_same_draws():
    x = _normals(31, 3, 120, 2)
    rng = np.random.default_rng(5)
    many = pf.cbb_pvalues_many(x, 5, 99, rng)
    # the same draws, series by series, in dataset-then-hypothesis order
    rng2 = np.random.default_rng(5)
    for i in range(3):
        for j in range(2):
            one = pf.cbb_pvalues_many(x[i : i + 1, :, j : j + 1], 5, 99, rng2)
            assert one[0, 0] == pytest.approx(many[i, j], abs=1e-12)


def test_circular_block_means_and_zero_variance():
    x = np.arange(10, dtype=float)[None, :, None]
    cb = pf._circular_block_means(x, 3)[0, :, 0]
    assert cb[0] == pytest.approx(1.0) and cb[9] == pytest.approx((9 + 0 + 1) / 3)
    flat = np.ones((2, 30, 1))
    assert np.isnan(pf.cbb_pvalues_many(flat, 5, 9, np.random.default_rng(0))).all()
    assert np.isnan(pf.hac_pvalues_many(flat, 5)).all()


def test_holm_many_matches_scalar_holm_row_by_row():
    p = np.random.default_rng(3).uniform(size=(200, 6)) ** 3
    p[5, 2] = np.nan
    p[6, :] = 0.0
    rej = pf.holm_many(p, 0.02)
    for i in range(200):
        assert rej[i].tolist() == c.holm(p[i], 0.02)["rejected"].tolist()


def test_block_length_rule_on_development_series():
    # iid: no residual dependence beyond lag 0 at this seed -> the floor
    iid = _normals(41, 500)
    r = pf.block_length_rule(iid, max_lag=60, floor=10, cap=60)
    assert r["L_dep"] == 0 and r["block_length"] == 10 and len(r["acf"]) == 60 and r["T_dev"] == 500
    # MA(19) (h = 20): a long dependence range -> the cap (the sample autocorrelations
    # beyond the MA order carry Bartlett-inflated variance, so the white-noise tail test
    # keeps rejecting and L_dep reaches max_lag; conservative for the bootstrap, D-68)
    ma = gen.draw(gen.Stream(42), "overlap_ma", {"h": 20}, 1, 500, 1)[0, :, 0]
    r = pf.block_length_rule(ma, max_lag=60, floor=10, cap=60)
    assert r["L_dep"] >= 19 and r["block_length"] == 60
    # AR(1) 0.5: a short range -> a short block
    ar = gen.draw(gen.Stream(43), "ar1", {"rho": 0.5}, 1, 250, 1)[0, :, 0]
    r = pf.block_length_rule(ar, max_lag=20, floor=5, cap=20)
    assert 1 <= r["L_dep"] <= 6 and 5 <= r["block_length"] <= 12
    # the record recomputes to itself
    assert pf._block_from_acf(r, floor=5, cap=20, multiplier=2)["block_length"] == r["block_length"]
    with pytest.raises(ValueError):
        pf.block_length_rule(iid[:100], max_lag=60, floor=10, cap=60)
    with pytest.raises(ValueError):
        pf.block_length_rule(np.ones(200), max_lag=20, floor=5, cap=20)


def test_planted_effect_is_the_one_noncentral_t_inversion():
    d = pf.planted_effect(250, 4)
    assert d == pytest.approx(c.mde(250, 0.005, 0.9, procedure="P-IND"))
    assert 0.15 < d < 0.30
    assert pf.planted_effect(120, 4) > d


def test_generator_theoretical_acf1_and_garch_constants():
    assert pf.generator_acf1("overlap_ma", {"h": 5}) == pytest.approx(0.8)
    assert pf.generator_acf1("ar1", {"rho": 0.3}) == 0.3
    assert pf.generator_acf1("garch11", {}) == 0.0 and pf.generator_acf1("t3", {}) == 0.0
    a, b = pf.GARCH_ALPHA, pf.GARCH_BETA
    kurt = 3 * (1 - (a + b) ** 2) / (1 - (a + b) ** 2 - 2 * a * a) - 3
    assert pf.GARCH_EXCESS_KURTOSIS == pytest.approx(kurt, abs=5e-4)
    sq = a * (1 - a * b - b * b) / (1 - 2 * a * b - b * b)
    assert pf.GARCH_SQ_ACF1 == pytest.approx(sq, abs=5e-4)
    with pytest.raises(ValueError):
        pf.generator_acf1("nope", {})


def test_generators_have_unit_variance_and_the_declared_dependence():
    s = gen.Stream(99)
    for g, params, acf1 in (
        ("iid_normal", {}, 0.0),
        ("ar1", {"rho": 0.5}, 0.5),
        ("garch11", {"alpha": 0.08, "beta": 0.9}, 0.0),
        ("t3", {}, 0.0),
        ("overlap_ma", {"h": 5}, 0.8),
    ):
        x = gen.draw(s, g, params, 400, 250, 1)[:, :, 0]
        assert x.var() == pytest.approx(1.0, abs=0.08), g
        assert abs(x.mean()) < 0.02, g
        est = np.mean([pf.acf(x[i], 1) for i in range(400)])
        assert est == pytest.approx(acf1, abs=0.05), g
    with pytest.raises(ValueError):
        gen.draw(s, "nope", {}, 1, 10)


def test_cell_table_and_seeds():
    for pid in ("PF-1", "PF-2"):
        prof = pf.PROFILES[pid]
        cells = gen.cells_for(prof, planted=lambda T: pf.planted_effect(T, gen.K))
        assert len(cells) == 40 and sum(c_.kind == "size" for c_ in cells) == 20
        seeds = [c_.seed for c_ in cells]
        assert len(set(seeds)) == 40 and all(20330000 <= s < 20350000 for s in seeds)
        assert all(c_.dev_seed == c_.seed + 5000 for c_ in cells)
        assert all(c_.n == 20000 for c_ in cells if c_.kind == "size")
        assert all(c_.n == 5000 and c_.delta > 0 for c_ in cells if c_.kind == "power")
    pf1 = gen.cells_for(pf.PROFILES["PF-1"], planted=lambda T: 0.1)
    assert pf1[0].seed == 20330000 and pf1[20].seed == 20330100
    pf2 = gen.cells_for(pf.PROFILES["PF-2"], planted=lambda T: 0.1)
    assert pf2[0].seed == 20340000 and pf2[-1].seed == 20340119
    # no overlap with R0's streams
    assert not any(20301000 <= s < 20301500 for s in [c_.seed for c_ in pf1 + pf2])


# --------------------------------------------------------------- the smoke --
@pytest.mark.parametrize("pid,idx", [("PF-1", (0, 16)), ("PF-2", (0, 19))])
def test_smoke_subset_of_the_synthetic_study(pid, idx):
    prof = pf.PROFILES[pid]
    cells = [
        c_
        for c_ in gen.cells_for(prof, planted=lambda T: pf.planted_effect(T, gen.K))
        if c_.kind == "size" and c_.index in idx
    ]
    cells.append(
        [
            c_
            for c_ in gen.cells_for(prof, planted=lambda T: pf.planted_effect(T, gen.K))
            if c_.kind == "power"
        ][0]
    )
    for cell in cells:
        r = suite.run_cell(cell, prof, n=200)
        assert r["n"] == 200 and set(r["procedures"]) == set(prof.procedures)
        for name, v in r["procedures"].items():
            assert v["result"] == "smoke" and 0 <= v["k"] <= 200 and v["nan_p"] == 0
            assert 0.0 <= v["lower"] <= v["estimate"] <= v["upper"] <= 1.0
            if name == "P-CBB":
                assert v["block_length"] >= prof.procedures[name]["floor"]
        if cell.kind == "power" and cell.generator == "iid_normal":
            assert max(v["k"] for v in r["procedures"].values()) >= 140  # power ~0.9 at n = 200


# ------------------------------------------------------------- the fixtures --
def _evidence(
    root: Path,
    pid: str = "PF-1",
    *,
    passing: tuple[str, ...] = ("iid_normal", "ar1_0.1", "garch11"),
) -> None:
    """A fabricated evidence file for the fixtures: the named generator cells pass at every T."""
    prof = pf.PROFILES[pid]
    cells = []
    for cell in gen.cells_for(prof, planted=lambda T: 0.1):
        key = cell.generator + (f"_{cell.params['rho']}" if cell.generator == "ar1" else "")
        procs = {}
        for name in prof.procedures:
            ok = key in passing and name != "P-CBB"
            k = 400 if ok else 900
            procs[name] = {
                "k": k,
                "result": ("pass" if ok else "fail") if cell.kind == "size" else "reported",
            }
        cells.append(
            {
                "kind": cell.kind,
                "generator": cell.generator,
                "params": cell.params,
                "T": cell.T,
                "procedures": procs,
            }
        )
    doc = {"profile": pid, "size_pass_k": 463, "n_size": 20000, "cells": cells}
    path = pf.evidence_path(pid, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _reg(**over) -> dict:
    d = {
        "profile": "PF-1",
        "statistic": pf.STATISTIC,
        "horizon_sessions": 3,
        "family": ["D1_h1", "D1_h3", "D1_h5"],
        "procedure": "P-HAC-L5",
        "alpha_op": 0.020,
        "hac_lag": 5,
        "sample_size": {"T_min": 120, "cap": 1.25},
        "refusal_conditions": ["acf1", "sq_acf1", "excess_kurtosis"],
        "economic_screen": {"capital_usd": 1_000_000, "hurdle_bps": 0.0, "margin": 0.0},
        "registry": {"window_state": "fresh", "confirmation_dataset_looks": 0},
        "data": {"origin": "synthetic", "partition": "none"},
    }
    d.update(over)
    return d


def _cbb_reg(seed: int = 7, **over) -> dict:
    dev = _normals(seed, 250)
    diag = pf.block_length_rule(dev, max_lag=20, floor=5, cap=20)
    diag = {k: v for k, v in diag.items() if k != "ljung_box_tail"}
    diag.update({"source": "development", "development_sha256": "c" * 64})
    bl = {"block_length": diag["block_length"], "bootstrap_seed": 11, "diagnostic": diag}
    d = _reg(procedure="P-CBB", block_length=bl)
    d.pop("hac_lag")
    d.update(over)
    return d


@pytest.fixture
def root(tmp_path):
    _evidence(tmp_path)
    return tmp_path


@pytest.fixture
def no_hook():
    old = guard.registered()
    guard.reset()
    yield
    guard.register(old)


def _series(seed: int = 5, T: int = 150, K: int = 3, effect: float = 0.0) -> np.ndarray:
    x = _normals(seed, T, K)
    x[:, 0] += effect
    return x


def test_operating_domain_from_fabricated_evidence(root):
    dom = pf.operating_domain("PF-1", root)
    assert dom["P-HAC-L5"]["reference_passes"] and not dom["P-CBB"]["reference_passes"]
    gens = dom["P-HAC-L5"]["generators"]
    assert (
        ("iid_normal", {}) in gens
        and ("ar1", {"rho": 0.1}) in gens
        and ("garch11", {"alpha": 0.08, "beta": 0.9}) in gens
    )
    assert ("t3", {}) not in gens and dom["P-HAC-L5"]["acf1_max"] == pytest.approx(0.1)
    assert dom["P-HAC-L5"]["garch_in_domain"] and not dom["P-HAC-L5"]["t3_in_domain"]
    assert pf.operating_domain("PF-2", root) is None  # no file
    assert pf.operating_domain("PF-3", root) is None


def test_planted_effect_promotes_with_the_inference_hash(root):
    v = pf.decide("PF-1", _series(effect=0.6), _reg(), root=root)
    assert v.is_promote and v.promoted == ("D1_h1",) and v.token.sha256 == v.inference_sha256
    assert (
        v.statistics[0]["rejected"]
        and v.statistics[0]["p_holm"] <= 0.02
        and v.statistics[0]["df"] == 45
    )
    assert v.economic[0]["passes"] and v.bounds["acf1"] == pytest.approx(0.1 + 2 / math.sqrt(150))
    doc = v.to_json()
    assert (
        doc["gate"] == "confirmation_profile"
        and "token" not in doc
        and doc["promoted"] == ["D1_h1"]
    )
    null = pf.decide("PF-1", _series(seed=6), _reg(), root=root)
    assert null.outcome == "REJECT" and null.token is None and not null.is_promote


def test_economic_screen_blocks_a_significant_but_uneconomic_hypothesis(root):
    reg = _reg(economic_screen={"capital_usd": 1e6, "hurdle_bps": 1.0, "margin": 0.25})
    v = pf.decide("PF-1", _series(effect=0.6), reg, root=root)
    assert v.outcome == "REJECT" and v.statistics[0]["rejected"] and not v.economic[0]["passes"]
    assert v.economic[0]["hurdle_bps_with_margin"] == pytest.approx(1.25)


def test_cbb_registration_decides_and_the_rule_is_recomputed(root):
    reg = _cbb_reg()
    v = pf.decide("PF-1", _series(effect=0.6), reg, root=root)
    # P-CBB has no domain in the fabricated evidence: refused, not decided
    assert v.outcome == "refused: procedure outside validated domain"
    # the fabricated file never passes P-CBB; make one that does (iid normal and GARCH)
    doc = json.loads(pf.evidence_path("PF-1", root).read_text())
    for cell in doc["cells"]:
        if cell["generator"] in ("iid_normal", "garch11") and cell["kind"] == "size":
            cell["procedures"]["P-CBB"] = {"k": 400, "result": "pass"}
    pf.evidence_path("PF-1", root).write_text(json.dumps(doc))
    v = pf.decide("PF-1", _series(effect=0.6), reg, root=root)
    assert v.is_promote and v.statistics[0]["block_length"] == reg["block_length"]["block_length"]
    # a block length that is not the rule's value for the bound diagnostic
    bad = _cbb_reg()
    bad["block_length"]["block_length"] += 1
    assert pf.decide("PF-1", _series(), bad, root=root).outcome == (
        "refused: block length is not the declared rule applied to the development diagnostic"
    )
    # a diagnostic not from development data
    bad = _cbb_reg()
    bad["block_length"]["diagnostic"]["source"] = "confirmation"
    assert (
        pf.decide("PF-1", _series(), bad, root=root).outcome
        == "refused: block-length diagnostic not bound to development data"
    )
    bad = _cbb_reg()
    del bad["block_length"]["diagnostic"]
    assert (
        pf.decide("PF-1", _series(), bad, root=root).outcome
        == "refused: block length without its development diagnostic"
    )
    bad = _cbb_reg()
    del bad["block_length"]["bootstrap_seed"]
    assert (
        pf.decide("PF-1", _series(), bad, root=root).outcome
        == "refused: bootstrap seed not registered"
    )


@pytest.mark.parametrize(
    "over,outcome",
    [
        ({"profile": "PF-2"}, "refused: registration names another profile"),
        ({"statistic": "session_value"}, "refused: unregistered statistic"),
        ({"horizon_sessions": 20}, "refused: horizon outside the profile"),
        ({"horizon_sessions": "3"}, "refused: horizon outside the profile"),
        ({"family": []}, "refused: empty or duplicated family"),
        ({"family": ["a", "a"]}, "refused: empty or duplicated family"),
        ({"procedure": "P-IND"}, "refused: procedure not a candidate of the profile"),
        ({"procedure": "P-BLK"}, "refused: procedure not a candidate of the profile"),
        ({"procedure": "P-CBB"}, "refused: procedure outside validated domain"),
        ({"alpha_op": 0.05}, "refused: alpha_op differs from the framework"),
        ({"hac_lag": 10}, "refused: HAC lag is not the profile's pre-fixed lag"),
        (
            {"sample_size": {"T_min": 60, "cap": 1.25}},
            "refused: sample-size rule missing or below the validated T",
        ),
        (
            {"sample_size": {"T_min": 120, "cap": 2.0}},
            "refused: sample-size cap is not the framework's 1.25",
        ),
        (
            {"refusal_conditions": ["acf1"]},
            "refused: refusal conditions do not name the profile's diagnostics",
        ),
        (
            {"economic_screen": None},
            "refused: economic screen missing (capital, hurdle, common margin)",
        ),
        (
            {"economic_screen": {"capital_usd": 0, "hurdle_bps": 0, "margin": 0}},
            "refused: economic screen missing (capital, hurdle, common margin)",
        ),
        (
            {"registry": {"window_state": "spent", "confirmation_dataset_looks": 0}},
            "refused: window spent or looks on the confirmation dataset",
        ),
        (
            {"registry": {"window_state": "fresh", "confirmation_dataset_looks": 3}},
            "refused: window spent or looks on the confirmation dataset",
        ),
        ({"data": {"origin": "sealed"}}, "refused: data origin not declared"),
    ],
)
def test_registration_fixtures_refuse_whatever_the_data(root, over, outcome):
    v = pf.decide("PF-1", _series(effect=5.0), _reg(**over), root=root)
    assert v.outcome == outcome and v.token is None and v.statistics == ()


def test_profile_fixtures(root):
    assert pf.decide("PF-9", _series(), _reg(), root=root).outcome == "refused: unknown profile"
    assert (
        pf.decide("PF-3", _series(), _reg(profile="PF-3"), root=root).outcome
        == "refused: profile not validated (PF-3 deferred)"
    )
    assert (
        pf.decide("PF-2", _series(), _reg(profile="PF-2"), root=root).outcome
        == "refused: profile not validated (no committed evidence)"
    )
    assert (
        pf.decide("PF-1", _series(), None, root=root).outcome
        == "refused: registration record missing"
    )
    # a tampered evidence file (wrong thresholds) is no evidence
    path = pf.evidence_path("PF-1", root)
    doc = json.loads(path.read_text())
    doc["size_pass_k"] = 9999
    path.write_text(json.dumps(doc))
    assert (
        pf.decide("PF-1", _series(effect=5.0), _reg(), root=root).outcome
        == "refused: profile not validated (no committed evidence)"
    )


def test_series_fixtures(root):
    reg = _reg()
    assert (
        pf.decide("PF-1", _series(K=2), reg, root=root).outcome
        == "refused: series shape differs from the family"
    )
    assert (
        pf.decide("PF-1", _series(T=100), reg, root=root).outcome
        == "refused: fewer sessions than registered"
    )
    x = _series()
    x[3, 1] = np.nan
    assert pf.decide("PF-1", x, reg, root=root).outcome == "refused: non-finite values"
    x = _series()
    x[:, 2] = 1.0
    assert pf.decide("PF-1", x, reg, root=root).outcome == "refused: zero variance"
    # (x) an AR(1) 0.8 series: lag-1 autocorrelation above the bound
    ar = gen.draw(gen.Stream(8), "ar1", {"rho": 0.8}, 1, 150, 3)[0]
    v = pf.decide("PF-1", ar, reg, root=root)
    assert v.outcome.startswith("refused: dependence assumptions unsupported (acf1")
    # (y) squared-return dependence beyond the GARCH bound.  With Gaussian innovations a
    # volatility pattern strong enough to pass 0.37 in squared autocorrelation also pushes
    # the excess kurtosis past the GARCH bound, so this fixture uses an evidence file with
    # t3 in domain (kurtosis bound 31) and trips the squared-return screen alone.
    _evidence(root, passing=("iid_normal", "ar1_0.1", "garch11", "t3"))
    vol = np.repeat(np.array([0.1, 3.0] * 3), 30)[:150]
    y = (
        np.sign(_series(seed=9)) * vol[:, None]
    )  # sign innovations: squares follow the volatility blocks exactly
    v = pf.decide("PF-1", y, reg, root=root)
    assert v.outcome.startswith("refused: dependence assumptions unsupported (sq_acf1")
    _evidence(root)
    # (z) excess kurtosis beyond the bound (t3 is not in the fabricated domain)
    z = _series(seed=10)
    z[7, 0] = 40.0
    v = pf.decide("PF-1", z, reg, root=root)
    assert v.outcome.startswith("refused: dependence assumptions unsupported (excess_kurtosis")


def test_single_read_and_identity_rules(root, tmp_path, no_hook):
    reg = _reg(data={"origin": "real", "partition": "iap-sealed-b", "dataset": "d1"})
    x = _series(effect=0.6)
    # (q) a real series passed as an array is not a runner read
    assert (
        pf.decide("PF-1", x, reg, root=root).outcome
        == "refused: real series not read through read_series by the registered runner"
    )
    f = tmp_path / "synthetic_tree" / "series.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"values": x.tolist()}))
    # (r) read by the developer identity
    dev = pf.read_series(f, reg, identity="ashis")
    assert dev.identity == "ashis" and dev.purpose.startswith("confirmation:PF-1:")
    assert (
        pf.decide("PF-1", dev, reg, root=root).outcome
        == "refused: series read by an identity other than the registered runner"
    )
    # the runner's read decides, and the verdict carries the read record
    run = pf.read_series(f, reg, identity="iap-runner")
    v = pf.decide("PF-1", run, reg, root=root)
    assert v.is_promote and v.read["identity"] == "iap-runner" and v.read["sha256"] == run.sha256
    # (s) the guard refuses real data without a hook: the refusal propagates
    real = tmp_path / "data" / "real" / "series.json"
    real.parent.mkdir(parents=True)
    real.write_text(json.dumps({"values": x.tolist()}))
    with pytest.raises(guard.GuardRefused):
        pf.read_series(real, reg, identity="iap-runner")
    # a registered hook decides the read
    seen = []
    guard.register(lambda p, purpose: seen.append((p, purpose)))
    pf.read_series(real, reg, identity="iap-runner")
    assert seen and seen[0][1].startswith("confirmation:PF-1:")
    # a SeriesRead cannot be forged
    with pytest.raises(PermissionError):
        pf.SeriesRead(x, "p", "h", "iap-runner", "purpose")


def test_only_decide_constructs_the_profile_promote():
    with pytest.raises(PermissionError):
        pf.ProfileToken(object(), "deadbeef")
    v = pf.ProfileVerdict(
        outcome="PROMOTE",
        reason="forged",
        profile="PF-1",
        procedure="P-HAC-L5",
        T=150,
        K=1,
        alpha_op=0.02,
        hypotheses=("H",),
    )
    assert not v.is_promote
    src = (REPO / "python" / "src" / "iap" / "validation" / "profiles.py").read_text(
        encoding="utf-8"
    )
    assert 'return "PROMOTE"' not in src and "PromoteToken(" not in src


def test_committed_evidence_if_present_decides_a_planted_effect_in_its_domain():
    """With the committed evidence (when it exists), every procedure that has an operating
    domain promotes a planted iid-normal effect and refuses an AR(1) 0.9 series."""
    for pid in ("PF-1", "PF-2"):
        dom = pf.operating_domain(pid)
        if dom is None:
            pytest.skip(f"{pid}: no committed evidence in this checkout")
        prof = pf.PROFILES[pid]
        T = max(prof.T_grid)
        for proc, d in dom.items():
            if not d["reference_passes"]:
                continue
            reg = _reg(
                profile=pid,
                horizon_sessions=prof.horizon[0],
                procedure=proc,
                sample_size={"T_min": T, "cap": 1.25},
            )
            spec = prof.procedures[proc]
            if spec["kind"] == "hac":
                reg["hac_lag"] = spec["lag"]
            else:
                reg.pop("hac_lag")
                diag = pf.block_length_rule(
                    _normals(3, T), max_lag=spec["max_lag"], floor=spec["floor"], cap=spec["cap"]
                )
                diag = {k: v for k, v in diag.items() if k != "ljung_box_tail"}
                diag.update({"source": "development", "development_sha256": "d" * 64})
                reg["block_length"] = {
                    "block_length": diag["block_length"],
                    "bootstrap_seed": 1,
                    "diagnostic": diag,
                }
            v = pf.decide(pid, _series(seed=4, T=T, effect=0.5), reg)
            assert v.is_promote, (pid, proc, v.outcome)
            ar = gen.draw(gen.Stream(2), "ar1", {"rho": 0.9}, 1, T, 3)[0]
            assert pf.decide(pid, ar, reg).outcome.startswith(
                "refused: dependence assumptions unsupported"
            )
