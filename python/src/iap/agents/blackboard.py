"""Append-only, hash-chained blackboard (AL01).

One JSON object per line: ``seq``, ``prev`` (hash of the previous line),
``kind``, ``agent``, ``ts`` (ns, supplied by the caller), ``body``, ``hash``.
Editing, deleting or reordering any line breaks the chain and
:meth:`Blackboard.verify` says where.  Only :mod:`iap.agents.broker` appends.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
KINDS = ("task", "claim", "release", "finding", "prereg", "reserve", "approval")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("ascii")).hexdigest()


class BlackboardError(ValueError):
    pass


class Blackboard:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = []
        for n, line in enumerate(self.path.read_text(encoding="ascii").splitlines(), 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError as exc:
                    raise BlackboardError(f"line {n}: not JSON") from exc
        return out

    def verify(self) -> int:
        """Entry count if the chain is intact, else :class:`BlackboardError`."""
        prev = GENESIS
        count = 0
        for i, e in enumerate(self.entries(), 1):
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("seq") != i or e.get("prev") != prev or e.get("hash") != digest(body):
                raise BlackboardError(f"chain broken at entry {i}")
            prev = e["hash"]
            count = i
        return count

    def append(self, kind: str, agent: str, ts: int, body: dict[str, Any]) -> dict[str, Any]:
        """Broker-only.  Verifies the chain first so a tampered log is never extended."""
        if kind not in KINDS:
            raise BlackboardError(f"unknown kind {kind!r}")
        n = self.verify()
        prev = self.entries()[-1]["hash"] if n else GENESIS
        entry = {"seq": n + 1, "prev": prev, "kind": kind, "agent": agent, "ts": ts, "body": body}
        entry["hash"] = digest(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="ascii", newline="\n") as fh:
            fh.write(canonical(entry) + "\n")
        return entry
