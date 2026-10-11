"""Append-only, hash-chained blackboard (AL01).

One JSON object per line: ``seq``, ``prev`` (hash of the previous line),
``kind``, ``agent``, ``ts`` (ns, supplied by the caller), ``body``, ``hash``.
Editing, deleting or reordering any line breaks the chain and
:meth:`Blackboard.verify` says where.  Only :mod:`iap.agents.broker` appends.

v1.10 (G4) adds an OPTIONAL ``auth`` field on entries written through an
authenticated broker: the agent's signed request (``scheme``, ``key_id``,
``op``, ``args_digest``, ``nonce``, ``sig``; :mod:`iap.agents.signing`,
Ed25519 by default; records without ``scheme`` are legacy HMAC-SHA256 with
``mac``, see :func:`request_mac`, verify-only).  It is part
of the hashed entry like any other field, so v1.9 code verifies a v1.10
board unchanged and v1.9 entries (no ``auth``) verify under v1.10: no
migration is needed.  A plain hash chain can be rebuilt by anyone with
write access (re-chaining); :meth:`Blackboard.verify_signatures` and
:mod:`iap.agents.anchor` (G3) are the external checks that catch it.

v1.13 (G-P2-1): :meth:`Blackboard.append` verifies and appends under an
exclusive :class:`iap.experiment.locking.FileLock` on ``<board>.lock``, so
two writers can no longer both verify the same head and write two entries
with the same ``seq``.  ``ts`` must be non-decreasing along the board; an
append stamped earlier than the head entry is refused.
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
    """LEGACY HMAC-SHA256 over a broker request (the first v1.10 draft).

    Kept so stored ``hmac-sha256`` records can be re-verified (and tests can
    build them); the broker no longer accepts new HMAC-signed requests -
    agents sign with Ed25519 (:func:`iap.agents.signing.sign`)."""
    return _mac(key, agent, op, digest(args), nonce)


def _mac(key: bytes, agent: str, op: Any, args_digest: Any, nonce: Any) -> str:
    msg = canonical({"agent": agent, "op": op, "args_digest": args_digest, "nonce": nonce})
    return hmac.new(key, msg.encode("ascii"), hashlib.sha256).hexdigest()


class Blackboard:
    def __init__(self, path: Path, lock_timeout_s: float = 30.0) -> None:
        self.path = Path(path)
        self.lock_timeout_s = float(lock_timeout_s)

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

    def verify_signatures(
        self,
        pubkeys: dict[str, str],
        legacy_hmac_keys: dict[str, bytes] | None = None,
    ) -> list[str]:
        """Problems with the signed requests on the board (G4); ``[]`` = all good.

        ``pubkeys`` maps agent id -> Ed25519 public key (hex): an entry by an
        agent with a registered key must carry a valid ``auth`` (a re-chained
        entry cannot be re-signed without the private key).  Every entry that
        carries an ``auth`` is checked: ``ed25519`` against ``pubkeys``,
        legacy ``hmac-sha256`` against ``legacy_hmac_keys``.  Entries by other
        writers without ``auth`` (v1.9 entries, the operator, trusted
        writers) are not checked here."""
        from iap.agents import signing

        problems: list[str] = []
        legacy = legacy_hmac_keys or {}
        for e in self.entries():
            agent = e.get("agent", "")
            a = e.get("auth")
            if not isinstance(a, dict):
                if agent in pubkeys or agent in legacy:
                    problems.append(f"entry {e.get('seq')}: unsigned write by keyed agent {agent}")
                continue
            why = signing.verify(a, agent, pubkeys, legacy)
            if why is not None:
                problems.append(f"entry {e.get('seq')}: {why}")
        return problems

    def append(
        self,
        kind: str,
        agent: str,
        ts: int,
        body: dict[str, Any],
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Broker-only.  Verifies the chain first so a tampered log is never
        extended; verify and append run under one exclusive file lock (v1.13)."""
        from iap.experiment.locking import FileLock

        if kind not in KINDS:
            raise BlackboardError(f"unknown kind {kind!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path, timeout_s=self.lock_timeout_s):
            n = self.verify()
            last = self.entries()[-1] if n else None
            prev = last["hash"] if last is not None else GENESIS
            if last is not None and int(ts) < int(last.get("ts", 0)):
                raise BlackboardError(
                    f"ts {ts} is earlier than the head entry's ts {last.get('ts')} "
                    "(ts must be non-decreasing)"
                )
            entry = {
                "seq": n + 1,
                "prev": prev,
                "kind": kind,
                "agent": agent,
                "ts": ts,
                "body": body,
            }
            if auth is not None:
                entry["auth"] = auth
            entry["hash"] = digest(entry)
            with self.path.open("a", encoding="ascii", newline="\n") as fh:
                fh.write(canonical(entry) + "\n")
        return entry
