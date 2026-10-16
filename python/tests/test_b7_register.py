"""v1.17.1 B7: the shortfall-gate registration tool refuses an incomplete
draft and dry-runs a complete one without writing anything."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import b7_shortfall_register as reg  # noqa: E402


def _root(tmp_path: Path, **patch) -> Path:
    root = tmp_path / "repo"
    (root / "research/execution/benchmark").mkdir(parents=True)
    (root / "research/preregs").mkdir(parents=True)
    (root / "configs/prereg").mkdir(parents=True)
    shutil.copy(REPO / "configs/prereg/v2.json", root / "configs/prereg/v2.json")
    doc = json.loads((REPO / reg.DRAFT).read_text(encoding="utf-8"))
    doc.update(patch)
    (root / reg.DRAFT).write_text(json.dumps(doc), encoding="utf-8")
    return root


def _complete(doc: dict) -> dict:
    doc = dict(doc)
    doc["expected_effect"] = "in-sample: urgency -0.3 bps [..], pacing +0.1 bps [..]"
    doc["signal_arm"] = {
        **doc["signal_arm"],
        "alpha_id": "EQ10",
        "thresholds": "threshold_z 1.0, passive_urgency 0.1, gain 0.5",
        "alpha": "EQ10 (owner decision 2026-10-xx)",
        "thresholds_status": "decided",
    }
    doc["simulator"] = {**doc["simulator"], "walk_depth_levels": 50}
    doc["fresh_test"] = {
        "command": "python -m iap.research.execution_benchmark ...",
        "sessions": [
            {
                "day": "2025-11-28",
                "nasdaq_dataset": "r7_20251128",
                "nasdaq_dataset_version": "a" * 64,
                "iex_dataset": "iex/iex_20251128",
                "iex_dataset_version": "b" * 64,
            }
        ],
    }
    doc["fingerprints"] = {
        reg.V2: hashlib.sha256((REPO / reg.V2).read_bytes()).hexdigest(),
    }
    doc["signed"] = False
    doc["status"] = "DRAFT - complete, awaiting the owner's signature"
    return doc


def test_committed_draft_is_unsigned_and_the_tool_refuses_it(tmp_path):
    doc = json.loads((REPO / reg.DRAFT).read_text(encoding="utf-8"))
    assert doc["signed"] is False and "DRAFT" in doc["status"]
    root = _root(tmp_path)
    with pytest.raises(SystemExit, match="incomplete"):
        reg.checks(root)


@pytest.mark.parametrize(
    "field, value, needle",
    [
        ("expected_effect", "to be filled", "expected_effect"),
        ("simulator", {"walk_depth_levels": 10}, "walk_depth_levels"),
        ("fresh_test", {"command": "x", "sessions": []}, "sessions is empty"),
        ("fingerprints", {reg.V2: "0" * 64}, "sha256"),
    ],
)
def test_each_missing_piece_is_named(tmp_path, field, value, needle):
    doc = _complete(json.loads((REPO / reg.DRAFT).read_text(encoding="utf-8")))
    doc[field] = value
    root = _root(tmp_path, **doc)
    with pytest.raises(SystemExit, match=needle):
        reg.checks(root)


def test_open_signal_arm_fields_block(tmp_path):
    doc = _complete(json.loads((REPO / reg.DRAFT).read_text(encoding="utf-8")))
    doc["signal_arm"]["alpha_id"] = "OPEN (owner)"
    with pytest.raises(SystemExit, match="alpha_id"):
        reg.checks(_root(tmp_path, **doc))


def test_dry_run_on_a_complete_draft_writes_nothing(tmp_path, capsys):
    doc = _complete(json.loads((REPO / reg.DRAFT).read_text(encoding="utf-8")))
    root = _root(tmp_path, **doc)
    assert reg.main([str(root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run] would call WriteBroker.preregister(EQ10/shortfall:b7-signal-aware" in out
    assert not (root / reg.OUT).exists()
    assert json.loads((root / reg.DRAFT).read_text())["signed"] is False


def test_existing_record_blocks(tmp_path):
    doc = _complete(json.loads((REPO / reg.DRAFT).read_text(encoding="utf-8")))
    root = _root(tmp_path, **doc)
    (root / reg.OUT).write_text("{}")
    with pytest.raises(SystemExit, match="already exists"):
        reg.checks(root)
