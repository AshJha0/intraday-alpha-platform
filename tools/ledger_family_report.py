#!/usr/bin/env python3
"""tools/ledger_family_report.py: one-family view of the multiple-testing ledgers (v1.18 REL-02c).

Reads ``research/experiments.json`` and every per-dataset ledger
(``<data-root>/**/research/experiments.json``, git-ignored with the data),
classifies each entry ``real`` / ``synthetic`` / ``unknown``
(:meth:`ExperimentLedger.entry_data_family`; the real dataset versions come from
``research/holdout_registry.json``), merges the per-dataset ledgers by entry
identity (:func:`tools.merge_ledger.merge`) and prints the DRY-RUN report:
how many distinct configurations were tried on real data (N for the gates and
the deflated Sharpe), how many synthetic looks stay recorded but excluded, and
what a migration would change.

It never writes ``research/experiments.json`` (hash-chained, golden-pinned).
``--out PATH`` writes the merged real-family ledger to a NEW path for review;
the migration of the committed ledger and its regenerate are an owner decision
(see the REL-02 PR description).

Usage: python tools/ledger_family_report.py [--data-root data/real] [--out MERGED.json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.experiment.locking import atomic_write_text  # noqa: E402
from iap.validation.ledger import ExperimentLedger  # noqa: E402

_spec = importlib.util.spec_from_file_location("merge_ledger", REPO / "tools" / "merge_ledger.py")
_ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ml)


def real_versions(registry: Path) -> set[str]:
    doc = json.loads(registry.read_text(encoding="utf-8"))
    return set(doc.get("datasets", {}))


def per_dataset_ledgers(data_root: Path) -> list[Path]:
    return (
        sorted(Path(data_root).glob("**/research/experiments.json")) if data_root.is_dir() else []
    )


#: dataset-version prefixes the REL-01 audit identified as synthetic (BASELINE_v1_18.md section 1)
SYNTHETIC_PREFIXES = ("116b7787", "203c8f54")


def report(
    repo_ledger: Path,
    others: list[Path],
    real: set[str],
    synthetic_prefixes: tuple[str, ...] = SYNTHETIC_PREFIXES,
) -> tuple[dict, ExperimentLedger]:
    base = ExperimentLedger(repo_ledger)
    merged = base
    merged_from: list[str] = []
    conflicts: list[str] = []
    for p in others:
        merged, rep = _ml.merge(repo_ledger, p) if merged is base else _merge_into(merged, p)
        merged_from.append(str(p))
        conflicts += rep["result_conflicts"]
    synth = {
        v
        for e in merged.entries
        if (v := ExperimentLedger.entry_dataset_version(e))
        and v.startswith(tuple(synthetic_prefixes))
    }
    fam_repo = base.family_counts(real, synth)
    fam_all = merged.family_counts(real, synth)
    doc = {
        "repo_ledger": {
            "path": str(repo_ledger),
            "entries": len(base.entries),
            "total_looks": base.total_experiments,
            "by_family": fam_repo,
            "n_real_distinct": base.real_distinct_experiments(real),
        },
        "per_dataset_ledgers": merged_from,
        "merged": {
            "entries": len(merged.entries),
            "total_looks": merged.total_experiments,
            "by_family": fam_all,
            "n_real_distinct": merged.real_distinct_experiments(real),
            "excluded_synthetic_looks": fam_all["synthetic"]["looks"],
            "unclassified_entries": fam_all["unknown"]["entries"],
        },
        "result_conflicts": sorted(set(conflicts)),
        "note": (
            "dry run: research/experiments.json is not rewritten. N for the gates and the DSR "
            "would be merged.n_real_distinct (+1 for the look being judged); synthetic looks "
            "stay in the file. Entries without a family tag and outside the registry's real "
            "versions are 'unknown' and counted in neither family: classify them before migrating."
        ),
    }
    return doc, merged


def _merge_into(led: ExperimentLedger, theirs: Path) -> tuple[ExperimentLedger, dict]:
    """Merge a further ledger into an in-memory merged one (same rule as merge_ledger)."""
    import copy

    other = json.loads(Path(theirs).read_text())
    appended, conflicts = 0, []
    for e in other.get("entries", []):
        k = _ml._key(e)
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
        appended += 1
    return led, {"appended_entries": appended, "result_conflicts": conflicts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-ledger", type=Path, default=REPO / "research" / "experiments.json")
    ap.add_argument("--registry", type=Path, default=REPO / "research" / "holdout_registry.json")
    ap.add_argument("--data-root", type=Path, default=REPO / "data" / "real")
    ap.add_argument(
        "--out", type=Path, default=None, help="write the merged ledger HERE (new path)"
    )
    a = ap.parse_args(argv)
    if a.out is not None and a.out.resolve() == a.repo_ledger.resolve():
        ap.error("refusing to overwrite the committed ledger: the migration is an owner decision")
    doc, merged = report(a.repo_ledger, per_dataset_ledgers(a.data_root), real_versions(a.registry))
    if a.out is not None:
        atomic_write_text(a.out, merged._render())
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
