"""The machine-readable calibration manifest must equal what the runner would execute (totals reconciled)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CAL = REPO / "research" / "v118" / "rel08" / "calibration" / "run_calibration.py"
MANIFEST = REPO / "research" / "v118" / "rel08" / "REL08_V2_CALIBRATION_MANIFEST.json"

_spec = importlib.util.spec_from_file_location("run_calibration", CAL)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def test_manifest_file_equals_runner_manifest():
    assert json.loads(MANIFEST.read_text(encoding="utf-8")) == json.loads(json.dumps(rc.manifest()))


def test_totals_reconcile_with_cells_and_the_plan():
    m = rc.manifest()
    t = m["totals"]
    assert t["gating_screens"] == 6 * 200 == 1200
    assert t["null_screens"] == 8 * 200 == 1600
    assert t["power_screens"] == 5 * 30 == 150
    assert t["all_screens"] == 1750
    assert sum(c["replications"] for c in m["null_cells"]) == t["null_screens"]
    assert [c["cell"] for c in m["null_cells"] if c["role"] == "gating"] == [
        "N1S1", "N1S4", "N2S1", "N2S4", "N3S1", "N3S4",
    ]  # fmt: skip
    assert m["sensitivity_cells"] == []


def test_seed_ranges_are_disjoint_and_outside_the_timing_and_equivalence_seeds():
    m = rc.manifest()
    spans = [(c["seed_first"], c["seed_last"]) for c in m["null_cells"]] + [
        (p["seed_first"], p["seed_last"]) for p in m["power_cells"]
    ]
    spans.sort()
    for (_, hi), (lo, _) in zip(spans, spans[1:], strict=False):
        assert hi < lo
    for s in m["timing_seeds"] + [20269300, 20269330, 20269401, 20269403]:
        assert all(not (lo <= s <= hi) for lo, hi in spans)


def test_checkpoint_is_the_first_100_of_each_gating_cell_only():
    m = rc.manifest()
    for c in m["null_cells"]:
        if c["role"] == "gating":
            assert c["checkpoint_replications_non_decisional"] == 100 < c["replications"]
        else:
            assert c["checkpoint_replications_non_decisional"] is None
