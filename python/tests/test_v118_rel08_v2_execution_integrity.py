"""REL-08 v2 calibration execution integrity: freeze, seeds, resume/void/completed, checkpoint, result schema, audit.

Synthetic fixture records only; no screen is run and no data is read.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CAL = REPO / "research" / "v118" / "rel08" / "calibration"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"t_{name}", CAL / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ig = _load("integrity")
fz = _load("freeze")
rc = _load("run_calibration")
M = rc.manifest()
ID = "f" * 64
OLD_ID = "0" * 64
CRIT = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")


def _variant(vid: str, outcome: str = "underpowered", **crit):
    c = {k: False for k in CRIT}
    c.update(crit)
    return {"id": vid, "outcome": outcome, "reason": "fixture", "criteria": c}


def _rec(unit: str, seed: int, identity: str = ID, continues: int = 0, c3: int = 0):
    vs = []
    for i, vid in enumerate(ig.VARIANT_IDS):
        out = "continue" if i < continues else "underpowered"
        vs.append(_variant(vid, out, C3=i < c3, C1=True, C2=True, C4=True))
    return ig.stamp({"seed": seed, "variants": vs, "sec": 1.0}, identity, unit)


def _fill(out: Path, events: dict[str, int] | None = None, only: set[str] | None = None):
    """Write valid fixture records for every unit (or ``only``); ``events[unit]`` replications get one continue."""
    events = events or {}
    units = ig.expected_units(M)
    by_file: dict[str, list] = {}
    for u, spec in units.items():
        if only is not None and u not in only:
            continue
        for k, seed in enumerate(spec["seeds"]):
            by_file.setdefault(spec["file"], []).append(
                _rec(u, seed, continues=1 if k < events.get(u, 0) else 0)
            )
    for f, recs in by_file.items():
        (out / f).write_text(json.dumps(recs), encoding="utf-8")


# ------------------------------------------------------------------ seeds --


def test_seed_streams_disjoint_and_original_null_seeds_unchanged():
    r = fz.seed_report(M)
    assert r["disjoint"] and not r["overlaps"]
    assert r["original_null_seeds_unchanged"]
    assert r["power_seeds_start_at"] == 20268500
    assert [c["seed_first"] for c in M["null_cells"]] == [20261100 + 1000 * i for i in range(8)]


def test_overlap_is_detected():
    bad = copy.deepcopy(M)
    bad["power_cells"][1]["seed_first"] = 20268100  # the old, overlapping power range
    bad["power_cells"][1]["seed_last"] = 20268129
    assert not fz.seed_report(bad)["disjoint"]


# ----------------------------------------------------------------- freeze --


def test_frozen_files_and_manifest_match_the_working_tree():
    """The committed execution manifest binds the current content of every frozen file (environment excluded)."""
    doc = json.loads((CAL / "execution_manifest.json").read_text(encoding="utf-8"))
    parts = doc["identity_parts"]
    for rel, sha in parts["files"].items():
        assert fz._norm_sha(REPO / rel) == sha, f"{rel} changed after the freeze: re-freeze"
    assert set(parts["files"]) == set(fz.FROZEN_FILES)
    assert parts["calibration_manifest"] == json.loads(json.dumps(M))
    assert parts["gate"] == ig.GATE
    assert parts["null_definitions"] == fz.NULL_DEFINITIONS
    assert doc["identity_sha256"] == fz.identity_sha256(parts)
    assert doc["seed_disjointness"]["disjoint"]


def test_runner_refuses_a_checkout_that_differs_from_the_frozen_identity(tmp_path):
    doc = json.loads((CAL / "execution_manifest.json").read_text(encoding="utf-8"))
    doc["identity_sha256"] = "0" * 64
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SystemExit, match="REFUSED"):
        rc.frozen_identity(REPO, p)
    with pytest.raises(SystemExit, match="--freeze"):
        rc.main(["all", "--out", str(tmp_path / "o")])


# ------------------------------------------------------ resume / void / done --


def test_valid_records_verify_and_resume_counts(tmp_path):
    _fill(tmp_path, only={"N1S1"})
    v = ig.verify_outputs(tmp_path, M, ID)
    assert not v["problems"] and v["counts"]["N1S1"] == 200 and v["counts"]["N1S4"] == 0
    assert ig.guard_start(tmp_path, M, ID)["N1S1"] == 200  # resume allowed


@pytest.mark.parametrize("kind", ["hash", "identity", "range", "duplicate", "unstamped", "unit"])
def test_invalid_records_void_the_study(tmp_path, kind):
    _fill(tmp_path, only={"N1S1"})
    p = tmp_path / "cell_N1S1.json"
    recs = json.loads(p.read_text(encoding="utf-8"))
    if kind == "hash":
        recs[0]["variants"][0]["outcome"] = "continue"  # edited after stamping
    elif kind == "identity":
        recs[0] = _rec("N1S1", recs[0]["seed"], identity=OLD_ID)
    elif kind == "range":
        recs[0] = _rec("N1S1", 5)
    elif kind == "duplicate":
        recs.append(copy.deepcopy(recs[3]))
    elif kind == "unstamped":  # a replication of the discarded first launch: no identity, no hash
        recs[0] = {"seed": recs[0]["seed"], "variants": recs[0]["variants"], "sec": 858.0}
    elif kind == "unit":
        recs[0] = _rec("N2S1", recs[0]["seed"])
    p.write_text(json.dumps(recs), encoding="utf-8")
    assert ig.verify_outputs(tmp_path, M, ID)["problems"]
    with pytest.raises(SystemExit, match="VOID"):
        ig.guard_start(tmp_path, M, ID)
    assert ig.state(tmp_path) == "void"
    with pytest.raises(SystemExit, match="VOID"):
        ig.guard_start(tmp_path, M, ID)  # a void study is never resumed


def test_completed_study_cannot_be_rerun_and_finalize_needs_every_unit(tmp_path):
    _fill(tmp_path, only={"N1S1", "N1S4"})
    with pytest.raises(SystemExit, match="not complete"):
        ig.finalize(tmp_path, M, ID)
    assert ig.state(tmp_path) == "open"
    _fill(tmp_path)
    res = ig.finalize(tmp_path, M, ID)
    assert (
        res["complete"] and res["counts"]["total_valid"] == 1750 == res["counts"]["expected_total"]
    )
    done = json.loads((tmp_path / "COMPLETED.json").read_text(encoding="utf-8"))
    assert done["total_valid_replications"] == 1750 and "result.json" in done["file_sha256"]
    with pytest.raises(SystemExit, match="COMPLETED"):
        ig.guard_start(tmp_path, M, ID)
    with pytest.raises(SystemExit, match="REFUSED"):
        ig.finalize(tmp_path, M, ID)
    events = [
        json.loads(x)["event"] for x in (tmp_path / "audit_log.jsonl").read_text().splitlines()
    ]
    assert events[-1] == "completed"


def test_discarded_first_launch_replications_cannot_enter(tmp_path):
    """N1S1 seeds 20261100 and 20261101 were consumed by the stopped launch; their records have no identity."""
    old = [
        {"seed": 20261100, "variants": _rec("N1S1", 20261100)["variants"], "sec": 858.0},
        {"seed": 20261101, "variants": _rec("N1S1", 20261101)["variants"], "sec": 857.0},
    ]
    (tmp_path / "cell_N1S1.json").write_text(json.dumps(old), encoding="utf-8")
    assert len(ig.verify_outputs(tmp_path, M, ID)["problems"]) == 2
    with pytest.raises(SystemExit):
        ig.guard_start(tmp_path, M, ID)


# ------------------------------------------------------------- checkpoint --


def test_checkpoint_is_operational_only(tmp_path):
    # records carry outcomes the checkpoint must never surface
    _fill(tmp_path, events={"N1S1": 7, "N2S4": 3}, only={"N1S1", "N2S4"})
    doc = ig.checkpoint_operational(tmp_path, M, ID, free_ram_gb=5.0, started=0.0)
    assert tuple(doc) == ig.CHECKPOINT_KEYS
    text = (tmp_path / "checkpoint_100_operational.json").read_text(encoding="utf-8").lower()
    for banned in (
        "continue",
        "rate",
        "bound",
        "upper",
        "pass",
        "fail",
        "family",
        "events",
        "criteria",
        "outcome_",
    ):
        assert banned not in text, banned
    assert doc["progress"]["N1S1"] == 200 and doc["integrity"]["ok"]
    assert set(doc["resource"]) == {"free_ram_gb", "elapsed_s"}


# ----------------------------------------------------------------- result --


def test_result_separates_gate_from_information_and_applies_the_preregistered_rule(tmp_path):
    _fill(tmp_path)  # no continuation anywhere
    res = ig.final_result(tmp_path, M, ID)
    g = res["gate"]
    assert g["rule"] == ig.GATE and g["verdict"] == "PASS"
    assert set(g) >= {
        "A_each_gating_cell_family_upper_le_5pct",
        "C_pooled_family_point_le_5pct",
        "verdict",
    }
    info = res["information_only"]
    assert info["bonferroni_level"] == pytest.approx(1 - 0.05 / 6)
    assert set(info["cells"]) == {c["cell"] for c in M["null_cells"]}
    cell = info["cells"]["N1S1"]
    assert cell["family_upper_bonferroni_info_only"] > cell["family_upper_95"]
    assert {"C1", "C2", "C3", "C4"} <= set(cell["criterion_pass_frequency"])
    assert "all_four_c1_c2_c3_c4_pass_frequency" in cell
    assert any("rarely permits trading" in s for s in info["statements"])
    assert any("C5, C6 and C7 are not measured" in s for s in info["statements"])
    assert "verdict" not in info and "gate" not in info


def test_gate_arithmetic_at_the_boundary(tmp_path):
    assert ig.cp_upper(4, 200) <= 0.05 < ig.cp_upper(5, 200)  # at most 4 family events in 200
    _fill(tmp_path, events={"N1S1": 4})
    assert ig.final_result(tmp_path, M, ID)["gate"]["verdict"] == "PASS"
    _fill(tmp_path, events={"N1S1": 5})
    res = ig.final_result(tmp_path, M, ID)
    assert res["gate"]["verdict"] == "FAIL"
    assert res["gate"]["A_each_gating_cell_family_upper_le_5pct"]["N1S1"] is False
    # N4 is non-gating: many events there never change the verdict
    _fill(tmp_path, events={"N4S1": 100})
    assert ig.final_result(tmp_path, M, ID)["gate"]["verdict"] == "PASS"


def test_incomplete_run_has_no_verdict(tmp_path):
    _fill(tmp_path, only={"N1S1"})
    r = ig.final_result(tmp_path, M, ID)
    assert not r["complete"] and r["gate"]["verdict"] == "INCOMPLETE"
