"""Synthetic size and power study of the confirmation profiles PF-1 and PF-2 (U13).

Every cell's panel (:mod:`pf_generators`) is read by every candidate procedure of the
profile (``iap.validation.profiles``: ``hac_pvalues_many`` at the profile's pre-fixed
lag, ``cbb_pvalues_many`` at the block length that the declared rule fixes on the cell's
DEVELOPMENT series), then Holm over K = 4.  Size cell: k = datasets with at least one
rejection (complete null; pass iff k <= 463 of 20,000).  Power cell: detections =
datasets where Holm rejects hypothesis 0 (planted; acceptance >= 4,047 of 5,000 applies
to the iid normal reference cells only; every other cell is reported with its lower
bound and no acceptance)::

    PYTHONPATH=python/src python python/tests/spec/pf_suite.py --write            # both profiles, ~minutes
    PYTHONPATH=python/src python python/tests/spec/pf_suite.py --check            # recompute, compare k
    PYTHONPATH=python/src python python/tests/spec/pf_suite.py --profile PF-1 --cells 0,1 --n 200  # smoke

Output: ``research/v2/confirmation_evidence/<profile>_RESULTS.json`` (per cell seed,
development seed, n, k, estimate, bounds, half-width, block length and its diagnostic,
result, the versions) and ``CONFIRMATION_EVIDENCE.md`` rendered from the JSONs.  The
seeds are fixed by the profiles specification; a re-run with other seeds is a new
revision of the profile, never a retry.
"""

from __future__ import annotations

import argparse
import json
import math
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

import pf_generators as gen  # noqa: E402
from iap.validation import profiles as pf  # noqa: E402

EVIDENCE = REPO / pf.EVIDENCE_DIR
CHUNK = 1_000


def _bounds(k: int, n: int) -> dict[str, float]:
    from scipy import stats

    est = k / n
    upper = float(stats.beta.ppf(0.95, k + 1, n - k)) if k < n else 1.0
    lower = float(stats.beta.ppf(0.05, k, n - k + 1)) if k > 0 else 0.0
    return {
        "estimate": est,
        "upper": upper,
        "lower": lower,
        "half_width": 1.96 * math.sqrt(est * (1 - est) / n),
    }


def run_cell(
    cell: gen.Cell, profile: pf.Profile, *, n: int | None = None, chunk: int = CHUNK
) -> dict[str, Any]:
    """One cell through every candidate procedure; the counts and the per-procedure block rule."""
    n = int(n or cell.n)
    procs = dict(profile.procedures)
    counts = dict.fromkeys(procs, 0)
    nan = dict.fromkeys(procs, 0)
    block: dict[str, Any] = {}
    for name, spec in procs.items():
        if spec["kind"] == "cbb":
            dev = gen.development_series(cell)
            block[name] = pf.block_length_rule(
                dev,
                max_lag=spec["max_lag"],
                floor=spec["floor"],
                cap=spec["cap"],
                multiplier=spec["multiplier"],
            )
    rngs = {
        name: np.random.default_rng(cell.seed)
        for name, spec in procs.items()
        if spec["kind"] == "cbb"
    }
    t0 = time.perf_counter()
    done = 0
    for _start, x in gen.panel_chunks(gen.Cell(**{**cell.__dict__, "n": n}), chunk):
        for name, spec in procs.items():
            if spec["kind"] == "hac":
                p = pf.hac_pvalues_many(x, spec["lag"])
            else:
                p = pf.cbb_pvalues_many(
                    x, block[name]["block_length"], spec["resamples"], rngs[name]
                )
            nan[name] += int((~np.isfinite(p)).sum())
            rej = pf.holm_many(p, gen.ALPHA_OP)
            counts[name] += (
                int(rej[:, 0].sum()) if cell.kind == "power" else int(rej.any(axis=1).sum())
            )
        done += x.shape[0]
    out: dict[str, Any] = {
        "id": cell.id,
        "profile": cell.profile,
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
        "acf1_theoretical": pf.generator_acf1(cell.generator, cell.params),
        "seconds": round(time.perf_counter() - t0, 2),
        "procedures": {},
    }
    reference = cell.generator == "iid_normal"
    for name in procs:
        k = counts[name]
        b = _bounds(k, done)
        if cell.kind == "size":
            full = done == gen.N_SIZE
            res = ("pass" if k <= gen.SIZE_PASS_K else "fail") if full else "smoke"
        else:
            full = done == gen.N_POWER
            if not full:
                res = "smoke"
            elif reference:
                res = "pass" if k >= gen.POWER_PASS_DETECTIONS else "fail"
            else:
                res = "reported"
        rec = {"k": k, "nan_p": nan[name], "result": res, **b}
        if name in block:
            rec["block_length"] = block[name]["block_length"]
            rec["block_diagnostic"] = {
                k2: v for k2, v in block[name].items() if k2 not in ("acf", "ljung_box_tail")
            }
        out["procedures"][name] = rec
    return out


def versions() -> dict[str, Any]:
    import scipy

    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }


def _print(r: dict[str, Any]) -> None:
    print(
        f"{r['id']:<50} "
        + "  ".join(f"{p}: k={v['k']:5d} {v['result']}" for p, v in r["procedures"].items()),
        flush=True,
    )


def write_profile(
    profile: pf.Profile,
    cells: list[gen.Cell],
    *,
    n: int | None,
    path: Path,
    write: bool = False,
    partial: Path | None = None,
    assemble: bool = False,
) -> dict[str, Any]:
    """Run ``cells`` (or, with ``assemble``, read their partial files) and, with ``write``,
    commit the profile's evidence.  ``partial`` is a directory of one JSON per cell, so a
    full run can be split into several invocations and assembled at the end."""
    t0 = time.perf_counter()
    rows = []
    for cell in cells:
        pfile = (partial / f"{cell.profile}_{cell.kind}_{cell.index:02d}.json") if partial else None
        if assemble:
            r = json.loads(pfile.read_text(encoding="utf-8"))
        else:
            r = run_cell(cell, profile, n=n)
            if pfile is not None and n is None:
                pfile.parent.mkdir(parents=True, exist_ok=True)
                pfile.write_text(json.dumps(r, indent=1) + "\n", encoding="utf-8")
        rows.append(r)
        _print(r)
    doc = {
        "profile": profile.id,
        "title": profile.title,
        "specification": "python/tests/spec/CONFIRMATION_PROFILES.md",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "versions": versions(),
        "alpha_op": gen.ALPHA_OP,
        "fwer_claim": pf.FWER_CLAIM,
        "n_size": gen.N_SIZE,
        "size_pass_k": gen.SIZE_PASS_K,
        "n_power": gen.N_POWER,
        "power_pass_detections": gen.POWER_PASS_DETECTIONS,
        "K": gen.K,
        "seed_rule": "20320000 + 10000 * profile_index + 100 * kind + cell_index; development series at + 5000; bootstrap resample indices numpy PCG64 default_rng(cell seed)",
        "procedures": {k: dict(v) for k, v in profile.procedures.items()},
        "T_grid": list(profile.T_grid),
        "seconds_total": round(sum(float(r["seconds"]) for r in rows), 1),
        "seconds_wall_this_invocation": round(time.perf_counter() - t0, 1),
        "cells": rows,
    }
    if write and n is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        dom = pf.operating_domain(profile.id, REPO)
        doc["operating_domain"] = dom
        path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return doc


def render_markdown(docs: list[dict[str, Any]], path: Path) -> None:
    lines = ["# Confirmation profiles PF-1 and PF-2: synthetic size and power evidence (U13)", ""]
    lines.append(
        "Generated from the `<profile>_RESULTS.json` files in this directory by `python/tests/spec/pf_suite.py --write`. "
        "Specification: `python/tests/spec/CONFIRMATION_PROFILES.md`; framework: `research/v2/CONFIRMATION_FRAMEWORK.md`. "
        "Size acceptance k <= 463 of 20,000 per cell (one-sided 95 % Clopper-Pearson upper bound <= 0.025 at alpha_op 0.020, the R0 form); "
        "power acceptance >= 4,047 detections of 5,000 on the iid normal reference cells only. "
        "A pass means: under the predeclared synthetic data-generating process of the cell, the procedure's simulated family-wise error rate "
        "met the predeclared Monte Carlo acceptance criterion. It is a validated-domain Monte Carlo claim, not a real-world false-positive rate."
    )
    lines.append("")
    for doc in docs:
        pid = doc["profile"]
        procs = list(doc["procedures"])
        lines.append(f"## {pid}: {doc['title']}")
        lines.append("")
        lines.append(
            f"Run {doc['generated_utc']}, Python {doc['versions']['python']}, numpy {doc['versions']['numpy']}, SciPy {doc['versions']['scipy']}; "
            f"{doc['seconds_total']} s on one core; K = {doc['K']}; T grid {doc['T_grid']}."
        )
        lines.append("")
        dom = doc.get("operating_domain") or {}
        lines.append("### Operating domain (size passes at every T of the grid)")
        lines.append("")
        lines.append(
            "| procedure | reference passes | generator cells in domain | acf1 max | GARCH | t3 |"
        )
        lines.append("|---|---|---|---:|---|---|")
        for proc in procs:
            d = dom.get(proc) or {}
            gens = (
                ", ".join(g + (json.dumps(p) if p else "") for g, p in d.get("generators", []))
                or "(none)"
            )
            lines.append(
                f"| {proc} | {d.get('reference_passes')} | {gens} | {d.get('acf1_max', 0):.3f} | {d.get('garch_in_domain')} | {d.get('t3_in_domain')} |"
            )
        lines.append("")
        for kind in ("size", "power"):
            lines.append(f"### {kind} cells")
            lines.append("")
            head = (
                "| id | generator | T | n | "
                + " | ".join(f"{p} k / result / bound" for p in procs)
                + " |"
            )
            lines.append(head)
            lines.append("|---|---|---:|---:|" + "|".join("---" for _ in procs) + "|")
            for c in doc["cells"]:
                if c["kind"] != kind:
                    continue
                cols = []
                for p in procs:
                    r = c["procedures"][p]
                    b = r["upper"] if kind == "size" else r["lower"]
                    extra = f" L={r['block_length']}" if "block_length" in r else ""
                    cols.append(f"{r['k']} / {r['result']} / {b:.4f}{extra}")
                label = c["generator"] + (" " + json.dumps(c["params"]) if c["params"] else "")
                if kind == "power":
                    label += f" delta={c['delta']:.4f}"
                lines.append(
                    f"| {c['id']} | {label} | {c['T']} | {c['n']} | " + " | ".join(cols) + " |"
                )
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--profile", default=None)
    ap.add_argument(
        "--cells",
        default=None,
        help="comma-separated cell ids as <kind>:<index>, or indices (size cells)",
    )
    ap.add_argument(
        "--n",
        type=int,
        default=None,
        help="datasets per cell (smoke only; the evidence needs the full n)",
    )
    ap.add_argument(
        "--partial", default=None, help="directory of per-cell partial results (split a full run)"
    )
    ap.add_argument(
        "--assemble",
        action="store_true",
        help="assemble the evidence from --partial instead of running",
    )
    a = ap.parse_args(argv)
    profiles = (
        [pf.PROFILES[a.profile]]
        if a.profile
        else [p for p in pf.PROFILES.values() if not p.deferred]
    )
    docs = []
    for profile in profiles:
        cells = gen.cells_for(profile, planted=lambda T: pf.planted_effect(T, gen.K))
        if a.cells:
            want = set()
            for tok in a.cells.split(","):
                kind, _, idx = tok.partition(":")
                want.add((kind, int(idx)) if idx else ("size", int(kind)))
            cells = [c for c in cells if (c.kind, c.index) in want]
        path = EVIDENCE / f"{profile.id}_RESULTS.json"
        if a.check:
            committed = json.loads(path.read_text(encoding="utf-8"))
            by = {c["id"]: c for c in committed["cells"]}
            bad = []
            for cell in cells:
                r = run_cell(cell, profile, n=a.n)
                for p, v in r["procedures"].items():
                    if a.n is None and v["k"] != by[cell.id]["procedures"][p]["k"]:
                        bad.append((cell.id, p, v["k"], by[cell.id]["procedures"][p]["k"]))
                print(r["id"], {p: v["k"] for p, v in r["procedures"].items()}, flush=True)
            print("check:", "ok" if not bad else bad)
            if bad:
                return 1
            continue
        docs.append(
            write_profile(
                profile,
                cells,
                n=a.n,
                path=path,
                write=a.write and not a.cells,
                partial=Path(a.partial) if a.partial else None,
                assemble=a.assemble,
            )
        )
    if a.write and not a.profile and not a.cells:
        render_markdown(docs, EVIDENCE / "CONFIRMATION_EVIDENCE.md")
    elif a.write:
        existing = []
        for p in pf.PROFILES.values():
            f = EVIDENCE / f"{p.id}_RESULTS.json"
            if f.is_file():
                existing.append(json.loads(f.read_text(encoding="utf-8")))
        render_markdown(existing, EVIDENCE / "CONFIRMATION_EVIDENCE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
