#!/usr/bin/env python3
"""RL1 research loop: one development or validation run of a U3 family (v2.0 stage 7).

    python tools/research/run_family.py --spec research/v2/families/D1_short_term_reversal.md \\
        --data <panel.csv> --manifest <manifest.json> [--partition development|validation] \\
        [--out <dir>] [--ledger research/experiments.json] [--capital 1000000]

What it does (:mod:`iap.research.loop`): refuses any partition other than the
development or validation partition (identity and path checks, fail closed),
refuses a specification whose gate forbids a fit, builds the features through
the data contracts, fits and registers the M0 forecast, runs the engine
(projection, auction execution, ledgers), builds the E5-class report, writes it
with its sha256 and records the run as an **analysis event with its outcome
exposure** in the selection ledger.  Every run appears in the ledger; without
``--ledger`` the run is refused (a research run that leaves no ledger entry is
not a research run).  Exit codes: 0 run recorded; 2 refused (partition,
identity, path, gate or route); 1 any other error.

Nothing here reads a sealed partition, the network or the wall clock.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.research.loop import PartitionRefused, SpecError, run_family  # noqa: E402

DEFAULT_LEDGER = ROOT / "research" / "experiments.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--spec", required=True, help="a U3 family specification file")
    ap.add_argument("--data", required=True, help="the daily panel CSV")
    ap.add_argument(
        "--manifest", required=True, help="the panel's manifest (partitions, route, sha256)"
    )
    ap.add_argument(
        "--partition", default="development", help="development (default) or validation"
    )
    ap.add_argument(
        "--out", default=None, help="directory for the report, research ledger and registry"
    )
    ap.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER),
        help=f"the selection ledger to record the run in (default {DEFAULT_LEDGER.relative_to(ROOT).as_posix()})",
    )
    ap.add_argument("--capital", type=float, default=1_000_000.0, help="registered capital C")
    ap.add_argument("--identity", default=None, help=argparse.SUPPRESS)  # tests only
    args = ap.parse_args(argv)
    if not args.ledger:
        print("refused: a run without a ledger is not a research run (--ledger)", file=sys.stderr)
        return 2
    try:
        result = run_family(
            args.spec,
            args.data,
            args.manifest,
            partition=args.partition,
            out_dir=args.out,
            ledger_path=args.ledger,
            identity=args.identity,
            capital=args.capital,
        )
    except PartitionRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except SpecError as exc:
        print(f"specification error: {exc}", file=sys.stderr)
        return 1
    rep = result["report"]
    summary = {
        "family": rep["identity"]["spec_path"],
        "partition": rep["identity"]["partition"],
        "route": rep["identity"]["route"],
        "report_sha256": result["sha256"],
        "sessions": rep["sessions"],
        "economic_screen": {
            k: rep["economic_screen"][k] for k in ("mean_bps", "se_bps", "retention", "passed")
        },
        "decision_tree": rep["decision_tree"],
        "ledger": result["ledger"],
        "paths": result["paths"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
