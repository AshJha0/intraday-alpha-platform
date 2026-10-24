"""F3 acceptance-study runner (``research/v118/f3_accept/PROTOCOL.md``).  UNSIGNED DRAFT.

None of the subcommands has been run for acceptance by the session that wrote this file.

``dry-run``   (anyone) 200 datasets per cell with the dry-run seeds; checks the code against the pilot generators,
              the platform Student-t p and ``known_null.holm``; prints PASS/FAIL, timing and the costed estimate;
              writes NOTHING (output discarded).  Needs no signature.
``bind``      (anyone, clean checkout, CI-lock venv) writes the UNSIGNED prereg through
              ``tools/prereg_bind_identity.py bind``.  Reads no data (there is none).
``run``       (a non-signing actor, after the owner signed and the prereg is committed) refuses unless the prereg is
              signed, its analysis_identity matches this checkout, ``IAP_RUN_ACTOR`` is set and is not the signer,
              no signing key is in the environment, enough RAM is free and no DECISION.json exists.  Then runs the
              12 cells (9 mandatory + 3 a1 sensitivity) on at most 3 worker processes with a checkpoint file per
              cell, then ``analyse``.  Fixed n, no interim looks.
``analyse``   rebuilds DECISION.json / REPORT.md from the cell files (reads no data).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
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

import core  # noqa: E402
import joint_bound  # noqa: E402

DECL = HERE / "declaration.json"
SELF_REL = "research/v118/f3_accept/run_f3_accept.py"
BOUND = [
    SELF_REL,
    "research/v118/f3_accept/core.py",
    "research/v118/f3_accept/joint_bound.py",
    "research/v118/f3_accept/declaration.json",
]
INPUTS = [
    "research/v118/F3_SPEC_DRAFT.md",
    "docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md",
    "research/v118/f3_accept/JOINT_BOUND.md",
    "research/v118/f3_spec/generators.py",
    "research/v118/f3_spec/procedures.py",
]
METRICS = ["fwer_observed", "ucb95_clopper_pearson", "pass_count_threshold", "nan_p_count"]


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


def cells(decl: Mapping[str, Any]) -> list[tuple[str, int, bool]]:
    """(regime, G, mandatory) for every cell, mandatory first."""
    out = [(r, g, True) for r in decl["mandatory"]["regimes"] for g in decl["mandatory"]["G"]]
    out += [(r, g, False) for r in decl["sensitivity"]["regimes"] for g in decl["sensitivity"]["G"]]
    return out


def identity_args(decl: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scripts": list(BOUND),
        "inputs": list(INPUTS),
        "data_versions": ["synthetic-no-data"],
        "family": "F3 acceptance study: P1 Student t(G-1) + Holm, FWER calibration, synthetic regimes a0 b0 b1 (a1 sensitivity), G 5 7 10, K 3 6 16",
        "metrics": list(METRICS),
        "thresholds": dict(decl),
    }


# ------------------------------------------------------------------ dry run --
def _check_against_pilot(decl: Mapping[str, Any]) -> list[str]:
    """Self-checks that need no signature; return a list of failure strings."""
    import numpy as np

    bad: list[str] = []
    sys.path.insert(0, str(ROOT / "research" / "v118" / "f3_spec"))
    import generators as pg  # noqa: PLC0415 (pilot generators, bound as an input)
    from iap.research import known_null as kn  # noqa: PLC0415

    pilot_decl = {
        "generators": decl["generators"],
        "seeds": {"base": 0, "dry_run_base": 0, "regime_index": decl["seeds"]["regime_index"]},
    }
    for reg in ("a0", "a1", "b0", "b1"):
        for g in (5, 7, 10):
            rng = kn.SplitMix64(pg.seed_for(reg, g))
            mine = core.draw_chunk(pilot_decl, reg, g, 300, rng)
            if not np.array_equal(mine, pg.generate(reg, g, 300)):
                bad.append(f"generator {reg} G{g} differs from the pilot generator")
    x = pg.generate("b1", 7, 40)  # (n, g, 16)
    p = core.p1_pvalues(x)
    for i in range(0, 40, 7):
        for j in range(16):
            ref = kn.cluster_inference(x[i, :, j], 0.025)["p_student_t"]
            if abs(ref - p[i, j]) > 1e-12:
                bad.append("P1 p differs from the platform Student-t p")
    rng2 = np.random.default_rng(1)
    for _ in range(3000):
        for k in decl["families"]:
            q = rng2.random(k) ** 3
            adj = kn.holm(list(q))
            if (min(adj) <= decl["alpha_op"]) != (q.min() <= decl["alpha_op"] / k):
                bad.append(f"Holm identity fails K{k}")
                return bad
    return bad


def cmd_dry_run(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    n = int(decl["dry_run"]["n"])
    bad = _check_against_pilot(decl)
    jb = (HERE / "JOINT_BOUND.md").read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10))
    if jb != joint_bound.render(joint_bound.table(decl)):
        bad.append("JOINT_BOUND.md is not what joint_bound.py produces")
    t0 = time.perf_counter()
    per_cell: list[tuple[str, int, float]] = []
    for reg, g, _ in cells(decl):
        t1 = time.perf_counter()
        r = core.run_cell(decl, reg, g, n, dry=True)  # result discarded
        if r["nan_p"]:
            bad.append(f"{reg} G{g}: NaN p in the dry run")
        per_cell.append((reg, g, time.perf_counter() - t1))
    km = core.k_max(int(decl["n_per_cell"]), float(decl["fwer_target"]), float(decl["ucb_conf"]))
    sec = sum(t for _, _, t in per_cell) / n * int(decl["n_per_cell"])
    print(
        f"[dry-run] {len(per_cell)} cells x {n} datasets in {time.perf_counter() - t0:.1f}s (output discarded)"
    )
    print(f"[dry-run] pass threshold at n={decl['n_per_cell']}: count <= {km}")
    print(f"[dry-run] estimated CPU for {len(per_cell)} cells x {decl['n_per_cell']} datasets: "
          f"{sec:.0f} s = {sec / 3600:.3f} machine hours single-core")  # fmt: skip
    if bad:
        print("[dry-run] FAIL: " + "; ".join(bad))
        return 1
    print("[dry-run] PASS")
    return 0


# --------------------------------------------------------------------- run --
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
    }


def _cell_job(decl: dict, reg: str, g: int, out: str, rec: dict) -> str:
    t0 = time.perf_counter()
    r = core.run_cell(decl, reg, g, int(decl["n_per_cell"]))
    r["cpu_wall_s"] = time.perf_counter() - t0
    r["prereg_sha256"] = rec["prereg_sha256"]
    write_json(Path(out) / f"cell_{reg}_G{g}.json", r)
    return f"{reg}_G{g}"


def cmd_run(a: argparse.Namespace) -> int:
    from iap.marketdata.runmem import available_ram_gb

    decl = load_decl(a.decl)
    prereg = (ROOT / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl)
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
    for reg, g, _ in cells(decl):
        f = out / f"cell_{reg}_G{g}.json"
        if f.exists():
            if (
                json.loads(f.read_text(encoding="utf-8")).get("prereg_sha256")
                != rec["prereg_sha256"]
            ):
                raise Refused(f"{f.name} was produced under a different prereg")
            print(f"[f3acc] resume: {f.name} complete", flush=True)
        else:
            todo.append((reg, g))
    write_json(out / "run_record.json", {**rec, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                         "workers": min(3, int(decl["workers"])), "pid": os.getpid()})  # fmt: skip
    workers = min(3, int(decl["workers"]))
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_cell_job, decl, r, g, str(out), rec) for r, g in todo]
        for f in cf.as_completed(futs):
            print(f"[f3acc] done {f.result()}", flush=True)
    return cmd_analyse(argparse.Namespace(decl=a.decl, out=a.out))


# ----------------------------------------------------------------- analyse --
def decide(
    decl: Mapping[str, Any], res: Mapping[tuple[str, int], Mapping[str, Any]]
) -> dict[str, Any]:
    n, tgt, conf = int(decl["n_per_cell"]), float(decl["fwer_target"]), float(decl["ucb_conf"])
    km = core.k_max(n, tgt, conf)
    evals, all_pass = [], True
    for reg, g, mand in cells(decl):
        r = res.get((reg, g))
        if r is None:
            raise Refused(f"missing cell {reg} G{g}")
        for k in decl["families"]:
            c = int(r["rejections"][f"K{k}"])
            ucb = core.cp_upper(c, n, conf)
            ok = ucb <= tgt
            if mand and not ok:
                all_pass = False
            evals.append({"regime": reg, "G": g, "K": k, "mandatory": mand, "count": c, "n": n,
                          "fwer_observed": c / n, "ucb95": ucb, "pass": ok, "nan_p": r["nan_p"]})  # fmt: skip
    return {"overall_pass_all_27_mandatory": all_pass, "k_max": km, "alpha_op": decl["alpha_op"],
            "fwer_target": tgt, "evaluations": evals,
            "scope": "calibration under the declared synthetic regimes only; no real-data claim"}  # fmt: skip


def report(dec: Mapping[str, Any]) -> str:
    L = [f"# F3 acceptance study result\n\nOverall (27 mandatory evaluations): **{'PASS' if dec['overall_pass_all_27_mandatory'] else 'FAIL'}**. "
         f"alpha_op {dec['alpha_op']}, FWER target {dec['fwer_target']}, pass iff count <= {dec['k_max']}. {dec['scope']}.\n",
         "| regime | G | K | mandatory | count | FWER observed | UCB95 | pass |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for e in dec["evaluations"]:
        L.append(f"| {e['regime']} | {e['G']} | {e['K']} | {e['mandatory']} | {e['count']} | {e['fwer_observed']:.5f} | "
                 f"{e['ucb95']:.5f} | {e['pass']} |")  # fmt: skip
    return "\n".join(L) + "\n"


def cmd_analyse(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    out = Path(a.out)
    res = {}
    for reg, g, _ in cells(decl):
        f = out / f"cell_{reg}_G{g}.json"
        if f.exists():
            res[(reg, g)] = json.loads(f.read_text(encoding="utf-8"))
    dec = decide(decl, res)
    write_json(out / "DECISION.json", dec)
    (out / "REPORT.md").write_text(report(dec), encoding="utf-8", newline="\n")
    print("[f3acc] overall", "PASS" if dec["overall_pass_all_27_mandatory"] else "FAIL", flush=True)
    return 0


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
