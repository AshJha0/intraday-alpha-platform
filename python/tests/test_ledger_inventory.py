"""Ledger inventory (v1.18 review finding 4): deterministic, finds unmerged ledgers, read-only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "ledger_inventory", REPO / "tools" / "ledger_inventory.py"
)
li = importlib.util.module_from_spec(spec)
spec.loader.exec_module(li)


def _central_entry(n: int = 0) -> dict:
    return json.loads((REPO / "research" / "experiments.json").read_text(encoding="utf-8"))[
        "entries"
    ][n]


def _data_root(tmp_path: Path) -> Path:
    root = tmp_path / "real"
    merged = root / "a" / "research" / "experiments.json"
    unmerged = root / "b" / "research" / "alpha_reports" / "experiments.json"
    merged.parent.mkdir(parents=True)
    unmerged.parent.mkdir(parents=True)
    merged.write_text(json.dumps({"entries": [_central_entry()]}), encoding="utf-8")
    extra = {
        "alpha_id": "ZZ99",
        "kind": "promotion_pipeline",
        "config": {"x": 1},
        "count": 7,
        "dataset_version": "not-a-registered-version",
    }
    unmerged.write_text(json.dumps({"entries": [extra]}), encoding="utf-8")
    (root / "b" / "research" / "experiments_notes.json").write_text(
        "[]", encoding="utf-8"
    )  # not a ledger
    return root


def test_inventory_is_deterministic(tmp_path):
    root = _data_root(tmp_path)
    outs = []
    for d in ("o1", "o2"):
        assert li.main(["--data-root", str(root), "--out-dir", str(tmp_path / d)]) == 0
        outs.append({p.name: p.read_bytes() for p in sorted((tmp_path / d).iterdir())})
    assert outs[0] == outs[1] and set(outs[0]) == {"LEDGER_INVENTORY.json", "LEDGER_INVENTORY.md"}
    assert li.main(["--data-root", str(root), "--out-dir", str(tmp_path / "o1"), "--check"]) == 0
    assert str(tmp_path) not in outs[0]["LEDGER_INVENTORY.md"].decode()  # no absolute paths


def test_inventory_reports_relations_and_missing_looks(tmp_path):
    inv = li.inventory(_data_root(tmp_path))
    rel = {x["path"]: x for x in inv["external_ledgers"]}
    assert set(rel) == {
        "<data-root>/a/research/experiments.json",
        "<data-root>/b/research/alpha_reports/experiments.json",
    }
    assert rel["<data-root>/a/research/experiments.json"]["relation"] == "fully merged"
    b = rel["<data-root>/b/research/alpha_reports/experiments.json"]
    assert b["relation"] == "not merged" and b["missing_looks"] == 7
    assert b["missing_from_central"][0]["family"] == "unknown"  # never guessed
    assert inv["summary"]["external_looks_missing_from_central"] == 7
    assert inv["summary"]["chain_ok"] is True
    assert "## Coverage statement" in li.render_md(inv)


def test_inventory_does_not_touch_the_central_ledger(tmp_path):
    p = REPO / "research" / "experiments.json"
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    li.inventory(_data_root(tmp_path))
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before


def test_headline_numbers_research_section_not_broken():
    r = subprocess.run(
        [sys.executable, str(REPO / "tests" / "harness" / "check_headline_numbers.py")],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
