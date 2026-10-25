#!/usr/bin/env python3
"""tools/ledger_family_map.py: the ledger family dependency map (v1.19 stage 1, R3; defect D-08).

Writes ``research/validation/LEDGER_FAMILY_MAP.json``: for every hypothesis family
(ledger ``alpha_id``) the looks the central ledger ``research/experiments.json`` records on
real and synthetic data, the real looks that external per-dataset ledgers hold and the
central ledger does NOT (the 13 entries / 1,224 looks of
``research/v118/LEDGER_INVENTORY.md``, enumerated one by one from the committed inventory
JSON and, when the external file is on disk, re-read and checked against the inventory's
sha256), the lower bound on the family's real-data search breadth that follows, and the
families that are DEPENDENT on it because they took looks on the same real dataset version
(a selection on shared data: their looks count toward each other's selection history).

The map is what ``iap.agents.prereg_gate.selection_history`` reads: the selection-history
fields it returns (``central_real_looks``, ``external_unmerged_looks``,
``real_looks_lower_bound``, ``dependent_families``, ``map_sha256``) are attached to the
pre-registration entry ``prereg_gate.require`` returns and are the ``registry`` /
``selection_history`` record the canonical gate carries on its verdict
(``iap.validation.confirmatory.Verdict.selection_history``).

This is an ENUMERATION, not a merge: the central ledger is hash-chained and golden-pinned
and merging the external looks into it is the owner-approved regenerate flow
(``research/v118/REL02_LEDGER_MIGRATION.md``), not done here.  Nothing here reads market
data; the external ledger file is a look ledger (metadata) under the git-ignored data root.

    PYTHONPATH=python/src python tools/ledger_family_map.py [--data-root data/real] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.validation.ledger import ExperimentLedger  # noqa: E402

LEDGER = REPO / "research" / "experiments.json"
INVENTORY = REPO / "research" / "v118" / "LEDGER_INVENTORY.json"
REGISTRY = REPO / "research" / "holdout_registry.json"
OUT = REPO / "research" / "validation" / "LEDGER_FAMILY_MAP.json"
SCHEMA = "ledger_family_map/1"
#: dataset-version prefixes the REL-01 audit identified as synthetic (tools/ledger_inventory.py)
SYNTHETIC_PREFIXES = ("116b7787", "203c8f54")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _family_of(e: dict, real: set[str]) -> str:
    tag = e.get("data_family")
    if tag:
        return str(tag)
    v = ExperimentLedger.entry_dataset_version(e)
    if v is not None and v in real:
        return "real"
    if v is not None and v.startswith(SYNTHETIC_PREFIXES):
        return "synthetic"
    return "unknown"


def _blank() -> dict[str, Any]:
    return {
        "central": {k: {"entries": 0, "looks": 0} for k in ("real", "synthetic", "unknown")},
        "central_by_kind": {},
        "preregs": 0,
        "real_dataset_versions": set(),
        "external_unmerged": {"entries": 0, "looks": 0, "ledgers": []},
    }


def build(
    ledger_path: Path = LEDGER,
    inventory_path: Path = INVENTORY,
    registry_path: Path = REGISTRY,
    data_root: Path | None = None,
) -> dict[str, Any]:
    central = json.loads(ledger_path.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    real = set(json.loads(registry_path.read_text(encoding="utf-8")).get("datasets", {}))
    fams: dict[str, dict[str, Any]] = defaultdict(_blank)
    by_version: dict[str, set[str]] = defaultdict(set)
    for e in central["entries"]:
        a = str(e["alpha_id"])
        f = _family_of(e, real)
        looks = int(e.get("count", 1))
        row = fams[a]
        row["central"][f]["entries"] += 1
        row["central"][f]["looks"] += looks
        kind = f"{e.get('kind')}|{f}"
        kr = row["central_by_kind"].setdefault(kind, {"entries": 0, "looks": 0})
        kr["entries"] += 1
        kr["looks"] += looks
        if e.get("kind") == "prereg":
            row["preregs"] += 1
        v = ExperimentLedger.entry_dataset_version(e)
        if f == "real" and v:
            row["real_dataset_versions"].add(v)
            by_version[v].add(a)
    # external unmerged looks: enumerated from the committed inventory, entry by entry
    external: list[dict[str, Any]] = []
    unmerged_total = 0
    for led in inventory["external_ledgers"]:
        missing = led.get("missing_from_central") or []
        rec = {
            "path": led["path"],
            "sha256": led["sha256"],
            "relation": led["relation"],
            "dataset_versions": led.get("dataset_versions", []),
            "entries_missing": len(missing),
            "looks_missing": int(led.get("missing_looks", 0)),
            "re_read": None,
        }
        if data_root is not None:
            p = Path(data_root) / led["path"].replace("<data-root>/", "")
            if p.is_file():
                rec["re_read"] = {
                    "sha256_matches_inventory": _sha256(p) == led["sha256"],
                    "entries": len(json.loads(p.read_text(encoding="utf-8"))["entries"]),
                }
        external.append(rec)
        for m in missing:
            a = str(m["alpha_id"])
            looks = int(m["count"])
            row = fams[a]
            row["external_unmerged"]["entries"] += 1
            row["external_unmerged"]["looks"] += looks
            row["external_unmerged"]["ledgers"].append(
                {
                    "path": led["path"],
                    "ledger_entry_key": m["key"],
                    "kind": m["kind"],
                    "looks": looks,
                    "family": m.get("family", "real"),
                }
            )
            unmerged_total += looks
            for v in led.get("dataset_versions", []):
                row["real_dataset_versions"].add(v)
                by_version[v].add(a)
    families: dict[str, Any] = {}
    for a in sorted(fams):
        row = fams[a]
        versions = sorted(row["real_dataset_versions"])
        dependent = sorted({b for v in versions for b in by_version[v]} - {a})
        families[a] = {
            "central": row["central"],
            "central_by_kind": dict(sorted(row["central_by_kind"].items())),
            "preregs": row["preregs"],
            "external_unmerged": row["external_unmerged"],
            "real_looks_lower_bound": row["central"]["real"]["looks"]
            + row["external_unmerged"]["looks"],
            "real_dataset_versions": versions,
            "dependent_families": dependent,
        }
    groups = {v: sorted(s) for v, s in sorted(by_version.items())}
    return {
        "schema": SCHEMA,
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "central_ledger": {
                "path": "research/experiments.json",
                "sha256": _sha256(ledger_path),
                "entries": len(central["entries"]),
                "looks": int(central["total_experiments"]),
                "chain_head": central.get("chain_head"),
            },
            "inventory": {
                "path": "research/v118/LEDGER_INVENTORY.json",
                "sha256": _sha256(inventory_path),
            },
            "holdout_registry": {
                "path": "research/holdout_registry.json",
                "sha256": _sha256(registry_path),
            },
            "external_ledgers": external,
        },
        "unmerged_external_looks": {
            "total": unmerged_total,
            "entries": sum(
                len(l.get("missing_from_central") or []) for l in inventory["external_ledgers"]
            ),
            "status": "enumerated, not merged (merging is the owner-approved ledger regenerate; "
            "research/v118/REL02_LEDGER_MIGRATION.md)",
        },
        "dependency_rule": "two families are dependent when they hold real-data looks on the same "
        "dataset version (central or external): a selection among them was made on shared data, so "
        "each one's looks count toward the other's selection history",
        "groups_by_real_dataset_version": groups,
        "selection_history_fields": {
            "family_id": "the ledger alpha_id",
            "central_real_looks": "real-data looks the central ledger records for the family",
            "central_synthetic_looks": "synthetic looks (recorded, excluded from the real-data N)",
            "external_unmerged_looks": "real looks in external ledgers not in the central ledger",
            "real_looks_lower_bound": "central_real_looks + external_unmerged_looks (a lower bound: LEDGER_INVENTORY.md coverage statement)",
            "dependent_families": "families with looks on the same real dataset version",
            "dependent_real_looks_lower_bound": "the sum of real_looks_lower_bound over the dependent families",
            "map_sha256": "sha256 of this file as read",
            "known": "False when the family has no entry in the map (a new hypothesis): every count is 0",
        },
        "families": families,
    }


def _strip_volatile(doc: dict[str, Any]) -> dict[str, Any]:
    d = json.loads(json.dumps(doc))
    d.pop("generated_utc", None)
    for led in d["sources"]["external_ledgers"]:
        led.pop("re_read", None)
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--data-root", type=Path, default=None, help="re-read external ledgers if present"
    )
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument(
        "--check",
        action="store_true",
        help="compare with the committed map (exit 1 on a difference)",
    )
    a = ap.parse_args(argv)
    doc = build(data_root=a.data_root)
    if a.check:
        have = json.loads(a.out.read_text(encoding="utf-8"))
        if _strip_volatile(have) != _strip_volatile(doc):
            print(f"{a.out}: differs from a rebuild")
            return 1
        print(f"{a.out}: up to date")
        return 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    u = doc["unmerged_external_looks"]
    print(
        f"wrote {a.out}: {len(doc['families'])} families; {u['entries']} external entries / "
        f"{u['total']} looks enumerated, not merged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
