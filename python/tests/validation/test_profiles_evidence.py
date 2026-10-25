"""Confirmation profiles PF-1 / PF-2: the committed synthetic evidence (slow).

Recomputes a declared subset of cells of each profile from the committed seeds (the iid
normal size cells and the first power cell) and asserts the counts equal the committed
``research/v2/confirmation_evidence/<profile>_RESULTS.json``; checks every committed cell's
result against the acceptance thresholds; and checks that the operating domain written in
the file is what ``iap.validation.profiles.operating_domain`` recomputes from its cells.
The full re-run is ``python/tests/spec/pf_suite.py --check`` (about an hour on one core).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from iap.validation import profiles as pf

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "python" / "tests" / "spec"))
import pf_generators as gen  # noqa: E402
import pf_suite as suite  # noqa: E402

RECOMPUTE = {"PF-1": (("size", 0), ("size", 1), ("power", 0)), "PF-2": (("size", 0), ("power", 0))}


@pytest.fixture(scope="module", params=["PF-1", "PF-2"])
def committed(request):
    path = pf.evidence_path(request.param, REPO)
    if not path.is_file():
        pytest.skip(f"{request.param}: no committed evidence")
    return request.param, json.loads(path.read_text(encoding="utf-8"))


def test_every_committed_cell_result_follows_the_thresholds(committed):
    pid, doc = committed
    assert doc["n_size"] == 20000 and doc["size_pass_k"] == 463
    assert doc["n_power"] == 5000 and doc["power_pass_detections"] == 4047
    for cell in doc["cells"]:
        for name, r in cell["procedures"].items():
            if cell["kind"] == "size":
                assert cell["n"] == 20000
                assert r["result"] == ("pass" if r["k"] <= 463 else "fail"), (cell["id"], name)
                assert (r["upper"] <= 0.025) == (r["result"] == "pass")
            else:
                assert cell["n"] == 5000
                if cell["generator"] == "iid_normal":
                    assert r["result"] == ("pass" if r["k"] >= 4047 else "fail")
                else:
                    assert r["result"] == "reported"


def test_reference_cells_pass_for_at_least_one_procedure(committed):
    pid, doc = committed
    dom = pf.operating_domain(pid, REPO)
    assert dom is not None
    assert any(d["reference_passes"] for d in dom.values()), (
        f"{pid}: no procedure passes the iid normal cells"
    )
    for proc, d in dom.items():
        if d["reference_passes"]:
            for cell in doc["cells"]:
                if cell["kind"] == "power" and cell["generator"] == "iid_normal":
                    assert cell["procedures"][proc]["result"] == "pass", (pid, proc, cell["id"])


def test_operating_domain_in_the_file_is_recomputed_from_its_cells(committed):
    pid, doc = committed
    dom = pf.operating_domain(pid, REPO)
    written = doc["operating_domain"]
    for proc in dom:
        assert [list(g) for g in dom[proc]["generators"]] == [
            list(g) for g in written[proc]["generators"]
        ]
        assert dom[proc]["reference_passes"] == written[proc]["reference_passes"]


def test_recomputed_counts_equal_the_committed_evidence(committed):
    pid, doc = committed
    prof = pf.PROFILES[pid]
    by = {(c["kind"], c["index"]): c for c in doc["cells"]}
    cells = gen.cells_for(prof, planted=lambda T: pf.planted_effect(T, gen.K))
    for kind, idx in RECOMPUTE[pid]:
        cell = next(c for c in cells if c.kind == kind and c.index == idx)
        r = suite.run_cell(cell, prof)
        for name, v in r["procedures"].items():
            assert v["k"] == by[(kind, idx)]["procedures"][name]["k"], (cell.id, name)
