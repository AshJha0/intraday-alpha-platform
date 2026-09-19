"""``python -m iap.lifecycle`` — bootstrap / status / retire / reset.

    python -m iap.lifecycle bootstrap [--dry-run]
    python -m iap.lifecycle status
    python -m iap.lifecycle retire <ID> --reason "..." [--event-ts NS]
    python -m iap.lifecycle reset  <ID> --reason "..." [--event-ts NS]

``retire`` / ``reset`` are HUMAN actions on ``research/alpha_registry.json``
and append to ``research/lifecycle_transitions.jsonl``.  ``--event-ts`` is
the event time of the action; when omitted it defaults to the registry's
latest known event time (the maximum ``since_ts``) — never the wall clock.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from iap.contracts.types import Actor
from iap.lifecycle.bootstrap import (
    REGISTRY_RELPATH,
    TRANSITIONS_RELPATH,
    render_status,
    run_bootstrap,
)
from iap.lifecycle.config import load_policy_config, repo_root
from iap.lifecycle.machine import AlphaLifecycle
from iap.lifecycle.registry import AlphaRegistry, LifecycleTransitionLog


def _load(root: Path):
    cfg = load_policy_config(root / "configs" / "strategies" / "lifecycle.json",
                             root / "configs" / "strategies" / "strategies.json")
    path = root / REGISTRY_RELPATH
    if not path.is_file():
        raise SystemExit(f"{path}: not found — run `python -m iap.lifecycle bootstrap`")
    registry = AlphaRegistry.load(path)
    log = LifecycleTransitionLog(root / TRANSITIONS_RELPATH)
    return cfg, registry, AlphaLifecycle(cfg, registry, log)


def _default_event_ts(registry: AlphaRegistry) -> int:
    return max(rec.since_ts for rec in registry.records())


def cmd_bootstrap(root: Path, dry_run: bool) -> int:
    result = run_bootstrap(root, write=not dry_run)
    print(f"bootstrap event_ts = {result.event_ts}")
    print(render_status(result.registry))
    print(f"states: {result.count_by_state()}")
    for row in result.rows:
        if row.missing_metrics:
            print(f"{row.alpha_id}: report metrics missing/non-finite: "
                  f"{', '.join(row.missing_metrics)} — kept at RESEARCH")
    if not dry_run:
        print(f"wrote {root / REGISTRY_RELPATH} and {root / TRANSITIONS_RELPATH}")
    return 0


def cmd_status(root: Path) -> int:
    _, registry, _ = _load(root)
    print(render_status(registry))
    return 0


def cmd_manual(root: Path, alpha_id: str, reason: str, event_ts: Optional[int],
               action: str) -> int:
    _, registry, machine = _load(root)
    ts = event_ts if event_ts is not None else _default_event_ts(registry)
    if action == "retire":
        tr = machine.retire(alpha_id, ts, reason, actor=Actor.HUMAN)
    else:
        tr = machine.reset_to_research(alpha_id, ts, reason, actor=Actor.HUMAN)
    registry.save(root / REGISTRY_RELPATH)
    print(f"{tr.alpha_id}: {tr.from_state.name} -> {tr.to_state.name} at {tr.event_ts} "
          f"({tr.actor.value}): {tr.reason}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m iap.lifecycle",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None,
                        help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_boot = sub.add_parser("bootstrap", help="build the registry from research/")
    p_boot.add_argument("--dry-run", action="store_true",
                        help="compute and print, write nothing")
    sub.add_parser("status", help="print the registry table")
    for name in ("retire", "reset"):
        p = sub.add_parser(name, help=f"{name} an alpha (HUMAN action)")
        p.add_argument("alpha_id")
        p.add_argument("--reason", required=True)
        p.add_argument("--event-ts", type=int, default=None)
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else repo_root()
    try:
        if args.command == "bootstrap":
            return cmd_bootstrap(root, args.dry_run)
        if args.command == "status":
            return cmd_status(root)
        return cmd_manual(root, args.alpha_id, args.reason, args.event_ts, args.command)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
