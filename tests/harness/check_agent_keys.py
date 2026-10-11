#!/usr/bin/env python3
"""tests/harness/check_agent_keys.py — the agent public-key registry covers the board (G-P1-2).

Every agent that has a SIGNED entry (an ``auth`` record) on
``research/agents/blackboard.jsonl`` must have its Ed25519 public key in
``research/agents/agent_pubkeys.json``; otherwise ``verify-board`` has
nothing to check the signature against.  An empty registry is tolerated
only while the board holds no ``auth`` entries (today: six operator
entries, none signed).  Every registry value must be a 32-byte hex key.

v1.14 (G-P1-4): a non-empty registry must also carry a maintainer
signature (``research/agents/agent_pubkeys.sig.json``) by a trust root of
``research/agents/trust_roots.json``.  With ``--trust-ref REF`` (CI:
``origin/main``) the trust roots are read from that ref, not the working
tree, so a change cannot vouch for itself.

Exit codes: 0 ok, 1 a problem (listed on stderr).

Usage:
    python tests/harness/check_agent_keys.py [--root .] [--trust-ref origin/main]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python" / "src"))

BOARD_RELPATH = Path("research") / "agents" / "blackboard.jsonl"
PUBKEYS_RELPATH = Path("research") / "agents" / "agent_pubkeys.json"
_HEX32 = re.compile(r"^[0-9a-f]{64}$")


def check(entries: list[dict], pubkeys: object) -> list[str]:
    """Problems with the registry against the board entries; ``[]`` = ok."""
    problems: list[str] = []
    if not isinstance(pubkeys, dict):
        return [f"{PUBKEYS_RELPATH} must be a JSON object agent id -> public key hex"]
    for agent, key in sorted(pubkeys.items()):
        if not isinstance(key, str) or not _HEX32.match(key):
            problems.append(f"registry key for {agent!r} is not 32-byte lowercase hex")
    signed = sorted({str(e.get("agent")) for e in entries if isinstance(e.get("auth"), dict)})
    if signed and not pubkeys:
        problems.append(f"the board has signed entries by {signed} but {PUBKEYS_RELPATH} is empty")
    else:
        for agent in signed:
            if agent not in pubkeys:
                problems.append(f"agent {agent!r} has signed board entries but no public key")
    return problems


def load(root: Path) -> tuple[list[dict], object]:
    board = root / BOARD_RELPATH
    entries = []
    if board.is_file():
        for line in board.read_text(encoding="ascii").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    reg = root / PUBKEYS_RELPATH
    pubkeys = json.loads(reg.read_text(encoding="ascii")) if reg.is_file() else {}
    return entries, pubkeys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--trust-ref", default=None, help="git ref to read the trust roots from")
    a = ap.parse_args(argv)
    from iap.agents import trust

    entries, pubkeys = load(a.root)
    problems = check(entries, pubkeys)
    if isinstance(pubkeys, dict):
        sig = a.root / trust.REGISTRY_SIG_RELPATH
        sig_doc = json.loads(sig.read_text(encoding="ascii")) if sig.is_file() else None
        problems += trust.verify_registry(pubkeys, sig_doc, trust.read_roots(a.root, a.trust_ref))
    signed = sum(1 for e in entries if isinstance(e.get("auth"), dict))
    n_keys = len(pubkeys) if isinstance(pubkeys, dict) else 0
    print(f"board entries {len(entries)} (signed {signed}); registry keys {n_keys}")
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
