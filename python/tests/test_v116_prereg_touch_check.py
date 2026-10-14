"""v1.16 item 12 (b): warning when code an open pre-registration fingerprints changed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from iap.agents.blackboard import Blackboard
from iap.agents.fingerprint import fingerprint

TOOL = Path(__file__).resolve().parents[2] / "tools" / "prereg_touch_check.py"
spec = importlib.util.spec_from_file_location("prereg_touch_check", TOOL)
ptc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ptc)


def _board(tmp_path: Path, code_hash: str, closed: bool) -> Path:
    path = tmp_path / "blackboard.jsonl"
    bb = Blackboard(path)
    fp = fingerprint("EQ10")
    code = {"code_hash": code_hash, "feature_hash": fp["feature_hash"]}
    body = {"alpha_id": "EQ10", "horizon": "1s:t", "prereg_id": "p1", "code": code}
    bb.append("prereg", "owner", 1, body)
    if closed:
        bb.append("outcome", "outcomes", 2, {"outcome": "confirmed", "prereg_id": "p1"})
    return path


def test_unchanged_code_passes(tmp_path, capsys):
    path = _board(tmp_path, fingerprint("EQ10")["code_hash"], closed=False)
    assert ptc.open_mismatches(path) == []
    assert ptc.main(["--board", str(path), "--strict"]) == 0


def test_changed_code_warns_but_does_not_fail(tmp_path, capsys):
    path = _board(tmp_path, "0" * 64, closed=False)
    (m,) = ptc.open_mismatches(path)
    assert m["changed"] == ["code_hash"]
    assert ptc.main(["--board", str(path)]) == 0
    assert "::warning::" in capsys.readouterr().out
    assert ptc.main(["--board", str(path), "--strict"]) == 1


def test_closed_prereg_is_ignored(tmp_path):
    path = _board(tmp_path, "0" * 64, closed=True)
    assert ptc.open_mismatches(path) == []
