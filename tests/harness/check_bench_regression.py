#!/usr/bin/env python3
"""Coarse latency-regression guard for the C++ ``bench_all`` table.

Compares a fresh ``bench_all`` markdown table (``--current``) with the
committed one (``benchmarks/results_cpp.md``) row by row and fails when a
row is more than ``--factor`` times slower than the committed figure.

What this is and is not.  The committed figures come from one 2-CPU Xeon
container; a CI runner is different hardware under shared load, and
benchmarks/RESULTS.md says honest tail latencies are impossible there.  So the
guard is deliberately coarse (default factor 5): it catches an algorithmic
regression such as a lost lazy heap, an accidental O(n^2) or a per-event
allocation (the CRC trailer alone moved decode 50x), and it does NOT catch a
few-percent drift or a slow runner day.  Only the hot, cache-resident tables
are compared; the single-pass cold rows are one unwarmed run each and are far
too noisy.  Rows are matched by name, and the guard also fails when fewer than
``--min-coverage`` of the committed rows can be matched, so renaming the
tables cannot turn it into a silent pass.

Usage:  bench_all out.md && python3 tests/harness/check_bench_regression.py --current out.md
Exit status: 0 ok, 1 regression or too little coverage, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "benchmarks" / "results_cpp.md"
DEFAULT_FACTOR = 5.0
DEFAULT_MIN_COVERAGE = 0.8

_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([\d.]+)\s*\|")
_COLD = "cold reference"


def parse(text: str) -> dict[str, float]:
    """``{row name: first numeric column}`` of the hot tables (ns per event / trace).

    Stops at the cold-reference section: those rows are single unwarmed passes."""
    rows: dict[str, float] = {}
    for line in text.splitlines():
        if line.lower().startswith(_COLD):
            break
        m = _ROW.match(line)
        if m and not m.group(1).startswith(("benchmark", "-")):
            rows[m.group(1)] = float(m.group(2))
    return rows


def compare(
    baseline: dict[str, float],
    current: dict[str, float],
    factor: float = DEFAULT_FACTOR,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> tuple[list[tuple[str, float, float, float]], list[str]]:
    """``(rows, problems)``; each row is (name, baseline, current, ratio)."""
    rows = [(n, b, current[n], current[n] / b) for n, b in baseline.items() if n in current and b > 0]
    problems = [
        f"{n}: {c:.1f} vs committed {b:.1f} ({r:.1f}x slower, limit {factor:g}x)"
        for n, b, c, r in rows
        if r > factor
    ]
    if not baseline:
        problems.append("the committed table has no benchmark rows")
    elif len(rows) / len(baseline) < min_coverage:
        missing = sorted(set(baseline) - set(current))
        problems.append(
            f"only {len(rows)} of {len(baseline)} committed rows found in the current run "
            f"(minimum {min_coverage:.0%}); missing: {missing[:5]}"
        )
    return rows, problems


def render(rows: list[tuple[str, float, float, float]], factor: float) -> str:
    out = [
        f"| benchmark | committed | this run | ratio (limit {factor:g}x) |",
        "|---|---:|---:|---:|",
    ]
    for name, base, cur, ratio in rows:
        flag = " **REGRESSION**" if ratio > factor else ""
        out.append(f"| {name} | {base:.1f} | {cur:.1f} | {ratio:.2f}{flag} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--current", type=Path, required=True, help="bench_all markdown output")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--factor", type=float, default=DEFAULT_FACTOR)
    ap.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    args = ap.parse_args(argv)
    try:
        baseline = parse(args.baseline.read_text(encoding="utf-8"))
        current = parse(args.current.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rows, problems = compare(baseline, current, args.factor, args.min_coverage)
    table = render(rows, args.factor)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### C++ benchmark regression guard (coarse)\n\n" + table + "\n")
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"ok: {len(rows)} rows within {args.factor:g}x of the committed table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
