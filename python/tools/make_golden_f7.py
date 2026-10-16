#!/usr/bin/env python3
"""Generate the v1.17 F7 C++ parity goldens (tests/golden/expected_f7_*.json).

One golden per ported rule, produced by the Python reference
(``python/tools/f7_golden/r*.py``; see that package's docstring for the
parity contract). The C++ parity tests are ``cpp/tests/test_f7_*.cpp``;
``python/tests/test_f7_golden.py`` regenerates every document in-process and
pins the committed bytes (so a reference change cannot drift silently).

Usage (from ``python/``):
    PYTHONPATH=src python tools/make_golden_f7.py [--only <module>] [--force] [--check]
    PYTHONPATH=src python tools/make_golden_f7.py --out <dir>   (write elsewhere, e.g. CI)

``--check`` regenerates and compares bytes without writing (exit 1 on a
difference, naming the first differing offset).
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from f7_golden import GOLDEN_DIR, dumps  # noqa: E402

MODULES = (
    "r01_sor_sweep",
    "r02_sor_passive",
    "r03_impact_parent",
    "r04_pov_exact",
    "r05_almgren_chriss",
    "r06_urgency",
    "r07_pacing",
    "r08_fading",
    "r09_queue_cancel",
    "r10_markout_decomposition",
    "r11_dark_venue",
    "r12_hazard_grid",
)


def first_diff(a: bytes, b: bytes) -> str:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            lo = max(0, i - 60)
            return (
                f"offset {i}: committed ...{a[lo : i + 60]!r} vs regenerated ...{b[lo : i + 60]!r}"
            )
    return f"length {len(a)} vs {len(b)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", help="module name(s) to run")
    ap.add_argument("--force", action="store_true", help="overwrite an existing golden")
    ap.add_argument("--check", action="store_true", help="regenerate and compare, no write")
    ap.add_argument("--out", type=Path, default=GOLDEN_DIR, help="output directory")
    args = ap.parse_args()
    names = args.only or list(MODULES)
    rc = 0
    for name in names:
        mod = importlib.import_module(f"f7_golden.{name}")
        path = args.out / mod.NAME
        data = dumps(mod.document())
        if args.check:
            if not path.exists():
                print(f"{mod.NAME}: MISSING")
                rc = 1
            elif path.read_bytes() != data:
                print(f"{mod.NAME}: DIFFERS ({first_diff(path.read_bytes(), data)})")
                rc = 1
            else:
                print(f"{mod.NAME}: ok ({len(data)} bytes)")
            continue
        if path.exists() and not args.force and args.out == GOLDEN_DIR:
            print(f"{mod.NAME} already exists - pinned; pass --force to regenerate")
            rc = 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
