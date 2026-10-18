"""REL-04b B8 v2 runner (``research/execution/b8v2/PREDECLARATION.md``).

Three subcommands; none of them is run by the session that wrote this file.

``bind`` (anyone, clean checkout of the merged commit) writes the UNSIGNED
prereg ``research/preregs/b8v2_prereg.json`` through
``tools/prereg_bind_identity.py bind`` with exactly the identity arguments
:func:`identity_args` derives from ``predeclaration.json``, so what is signed
and what the runner checks cannot drift apart.  Refuses while a consolidated
ADV value is null.

``run`` (a non-signing actor, after the prereg is signed and committed)
refuses unless, in this order: the prereg is signed under the trust root; its
``analysis_identity`` matches this checkout (scripts, ``python/src/iap`` tree,
commit ancestry, data versions, family, metrics, thresholds);
``IAP_RUN_ACTOR`` is set and is not the signer and no signing key is in the
environment; every dataset directory on disk has the declared
``dataset_version`` and passes the holdout registry under this prereg id.
Then it suspends chain B (the ``guard_chain_b`` mechanism: every python whose
command line runs ``process-iex-day``), runs AAPL then MSFT one process at a
time (headline, then the declared sensitivities), resumes chain B whatever
happens, and writes ``DECISION.json`` and ``REPORT.md`` here.

``analyse`` rebuilds ``DECISION.json`` / ``REPORT.md`` from the benchmark
JSONs already in this directory (reproduction step; reads no market data).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

DECL = HERE / "predeclaration.json"
SELF_REL = "research/execution/b8v2/run_b8v2.py"
DEFAULT_DATA_ROOT = ROOT / "data" / "real"
METRICS = (
    "trading_cost_per_filled_share_bps",
    "completion_charge_bps",
    "fill_rate",
    "shortfall_per_target_share_bps",
    "cost_per_filled_share_bps",
)
CHAIN_PATTERN = "process-iex-day"


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def load_decl(path: Path = DECL) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def bound_files(decl: Mapping[str, Any], root: Path = ROOT) -> list[str]:
    """Every file the run's result depends on besides ``python/src/iap``."""
    cal = decl["headline"]["calibration_dir"]
    cal_files = sorted(p.relative_to(root).as_posix() for p in (root / cal).glob("*.json"))
    b = decl["benchmark"]
    return sorted(
        {
            SELF_REL,
            "research/execution/b8v2/predeclaration.json",
            b["venues"],
            b["consolidated_adv"],
            *cal_files,
        }
    )


def identity_args(decl: Mapping[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """The ``analysis_identity`` arguments: the binder and the runner both call this."""
    st = decl["statistics"]
    family = (
        f"REL-04b B8 v2: {st['family']} (K={st['family_k']}); baseline "
        f"{decl['baseline_policy']}; evaluate {','.join(decl['headline']['evaluate_sessions'])}"
    )
    thresholds = {
        "symbols": list(decl["symbols"]),
        "policies": list(decl["policies"]),
        "baseline_policy": decl["baseline_policy"],
        "evaluate_sessions": list(decl["headline"]["evaluate_sessions"]),
        "calibrate_sessions": list(decl["headline"]["calibrate_sessions"]),
        "benchmark": dict(decl["benchmark"]),
        "latency_assumption": dict(decl["latency_assumption"]),
        "sensitivity": list(decl["sensitivity"]),
        "statistics": dict(st),
        "decision_rule": dict(decl["decision_rule"]),
        "reproducibility_tolerance": dict(decl["reproducibility"]["tolerance"]),
    }
    return {
        "scripts": bound_files(decl, root),
        "data_versions": sorted(decl["headline"]["datasets"].values()),
        "family": family,
        "metrics": list(METRICS),
        "thresholds": thresholds,
    }


def check_consolidated(decl: Mapping[str, Any], root: Path = ROOT) -> None:
    doc = json.loads((root / decl["benchmark"]["consolidated_adv"]).read_text(encoding="utf-8"))
    missing = [s for s in decl["symbols"] if not doc.get(s)]
    if missing or not doc.get("_source"):
        raise Refused(
            f"{decl['benchmark']['consolidated_adv']}: consolidated ADV for {missing or '-'} "
            f"or its _source is null; fill it (free public source) and commit before binding"
        )


# ------------------------------------------------------------------ verify --
def verify(
    prereg: Path,
    decl: Mapping[str, Any],
    *,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Signed, identity matches, runner independent. Raises :class:`Refused`."""
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
    args = identity_args(decl, root)
    try:
        verify_identity(
            root,
            doc.get("analysis_identity"),
            data_versions=args["data_versions"],
            family=args["family"],
            metrics=args["metrics"],
            thresholds=args["thresholds"],
        )
        actor = require_independent_runner(doc, env)
    except IdentityError as exc:
        raise Refused(str(exc)) from exc
    return {
        "prereg": prereg.resolve().relative_to(root.resolve()).as_posix(),
        "prereg_sha256": hashlib.sha256(prereg.read_bytes()).hexdigest(),
        "runner_actor": actor,
        "signer": doc["signature"]["maintainer"],
        "commit_bound": doc["analysis_identity"]["commit"],
    }


def check_datasets(decl: Mapping[str, Any], data_root: Path, prereg_id: str) -> dict[str, Any]:
    """Declared dataset_version on disk, and the registry allows each read."""
    from iap.research.holdout_registry import HoldoutRefused, dataset_identity, guard

    h = decl["headline"]
    sess = {
        h["primary_dir"]: [h["evaluate_sessions"][0]],
        h["second_dir"]: [h["evaluate_sessions"][0]],
    }
    for nd, xd in h["pairs"]:
        lab = Path(xd).name.removeprefix("iex_")
        iso = f"{lab[:4]}-{lab[4:6]}-{lab[6:]}"
        sess.setdefault(nd, []).append(iso)
        sess[xd] = [iso]
    out = {}
    for rel, want in h["datasets"].items():
        d = data_root / rel
        try:
            got = dataset_identity(d)["dataset_version"]
            if got != want:
                raise Refused(f"{rel}: dataset_version {got} != declared {want}")
            guard(
                d,
                purpose="b8v2",
                prereg_id=prereg_id,
                sessions=sess[rel],
                symbols=list(decl["symbols"]),
            )
        except HoldoutRefused as exc:
            raise Refused(str(exc)) from exc
        out[rel] = want
    return out


# ------------------------------------------------------------------ chain B --
def find_chain_pids() -> list[int]:
    """PIDs of chain B's python (``guard_chain_b.ps1`` matches the same command line)."""
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
        f"$_.CommandLine -match '{CHAIN_PATTERN}' }} | ForEach-Object {{ $_.ProcessId }}"
    )
    p = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=False
    )
    return [int(x) for x in p.stdout.split() if x.strip().isdigit()]


def _nt_call(pid: int, fn: str) -> bool:
    import ctypes

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not h:
        return False
    try:
        return getattr(ctypes.windll.ntdll, fn)(h) == 0  # type: ignore[attr-defined]
    finally:
        k32.CloseHandle(h)


def suspend(pid: int) -> bool:
    return _nt_call(pid, "NtSuspendProcess")


def resume(pid: int) -> bool:
    return _nt_call(pid, "NtResumeProcess")


# --------------------------------------------------------------------- run --
def run_policy(pol: str, b: Mapping[str, Any]) -> str:
    """The benchmark policy name of a declared route under the declared child
    variant: ``sor`` -> ``sor+capped`` (REL-04 capped_ioc); no variant -> ``sor``."""
    v = b.get("child_variant")
    return f"{pol}+{v}" if v else pol


def benchmark_kwargs(
    decl: Mapping[str, Any],
    data_root: Path,
    symbol: str,
    change: Mapping[str, Any],
    root: Path = ROOT,
) -> dict[str, Any]:
    b = {**decl["benchmark"], **change}
    h = decl["headline"]
    return {
        "data_dir": data_root / h["primary_dir"],
        "second_data_dir": data_root / h["second_dir"],
        "more_pairs": tuple((data_root / a, data_root / x) for a, x in h["pairs"]),
        "symbols": (symbol,),
        "algos": tuple(b["algos"]),
        "policies": tuple(run_policy(p, b) for p in decl["policies"]),
        "baseline_policy": run_policy(decl["baseline_policy"], b),
        "child_exec": child_exec_params(b),
        "sizes_pct": tuple(float(x) for x in b["sizes_pct"]),
        "window_min": float(b["window_min"]),
        "warmup_min": float(b["warmup_min"]),
        "slices": int(b["slices"]),
        "participation": float(b["participation"]),
        "max_child_qty": int(b["max_child_qty"]),
        "urgency": float(b["urgency"]),
        "seed": int(b["seed"]),
        "impact_model": b["impact_model"],
        "impact_exponent": float(b["impact_exponent"]),
        "walk_depth_levels": int(b["walk_depth_levels"]),
        "adv_table": True if b["adv_table"] == "instruments" else None,
        "cover_all_cells": bool(b["cover_all_cells"]),
        "hidden_prints": bool(b["hidden_prints"]),
        "iex_clock": b["iex_clock"],
        "venues": root / b["venues"],
        "venue_calibration": root / h["calibration_dir"],
        "consolidated_adv": root / b["consolidated_adv"],
        "router_tox_horizon": b["router_tox_horizon"],
        "router_fill_horizon_s": float(b["router_fill_horizon_s"]),
        "md_delay_ns": int(b["md_delay_ns"]),
        "paired_v2": True,
        "family_k": int(decl["statistics"]["family_k"]),
        "timestamp": False,
    }


def child_exec_params(b: Mapping[str, Any]) -> Any:
    """The declared :class:`ChildExecParams` (``None`` = the v1.17 child)."""
    if not b.get("child_variant"):
        return None
    from iap.execution.child_exec import ChildExecParams

    return ChildExecParams(**b["child_exec"])


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def runs(decl: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(tag, symbol, change) in run order: both headlines, then sensitivities."""
    out: list[tuple[str, str, dict[str, Any]]] = [(f"v2_oos3_{s}", s, {}) for s in decl["symbols"]]
    for sens in decl["sensitivity"]:
        out += [(f"v2_oos3_{sens['tag']}_{s}", s, dict(sens["change"])) for s in decl["symbols"]]
    return out


def cmd_run(
    a: argparse.Namespace,
    *,
    run_one: Callable[..., dict] | None = None,
    find: Callable[[], list[int]] = find_chain_pids,
    pause: Callable[[int], bool] = suspend,
    unpause: Callable[[int], bool] = resume,
) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    prereg = (root / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl, root=root)
    check_consolidated(decl, root)
    data_root = Path(a.data_root)
    rec["datasets"] = check_datasets(decl, data_root, rec["prereg"])
    render: Callable[[dict], str] | None = None
    if run_one is None:
        from iap.research import execution_benchmark as eb

        def run_one(**kw: Any) -> dict:
            return eb.run_benchmark(allow_holdout=rec["prereg"], **kw)

        render = eb.render
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec["started_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    suspended = [p for p in find() if pause(p)]
    rec["chain_b_suspended"] = suspended
    rec["runs"] = []
    try:
        for tag, sym, change in runs(decl):
            print(f"[b8v2] {tag}", flush=True)
            doc = run_one(**benchmark_kwargs(decl, data_root, sym, change, root))
            write_json(out / f"benchmark_{tag}.json", doc)
            if render is not None:
                (out / f"BENCHMARK_{tag}.md").write_text(
                    render(doc), encoding="utf-8", newline="\n"
                )
            rec["runs"].append(tag)
            write_json(out / "run_manifest.json", rec)
    finally:
        rec["chain_b_resumed"] = [p for p in suspended if unpause(p)]
        rec["ended_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        write_json(out / "run_manifest.json", rec)
    return cmd_analyse(a)


# ----------------------------------------------------------------- analyse --
def classify(p: Mapping[str, Any], st: Mapping[str, Any]) -> dict[str, Any]:
    prim, fr, cc = p[st["primary"]], p["fill_rate"], p["completion_charge_bps"]

    def below(s: Mapping[str, Any]) -> bool:
        return s["ci_family"] is not None and s["ci_family"][1] < 0

    def above(s: Mapping[str, Any]) -> bool:
        return s["ci_family"] is not None and s["ci_family"][0] > 0

    if prim["ci_family"] is None or fr["ci_family"] is None:
        verdict = "not_estimable"
    elif below(prim) and not below(fr) and not above(cc):
        verdict = "better"
    elif above(prim) or below(fr):
        verdict = "worse"
    else:
        verdict = "no_evidence"
    under = verdict == "no_evidence" and (
        (prim["mde_t_family"] or 0) > st["economic_threshold_bps"]
        or (fr["mde_t_family"] or 0) > st["fill_rate_threshold"]
    )
    return {"verdict": verdict, "underpowered": under}


def _r(x: Any, nd: int) -> Any:
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, list):
        return [_r(v, nd) for v in x]
    return x


def decide(decl: Mapping[str, Any], docs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    st = decl["statistics"]
    base = decl["baseline_policy"]
    b = decl["benchmark"]
    cells: dict[str, Any] = {}
    diag: dict[str, Any] = {}
    for sym in decl["symbols"]:
        for key, c in sorted((docs[sym].get("cells") or {}).items()):
            de = c.get("depth_exhausted") or {}
            diag[f"{key}|{sym}"] = _r(de.get("parent_share"), 4)
        pv = docs[sym].get("paired_v2_vs_baseline") or {}
        for pol in decl["policies"]:
            if pol == base:
                continue
            p = pv.get(f"{run_policy(pol, b)}-{run_policy(base, b)}")
            if p is None:
                cells[f"{pol}|{sym}"] = {"verdict": "not_estimable", "underpowered": False}
                continue
            row: dict[str, Any] = classify(p, st)
            for m in METRICS:
                nd = 4 if m == "fill_rate" else 2
                row[m] = {
                    k: _r(p[m][k], nd)
                    for k in ("estimate", "se", "ci95", "ci_family", "mde_t", "mde_t_family")
                }
            row["n_pairs"] = p["n_pairs"]
            row["n_clusters"] = p[st["primary"]]["n_clusters"]
            cells[f"{pol}|{sym}"] = row
    policy_claim = {
        pol: all(cells[f"{pol}|{s}"]["verdict"] == "better" for s in decl["symbols"])
        for pol in decl["policies"]
        if pol != base
    }
    return {
        "x-version": 1,
        "declaration": "research/execution/b8v2/predeclaration.json",
        "family_k": st["family_k"],
        "family_level": 1 - 0.05 / st["family_k"],
        "cells": cells,
        "better_than_baseline_on_both_symbols": policy_claim,
        "child_variant": b.get("child_variant"),
        "depth_exhausted_parent_share_by_cell": diag,
        "small_g": decl["small_g"],
        "latency_assumption": decl["latency_assumption"]["name"],
    }


def report(decl: Mapping[str, Any], dec: Mapping[str, Any]) -> str:
    st = decl["statistics"]
    lines = [
        "# B8 v2 (REL-04b): result under the signed pre-declaration",
        "",
        f"Declaration: `{dec['declaration']}`. Latency assumption: {dec['latency_assumption']}.",
        f"Contrast: policy - {decl['baseline_policy']}, pooled over all 16 cells, per symbol; "
        f"intervals at 95 % and family-adjusted ({dec['family_level']:.4f}, K = {dec['family_k']}).",
        "",
        dec["small_g"],
        "",
        "Negative results first.",
        "",
        f"| policy | symbol | verdict | d {st['primary']} [family CI] (MDE) | d fill rate "
        "[family CI] (MDE) | d completion charge [family CI] | G |",
        "|---|---|---|---|---|---|---|",
    ]
    order = {"worse": 0, "no_evidence": 1, "not_estimable": 2, "better": 3}
    for key, c in sorted(dec["cells"].items(), key=lambda kv: (order[kv[1]["verdict"]], kv[0])):
        pol, sym = key.split("|")
        v = c["verdict"] + (" (underpowered)" if c["underpowered"] else "")
        if c["verdict"] == "not_estimable" and st["primary"] not in c:
            lines.append(f"| {pol} | {sym} | {v} | - | - | - | - |")
            continue
        p, f, cc = c[st["primary"]], c["fill_rate"], c["completion_charge_bps"]
        lines.append(
            f"| {pol} | {sym} | {v} | {p['estimate']} {p['ci_family']} ({p['mde_t_family']}) | "
            f"{f['estimate']} {f['ci_family']} ({f['mde_t_family']}) | "
            f"{cc['estimate']} {cc['ci_family']} | {c['n_clusters']} |"
        )
    lines += [
        "",
        "Better than pinned Nasdaq on both symbols: "
        + ", ".join(
            f"`{k}` {'yes' if v else 'no'}"
            for k, v in dec["better_than_baseline_on_both_symbols"].items()
        ),
        "",
    ]
    return "\n".join(lines)


def cmd_analyse(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    out = Path(a.out)
    docs = {}
    for sym in decl["symbols"]:
        p = out / f"benchmark_v2_oos3_{sym}.json"
        if not p.is_file():
            raise Refused(f"{p} missing: run first")
        docs[sym] = json.loads(p.read_text(encoding="utf-8"))
    dec = decide(decl, docs)
    write_json(out / "DECISION.json", dec)
    (out / "REPORT.md").write_text(report(decl, dec), encoding="utf-8", newline="\n")
    print(f"wrote {out / 'DECISION.json'} and REPORT.md")
    return 0


def cmd_bind(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    check_consolidated(decl, root)
    args = identity_args(decl, root)
    sys.path.insert(0, str(ROOT / "tools"))
    import prereg_bind_identity as pbi  # type: ignore[import-not-found]

    argv: list[str] = ["--root", str(root), "bind", "--protocol", decl["protocol"]]
    argv += ["--out", decl["prereg"], "--scripts", *args["scripts"]]
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
    b = sub.add_parser("bind", help="write the UNSIGNED prereg (clean checkout)")
    b.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run", help="verify, suspend chain B, run AAPL then MSFT, analyse")
    r.add_argument("--prereg", type=Path, default=None)
    r.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    r.add_argument("--out", type=Path, default=HERE)
    n = sub.add_parser("analyse", help="DECISION.json + REPORT.md from the benchmark JSONs")
    n.add_argument("--out", type=Path, default=HERE)
    a = ap.parse_args(argv)
    if a.cmd == "bind":
        return cmd_bind(a)
    if a.cmd == "run":
        return cmd_run(a)
    return cmd_analyse(a)


if __name__ == "__main__":
    raise SystemExit(main())
