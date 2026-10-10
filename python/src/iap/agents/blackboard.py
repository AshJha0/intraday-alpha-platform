"""Append-only, hash-chained blackboard (AL01).

One JSON object per line: ``seq``, ``prev`` (hash of the previous line),
``kind``, ``agent``, ``ts`` (ns, supplied by the caller), ``body``, ``hash``.
Editing, deleting or reordering any line breaks the chain and
:meth:`Blackboard.verify` says where.  Only :mod:`iap.agents.broker` appends.

v1.10 (G4) adds an OPTIONAL ``auth`` field on entries written through an
authenticated broker: the agent's signed request (``key_id``, ``op``,
``args_digest``, ``nonce``, ``mac``; see :func:`request_mac`).  It is part
of the hashed entry like any other field, so v1.9 code verifies a v1.10
board unchanged and v1.9 entries (no ``auth``) verify under v1.10: no
migration is needed.  A plain hash chain can be rebuilt by anyone with
write access (re-chaining); :meth:`Blackboard.verify_signatures` and
:mod:`iap.agents.anchor` (G3) are the external checks that catch it.
"""

from __future__ import annotations

import hashlib
import hmac
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


def request_mac(key: bytes, agent: str, op: str, args: Any, nonce: str) -> str:
    """HMAC-SHA256 an agent puts on a broker request (G4).

    Covers the agent id, the operation, a digest of its arguments and a
    single-use nonce, so a signature cannot be moved to another agent,
    operation or argument set, nor replayed."""
    return _mac(key, agent, op, digest(args), nonce)


def _mac(key: bytes, agent: str, op: Any, args_digest: Any, nonce: Any) -> str:
    msg = canonical({"agent": agent, "op": op, "args_digest": args_digest, "nonce": nonce})
    return hmac.new(key, msg.encode("ascii"), hashlib.sha256).hexdigest()


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

    def verify_signatures(self, keys: dict[str, bytes]) -> list[str]:
        """Problems with the ``auth`` of every entry (G4); ``[]`` = all good.

        ``keys`` maps agent id -> key.  An entry by an agent that has a key
        must carry a valid ``auth`` (a re-chained entry cannot be re-signed
        without the key); entries by any other writer (v1.9 entries, the
        operator, trusted writers) are not checked here."""
        problems: list[str] = []
        for e in self.entries():
            agent = e.get("agent", "")
            key = keys.get(agent)
            if key is None:
                continue
            a = e.get("auth")
            if not isinstance(a, dict):
                problems.append(f"entry {e.get('seq')}: unsigned write by keyed agent {agent}")
                continue
            want = _mac(key, agent, a.get("op"), a.get("args_digest"), a.get("nonce"))
            if a.get("key_id") != agent or not hmac.compare_digest(want, str(a.get("mac"))):
                problems.append(f"entry {e.get('seq')}: bad signature for {agent}")
        return problems

    def append(
        self,
        kind: str,
        agent: str,
        ts: int,
        body: dict[str, Any],
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Broker-only.  Verifies the chain first so a tampered log is never extended."""
        if kind not in KINDS:
            raise BlackboardError(f"unknown kind {kind!r}")
        n = self.verify()
        prev = self.entries()[-1]["hash"] if n else GENESIS
        entry = {"seq": n + 1, "prev": prev, "kind": kind, "agent": agent, "ts": ts, "body": body}
        if auth is not None:
            entry["auth"] = auth
        entry["hash"] = digest(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="ascii", newline="\n") as fh:
            fh.write(canonical(entry) + "\n")
        return entry
