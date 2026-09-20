#!/usr/bin/env python3
"""One-time ledger migration (2026-09-20): take ``looks`` out of the
experiment IDENTITY and correct the per-alpha look count 21 -> 28.

Why this exists
---------------
``research/experiments.json`` is the denominator of every multiple-testing
correction in this repository, so what counts as "one experiment" has to be
stable. ``ExperimentLedger.experiment_key`` hashes ``(alpha_id, kind,
config)``, and ``run_all.py`` was putting ``looks`` inside that ``config``.
``looks`` is not part of what was looked AT — it is how expensive the look
was, and it already travels in ``count``. Carrying it in the identity meant
that correcting the look accounting forked every alpha into a second
"configuration": the ledger would have read 48 promotion-pipeline
configurations where 24 exist, and the Bonferroni denominator — the number
this file exists to keep honest — would have silently doubled for the wrong
reason.

The look count itself was also wrong. 21 omitted the time-latency grid (4),
the crossed/uncrossed conditional IC split (2) and the leakage shift IC (1);
the chain really makes 28 looks per alpha. See
``iap.research.LOOKS_PER_EXPERIMENT`` for the itemisation. Correcting it
RAISES the denominator, which makes every corrected t-statistic harder to
clear, not easier.

What it does
------------
For each ``promotion_pipeline`` entry: drops ``looks`` from ``config``,
rewrites ``count`` to ``LOOKS_PER_EXPERIMENT``, and recomputes ``key`` under
the new identity. ``total_experiments`` is re-derived as the sum of counts.
Every other entry kind is left untouched. Idempotent: running it twice is a
no-op. After this, ``run_all.py`` matches the existing entries by key and
updates them in place (``reruns`` increments) instead of appending.

Usage: python3 research/migrate_ledger_looks_identity.py [--check]
  --check  exit 1 if the file would change, without writing (for CI)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.research import LOOKS_PER_EXPERIMENT  # noqa: E402
from iap.validation.ledger import ExperimentLedger  # noqa: E402

LEDGER = REPO / "research" / "experiments.json"
#: Both kinds run the same validate_alpha chain and therefore make the same
#: number of looks; only ``promotion_pipeline`` also carried ``looks`` in its
#: identity, so only it needs the key recomputed.
KINDS = ("promotion_pipeline", "experiment_runner")


def migrate(doc: dict) -> tuple[dict, list[str]]:
    """Return (migrated document, list of human-readable changes)."""
    changes: list[str] = []
    for entry in doc["entries"]:
        kind = entry.get("kind")
        if kind not in KINDS:
            continue
        config = dict(entry.get("config") or {})
        before_looks = config.pop("looks", None)
        before_count = int(entry.get("count", 1))
        key = ExperimentLedger.experiment_key(entry["alpha_id"], kind, config)
        if (before_looks is None and before_count == LOOKS_PER_EXPERIMENT
                and entry.get("key") == key):
            continue  # already migrated
        entry["config"] = config
        entry["count"] = LOOKS_PER_EXPERIMENT
        entry["key"] = key
        dropped = (f"looks {before_looks} -> (identity dropped), "
                   if before_looks is not None else "")
        changes.append(f"{entry['alpha_id']} [{kind}]: {dropped}"
                       f"count {before_count} -> {LOOKS_PER_EXPERIMENT}")
    doc["total_experiments"] = sum(int(e.get("count", 1)) for e in doc["entries"])
    doc["distinct_experiments"] = len(doc["entries"])
    return doc, changes


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    original = LEDGER.read_text(encoding="utf-8")
    doc, changes = migrate(json.loads(original))
    rendered = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True,
                          allow_nan=False) + "\n"
    if not changes and rendered == original:
        print("ledger already migrated — no change")
        return 0
    for line in changes:
        print(line)
    print(f"total_experiments -> {doc['total_experiments']} "
          f"over {doc['distinct_experiments']} distinct experiments")
    if check_only:
        print("--check: the ledger would change", file=sys.stderr)
        return 1
    LEDGER.write_text(rendered, encoding="ascii")
    print(f"wrote {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
