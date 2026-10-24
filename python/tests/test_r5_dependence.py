"""R5 (v1.19 / v2.0 stage 1): the real-data dependence audit runner, on synthetic values only.

Checks the predeclared statistics on planted panels, the registry refusals,
the two-part result document and the decision rule.  No real data is read.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

import numpy as np
import pytest
from iap.core.rng import SplitMix64

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "research" / "v119" / "r5_dependence.py"
spec = importlib.util.spec_from_file_location("r5_dependence", RUNNER)
assert spec and spec.loader
r5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r5)


def _ar1(g: int, k: int, rho: float, seed: int) -> np.ndarray:
    rng = SplitMix64(seed)
    x = np.empty((g, k))
    for j in range(k):
        x[0, j] = rng.normal()
        for t in range(1, g):
            x[t, j] = rho * x[t - 1, j] + math.sqrt(1 - rho * rho) * rng.normal()
    return x


def _sessions(g: int) -> list[str]:
    return [f"2019-01-{1 + i:02d}" for i in range(g)]


# ------------------------------------------------------------- statistics --
def test_autocorrelation_matches_numpy_definition():
    rng = SplitMix64(7)
    x = np.array([rng.normal() for _ in range(50)])
    d = x - x.mean()
    for h in (1, 2, 5):
        want = float((d[h:] * d[:-h]).sum() / (d * d).sum())
        assert r5.autocorrelation(x, h) == pytest.approx(want)
    assert math.isnan(r5.autocorrelation(x, 0)) and math.isnan(r5.autocorrelation(x, 50))
    assert math.isnan(r5.autocorrelation(np.ones(10), 1))


def test_autocorrelation_recovers_a_planted_ar1_on_a_long_series():
    x = _ar1(4000, 1, 0.5, 11)[:, 0]
    assert r5.autocorrelation(x, 1) == pytest.approx(0.5, abs=0.05)
    assert r5.autocorrelation(x, 2) == pytest.approx(0.25, abs=0.05)


def test_bartlett_band_is_1_96_over_root_n():
    lo, hi = r5.bartlett_interval(0.1, 16)
    assert hi - lo == pytest.approx(2 * 1.959963984540054 / 4)


def test_excess_kurtosis_is_near_zero_for_normal_and_large_for_heavy_tails():
    rng = SplitMix64(5)
    z = np.array([rng.normal() for _ in range(5000)])
    g2, lo, hi = r5.excess_kurtosis(z)
    assert lo < 0 < hi and abs(g2) < 0.2
    w = np.array([rng.normal() for _ in range(3 * 5000)]).reshape(3, 5000)
    t3 = z / np.sqrt((w**2).sum(axis=0) / 3)  # Student t with 3 df: infinite kurtosis
    g2t, lot, _ = r5.excess_kurtosis(t3)
    assert g2t > 1 and lot > 1  # disqualifying under the R0 band
    assert all(math.isnan(v) for v in r5.excess_kurtosis(np.ones(3)))


def test_first_principal_component_sees_equicorrelation():
    rng = SplitMix64(9)
    g, k, rho = 400, 16, 0.6
    f = np.array([rng.normal() for _ in range(g)])[:, None]
    z = np.array([[rng.normal() for _ in range(k)] for _ in range(g)])
    panel = math.sqrt(rho) * f + math.sqrt(1 - rho) * z
    pc = r5.first_principal_component(panel)
    assert pc["mean_offdiag_corr"] == pytest.approx(rho, abs=0.08)
    assert pc["share"] == pytest.approx(rho + (1 - rho) / k, abs=0.08)
    assert len(pc["loadings"]) == k and all(v > 0 for v in pc["loadings"])
    indep = r5.first_principal_component(z)
    assert indep["mean_offdiag_corr"] == pytest.approx(0.0, abs=0.05)


def test_block_permutation_is_seeded_and_flags_strong_order_dependence():
    strong = _ar1(16, 16, 0.9, 21)
    a = r5.block_permutation(strong, n_perm=300)
    b = r5.block_permutation(strong, n_perm=300)
    assert a == b  # pinned stream
    assert a["share_at_least_observed"] < 0.05
    assert "does not establish independence" in a["reading"]
    rng = SplitMix64(22)
    indep = np.array([[rng.normal() for _ in range(16)] for _ in range(16)])
    c = r5.block_permutation(indep, n_perm=300)
    assert c["share_at_least_observed"] > 0.05


def test_stationarity_split_classifies_by_year():
    panel = _ar1(8, 2, 0.0, 3)
    sessions = [
        "2019-01-02",
        "2020-03-04",
        "2021-07-13",
        "2021-08-13",
        "2025-11-28",
        "2025-12-08",
        "2026-05-15",
        "2026-06-12",
    ]
    st = r5.stationarity_split(panel, sessions)
    assert (st["n_early"], st["n_late"]) == (4, 4) and st["unclassified_sessions"] == []
    row = st["per_hypothesis"][0]
    assert math.isfinite(row["welch_t"]) and row["welch_df"] > 0
    st2 = r5.stationarity_split(panel, sessions[:7] + ["2023-01-01"])
    assert st2["unclassified_sessions"] == ["2023-01-01"]


# ----------------------------------------------------------- decision rule --
def _panel(values, sessions=None, design=True):
    g, k = values.shape
    return {
        "source": "test",
        "sessions": sessions or _sessions(g),
        "hypotheses": [f"H{j}" for j in range(k)],
        "values": values,
        "design": (
            {
                "non_overlapping_sessions": True,
                "no_label_crossing_session_boundary": True,
                "within_session_conditioning": True,
                "manifest": "test",
            }
            if design
            else None
        ),
    }


def test_missing_design_manifest_is_row_1_whatever_the_data():
    res = r5.audit(_panel(_ar1(16, 16, 0.0, 31), design=False), label="t", ledger_note="")
    assert res["conclusion"]["row"] == "missing or incomplete evidence"
    assert res["part_b"]["elements"]["design_independence"]["available"] is False


def test_strong_serial_dependence_is_row_2():
    res = r5.audit(_panel(_ar1(16, 16, 0.9, 32)), label="t", ledger_note="")
    assert res["conclusion"]["row"] == "material dependence detected"
    assert res["part_b"]["elements"]["serial_dependence"]["disqualifying"] is True
    assert "serial_dependence" in res["conclusion"]["why"]


def test_independent_single_hypothesis_small_g_is_row_3_never_row_4():
    res = r5.audit(_panel(_ar1(16, 1, 0.0, 33)), label="t", ledger_note="")
    assert res["conclusion"]["row"] == "dependence not detected, low power"
    assert "weak evidence" in res["conclusion"]["why"]
    assert res["conclusion"]["from"] == "part B only"


def test_independent_k16_panel_shows_the_multiplicity_of_the_r0_screen():
    """Plan section 4 (defect D-28): the per-hypothesis R0 screen flags independent
    data often at K = 16; the family-adjusted companion does not; the row follows the
    R0 screen as written (fail-closed)."""
    from scipy import stats

    flagged_rows = 0
    kurtosis_companion_flags = 0
    for seed in range(40, 52):
        res = r5.audit(_panel(_ar1(16, 16, 0.0, seed)), label="t", ledger_note="")
        els = res["part_b"]["elements"]
        for name in ("serial_dependence", "nonlinear_dependence"):
            assert els[name]["family_adjusted"]["disqualifying"] is False, (seed, name)
        # the kurtosis sampling distribution at G = 16 is right-skewed and the
        # normal-theory SE understates its upper tail: allow a rare companion flag
        kurtosis_companion_flags += els["kurtosis"]["family_adjusted"]["disqualifying"]
        n_flag = sum(r["disqualifying"] for r in els["serial_dependence"]["per_hypothesis"])
        p_flag = 1 - 0.95**5
        assert n_flag <= stats.binom.ppf(0.999, 16, p_flag) + 3, (seed, n_flag)
        any_dq = any(e.get("disqualifying") for e in els.values())
        assert res["conclusion"]["row"] == (
            "material dependence detected" if any_dq else "dependence not detected, low power"
        )
        flagged_rows += any_dq
    assert flagged_rows >= 6  # the multiplicity is real, and recorded
    assert kurtosis_companion_flags <= 2
    exp = els["serial_dependence"]["expected_false_flags_under_independence"]
    assert exp["expected_hypotheses_flagged"] == pytest.approx(16 * (1 - 0.95**5))


def test_g_below_10_is_a_hard_refusal_row_2():
    res = r5.audit(_panel(_ar1(8, 16, 0.0, 34)), label="t", ledger_note="")
    assert res["part_b"]["elements"]["G"]["disqualifying"] is True
    assert res["conclusion"]["row"] == "material dependence detected"


def test_heavy_tails_disqualify_through_the_kurtosis_band():
    rng = SplitMix64(35)
    g, k = 60, 4
    z = np.array([[rng.normal() for _ in range(k)] for _ in range(g)])
    c = np.array([[sum(rng.normal() ** 2 for _ in range(2)) for _ in range(k)] for _ in range(g)])
    t2 = z / np.sqrt(c / 2)  # t with 2 df: infinite kurtosis
    res = r5.audit(
        _panel(t2, sessions=[f"2019-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(g)]),
        label="t",
        ledger_note="",
    )
    assert res["part_b"]["elements"]["kurtosis"]["disqualifying"] is True
    assert res["conclusion"]["row"] == "material dependence detected"


def test_conclusion_rows_and_permitted_conclusions_are_the_plan_5_12_table():
    assert set(r5.ROWS) == set(r5.PERMITTED)
    assert r5.PERMITTED["missing or incomplete evidence"] == "no real-data FWER claim"
    assert "prohibited" in r5.PERMITTED["material dependence detected"]


# ------------------------------------------------------------------ panel --
def test_load_panel_refuses_malformed_input(tmp_path):
    def write(doc):
        p = tmp_path / "panel.json"
        p.write_text(json.dumps(doc))
        return p

    good = {
        "sessions": ["2019-01-01", "2019-01-02", "2019-01-03"],
        "hypotheses": ["a", "b"],
        "values": [[0, 1], [1, 2], [2, 3]],
    }
    assert r5.load_panel(write(good))["values"].shape == (3, 2)
    with pytest.raises(SystemExit):
        r5.load_panel(write({**good, "values": [[0, 1], [1, 2]]}))
    with pytest.raises(SystemExit):
        r5.load_panel(write({**good, "sessions": ["2019-01-03", "2019-01-02", "2019-01-01"]}))
    with pytest.raises(SystemExit):
        r5.load_panel(write({**good, "sessions": ["2019-01-01", "2019-01-01", "2019-01-03"]}))
    with pytest.raises(SystemExit):
        r5.load_panel(write({**good, "values": [[0, 1], [1, float("nan")], [2, 3]]}))


REGISTRY = {
    "sessions": {
        "2019-01-30": [{"state": "in_sample", "symbols": "*"}],
        "2021-08-13": [{"state": "in_sample", "symbols": ["AAPL", "MSFT", "QQQ"]}],
        "2025-12-09": [
            {"state": "spent", "symbols": ["AAPL", "MSFT", "QQQ"], "spent_by": ["p1"]},
            {"state": "unread", "symbols": "*", "reserved_for": ["breadth"]},
        ],
        "2026-06-12": [{"state": "unread", "symbols": "*"}],
    }
}


def test_registry_check_admits_in_sample_and_replayed_spent_sessions_only():
    ok = r5.check_registry(["2019-01-30", "2021-08-13"], ["AAPL"], None, REGISTRY)
    assert [o["access"] for o in ok] == ["in_sample", "in_sample"]
    ok = r5.check_registry(["2025-12-09"], ["AAPL", "MSFT"], "p1", REGISTRY)
    assert ok[0]["access"] == "spent by p1"
    # spent, but not by this prereg: refused
    with pytest.raises(SystemExit):
        r5.check_registry(["2025-12-09"], ["AAPL"], "other", REGISTRY)
    with pytest.raises(SystemExit):
        r5.check_registry(["2025-12-09"], ["AAPL"], None, REGISTRY)
    # a symbol outside the spent scope on that date is sealed: refused
    with pytest.raises(SystemExit):
        r5.check_registry(["2025-12-09"], ["NVDA"], "p1", REGISTRY)
    # sealed and unregistered sessions: refused before any statistic
    with pytest.raises(SystemExit):
        r5.check_registry(["2026-06-12"], ["AAPL"], "p1", REGISTRY)
    with pytest.raises(SystemExit):
        r5.check_registry(["2019-02-01"], None, None, REGISTRY)


def test_the_committed_registry_seals_every_2025_2026_session_for_r5_without_a_prereg():
    from iap.research.holdout_registry import load_registry

    reg = load_registry(root=REPO)
    late = sorted(s for s in reg["sessions"] if s[:4] in ("2025", "2026"))
    assert late
    for iso in late:
        with pytest.raises(SystemExit):
            r5.check_registry([iso], ["AAPL"], None, reg)
    early = sorted(s for s in reg["sessions"] if s[:4] in ("2019", "2020", "2021"))
    assert len(early) == 10
    assert [o["access"] for o in r5.check_registry(early, ["AAPL", "MSFT", "QQQ"], None, reg)] == [
        "in_sample"
    ] * 10


# ------------------------------------------------------------- documents --
def test_selftest_writes_a_two_part_result_that_never_claims_independence(tmp_path):
    assert r5.main(["selftest", "--out", str(tmp_path)]) == 0
    md = (tmp_path / "R5_DEPENDENCE_RESULT_SELFTEST.md").read_text(encoding="utf-8")
    js = json.loads((tmp_path / "r5_dependence_result_SELFTEST.json").read_text(encoding="utf-8"))
    heads = re.findall(r"^## (.+)$", md, flags=re.M)
    assert heads[0].startswith("Part A: diagnostics")
    assert heads[1].startswith("Part B: affirmative evidence for domain membership")
    assert heads[2].startswith("Conclusion (exactly one row of plan 5.12, drawn from part B only)")
    assert "**Part A conclusion.**" in md and "**Part B conclusion.**" in md
    els = js["part_b"]["elements"]
    any_dq = any(e.get("disqualifying") for e in els.values())
    assert js["conclusion"]["row"] == (
        "material dependence detected" if any_dq else "dependence not detected, low power"
    )
    for name in ("serial_dependence", "kurtosis", "nonlinear_dependence"):
        assert els[name]["family_adjusted"]["disqualifying"] is False
    assert "Companion (not an R0 band" in md
    assert "SYNTHETIC SELF-TEST" in md
    # the forbidden reading: nothing says non-detection proves or establishes independence
    assert not re.search(r"(proves|establishes|demonstrates|shows) independence", md)
    assert "not evidence of independence" in md
    assert "does not authorise PROMOTE" in md
    assert js["plan_sha256"] and js["panel"]["sha256"]


def test_plan_document_predeclares_the_constants_the_runner_uses():
    plan = (REPO / "research" / "v119" / "R5_DEPENDENCE_PLAN.md").read_text(encoding="utf-8")
    for needle in (
        f"seed {r5.PERMUTATION_SEED}",
        f"seed {r5.SELFTEST_SEED}",
        f"{r5.N_PERMUTATIONS:,} permutations",
        "lag-1 point estimate above 0.3",
        "2019-2021 vs 2025-2026",
        "described, here or in the result, as evidence of independence",
        "G < 44",
    ):
        assert needle in plan, needle
    assert "## 3. Part A" in plan and "## 4. Part B" in plan


def test_runner_is_not_registered_as_a_prereg_consumer():
    """R5 verifies no signature: it must not appear to consume a signed document."""
    src = RUNNER.read_text(encoding="utf-8")
    assert not re.search(
        r"verify_signed_document|load_signed_rule|analysis_identity|prereg_is_signed", src
    )
