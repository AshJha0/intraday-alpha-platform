#!/usr/bin/env python3
"""Per-package Python line-coverage floors (2026-11-18 quality review).

The ``coverage`` CI job already enforces one floor on the whole-suite total.
A total can hold while one package loses its tests, so this check reads a
``coverage json`` report and fails when any package listed in
``tests/harness/coverage_floors.json`` is below its floor.

A floor is a MEASURED figure minus two points, never a number typed in: the
measurement, its commands and the uncovered code are in
``docs/review/COVERAGE_BASELINE_2026-11-18.md``.  The baseline ran only the
tests that import each package; CI runs the whole suite, so CI's per-package
figure can only be equal or higher.  Raise a floor when coverage rises; never
lower one without a CHANGELOG line.

A package is a dotted module prefix (``iap.llm``) or a single module
(``iap.research.pnl_power``); its line coverage is covered statements over
statements, summed over the files under it.  A listed package with no files
in the report is a failure (a renamed or unmeasured package must not pass
silently).

    python3 tests/harness/check_coverage_floor.py coverage.json [--floors FILE]

Exit status: 0 every floor holds, 1 a package is below its floor or missing,
2 cannot run (unreadable report or floors file).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FLOORS = ROOT / "tests" / "harness" / "coverage_floors.json"


def _module_of(path: str) -> str | None:
    """``.../src/iap/llm/loop.py`` -> ``iap.llm.loop``; ``None`` outside ``iap``."""
    parts = path.replace("\\", "/").split("/")
    if "iap" not in parts:
        return None
    rel = parts[len(parts) - 1 - parts[::-1].index("iap") :]
    rel[-1] = rel[-1].removesuffix(".py")
    if rel[-1] == "__init__":
        rel = rel[:-1]
    return ".".join(rel)


def _belongs(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def package_lines(report: Mapping[str, Any], package: str) -> tuple[int, int]:
    """``(covered, statements)`` summed over the report's files under ``package``."""
    covered = statements = 0
    for path, data in (report.get("files") or {}).items():
        module = _module_of(path)
        if module is not None and _belongs(module, package):
            summary = data["summary"]
            covered += int(summary["covered_lines"])
            statements += int(summary["num_statements"])
    return covered, statements


def check(report: Mapping[str, Any], floors: Mapping[str, float]) -> list[str]:
    """One line per package; a line starting with ``FAIL`` is a failure."""
    out = []
    for package, floor in sorted(floors.items()):
        covered, statements = package_lines(report, package)
        if statements == 0:
            out.append(f"FAIL {package}: no measured files in the report (floor {floor:.2f}%)")
            continue
        pct = 100.0 * covered / statements
        verdict = "ok  " if pct >= floor else "FAIL"
        out.append(
            f"{verdict} {package}: {pct:.2f}% ({covered}/{statements} lines), floor {floor:.2f}%"
        )
    return out


def load_floors(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: float(v["line_floor"]) for name, v in data["packages"].items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", type=Path, help="coverage json report")
    ap.add_argument("--floors", type=Path, default=FLOORS)
    args = ap.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        floors = load_floors(args.floors)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"check_coverage_floor: cannot run: {exc}", file=sys.stderr)
        return 2
    lines = check(report, floors)
    print("\n".join(lines))
    failed = [ln for ln in lines if ln.startswith("FAIL")]
    if failed:
        print(f"check_coverage_floor: {len(failed)} package(s) below floor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
