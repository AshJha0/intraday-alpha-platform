"""R3 adversarial Monte Carlo: run the cells of :mod:`r3_generators` through the gate.

Every cell's panel goes through the vectorised statistic of the canonical gate
(``iap.validation.confirmatory``): claimed-domain cells through ``decide_many`` (the
gate admits the declaration and decides every dataset), candidate and stress cells
through ``inference_many`` (R0 section 5: their counts are reported although the gate
refuses or has not yet validated the declaration).  The test file
``python/tests/test_confirmatory_adversarial.py`` asserts the acceptance rules; this
module computes and writes the evidence::

    PYTHONPATH=python/src python python/tests/spec/r3_suite.py --write   # minutes, one core
    PYTHONPATH=python/src python python/tests/spec/r3_suite.py --check   # recompute, compare k

Output: ``research/validation/R0_RESULTS.json`` (R0 section 8: per cell seed, n, k,
estimate, bounds, half-width, procedure, domain class, result, the versions).  The
seeds are fixed by R0; a re-run with other seeds would be a new R0 revision.
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

import r3_generators as gen  # noqa: E402
from iap.validation import confirmatory as c  # noqa: E402

RESULTS = REPO / "research" / "validation" / "R0_RESULTS.json"
CHUNK = 4_000  # datasets per vectorised call (memory: (4000, 90, 16) doubles = 46 MB)
FAMILY = tuple(f"H{j:02d}" for j in range(gen.K))


def design_for(cell: gen.Cell, *, family: tuple[str, ...] = FAMILY) -> dict[str, Any]:
    """The registered design the gate would read for this cell: the synthetic origin,
    the generator and its parameters; domain and tier from the cell's class."""
    proc = cell.procedure
    d: dict[str, Any] = {
        "family": list(family),
        "procedure": proc,
        "G": cell.G,
        "alpha_op": gen.ALPHA_OP,
        "power_table": {"G_min": 10},
        "T0": "2026-03-01",
        "registry": {"window_state": "fresh", "confirmation_dataset_looks": 0},
        "data": {"origin": "synthetic", "generator": cell.generator, **cell.params},
    }
    if proc == "P-BLK":
        d["domain"] = "D-BLK"
        d["tier"] = "1a"
        d["dependence"] = {
            "rho": cell.params.get("rho", 0.0),
            "evidence": {"generator": cell.generator, "block_length": c.BLOCK_LENGTH},
        }
    else:
        d["domain"] = "D-IND"
        g = cell.generator
        if g.startswith("normal_heteroskedastic"):
            d["tier"] = "1b"
        elif g.startswith(("t3_", "lognormal_")):
            d["tier"] = "2"
        else:
            d["tier"] = "1a"
    if cell.case == "R3-E":
        d["m_min"] = {"usable_units": 60, "coverage_time_min": 234.0, "coverage_grid": 0.5}
    return d


def _chunks(n: int):
    for lo in range(0, n, CHUNK):
        yield lo, min(n, lo + CHUNK)


def _per_hypothesis_rejections(p: np.ndarray, n_rej: np.ndarray) -> np.ndarray:
    """Holm's rejected set per dataset from the p matrix and the rejection counts:
    hypothesis j is rejected iff its rank among the dataset's p-values is below the
    count (the leading run of the sorted comparisons)."""
    pf = np.where(np.isfinite(p), p, 1.0)
    ranks = np.argsort(np.argsort(pf, axis=1, kind="stable"), axis=1, kind="stable")
    return ranks < n_rej[:, None]


def run_cell(cell: gen.Cell) -> dict[str, Any]:
    """One cell: generate, decide, count.  Returns the R0 section 8 record."""
    t0 = time.perf_counter()
    data = gen.generate(cell)
    panel = data["panel"]
    design = design_for(cell)
    n = cell.n
    rec: dict[str, Any] = {
        "id": cell.id,
        "case": cell.case,
        "cell_index": cell.index,
        "name": cell.name,
        "seed": int(cell.seed),
        "kind": cell.kind,
        "procedure": cell.procedure,
        "generator": cell.generator,
        "params": dict(cell.params),
        "G": cell.G,
        "K": gen.K,
        "n": n,
        "domain_class": cell.domain_class,
        "note": cell.note,
    }
    if cell.case == "R3-D":
        return _run_selection(cell, data, rec, t0)
    sessions = data["sessions"]
    counts = np.empty(n, dtype=int)
    hyp_rej = np.zeros((n, gen.K), dtype=bool) if cell.kind == "power" else None
    gate_outcome = None
    for lo, hi in _chunks(n):
        if cell.domain_class == "claimed":
            res = c.decide_many(design, panel[lo:hi], root=REPO, sessions_meta=sessions)
            if res["outcome"] != "per-dataset":
                raise RuntimeError(f"{cell.id}: the gate refused a claimed cell: {res['outcome']}")
            gate_outcome = "admitted"
        else:
            res = c.inference_many(design, panel[lo:hi])
            gate_outcome = c.decide_many(
                design, panel[lo : lo + 2], root=REPO, sessions_meta=sessions
            )["outcome"]
            if gate_outcome == "per-dataset":
                gate_outcome = "admitted"
        counts[lo:hi] = res["n_rejected"]
        if hyp_rej is not None:
            hyp_rej[lo:hi] = _per_hypothesis_rejections(res["p"], res["n_rejected"])
    rec["gate_outcome"] = gate_outcome
    if cell.kind == "power":
        j = int(cell.params["planted_hypothesis"])
        det = int(hyp_rej[:, j].sum())
        rec.update(
            planted={
                "hypothesis": FAMILY[j],
                "effect_sd_units": gen.planted_effect(cell),
                "theoretical_power": cell.params["theoretical_power"],
                "convention": "one-sided Student t at alpha_op / K (the Holm floor), G - 1 df",
            },
            detections=det,
            estimate=det / n,
            lower95=gen.clopper_pearson_lower(det, n),
            threshold={"detections_min": gen.POWER_PASS_DETECTIONS},
            result="pass" if det >= gen.POWER_PASS_DETECTIONS else "fail",
            false_rejections_of_nulls=int(hyp_rej.sum() - det),
        )
    else:
        k = int((counts > 0).sum())
        rec.update(_size_fields(k, n, cell.domain_class))
    if "dropped_positions" in data:
        rec["dropped_positions"] = data["dropped_positions"]
        rec["sessions_attempted"] = len(sessions)
    rec["seconds"] = round(time.perf_counter() - t0, 2)
    return rec


def _size_fields(k: int, n: int, domain_class: str) -> dict[str, Any]:
    est = k / n
    out = {
        "k": k,
        "estimate": est,
        "upper95": gen.clopper_pearson_upper(k, n),
        "lower95": gen.clopper_pearson_lower(k, n),
        "half_width": 1.959963984540054 * math.sqrt(max(est * (1 - est), 0.0) / n),
        "threshold": {"k_max": gen.SIZE_PASS_K, "alpha_op": gen.ALPHA_OP, "claim_fwer": 0.025},
    }
    if domain_class in ("claimed", "candidate"):
        out["result"] = "pass" if k <= gen.SIZE_PASS_K else "fail"
    else:
        out["result"] = "reported"  # stress: never fails CI
    return out


def _run_selection(cell: gen.Cell, data: dict[str, Any], rec: dict[str, Any], t0: float) -> dict:
    """R3-D (i) clean and (ii) contaminated selection on the same streams."""
    sel = int(cell.params["select"])
    cal, con = data["calibration"], data["panel"]
    n = cell.n
    fam = tuple(f"S{i + 1}" for i in range(sel))
    design = design_for(cell, family=fam)
    # (i) clean: the best `sel` candidates by calibration mean, tested on the
    # independent confirmation panel
    order = np.argsort(-cal.mean(axis=1), axis=1, kind="stable")[:, :sel]
    clean = np.take_along_axis(con, order[:, None, :], axis=2)
    counts = np.empty(n, dtype=int)
    for lo, hi in _chunks(n):
        res = c.decide_many(design, clean[lo:hi], root=REPO, sessions_meta=data["sessions"])
        if res["outcome"] != "per-dataset":
            raise RuntimeError(f"{cell.id}: refused: {res['outcome']}")
        counts[lo:hi] = res["n_rejected"]
    k_clean = int((counts > 0).sum())
    rec.update(_size_fields(k_clean, n, "claimed"))
    rec["gate_outcome"] = "admitted"
    rec["K"] = sel
    rec["selection"] = {
        "candidates": gen.K,
        "registered": sel,
        "rule": "largest calibration means",
    }
    # (ii) contaminated: selection and test on the confirmation panel itself
    order2 = np.argsort(-con.mean(axis=1), axis=1, kind="stable")[:, :sel]
    cont = np.take_along_axis(con, order2[:, None, :], axis=2)
    counts2 = np.empty(n, dtype=int)
    for lo, hi in _chunks(n):
        counts2[lo:hi] = c.inference_many(design, cont[lo:hi])["n_rejected"]
    k_cont = int((counts2 > 0).sum())
    contaminated_design = {
        **design,
        "registry": {"window_state": "fresh", "confirmation_dataset_looks": gen.K},
    }
    refusal = c.decide_many(
        contaminated_design, cont[:2], root=REPO, sessions_meta=data["sessions"]
    )
    rec["contaminated"] = {
        **_size_fields(k_cont, n, "stress"),
        "note": "R3-D (ii): selection and test on one dataset; inflation recorded; "
        "the gate refuses the declaration",
        "gate_outcome": refusal["outcome"],
    }
    spent = {**design, "registry": {"window_state": "spent", "confirmation_dataset_looks": 0}}
    rec["spent_window"] = {
        "gate_outcome": c.decide_many(spent, con[:2], root=REPO, sessions_meta=data["sessions"])[
            "outcome"
        ],
        "note": "R3-D (iii): repeated look on a spent window, refused before any statistic",
    }
    rec["seconds"] = round(time.perf_counter() - t0, 2)
    return rec


def versions() -> dict[str, Any]:
    import scipy

    try:
        import statsmodels

        sm = statsmodels.__version__
    except ImportError:
        sm = None
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "statsmodels": sm,
        "platform": platform.platform(),
    }


def summarise(cells: list[dict[str, Any]]) -> dict[str, Any]:
    size = [r for r in cells if r["kind"] == "size"]
    claimed = [r for r in size if r["domain_class"] == "claimed"]
    candidate = [r for r in size if r["domain_class"] == "candidate"]
    stress = [r for r in size if r["domain_class"] == "stress"]
    power = [r for r in cells if r["kind"] == "power"]
    return {
        "claimed_size_cells": len(claimed),
        "claimed_size_pass": sum(r["result"] == "pass" for r in claimed),
        "claimed_max_k": max(r["k"] for r in claimed),
        "candidate_cells": len(candidate),
        "candidate_pass": sum(r["result"] == "pass" for r in candidate),
        "dblk_validated": len(candidate) == 12 and all(r["result"] == "pass" for r in candidate),
        "stress_cells": len(stress),
        "stress_k": {r["id"]: r["k"] for r in stress},
        "power_cells": len(power),
        "power_pass": sum(r["result"] == "pass" for r in power),
        "selection_clean_k": next((r["k"] for r in cells if r["case"] == "R3-D"), None),
        "selection_contaminated_k": next(
            (r["contaminated"]["k"] for r in cells if r["case"] == "R3-D"), None
        ),
    }


def run_all(verbose: bool = True) -> dict[str, Any]:
    cells = []
    for cell in gen.CELLS:
        rec = run_cell(cell)
        cells.append(rec)
        if verbose:
            what = f"detections {rec['detections']}" if rec["kind"] == "power" else f"k {rec['k']}"
            print(
                f"{rec['id']:<48} {what:<18} {rec['result']:<8} {rec['seconds']:>7.1f}s", flush=True
            )
    summary = summarise(cells)
    return {
        "spec": "python/tests/spec/CONFIRMATORY_TEST_SPEC.md (R0 revision 4, frozen 2026-10-24)",
        "generator": "python/tests/spec/r3_generators.py",
        "runner": "python/tests/spec/r3_suite.py",
        "gate": "iap.validation.confirmatory (decide_many for claimed cells; inference_many for candidate and stress cells)",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "versions": versions(),
        "thresholds": {
            "alpha_op": gen.ALPHA_OP,
            "claim_fwer": 0.025,
            "size": {"n": gen.N_SIZE, "k_max": gen.SIZE_PASS_K},
            "power": {"n": gen.N_POWER, "detections_min": gen.POWER_PASS_DETECTIONS},
            "seed_rule": "20301000 + 100 * case_index + cell_index",
            "case_index": gen.CASE_INDEX,
        },
        "summary": summary,
        "cells": cells,
    }


def write(doc: dict[str, Any], path: Path = RESULTS) -> None:
    existing = {}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    if "references" in existing:  # the D-40 statsmodels record is kept across re-runs
        doc["references"] = existing["references"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8", newline="\n"
    )


def compare(doc: dict[str, Any], path: Path = RESULTS) -> list[str]:
    """Differences in the deterministic fields (k / detections per cell) against the
    committed file."""
    committed = json.loads(path.read_text(encoding="utf-8"))
    have = {r["id"]: r for r in committed["cells"]}
    diffs = []
    for r in doc["cells"]:
        o = have.get(r["id"])
        if o is None:
            diffs.append(f"{r['id']}: not in the committed file")
            continue
        for key in ("k", "detections", "seed", "n", "result"):
            if r.get(key) != o.get(key):
                diffs.append(f"{r['id']}: {key} {o.get(key)!r} -> {r.get(key)!r}")
        if "contaminated" in r and r["contaminated"]["k"] != o.get("contaminated", {}).get("k"):
            diffs.append(f"{r['id']}: contaminated k differs")
    return diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--write", action="store_true", help="write research/validation/R0_RESULTS.json"
    )
    ap.add_argument(
        "--check", action="store_true", help="recompute and compare with the committed file"
    )
    ap.add_argument("--out", type=Path, default=RESULTS)
    a = ap.parse_args(argv)
    doc = run_all()
    print(json.dumps(doc["summary"], indent=1))
    if a.check:
        diffs = compare(doc, a.out)
        for d in diffs:
            print(d)
        return 1 if diffs else 0
    if a.write:
        write(doc, a.out)
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
