"""REL-10 volume-clock OFI runner (``research/v118/rel10/PREDECLARATION.md``).

Three subcommands; none of them is run by the session that wrote this file.

``bind`` (anyone, clean checkout of the merged commit) writes the UNSIGNED
prereg ``research/preregs/rel10_prereg.json`` through
``tools/prereg_bind_identity.py bind`` with exactly the identity arguments
:func:`identity_args` derives from ``predeclaration.json`` (``--dry-run``
prints it without writing).

``run`` (a non-signing actor, after the prereg is signed and committed)
refuses unless, in this order: the prereg is signed under the trust root; its
``analysis_identity`` matches this checkout; ``IAP_RUN_ACTOR`` is set, is not
the signer and no signing key is in the environment; every declared dataset
on disk has the declared ``dataset_version`` and the holdout registry allows
the read of each declared session and symbol under this prereg id. Then it
suspends chain B, derives each symbol's volume-clock bucket and signal
thresholds from the calibrate sessions (rows and signals only: no label, fill
or P&L there), evaluates taker and maker trips on the evaluate sessions (one
symbol and one session at a time), resumes chain B whatever happens and
writes ``calibration.json``, ``trips_evaluate.json``, ``DECISION.json`` and
``REPORT.md`` here.

``analyse`` rebuilds ``DECISION.json`` / ``REPORT.md`` from the files already
in this directory (reads no market data).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
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
SELF_REL = "research/v118/rel10/run_rel10.py"
DECL_REL = "research/v118/rel10/predeclaration.json"
DEFAULT_DATA_ROOT = ROOT / "data" / "real"
METRICS = (
    "net_usd_per_session",
    "gross_usd",
    "cost_usd",
    "fees_usd",
    "missed_usd",
    "unfilled_qty",
)
CHAIN_PATTERN = "process-iex-day"
MIN_NS = 60_000_000_000


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def load_decl(path: Path = DECL) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def identity_args(decl: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analysis_identity`` arguments: the binder and the runner both call this."""
    st = decl["statistics"]
    ev = [s["session"] for s in decl["sessions"]["evaluate"]]
    family = (
        f"REL-10 volume-clock OFI: {st['family']} (K={st['family_k']}); evaluate {','.join(ev)}"
    )
    thresholds = {
        k: decl[k]
        for k in (
            "symbols",
            "venues",
            "iex_clock",
            "sessions",
            "calibration",
            "signals",
            "horizons_min",
            "window_rule",
            "target",
            "execution",
            "statistics",
            "decision_rule",
        )
    }
    return {
        "scripts": sorted({SELF_REL, DECL_REL}),
        "data_versions": sorted(decl["datasets"].values()),
        "family": family,
        "metrics": list(METRICS),
        "thresholds": thresholds,
    }


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


def all_sessions(decl: Mapping[str, Any]) -> list[dict[str, str]]:
    return [*decl["sessions"]["calibrate"], *decl["sessions"]["evaluate"]]


def dataset_sessions(decl: Mapping[str, Any]) -> dict[str, list[str]]:
    """Declared dataset key -> the sessions this study reads from it."""
    out: dict[str, list[str]] = {}
    for s in all_sessions(decl):
        for k in ("nasdaq_dir", "iex_dir"):
            out.setdefault(s[k], []).append(s["session"])
    return out


def check_datasets(decl: Mapping[str, Any], data_root: Path, prereg_id: str) -> dict[str, Any]:
    """Every declared dataset: declared dataset_version on disk, and the holdout
    registry allows the read of its declared sessions and symbols under ``prereg_id``."""
    from iap.research.holdout_registry import HoldoutRefused, dataset_identity, guard

    used = dataset_sessions(decl)
    if set(used) != set(decl["datasets"]):
        raise Refused(f"declared datasets {sorted(decl['datasets'])} != used {sorted(used)}")
    out = {}
    for key, want in sorted(decl["datasets"].items()):
        d = data_root / decl["dataset_paths"][key]
        try:
            got = dataset_identity(d)["dataset_version"]
            if got != want:
                raise Refused(f"{key}: dataset_version {got} != declared {want}")
            guard(
                d,
                purpose="rel10",
                prereg_id=prereg_id,
                sessions=sorted(set(used[key])),
                symbols=list(decl["symbols"]),
            )
        except HoldoutRefused as exc:
            raise Refused(str(exc)) from exc
        out[key] = want
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
def load_session(
    decl: Mapping[str, Any], data_root: Path, sess: Mapping[str, str], symbol: str
) -> tuple[list[Any], int, float]:
    """The merged two-venue stream of one declared session and symbol (the guard
    already ran in :func:`check_datasets`)."""
    from iap.research import two_venue as tv

    nd = data_root / decl["dataset_paths"][sess["nasdaq_dir"]]
    xd = data_root / decl["dataset_paths"][sess["iex_dir"]]
    label = sess["session"].replace("-", "")
    prim = tv.symbols_of(nd)
    iid = prim[symbol]
    id_map = tv.instrument_map(prim, tv.symbols_of(xd))
    keep = {iid}
    keep_iex = {k for k, v in id_map.items() if v == iid}
    a = tv.read_events(tv.session_file(nd, label), keep)
    b = tv.read_events(tv.session_file(xd, label), keep_iex)
    events = tv.merge_events(a, b, id_map=id_map, iex_clock=decl["iex_clock"], keep=keep)
    inst = json.loads(
        (nd / "configs" / "instruments" / "instruments.json").read_text(encoding="utf-8")
    )
    tick = {int(r["instrument_id"]): float(r["tick_size"]) for r in inst["instruments"]}[iid]
    return events, iid, tick


def n_shares_rule(decl: Mapping[str, Any], traded: Sequence[int]) -> int:
    """Declared bucket: mean traded shares per minute over the calibrate sessions,
    rounded to the nearest 100, at least 100."""
    per_min = sum(traded) / (len(traded) * int(decl["calibration"]["minutes_per_session"]))
    return max(100, int(math.floor(per_min / 100.0 + 0.5)) * 100)


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy's default), plain Python."""
    v = sorted(xs)
    if not v:
        raise ValueError("no values")
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def calibrate(
    decl: Mapping[str, Any], tapes: Sequence[Any], session_names: Sequence[str]
) -> dict[str, Any]:
    """Bucket size and thresholds of one symbol from its calibrate tapes (signals only)."""
    from iap.research.volume_clock_ofi import volume_clock_rows

    n = n_shares_rule(decl, [sum(t.t_qty) for t in tapes])
    rows: list[dict[str, Any]] = []
    for t, s in zip(tapes, session_names, strict=True):
        rows += volume_clock_rows(t, n, windows_min=decl["horizons_min"], session=s)
    q = float(decl["calibration"]["threshold_quantile"])
    th: dict[str, float] = {}
    for sig in decl["signals"]:
        for h in decl["horizons_min"]:
            k = f"{sig}_{h}m"
            vals = [abs(float(r[k])) for r in rows if r.get(k) is not None]
            th[k] = quantile(vals, q) if vals else math.inf
    return {"n_shares": n, "thresholds": th, "calibrate_rows": len(rows)}


def evaluate_session(
    decl: Mapping[str, Any],
    tape: Any,
    tick: float,
    session: str,
    symbol: str,
    cal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Taker and maker trips of every declared (signal, horizon) on one session."""
    from iap.research.volume_clock_ofi import evaluate_maker, evaluate_taker, volume_clock_rows

    x = decl["execution"]
    fee = float(x["taker_fee_per_share"])
    rows = volume_clock_rows(
        tape, int(cal["n_shares"]), windows_min=decl["horizons_min"], session=session
    )
    out: list[dict[str, Any]] = []
    for sig in decl["signals"]:
        for h in decl["horizons_min"]:
            key = f"{sig}_{h}m"
            h_ns = int(h) * MIN_NS
            th = float(cal["thresholds"][key])
            trips = evaluate_taker(
                tape, rows, key, h_ns=h_ns, threshold=th, qty=int(x["qty"]), tick=tick,
                session=session, taker_fee_per_share=fee,
            )  # fmt: skip
            trips += evaluate_maker(
                tape, rows, key, h_ns=h_ns, threshold=th, qty=int(x["qty"]), tick=tick,
                session=session, entry_window_ns=int(x["entry_window_ns"]),
                exit_window_ns=int(x["exit_window_ns"]), taker_fee_per_share=fee,
                maker_rebate_per_share=float(x["maker_rebate_per_share"]),
            )  # fmt: skip
            for t in trips:
                t["symbol"] = symbol
                t["signal"] = sig
                t["horizon_min"] = int(h)
            out += trips
    return out


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def cmd_run(
    a: argparse.Namespace,
    *,
    loader: Callable[..., tuple[list[Any], int, float]] | None = None,
    find: Callable[[], list[int]] = find_chain_pids,
    pause: Callable[[int], bool] = suspend,
    unpause: Callable[[int], bool] = resume,
) -> int:
    from iap.research.volume_clock_ofi import build_tape

    decl = load_decl(a.decl)
    root = Path(a.root)
    prereg = (root / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl, root=root)
    data_root = Path(a.data_root)
    rec["datasets"] = check_datasets(decl, data_root, rec["prereg"])
    load = loader or load_session
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec["started_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    suspended = [p for p in find() if pause(p)]
    rec["chain_b_suspended"] = suspended
    try:
        cals: dict[str, Any] = {}
        trips: list[dict[str, Any]] = []
        for sym in decl["symbols"]:
            tapes, names = [], []
            for s in decl["sessions"]["calibrate"]:
                ev, iid, _ = load(decl, data_root, s, sym)
                tapes.append(build_tape(ev, iid))
                names.append(s["session"])
                del ev
            cals[sym] = calibrate(decl, tapes, names)
            del tapes
            write_json(out / "calibration.json", cals)
            for s in decl["sessions"]["evaluate"]:
                ev, iid, tick = load(decl, data_root, s, sym)
                tape = build_tape(ev, iid)
                del ev
                trips += evaluate_session(decl, tape, tick, s["session"], sym, cals[sym])
                write_json(out / "trips_evaluate.json", trips)
    finally:
        rec["chain_b_resumed"] = [p for p in suspended if unpause(p)]
        rec["ended_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        write_json(out / "run_manifest.json", rec)
    return cmd_analyse(a)


# ----------------------------------------------------------------- analyse --
def _t(q: float, df: int) -> float:
    from scipy.stats import t as student_t

    return float(student_t.ppf(q, df))


def cell_stats(
    decl: Mapping[str, Any], trips: Sequence[Mapping[str, Any]], sessions: Sequence[str]
) -> dict[str, Any]:
    """Session-clustered statistics of one cell (family-adjusted)."""
    from iap.research.study_stats import summarise_study

    st = decl["statistics"]
    filled = [t for t in trips if t["filled"] > 0]
    s = summarise_study(filled, sessions, alpha=st["alpha"], power=st["power"])
    g, sd, mean = s["n_sessions"], s["sd_usd"], s["mean_usd"]
    k = int(st["family_k"])
    fam = mde_fam = None
    if g >= 3 and sd is not None and sd > 0 and mean is not None:
        q = 1 - st["alpha"] / (2 * k)
        half = _t(q, g - 1) * sd / math.sqrt(g)
        fam = [mean - half, mean + half]
        mde_fam = (_t(q, g - 1) + _t(st["power"], g - 1)) * sd / math.sqrt(g)
    return {
        "G": g,
        "mean_usd": mean,
        "sd_usd": sd,
        "ci_family_usd": fam,
        "mde_family_usd": mde_fam,
        "per_session_usd": s["per_session_usd"],
        "trips": len(filled),
        "opportunities": len(trips),
        "gross_usd": math.fsum(float(t["gross_usd"]) for t in filled),
        "cost_usd": math.fsum(float(t["cost_usd"]) for t in filled),
        "fees_usd": math.fsum(float(t["fees_usd"]) for t in filled),
        "missed_usd": math.fsum(float(t["missed_usd"]) for t in trips),
        "unfilled_qty": sum(int(t["unfilled"]) for t in trips),
    }


def classify(s: Mapping[str, Any], threshold: float) -> str:
    ci, m = s["ci_family_usd"], s["mde_family_usd"]
    if ci is None:
        return "underpowered"
    if ci[0] > 0:
        return "candidate"
    if ci[1] < 0:
        return "reject"
    return "underpowered" if (m is None or m > threshold) else "reject"


def use_verdict(cells: Mapping[str, Mapping[str, Any]], symbols: Sequence[str]) -> str:
    """Per use; cells keyed ``signal|h|symbol`` (the declared map)."""
    v = {k: c["verdict"] for k, c in cells.items()}
    pairs = {k.rsplit("|", 1)[0] for k in v}
    if any(all(v.get(f"{p}|{s}") == "candidate" for s in symbols) for p in pairs):
        return "candidate"
    if "candidate" in v.values():
        return "iterate"
    if all(x == "underpowered" for x in v.values()):
        return "underpowered"
    return "reject"


def decide(decl: Mapping[str, Any], trips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    st = decl["statistics"]
    sessions = [s["session"] for s in decl["sessions"]["evaluate"]]
    uses: dict[str, Any] = {}
    for use in ("taker", "maker"):
        cells: dict[str, Any] = {}
        for sig in decl["signals"]:
            for h in decl["horizons_min"]:
                for sym in decl["symbols"]:
                    tt = [
                        t
                        for t in trips
                        if t["use"] == use
                        and t["signal"] == sig
                        and t["horizon_min"] == int(h)
                        and t["symbol"] == sym
                    ]
                    c = cell_stats(decl, tt, sessions)
                    c["verdict"] = classify(c, float(st["economic_threshold_usd"]))
                    cells[f"{sig}|{h}|{sym}"] = c
        uses[use] = {"cells": cells, "verdict": use_verdict(cells, decl["symbols"])}
    return {
        "x-version": 1,
        "declaration": DECL_REL,
        "family_k": st["family_k"],
        "family_level": 1 - st["alpha"] / st["family_k"],
        "uses": uses,
        "execution_input_first": True,
        "small_g": decl["small_g"],
    }


def report(decl: Mapping[str, Any], dec: Mapping[str, Any]) -> str:
    lines = [
        "# REL-10 volume-clock OFI: result under the signed pre-declaration",
        "",
        f"Declaration: `{dec['declaration']}`. Family K = {dec['family_k']}, level "
        f"{dec['family_level']:.4f}. Taker and maker are evaluated separately; an execution "
        "input first.",
        "",
        dec["small_g"],
        "",
    ]
    for use in ("taker", "maker"):
        u = dec["uses"][use]
        lines += [
            f"## {use}: **{u['verdict']}**",
            "",
            "| signal | h (min) | symbol | verdict | mean net USD | family CI | family MDE | "
            "trips | missed USD |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for key, c in sorted(u["cells"].items(), key=lambda kv: (kv[1]["verdict"], kv[0])):
            sig, h, sym = key.split("|")
            lines.append(
                f"| {sig} | {h} | {sym} | {c['verdict']} | {c['mean_usd']} | "
                f"{c['ci_family_usd']} | {c['mde_family_usd']} | {c['trips']} | "
                f"{c['missed_usd']} |"
            )
        lines.append("")
    return "\n".join(lines)


def cmd_analyse(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    out = Path(a.out)
    p = out / "trips_evaluate.json"
    if not p.is_file():
        raise Refused(f"{p} missing: run first")
    dec = decide(decl, json.loads(p.read_text(encoding="utf-8")))
    write_json(out / "DECISION.json", dec)
    (out / "REPORT.md").write_text(report(decl, dec), encoding="utf-8", newline="\n")
    print(f"wrote {out / 'DECISION.json'} and REPORT.md")
    return 0


def cmd_bind(a: argparse.Namespace) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
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
    r = sub.add_parser("run", help="verify, suspend chain B, calibrate, evaluate, analyse")
    r.add_argument("--prereg", type=Path, default=None)
    r.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    r.add_argument("--out", type=Path, default=HERE)
    n = sub.add_parser("analyse", help="DECISION.json + REPORT.md from the trips")
    n.add_argument("--out", type=Path, default=HERE)
    a = ap.parse_args(argv)
    if a.cmd == "bind":
        return cmd_bind(a)
    if a.cmd == "run":
        return cmd_run(a)
    return cmd_analyse(a)


if __name__ == "__main__":
    raise SystemExit(main())
