"""v1.18 REL-14a: parity counts derived from the CI jobs' outputs
(tests/harness/parity_counts.py) and compared with README's table
(tests/harness/check_headline_numbers.py --parity-json)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

from conftest import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "parity_counts", REPO_ROOT / "tests" / "harness" / "parity_counts.py"
)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def _junit(path, tests, skipped=0, failures=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<testsuites><testsuite name="pytest" tests="{tests}" skipped="{skipped}" '
        f'failures="{failures}" errors="0"/></testsuites>'
    )


def _artifacts(tmp_path, py_fast=2884, py_skip=17, slow=343, cpp=333, rust=(300, 76)):
    d = tmp_path / "parity"
    _junit(d / "parity-python" / "junit.xml", py_fast, skipped=py_skip)
    _junit(d / "parity-python-slow" / "junit.xml", slow, skipped=2)
    _junit(d / "parity-integration" / "integration.xml", 35)
    _junit(d / "parity-integration" / "replay.xml", 6)
    (d / "parity-cpp").mkdir()
    (d / "parity-cpp" / "ctest.txt").write_text(
        f"2026-10-17T15:09:39Z 100% tests passed, 0 tests failed out of {cpp}\n"
    )
    (d / "parity-rust").mkdir()
    (d / "parity-rust" / "cargo_test.txt").write_text(
        "".join(f"test result: ok. {n} passed; 0 failed; 0 ignored\n" for n in rust)
    )
    (d / "parity-golden").mkdir()
    (d / "parity-golden" / "golden.txt").write_text(
        "language | golden passed  | time   | status\n"
        "---------+----------------+--------+-------\n"
        "python   | 217            |  121s | PASS\n"
        "cpp      | 74             |   95s | PASS\n"
        "rust     | 75             |   37s | PASS\n"
    )
    return d


def test_derive_matches_the_run_all_definitions(tmp_path):
    counts = pc.derive(_artifacts(tmp_path))
    assert counts["tests"] == {
        "python": 2867 + 341,  # passed = tests - skipped, fast + slow
        "cpp": 333,
        "rust": 376,
        "integration": 35,
        "replay": 6,
    }
    assert counts["golden"] == {"python": 217, "cpp": 74, "rust": 75}
    assert counts["complete"]


def test_a_missing_artifact_is_null_never_a_guess(tmp_path):
    d = _artifacts(tmp_path)
    (d / "parity-python-slow" / "junit.xml").unlink()
    counts = pc.derive(d)
    assert counts["tests"]["python"] is None and not counts["complete"]


def _headline(*args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "harness" / "check_headline_numbers.py"), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_headline_check_compares_readme_with_the_derived_counts(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(pc.derive(_artifacts(tmp_path))))
    r = _headline("--parity-json", str(good), "--parity-strict")
    assert "parity_table_vs_ci: README table = the counts CI derived" in r.stdout
    drift = tmp_path / "drift.json"
    drift.write_text(json.dumps(pc.derive(_artifacts(tmp_path / "x", cpp=334))))
    warn = _headline("--parity-json", str(drift))
    assert "::warning::README parity table differs from CI: cpp tests: README 333, CI 334" in (
        warn.stdout
    )
    strict = _headline("--parity-json", str(drift), "--parity-strict")
    assert strict.returncode == 1 and "[FAIL] parity_table_vs_ci" in strict.stdout
