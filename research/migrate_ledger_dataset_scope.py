#!/usr/bin/env python3
"""One-off ledger migration (v1.4.0): stamp every entry with its dataset.

    PYTHONPATH=python/src python3 research/migrate_ledger_dataset_scope.py [--check]

``research/experiments.json`` is the denominator of every multiple-testing
correction in the research. Until v1.4.0 an entry of the report pipelines
(``design_horizon_scan``, ``promotion_pipeline``, ``adaptive_deployment``)
was identified by (alpha, kind, config) alone, with no record of the dataset
it was computed on. v1.4.0 regenerates the bundled dataset (equity flow now
reaches the close; ``data_version`` ``203c8f54...`` is retired), and without
a dataset in the identity the regenerated pipelines would have landed on the
old keys: the results of the old dataset overwritten in place and the new
looks not counted.

The rule since v1.4.0 (``iap.validation.ledger``, "Dataset scope"): a ledger
opened with ``dataset_version`` stamps and keys its entries by dataset, and
the looks of earlier datasets are kept. This script brings the existing file
under that rule. For every entry that carries no dataset — neither a
``dataset_version`` stamp nor one inside its config — it adds

    "dataset_version": LEGACY_DATASET_VERSION

and nothing else: ``key``, ``n``, ``count``, ``config`` and ``result`` are
untouched, so every experiment id derived from a key (the registry's
``experiment_id``, the archived transition log) still resolves. The
``experiment_runner`` entries already name their dataset in the config and
are left alone. The document is re-rendered at ``x-version`` 2 with the
``datasets`` summary.

It is only correct to run this on a ledger whose unstamped entries were all
recorded on ``LEGACY_DATASET_VERSION`` — true of the committed v1.3.0 file,
where every runner entry names that dataset and the pipelines ran on the same
tree. Idempotent: a second run changes nothing. ``--check`` exits 1 if the
file would change.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.validation.ledger import ExperimentLedger  # noqa: E402

LEDGER = REPO / "research" / "experiments.json"

#: ``data_version`` of the bundled dataset up to and including v1.3.0.
LEGACY_DATASET_VERSION = "203c8f540f75de984fa80f5ec9c04a91a1252819586a9fca6d48483f1462e67b"


def migrate(ledger: ExperimentLedger) -> list[str]:
    """Stamp the unstamped entries in place; return the changes made."""
    changes: list[str] = []
    for entry in ledger.entries:
        if ExperimentLedger.entry_dataset_version(entry) is not None:
            continue
        entry["dataset_version"] = LEGACY_DATASET_VERSION
        changes.append(
            f"{entry['alpha_id']} [{entry['kind']}] n={entry['n']}: "
            f"dataset_version -> {LEGACY_DATASET_VERSION[:8]}..."
        )
    return changes


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    original = LEDGER.read_text(encoding="utf-8")
    ledger = ExperimentLedger(LEDGER)
    changes = migrate(ledger)
    rendered = ledger._render()
    if not changes and rendered == original:
        print("ledger already dataset-scoped — no change")
        return 0
    for line in changes:
        print(line)
    for row in ledger.datasets():
        print(f"dataset {row['dataset_version']}: {row['entries']} entries, {row['looks']} looks")
    if check_only:
        print("--check: the ledger would change", file=sys.stderr)
        return 1
    with open(LEDGER, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)
    print(f"wrote {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
