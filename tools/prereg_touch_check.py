#!/usr/bin/env python3
"""tools/prereg_touch_check.py: warn when code an OPEN pre-registration fingerprints changed (v1.16 item 12).

An open pre-registration (no outcome recorded on the board) commits to the
alpha's code and feature fingerprints (``iap.agents.fingerprint``). A PR that
changes that code silently invalidates the registration: ``prereg_gate.require``
then refuses the registered run, as happened to R7 in v1.15 (#117). This check
compares every open, fingerprinted prereg on the board with the working tree
and prints one GitHub Actions ``::warning::`` per mismatch. It never fails the
job (exit 0); ``--strict`` exits 1 on a mismatch, for local use.

Usage: python tools/prereg_touch_check.py [--board PATH] [--strict]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.agents.blackboard import Blackboard  # noqa: E402
from iap.agents.fingerprint import fingerprint  # noqa: E402
from iap.agents.prereg_gate import _closing_outcome  # noqa: E402

KEYS = ("code_hash", "feature_hash")


def _registered(code) -> dict:
    if isinstance(code, dict):
        return code
    try:
        return ast.literal_eval(str(code))
    except (ValueError, SyntaxError):
        return {}


def open_mismatches(board: Path) -> list[dict]:
    """Open fingerprinted preregs whose code or feature hash changed."""
    bb = Blackboard(board)
    bb.verify()
    latest: dict[str, dict] = {}
    for e in bb.entries():
        if e["kind"] == "prereg":
            latest[e["body"]["prereg_id"]] = e
    out = []
    for pid, e in sorted(latest.items(), key=lambda kv: kv[1]["seq"]):
        reg = _registered(e["body"].get("code"))
        if not reg or _closing_outcome(bb, pid, e["seq"]) is not None:
            continue
        now = fingerprint(e["body"]["alpha_id"]) or {}
        changed = [k for k in KEYS if reg.get(k) and now.get(k) != reg.get(k)]
        if changed:
            out.append(
                {
                    "seq": e["seq"],
                    "alpha_id": e["body"]["alpha_id"],
                    "horizon": e["body"]["horizon"],
                    "changed": changed,
                    "modules": now.get("modules"),
                }
            )
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--board", type=Path, default=REPO / "research" / "agents" / "blackboard.jsonl")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    bad = open_mismatches(a.board)
    for m in bad:
        print(
            f"::warning::open pre-registration seq {m['seq']} ({m['alpha_id']}/{m['horizon']}): "
            f"{', '.join(m['changed'])} changed (modules {m['modules']}); the registered run will be "
            "refused by prereg_gate unless re-registered"
        )
    if not bad:
        print("prereg touch check: no open pre-registration's fingerprinted code changed")
    return 1 if bad and a.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
