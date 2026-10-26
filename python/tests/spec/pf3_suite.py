"""Synthetic size and power study of PF-3, the combined multi-horizon book (U13).

Every cell's ``(n, T, 3)`` panel (:mod:`pf3_generators`) is read by ``P-SCBB``
(``iap.validation.pf3.scbb_pvalues_many``) at the block length that the declared rule
fixes on the cell's DEVELOPMENT series, then Holm over K_C = 3 at alpha_op 0.020.  Size
cell: k = datasets with at least one rejection (pass iff k <= 463 of 20,000).  Power cell:
detections = datasets where Holm rejects hypothesis 0 (acceptance >= 4,047 of 5,000 on the
iid normal reference cells only; every other cell reported with its lower bound).  Each
size cell also records the declared screens' ``SCREEN_QUANTILE`` quantiles, which become
the domain screen bounds of the cells that pass.

Resumable: one JSON per cell under ``--partial`` (default
``research/v2/confirmation_evidence/pf3_partial``); a cell whose file exists is not re-run.
Split across processes with ``--shard i/m``::

    PYTHONPATH=python/src python python/tests/spec/pf3_suite.py --shard 0/2      # one process
    PYTHONPATH=python/src python python/tests/spec/pf3_suite.py --shard 1/2      # another
    PYTHONPATH=python/src python python/tests/spec/pf3_suite.py --assemble       # PF-3_STUDY.json + .md
    PYTHONPATH=python/src python python/tests/spec/pf3_suite.py --n 200 --cells size:0,power:0   # smoke

The seeds are fixed by the protocol; a re-run with other seeds is a new revision, never a
retry.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "python" / "src"))

import pf3_generators as gen  # noqa: E402
from iap.validation import pf3  # noqa: E402
from iap.validation import profiles as pf  # noqa: E402

EVIDENCE = REPO / "research" / "v2" / "confirmation_evidence"
PARTIAL = EVIDENCE / "pf3_partial"
CHUNK = 500


def _bounds(k: int, n: int) -> dict[str, float]:
    from scipy import stats

    return {
        "estimate": k / n,
        "upper": float(stats.beta.ppf(0.95, k + 1, n - k)) if k < n else 1.0,
        "lower": float(stats.beta.ppf(0.05, k, n - k + 1)) if k > 0 else 0.0,
    }


def planted(T: int) -> float:
    return pf.planted_effect(T, gen.K)


def run_cell(cell: gen.Cell, *, n: int | None = None, chunk: int = CHUNK) -> dict[str, Any]:
    n = int(n or cell.n)
    rule = pf.block_length_rule(
        gen.development_series(cell),
        max_lag=pf3.MAX_LAG,
        floor=pf3.FLOOR,
        cap=pf3.CAP,
        multiplier=pf3.MULTIPLIER,
    )
    L = int(rule["block_length"])
    rng = np.random.default_rng(cell.seed)
    k = nan = 0
    screens: dict[str, list[np.ndarray]] = {s: [] for s in pf3.SCREENS}
    t0 = time.perf_counter()
    done = 0
    for _s, x in gen.panel_chunks(cell, chunk, n):
        p = pf3.scbb_pvalues_many(x, L, pf3.RESAMPLES, rng)
        nan += int((~np.isfinite(p)).sum())
        rej = pf.holm_many(p, gen.ALPHA_OP)
        k += int(rej[:, 0].sum()) if cell.kind == "power" else int(rej.any(axis=1).sum())
        if cell.kind == "size":
            for s, v in pf3.screen_stats_many(x).items():
                screens[s].append(np.asarray(v, dtype=float).ravel())
        done += x.shape[0]
    if cell.kind == "size":
        full = done == gen.N_SIZE
        res = ("pass" if k <= gen.SIZE_PASS_K else "fail") if full else "smoke"
    else:
        full = done == gen.N_POWER
        if not full:
            res = "smoke"
        elif cell.generator == "iid_normal":
            res = "pass" if k >= gen.POWER_PASS_DETECTIONS else "fail"
        else:
            res = "reported"
    out: dict[str, Any] = {
        "id": cell.id,
        "kind": cell.kind,
        "index": cell.index,
        "generator": cell.generator,
        "params": cell.params,
        "T": cell.T,
        "K": gen.K,
        "n": done,
        "seed": cell.seed,
        "dev_seed": cell.dev_seed,
        "delta": cell.delta,
        "block_length": L,
        "L_dep_dev": int(rule["L_dep"]),
        "blocks": -(-cell.T // L),
        "k": k,
        "nan_p": nan,
        "result": res,
        **_bounds(k, done),
        "seconds": round(time.perf_counter() - t0, 2),
    }
    if cell.kind == "size":
        allv = {s: np.concatenate(v) for s, v in screens.items()}
        out["screen_q"] = {s: float(np.quantile(v, pf3.SCREEN_QUANTILE)) for s, v in allv.items()}
        out["dep_length_within_block"] = float(np.mean(allv["dep_length"] <= L))
    return out


def versions() -> dict[str, str]:
    import scipy

    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }


def assemble(partial: Path) -> dict[str, Any]:
    rows = []
    for cell in gen.cells(planted=planted):
        f = partial / f"{cell.kind}_{cell.index:02d}.json"
        rows.append(json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None)
    have = [r for r in rows if r is not None]
    full = len(have) == len(rows) and all(r["result"] != "smoke" for r in have)
    doc = {
        "profile": pf3.PROFILE_ID,
        "procedure": pf3.PROCEDURE,
        "specification": "research/v2/U13_CONFIRMATION_PROTOCOL.md",
        "status": "full" if full else "partial",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "versions": versions(),
        "alpha_op": gen.ALPHA_OP,
        "K": gen.K,
        "n_size": gen.N_SIZE,
        "size_pass_k": gen.SIZE_PASS_K,
        "n_power": gen.N_POWER,
        "power_pass_detections": gen.POWER_PASS_DETECTIONS,
        "resamples": pf3.RESAMPLES,
        "block_rule": {
            "max_lag": pf3.MAX_LAG,
            "floor": pf3.FLOOR,
            "cap": pf3.CAP,
            "multiplier": pf3.MULTIPLIER,
        },
        "screen_quantile": pf3.SCREEN_QUANTILE,
        "seed_rule": "20350000 + 100 * kind + cell_index; development series at + 5000; bootstrap indices numpy PCG64 default_rng(cell seed)",
        "T_grid": list(gen.T_GRID),
        "seconds_total": round(sum(float(r["seconds"]) for r in have), 1),
        "cells": have,
    }
    doc["operating_domain"] = pf3.operating_domain(doc)
    return doc


def render(doc: dict[str, Any], path: Path) -> None:
    v = doc["versions"]
    lines = [
        "# PF-3 combined-book confirmation: synthetic size and power evidence (U13)",
        "",
        f"Assembled {doc['generated_utc']} by `python/tests/spec/pf3_suite.py --assemble` (Python {v['python']}, "
        f"numpy {v['numpy']}, SciPy {v['scipy']}); status **{doc['status']}**; {doc['seconds_total']} s of cell time. "
        "Protocol: `research/v2/U13_CONFIRMATION_PROTOCOL.md`. The JSON beside this file is authoritative. "
        "Size: pass iff k <= 463 of 20,000 (one-sided 95 % Clopper-Pearson upper bound <= 0.025 at alpha_op 0.020). "
        "Power: >= 4,047 of 5,000 on the iid normal reference cells only. A pass is a validated-domain Monte Carlo "
        "claim, not a real-world false-positive rate.",
        "",
        f"Operating domain: `{json.dumps(doc['operating_domain'])}`",
        "",
        "| id | T | L (L_dep dev) | blocks | n | k | result | bound | screens (q99.5: dep_length / vol_cluster / kurtosis) |",
        "|---|---:|---|---:|---:|---:|---|---:|---|",
    ]
    for c in doc["cells"]:
        b = c["upper"] if c["kind"] == "size" else c["lower"]
        q = c.get("screen_q")
        qs = (
            f"{q['dep_length']:.0f} / {q['vol_cluster']:.3f} / {q['excess_kurtosis']:.2f}"
            if q
            else ""
        )
        lines.append(
            f"| {c['id']} | {c['T']} | {c['block_length']} ({c['L_dep_dev']}) | {c['blocks']} | {c['n']} | {c['k']} | "
            f"{c['result']} | {b:.4f} | {qs} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--shard", default="0/1", help="i/m: run cells with position %% m == i")
    ap.add_argument("--cells", default=None, help="comma-separated <kind>:<index>")
    ap.add_argument(
        "--n", type=int, default=None, help="datasets per cell (smoke only; nothing is saved)"
    )
    ap.add_argument("--partial", default=str(PARTIAL))
    ap.add_argument(
        "--assemble", action="store_true", help="write PF-3_STUDY.json and .md from --partial"
    )
    a = ap.parse_args(argv)
    partial = Path(a.partial)
    if a.assemble:
        doc = assemble(partial)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "PF-3_STUDY.json").write_text(
            json.dumps(doc, indent=1) + "\n", encoding="utf-8"
        )
        render(doc, EVIDENCE / "PF-3_STUDY.md")
        print("status:", doc["status"], "domain:", json.dumps(doc["operating_domain"]))
        return 0
    i, m = (int(v) for v in a.shard.split("/"))
    todo = gen.cells(planted=planted)
    if a.cells:
        want = {(t.split(":")[0], int(t.split(":")[1])) for t in a.cells.split(",")}
        todo = [c for c in todo if (c.kind, c.index) in want]
    # interleave expensive and cheap cells: order by position, take every m-th
    todo = [c for pos, c in enumerate(todo) if pos % m == i]
    for cell in todo:
        f = partial / f"{cell.kind}_{cell.index:02d}.json"
        if a.n is None and f.is_file():
            print("done already:", cell.id, flush=True)
            continue
        r = run_cell(cell, n=a.n)
        if a.n is None:
            partial.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(r, indent=1) + "\n", encoding="utf-8")
        print(
            f"{r['id']:<62} L={r['block_length']:>2} k={r['k']:>5} {r['result']:<8} {r['seconds']:.1f}s",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
