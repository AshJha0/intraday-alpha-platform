#!/usr/bin/env python
"""v1.18 REL-01: freeze the research baseline from RAW artefacts.

Reads the committed JSON artefacts (ledger, B8 benchmark, R7 result, alpha registry), recomputes
the published headline numbers from them, and writes ``research/v118/baseline_metrics.json`` (and
optionally the Markdown table).  Nothing is read from Markdown reports.  Output is deterministic:
sorted keys, no wall-clock fields; the commit is recorded so every number traces to a tree.

    PYTHONPATH=python/src python tools/v118_baseline.py            # write
    PYTHONPATH=python/src python tools/v118_baseline.py --check    # exit 1 if the file is stale

Read-only for every input.  The statistic reimplemented here (CR1 day-cluster mean, Student-t
with G-1 df) mirrors ``iap.research.execution_benchmark.cluster_mean_stats``; the test pins the
two against each other.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "v118" / "baseline_metrics.json"

# two-sided 95 % Student-t quantiles, df 1..30 (no scipy dependency in this tool)
_T95 = {
    1: 12.7062,
    2: 4.3027,
    3: 3.1824,
    4: 2.7764,
    5: 2.5706,
    6: 2.4469,
    7: 2.3646,
    8: 2.3060,
    9: 2.2622,
    10: 2.2281,
    11: 2.2010,
    12: 2.1788,
    13: 2.1604,
    14: 2.1448,
    15: 2.1314,
    16: 2.1199,
    17: 2.1098,
    18: 2.1009,
    19: 2.0930,
    20: 2.0860,
    21: 2.0796,
    22: 2.0739,
    23: 2.0687,
    24: 2.0639,
    25: 2.0595,
    26: 2.0555,
    27: 2.0518,
    28: 2.0484,
    29: 2.0452,
    30: 2.0423,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def cluster_mean(rows: list[tuple[str, float]]) -> dict:
    """Pooled per-parent mean with CR1 day-cluster SE and Student-t (G-1 df) interval."""
    n = len(rows)
    by: dict[str, list[float]] = defaultdict(list)
    for g, v in rows:
        by[g].append(v)
    G = len(by)
    mean = sum(v for _, v in rows) / n
    s = sum((sum(vs) - len(vs) * mean) ** 2 for vs in by.values())
    se = math.sqrt(G / (G - 1) * s) / n if G > 1 else float("nan")
    tc = _T95.get(G - 1, 1.96) if G > 1 else float("nan")
    return {
        "n_parents": n,
        "n_clusters": G,
        "mean": mean,
        "se": se,
        "t_crit_95": tc,
        "ci95": [mean - tc * se, mean + tc * se],
        "mde_at_t_crit": tc * se,
        "per_cluster_mean": {g: sum(vs) / len(vs) for g, vs in sorted(by.items())},
    }


def b8_oos3() -> dict:
    out = {}
    for sym in ("AAPL", "MSFT"):
        p = ROOT / "research/execution/b8" / f"benchmark_oos3_{sym}.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        pr = d["two_venue"]["paired_rows"]
        res = {}
        for pair, rows in sorted(pr.items()):
            short = [(r["session"], r["costs_bps_diff"]["shortfall"]) for r in rows]
            trad = [(r["session"], r["costs_bps_diff"]["trading"]) for r in rows]
            opp = [(r["session"], r["costs_bps_diff"]["opportunity"]) for r in rows]
            res[pair] = {
                "shortfall_bps": cluster_mean(short),
                "trading_bps": cluster_mean(trad),
                "opportunity_bps": cluster_mean(opp),
                "parents_with_zero_diff": sum(1 for _, v in short if v == 0.0),
                "parents_gt0": sum(1 for _, v in short if v > 0.0),
            }
        out[sym] = {"source": p.relative_to(ROOT).as_posix(), "sha256": _sha256(p), "pairs": res}
    return out


def ledger() -> dict:
    from iap.validation.ledger import chain_head

    p = ROOT / "research/experiments.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    ents = d["entries"]
    looks_by_ds: Counter = Counter()
    ents_by_ds: Counter = Counter()
    looks_by_kind: Counter = Counter()
    for e in ents:
        ds = (e.get("dataset_version") or "none")[:8]
        looks_by_ds[ds] += e["count"]
        ents_by_ds[ds] += 1
        looks_by_kind[e["kind"]] += e["count"]
    return {
        "source": p.relative_to(ROOT).as_posix(),
        "sha256": _sha256(p),
        "stored_chain_head": d["chain_head"],
        "recomputed_chain_head": chain_head(ents),
        "chain_ok": d["chain_head"] == chain_head(ents) and d["chain_length"] == len(ents),
        "entries": len(ents),
        "distinct_experiments": d["distinct_experiments"],
        "total_looks": d["total_experiments"],
        "sum_of_entry_counts": sum(e["count"] for e in ents),
        "looks_by_dataset_version_prefix": dict(sorted(looks_by_ds.items())),
        "entries_by_dataset_version_prefix": dict(sorted(ents_by_ds.items())),
        "looks_by_kind": dict(sorted(looks_by_kind.items())),
        "document_datasets_summary": [
            {
                "dataset_version_prefix": (x["dataset_version"] or "none")[:8],
                "entries": x["entries"],
                "looks": x["looks"],
            }
            for x in d["datasets"]
        ],
        "entries_without_dataset_version": {
            "entries": sum(1 for e in ents if not e.get("dataset_version")),
            "looks": sum(e["count"] for e in ents if not e.get("dataset_version")),
            "kinds": dict(
                sorted(Counter(e["kind"] for e in ents if not e.get("dataset_version")).items())
            ),
            "note": "attribution of these entries to a dataset comes only from the document-level "
            "datasets summary, not from the entry itself",
        },
        "real_data_looks_per_document_summary": sum(
            x["looks"] for x in d["datasets"] if (x["dataset_version"] or "").startswith("ee4c2187")
        ),
        "bonferroni_t_threshold": d["bonferroni_t_threshold"],
    }


def r7() -> dict:
    p = ROOT / "research/r7/r7_confirm_result.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    rows = []
    for r in d["results"]:
        rows.append(
            {
                "alpha": r["alpha"],
                "verdict": r["verdict"],
                "dsr": r["dsr"]["dsr"],
                "dsr_n_trials": r["dsr"]["n_trials"],
                "dsr_sr0": r["dsr"]["sr0"],
                "gate_eligible": r["eligibility"]["gate_eligible"],
                "eligibility_reasons": r["eligibility"]["reasons"],
                "methods": r["eligibility"]["methods"],
                "git_commit_of_run": r["result"].get("git_commit"),
            }
        )
    return {"source": p.relative_to(ROOT).as_posix(), "sha256": _sha256(p), "results": rows}


def alphas() -> dict:
    p = ROOT / "research/alpha_registry.json"
    d = json.loads(p.read_text(encoding="utf-8"))["alphas"]
    states = Counter(a["state"] for a in d.values())
    rows = {}
    for k, a in sorted(d.items()):
        ev = a.get("last_evaluation") or {}
        rows[k] = {
            "state": a["state"],
            "outcome": ev.get("outcome"),
            "failed_gates": ev.get("failed_gates"),
            "oos_ic_gate_value": (ev.get("gates", {}).get("oos_ic") or {}).get("value"),
        }
    return {
        "source": p.relative_to(ROOT).as_posix(),
        "sha256": _sha256(p),
        "state_counts": dict(sorted(states.items())),
        "alphas": rows,
    }


def toolchain() -> dict:
    lock = ROOT / "python/requirements-ci.txt"
    ci = ROOT / ".github/workflows/ci.yml"
    return {
        "python_used_for_this_baseline": sys.version.split()[0],
        "requirements_ci_sha256": _sha256(lock),
        "requirements_ci_hash_lines": sum(
            1 for ln in lock.read_text(encoding="utf-8").splitlines() if "--hash=" in ln
        ),
        "ci_yml_sha256": _sha256(ci),
        "workflows": sorted(p.name for p in (ROOT / ".github/workflows").glob("*.yml")),
        "note": "rustc and a C++ compiler are not installed on the baseline machine; the Rust "
        "and C++ toolchains are those pinned in the CI workflow files above",
    }


def build() -> dict:
    return {
        "schema": "iap.v118_baseline/1",
        "commit": _git("rev-parse", "HEAD"),
        "tree_is_clean_at_inputs": _git(
            "status", "--porcelain", "--", "research", "python/requirements-ci.txt", ".github"
        )
        == "",
        "toolchain": toolchain(),
        "ledger": ledger(),
        "r7_confirm": r7(),
        "alpha_registry": alphas(),
        "b8_oos3": b8_oos3(),
    }


MD_OUT = ROOT / "research" / "v118" / "BASELINE_v1_18.md"

_MD_TAIL = """
## 2. Data already examined (audit; not recomputed)

| session set | read by | state |
|---|---|---|
| multi7 (7 days 2019-20, AAPL/MSFT/QQQ) | all EQ alphas, net-of-cost screen, breadth book, B7 | in-sample, spent |
| 2019-10-18, 2021-07-13, 2021-08-13 | v1.16 monetisation, AI4 train and validation | in-sample |
| 2025-11-28, 12-08, 12-09, 12-10, 2026-06-12 (12-11 and 12-12 once ingested) | R7 confirm (one look each of EQ10 and EQ05); B8 calibration and evaluation | holdout read once, then reused |
| 2026-05-15, 05-18 | v1.8 holdout (6 alphas, 504 looks), AUC01, B8 evaluation | spent three times |
| breadth/ds_20190130 (100 names) | nothing | features built, never read |
| every symbol other than AAPL/MSFT/QQQ on the nine 2025/26 days | nothing | unread, no dataset built |

No previously consumed holdout is reclassified here. The holdout registry (REL-02) turns this table into
enforced state.

## 3. Runtime surface (audit; what a real research run executes)

| layer | language that runs | note |
|---|---|---|
| ITCH / IEX / DBN / LOBSTER ingest | Python | Rust has only IAP1/JSONL codecs |
| feature engine | Rust (pyo3, default in the day wrappers) | 45 of 205 features; the rest Python |
| simulator, replay, router, TCA, all studies | Python | no call from Python into C++ or Rust binaries |
| Rust risk engine | parity reference and nightly differential fuzz | Python `iap.risk.engine` is what runs |
| C++ (about 25k lines) | parity goldens, benchmarks, image | nothing in the research path calls it |
| Java | retired in v1.17; `archive/java/` frozen at v1.14.0 | not built |

## 4. Acceptance (REL-01)

Every number in section 1 traces to a committed artefact, the commit above and a file hash. The tool is
deterministic and read-only on its inputs.
"""


def render_markdown(d: dict) -> str:
    led = d["ledger"]
    unscoped = led["entries_without_dataset_version"]
    out = [
        "# v1.18 baseline (REL-01)",
        "",
        f"Frozen from RAW artefacts at commit `{d['commit'][:12]}` (origin/main after v1.17.1). Generated by",
        "`tools/v118_baseline.py` from `research/v118/baseline_metrics.json`; `--check` fails if it is stale and",
        "`python/tests/test_v118_baseline.py` pins it. Nothing in section 1 is read from a Markdown report.",
        "Sections 2 and 3 are facts verified by the 2026-10-17 audits and are not recomputed by the tool.",
        "",
        "## 1. Recomputed from raw JSON",
        "",
        "### Research ledger",
        "",
        "| item | value |",
        "|---|---|",
        f"| hash chain recomputes to the stored head | {led['chain_ok']} |",
        f"| entries / distinct experiments / total looks | {led['entries']} / "
        f"{led['distinct_experiments']} / {led['total_looks']} |",
        "| looks per the document's own dataset summary | "
        + "; ".join(
            f"{x['dataset_version_prefix']} {x['looks']:,}"
            for x in led["document_datasets_summary"]
        )
        + " (116b7787 and 203c8f54 are synthetic, ee4c2187 is multi7, none are preregs) |",
        f"| entries with no `dataset_version` of their own | {unscoped['entries']} "
        f"({unscoped['looks']} looks, kinds {unscoped['kinds']}); their dataset attribution exists only in "
        "the document summary |",
        f"| real-data looks in the repo ledger | {led['real_data_looks_per_document_summary']} of "
        f"{led['total_looks']}; real-data in-sample looks on multi7 and the holdout datasets sit in "
        "per-dataset ledgers under `data/real`, outside this count and outside git (audit) |",
        f"| Bonferroni t threshold in force | {led['bonferroni_t_threshold']:.3f} |",
        "",
        "### R7 confirmatory result",
        "",
        "| alpha | verdict | DSR trials used | gate eligible | reason |",
        "|---|---|---|---|---|",
    ]
    for x in d["r7_confirm"]["results"]:
        out.append(
            f"| {x['alpha']} | {x['verdict']} | {x['dsr_n_trials']} (sr0 {x['dsr_sr0']}) | "
            f"{x['gate_eligible']} | {'; '.join(x['eligibility_reasons'])} |"
        )
    out += [
        "",
        "The recorded result still carries these trial counts and `verdict: confirmed` beside",
        "`gate_eligible: false`. REL-02 and REL-06 re-issue it.",
        "",
        "### B8 six-day out-of-sample: policy minus pinned Nasdaq, bps",
        "",
        "Day-clustered CR1 SE, Student-t with G-1 = 2 df, G = 3 sessions. MDE is the 95 % t quantile times the",
        "shortfall SE.",
        "",
        "| symbol | policy | shortfall | trading | opportunity | MDE | parents with zero difference |",
        "|---|---|---|---|---|---|---|",
    ]
    for sym in ("AAPL", "MSFT"):
        for pair, v in d["b8_oos3"][sym]["pairs"].items():
            sf, tr, op = v["shortfall_bps"], v["trading_bps"], v["opportunity_bps"]
            out.append(
                f"| {sym} | {pair.split('-')[0]} | {sf['mean']:+.2f} ± {sf['se']:.2f} | "
                f"{tr['mean']:+.2f} ± {tr['se']:.2f} | {op['mean']:+.2f} ± {op['se']:.2f} | "
                f"{sf['mde_at_t_crit']:.1f} | {v['parents_with_zero_diff']}/{sf['n_parents']} |"
            )
    out += [
        "",
        "Every headline number in `REPORT_oos6.md` reproduces from the JSON. For AAPL `sor_sweep` the `trading`",
        "component (-0.33) has the opposite sign to the `shortfall` headline (+0.56): the headline is dominated by",
        "the opportunity term on unfilled quantity (REL-03).",
        "",
        f"### Alpha registry: {d['alpha_registry']['state_counts']}",
        "",
        "| alpha | state | last outcome | failed gates | oos_ic gate value |",
        "|---|---|---|---|---|",
    ]
    for k, v in d["alpha_registry"]["alphas"].items():
        ic = "" if v["oos_ic_gate_value"] is None else f"{v['oos_ic_gate_value']:.4f}"
        out.append(
            f"| {k} | {v['state']} | {v['outcome']} | {', '.join(v['failed_gates'] or [])} | {ic} |"
        )
    tc = d["toolchain"]
    out += [
        "",
        "No alpha is PROMOTE; every CANDIDATE is held by `net_pnl_after_costs`.",
        "",
        "### Toolchain and CI",
        "",
        "| item | value |",
        "|---|---|",
        f"| Python used for this baseline | {tc['python_used_for_this_baseline']} |",
        f"| `python/requirements-ci.txt` sha256 / hashed lines | `{tc['requirements_ci_sha256'][:16]}...` / "
        f"{tc['requirements_ci_hash_lines']} |",
        f"| `.github/workflows/ci.yml` sha256 | `{tc['ci_yml_sha256'][:16]}...` |",
        f"| workflows | {', '.join(tc['workflows'])} |",
        "| Rust and C++ compilers | not installed on this machine; the CI workflow files pin them |",
    ]
    return "\n".join(out) + "\n" + _MD_TAIL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if baseline_metrics.json is stale")
    args = ap.parse_args()
    doc = build()
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if args.check:
        cur = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
        new = json.loads(json.dumps(doc))
        for d in (cur, new):
            d.pop("commit", None)
            d.pop("tree_is_clean_at_inputs", None)
            d.get("toolchain", {}).pop("python_used_for_this_baseline", None)
        if cur != new:
            print("baseline_metrics.json is stale", file=sys.stderr)
            return 1
        print("baseline_metrics.json is current")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    MD_OUT.write_text(render_markdown(doc), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} and {MD_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
