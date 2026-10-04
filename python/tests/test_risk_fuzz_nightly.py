"""End-to-end test of the nightly differential-fuzz driver
(``python/tools/risk_fuzz_nightly.py``) with a stand-in engine.

The real engines of the nightly job are the Rust and Java golden replays,
which only exist in CI. The driver does not care what an engine is — only
that it speaks the replay protocol (``IAP_RISK_FUZZ_DIR`` in,
``IAP_RISK_FUZZ_OUT/<name>.tsv`` out) — so here the engine is the Python
replay in a subprocess, once as it is (no divergence, exit 0) and once with a
planted bug (divergence found, minimised, artifacts written, exit 1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import risk_fuzz as rf  # noqa: E402
import risk_fuzz_nightly as nightly  # noqa: E402

ENGINE = """
import os, sys
from pathlib import Path
sys.path.insert(0, {tools!r})
import risk_fuzz as rf
from iap.risk import RiskEngine
if {plant!r}:
    # the planted bug: PEG orders are tracked unpriced (self-match and gross
    # then treat them like MARKET orders)
    RiskEngine._tracked_price = lambda self, order: max(order.price_ticks, 0)
rf.replay_report(Path(os.environ["IAP_RISK_FUZZ_DIR"]), Path(os.environ["IAP_RISK_FUZZ_OUT"]), "python")
"""


def _engine(tmp_path: Path, plant: bool) -> list[str]:
    script = tmp_path / ("engine_bug.py" if plant else "engine.py")
    script.write_text(ENGINE.format(tools=str(TOOLS), plant=plant), encoding="utf-8")
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    return ["--engine", "python", ".", command]


def test_nightly_run_without_divergence_exits_zero(tmp_path, capsys):
    code = nightly.main(
        ["--seed", "7", "--count", "13", "--work", str(tmp_path / "work")]
        + ["--artifacts", str(tmp_path / "artifacts")]
        + _engine(tmp_path, plant=False)
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "13 scripts" in out and "engine python: 13 OK, 0 diverged" in out
    assert "no divergence" in out
    assert not (tmp_path / "artifacts").exists()
    report = rf.read_report(tmp_path / "work" / "out" / "python.tsv")
    assert len(report) == 13 and not any(report.values())


def test_nightly_run_minimises_and_uploads_the_first_divergence(tmp_path, capsys):
    artifacts = tmp_path / "artifacts"
    code = nightly.main(
        ["--seed", "7", "--count", "26", "--work", str(tmp_path / "work")]
        + ["--artifacts", str(artifacts)]
        + _engine(tmp_path, plant=True)
    )
    out = capsys.readouterr().out
    assert code == 1, out
    assert "DIVERGED" in out and "minimised" in out
    summary = (artifacts / "SUMMARY.txt").read_text(encoding="utf-8")
    assert "seed 7" in summary and "minimised" in summary
    minimised = sorted(artifacts.glob("*_min.json"))
    assert len(minimised) == 1
    small = json.loads(minimised[0].read_text(encoding="utf-8"))
    original = json.loads(
        (artifacts / (small["name"][: -len("_min")] + ".json")).read_text("utf-8")
    )
    assert len(small["steps"]) <= 8 < len(original["steps"])
    # the reduced case replays cleanly on the real engine and still shows the bug
    base = rf.load_base_config()
    stem = minimised[0].name[: -len(".json")]
    want_audit = (artifacts / f"{stem}.audit.jsonl").read_bytes().decode("utf-8")
    want_snapshot = (artifacts / f"{stem}.snapshot.json").read_bytes().decode("utf-8")
    rf.verify_script(small, base, want_audit, want_snapshot)
    produced = (
        (artifacts / f"{stem}.python.audit.jsonl").read_bytes(),
        (artifacts / f"{stem}.python.snapshot.json").read_bytes(),
    )
    assert produced != (want_audit.encode("utf-8"), want_snapshot.encode("utf-8"))
    assert any(
        step["type"] == "order" and step["order"]["order_type"] == 5 for step in small["steps"]
    )
    assert any(rf.read_report(artifacts / "python.tsv").values())
    assert list(rf.read_report(artifacts / "python_min.tsv").values()) != [""]


def test_an_engine_that_cannot_run_is_an_error_not_a_pass(tmp_path, capsys):
    command = f'"{Path(sys.executable).as_posix()}" -c "raise SystemExit(3)"'
    code = nightly.main(
        ["--seed", "7", "--count", "2", "--work", str(tmp_path / "work")]
        + ["--artifacts", str(tmp_path / "artifacts"), "--engine", "python", ".", command]
    )
    assert code == 2
    assert "exited 3" in capsys.readouterr().out
