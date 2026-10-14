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
    # quoting v1 (aborted): its preregs pin a deps fingerprint, which the full
    # gate rechecks against the running environment, so look the entries up
    # on the archived board directly (the driver's gate reads the same board).
    quoting = (ROOT / "research/quoting_real/prereg.json").read_bytes()
    sha = hashlib.sha256(quoting).hexdigest()
    board = Blackboard(quoting_real.PREREG_BOARD_V1).entries()
    for cell in json.loads(quoting)["cells"]:
        hit = [
            e
            for e in board
            if e["kind"] == "prereg"
            and e["body"].get("alpha_id") == cell["id"]
            and e["body"].get("horizon") == f"{cell['horizon']}:quoting-insample"
        ]
        assert hit and sha in hit[-1]["body"]["hypothesis"]


def test_main_board_records_the_import():
    entries = Blackboard(ROOT / "research/agents/blackboard.jsonl").entries()
    found = [
        e
        for e in entries
        if e["kind"] == "finding" and "Fork boards archived" in e["body"]["title"]
    ]
    assert len(found) == 1
