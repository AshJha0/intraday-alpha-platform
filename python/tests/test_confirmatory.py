"""R1 deterministic cases of the frozen R0 specification (section 6) and R2's R1-D.

R1-A Student t against SciPy (and statsmodels when importable; defect D-29), P-BLK,
HAC and CR1 diagnostics; R1-B Holm against statsmodels / a textbook step-down with
ties and all-equal p-values, the family check and the selection history; R1-C
low-cluster promotion prevention and the enumeration test (only
``iap.validation.confirmatory.decide`` constructs PROMOTE); R1-D exact MDE;
R1-E the production path consumes the canonical result.  Tolerances: relative
1e-10 on statistics, absolute 1e-12 on p-values, rejection sets exact.
"""

from __future__ import annotations

import importlib
import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest
from iap.core.rng import SplitMix64
from iap.validation import confirmatory as c

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "python" / "src"
statsmodels = (
    pytest.importorskip("statsmodels", reason="D-29: statsmodels is not in the CI lock")
    if False
    else None
)
try:  # the comparison runs when statsmodels is importable (locally); CI skips it with a reason
    import statsmodels.api as sm
    import statsmodels.stats.multitest as smm
except ImportError:  # pragma: no cover - CI lock has no statsmodels (D-29)
    sm = smm = None
needs_statsmodels = pytest.mark.skipif(sm is None, reason="D-29: statsmodels is not in the CI lock")

REL = 1e-10
ABS_P = 1e-12


def _normals(seed: int, *shape: int) -> np.ndarray:
    rng = SplitMix64(seed)
    return np.array([rng.normal() for _ in range(int(np.prod(shape)))]).reshape(shape)


def _design(G: int = 20, K: int = 16, **over) -> dict:
    d = {
        "family": [f"H{j:02d}" for j in range(K)],
        "procedure": "P-IND",
        "domain": "D-IND",
        "tier": "1a",
        "G": G,
        "alpha_op": 0.020,
        "power_table": {"G_min": 10},
        "T0": "2026-01-01",
        "data": {"origin": "synthetic", "generator": "normal_homoskedastic"},
    }
    d.update(over)
    return d


def _sessions(values: np.ndarray, start: int = 0) -> list[dict]:
    return [
        {"session": f"2026-02-{i + 1 + start:02d}", "values": values[i]}
        for i in range(values.shape[0])
    ]


# ------------------------------------------------------------------ R1-A --
@pytest.mark.parametrize("G", [3, 5, 7, 10, 20, 60])
def test_r1a_student_t_matches_scipy(G):
    from scipy import stats

    x = _normals(100 + G, G) * 3.0 + 0.4
    st = c.student_t_one_sample(x)
    ref = stats.ttest_1samp(x, 0.0, alternative="greater")
    assert st["t"] == pytest.approx(float(ref.statistic), rel=REL)
    assert abs(st["p"] - float(ref.pvalue)) <= ABS_P
    assert st["df"] == G - 1 and st["se"] == pytest.approx(x.std(ddof=1) / math.sqrt(G), rel=REL)
    # CR1 with one observation per cluster is the same statistic
    assert c.cr1_tstat(x) == pytest.approx(st["t"], rel=REL)


@pytest.mark.parametrize("G", [20, 60])
def test_r1a_p_blk_is_the_student_t_of_the_block_means(G):
    from scipy import stats

    x = _normals(200 + G, G)
    bm = c.block_means(x)
    assert bm.shape == (G // 5,) and np.allclose(bm, x.reshape(-1, 5).mean(axis=1))
    st = c.student_t_one_sample(bm)
    ref = stats.ttest_1samp(bm, 0.0, alternative="greater")
    assert st["t"] == pytest.approx(float(ref.statistic), rel=REL) and st["df"] == G // 5 - 1
    assert abs(st["p"] - float(ref.pvalue)) <= ABS_P
    # a trailing incomplete block is dropped and its sessions do not count
    assert c.block_means(_normals(1, 23)).shape == (4,)
    assert c.block_means(_normals(1, 4)).shape == (0,)


def test_r1a_hac_textbook_reference():
    """Bartlett HAC (lag 2) of the mean: hand-written Newey-West."""
    x = _normals(7, 40)
    d = x - x.mean()
    s = float(d @ d) + 2 * (2 / 3) * float(d[1:] @ d[:-1]) + 2 * (1 / 3) * float(d[2:] @ d[:-2])
    want = x.mean() / math.sqrt(s / 40**2)
    assert c.hac_tstat(x, lags=2) == pytest.approx(want, rel=REL)


@needs_statsmodels
@pytest.mark.parametrize("G", [10, 20, 60])
def test_r1a_hac_and_cr1_against_statsmodels(G):
    x = _normals(300 + G, G)
    ones = np.ones((G, 1))
    hac = sm.OLS(x, ones).fit(cov_type="HAC", cov_kwds={"maxlags": 2, "use_correction": False})
    assert c.hac_tstat(x, lags=2) == pytest.approx(float(hac.tvalues[0]), rel=1e-8)
    cl = sm.OLS(x, ones).fit(
        cov_type="cluster", cov_kwds={"groups": np.arange(G), "use_correction": True}
    )
    # statsmodels CR1 uses G/(G-1) * (n-1)/(n-k) with n = G, k = 1: identical to sd/sqrt(G)
    assert c.cr1_tstat(x) == pytest.approx(float(cl.tvalues[0]), rel=1e-8)


# ------------------------------------------------------------------ R1-B --
def _holm_textbook(p: np.ndarray, alpha: float) -> np.ndarray:
    """Holm (1979): sort ascending, reject while p_(i) <= alpha / (m - i + 1)."""
    m = len(p)
    order = np.argsort(p, kind="stable")
    rej = np.zeros(m, dtype=bool)
    for i, idx in enumerate(order):
        if p[idx] <= alpha / (m - i):
            rej[idx] = True
        else:
            break
    return rej


@pytest.mark.parametrize("K", [3, 6, 16])
def test_r1b_holm_matches_the_textbook_step_down_including_ties(K):
    rng = SplitMix64(400 + K)
    for trial in range(200):
        p = np.array([rng.uniform() ** 3 for _ in range(K)])
        if trial % 3 == 0:
            p[: K // 2] = p[0]  # ties
        if trial % 7 == 0:
            p[:] = p[0]  # all equal
        h = c.holm(p, 0.020)
        assert np.array_equal(h["rejected"], _holm_textbook(p, 0.020))
        assert h["n_rejected"] == int(h["rejected"].sum())
        assert np.all(np.diff(np.sort(h["adjusted"])) >= 0) and np.all(h["adjusted"] <= 1.0)
    # all-equal below the Bonferroni floor rejects everything; above it nothing
    assert c.holm([0.001] * K, 0.020)["n_rejected"] == K
    assert c.holm([0.019] * K, 0.020)["n_rejected"] == (K if 0.019 <= 0.020 / K else 0)


@needs_statsmodels
@pytest.mark.parametrize("K", [3, 6, 16])
def test_r1b_holm_against_statsmodels(K):
    rng = SplitMix64(500 + K)
    for trial in range(100):
        p = np.array([rng.uniform() ** 2 for _ in range(K)])
        if trial % 4 == 0:
            p[:2] = p[0]
        want_rej, want_adj, *_ = smm.multipletests(p, alpha=0.020, method="holm")
        h = c.holm(p, 0.020)
        assert np.array_equal(h["rejected"], want_rej)
        assert np.allclose(h["adjusted"], want_adj, atol=ABS_P)


def test_r1b_registered_family_must_equal_the_prereg_family_and_history_is_attached():
    vals = _normals(11, 20, 16)
    base = _design(
        registry={"window_state": "fresh", "confirmation_dataset_looks": 0, "looks_before_T0": 42}
    )
    v = c.decide({**base, "prereg_family": base["family"]}, _sessions(vals))
    assert v.outcome in ("PROMOTE", "REJECT") and v.selection_history["looks_before_T0"] == 42
    bad = c.decide({**base, "prereg_family": base["family"][:-1] + ["H99"]}, _sessions(vals))
    assert bad.outcome == "refused: family differs from the registered prereg"
    assert c.decide({**base, "family": []}, _sessions(vals)).outcome == "refused: empty family"
    assert c.decide({**base, "family": ["A", "A"]}, _sessions(vals)).outcome.startswith(
        "refused: duplicated hypothesis"
    )


# ------------------------------------------------------------------ R1-C --
def test_r1c_low_cluster_promotion_is_refused_by_the_canonical_gate():
    """G = 4 fixture: whatever the within-day HAC t (6.0) says, the gate refuses."""
    vals = _normals(21, 4, 16) + 2.0  # strong effect everywhere
    v = c.decide(_design(G=4, power_table={"G_min": 4}), _sessions(vals))
    assert v.outcome == "refused: G below registered minimum" and v.token is None
    assert (
        c.decide(_design(G=9), _sessions(_normals(22, 9, 16))).outcome
        == "refused: G below registered minimum"
    )
    # a registered power-table minimum above 10 binds too
    assert (
        c.decide(_design(G=12, power_table={"G_min": 20}), _sessions(_normals(23, 12, 16))).outcome
        == "refused: G below registered minimum"
    )


def test_r1c_legacy_paths_are_at_most_iterate_or_ineligible():
    from iap.contracts.types import Verdict as V
    from iap.research.runner import canonical_verdict, gate_record

    assert canonical_verdict("PROMOTE") is V.ITERATE
    assert canonical_verdict("ITERATE") is V.ITERATE and canonical_verdict("REJECT") is V.REJECT
    rec = gate_record({"verdict": "PROMOTE"})
    assert rec == {"gate": "legacy_walk_forward", "legacy_promote_blocked": True}
    sys.path.insert(0, str(REPO / "tools"))
    rc = importlib.import_module("r7_confirm")
    assert rc.verdict(True, {"passed": True}, {"gate_eligible": True}) == "ineligible"


def test_r1c_only_confirmatory_decide_constructs_promote():
    """Enumeration: outside confirmatory.py, no module in python/src constructs a
    PROMOTE verdict string or token (the contracts enum member and comparisons
    against it are reads, not constructions)."""
    constructors = []
    pattern = re.compile(
        r"""(verdict\s*=\s*["']PROMOTE["']|PromoteToken\(|return\s+["']PROMOTE["'])"""
    )
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if rel == "iap/validation/confirmatory.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                constructors.append(f"{rel}:{i}: {line.strip()}")
    assert constructors == [], constructors
    # the token itself cannot be minted outside the module
    with pytest.raises(PermissionError):
        c.PromoteToken(object(), "deadbeef")
    # the contracts enum member is a label, not a gate; a Verdict built from a
    # string outside decide never carries a token
    v = c.Verdict(
        outcome="PROMOTE",
        reason="forged",
        procedure="P-IND",
        domain="D-IND",
        tier="1a",
        G=20,
        K=1,
        alpha_op=0.02,
        hypotheses=("H",),
    )
    assert not v.is_promote


# ------------------------------------------------------------------ R1-D --
@pytest.mark.parametrize(
    ("G", "want"),
    [(5, 1.68), (7, 1.27), (10, 1.00), (20, 0.66), (30, 0.53), (60, 0.37)],
)
def test_r1d_mde_reference_values_at_one_sided_0_025(G, want):
    assert round(c.mde(G, 0.025), 2) == want


@pytest.mark.parametrize("G", [3, 4, 5, 7, 10, 20, 30, 60, 120])
@pytest.mark.parametrize("alpha", [0.020, 0.020 / 3, 0.020 / 16])
@pytest.mark.parametrize("procedure", ["P-IND", "P-BLK"])
def test_r1d_mde_inverts_the_noncentral_t_exactly(G, alpha, procedure):
    from scipy import stats

    n = G if procedure == "P-IND" else G // 5
    if n < 2:
        with pytest.raises(c.MDERefused):
            c.mde(G, alpha, procedure=procedure)
        return
    delta = c.mde(G, alpha, 0.8, procedure=procedure)
    df = n - 1
    tcrit = stats.t.isf(alpha, df)
    assert stats.nct.sf(tcrit, df, delta * math.sqrt(n)) == pytest.approx(0.8, abs=1e-8)
    # monotone in alpha and in G
    assert c.mde(G, alpha / 2, procedure=procedure) > delta
    if procedure == "P-IND":
        assert c.mde(G + 5, alpha) < delta


def test_r1d_mde_extremes():
    assert math.isfinite(c.mde(3, 1e-4)) and c.mde(3, 1e-4) > 10  # df = 2
    with pytest.raises(c.MDERefused):
        c.mde(10, 0.5)
    with pytest.raises(c.MDERefused):
        c.mde(10, 0.02, 1.0)
    with pytest.raises(c.MDERefused):
        c.mde(10, 0.02, 0.01)  # power below alpha
    with pytest.raises(c.MDERefused):
        c.mde(1, 0.02)
    with pytest.raises(c.MDERefused):
        c.mde(10, 0.02, procedure="P-XYZ")


def test_r1d_one_mde_implementation():
    """`grep` finds one noncentral-t inversion: every other MDE emitter scales it."""
    hits = []
    # research/v118/f3_power/power_math.py is bound by the signed F3 power prereg
    # (its bytes cannot change) and serves that closed synthetic study only.
    bound_historical = {"research/v118/f3_power/power_math.py"}
    for base in (SRC, REPO / "research", REPO / "tools"):
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts or p.relative_to(REPO).as_posix() in bound_historical:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"stats\.nct\b|\bnct\.(sf|cdf|ppf)", text):
                hits.append(p.relative_to(REPO).as_posix())
    assert hits == ["python/src/iap/validation/confirmatory.py"], hits


# ------------------------------------------------------------------ R1-E --
def test_r1e_decide_promotes_a_planted_effect_and_carries_the_inference_hash():
    vals = _normals(31, 20, 16)
    vals[:, 5] += 1.5
    v = c.decide(_design(), _sessions(vals))
    assert v.is_promote and v.promoted == ("H05",) and v.token.sha256 == v.inference_sha256
    assert len(v.inference_sha256) == 64
    doc = v.to_json()
    assert doc["gate"] == "confirmatory" and "token" not in doc and doc["promoted"] == ["H05"]
    assert v.statistics[5]["rejected"] and v.statistics[5]["p_holm"] <= 0.020
    assert all(not s["rejected"] for i, s in enumerate(v.statistics) if i != 5)
    # the same data, nothing planted: REJECT, no token, different hash
    r = c.decide(_design(), _sessions(_normals(31, 20, 16)))
    assert r.outcome == "REJECT" and r.token is None and r.inference_sha256 != v.inference_sha256


def test_r1e_production_path_consumes_the_canonical_result():
    """The runner's record takes PROMOTE from the canonical verdict alone: a
    monkeypatched legacy PROMOTE leaves the verdict unchanged; a canonical PROMOTE
    is recorded with gate "confirmatory" and the inference sha256."""
    from iap.contracts.types import Verdict as V
    from iap.research.runner import canonical_verdict, gate_record

    vals = _normals(31, 20, 16)
    vals[:, 5] += 1.5
    promote = c.decide(_design(), _sessions(vals))
    reject = c.decide(_design(), _sessions(_normals(31, 20, 16)))
    assert canonical_verdict("ITERATE", promote) is V.PROMOTE
    assert canonical_verdict("PROMOTE", reject) is V.ITERATE  # legacy PROMOTE, canonical REJECT
    assert canonical_verdict("REJECT", reject) is V.REJECT
    rec = gate_record({"verdict": "PROMOTE", "legacy_promote_blocked": True}, promote)
    assert rec["gate"] == "confirmatory" and rec["inference_sha256"] == promote.inference_sha256
    assert rec["confirmatory_outcome"] == "PROMOTE" and rec["legacy_promote_blocked"] is True


def test_r1e_runner_refuses_without_the_canonical_module(monkeypatch, tmp_path):
    from iap.research import runner as rmod

    monkeypatch.setitem(sys.modules, "iap.validation.confirmatory", None)
    r = rmod.ExperimentRunner.__new__(rmod.ExperimentRunner)
    with pytest.raises(rmod.ResearchError, match="canonical gate"):
        rmod.ExperimentRunner.run(r, spec=None)  # type: ignore[arg-type]


# ----------------------------------------------------- decide_many parity --
def test_decide_many_equals_decide_dataset_by_dataset():
    panels = _normals(41, 300, 20, 16)
    panels[:, :, 2] += 0.6
    design = _design()
    many = c.decide_many(design, panels)
    assert many["outcome"] == "per-dataset"
    for i in range(300):
        v = c.decide(design, _sessions(panels[i]))
        assert (v.outcome == "PROMOTE") == bool(many["promote"][i])
        assert len(v.promoted) == int(many["n_rejected"][i])
        for j, st in enumerate(v.statistics):
            assert abs(st["p"] - many["p"][i, j]) <= ABS_P
            assert st["t"] == pytest.approx(many["t"][i, j], rel=REL)
    # a design-level refusal applies to every dataset
    refused = c.decide_many({**design, "tier": "2"}, panels)
    assert refused["outcome"] == "refused: distribution outside validated domain"
    assert not refused["promote"].any()
