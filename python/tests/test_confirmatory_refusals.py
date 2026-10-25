"""R3 deterministic cases of the frozen R0 specification (sections 5.4 (ii)-(iii), 5.5,
5.6 and 5.7): the gate's refusals, asserted exactly, independent of the Monte Carlo.

R3-D (ii) contaminated selection and (iii) a repeated look on a spent window; R3-E
duplicated ids, out-of-order dates, a session straddling T0, too few sessions; R3-F
boundary conditions (zero variance, non-finite, identical values, empty family, G < 10,
B < 4, magnitudes 1e-300 and 1e300 exact, never NaN); R3-G the ten fixtures (a)-(j) that
show a domain declaration is not self-certifying, and that no field a caller controls
produces eligibility without the evidence fields.  Also the mechanics of the candidate
domain: ``dblk_validated`` reads the committed ``research/validation/R0_RESULTS.json``
and the gate applies the withdrawal.  The Monte Carlo itself is
``test_confirmatory_adversarial.py`` (slow).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from iap.core.rng import SplitMix64
from iap.validation import confirmatory as c

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "python" / "tests" / "spec"
sys.path.insert(0, str(SPEC))
import r3_generators as gen  # noqa: E402
import r3_suite as suite  # noqa: E402

RESULTS = REPO / "research" / "validation" / "R0_RESULTS.json"


def _values(seed: int, G: int, K: int = 16, scale: float = 1.0) -> np.ndarray:
    return gen.Stream(seed).normals(G, K) * scale


def _sessions(values: np.ndarray, ids: list[str] | None = None, **extra) -> list[dict]:
    g = values.shape[0]
    ids = ids or [f"2026-03-{i + 1:02d}" for i in range(g)]
    return [{"session": ids[i], "values": values[i], **extra} for i in range(g)]


def _design(G: int = 20, **over) -> dict:
    d = {
        "family": [f"H{j:02d}" for j in range(16)],
        "procedure": "P-IND",
        "domain": "D-IND",
        "tier": "1a",
        "G": G,
        "alpha_op": 0.020,
        "power_table": {"G_min": 10},
        "T0": "2026-03-01",
        "registry": {"window_state": "fresh", "confirmation_dataset_looks": 0},
        "data": {"origin": "synthetic", "generator": "normal_homoskedastic"},
    }
    d.update(over)
    return d


def _passing_evidence() -> dict:
    """Section 4 evidence with every diagnostic passing (G = 20)."""
    return {
        "kurtosis": [{"estimate": 0.1, "lo": -0.9, "hi": 1.1} for _ in range(16)],
        "design_independence": {
            "non_overlapping_sessions": True,
            "no_label_crossing_session_boundary": True,
            "within_session_conditioning": True,
        },
        "serial_dependence": [
            {
                "lag1": 0.05,
                "intervals": [[-0.4, 0.5], [-0.45, 0.4], [-0.4, 0.4], [-0.5, 0.3], [-0.4, 0.45]],
            }
            for _ in range(16)
        ],
        "nonlinear_dependence": [{"p_by_lag": [0.4, 0.6, 0.3, 0.8, 0.5]} for _ in range(16)],
    }


def _approved_memo(conclusion: str | None = "eligible") -> dict:
    m = {
        "path": "research/v119/H1_DOMAIN_JUSTIFICATION.md",
        "sha256": "a" * 64,
        "approval_path": "docs/release/REVIEWER_H1_DOMAIN_2026-11-01.md",
        "approval_sha256": "b" * 64,
    }
    if conclusion is not None:
        m["conclusion"] = conclusion
    return m


def _real_design(**over) -> dict:
    return _design(
        data={"origin": "real", "evidence": _passing_evidence(), "memo": _approved_memo()}, **over
    )


# ------------------------------------------------------------ the stream --
def test_generator_stream_is_splitmix64_bit_for_bit():
    s, r = gen.Stream(20301000), SplitMix64(20301000)
    u = s.uniforms(400)
    assert all(u[i] == r.uniform() for i in range(400))
    s, r = gen.Stream(20301217), SplitMix64(20301217)
    z = s.normals(300)
    assert all(z[i] == r.normal() for i in range(300))


def test_cell_seeds_follow_the_r0_rule_and_are_distinct():
    seeds = [cell.seed for cell in gen.CELLS]
    assert len(set(seeds)) == len(seeds)
    assert all(20301000 <= s < 20301500 for s in seeds)
    for cell in gen.CELLS:
        assert cell.seed == 20301000 + 100 * gen.CASE_INDEX[cell.case] + cell.index


def test_cell_table_matches_r0_section_5():
    by = {}
    for cell in gen.CELLS:
        by.setdefault((cell.case, cell.domain_class, cell.kind), []).append(cell)
    assert len(by[("R3-A", "claimed", "size")]) == 4  # rho = 0, P-IND, G in {10, 20, 30, 60}
    assert len(by[("R3-A", "candidate", "size")]) == 12  # rho <= 0.3, P-BLK, G in {20, 30, 60}
    assert len(by[("R3-B", "claimed", "size")]) == 8
    assert len(by[("R3-C", "claimed", "size")]) == 14  # tier 1 (a) and (b) at the 7 G
    assert len(by[("R3-C", "stress", "size")]) == 4  # t3 and lognormal at G in {10, 30}
    assert len(by[("R3-C", "claimed", "power")]) == 2 and all(
        cell.n == 5000 for cell in by[("R3-C", "claimed", "power")]
    )
    assert len(by[("R3-D", "claimed", "size")]) == 1
    assert len(by[("R3-E", "claimed", "size")]) == 2
    assert all(cell.n == 20000 for cell in gen.CELLS if cell.kind == "size")


# ------------------------------------------------------------- R3-D (ii)/(iii) --
def test_r3d_contaminated_selection_is_refused_by_the_ledger_before_any_statistic():
    v = _values(7, 20)
    d = _design(registry={"window_state": "fresh", "confirmation_dataset_looks": 16})
    out = c.decide(d, _sessions(v))
    assert out.outcome == "refused: looks on confirmation data by the same family"
    assert out.statistics == () and out.token is None


def test_r3d_repeated_look_on_a_spent_window_is_refused_by_the_registry():
    v = _values(8, 20)
    out = c.decide(_design(registry={"window_state": "spent"}), _sessions(v))
    assert out.outcome == "refused: confirmation window spent" and out.statistics == ()


# -------------------------------------------------------------------- R3-E --
def test_r3e_duplicated_session_ids_refused():
    v = _values(9, 20)
    ids = [f"2026-03-{i + 1:02d}" for i in range(20)]
    ids[5] = ids[4]
    assert c.decide(_design(), _sessions(v, ids)).outcome == "refused: duplicated session"


def test_r3e_out_of_order_dates_refused():
    v = _values(10, 20)
    ids = [f"2026-03-{i + 1:02d}" for i in range(20)]
    ids[3], ids[4] = ids[4], ids[3]
    assert c.decide(_design(), _sessions(v, ids)).outcome == "refused: sessions out of order"


def test_r3e_session_straddling_t0_refused():
    v = _values(11, 20)
    s = _sessions(v)
    s[0]["straddles_T0"] = True
    assert c.decide(_design(), s).outcome == "refused: session straddles or precedes T0"
    s = _sessions(v, [f"2026-02-{i + 1:02d}" for i in range(20)])  # before T0
    assert c.decide(_design(), s).outcome == "refused: session straddles or precedes T0"


def test_r3e_missing_sessions_below_registered_g_refused_and_dropped_listed():
    v = _values(12, 20)
    m = {"usable_units": 60, "coverage_time_min": 234.0, "coverage_grid": 0.5}
    s = _sessions(v, usable_units=120, coverage_time_min=300.0, coverage_grid=0.8)
    s[7]["usable_units"] = 30  # 10 % of the window fails m_min
    s[15]["coverage_time_min"] = 100.0
    out = c.decide(_design(m_min=m), s)
    assert out.outcome == "refused: fewer sessions than registered"
    assert [d["session"] for d in out.dropped_sessions] == ["2026-03-08", "2026-03-16"]
    assert out.dropped_sessions[0]["failing"] == ["usable_units"]
    assert out.dropped_sessions[1]["failing"] == ["coverage_time"]


def test_r3e_window_cap_reached_is_confirmation_pending_with_nothing_examined():
    v = _values(13, 25)
    m = {"usable_units": 60}
    s = _sessions(v, usable_units=120)
    for i in (1, 4, 9, 12, 17, 20):
        s[i]["usable_units"] = 10
    out = c.decide(_design(m_min=m), s)  # 25 attempted = cap 1.25 x 20, 19 usable
    assert out.outcome == "confirmation pending" and out.statistics == ()


def test_r3e_dropped_sessions_do_not_count_and_the_size_cell_design_is_admitted():
    cell = gen.cell_by_id("R3-E/00/missing_10pct_dropped_G20")
    data = gen.generate(cell)
    d = suite.design_for(cell)
    s = [{**m, "values": data["panel"][0, i]} for i, m in enumerate(data["sessions"])]
    out = c.decide(d, s)
    assert out.outcome in ("PROMOTE", "REJECT")
    assert len(out.sessions_used) == 20 and "2026-03-08" not in out.sessions_used
    assert [x["session"] for x in out.dropped_sessions] == ["2026-03-08", "2026-03-16"]


# -------------------------------------------------------------------- R3-F --
def test_r3f_zero_variance_and_identical_values_refused():
    v = _values(14, 20)
    v[:, 3] = 0.0
    assert c.decide(_design(), _sessions(v)).outcome == "refused: zero variance"
    v = _values(15, 20)
    v[:, 0] = 2.5
    assert c.decide(_design(), _sessions(v)).outcome == "refused: zero variance"


def test_r3f_non_finite_refused():
    for bad in (math.nan, math.inf, -math.inf):
        v = _values(16, 20)
        v[2, 5] = bad
        assert c.decide(_design(), _sessions(v)).outcome == "refused: non-finite value"


def test_r3f_empty_family_refused():
    v = _values(17, 20)
    assert c.decide(_design(family=[]), _sessions(v)).outcome == "refused: empty family"
    assert c.decide(_design(family=None), _sessions(v)).outcome == "refused: empty family"


def test_r3f_g_below_10_for_p_ind_and_b_below_4_for_p_blk_refused():
    v = _values(18, 9)
    out = c.decide(_design(G=9, power_table={"G_min": 5}), _sessions(v))
    assert out.outcome == "refused: G below registered minimum"
    v = _values(19, 15)
    d = _design(
        G=15,
        procedure="P-BLK",
        domain="D-BLK",
        dependence={
            "rho": 0.0,
            "evidence": {"generator": c.DBLK_GENERATOR, "block_length": 5},
        },
        data={"origin": "synthetic", "generator": c.DBLK_GENERATOR, "rho": 0.0},
    )
    assert c.decide(d, _sessions(v)).outcome == "refused: B below 4"


@pytest.mark.parametrize("scale", [1e-300, 1e300])
def test_r3f_extreme_magnitudes_are_exact_never_nan(scale):
    v = _values(20, 20)
    ref = c.decide(_design(), _sessions(v))
    out = c.decide(_design(), _sessions(v * scale))
    assert out.outcome == ref.outcome and out.promoted == ref.promoted
    for a, b in zip(out.statistics, ref.statistics, strict=True):
        assert math.isfinite(a["t"]) and math.isfinite(a["p"])
        assert a["t"] == pytest.approx(b["t"], rel=1e-10)
        assert abs(a["p"] - b["p"]) <= 1e-12
    st = c.student_t_one_sample(v[:, 0] * scale)
    assert math.isfinite(st["t"]) and math.isfinite(st["p"])


# -------------------------------------------------------------------- R3-G --
def _dblk_real(**over) -> dict:
    d = _real_design(
        procedure="P-BLK",
        domain="D-BLK",
        G=20,
        dependence={"rho": 0.1, "evidence": {"generator": c.DBLK_GENERATOR, "block_length": 5}},
    )
    d.update(over)
    return d


V20 = _values(21, 20)


def test_r3g_a_dblk_without_section_4_evidence_fields_refused():
    d = _dblk_real(dependence={"rho": 0.1})
    assert c.decide(d, _sessions(V20)).outcome == "refused: dependence evidence missing"
    d = _dblk_real()
    del d["dependence"]
    assert c.decide(d, _sessions(V20)).outcome == "refused: dependence evidence missing"


@pytest.mark.parametrize("rho", [-0.1, 0.25, 0.4, 0.5])
def test_r3g_b_dblk_rho_outside_the_tested_grid_or_negative_refused(rho):
    d = _dblk_real(
        dependence={"rho": rho, "evidence": {"generator": c.DBLK_GENERATOR, "block_length": 5}}
    )
    assert c.decide(d, _sessions(V20)).outcome == "refused: dependence assumptions unsupported"


def test_r3g_dblk_in_grid_is_refused_while_withdrawn_by_the_committed_results():
    # the committed R0_RESULTS.json decides: 6 of 12 candidate cells fail -> withdrawn
    assert c.dblk_validated(REPO) is False
    d = _dblk_real()
    assert (
        c.decide(d, _sessions(V20), root=REPO).outcome
        == "refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19)"
    )


def test_dblk_validated_mechanics(tmp_path):
    rel = tmp_path / c.RESULTS_REL
    rel.parent.mkdir(parents=True)
    assert c.dblk_validated(tmp_path) is False  # absent file
    cells = [{"domain_class": "candidate", "result": "pass"} for _ in range(12)]
    rel.write_text(json.dumps({"cells": cells}), encoding="utf-8")
    assert c.dblk_validated(tmp_path) is True
    cells[11]["result"] = "fail"
    rel.write_text(json.dumps({"cells": cells}), encoding="utf-8")
    assert c.dblk_validated(tmp_path) is False  # one failure withdraws the whole domain
    rel.write_text(json.dumps({"cells": cells[:11]}), encoding="utf-8")
    assert c.dblk_validated(tmp_path) is False  # 11 cells are not 12
    rel.write_text("not json", encoding="utf-8")
    assert c.dblk_validated(tmp_path) is False


def test_r3g_c_disqualifying_kurtosis_interval_at_g20_refused():
    d = _real_design()
    d["data"]["evidence"]["kurtosis"][4] = {"estimate": 2.4, "lo": 1.2, "hi": 3.6}
    assert c.decide(d, _sessions(V20)).outcome == "refused: distribution outside validated domain"


def test_r3g_d_lag1_autocorrelation_0_4_refused():
    d = _real_design()
    d["data"]["evidence"]["serial_dependence"][0]["lag1"] = 0.4
    assert c.decide(d, _sessions(V20)).outcome == "refused: dependence assumptions unsupported"
    d = _real_design()
    d["data"]["evidence"]["serial_dependence"][2]["intervals"][3] = [0.05, 0.4]  # excludes 0
    assert c.decide(d, _sessions(V20)).outcome == "refused: dependence assumptions unsupported"


@pytest.mark.parametrize("G", [10, 20, 60])
def test_r3g_e_tier_2_refused_at_any_g(G):
    v = _values(22, G)
    d = _design(
        G=G,
        tier="2",
        data={"origin": "synthetic", "generator": "t3_heteroskedastic_logspaced_1_5_cyclic"},
    )
    assert c.decide(d, _sessions(v)).outcome == "refused: distribution outside validated domain"
    d = _real_design(G=G, tier="2")
    assert c.decide(d, _sessions(v)).outcome == "refused: distribution outside validated domain"


def test_r3g_f_omitted_power_table_refused():
    d = _real_design()
    del d["power_table"]
    assert c.decide(d, _sessions(V20)).outcome == "refused: power table missing"
    d = _real_design(power_table={})
    assert c.decide(d, _sessions(V20)).outcome == "refused: power table missing"


def test_r3g_g_every_diagnostic_passing_but_no_approved_memo_refused():
    d = _real_design()
    del d["data"]["memo"]
    assert c.decide(d, _sessions(V20)).outcome == "refused: domain justification memo missing"
    d = _real_design()
    d["data"]["memo"] = {
        "path": "x.md",
        "sha256": "c" * 64,
        "conclusion": "eligible",
    }  # no approval
    assert (
        c.decide(d, _sessions(V20)).outcome
        == "refused: domain justification memo approval not bound"
    )


@pytest.mark.parametrize("interpolation", [None, "interpolated from G = 20 and 30"])
def test_r3g_h_heteroskedastic_tier_1b_at_untested_g_25_refused(interpolation):
    v = _values(23, 25)
    data = {"origin": "synthetic", "generator": c.TIER1B_GENERATORS[0]}
    if interpolation:
        data["interpolation"] = interpolation
    d = _design(G=25, tier="1b", data=data)
    assert c.decide(d, _sessions(v)).outcome == "refused: G outside validated grid"
    for g in c.TIER1B_G_GRID:  # the tested grid is admitted
        assert c.decide(_design(G=g, tier="1b", data=data), _sessions(_values(24, g))).outcome in (
            "PROMOTE",
            "REJECT",
        )


def test_r3g_i_approved_memo_concluding_not_eligible_always_refuses():
    d = _real_design()
    d["data"]["memo"] = _approved_memo("not eligible")
    assert (
        c.decide(d, _sessions(V20)).outcome
        == "refused: domain justification memo concludes not eligible"
    )


def test_r3g_j_approved_memo_without_a_conclusion_field_refused():
    d = _real_design()
    d["data"]["memo"] = _approved_memo(None)
    assert (
        c.decide(d, _sessions(V20)).outcome
        == "refused: domain justification memo has no conclusion"
    )


def test_r3g_real_data_with_complete_evidence_and_eligible_memo_is_admitted():
    out = c.decide(_real_design(), _sessions(V20))
    assert out.outcome in ("PROMOTE", "REJECT") and out.inference_sha256


def test_r3g_no_caller_controlled_field_produces_eligibility_without_the_evidence():
    """Flags a caller might add (eligible, approved, reviewed, domain_ok, override) change
    nothing: without the evidence fields the real-data declaration is refused."""
    base = _design(data={"origin": "real"})
    flags = {
        "eligible": True,
        "approved": True,
        "reviewed": True,
        "domain_ok": True,
        "override": "owner",
        "force": True,
        "dependence_ok": True,
        "validated": True,
    }
    d = {**base, **flags}
    d["data"].update(flags)
    d["data"]["memo"] = {"conclusion": "eligible", **flags}
    out = c.decide(d, _sessions(V20))
    assert out.refused and out.outcome == "refused: domain evidence missing"
    # with evidence but an unbound memo the flags still do nothing
    d["data"]["evidence"] = _passing_evidence()
    out = c.decide(d, _sessions(V20))
    assert out.outcome == "refused: domain justification memo approval not bound"
    # a refusal is terminal: no PROMOTE token can be minted outside decide
    with pytest.raises(PermissionError):
        c.PromoteToken(object(), "0" * 64)


# ------------------------------------------------ the committed evidence file --
def test_committed_r0_results_apply_the_acceptance_rules():
    doc = json.loads(RESULTS.read_text(encoding="utf-8"))
    cells = doc["cells"]
    assert {cell.id for cell in gen.CELLS} == {r["id"] for r in cells}
    for r in cells:
        if r["kind"] == "power":
            assert r["result"] == ("pass" if r["detections"] >= 4047 else "fail")
            assert r["planted"]["theoretical_power"] == 0.9 and r["n"] == 5000
            assert r["lower95"] == pytest.approx(gen.clopper_pearson_lower(r["detections"], r["n"]))
            continue
        assert r["n"] == 20000
        assert r["upper95"] == pytest.approx(gen.clopper_pearson_upper(r["k"], r["n"]))
        if r["domain_class"] in ("claimed", "candidate"):
            assert r["result"] == ("pass" if r["k"] <= 463 else "fail")
        else:
            assert r["result"] == "reported"
    claimed = [r for r in cells if r["domain_class"] == "claimed" and r["kind"] == "size"]
    assert claimed and all(r["result"] == "pass" for r in claimed)
    assert all(r["result"] == "pass" for r in cells if r["kind"] == "power")
    candidate = [r for r in cells if r["domain_class"] == "candidate"]
    assert len(candidate) == 12
    assert doc["summary"]["dblk_validated"] == all(r["result"] == "pass" for r in candidate)
    assert c.dblk_validated(REPO) == doc["summary"]["dblk_validated"]
    assert gen.clopper_pearson_upper(463, 20000) == pytest.approx(0.02498, abs=5e-6)
    assert gen.clopper_pearson_lower(4047, 5000) >= 0.800
    assert gen.clopper_pearson_lower(4046, 5000) < 0.800
