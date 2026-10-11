#!/usr/bin/env python3
"""tests/harness/check_ledger_monotone.py — research state only grows (G-P1-1).

The multiple-testing ledger and the agent blackboard are append-only.  A
pull request that merges a research branch must not lose looks or rewrite
board history.  Against a base ref (default ``origin/main``) this checks the
working tree:

1. ``research/experiments.json``: ``total_experiments`` at head >= base, and
   every base entry ``key`` is still present (merged by entry identity, never
   replaced);
2. ``research/models/ledger.json``: ``experiment_count`` at head >= base;
3. ``research/agents/blackboard.jsonl``: head length >= base, and the base
   board's bytes are a byte-identical prefix of the head board's
   (append-only: no entry edited, removed, reordered or re-chained).

A file absent at the base is not checked (nothing to lose).  CRLF is
normalised to LF on both sides so a Windows checkout compares like CI.
Needs full history (``actions/checkout`` with ``fetch-depth: 0``).

Exit codes: 0 ok, 1 a regression (listed on stderr), 2 the base ref is
missing.

Usage:
    python tests/harness/check_ledger_monotone.py [--base origin/main] [--root .]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

LEDGER = "research/experiments.json"
MODELS_LEDGER = "research/models/ledger.json"
BOARD = "research/agents/blackboard.jsonl"


def _lf(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def compare_ledger(base: dict, head: dict) -> list[str]:
    problems = []
    bt, ht = int(base.get("total_experiments", 0)), int(head.get("total_experiments", 0))
    if ht < bt:
        problems.append(f"{LEDGER}: total_experiments {ht} < base {bt} (looks were lost)")
    head_keys = {e.get("key") for e in head.get("entries", [])}
    lost = [e.get("key") for e in base.get("entries", []) if e.get("key") not in head_keys]
    if lost:
        problems.append(f"{LEDGER}: {len(lost)} base entries missing at head, e.g. {lost[:3]}")
    return problems


def compare_models_ledger(base: dict, head: dict) -> list[str]:
    bc, hc = int(base.get("experiment_count", 0)), int(head.get("experiment_count", 0))
    if hc < bc:
        return [f"{MODELS_LEDGER}: experiment_count {hc} < base {bc}"]
    return []


def compare_board(base: bytes, head: bytes) -> list[str]:
    base, head = _lf(base), _lf(head)
    bn = sum(1 for x in base.splitlines() if x.strip())
    hn = sum(1 for x in head.splitlines() if x.strip())
    problems = []
    if hn < bn:
        problems.append(f"{BOARD}: {hn} entries < base {bn}")
    if not head.startswith(base):
        problems.append(
            f"{BOARD}: the base board is not a byte-identical prefix of the head board "
            "(an entry was edited, removed, reordered or re-chained; re-chain only via "
            "`python -m iap.agents.cli anchor` after a reviewed merge)"
        )
    return problems


def _show(root: Path, ref: str, rel: str) -> bytes | None:
    out = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{rel}"], capture_output=True, check=False
    )
    return out.stdout if out.returncode == 0 else None


def check(root: Path, base_ref: str) -> list[str]:
    problems: list[str] = []
    for rel, fn in ((LEDGER, compare_ledger), (MODELS_LEDGER, compare_models_ledger)):
        base = _show(root, base_ref, rel)
        if base is None:
            continue
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: present at {base_ref} but deleted at head")
            continue
        problems += fn(json.loads(base), json.loads(path.read_bytes()))
    base = _show(root, base_ref, BOARD)
    if base is not None:
        path = root / BOARD
        if not path.is_file():
            problems.append(f"{BOARD}: present at {base_ref} but deleted at head")
        else:
            problems += compare_board(base, path.read_bytes())
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    a = ap.parse_args(argv)
    ok = subprocess.run(
        ["git", "-C", str(a.root), "rev-parse", "--verify", "--quiet", a.base + "^{commit}"],
        capture_output=True,
        check=False,
    )
    if ok.returncode != 0:
        print(f"error: base ref {a.base!r} not found (fetch-depth 0?)", file=sys.stderr)
        return 2
    problems = check(a.root, a.base)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if not problems:
        print(f"ledgers and blackboard are monotone vs {a.base}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
