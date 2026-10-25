"""R3 adversarial Monte Carlo of the frozen R0 specification (section 5), slow.

Runs every cell of ``python/tests/spec/r3_generators.py`` through the canonical gate
(``python/tests/spec/r3_suite.py``) and asserts the acceptance rules exactly as R0
states them: every claimed-domain size cell has k <= 463 of 20,000; the two power
cells have >= 4,047 detections of 5,000; the candidate-domain consequence (D-BLK
withdrawn unless all 12 cells pass) equals what ``confirmatory.dblk_validated`` reads
from the committed ``research/validation/R0_RESULTS.json``; stress cells never fail;
out-of-domain declarations are refused by the gate; and the recomputed counts equal
the committed evidence (deterministic given the seeds).  About two minutes on one
core.  The deterministic refusal cases are ``test_confirmatory_refusals.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from iap.validation import confirmatory as c

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "tests" / "spec"))
import r3_generators as gen  # noqa: E402
import r3_suite as suite  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def results() -> dict:
    return suite.run_all(verbose=False)


def test_every_claimed_domain_size_cell_passes(results):
    claimed = [
        r for r in results["cells"] if r["domain_class"] == "claimed" and r["kind"] == "size"
    ]
    assert len(claimed) == 29
    failing = [(r["id"], r["k"]) for r in claimed if r["k"] > gen.SIZE_PASS_K]
    assert failing == [], f"a D-IND cell fails: R1 is wrong, reopen R1 (R0 section 5): {failing}"
    assert all(r["gate_outcome"] == "admitted" for r in claimed)


def test_both_power_cells_pass(results):
    power = [r for r in results["cells"] if r["kind"] == "power"]
    assert len(power) == 2 and all(r["detections"] >= gen.POWER_PASS_DETECTIONS for r in power)
    for r in power:
        assert r["planted"]["theoretical_power"] == 0.90 and r["lower95"] >= 0.80
        # the planted effect names the hypothesis, the effect in sd units and the power
        assert r["planted"]["hypothesis"] == "H00" and r["planted"]["effect_sd_units"] > 0


def test_candidate_domain_consequence_is_applied_mechanically(results):
    candidate = [r for r in results["cells"] if r["domain_class"] == "candidate"]
    assert len(candidate) == 12
    all_pass = all(r["k"] <= gen.SIZE_PASS_K for r in candidate)
    assert results["summary"]["dblk_validated"] is all_pass
    # the gate reads the committed file: the withdrawal is what the data says
    committed = json.loads((REPO / c.RESULTS_REL).read_text(encoding="utf-8"))
    assert committed["summary"]["dblk_validated"] is all_pass
    assert c.dblk_validated(REPO) is all_pass
    while_withdrawn = "refused: dependence assumptions unsupported (D-BLK withdrawn for v1.19)"
    for r in candidate:
        assert r["gate_outcome"] == ("admitted" if all_pass else while_withdrawn)


def test_stress_cells_are_reported_and_refused_by_the_gate(results):
    stress = [r for r in results["cells"] if r["domain_class"] == "stress"]
    assert len(stress) == 23 and all(r["result"] == "reported" for r in stress)
    for r in stress:
        assert r["gate_outcome"].startswith("refused: "), r["id"]
    # the inflation R0 asks to document: serial correlation under P-IND grows with rho
    by_rho = {}
    for r in stress:
        if r["procedure"] == "P-IND" and r["generator"] == "ar1_gaussian_stationary":
            by_rho.setdefault(r["params"]["rho"], []).append(r["k"])
    assert min(by_rho[0.1]) > gen.SIZE_PASS_K  # even rho = 0.1 inflates past the line
    assert min(by_rho[0.5]) > max(by_rho[0.3]) > max(by_rho[0.2]) > max(by_rho[0.1])


def test_selection_cells(results):
    r = next(r for r in results["cells"] if r["case"] == "R3-D")
    assert r["result"] == "pass" and r["K"] == 3
    assert r["contaminated"]["k"] > 4 * r["k"]  # inflation recorded
    assert (
        r["contaminated"]["gate_outcome"]
        == "refused: looks on confirmation data by the same family"
    )
    assert r["spent_window"]["gate_outcome"] == "refused: confirmation window spent"


def test_recomputed_counts_equal_the_committed_evidence(results):
    diffs = suite.compare(results, REPO / c.RESULTS_REL)
    assert diffs == [], diffs
    committed = json.loads((REPO / c.RESULTS_REL).read_text(encoding="utf-8"))
    assert committed["thresholds"]["size"]["k_max"] == 463
    assert committed["thresholds"]["power"]["detections_min"] == 4047
    assert committed["thresholds"]["seed_rule"] == "20301000 + 100 * case_index + cell_index"
