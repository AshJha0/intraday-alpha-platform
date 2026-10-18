"""REL-08c runner (``research/auction/rel08c/PREDECLARATION.md``).

Subcommands; none of them is run by the session that wrote this file.

``bind`` (anyone, clean checkout of the merged commit) writes the UNSIGNED
prereg ``research/preregs/rel08c_prereg.json`` through
``tools/prereg_bind_identity.py bind`` with exactly the identity arguments
:func:`identity_args` derives from ``predeclaration.json``.  ``--dry-run``
prints the block without writing.

``run`` (a non-signing actor, after the prereg is signed and committed)
refuses (exit 2) unless, in this order: the prereg is signed under the trust
root; its ``analysis_identity`` matches this checkout; ``IAP_RUN_ACTOR`` is
set, is not the signer, and no signing key is in the environment; the session
is ``in_sample`` in ``research/holdout_registry.json``; no chain-B ingest is
running; the input file's sha256 equals the declared one.  Only then does it
read the file: first the one-pass NOII extraction
(:func:`iap.auction.rel08c_extract.extract_rel08c`), then the study
(:func:`iap.auction.rel08c_study.run_study`), writing ``DECISION.json`` and
``REPORT.md`` here.

``analyse`` re-runs the study on an existing extract (reads no ITCH bytes).
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
SELF_REL = "research/auction/rel08c/run_rel08c.py"
DEFAULT_DATA_ROOT = Path("C:/Work/Claude/intraday-alpha-platform/intraday-alpha-platform")
METRICS = (
    "incremental_net_taker_bps",
    "net_taker_bps",
    "gross_taker_bps",
    "gross_cross_bps",
    "net_cross_bps",
    "hit_rate",
    "fill_entry",
    "fill_exit",
    "loc_fill",
)
CHAIN_PATTERN = "process-iex-day|process-day"


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def load_decl(path: Path = DECL) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def identity_args(decl: Mapping[str, Any]) -> dict[str, Any]:
    """The ``analysis_identity`` arguments: the binder and the runner both call this."""
    st = decl["statistics"]
    return {
        "scripts": sorted(
            {SELF_REL, "research/auction/rel08c/predeclaration.json", decl["input"]["universe"]}
        ),
        "data_versions": [decl["input"]["sha256"]],
        "family": f"{st['family']} (K={st['family_k']}); session {decl['input']['session']}",
        "metrics": list(METRICS),
        "thresholds": {
            "study": dict(decl["study"]),
            "statistics": dict(st),
            "input": dict(decl["input"]),
        },  # fmt: skip
    }


def _tod_ns(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return (h * 3600 + m * 60 + s) * 1_000_000_000


def study_config(decl: Mapping[str, Any]) -> Any:
    from iap.auction.rel08c_study import Rel08cConfig

    s = dict(decl["study"])
    return Rel08cConfig(
        decision_tod_ns=tuple(_tod_ns(t) for t in s.pop("decision_times")),
        grid_start_tod_ns=_tod_ns(s.pop("grid_start")),
        **s,
    )


# ------------------------------------------------------------------ checks --
def verify(prereg: Path, decl: Mapping[str, Any], *, root: Path = ROOT,
           env: Mapping[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
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


def check_holdout(decl: Mapping[str, Any], root: Path = ROOT) -> str:
    reg = json.loads((root / "research" / "holdout_registry.json").read_text(encoding="utf-8"))
    sess = decl["input"]["session"]
    states = {e.get("state") for e in (reg.get("sessions") or {}).get(sess, [])}
    want = decl["input"]["holdout_state_required"]
    if states != {want}:
        raise Refused(f"session {sess}: holdout registry state {sorted(states)} != {want!r}")
    return want


def find_chain_pids() -> list[int]:
    """PIDs of a running ingest (chain B or process-day)."""
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


def write_json(path: Path, doc: Any) -> None:
    path.write_text(
        json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# --------------------------------------------------------------------- run --
def cmd_run(
    a: argparse.Namespace,
    *,
    extract: Callable[..., dict] | None = None,
    find: Callable[[], list[int]] = find_chain_pids,
) -> int:
    decl = load_decl(a.decl)
    root = Path(a.root)
    prereg = (root / decl["prereg"]) if a.prereg is None else Path(a.prereg)
    rec = verify(prereg, decl, root=root)
    rec["holdout_state"] = check_holdout(decl, root)
    busy = find()
    if busy:
        raise Refused(f"an ingest is running (pids {busy}); never two jobs at once")
    data_root = Path(a.data_root)
    itch = data_root / decl["input"]["file"]
    if extract is None:
        from iap.auction.rel08c_extract import extract_rel08c, load_universe

        def extract(**kw: Any) -> dict:
            return extract_rel08c(**kw)
    else:
        from iap.auction.rel08c_extract import load_universe
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec["started_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    write_json(out / "run_manifest.json", rec)
    # refuses on a sha256 mismatch before any message is decoded
    meta = extract(
        itch_path=itch,
        symbols=load_universe(root / decl["input"]["universe"]),
        out_dir=data_root / decl["input"]["extract_out"],
        date=decl["input"]["session"],
        expected_sha256=decl["input"]["sha256"],
    )
    rec["extract_meta"] = meta
    rec["ended_extract_utc"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    write_json(out / "run_manifest.json", rec)
    a.extract_dir = data_root / decl["input"]["extract_out"]
    return cmd_analyse(a, prereg_id=rec["prereg"])


def report(decl: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    m = summary["measures"]
    lines = [
        "# REL-08c closing-auction imbalance: result under the signed pre-declaration",
        "",
        f"Declaration: `research/auction/rel08c/predeclaration.json`. {decl['framing']}.",
        "",
        f"**Verdict: {summary['verdict']}** - " + "; ".join(summary["reasons"]),
        "",
        f"Sessions {summary['n_sessions']}, imbalance trades {summary['n_imbalance_trades']}; "
        f"mean incremental net {summary['mean_incremental_bps']:.3g} bps "
        f"[{summary['ci_low_bps']:.3g}, {summary['ci_high_bps']:.3g}], "
        f"MDE {summary['mde_bps']:.3g} bps.",
        "",
        "| policy | n | gross taker bps | gross to cross bps | hit | fill in / out | "
        "LOC fill | net taker bps | net cross bps |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for pol, x in m.items():
        p, f, e = x["prediction"], x["fill"], x["execution"]
        lines.append(
            f"| {pol} | {x['n']} | {p['gross_taker_bps']:.3g} | {p['gross_cross_bps']:.3g} | "
            f"{p['hit_rate']:.3g} | {f['entry']:.3g} / {f['exit']:.3g} | "
            f"{f['loc_at_exit_mid']:.3g} | {e['net_taker_bps']:.3g} | {e['net_cross_bps']:.3g} |"
        )
    return "\n".join([*lines, ""])


def cmd_analyse(a: argparse.Namespace, prereg_id: str | None = None) -> int:
    from iap.auction.rel08c_study import run_study

    decl = load_decl(a.decl)
    root = Path(a.root)
    pid = prereg_id or decl["prereg"]
    res = run_study([a.extract_dir], study_config(decl), prereg_id=pid, root=root)
    out = Path(a.out)
    write_json(out / "DECISION.json", res.to_json())
    (out / "REPORT.md").write_text(report(decl, res.summary), encoding="utf-8", newline="\n")
    print(f"wrote {out / 'DECISION.json'} and REPORT.md: {res.summary['verdict']}")
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
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--decl", type=Path, default=DECL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bind", help="write the UNSIGNED prereg (clean checkout)")
    b.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run", help="verify, extract NOII, run the study")
    r.add_argument("--prereg", type=Path, default=None)
    r.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    r.add_argument("--out", type=Path, default=HERE)
    n = sub.add_parser("analyse", help="study on an existing extract (no ITCH read)")
    n.add_argument("--extract-dir", type=Path, required=True)
    n.add_argument("--out", type=Path, default=HERE)
    a = ap.parse_args(argv)
    if a.cmd == "bind":
        return cmd_bind(a)
    if a.cmd == "run":
        return cmd_run(a)
    return cmd_analyse(a)


if __name__ == "__main__":
    raise SystemExit(main())
