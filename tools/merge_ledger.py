#!/usr/bin/env python3
"""tools/merge_ledger.py — merge two multiple-testing ledgers by entry identity (G-P1-1).

A research branch forks ``research/experiments.json``; merging it back must
neither drop looks nor count one twice.  This merges ``--ours`` (normally
the ledger on ``main``) with ``--theirs`` (the branch's) by entry identity
(the pinned ``key`` = alpha, kind, canonical config[, dataset]):

* every entry of ``ours`` is kept, in order, unchanged;
* an entry of ``theirs`` whose key is not in ``ours`` is appended (in
  ``theirs`` order) and its looks (``count``) are added to the total; its
  ``n`` is renumbered to the merged running total;
* an entry in both is one experiment: kept once, as in ``ours``; if its
  ``result`` differs it is reported as a conflict (the release owner decides;
  nothing is silently overwritten).

The merged total is ``ours.total + sum(count of the appended entries)``, so
it is >= both inputs.  The output is rendered by
:class:`iap.validation.ledger.ExperimentLedger` (same bytes a writer would
produce).  Prints a JSON report of the totals; ``--dry-run`` writes nothing.

Usage:
    python tools/merge_ledger.py --ours A.json --theirs B.json --out MERGED.json [--dry-run]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.experiment.locking import atomic_write_text  # noqa: E402
from iap.validation.ledger import ExperimentLedger  # noqa: E402


def _key(e: dict) -> str:
    return e.get("key") or ExperimentLedger.experiment_key(
        e.get("alpha_id", ""), e.get("kind", ""), e.get("config"), e.get("dataset_version")
    )


def merge(ours: Path, theirs: Path) -> tuple[ExperimentLedger, dict]:
    """The merged ledger (in memory, bound to ``ours``' path) and a report."""
    led = ExperimentLedger(ours)
    other = json.loads(Path(theirs).read_text())
    base_total = led.total_experiments
    other_total = int(other.get("total_experiments", 0))
    appended, conflicts = [], []
    for e in other.get("entries", []):
        k = _key(e)
        idx = led._index.get(k)
        if idx is not None:
            if led.entries[idx].get("result") != e.get("result"):
                conflicts.append(k)
            continue
        new = copy.deepcopy(e)
        new["key"] = k
        led.total_experiments += int(e.get("count", 1))
        new["n"] = led.total_experiments
        led._index[k] = len(led.entries)
        led.entries.append(new)
        appended.append(k)
    report = {
        "ours_total": base_total,
        "theirs_total": other_total,
        "merged_total": led.total_experiments,
        "ours_entries": len(led.entries) - len(appended),
        "theirs_entries": len(other.get("entries", [])),
        "appended_entries": len(appended),
        "appended_looks": led.total_experiments - base_total,
        "result_conflicts": conflicts,
    }
    return led, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ours", type=Path, required=True)
    ap.add_argument("--theirs", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    led, report = merge(a.ours, a.theirs)
    if not a.dry_run:
        if a.out is None:
            ap.error("--out is required unless --dry-run")
        atomic_write_text(a.out, led._render())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
