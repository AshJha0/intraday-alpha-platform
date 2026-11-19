"""tests/harness/check_coverage_floor.py: per-package line-coverage floors."""

from __future__ import annotations

import importlib.util
import json

from conftest import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "check_coverage_floor", REPO_ROOT / "tests" / "harness" / "check_coverage_floor.py"
)
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)


def _file(covered: int, statements: int) -> dict:
    return {"summary": {"covered_lines": covered, "num_statements": statements}}


REPORT = {
    "files": {
        "src/iap/llm/__init__.py": _file(0, 0),
        "src/iap/llm/loop.py": _file(90, 100),
        "src\\iap\\llm\\agent.py": _file(10, 100),  # Windows separators
        "src/iap/llmx/other.py": _file(0, 100),  # prefix, not a subpackage
        "src/iap/research/pnl_power.py": _file(45, 50),
        "src/iap/research/pnl_power_extra.py": _file(0, 50),
    }
}


def test_package_sums_its_files_only():
    assert guard.package_lines(REPORT, "iap.llm") == (100, 200)
    assert guard.package_lines(REPORT, "iap.research.pnl_power") == (45, 50)


def test_floor_holds_and_fails_at_the_boundary():
    assert guard.check(REPORT, {"iap.llm": 50.0})[0].startswith("ok")
    assert guard.check(REPORT, {"iap.llm": 50.01})[0].startswith("FAIL")
    assert guard.check(REPORT, {"iap.research.pnl_power": 90.0})[0].startswith("ok")


def test_missing_package_fails():
    (line,) = guard.check(REPORT, {"iap.gone": 10.0})
    assert line.startswith("FAIL") and "no measured files" in line


def test_main_exit_codes(tmp_path):
    report = tmp_path / "cov.json"
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    floors = tmp_path / "floors.json"
    floors.write_text(json.dumps({"packages": {"iap.llm": {"line_floor": 40}}}), encoding="utf-8")
    assert guard.main([str(report), "--floors", str(floors)]) == 0
    floors.write_text(json.dumps({"packages": {"iap.llm": {"line_floor": 60}}}), encoding="utf-8")
    assert guard.main([str(report), "--floors", str(floors)]) == 1
    assert guard.main([str(tmp_path / "absent.json"), "--floors", str(floors)]) == 2


def test_committed_floors_parse_and_never_exceed_the_measurement():
    data = json.loads(
        (REPO_ROOT / "tests" / "harness" / "coverage_floors.json").read_text(encoding="utf-8")
    )
    assert data["packages"]
    for name, entry in data["packages"].items():
        assert entry["line_floor"] <= entry["measured_line"] - 2, name
