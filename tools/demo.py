#!/usr/bin/env python3
"""One-command synthetic demo (v2.1 Track 0): the whole loop on one instrument, one order's
decision chain, and the leakage canaries.  Synthetic data only; no network, no key.

    python tools/demo.py              # about a minute
    python tools/demo.py --replay     # also replay the run from its capture (about one more minute)

Steps, each printed with its wall time:

1. ``python -m iap.mvp run``: seeded feed -> book -> features -> alphas -> portfolio -> risk ->
   execution -> routing -> fills -> TCA, one decision trace per decision (docs/MVP.md).
2. ``python -m iap.mvp explain``: the chain behind one parent order.
3. the leakage canaries (python/tests/leakage): features built to leak on purpose, which the
   point-in-time checks must catch.
4. with ``--replay``: re-run from the captured events and compare the trace digest.

Nothing the demo prints carries an evidence class: it is a correctness check on fixtures.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "python"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PY_DIR / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    return env


def step(title: str, args: list[str], show: int | None = None) -> None:
    print(f"\n== {title}", flush=True)
    t0 = time.monotonic()
    res = subprocess.run(
        [sys.executable, *args], cwd=PY_DIR, env=_env(), capture_output=True, text=True
    )
    out = (res.stdout + res.stderr).strip().splitlines()
    for line in out if show is None else out[-show:]:
        print("   " + line)
    print(f"   ({time.monotonic() - t0:.1f} s)", flush=True)
    if res.returncode != 0:
        sys.exit(f"demo step failed: {title} (exit {res.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="also replay the run from its capture")
    ap.add_argument("--out", type=Path, help="run directory (default: a temporary directory)")
    a = ap.parse_args()

    t0 = time.monotonic()
    out = a.out or Path(tempfile.mkdtemp(prefix="iap-demo-")) / "mvp"
    step("1. full loop on SYN.EQ.AAPL, seed 12345", ["-m", "iap.mvp", "run", "--out", str(out)])
    step("2. why parent order 5 was sent", ["-m", "iap.mvp", "explain", "--run", str(out), "5"])
    step(
        "3. leakage canaries",
        ["-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:cacheprovider", "tests/leakage"],
        show=1,
    )
    if a.replay:
        step("4. replay from the capture", ["-m", "iap.mvp", "replay", "--run", str(out)])
    print(f"\nreport: {out / 'report.md'}")
    print(f"total {time.monotonic() - t0:.0f} s; synthetic data, no evidence class.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
