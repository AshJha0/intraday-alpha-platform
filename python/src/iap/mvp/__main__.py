"""``python -m iap.mvp`` — run / replay / verify / explain the MVP loop.

    python -m iap.mvp run     [--config configs/mvp/mvp.json] [--seed N]
                              [--instrument SYMBOL] [--out data/mvp/<run_id>]
                              [--repo-root DIR]
    python -m iap.mvp replay  --run <dir> [--out <dir>/replay] [--repo-root DIR]
    python -m iap.mvp verify  [--config ...] [--seed N] [--instrument SYMBOL]
                              [--repo-root DIR]
    python -m iap.mvp explain --run <dir> <parent_order_id>

``run`` generates the seeded feed, runs the loop and writes every artefact
under ``--out`` (default ``data/mvp/<run_id>``, ``run_id`` = first 16 hex
of ``content_hash(config + seed)``).  ``replay`` is the incident flow: it
re-runs the loop from the CAPTURED ``events.jsonl`` (not the generator)
under the configuration recorded in ``config.json`` and asserts that the
trace digest, the report and the event-stream sha256 reproduce (exit 1
with a diff otherwise).  ``verify`` runs twice from scratch into temporary
directories and asserts identical digest / report / stream sha256.
``explain`` prints the decision chain of one parent order from the run's
SQLite store (``Store.explain``).  ``--repo-root`` is the directory the
``reference`` paths of ``mvp.json`` (``configs/...``, ``research/...``) are
resolved against — the repository checkout by default, ``/app`` inside the
Python image (``deployment/docker/Dockerfile.python``).  Exit codes: 0 ok,
1 mismatch, 2 usage / configuration error.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

from iap.mvp.config import DEFAULT_CONFIG_PATH, REPO_ROOT, MvpConfig, load_config
from iap.mvp.feed import generate_feed, load_feed
from iap.mvp.session import (
    CONFIG_FILE,
    STORE_FILE,
    RunResult,
    compare_runs,
    load_report,
    run_session,
)
from iap.store.db import Store

__all__ = ["main", "cmd_run", "cmd_replay", "cmd_verify", "cmd_explain"]

DEFAULT_OUT_ROOT = REPO_ROOT / "data" / "mvp"


def _summary(res: RunResult) -> str:
    r = res.report
    return (f"mvp run {r['run']['run_id']}: events={r['counts']['n_events']} "
            f"decisions={r['counts']['n_decisions']} parents={r['counts']['n_parent_orders']} "
            f"children={r['counts']['n_child_orders_submitted']} fills={r['counts']['n_fills']} "
            f"pnl={r['pnl']['total']:.6f} USD digest={res.trace_digest[:16]}... "
            f"out={res.out_dir}")


def cmd_run(config: Optional[Path], seed: Optional[int], instrument: Optional[str],
            out: Optional[Path], repo_root: Optional[Path] = None) -> RunResult:
    """Generate the feed and run one session into ``out``."""
    cfg = load_config(config, seed=seed, instrument=instrument, repo_root=repo_root)
    out_dir = Path(out) if out is not None else DEFAULT_OUT_ROOT / cfg.run_id
    feed = generate_feed(cfg, out_dir)
    return run_session(cfg, feed, out_dir)


def _config_from_run(run_dir: Path, repo_root: Optional[Path] = None) -> MvpConfig:
    path = run_dir / CONFIG_FILE
    if not path.is_file():
        raise ValueError(f"not a run directory (missing {CONFIG_FILE}): {run_dir}")
    with open(path, encoding="utf-8") as fh:
        recorded = json.load(fh)
    return MvpConfig.from_document(recorded["document"], where=str(path), repo_root=repo_root)


def cmd_replay(run_dir: Path, out: Optional[Path],
               repo_root: Optional[Path] = None) -> List[str]:
    """Re-run from the captured stream; return the list of differences."""
    run_dir = Path(run_dir)
    cfg = _config_from_run(run_dir, repo_root)
    feed = load_feed(run_dir)
    expected = load_report(run_dir)
    with open(run_dir / CONFIG_FILE, encoding="utf-8") as fh:
        recorded = json.load(fh)
    diffs: List[str] = []
    if feed.events_sha256 != expected["run"]["events_sha256"]:
        diffs.append(f"events_sha256: {expected['run']['events_sha256']} != {feed.events_sha256}")
    if feed.data_version != expected["run"]["data_version"]:
        diffs.append(f"data_version: {expected['run']['data_version']} != {feed.data_version}")
    if recorded["config_version"] != cfg.config_version():
        diffs.append(f"config_version: {recorded['config_version']} != {cfg.config_version()} "
                     "(a reference document changed since the run)")
    if diffs:
        return diffs
    out_dir = Path(out) if out is not None else run_dir / "replay"
    res = run_session(cfg, feed, out_dir)
    if res.trace_digest != expected["run"]["trace_digest"]:
        diffs.append(f"trace_digest: {expected['run']['trace_digest']} != {res.trace_digest}")
    diffs.extend(compare_runs(expected, res.report))
    return diffs


def cmd_verify(config: Optional[Path], seed: Optional[int],
               instrument: Optional[str], repo_root: Optional[Path] = None) -> List[str]:
    """Run twice from scratch (temporary directories); return the differences."""
    cfg = load_config(config, seed=seed, instrument=instrument, repo_root=repo_root)
    with tempfile.TemporaryDirectory(prefix="iap-mvp-verify-") as tmp:
        base = Path(tmp)
        runs = []
        for leg in ("a", "b"):
            out_dir = base / leg
            feed = generate_feed(cfg, out_dir)
            runs.append(run_session(cfg, feed, out_dir))
        first, second = runs
        diffs: List[str] = []
        if first.feed.events_sha256 != second.feed.events_sha256:
            diffs.append(f"events_sha256: {first.feed.events_sha256} != "
                         f"{second.feed.events_sha256}")
        if first.trace_digest != second.trace_digest:
            diffs.append(f"trace_digest: {first.trace_digest} != {second.trace_digest}")
        diffs.extend(compare_runs(first.report, second.report))
        return diffs


def cmd_explain(run_dir: Path, parent_order_id: int) -> str:
    """The decision chain of one parent order (``Store.explain``)."""
    db = Path(run_dir) / STORE_FILE
    if not db.is_file():
        raise ValueError(f"no store in run directory: {db}")
    with Store.open(db) as store:
        return store.explain(parent_order_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m iap.mvp",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add_root(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo-root", type=Path, default=None, dest="repo_root",
                       help=f"directory the config's reference paths resolve against "
                            f"(default {REPO_ROOT})")

    def add_cfg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", type=Path, default=None,
                       help=f"MVP config (default {DEFAULT_CONFIG_PATH})")
        p.add_argument("--seed", type=int, default=None, help="override the config seed")
        p.add_argument("--instrument", default=None, help="override the instrument symbol")
        add_root(p)

    p_run = sub.add_parser("run", help="generate the feed and run one session")
    add_cfg(p_run)
    p_run.add_argument("--out", type=Path, default=None,
                       help=f"run directory (default {DEFAULT_OUT_ROOT}/<run_id>)")

    p_replay = sub.add_parser("replay", help="re-run from a captured events.jsonl")
    p_replay.add_argument("--run", type=Path, required=True, help="run directory")
    p_replay.add_argument("--out", type=Path, default=None,
                          help="replay output directory (default <run>/replay)")
    add_root(p_replay)

    p_verify = sub.add_parser("verify", help="run twice from scratch, compare")
    add_cfg(p_verify)

    p_explain = sub.add_parser("explain", help="print one parent order's decision chain")
    p_explain.add_argument("--run", type=Path, required=True, help="run directory")
    p_explain.add_argument("parent_order_id", type=int)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            res = cmd_run(args.config, args.seed, args.instrument, args.out, args.repo_root)
            print(_summary(res))
            return 0
        if args.command == "replay":
            diffs = cmd_replay(args.run, args.out, args.repo_root)
            if diffs:
                print(f"replay MISMATCH ({len(diffs)} differences):", file=sys.stderr)
                for d in diffs:
                    print("  " + d, file=sys.stderr)
                return 1
            print(f"replay OK: {args.run} reproduces its trace digest and report")
            return 0
        if args.command == "verify":
            diffs = cmd_verify(args.config, args.seed, args.instrument, args.repo_root)
            if diffs:
                print(f"verify MISMATCH ({len(diffs)} differences):", file=sys.stderr)
                for d in diffs:
                    print("  " + d, file=sys.stderr)
                return 1
            print("verify OK: two runs from scratch produced identical digest, report "
                  "and event stream")
            return 0
        print(cmd_explain(args.run, args.parent_order_id))
        return 0
    except (ValueError, KeyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
