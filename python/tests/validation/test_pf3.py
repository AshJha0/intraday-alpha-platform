"""PF-3, the combined-book confirmation procedure (U13): reference cases for the
studentized circular block bootstrap and the vectorised screens, the accrual rule, the
registration refusals, the fail-closed evaluation, and a smoke subset of the synthetic
study.  The study's evidence is ``research/v2/confirmation_evidence/PF-3_STUDY.json``.
"""

from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from iap.validation import pf3
from iap.validation import profiles as pf

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "python" / "tests" / "spec"))
import pf3_generators as gen  # noqa: E402
import pf3_suite as suite  # noqa: E402


def _normals(seed: int, *shape: int) -> np.ndarray:
    return gen.Stream(seed).normals(*shape)


def _ma(seed: int, T: int, h: int) -> np.ndarray:
    e = _normals(seed, T + h - 1)
    return np.convolve(e, np.ones(h) / math.sqrt(h), mode="valid")


# ----------------------------------------------------------- reference cases --
def _reference_scbb(x: np.ndarray, L: int, B: int, seed: int) -> float:
    """Resample by resample, scalar loops, the same rng draws."""
    T = x.size
    nb = -(-T // L)
    ext = np.concatenate([x, x[: L - 1]])
    cb = np.array([ext[t : t + L].mean() for t in range(T)])
    m = x.mean()
    g0 = sum((c - m) ** 2 for c in cb) / T
    g1 = sum((cb[t] - m) * (cb[(t + L) % T] - m) for t in range(T)) / T
    s = math.sqrt(g0 * max(1.0, 1.0 + 2.0 * g1 / g0) / nb)
    t0 = m / s
    idx = np.random.default_rng(seed).integers(0, T, size=(1, 1, B * nb))[0, 0]
    hits = 0
    for b in range(B):
        draw = cb[idx[b * nb : (b + 1) * nb]]
        sd = float(np.std(draw, ddof=1)) / math.sqrt(nb)
        ts = (draw.mean() - m) / sd if sd > 0 else -math.inf
        hits += ts >= t0
    return (1 + hits) / (B + 1)


@pytest.mark.parametrize("L", [5, 12, 37])
def test_scbb_matches_the_explicit_reference(L):
    x = _normals(20359001, 120) + 0.1
    got = pf3.scbb_pvalue(x, L, resamples=199, seed=7)["p"]
    assert got == pytest.approx(_reference_scbb(x, L, 199, 7), abs=1e-12)


def test_scbb_many_rows_equal_single_calls_and_nan_on_constant():
    x = _normals(20359002, 3, 80, 2)
    x[1, :, 1] = 0.5
    p = pf3.scbb_pvalues_many(x, 8, 99, np.random.default_rng(3))
    assert np.isnan(p[1, 1]) and np.isfinite(p[0]).all()
    assert ((p[np.isfinite(p)] > 0) & (p[np.isfinite(p)] <= 1)).all()


def test_dependence_length_matches_the_profiles_rule():
    for seed, h in ((20359003, 1), (20359004, 5), (20359005, 15)):
        x = _ma(seed, 300, h)
        want = pf.block_length_rule(x, max_lag=60, floor=5, cap=60)["L_dep"]
        got = pf3.dependence_length_many(x.reshape(1, -1, 1))[0, 0]
        assert got == want


def test_screens_on_known_series():
    T = 500
    iid = _normals(20359006, 1, T, 1)
    s = pf3.screen_stats_many(iid)
    assert s["dep_length"][0, 0] < 30 and abs(s["vol_cluster"][0, 0]) < 0.2
    ma = _ma(20359007, T, 40).reshape(1, -1, 1)
    assert pf3.screen_stats_many(ma)["dep_length"][0, 0] >= 20


# -------------------------------------------------------------- accrual rule --
@pytest.mark.parametrize(
    ("usable", "attempted", "state"),
    [(0, 0, "accruing"), (249, 300, "accruing"), (250, 260, "complete"), (249, 312, "pending")],
)
def test_accrual_status_counts_only(usable, attempted, state):
    assert (
        pf3.accrual_status(usable_sessions=usable, attempted_sessions=attempted, T_min=250) == state
    )


def test_accrual_status_rejects_impossible_counts():
    with pytest.raises(ValueError):
        pf3.accrual_status(usable_sessions=5, attempted_sessions=4, T_min=250)


# ------------------------------------------------------------- registration --
def _study(*, ref_pass: bool = True, power_pass: bool = True) -> dict:
    cells = []
    for T in pf3.T_GRID:
        for g, h, res in (
            ("iid_normal", 0, "pass" if ref_pass else "fail"),
            ("mix", 10, "pass"),
            ("mix", 40, "fail"),
        ):
            cells.append(
                {
                    "kind": "size",
                    "generator": g,
                    "params": {"h_long": h} if h else {},
                    "T": T,
                    "result": res,
                    "screen_q": {"dep_length": 30.0, "vol_cluster": 0.3, "excess_kurtosis": 2.0},
                }
            )
        cells.append(
            {
                "kind": "power",
                "generator": "iid_normal",
                "params": {},
                "T": T,
                "result": "pass" if power_pass else "fail",
            }
        )
    return {"status": "full", "T_grid": list(pf3.T_GRID), "cells": cells}


def _registration(dev: np.ndarray) -> dict:
    rule = pf.block_length_rule(dev, max_lag=pf3.MAX_LAG, floor=pf3.FLOOR, cap=pf3.CAP)
    return {
        "profile": "PF-3",
        "statistic": pf3.STATISTIC,
        "family": list(pf3.FAMILY),
        "selected": "C1",
        "procedure": pf3.PROCEDURE,
        "alpha_op": 0.020,
        "block_length": {
            "block_length": rule["block_length"],
            "diagnostic": {
                "source": "development",
                "development_sha256": "0" * 64,
                "acf": rule["acf"],
                "band": rule["band"],
                "T_dev": rule["T_dev"],
                "max_lag": rule["max_lag"],
            },
        },
        "bootstrap_seed": 4242,
        "sample_size": {"T_min": 250, "cap": 1.25},
        "component_horizons": [3, 10],
        "refusal_conditions": list(pf3.SCREENS),
        "economic_screen": {
            "on": "combined_capital",
            "capital_usd": 1e6,
            "hurdle_bps": 0.0,
            "margin": 0.0,
        },
        "registry": {"window_state": "fresh", "confirmation_dataset_looks": 0},
        "data": {"origin": "synthetic", "partition": "none", "dataset": "unit"},
    }


@pytest.fixture
def reg():
    return _registration(_normals(20359010, 300))


def test_an_admissible_registration_has_no_refusals(reg):
    assert pf3.registration_refusals(reg, pf3.operating_domain(_study())) == []


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("profile",), "PF-2", "another profile"),
        (("statistic",), "daily_mtm_portfolio_return", "unregistered statistic"),
        (("family",), ["C0", "C1"], "K_C = 3"),
        (("selected",), "C3", "selected"),
        (("procedure",), "P-CBB", "P-SCBB"),
        (("procedure",), "P-IND", "P-SCBB"),
        (("alpha_op",), 0.05, "alpha_op"),
        (("block_length", "block_length"), 7, "declared rule"),
        (("block_length", "diagnostic", "source"), "confirmation", "development-data"),
        (("sample_size", "T_min"), 120, "T_min >= 250"),
        (("sample_size", "cap"), 2.0, "cap 1.25"),
        (("component_horizons",), [3, 40], "overlapping-exposure"),
        (("refusal_conditions",), ["acf1"], "declared screens"),
        (("economic_screen", "on"), "per_component", "combined registered capital"),
        (("registry", "confirmation_dataset_looks"), 1, "looks"),
        (("registry", "window_state"), "spent", "spent window"),
        (("data", "origin"), None, "origin undeclared"),
    ],
)
def test_each_registration_defect_is_refused(reg, path, value, reason):
    bad = copy.deepcopy(reg)
    node = bad
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    out = pf3.registration_refusals(bad, pf3.operating_domain(_study()))
    assert any(reason in r for r in out), out


def test_track_b_partition_is_refused_for_real_data(reg):
    reg["data"] = {"origin": "real", "partition": "iap-sealed-b", "dataset": "x"}
    out = pf3.registration_refusals(reg, pf3.operating_domain(_study()))
    assert any("never Track B" in r for r in out)


@pytest.mark.parametrize("study", [{}, {"status": "partial"}, "noref", "nopower"])
def test_no_validated_domain_refuses_everything(reg, study):
    doc = (
        {"noref": _study(ref_pass=False), "nopower": _study(power_pass=False)}.get(study)
        if isinstance(study, str)
        else study
    )
    out = pf3.evaluate(reg, _normals(1, 300, 3), study=doc)
    assert out["outcome"].startswith("refused: profile not validated")


# ---------------------------------------------------------------- evaluation --
def test_domain_comes_from_the_study():
    dom = pf3.operating_domain(_study())
    assert (
        dom["generators"] == ["iid_normal", 'mix{"h_long": 10}']
        and dom["max_component_horizon"] == 10
    )


def test_planted_effect_on_the_selected_configuration_is_promotable(reg):
    x = _normals(20359011, 300, 3)
    x[:, 1] += 0.6
    out = pf3.evaluate(reg, x, study=_study())
    assert out["outcome"].startswith("inference") and out["holm_reject"][1] and out["promotable"]


def test_a_rejection_of_a_non_selected_configuration_is_not_promotable(reg):
    x = _normals(20359012, 300, 3)
    x[:, 0] += 0.6
    out = pf3.evaluate(reg, x, study=_study())
    assert out["holm_reject"][0] and not out["promotable"]


def test_the_economic_screen_binds(reg):
    reg["economic_screen"]["hurdle_bps"] = 1e5  # 10 per session: unattainable
    x = _normals(20359011, 300, 3)
    x[:, 1] += 0.6
    out = pf3.evaluate(reg, x, study=_study())
    assert out["holm_reject"][1] and not out["promotable"]


def test_series_refusals(reg):
    st = _study()
    assert "shape" in pf3.evaluate(reg, _normals(2, 300, 2), study=st)["outcome"]
    assert "T_min" in pf3.evaluate(reg, _normals(2, 200, 3), study=st)["outcome"]
    x = _normals(2, 300, 3)
    x[5, 0] = np.nan
    assert "non-finite" in pf3.evaluate(reg, x, study=st)["outcome"]
    x = _normals(2, 300, 3)
    x[:, 2] = 0.1
    assert "zero variance" in pf3.evaluate(reg, x, study=st)["outcome"]


def test_dependence_beyond_the_block_is_refused(reg):
    x = np.stack([_ma(20359020 + j, 300, 60) for j in range(3)], axis=1)
    out = pf3.evaluate(reg, x, study=_study())
    assert out["outcome"].startswith("refused: dep_length outside the validated domain")


def test_real_data_needs_the_runner_read(reg):
    reg["data"] = {"origin": "real", "partition": "iap-sealed-c", "dataset": "x"}
    out = pf3.evaluate(reg, _normals(3, 300, 3), study=_study())
    assert out["outcome"] == "refused: real-data series not read by the registered runner"


def test_profiles_gate_still_refuses_pf3():
    v = pf.decide("PF-3", np.zeros((300, 3)), {"profile": "PF-3"})
    assert str(v.outcome).startswith("refused") and "PF-3" in str(v.outcome)


# --------------------------------------------------------------------- smoke --
@pytest.mark.parametrize(("kind", "idx"), [("size", 0), ("size", 8), ("power", 0)])
def test_smoke_subset_of_the_study(kind, idx):
    cell = next(c for c in gen.cells(planted=suite.planted) if (c.kind, c.index) == (kind, idx))
    r = suite.run_cell(cell, n=40, chunk=20)
    assert r["result"] == "smoke" and r["n"] == 40 and r["nan_p"] == 0 and 0 <= r["k"] <= 40
    assert pf3.FLOOR <= r["block_length"] <= pf3.CAP


def test_generators_have_unit_variance_and_zero_mean():
    for g, p in gen.GENERATORS:
        x = gen.draw(gen.Stream(20359100), g, p, 400, 250, 1)
        assert abs(x.mean()) < 0.15, g
        assert x.var() == pytest.approx(1.0, abs=0.25), g


def test_committed_study_domain_excludes_overlapping_exposure(reg):
    dom = pf3.operating_domain(pf3.load_study())
    assert dom is not None and dom["generators"] == ["garch11", "iid_normal", "t3"]
    assert dom["max_component_horizon"] == 0
    assert any("overlapping-exposure" in r for r in pf3.registration_refusals(reg, dom))
    reg["component_horizons"] = [1]
    assert pf3.registration_refusals(reg, dom) == []
