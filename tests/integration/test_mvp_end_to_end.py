"""Integration level: the MVP command line end to end.

Runs ``python -m iap.mvp run`` (the golden configuration) as a subprocess
into a temporary directory, checks that every artefact of a run exists and
that ``report.json`` carries the documented sections, then ``explain``s a
parent order from the run's SQLite store and ``replay``s the captured
stream — the operator flow of docs/MVP.md, driven exactly as an operator
would drive it.
"""
from __future__ import annotations

import json
import subprocess
import sys

REPORT_SECTIONS = ("run", "counts", "risk", "routing", "controls", "pnl", "alpha",
                   "execution", "portfolio", "trace")
ARTEFACTS = ("config.json", "events.jsonl", "events.iap1", "feed.json", "traces.jsonl",
             "iap.sqlite", "risk_audit.jsonl", "report.json", "report.md",
             "paper_evidence.json")


def _mvp(repo_root, *args):
    env = {"PYTHONPATH": str(repo_root / "python" / "src"), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-m", "iap.mvp", *args], cwd=repo_root,
                          capture_output=True, text=True, env=env, check=False)


def test_cli_run_explain_replay(repo_root, tmp_path):
    out = tmp_path / "run"
    run = _mvp(repo_root, "run", "--out", str(out))
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("mvp run ")
    for name in ARTEFACTS:
        assert (out / name).is_file(), name

    report = json.loads((out / "report.json").read_text())
    assert report["x-version"] == 2
    for section in REPORT_SECTIONS:
        assert section in report, section
    assert report["run"]["instrument"] == "SYN.EQ.AAPL"
    assert report["counts"]["n_parent_orders"] > 0
    assert report["risk"]["decisions_by_rule"]
    assert set(report["routing"]["venue_shares"]) == {"XV1", "XV2", "XV3"}
    assert len(report["trace"]["digest"]) == 64
    assert report["trace"]["n_traces"] == report["counts"]["n_decisions"]

    explain = _mvp(repo_root, "explain", "--run", str(out), "1")
    assert explain.returncode == 0, explain.stderr
    lines = explain.stdout.splitlines()
    assert lines[0] == "Order 1"
    assert any(line.startswith("Risk:") for line in lines)
    assert any(line.startswith("SOR:") for line in lines)
    assert any(line.startswith("TCA:") for line in lines)

    unknown = _mvp(repo_root, "explain", "--run", str(out), "999999")
    assert unknown.returncode == 2 and "error:" in unknown.stderr

    replay = _mvp(repo_root, "replay", "--run", str(out))
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout.startswith("replay OK")


def test_cli_rejects_a_bad_config(repo_root, tmp_path):
    bad = tmp_path / "bad.json"
    doc = json.loads((repo_root / "configs" / "mvp" / "mvp_tiny.json").read_text())
    doc["portfolio"]["risk_aversion"] = -1.0
    bad.write_text(json.dumps(doc))
    res = _mvp(repo_root, "run", "--config", str(bad), "--out", str(tmp_path / "out"))
    assert res.returncode == 2
    assert "risk_aversion" in res.stderr
