"""F3 power-diagnostic runner (``research/v118/f3_power/PROTOCOL.md``).  UNSIGNED DRAFT, NON-GATING.

None of the subcommands has been run for the study by the session that wrote this file.

``dry-run``   (anyone) 200 datasets per cell with the dry-run seeds; checks the planted-effect code (d = 0 reproduces
              the acceptance P1 p-values, the added shift is mu = d sigma_RMS / sqrt(G), Holm agrees with
              ``known_null.holm``), runs the integrity band at the widened dry-run tolerance and the whole analysis
              path; prints PASS/FAIL, timing and the costed estimate; writes NOTHING (output discarded).
``bind``      (anyone, clean checkout, CI-lock venv) writes the UNSIGNED prereg through ``tools/prereg_bind_identity.py``.
``run``       (a non-signing actor, after the owner signed and the prereg is committed) refuses unless the prereg is
              signed, its identity matches this checkout, ``IAP_RUN_ACTOR`` is set and is not the signer, no signing key
              is in the environment, enough RAM is free and no DECISION.json exists.  Then runs the 9 stream cells on at
              most 3 worker processes with a checkpoint per cell, then ``analyse``.  Fixed n, no interim looks.
``analyse``   rebuilds DECISION.json / REPORT.md from the cell files (reads no data).  The programming-integrity band is
              evaluated FIRST; on failure the run is VOID and no power estimate is produced.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import ctypes
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402
import power_core as pc  # noqa: E402
import power_math as pm  # noqa: E402

DECL = HERE / "declaration.json"
SELF_REL = "research/v118/f3_power/run_f3_power.py"
BOUND = [
    SELF_REL,
    "research/v118/f3_power/power_core.py",
    "research/v118/f3_power/power_math.py",
    "research/v118/f3_power/check_freeze.py",
    "research/v118/f3_power/declaration.json",
    "research/v118/f3_accept/core.py",
]
INPUTS = [
    "research/v118/F3_POWER_DIAGNOSTIC_SPEC.md",
    "docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md",
    "docs/release/REVIEWER_F3_POSTEXEC_DECISION_2026-10-24.md",
    "docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md",
    "research/v118/f3_accept/DECISION.json",
]
METRICS = [
    "power_wilson95",
    "mde80_first_crossing",
    "mde80_bootstrap_interval",
    "het_hom_ratio",
    "integrity_band",
]
STREAM_FIELDS = (
    "regime",
    "G",
    "n",
    "seed",
    "chunk",
    "n_chunks",
    "nan_p",
    "counts",
    "null_rejection_datasets",
    "rng_final_state",
    "chunk_counts_sha256",
    "indicators_sha256",
)


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def load_decl(path: Path = DECL) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def cells(decl: Mapping[str, Any]) -> list[tuple[str, int]]:
    return [(r, g) for r in decl["regimes"] for g in decl["G"]]


def available_ram_gb() -> float | None:
    """Available physical memory (Windows GlobalMemoryStatusEx, else /proc); a local copy so that the runner
    imports nothing from the market-data packages."""
    try:
        if sys.platform == "win32":

            class MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return ms.ullAvailPhys / 2**30
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except Exception:  # pragma: no cover - diagnostics only
        return None
    return None


def identity_args(decl: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scripts": list(BOUND),
        "inputs": list(INPUTS),
        "data_versions": ["synthetic-no-data"],
        "family": "F3 power diagnostic (NON-GATING): P1 Student t(G-1) + Holm at alpha_op 0.020, planted shift on one of 16 hypotheses, synthetic regimes a0 b0 b1, G 5 7 10, K 3 6 16; NO market data consumed",
        "metrics": list(METRICS),
        "thresholds": dict(decl),
    }


# ------------------------------------------------------------------ analysis --
def _enc(v: float, grid: Sequence[float]) -> Any:
    return pm.mde_label(v, grid) if v in (pm.LOW, pm.HIGH) else round(float(v), 1)


def bootstrap_cell(decl: Mapping[str, Any], rec: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Paired bootstrap over dataset replications for one cell: the same index set for every d and every K."""
    grid = [float(v) for v in decl["grid"]]
    fams = list(decl["families"])
    b_n = int(decl["bootstrap"]["B"])
    n = int(rec["n"])
    ind = {k: pc.unpack_indicators(rec, k, len(grid)).astype(np.float64) for k in fams}
    rng = pc.SplitMix64(pc.bootstrap_seed(decl, rec["regime"], int(rec["G"])))
    mde = {k: [] for k in fams}
    diff = {k: np.empty((b_n, len(grid) - 1)) for k in fams}
    for b in range(b_n):
        idx = (pc.acc.splitmix_uniforms(rng, n) * n).astype(np.int64)
        w = np.bincount(idx, minlength=n).astype(np.float64)
        for k in fams:
            p = (ind[k] @ w) / n
            mde[k].append(pm.mde_point(grid, p, float(decl["power_target"])))
            diff[k][b] = p[1:] - p[:-1]
    return {k: {"mde": mde[k], "diff": diff[k]} for k in fams}


def integrity(
    decl: Mapping[str, Any], res: Mapping[tuple[str, int], Mapping[str, Any]], tau: float
) -> dict[str, Any]:
    grid = [float(v) for v in decl["grid"]]
    phat = {}
    for g in decl["G"]:
        rec = res[("a0", g)]
        n = int(rec["n"])
        for k in decl["families"]:
            for j, d in enumerate(grid):
                phat[(g, k, d)] = rec["counts"][f"K{k}"][j] / n
    bad = pm.integrity_violations(float(decl["alpha_op"]), phat, tau)
    return {
        "status": "PASS" if not bad else "VOID",
        "regime": decl["integrity"]["regime"],
        "tau": tau,
        "combinations_checked": len(phat),
        "violations": [{"G": g, "K": k, "d": d} for g, k, d in bad],
    }


def analyse_cells(
    decl: Mapping[str, Any],
    res: Mapping[tuple[str, int], Mapping[str, Any]],
    tau: float | None = None,
) -> dict[str, Any]:
    grid = [float(v) for v in decl["grid"]]
    fams = list(decl["families"])
    z = float(decl["interval"]["wilson_z"])
    target = float(decl["power_target"])
    bs = decl["bootstrap"]
    ig = integrity(decl, res, float(decl["integrity"]["tau"]) if tau is None else tau)
    if ig["status"] != "PASS":
        return {
            "status": "VOID_INTEGRITY",
            "integrity": ig,
            "note": "run VOID (programming integrity): no power table, MDE80 or ratio is produced and no estimate from this run may be cited; a corrected package needs the full sign-off route with fresh seeds",
            "non_gating": decl["non_gating"],
        }
    power, mdes, boots = [], [], {}
    point: dict[tuple[str, int, int], tuple[float, list]] = {}
    for reg, g in cells(decl):
        rec = res[(reg, g)]
        n = int(rec["n"])
        boots[(reg, g)] = bootstrap_cell(decl, rec)
        for k in fams:
            cnt = rec["counts"][f"K{k}"]
            ps = [c / n for c in cnt]
            ci = [pm.wilson(c, n, z) for c in cnt]
            for j, d in enumerate(grid):
                power.append({"regime": reg, "G": g, "K": k, "d": d, "count": cnt[j], "n": n, "power": ps[j],
                              "wilson_lo": ci[j][0], "wilson_hi": ci[j][1]})  # fmt: skip
            m = pm.mde_point(grid, ps, target)
            flags = pm.mde_nonmonotone(grid, ps, target)
            point[(reg, g, k)] = (m, flags)
            bl, bh = pm.ordered_percentiles(
                boots[(reg, g)][k]["mde"], float(bs["lo_q"]), float(bs["hi_q"])
            )
            share = pm.numeric_share(boots[(reg, g)][k]["mde"])
            mdes.append({
                "regime": reg, "G": g, "K": k,
                "mde80": _enc(m, grid),
                "nonmonotone": [{"d": a, "power": b} for a, b in flags],
                "bootstrap_lo": _enc(bl, grid), "bootstrap_hi": _enc(bh, grid),
                "bootstrap_numeric_share": share,
                "interval_note": "interval dominated by boundary results" if share < float(bs["min_numeric_share"]) else "",
                "wilson_bracket_lo": _enc(pm.mde_point(grid, [c[1] for c in ci], target), grid),
                "wilson_bracket_hi": _enc(pm.mde_point(grid, [c[0] for c in ci], target), grid),
            })  # fmt: skip
    ratios, mde_ratios, diffs = [], [], []
    for reg in decl["regimes"]:
        if reg == "a0":
            continue
        for g in decl["G"]:
            for k in fams:
                for j, d in enumerate(grid):
                    kb, ka = (
                        res[(reg, g)]["counts"][f"K{k}"][j],
                        res[("a0", g)]["counts"][f"K{k}"][j],
                    )
                    ratios.append({"regime": reg, "G": g, "K": k, "d": d,
                                   **pm.power_ratio(kb, ka, int(res[(reg, g)]["n"]), int(decl["ratio"]["min_count"]), z)})  # fmt: skip
                mb, fb = point[(reg, g, k)]
                ma, fa_ = point[("a0", g, k)]
                row = {"regime": reg, "G": g, "K": k, **pm.mde_ratio(mb, ma, fb, fa_)}
                if row["kind"] == "numeric":
                    rs = []
                    for vb, va in zip(
                        boots[(reg, g)][k]["mde"], boots[("a0", g)][k]["mde"], strict=True
                    ):
                        if vb in (pm.LOW, pm.HIGH) or va in (pm.LOW, pm.HIGH):
                            continue
                        rs.append(vb / va)
                    if len(rs) / len(boots[(reg, g)][k]["mde"]) >= float(bs["min_numeric_share"]):
                        lo, hi = pm.ordered_percentiles(rs, float(bs["lo_q"]), float(bs["hi_q"]))
                        row.update(lo=lo, hi=hi)
                    else:
                        row["interval_note"] = (
                            "interval not reported (fewer than the declared share of numeric replicates)"
                        )
                mde_ratios.append(row)
    for reg, g in cells(decl):
        for k in fams:
            dm = boots[(reg, g)][k]["diff"]
            cnt = res[(reg, g)]["counts"][f"K{k}"]
            n = int(res[(reg, g)]["n"])
            for j in range(len(grid) - 1):
                col = np.sort(dm[:, j])
                lo = col[int(math.floor(float(bs["lo_q"]) * (len(col) - 1)))]
                hi = col[int(math.ceil(float(bs["hi_q"]) * (len(col) - 1)))]
                diffs.append({"regime": reg, "G": g, "K": k, "d_from": grid[j], "d_to": grid[j + 1],
                              "difference": (cnt[j + 1] - cnt[j]) / n, "paired_lo": float(lo), "paired_hi": float(hi)})  # fmt: skip
    side = [
        {"regime": reg, "G": g, "K": k, "d": d, "datasets_with_a_null_rejection": res[(reg, g)]["null_rejection_datasets"][f"K{k}"][j],
         "n": int(res[(reg, g)]["n"])}
        for reg, g in cells(decl) for k in fams for j, d in enumerate(grid)
    ]  # fmt: skip
    return {
        "status": "COMPLETE",
        "integrity": ig,
        "non_gating": decl["non_gating"],
        "scope": "estimated power under the declared synthetic generators only; no market data consumed; no real-data, practical-power, alpha or trading claim",
        "crn_disclosure": "estimates at different d within a (regime, G) cell share the same null datasets and are positively correlated; they are never treated as independent; differences use the paired bootstrap",
        "power": power,
        "mde80": mdes,
        "power_ratio_het_over_hom": ratios,
        "mde80_ratio_het_over_hom": mde_ratios,
        "adjacent_power_differences": diffs,
        "null_rejection_side_table": side,
    }


def _fmt(v: Any) -> str:
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def report(dec: Mapping[str, Any]) -> str:
    if dec["status"] != "COMPLETE":
        bad = ", ".join(f"G{v['G']} K{v['K']} d{v['d']}" for v in dec["integrity"]["violations"])
        return (
            "# F3 power diagnostic: RUN VOID (programming integrity)\n\n"
            f"The a0 integrity band failed for: {bad}. {dec['note']}.\n\n{dec['non_gating']}.\n"
        )
    lines = [
        "# F3 power diagnostic result (NON-GATING)",
        "",
        f"**{dec['non_gating']}.**",
        "",
        f"Scope: {dec['scope']}.",
        f"Programming-integrity band (a0, {dec['integrity']['combinations_checked']} combinations, tau {dec['integrity']['tau']}): {dec['integrity']['status']}.",
        f"Common random numbers: {dec['crn_disclosure']}.",
        "",
        "## MDE80 (first-crossing interpolation, never extrapolated; one decimal; paired-bootstrap interval, Wilson bracket secondary)",
        "",
        "| regime | G | K | MDE80 | bootstrap 95 % | Wilson bracket | non-monotone | note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in dec["mde80"]:
        lines.append(
            f"| {r['regime']} | {r['G']} | {r['K']} | {r['mde80']} | {r['bootstrap_lo']} to {r['bootstrap_hi']} | "
            f"{r['wilson_bracket_lo']} to {r['wilson_bracket_hi']} | {'yes' if r['nonmonotone'] else 'no'} | {r['interval_note']} |"
        )
    lines += [
        "",
        "## MDE80 ratio, heterogeneous over homogeneous",
        "",
        "| regime | G | K | kind | ratio | interval | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in dec["mde80_ratio_het_over_hom"]:
        iv = f"{r['lo']:.2f} to {r['hi']:.2f}" if "lo" in r else ""
        lines.append(
            f"| {r['regime']} | {r['G']} | {r['K']} | {r['kind']} | {_fmt(r.get('ratio', ''))} | {iv} | {r.get('reason', r.get('interval_note', ''))} |"
        )
    lines += [
        "",
        "## Power, every estimate with its Wilson 95 % interval (324)",
        "",
        "| regime | G | K | d | count | n | power | Wilson 95 % |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in dec["power"]:
        lines.append(
            f"| {r['regime']} | {r['G']} | {r['K']} | {r['d']} | {r['count']} | {r['n']} | {r['power']:.4f} | {r['wilson_lo']:.4f} to {r['wilson_hi']:.4f} |"
        )
    lines += [
        "",
        "## Power ratio, heterogeneous over homogeneous (defined only when both counts are at least the declared minimum)",
        "",
        "| regime | G | K | d | ratio | interval | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in dec["power_ratio_het_over_hom"]:
        if r["defined"]:
            lines.append(
                f"| {r['regime']} | {r['G']} | {r['K']} | {r['d']} | {r['ratio']:.3f} | {r['lo']:.3f} to {r['hi']:.3f} | |"
            )
        else:
            lines.append(
                f"| {r['regime']} | {r['G']} | {r['K']} | {r['d']} | not reported | | {r['reason']} (counts {r['k_b']} / {r['k_a']}) |"
            )
    lines += [
        "",
        "Adjacent-d power differences with paired-bootstrap intervals and the null-rejection side table are in DECISION.json.",
        "",
    ]
    return "\n".join(lines) + "\n"


def cmd_analyse(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    out = Path(a.out)
    res = {}
    for reg, g in cells(decl):
        f = out / f"cell_{reg}_G{g}.json"
        if not f.exists():
            raise Refused(f"missing cell {reg} G{g}")
        res[(reg, g)] = json.loads(f.read_text(encoding="utf-8"))
    dec = analyse_cells(decl, res)
    write_json(out / "DECISION.json", dec)
    (out / "REPORT.md").write_text(report(dec), encoding="utf-8", newline="\n")
    print("[f3pow] integrity", dec["integrity"]["status"], "status", dec["status"], flush=True)
    return 0


# ------------------------------------------------------------------ dry run --
def _self_checks(decl: Mapping[str, Any]) -> list[str]:
    bad: list[str] = []
    from iap.research import known_null as kn  # noqa: PLC0415

    for reg in decl["regimes"]:
        for g in decl["G"]:
            rng = pc.SplitMix64(pc.seed_for(decl, reg, g, True))
            x = pc.acc.draw_chunk(decl, reg, g, 60, rng)
            if not np.array_equal(pc.plant(decl, x, reg, g, 0.0), x):
                bad.append(f"{reg} G{g}: d = 0 changes the data")
            p0 = pc.acc.p1_pvalues(pc.plant(decl, x, reg, g, 0.0))
            if not np.array_equal(p0, pc.acc.p1_pvalues(x)):
                bad.append(f"{reg} G{g}: d = 0 p-values differ from the acceptance P1")
            y = pc.plant(decl, x, reg, g, 4.0)
            delta = y - x
            mu = 4.0 * pc.sigma_rms(decl, reg, g) / math.sqrt(g)
            pi = int(decl["planted_index"])
            if not np.allclose(delta[:, :, pi], mu, rtol=0, atol=1e-12) or np.any(
                np.delete(delta, pi, axis=2) != 0
            ):
                bad.append(
                    f"{reg} G{g}: the shift is not mu = d sigma_RMS / sqrt(G) on the planted hypothesis only"
                )
    rng2 = np.random.default_rng(1)
    alpha = float(decl["alpha_op"])
    for _ in range(2000):
        for k in decl["families"]:
            q = rng2.random(k) ** 3
            adj = kn.holm(list(q))
            planted, anynull = pm.holm_planted_rejected(q[None, :], alpha, k)
            rej = [a_ <= alpha for a_ in adj]
            if bool(planted[0]) != rej[0] or bool(anynull[0]) != any(rej[1:]):
                bad.append(f"Holm identity fails K{k}")
                return bad
    return bad


def cmd_dry_run(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    n = int(decl["dry_run"]["n"])
    bad = _self_checks(decl)
    t0 = time.perf_counter()
    res, per_cell = {}, []
    for reg, g in cells(decl):
        t1 = time.perf_counter()
        r = pc.run_cell(decl, reg, g, n, dry=True)  # result discarded
        if r["nan_p"]:
            bad.append(f"{reg} G{g}: NaN p in the dry run")
        res[(reg, g)] = r
        per_cell.append(time.perf_counter() - t1)
    t2 = time.perf_counter()
    dec = analyse_cells(
        decl, res, tau=float(decl["integrity"]["tau_dry"])
    )  # gross code errors only
    t_an = time.perf_counter() - t2
    if dec["status"] != "COMPLETE":
        bad.append("dry-run integrity band failed at the widened tolerance")
    sec = sum(per_cell) / n * int(decl["n_per_cell"])
    print(
        f"[dry-run] {len(per_cell)} cells x {n} datasets in {time.perf_counter() - t0:.1f}s (output discarded)"
    )
    print(
        f"[dry-run] analysis path (bootstrap B={decl['bootstrap']['B']}) {t_an:.1f}s on the dry-run cells"
    )
    print(
        f"[dry-run] estimated CPU for {len(per_cell)} cells x {decl['n_per_cell']} datasets: {sec:.0f} s single-core, plus analysis"
    )
    if bad:
        print("[dry-run] FAIL: " + "; ".join(bad))
        return 1
    print("[dry-run] PASS")
    return 0


# --------------------------------------------------------------------- run --
def identity_sha256(block: Mapping[str, Any]) -> str:
    """Digest of the COMPLETE signed analysis_identity block (scripts, inputs, environment, commit, thresholds)."""
    return hashlib.sha256(
        json.dumps(block, sort_keys=True, ensure_ascii=True).encode("ascii")
    ).hexdigest()


def verify(prereg: Path, decl: Mapping[str, Any], root: Path = ROOT) -> dict[str, Any]:
    from iap.agents.analysis_identity import (
        IdentityError,
        SignatureError,
        require_independent_runner,
        verify_identity,
        verify_signed_document,
    )

    if not prereg.is_file():
        raise Refused(f"{prereg} does not exist: bind and sign the prereg first")
    doc = json.loads(prereg.read_text(encoding="utf-8"))
    try:
        verify_signed_document(root, doc)
    except SignatureError as exc:
        raise Refused(f"prereg not usable: {exc}") from exc
    args = identity_args(decl)
    try:
        verify_identity(root, doc.get("analysis_identity"), data_versions=args["data_versions"],
                        family=args["family"], metrics=args["metrics"], thresholds=args["thresholds"])  # fmt: skip
        actor = require_independent_runner(doc)
    except IdentityError as exc:
        raise Refused(str(exc)) from exc
    return {
        "prereg": prereg.resolve().relative_to(root.resolve()).as_posix(),
        "prereg_sha256": hashlib.sha256(prereg.read_bytes()).hexdigest(),
        "runner_actor": actor,
        "signer": doc["signature"]["maintainer"],
        "commit_bound": doc["analysis_identity"]["commit"],
        "identity_sha256": identity_sha256(doc["analysis_identity"]),
        "market_data_consumed": False,
    }


def validate_cell(decl: Mapping[str, Any], rec: Mapping[str, Any], path: Path) -> None:
    """Resume check for a finished cell: same prereg, same COMPLETE signed identity, and the same deterministic stream,
    established by recomputing the cell and comparing counts, final RNG state, chunk digest and indicator digest."""
    old = json.loads(path.read_text(encoding="utf-8"))
    for key in ("prereg_sha256", "identity_sha256"):
        if old.get(key) != rec[key]:
            raise Refused(
                f"{path.name}: {key} differs from the verified signed prereg; refusing to resume"
            )
    new = pc.run_cell(decl, old.get("regime"), old.get("G"), int(decl["n_per_cell"]))
    bad = [k for k in STREAM_FIELDS if old.get(k) != new[k]]
    if bad:
        raise Refused(f"{path.name}: the recomputed stream differs in {bad}; refusing to resume")


def _cell_job(decl: dict, reg: str, g: int, out: str, rec: dict) -> str:
    t0 = time.perf_counter()
    r = pc.run_cell(decl, reg, g, int(decl["n_per_cell"]))
    r["cpu_wall_s"] = time.perf_counter() - t0
    r["prereg_sha256"] = rec["prereg_sha256"]
    r["identity_sha256"] = rec["identity_sha256"]
    write_json(Path(out) / f"cell_{reg}_G{g}.json", r)
    return f"{reg}_G{g}"


def cmd_run(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    prereg = (root / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl, root)
    ram = available_ram_gb()
    if ram is not None and ram < float(decl["stop_rules"]["min_free_ram_gb"]):
        raise Refused(f"available RAM {ram:.1f} GB < declared minimum")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "DECISION.json").exists():
        raise Refused(
            f"{out} holds a DECISION.json: one run only; a parameter change is a new declaration"
        )
    todo = []
    for reg, g in cells(decl):
        f = out / f"cell_{reg}_G{g}.json"
        if f.exists():
            validate_cell(decl, rec, f)
            print(f"[f3pow] resume: {f.name} complete and re-verified", flush=True)
        else:
            todo.append((reg, g))
    write_json(out / "run_record.json", {**rec, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                         "workers": min(3, int(decl["workers"])), "pid": os.getpid()})  # fmt: skip
    workers = min(3, int(decl["workers"]))
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_cell_job, decl, r, g, str(out), rec) for r, g in todo]
        for f in cf.as_completed(futs):
            print(f"[f3pow] done {f.result()}", flush=True)
    return cmd_analyse(argparse.Namespace(decl=a.decl, out=a.out))


# -------------------------------------------------------------------- bind --
def cmd_bind(a: argparse.Namespace) -> int:
    from iap.agents.fingerprint import check_registration_env

    check_registration_env(require_ci_env=True)
    decl = load_decl(a.decl)
    args = identity_args(decl)
    sys.path.insert(0, str(ROOT / "tools"))
    import prereg_bind_identity as pbi  # type: ignore[import-not-found]

    argv: list[str] = [
        "--root",
        str(a.root),
        "bind",
        "--protocol",
        decl["protocol"],
        "--out",
        decl["prereg"],
    ]
    argv += ["--scripts", *args["scripts"], "--inputs", *args["inputs"]]
    argv += ["--data-version", *args["data_versions"], "--family", args["family"]]
    argv += ["--metric", *args["metrics"], "--thresholds", json.dumps(args["thresholds"])]
    if a.dry_run:
        argv.append("--dry-run")
    return int(pbi.main(argv))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--decl", type=Path, default=DECL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dry-run", help="200 datasets per cell, output discarded")
    b = sub.add_parser("bind", help="write the UNSIGNED prereg (clean checkout, CI-lock venv)")
    b.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run", help="verify the signed prereg, run all cells, analyse")
    r.add_argument("--prereg", type=Path, default=None)
    r.add_argument("--out", type=Path, default=HERE)
    n = sub.add_parser("analyse", help="DECISION.json + REPORT.md from the cell files")
    n.add_argument("--out", type=Path, default=HERE)
    a = ap.parse_args(argv)
    return {"dry-run": cmd_dry_run, "bind": cmd_bind, "run": cmd_run, "analyse": cmd_analyse}[
        a.cmd
    ](a)


if __name__ == "__main__":
    raise SystemExit(main())
