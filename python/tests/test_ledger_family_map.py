"""v1.19 R3 (defect D-08): the ledger family dependency map and the selection history
``prereg_gate`` attaches from it.

The committed ``research/validation/LEDGER_FAMILY_MAP.json`` rebuilds from the committed
central ledger, the committed inventory and the holdout registry (no external file read);
it enumerates the 13 external entries / 1,224 real looks the central ledger does not hold,
one by one; dependent families share a real dataset version; ``prereg_gate.require``
attaches the history and refuses a prereg bound to another map.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from iap.agents.broker import WriteBroker
from iap.agents.prereg_gate import (
    FAMILY_MAP_REL,
    PreregistrationError,
    require,
    selection_history,
)

REPO = Path(__file__).resolve().parents[2]
MAP = REPO / FAMILY_MAP_REL
_spec = importlib.util.spec_from_file_location(
    "ledger_family_map", REPO / "tools" / "ledger_family_map.py"
)
lfm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lfm)
SEC = 1_000_000_000


class Clock:
    def __init__(self) -> None:
        self.t = 1_000 * SEC

    def __call__(self) -> int:
        self.t += SEC
        return self.t


@pytest.fixture(scope="module")
def committed() -> dict:
    return json.loads(MAP.read_text(encoding="utf-8"))


def test_committed_map_rebuilds_from_the_committed_sources(committed):
    rebuilt = lfm.build()
    assert lfm._strip_volatile(rebuilt) == lfm._strip_volatile(committed)
    assert committed["schema"] == "ledger_family_map/1"
    assert (
        committed["sources"]["central_ledger"]["sha256"]
        == hashlib.sha256((REPO / "research" / "experiments.json").read_bytes()).hexdigest()
    )


def test_the_1224_unmerged_external_looks_are_enumerated_not_merged(committed):
    u = committed["unmerged_external_looks"]
    assert u["total"] == 1224 and u["entries"] == 13 and "not merged" in u["status"]
    ext = [row for f in committed["families"].values() for row in f["external_unmerged"]["ledgers"]]
    assert len(ext) == 13 and sum(r["looks"] for r in ext) == 1224
    assert len({r["key"] for r in ext}) == 13
    assert all(r["path"].endswith("multi7/research/alpha_reports/experiments.json") for r in ext)
    # the central ledger is untouched: the chain head of the committed file is the one recorded
    central = json.loads((REPO / "research" / "experiments.json").read_text(encoding="utf-8"))
    assert committed["sources"]["central_ledger"]["chain_head"] == central["chain_head"]
    assert committed["sources"]["central_ledger"]["looks"] == central["total_experiments"] == 6826


def test_family_rows_add_up(committed):
    fams = committed["families"]
    eq01 = fams["EQ01"]
    assert eq01["external_unmerged"]["looks"] == 84
    assert eq01["real_looks_lower_bound"] == eq01["central"]["real"]["looks"] + 84
    assert fams["ALL"]["external_unmerged"]["looks"] == 216  # the design horizon scan
    real_total = sum(f["central"]["real"]["looks"] for f in fams.values())
    syn_total = sum(f["central"]["synthetic"]["looks"] for f in fams.values())
    unk_total = sum(f["central"]["unknown"]["looks"] for f in fams.values())
    assert real_total == 1132 and real_total + syn_total + unk_total == 6826
    for a, f in fams.items():
        assert a not in f["dependent_families"]
        for b in f["dependent_families"]:
            assert a in fams[b]["dependent_families"]  # symmetric
            assert set(f["real_dataset_versions"]) & set(fams[b]["real_dataset_versions"])


def test_eq_families_on_multi7_are_mutually_dependent(committed):
    fams = committed["families"]
    eqs = {f"EQ{i:02d}" for i in range(1, 13)}
    for a in eqs:
        assert eqs - {a} <= set(fams[a]["dependent_families"]), a


def test_selection_history_fields_and_unknown_family():
    h = selection_history(REPO, "EQ01")
    assert h["known"] is True and h["external_unmerged_looks"] == 84
    assert h["real_looks_lower_bound"] == h["central_real_looks"] + h["external_unmerged_looks"]
    assert "EQ02" in h["dependent_families"] and h["dependent_real_looks_lower_bound"] > 0
    assert h["map_sha256"] == hashlib.sha256(MAP.read_bytes()).hexdigest()
    new = selection_history(REPO, "H1-NEW")
    assert (
        new["known"] is False
        and new["real_looks_lower_bound"] == 0
        and new["dependent_families"] == []
    )
    assert new["map_sha256"] == h["map_sha256"]


def test_selection_history_refuses_without_a_map(tmp_path):
    with pytest.raises(PreregistrationError, match="ledger family map missing"):
        selection_history(tmp_path, "EQ01")


def _root(tmp_path: Path) -> Path:
    exp = tmp_path / "research" / "experiments" / "abc123"
    exp.mkdir(parents=True)
    (exp / "spec.json").write_text('{"alpha_id": "EQ01"}', encoding="ascii")
    (exp / "result.json").write_text('{"verdict": "ITERATE"}', encoding="ascii")
    return tmp_path


def test_require_attaches_the_selection_history_when_the_map_is_present(tmp_path):
    root = _root(tmp_path)
    b = WriteBroker(root, {"alice"}, clock=Clock())
    b.preregister("alice", "EQ03", "1s", "h", 1)
    assert "selection_history" not in require(root, "EQ03", "1s")  # no map in this repo
    dest = root / FAMILY_MAP_REL
    dest.parent.mkdir(parents=True)
    dest.write_bytes(MAP.read_bytes())
    e = require(root, "EQ03", "1s")
    assert e["kind"] == "prereg" and e["selection_history"]["family_id"] == "EQ03"
    assert e["selection_history"]["known"] is True
    assert e["selection_history"]["map_sha256"] == hashlib.sha256(MAP.read_bytes()).hexdigest()


def test_require_refuses_a_prereg_bound_to_another_map(tmp_path):
    root = _root(tmp_path)
    b = WriteBroker(root, {"alice"}, clock=Clock())
    b.preregister("alice", "EQ03", "1s", "h", 1)
    board = b.board.path
    text = board.read_text(encoding="ascii")
    # a prereg that binds a map hash: rewrite the body and re-chain the board through the
    # broker's own writer would be the honest path; here the bound hash is injected through
    # a second, properly written entry
    assert '"ledger_family_map_sha256"' not in text
    from iap.agents.blackboard import Blackboard

    bb = Blackboard(board)
    clock = Clock()
    clock.t = int(bb.entries()[-1]["ts"])  # the board's ts must not decrease
    body = dict(bb.entries()[-1]["body"])
    body["ledger_family_map_sha256"] = "0" * 64
    bb.append("prereg", "alice", clock(), body)
    dest = root / FAMILY_MAP_REL
    dest.parent.mkdir(parents=True)
    dest.write_bytes(MAP.read_bytes())
    with pytest.raises(PreregistrationError, match="binds ledger family map"):
        require(root, "EQ03", "1s")
    body["ledger_family_map_sha256"] = hashlib.sha256(MAP.read_bytes()).hexdigest()
    bb.append("prereg", "alice", clock(), body)
    assert require(root, "EQ03", "1s")["selection_history"]["known"] is True
