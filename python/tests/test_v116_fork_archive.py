"""v1.16 item 11: research/maker-real and research/step2 fork boards archived, not replayed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from iap.agents.blackboard import Blackboard
from iap.agents.prereg_gate import PreregistrationError, require
from iap.research import auction_real, maker_real, quoting_real

ROOT = Path(__file__).resolve().parents[2]
FORKS = ROOT / "research" / "agents" / "forks"


@pytest.mark.parametrize("fork", ["maker-real", "step2"])
def test_fork_archive_verifies_and_matches_its_metadata(fork):
    d = FORKS / fork
    meta = json.loads((d / "FORK.json").read_text(encoding="utf-8"))
    bb = Blackboard(d / "blackboard.jsonl")
    assert bb.verify() == meta["entries"]
    assert bb.head() == meta["head_hash"]
    for name, sha in meta["files"].items():
        assert hashlib.sha256((d / name).read_bytes()).hexdigest() == sha
    keys = d / "agent_pubkeys.json"
    if keys.exists():
        assert bb.verify_signatures(json.loads(keys.read_text(encoding="utf-8"))) == []


def test_archived_preregs_resolve_on_their_fork_board_only():
    maker = json.loads((ROOT / "research/maker_real/prereg.json").read_text(encoding="utf-8"))
    assert len(maker_real.check_prereg(ROOT, maker)) == 4
    with pytest.raises(PreregistrationError):
        require(ROOT, "EQ10", "1s:maker-insample")  # not on main's board
    assert auction_real.PREREG_BOARD.is_file()
    quoting = (ROOT / "research/quoting_real/prereg.json").read_bytes()
    sha = hashlib.sha256(quoting).hexdigest()
    assert len(quoting_real.check_prereg(ROOT, json.loads(quoting), sha)) == 4


def test_main_board_records_the_import():
    entries = Blackboard(ROOT / "research/agents/blackboard.jsonl").entries()
    found = [
        e
        for e in entries
        if e["kind"] == "finding" and "Fork boards archived" in e["body"]["title"]
    ]
    assert len(found) == 1
