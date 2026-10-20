"""REL-05 real-tape fill test runner (``research/execution/rel05/PREDECLARATION.md``).

Three subcommands; none of them is run by the session that wrote this file.

``bind`` (anyone, clean checkout) writes the UNSIGNED prereg
``research/preregs/rel05_prereg.json`` through ``tools/prereg_bind_identity.py bind``
with the identity arguments :func:`identity_args` derives from ``predeclaration.json``.
Reads no market data.

``run`` (a non-signing actor, after the prereg is signed and committed) refuses unless,
in this order: the split is valid against the holdout registry (every day ``in_sample``,
fit and eval disjoint, every eval day after every fit day); the prereg is signed and its
``analysis_identity`` matches this checkout; ``IAP_RUN_ACTOR`` is set and is not the
signer and no signing key is in the environment; every dataset directory passes
``guard_or_exit`` and has the declared ``dataset_version``; enough RAM is free.  Then it
loads one (day, symbol) at a time, fits the fill hazard on the fit days and scores the
eval days with :func:`iap.paper.fill_tape.tape_test`, and writes ``tape_<symbol>.json``,
``DECISION.json`` and ``REPORT.md`` here.

``analyse`` rebuilds ``DECISION.json`` / ``REPORT.md`` from the ``tape_*.json`` already in
this directory (reads no market data).

The label is tape-implied under the simulator's queue rules, not a real fill: this is
Level 1, never Level 2 (``docs/SIM_TO_REAL.md``).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

DECL = HERE / "predeclaration.json"
SELF_REL = "research/execution/rel05/run_rel05.py"
DEFAULT_DATA_ROOT = ROOT / "data" / "real"
METRICS = ("skill_pooled", "skill_per_day", "brier", "reliability_gap_max")
NS = 1_000_000_000
PURPOSE = "rel05_tape"


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def load_decl(path: Path = DECL) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def bound_files(decl: Mapping[str, Any]) -> list[str]:
    return sorted({SELF_REL, "research/execution/rel05/predeclaration.json"})


def identity_args(decl: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analysis_identity`` arguments: the binder and the runner both call this."""
    st = decl["statistics"]
    family = (
        f"REL-05 real-tape fill test: {st['family']} (K={st['family_k']}); "
        f"fit {','.join(decl['split']['fit_days'])}; eval {','.join(decl['split']['eval_days'])}"
    )
    thresholds = {
        "split": dict(decl["split"]),
        "symbols": dict(decl["symbols"]),
        "label": dict(decl["label"]),
        "model": decl["model"],
        "statistics": dict(st),
        "decision_rule": dict(decl["decision_rule"]),
        "stop_rules": dict(decl["stop_rules"]),
    }
    return {
        "scripts": bound_files(decl),
        "data_versions": sorted(d["dataset_version"] for d in decl["datasets"].values()),
        "family": family,
        "metrics": list(METRICS),
        "thresholds": thresholds,
    }


# ------------------------------------------------------------------ split --
def check_split(decl: Mapping[str, Any], registry: Mapping[str, Any] | None = None) -> None:
    """Raises :class:`Refused` unless the declared split is valid.  Reads the registry only."""
    from iap.research.holdout_registry import load_registry

    sp = decl["split"]
    fit, ev = list(sp["fit_days"]), list(sp["eval_days"])
    if not fit or not ev:
        raise Refused("split: need at least one fit day and one eval day")
    if len(set(fit)) != len(fit) or len(set(ev)) != len(ev):
        raise Refused("split: a day is listed twice")
    both = sorted(set(fit) & set(ev))
    if both:
        raise Refused(f"split: days in both the fit and the eval set: {both}")
    if min(ev) <= max(fit):
        raise Refused(f"split: eval day {min(ev)} is not after the last fit day {max(fit)}")
    if set(decl["datasets"]) != set(fit) | set(ev):
        raise Refused("split: datasets must cover exactly the fit and eval days")
    reg = registry if registry is not None else load_registry()
    for day in [*fit, *ev]:
        rules = reg.get("sessions", {}).get(day)
        if rules is None or any(r["state"] != "in_sample" for r in rules):
            raise Refused(f"split: {day} is spent / unread / unregistered in the holdout registry")


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
    args = identity_args(decl)
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


def check_datasets(decl: Mapping[str, Any], data_root: Path, prereg_id: str) -> dict[str, str]:
    """``guard_or_exit`` on every dataset directory, then the declared ``dataset_version``."""
    from iap.research.holdout_registry import HoldoutRefused, dataset_identity, guard_or_exit

    out = {}
    for day, d in sorted(decl["datasets"].items()):
        path = data_root / d["dir"]
        guard_or_exit(path, PURPOSE, prereg_id)
        try:
            got = dataset_identity(path)["dataset_version"]
        except HoldoutRefused as exc:
            raise Refused(str(exc)) from exc
        if got != d["dataset_version"]:
            raise Refused(f"{d['dir']}: dataset_version {got} != declared {d['dataset_version']}")
        out[day] = got
    return out


def check_ram(decl: Mapping[str, Any], free_gb: Callable[[], float | None] | None = None) -> float:
    from iap.marketdata.runmem import available_ram_gb

    got = (free_gb or available_ram_gb)()
    need = float(decl["stop_rules"]["min_free_ram_gb"])
    if got is not None and got < need:
        raise Refused(f"available RAM {got:.1f} GB < declared minimum {need} GB")
    return float(got) if got is not None else -1.0


# -------------------------------------------------------------------- data --
def decision_grid(day: str, lab: Mapping[str, Any]) -> Any:
    """The declared 5 s grid between the declared ET times (UTC ns)."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    import numpy as np

    g = lab["grid"]

    def at(hhmm: str) -> int:
        h, m = (int(x) for x in hhmm.split(":"))
        d = dt.date.fromisoformat(day)
        return (
            int(
                dt.datetime(
                    d.year, d.month, d.day, h, m, tzinfo=ZoneInfo("America/New_York")
                ).timestamp()
            )
            * NS
        )

    return np.arange(at(g["start_et"]), at(g["end_et"]), int(g["stride_s"]) * NS, dtype=np.int64)


def day_frame(decl: Mapping[str, Any], data_root: Path, day: str, symbol: str) -> Any:
    """Survival samples of one (day, symbol); loads only that instrument's events."""
    from iap.execution.config import load_exec_config
    from iap.paper.fill_tape import day_samples
    from iap.research.maker_real import load_events

    iid = int(decl["symbols"][symbol])
    lab = decl["label"]
    events = load_events(data_root / decl["datasets"][day]["dir"], iid)
    if not events:
        raise Refused(f"{day} {symbol}: no events")
    ts = decision_grid(day, lab)
    if len(ts) < int(lab["min_decisions_per_day_symbol"]):
        raise Refused(f"{day} {symbol}: only {len(ts)} decisions on the declared grid")
    cfg = load_exec_config(data_root / decl["config_dir"])
    return day_samples(
        events,
        ts,
        instrument_id=iid,
        venue_id=int(events[0].venue_id),
        exec_config=cfg,
        ttl_ns=int(lab["ttl_ns"]),
        day=day,
        qty=int(lab["qty"]),
    )


def run_symbol(
    decl: Mapping[str, Any],
    data_root: Path,
    symbol: str,
    frame: Callable[..., Any] = day_frame,
) -> dict[str, Any]:
    """Fit on the fit days, score the eval days pooled and one by one."""
    from iap.paper.fill_tape import tape_test

    sp, ttl = decl["split"], int(decl["label"]["ttl_ns"])
    fit = {d: frame(decl, data_root, d, symbol) for d in sp["fit_days"]}
    ev = {d: frame(decl, data_root, d, symbol) for d in sp["eval_days"]}
    pooled = tape_test(fit, ev, ttl_ns=ttl)
    pooled["per_day"] = {d: tape_test(fit, {d: ev[d]}, ttl_ns=ttl)["overall"] for d in ev}
    pooled["symbol"] = symbol
    return pooled


# ---------------------------------------------------------------- decision --
def classify(doc: Mapping[str, Any], rule: Mapping[str, Any], min_n: int = 100) -> dict[str, Any]:
    """The pre-declared verdict of one symbol from its ``tape_test`` report."""
    ov = doc["overall"]
    skill = ov.get("skill")
    days = {d: c.get("skill") for d, c in doc["per_day"].items()}
    gaps = [
        abs(r["observed_rate"] - r["mean_predicted"])
        for r in doc["reliability"]
        if r["n"] >= min_n and r["observed_rate"] is not None
    ]
    gap = max(gaps) if gaps else None
    out = {
        "skill_pooled": skill,
        "skill_per_day": days,
        "brier": ov.get("brier"),
        "brier_naive": ov.get("brier_naive"),
        "reliability_gap_max": gap,
        "n_eval": doc["n_eval"],
        "n_fit": doc["n_fit"],
    }
    if skill is None or gap is None or any(v is None for v in days.values()):
        return {**out, "verdict": "not_estimable"}
    if skill < 0:
        verdict = "contradicts"
    elif (
        skill >= rule["skill_min"]
        and all(v > 0 for v in days.values())
        and gap <= rule["reliability_gap_max"]
    ):
        verdict = "supports"
    else:
        verdict = "inconclusive"
    return {**out, "verdict": verdict}


def decide(decl: Mapping[str, Any], docs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    rule = decl["decision_rule"]
    cells = {s: classify(docs[s], rule) for s in decl["symbols"]}
    return {
        "x-version": 1,
        "declaration": "research/execution/rel05/predeclaration.json",
        "level2": "not validated",
        "label": "tape-implied under the simulator's queue rules, not a measured real fill",
        "cells": cells,
        "model_supported_on_all_symbols": all(c["verdict"] == "supports" for c in cells.values()),
        "no_inference": decl["statistics"]["inference"],
        "cannot_claim": decl["claims"]["cannot_claim"],
    }


def _f(x: Any) -> str:
    return "-" if x is None else f"{x:.4f}"


def report(decl: Mapping[str, Any], dec: Mapping[str, Any]) -> str:
    lines = [
        "# REL-05 real-tape fill test: result under the pre-declaration",
        "",
        f"Declaration: `{dec['declaration']}`. Level 2: **{dec['level2']}**. Label: {dec['label']}.",
        f"Fit days {', '.join(decl['split']['fit_days'])}; eval days "
        f"{', '.join(decl['split']['eval_days'])}.",
        "",
        "Negative results first.",
        "",
        "| symbol | verdict | pooled skill | per-day skill | reliability gap | Brier | Brier naive |",
        "|---|---|---|---|---|---|---|",
    ]
    order = {"contradicts": 0, "inconclusive": 1, "not_estimable": 2, "supports": 3}
    for s, c in sorted(dec["cells"].items(), key=lambda kv: (order[kv[1]["verdict"]], kv[0])):
        per = ", ".join(f"{d}: {_f(v)}" for d, v in c["skill_per_day"].items())
        lines.append(
            f"| {s} | {c['verdict']} | {_f(c['skill_pooled'])} | {per} | "
            f"{_f(c['reliability_gap_max'])} | {_f(c['brier'])} | {_f(c['brier_naive'])} |"
        )
    lines += [
        "",
        f"Supported on all symbols: {'yes' if dec['model_supported_on_all_symbols'] else 'no'}.",
        f"Inference: {dec['no_inference']}",
        "",
        "Cannot claim:",
        *[f"- {x}" for x in dec["cannot_claim"]],
        "",
    ]
    return "\n".join(lines)


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


# -------------------------------------------------------------------- cmds --
def cmd_run(
    a: argparse.Namespace,
    *,
    run_one: Callable[..., dict] | None = None,
    free_gb: Callable[[], float | None] | None = None,
) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    check_split(decl)
    prereg = (root / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl, root=root)
    data_root = Path(a.data_root)
    rec["datasets"] = check_datasets(decl, data_root, rec["prereg"])
    rec["free_ram_gb"] = check_ram(decl, free_gb)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec["started_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    one = run_one or run_symbol
    for sym in decl["symbols"]:
        path = out / f"tape_{sym}.json"
        if path.is_file():  # one run only: an existing result is never overwritten
            raise Refused(f"{path} exists: a parameter change is a new declaration, not a rerun")
        print(f"[rel05] {sym}", flush=True)
        write_json(path, one(decl, data_root, sym))
    rec["ended_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    write_json(out / "run_manifest.json", rec)
    return cmd_analyse(a)


def cmd_analyse(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    out = Path(a.out)
    docs = {}
    for sym in decl["symbols"]:
        p = out / f"tape_{sym}.json"
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
    check_split(decl)
    args = identity_args(decl)
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
    r = sub.add_parser("run", help="verify, guard, fit, score, analyse")
    r.add_argument("--prereg", type=Path, default=None)
    r.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    r.add_argument("--out", type=Path, default=HERE)
    n = sub.add_parser("analyse", help="DECISION.json + REPORT.md from tape_*.json")
    n.add_argument("--out", type=Path, default=HERE)
    a = ap.parse_args(argv)
    if a.cmd == "bind":
        return cmd_bind(a)
    if a.cmd == "run":
        return cmd_run(a)
    return cmd_analyse(a)


if __name__ == "__main__":
    raise SystemExit(main())
