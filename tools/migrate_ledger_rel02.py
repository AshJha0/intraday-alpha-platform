#!/usr/bin/env python3
"""tools/migrate_ledger_rel02.py: the v1.18 REL-02 ledger migration (owner-approved 2026-10-18).

Applies the migration the ``tools/ledger_family_report.py`` dry run described
to ``research/experiments.json``, in three steps, all through
:class:`iap.validation.ledger.ExperimentLedger` (same bytes a writer produces):

1. **Tag.**  Every entry gets ``data_family``.  An entry whose dataset version
   is in the holdout registry is ``real``; one on a REL-01 synthetic version
   (``ledger_family_report.SYNTHETIC_PREFIXES``; this includes the 25
   ``experiment_runner`` entries, whose dataset version sits inside their
   config) is ``synthetic``.  The 14 ``prereg`` entries carry no dataset: each
   debits the look of a pre-registered test on real ITCH sessions, so they are
   ``real`` (conservative: it can only raise the real-data N).  The tag is not
   part of the chained identity, so tagging moves no key and no chain link.
2. **Merge the per-dataset real ledgers** (``<data-root>/*/research/experiments.json``,
   git-ignored) by entry identity, as ``tools/merge_ledger.py`` does: a new key
   is appended with ``n`` renumbered, a known key is kept once.
3. **Retroactive execution-study looks** (deviation, see
   ``research/v118/REL02_LEDGER_MIGRATION.md``): the B7 multi7 benchmark and the
   B8 two-venue benchmarks were run on real sessions without a ledger entry.
   Each paired policy-vs-baseline comparison of a study (policy, algo, size;
   per symbol for B8, pooled over symbols for B7) is one distinct
   configuration, ``kind`` ``execution_study``.  B8's ``overlap2`` comparisons
   are the same configurations on the same sessions as ``sweep``'s and are
   counted once.  Nothing is added for a run that did not happen.

Steps 2 and 3 only APPEND, so the old chain is a prefix of the new one
(``tests/harness/check_ledger_monotone.py``) and the chain head changes once.
Idempotent: a second run changes nothing.

Usage: python tools/migrate_ledger_rel02.py [--data-root data/real] [--dry-run]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))
sys.path.insert(0, str(REPO / "tools"))

from iap.experiment.locking import atomic_write_text  # noqa: E402
from iap.validation.ledger import ExperimentLedger  # noqa: E402
from ledger_family_report import SYNTHETIC_PREFIXES, _ml, per_dataset_ledgers  # noqa: E402

LEDGER = REPO / "research" / "experiments.json"
REGISTRY = REPO / "research" / "holdout_registry.json"
STUDY_KIND = "execution_study"

#: (alpha_id, study, source file, per-symbol?) in the order they were run
STUDIES = (
    ("B7", "multi7_signal", "research/execution/benchmark/benchmark_multi7_signal.json", False),
    ("B8", "overlap2", "research/execution/b8/benchmark_overlap2_AAPL.json", True),
    ("B8", "overlap2", "research/execution/b8/benchmark_overlap2_MSFT.json", True),
    ("B8", "sweep", "research/execution/b8/benchmark_sweep_AAPL.json", True),
    ("B8", "sweep", "research/execution/b8/benchmark_sweep_MSFT.json", True),
    ("B8", "oos3", "research/execution/b8/benchmark_oos3_AAPL.json", True),
    ("B8", "oos3", "research/execution/b8/benchmark_oos3_MSFT.json", True),
)

DEVIATION = (
    "REL-02 retroactive look (deviation): this comparison was run and published before "
    "it was recorded; recorded 2026-10-18 from the committed result file so the real-data "
    "N counts it. Not judged here (no gate_looks)."
)


def registry_labels(registry: Path) -> dict[str, str]:
    """dataset label -> registry dataset version."""
    doc = json.loads(registry.read_text(encoding="utf-8"))
    return {d["label"]: v for v, d in doc["datasets"].items()}


def classify(e: dict, real: set[str]) -> str:
    tag = e.get("data_family")
    if tag:
        return str(tag)
    v = ExperimentLedger.entry_dataset_version(e)
    if v is not None and v in real:
        return "real"
    if v is not None and v.startswith(SYNTHETIC_PREFIXES):
        return "synthetic"
    if e.get("kind") == "prereg" and v is None:
        return "real"
    raise SystemExit(f"cannot classify entry n={e.get('n')} key={e.get('key')}")


def _append(led: ExperimentLedger, e: dict) -> bool:
    k = e["key"]
    if k in led._index:
        return False
    led.total_experiments += int(e.get("count", 1))
    e["n"] = led.total_experiments
    led._index[k] = len(led.entries)
    led.entries.append(e)
    return True


def study_entries(labels: dict[str, str]) -> list[dict]:
    out = []
    for alpha, study, rel, per_symbol in STUDIES:
        doc = json.loads((REPO / rel).read_text(encoding="utf-8"))
        label = Path(str(doc["data_dir"]).replace("\\", "/")).name
        version = labels[label]
        symbols = sorted({k.rsplit("|", 1)[1] for k in doc["cells_by_symbol"]})
        for comparison in sorted(doc["paired_vs_baseline"]):
            config = {
                "comparison": comparison,
                # the policy roster is not part of a comparison's identity (sweep adds a
                # policy to overlap2's roster; their shared comparisons are the same runs)
                "grid": {k: v for k, v in doc["grid"].items() if k != "policies"},
                "sessions": doc["sessions"],
                "symbols": symbols,
                "study": "b8_two_venue" if alpha == "B8" else "b7_multi7_signal",
            }
            all_in = doc["paired_vs_baseline"][comparison].get("costs_bps", {}).get("all_in", {})
            e = {
                "alpha_id": alpha,
                "kind": STUDY_KIND,
                "config": config,
                "count": 1,
                "dataset_version": version,
                "data_family": "real",
                "key": ExperimentLedger.experiment_key(alpha, STUDY_KIND, config, version),
                "result": {
                    "all_in_bps_mean": all_in.get("mean"),
                    "all_in_t": all_in.get("t"),
                    "n_clusters": all_in.get("n_clusters"),
                    "note": DEVIATION,
                    "run": study,
                    "source": rel,
                },
            }
            assert per_symbol == (len(symbols) == 1)
            out.append(e)
    return out


def migrate(data_root: Path, ledger: Path = LEDGER) -> tuple[ExperimentLedger, dict]:
    led = ExperimentLedger(ledger)
    real = set(json.loads(REGISTRY.read_text(encoding="utf-8"))["datasets"])
    before = {"entries": len(led.entries), "looks": led.total_experiments, "head": led.chain_head}
    tagged = 0
    for e in led.entries:
        if "data_family" not in e:
            e["data_family"] = classify(e, real)
            tagged += 1
    merged = {}
    for p in per_dataset_ledgers(data_root):
        added = 0
        for e in json.loads(p.read_text(encoding="utf-8")).get("entries", []):
            new = copy.deepcopy(e)
            new["key"] = _ml._key(new)
            new["data_family"] = classify(new, real)
            added += _append(led, new)
        merged[p.parent.parent.name] = added
    studies: dict[str, int] = {}
    for e in study_entries(registry_labels(REGISTRY)):
        if _append(led, e):
            studies[e["alpha_id"]] = studies.get(e["alpha_id"], 0) + 1
    fam = led.family_counts()
    rep = {
        "before": before,
        "after": {
            "entries": len(led.entries),
            "looks": led.total_experiments,
            "head": led.chain_head,
        },
        "tagged": tagged,
        "merged_from_per_dataset": merged,
        "retroactive_study_entries": studies,
        "by_family": fam,
        "n_real_distinct": led.real_distinct_experiments(),
    }
    return led, rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", type=Path, default=REPO / "data" / "real")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    led, rep = migrate(a.data_root)
    if not a.dry_run:
        atomic_write_text(LEDGER, led._render())
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
