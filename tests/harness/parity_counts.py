#!/usr/bin/env python3
"""Parity counts derived from CI artefacts (v1.18 REL-14a).

The README parity table is "captured from tests/harness/run_all.sh", but CI
does not run run_all.sh: each language has its own job.  Each job uploads its
raw output as a ``parity-*`` artifact; the deployment job downloads them all
and this script turns them into the same counts run_all.sh would print:

* python   tests = passed in the fast (``python``, 3.11) + slow
           (``python-slow``) JUnit XML; golden = the golden job's run_all table
* cpp      tests = ``out of N`` of the cpp job's ctest log; golden = run_all table
* rust     tests = sum of ``test result: ok. N passed`` of ``cargo test
           --workspace``; golden = run_all table
* integration / replay = passed in their JUnit XML

    python3 tests/harness/parity_counts.py derive --dir parity --out parity_ci.json

``check_headline_numbers.py --parity-json parity_ci.json`` compares the result
with README's table.  A count that cannot be derived is ``null``, never a
guess.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROWS = ("python", "cpp", "rust", "integration", "replay")


def junit_passed(path: Path) -> int | None:
    """passed = tests - failures - errors - skipped, summed over testsuites."""
    if not path.is_file():
        return None
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    total = 0
    for s in suites:
        n = int(s.get("tests", 0))
        n -= int(s.get("failures", 0)) + int(s.get("errors", 0)) + int(s.get("skipped", 0))
        total += n
    return total


def ctest_total(path: Path) -> int | None:
    if not path.is_file():
        return None
    hits = re.findall(
        r"tests passed, 0 tests failed out of (\d+)", path.read_text(errors="replace")
    )
    return int(hits[-1]) if hits else None


def cargo_passed(path: Path) -> int | None:
    if not path.is_file():
        return None
    hits = re.findall(r"test result: ok\. (\d+) passed", path.read_text(errors="replace"))
    return sum(map(int, hits)) if hits else None


def run_all_golden(path: Path) -> dict[str, int | None]:
    """The golden column of a ``run_all.sh --golden-only`` table."""
    out: dict[str, int | None] = {k: None for k in ("python", "cpp", "rust")}
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"\s*(python|cpp|rust)\s*\|\s*(\d+)\s*\|.*\|\s*PASS\s*$", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _add(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else a + b


def derive(d: Path) -> dict:
    """Counts from the downloaded artifacts under ``d`` (one folder each)."""
    golden = run_all_golden(d / "parity-golden" / "golden.txt")
    tests = {
        "python": _add(
            junit_passed(d / "parity-python" / "junit.xml"),
            junit_passed(d / "parity-python-slow" / "junit.xml"),
        ),
        "cpp": ctest_total(d / "parity-cpp" / "ctest.txt"),
        "rust": cargo_passed(d / "parity-rust" / "cargo_test.txt"),
        "integration": junit_passed(d / "parity-integration" / "integration.xml"),
        "replay": junit_passed(d / "parity-integration" / "replay.xml"),
    }
    return {
        "schema": "iap.parity_counts/v1",
        "tests": tests,
        "golden": golden,
        "complete": all(v is not None for v in (*tests.values(), *golden.values())),
    }


def table(counts: dict) -> str:
    """The README table rows, filled with the derived counts."""
    lines = []
    for row in ROWS:
        t = counts["tests"].get(row)
        g = counts["golden"].get(row, "-") if row in counts["golden"] else "-"
        lines.append(
            f"{row:<8} | {('?' if t is None else t)!s:<12} | {('?' if g is None else g)!s:<14} |    -s | PASS"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive")
    d.add_argument("--dir", required=True, help="folder holding the parity-* artifacts")
    d.add_argument("--out", required=True)
    a = p.parse_args(argv)
    counts = derive(Path(a.dir))
    Path(a.out).write_text(json.dumps(counts, indent=2) + "\n", encoding="utf-8")
    print(table(counts))
    if not counts["complete"]:
        print("parity_counts: some counts could not be derived (null)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
