"""Decision equivalence of the REL-08 v2 short-circuit (speed option), incl. threshold-boundary and solver-fallback cases.

Synthetic data only.  The claim under test: when C1, C2, C4 or C3 settles the outcome, the two extra books behind
C6/C7 cannot change the decision, so skipping them changes no outcome, no reason and no other field.
"""

from __future__ import annotations

import copy
import importlib.util
import itertools
import math
from pathlib import Path

import pytest
from iap.research import rel08_screen as rs
from iap.research import rel08_screen_v2 as v2

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "rel08_v2_dryrun", REPO / "research" / "v118" / "rel08" / "dryrun" / "rel08_v2_dryrun.py"
)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

CRITERIA = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")
SKIPPED = ("influential_removed_net_bps", "alt_normalisation_net_bps")


def _same(a, b):
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def _rule(book=None, draws=99):
    rule = dr.base_rule(book or {"alpha_bps": 20.0, "risk_aversion": 100.0}, draws=draws)
    rule.doc["sufficiency_v2"] = {"sessions_total": 1, "sessions_min_qualifying": 1}
    return rule


def _variant(rule, vid="V1"):
    return dict(next(v for v in rule.variants if v["id"] == vid))


def _assert_equivalent(data, rule, vid="V1"):
    v = _variant(rule, vid)
    full = v2.evaluate_variant_v2(data, rule, v)
    fast = v2.evaluate_variant_v2(data, rule, v, short_circuit=True)
    for k in full:
        if k == "robustness_books_computed":
            continue
        if not fast.get("robustness_books_computed", True) and k in SKIPPED:
            continue
        assert _same(full[k], fast[k]), k
    dfull = v2.decide_v2([full], rule)[0]
    dfast = v2.decide_v2([fast], rule)[0]
    assert (dfull["outcome"], dfull["reason"]) == (dfast["outcome"], dfast["reason"])
    for c in ("C1", "C2", "C3", "C4", "C8"):
        assert dfull["criteria"][c] == dfast["criteria"][c]
    return full, fast


def test_decision_map_ignores_c5_to_c8_unless_c1_c2_c3_c4_hold():
    """Exhaustive over all 2^8 criteria vectors: the skipped books feed only C6/C7."""
    for bits in itertools.product((False, True), repeat=8):
        c = dict(zip(CRITERIA, bits, strict=True))
        settled = not (c["C1"] and c["C2"] and c["C4"] and c["C3"])
        if not settled:
            continue
        base = rs.outcome(c)
        for alt in itertools.product((False, True), repeat=2):
            c2 = dict(c, C6=alt[0], C7=alt[1])
            assert rs.outcome(c2) == base


def test_threshold_boundaries_do_not_change_the_decision():
    """Thresholds set exactly at, and an epsilon either side of, the observed C2/C3/C4 statistics.

    Statistics do not depend on the thresholds, so one full evaluation is judged under each rule: if the
    short-circuit condition fires under that rule, the decision with C6/C7 unknown must equal the full one.
    """
    data = dr.make_data("revert", 60.0, 20269401, days=1)
    rule = _rule()
    stats = v2.evaluate_variant_v2(data, rule, _variant(rule))
    net = stats["net_bps"]
    probes = {
        "C2_mde_max_bps": net["mde"],
        "C3_net_min_bps": net["mean"],
        "C3_lower_min_bps": net["lower"],
        "C4_ess_min": net["ess"],
    }
    eps = 1e-9
    n_fire = 0
    for key, centre in probes.items():
        for delta in (-eps, 0.0, eps):
            r = copy.deepcopy(rule)
            r.doc["thresholds"][key] = centre + delta
            early = v2.criteria_v2(stats, r)
            fires = not (early["C1"] and early["C2"] and early["C4"] and early["C3"])
            n_fire += fires
            if fires:
                blind = dict(stats, **{k: float("nan") for k in SKIPPED})
                dfull = v2.decide_v2([stats], r)[0]
                dblind = v2.decide_v2([blind], r)[0]
                assert (dfull["outcome"], dfull["reason"]) == (dblind["outcome"], dblind["reason"])
                for c in ("C1", "C2", "C3", "C4", "C8"):
                    assert dfull["criteria"][c] == dblind["criteria"][c]
    assert n_fire > 0


@pytest.mark.parametrize(
    "book",
    [
        {
            "alpha_bps": 5.0,
            "risk_aversion": 1000.0,
        },  # the v1 setting: the optimiser holds (flat book)
        {"alpha_bps": 20.0, "risk_aversion": 100.0, "turnover_cap": 0.01},  # trading cap binds
    ],
)
def test_solver_fallback_cases_are_equivalent(book):
    data = dr.make_data("null", 0.0, 20269402, days=1)
    _assert_equivalent(data, _rule(book))


def test_end_to_end_equivalent_on_planted_edge():
    """A planted effect large enough that C3 can pass: the books behind C6/C7 are then computed and equal."""
    data = dr.make_data("revert", 60.0, 20269403, days=1)
    full, fast = _assert_equivalent(data, _rule(), "V5")
    if fast["robustness_books_computed"]:
        for k in SKIPPED:
            assert _same(full[k], fast[k])
