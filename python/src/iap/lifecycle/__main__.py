"""``python -m iap.lifecycle`` — bootstrap / status / retire / reset.

    python -m iap.lifecycle bootstrap [--dry-run] [--force]
    python -m iap.lifecycle status
    python -m iap.lifecycle retire <ID> --reason "..." --approval FILE --keyfile PATH
    python -m iap.lifecycle reset  <ID> --reason "..." --approval FILE --keyfile PATH

``retire`` / ``reset`` are HUMAN actions on ``research/alpha_registry.json``
and append to ``research/lifecycle_transitions.jsonl``.  Since v1.13
(G-P1-3) they require a verified approval record
(:mod:`iap.agents.approvals`, issued with ``python -m iap.agents.cli issue``):
the approval must name the same alpha, action and reason, its signature is
checked against ``--keyfile`` (outside the repository), and its nonce is
burned on the blackboard - exactly what ``python -m iap.agents.cli apply``
does, which these commands route through.  The event time of an approved
action is the wall clock (as ``agents.cli apply``).  Without an approval
the command is refused (exit 2).  Library callers that hold their own proof
(tests, the golden replays) use ``cmd_manual(..., require_approval=False)``.  That log is an
append-only audit: ``bootstrap`` refuses to truncate it while it holds
transitions unless ``--force`` is given (exit code 3).  ``--event-ts`` is
the event time of the action; when omitted it defaults to the registry's
latest known event time (the maximum ``since_ts``) — never the wall clock.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iap.contracts.types import Actor
from iap.lifecycle.bootstrap import (
    REGISTRY_RELPATH,
    TRANSITIONS_RELPATH,
    TransitionLogExists,
    render_status,
    run_bootstrap,
)
from iap.lifecycle.config import load_policy_config, repo_root
from iap.lifecycle.machine import AlphaLifecycle
from iap.lifecycle.registry import AlphaRegistry, LifecycleTransitionLog


def _load(root: Path):
    cfg = load_policy_config(
        root / "configs" / "strategies" / "lifecycle.json",
        root / "configs" / "strategies" / "strategies.json",
    )
    path = root / REGISTRY_RELPATH
    if not path.is_file():
        raise SystemExit(f"{path}: not found — run `python -m iap.lifecycle bootstrap`")
    registry = AlphaRegistry.load(path)
    log = LifecycleTransitionLog(root / TRANSITIONS_RELPATH)
    return cfg, registry, AlphaLifecycle(cfg, registry, log)


def _default_event_ts(registry: AlphaRegistry) -> int:
    return max(rec.since_ts for rec in registry.records())


def cmd_bootstrap(root: Path, dry_run: bool, force: bool) -> int:
    try:
        result = run_bootstrap(root, write=not dry_run, force=force)
    except TransitionLogExists as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"bootstrap event_ts = {result.event_ts}")
    print(render_status(result.registry))
    print(f"states: {result.count_by_state()}")
    claimed: dict[str, int] = {}
    for row in result.rows:
        claimed[row.claim_status] = claimed.get(row.claim_status, 0) + 1
    print(f"evidence status (docs/RESEARCH_VALIDITY.md 4g): {dict(sorted(claimed.items()))}")
    for row in result.rows:
        if row.missing_metrics:
            print(
                f"{row.alpha_id}: report metrics missing/non-finite: "
                f"{', '.join(row.missing_metrics)} — kept at RESEARCH"
            )
    if not dry_run:
        print(f"wrote {root / REGISTRY_RELPATH} and {root / TRANSITIONS_RELPATH}")
    return 0


def cmd_status(root: Path) -> int:
    _, registry, _ = _load(root)
    print(render_status(registry))
    return 0


def cmd_manual(
    root: Path,
    alpha_id: str,
    reason: str,
    event_ts: int | None,
    action: str,
    require_approval: bool = True,
    approval: Path | None = None,
    keyfile: Path | None = None,
) -> int:
    if require_approval or approval is not None:
        return _approved_manual(root, alpha_id, reason, action, approval, keyfile)
    _, registry, machine = _load(root)
    ts = event_ts if event_ts is not None else _default_event_ts(registry)
    if action == "retire":
        tr = machine.retire(alpha_id, ts, reason, actor=Actor.HUMAN)
    else:
        tr = machine.reset_to_research(alpha_id, ts, reason, actor=Actor.HUMAN)
    registry.save(root / REGISTRY_RELPATH)
    print(
        f"{tr.alpha_id}: {tr.from_state.name} -> {tr.to_state.name} at {tr.event_ts} "
        f"({tr.actor.value}): {tr.reason}"
    )
    return 0


def _approved_manual(
    root: Path,
    alpha_id: str,
    reason: str,
    action: str,
    approval: Path | None,
    keyfile: Path | None,
) -> int:
    """G-P1-3: the HUMAN edge only with a verified, matching, unused approval."""
    import json

    if approval is None or keyfile is None:
        print(
            f"refused: `{action}` is a HUMAN action and needs a signed approval: "
            "issue one with `python -m iap.agents.cli issue`, then pass "
            "--approval FILE --keyfile PATH (or run `python -m iap.agents.cli apply`)",
            file=sys.stderr,
        )
        return 2
    from iap.agents import cli as agents_cli

    try:
        doc = json.loads(Path(approval).read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        print(f"refused: cannot read approval {approval}: {exc}", file=sys.stderr)
        return 2
    want = {"alpha_id": alpha_id, "action": action, "reason": reason}
    got = {k: doc.get(k) if isinstance(doc, dict) else None for k in want}
    if got != want:
        print(
            f"refused: the approval is for {got}, not {want}",
            file=sys.stderr,
        )
        return 2
    return agents_cli.main(
        [
            "--root",
            str(root),
            "apply",
            "--approval",
            str(approval),
            "--keyfile",
            str(keyfile),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m iap.lifecycle", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="repository root (default: this checkout)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_boot = sub.add_parser("bootstrap", help="build the registry from research/")
    p_boot.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    p_boot.add_argument(
        "--force",
        action="store_true",
        help="rebuild research/lifecycle_transitions.jsonl even though it "
        "already holds transitions (the log is append-only; without "
        "--force a non-empty log is refused with exit code 3)",
    )
    sub.add_parser("status", help="print the registry table")
    for name in ("retire", "reset"):
        p = sub.add_parser(name, help=f"{name} an alpha (HUMAN action)")
        p.add_argument("alpha_id")
        p.add_argument("--reason", required=True)
        p.add_argument(
            "--event-ts",
            type=int,
            default=None,
            help="unused with an approval (the approved edge is stamped like agents.cli apply)",
        )
        p.add_argument("--approval", type=Path, default=None, help="signed approval JSON")
        p.add_argument(
            "--keyfile", type=Path, default=None, help="approver key file (outside the repo)"
        )
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else repo_root()
    try:
        if args.command == "bootstrap":
            return cmd_bootstrap(root, args.dry_run, args.force)
        if args.command == "status":
            return cmd_status(root)
        return cmd_manual(
            root,
            args.alpha_id,
            args.reason,
            args.event_ts,
            args.command,
            require_approval=True,
            approval=args.approval,
            keyfile=args.keyfile,
        )
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
