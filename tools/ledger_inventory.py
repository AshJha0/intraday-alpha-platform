#!/usr/bin/env python3
"""tools/ledger_inventory.py: reproducible inventory of every experiment ledger (v1.18 review finding 4).

Lists the central ledger (``research/experiments.json``) and every other
ledger-shaped record of looks the repository knows about:

- every per-dataset ledger under the data root (``<data-root>/**/experiments*.json``
  with an ``entries`` list; the data root is git-ignored, so these are read in
  place and named relative to it);
- the ML model-run ledger ``research/models/ledger.json`` (one row per fitted model);
- the ExperimentRunner records ``research/experiments/<id>/spec.json``.

For each it reports the path, sha256, entry and look counts, the ``data_family``
split, the stored and recomputed chain head, how its entries relate to the
central ledger (present by identity key, present with a different count, or
missing) and the overlaps between external ledgers.  It only READS: the
central ledger is never rewritten here.

Output is deterministic (sorted, no timestamps, no absolute paths), so two runs
on the same files are byte-identical (``python/tests/test_ledger_inventory.py``).

Usage: python tools/ledger_inventory.py [--data-root data/real] [--out-dir research/v118] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))
sys.path.insert(0, str(REPO / "tools"))

from iap.validation.ledger import ExperimentLedger, chain_head  # noqa: E402
from ledger_family_report import SYNTHETIC_PREFIXES, _ml  # noqa: E402

LEDGER = REPO / "research" / "experiments.json"
REGISTRY = REPO / "research" / "holdout_registry.json"
ML_LEDGER = REPO / "research" / "models" / "ledger.json"
RUNNER_DIR = REPO / "research" / "experiments"
OUT_DIR = REPO / "research" / "v118"
#: central-ledger kinds that summarise the ML model zoo (many fits, few looks)
ML_KINDS = ("model_zoo_gate", "primary_model_choice", "metalabel_tau")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rel(p: Path, root: Path) -> str:
    return p.resolve().relative_to(root.resolve()).as_posix()


def _family(e: dict, real: set[str]) -> str:
    tag = e.get("data_family")
    if tag:
        return str(tag)
    v = ExperimentLedger.entry_dataset_version(e)
    if v is not None and v in real:
        return "real"
    if v is not None and v.startswith(SYNTHETIC_PREFIXES):
        return "synthetic"
    return "unknown"


def _split(entries: list[dict], real: set[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for e in entries:
        row = out.setdefault(_family(e, real), {"entries": 0, "looks": 0})
        row["entries"] += 1
        row["looks"] += int(e.get("count", 1))
    return dict(sorted(out.items()))


def external_ledgers(data_root: Path) -> list[Path]:
    """Every ``experiments*.json`` under the data root that holds an ``entries`` list."""
    if not data_root.is_dir():
        return []
    out = []
    for p in sorted(data_root.glob("**/experiments*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
            out.append(p)
    return out


def inventory(data_root: Path, ledger: Path = LEDGER) -> dict:
    real = set(json.loads(REGISTRY.read_text(encoding="utf-8"))["datasets"])
    central_doc = json.loads(ledger.read_text(encoding="utf-8"))
    centre = central_doc["entries"]
    by_key = {_ml._key(e): e for e in centre}
    looks = sum(int(e.get("count", 1)) for e in centre)
    central = {
        "path": _rel(ledger, REPO),
        "sha256": _sha256(ledger),
        "entries": len(centre),
        "looks": looks,
        "total_experiments": int(central_doc.get("total_experiments", 0)),
        "chain_head_stored": central_doc.get("chain_head"),
        "chain_head_recomputed": chain_head(centre),
        "by_family": _split(centre, real),
        "by_kind": {},
    }
    kinds: dict[str, dict[str, int]] = {}
    for e in centre:
        kr = kinds.setdefault(f"{e['kind']}|{_family(e, real)}", {"entries": 0, "looks": 0})
        kr["entries"] += 1
        kr["looks"] += int(e.get("count", 1))
    central["by_kind"] = dict(sorted(kinds.items()))

    ext, keysets = [], {}
    for p in external_ledgers(data_root):
        doc = json.loads(p.read_text(encoding="utf-8"))
        entries = doc["entries"]
        name = _rel(p, data_root)
        present, count_differs, missing = [], [], []
        for e in entries:
            k = _ml._key(e)
            c = by_key.get(k)
            row: dict[str, Any] = {
                "alpha_id": e.get("alpha_id"),
                "kind": e.get("kind"),
                "count": int(e.get("count", 1)),
                "key": k,
                "family": _family(e, real),
            }
            if c is None:
                missing.append(row)
            elif int(c.get("count", 1)) != row["count"]:
                count_differs.append({**row, "central_count": int(c.get("count", 1))})
            else:
                present.append(row)
        keysets[name] = {_ml._key(e) for e in entries}
        n_in = len(present) + len(count_differs)
        relation = (
            "fully merged" if not missing else "not merged" if n_in == 0 else "partially merged"
        )
        ext.append(
            {
                "path": f"<data-root>/{name}",
                "sha256": _sha256(p),
                "entries": len(entries),
                "looks": sum(int(e.get("count", 1)) for e in entries),
                "total_experiments": int(doc.get("total_experiments", 0)),
                "chain_head_stored": doc.get("chain_head"),
                "chain_head_recomputed": chain_head(entries),
                "dataset_versions": sorted(
                    {str(ExperimentLedger.entry_dataset_version(e)) for e in entries}
                ),
                "by_family": _split(entries, real),
                "relation": relation,
                "in_central": n_in,
                "count_differs": sorted(count_differs, key=lambda r: r["key"]),
                "missing_from_central": sorted(missing, key=lambda r: r["key"]),
                "missing_looks": sum(r["count"] for r in missing),
            }
        )
    names = sorted(keysets)
    overlaps = [
        {
            "a": f"<data-root>/{a}",
            "b": f"<data-root>/{b}",
            "shared_keys": len(keysets[a] & keysets[b]),
        }
        for i, a in enumerate(names)
        for b in names[i + 1 :]
        if keysets[a] & keysets[b]
    ]

    ml = {"path": _rel(ML_LEDGER, REPO), "present": ML_LEDGER.is_file()}
    if ml["present"]:
        mdoc = json.loads(ML_LEDGER.read_text(encoding="utf-8"))
        runs = mdoc.get("runs", [])
        per_model: dict[str, int] = {}
        for r in runs:
            per_model[str(r.get("name"))] = per_model.get(str(r.get("name")), 0) + 1
        ml.update(
            {
                "sha256": _sha256(ML_LEDGER),
                "fits": len(runs),
                "experiment_count": mdoc.get("experiment_count"),
                "fits_by_model": dict(sorted(per_model.items())),
                "central_entries": sum(1 for e in centre if e["kind"] in ML_KINDS),
                "central_looks": sum(
                    int(e.get("count", 1)) for e in centre if e["kind"] in ML_KINDS
                ),
            }
        )

    runner_ids = sorted(p.parent.name for p in RUNNER_DIR.glob("*/spec.json"))
    central_ids = {
        str(x)
        for e in centre
        for x in (
            (e.get("config") or {}).get("experiment_id"),
            (e.get("result") or {}).get("experiment_id"),
        )
        if x
    }
    runner: dict[str, Any] = {
        "path": _rel(RUNNER_DIR, REPO),
        "records": len(runner_ids),
        "in_central": sorted(i for i in runner_ids if i in central_ids),
        "not_in_central": sorted(i for i in runner_ids if i not in central_ids),
    }

    missing_entries = sum(len(x["missing_from_central"]) for x in ext)
    missing_looks = sum(x["missing_looks"] for x in ext)
    return {
        "central": central,
        "external_ledgers": ext,
        "external_overlaps": overlaps,
        "ml_model_ledger": ml,
        "runner_records": runner,
        "summary": {
            "central_entries": central["entries"],
            "central_looks": central["looks"],
            "central_real_entries": central["by_family"].get("real", {}).get("entries", 0),
            "central_real_looks": central["by_family"].get("real", {}).get("looks", 0),
            "chain_ok": central["chain_head_stored"] == central["chain_head_recomputed"],
            "external_ledgers": len(ext),
            "external_entries_missing_from_central": missing_entries,
            "external_looks_missing_from_central": missing_looks,
            "ml_fits_recorded_in_ml_ledger": ml.get("fits", 0),
            "ml_central_entries": ml.get("central_entries", 0),
            "runner_records_not_in_central": len(runner["not_in_central"]),
        },
    }


GAPS = """\
## Coverage statement

The central ledger is the multiple-testing denominator. It is **not** a complete record of
every look ever taken in this project, and no document should say it is. What it covers:

- every look recorded through `ExperimentLedger` in this repository since v1.4 (dataset-scoped
  identity), hash-chained since v1.14;
- the per-dataset real ledgers merged by REL-02 (`<data-root>/*/research/experiments.json`);
- 206 retroactive B7/B8 execution-study comparisons recorded from committed result files.

Known gaps (each is a lower bound on the true number of looks; none can be reconstructed
exactly from what is on disk):

1. **Per-dataset ledgers outside the REL-02 glob.** Any external ledger listed above as
   `not merged` or `partially merged` holds looks the central ledger does not count. REL-02
   merged only files named exactly `research/experiments.json`; the table shows what that left.
   Merging them is a ledger rewrite and needs the owner-approved regenerate flow (not done here).
2. **Pre-v1.4 unscoped entries.** Before v1.4 an entry's identity had no dataset, so a re-run
   on a regenerated dataset overwrote the earlier result in place
   (`research/migrate_ledger_dataset_scope.py`). Looks overwritten that way are lost; the
   number is unknown.
3. **ML fits compressed into decision looks.** The model zoo fitted many models (see the
   ML model ledger row) but the central ledger records only the decisions taken from them
   (`model_zoo_gate`, `primary_model_choice`, `metalabel_tau`). Each fit, hyper-parameter and
   threshold tried is a look that is not counted one by one.
4. **AI4 in-sample model selection.** AI4 (DeepLOB) is recorded as one pre-registration
   debit. The in-sample architecture, hyper-parameter and checkpoint choices that preceded it
   (`data/real/ai4_insample`, `ai4_cache`) were never recorded as looks and cannot be counted
   from the files on disk.
5. **Studies whose looks were never recorded.** Studies run before REL-02 without ledger
   writes (maker, auction, quoting v1, v1.16 monetisation and capacity, the B8 fill-model
   calibration runs, interactive notebook and agent exploration) are absent or present only
   through a later pre-registration debit. Their parameter sweeps are not reconstructible.
6. **Runner records without a central entry.** {runner_gap}

Consequence: Bonferroni thresholds, the expected max |t| and the real-data N computed from the
central ledger are **lower bounds on the true search breadth**. A result that only clears the
threshold because the denominator is small should be read as weaker than its |t| suggests.
"""


def render_md(inv: dict) -> str:
    c, s = inv["central"], inv["summary"]
    lines = [
        "# Ledger inventory (v1.18 review finding 4)",
        "",
        "Generated by `tools/ledger_inventory.py`; do not edit by hand. Paths under the",
        "git-ignored data root are written `<data-root>/...` (default `data/real`).",
        "",
        "## Headline",
        "",
        "| measure | value |",
        "|---|---:|",
        f"| central ledger entries (distinct configurations) | {s['central_entries']:,} |",
        f"| central ledger looks | {s['central_looks']:,} |",
        f"| of which real-data entries / looks | {s['central_real_entries']:,} / {s['central_real_looks']:,} |",
        f"| central chain head verifies | {'yes' if s['chain_ok'] else 'NO'} |",
        f"| external per-dataset ledgers found | {s['external_ledgers']} |",
        f"| external entries missing from the central ledger | {s['external_entries_missing_from_central']} |",
        f"| external looks missing from the central ledger | {s['external_looks_missing_from_central']:,} |",
        f"| ML model fits (model ledger) vs central ML decision entries | "
        f"{s['ml_fits_recorded_in_ml_ledger']} vs {s['ml_central_entries']} |",
        f"| ExperimentRunner records without a central entry | {s['runner_records_not_in_central']} |",
        "",
        "## Central ledger",
        "",
        f"`{c['path']}` sha256 `{c['sha256']}`",
        "",
        f"- entries {c['entries']:,}, looks {c['looks']:,} (`total_experiments` {c['total_experiments']:,})",
        f"- chain head stored `{c['chain_head_stored']}`, recomputed `{c['chain_head_recomputed']}`",
        "",
        "| kind | family | entries | looks |",
        "|---|---|---:|---:|",
    ]
    for k, v in c["by_kind"].items():
        kind, fam = k.split("|")
        lines.append(f"| {kind} | {fam} | {v['entries']:,} | {v['looks']:,} |")
    lines += [
        "",
        "## External per-dataset ledgers",
        "",
        "| path | sha256 (12) | entries | looks | families | chain head stored | relation | in central | missing entries / looks |",
        "|---|---|---:|---:|---|---|---|---:|---:|",
    ]
    for x in inv["external_ledgers"]:
        fam = ", ".join(f"{k} {v['entries']}/{v['looks']}" for k, v in x["by_family"].items())
        head = (x["chain_head_stored"] or "none")[:12]
        lines.append(
            f"| `{x['path']}` | `{x['sha256'][:12]}` | {x['entries']} | {x['looks']:,} | {fam} | "
            f"`{head}` | {x['relation']} | {x['in_central']} | "
            f"{len(x['missing_from_central'])} / {x['missing_looks']:,} |"
        )
    for x in inv["external_ledgers"]:
        if x["missing_from_central"] or x["count_differs"]:
            lines += [
                "",
                f"### `{x['path']}`: entries not in the central ledger",
                "",
                "| alpha | kind | family | looks | key (12) |",
                "|---|---|---|---:|---|",
            ]
            for r in x["missing_from_central"]:
                lines.append(
                    f"| {r['alpha_id']} | {r['kind']} | {r['family']} | {r['count']} | `{r['key'][:12]}` |"
                )
            for r in x["count_differs"]:
                lines.append(
                    f"| {r['alpha_id']} | {r['kind']} | {r['family']} | {r['count']} "
                    f"(central {r['central_count']}) | `{r['key'][:12]}` |"
                )
    lines += ["", "## Overlaps between external ledgers", ""]
    if inv["external_overlaps"]:
        lines += ["| a | b | shared keys |", "|---|---|---:|"]
        lines += [
            f"| `{o['a']}` | `{o['b']}` | {o['shared_keys']} |" for o in inv["external_overlaps"]
        ]
    else:
        lines.append("None: no identity key appears in two external ledgers.")
    ml = inv["ml_model_ledger"]
    lines += ["", "## ML model ledger", ""]
    if ml["present"]:
        lines += [
            f"`{ml['path']}` sha256 `{ml['sha256']}`: {ml['fits']} model fits "
            f"({', '.join(f'{k} {v}' for k, v in ml['fits_by_model'].items())}). "
            f"The central ledger carries {ml['central_entries']} ML decision entries "
            f"({ml['central_looks']} looks) for them.",
        ]
    else:
        lines.append("Not present.")
    r = inv["runner_records"]
    lines += [
        "",
        "## ExperimentRunner records",
        "",
        f"`{r['path']}`: {r['records']} records; {len(r['in_central'])} carry an `experiment_id` "
        f"found in the central ledger, {len(r['not_in_central'])} do not "
        f"({', '.join(f'`{i}`' for i in r['not_in_central']) or 'none'}).",
        "",
        GAPS.format(
            runner_gap=(
                f"{len(r['not_in_central'])} ExperimentRunner record(s) have no matching `experiment_id` "
                "in the central ledger (listed above)."
                if r["not_in_central"]
                else f"None today: all {r['records']} ExperimentRunner records resolve to a central entry."
            )
        ),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", type=Path, default=REPO / "data" / "real")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--check", action="store_true", help="exit 1 if the outputs would change")
    a = ap.parse_args(argv)
    inv = inventory(a.data_root)
    js = json.dumps(inv, indent=2, sort_keys=True) + "\n"
    md = render_md(inv)
    outs = {a.out_dir / "LEDGER_INVENTORY.json": js, a.out_dir / "LEDGER_INVENTORY.md": md}
    if a.check:
        stale = [
            p for p, t in outs.items() if not p.is_file() or p.read_text(encoding="utf-8") != t
        ]
        for p in stale:
            print(f"stale: {p}")
        return 1 if stale else 0
    for p, t in outs.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t, encoding="utf-8", newline="\n")
    print(json.dumps(inv["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
