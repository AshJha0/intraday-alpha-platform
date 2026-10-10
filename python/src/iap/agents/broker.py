"""The write broker (AL01, AL02): the only path to agent-visible shared state.

Every write is validated, attributed to a registered agent id, stamped by the
broker's clock and chained into the blackboard, so the log replays to exactly
the current state (:meth:`WriteBroker.state`).  Agents hold no verdict,
lifecycle or order authority: a finding is a claim with citations, nothing
more.

**Authenticated identity (G4, v1.10).**  Constructed with ``pubkeys`` (agent
id -> Ed25519 public key, from the committed registry
``research/agents/agent_pubkeys.json``), the broker refuses every agent write
that does not carry a signed request: ``auth = sign(private_key, agent, op,
args)`` (:func:`sign`), an Ed25519 signature over the agent id, the
operation, a digest of its arguments and a single-use nonce.  The private key
never leaves the agent; the broker and every verifier hold only public keys,
so they cannot forge.  Legacy HMAC-SHA256 requests are refused.  The signed
request is stored on the entry (``auth``) so
:meth:`Blackboard.verify_signatures` can re-check it offline and a re-chained
board cannot forge it.  Without ``pubkeys`` the broker
behaves as in v1.9 (ids are bare strings) - the mode the existing tests,
the evals and the operator's own pre-registrations use.

**Pre-registration costs a look (G1, v1.10).**  :meth:`preregister` debits
one look on the multiple-testing ledger (default ``research/experiments.json``
under the root; ``kind="prereg"``) and stores the alpha's code fingerprint
(:mod:`iap.agents.fingerprint`) in the body (``format: 2``).  Pre-registrations
written before v1.10 (no ``format``) were never debited and are not debited
retroactively, so the committed ledger numbers do not move.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from iap.agents import citations, signing
from iap.agents.blackboard import Blackboard, BlackboardError, digest

AGENT_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
LEASE_NS = 15 * 60 * 10**9
PREREG_FORMAT = 2


class BrokerError(ValueError):
    pass


def sign(private_hex: str, agent: str, op: str, args: Mapping[str, Any]) -> dict[str, str]:
    """The ``auth`` an agent passes with a request: Ed25519 over (agent, op,
    args digest, fresh nonce), signed with the agent's PRIVATE key."""
    return signing.sign(private_hex, agent, op, dict(args))


def _default_fingerprint(alpha_id: str) -> dict[str, Any] | None:
    from iap.agents.fingerprint import fingerprint

    return fingerprint(alpha_id)


class WriteBroker:
    def __init__(
        self,
        root: Path,
        agents: set[str],
        *,
        clock: Callable[[], int] = time.time_ns,
        lease_ns: int = LEASE_NS,
        board: Path | None = None,
        trusted: set[str] | None = None,
        pubkeys: Mapping[str, str] | None = None,
        fingerprinter: Callable[[str], dict[str, Any] | None] = _default_fingerprint,
    ) -> None:
        self.root = Path(root)
        self.agents = frozenset(agents)
        #: non-agent writers (the evaluator, the approval service); never an agent id
        self.trusted = frozenset(trusted or ())
        if self.agents & self.trusted:
            raise ValueError("an agent cannot also be a trusted writer")
        #: per-agent Ed25519 PUBLIC keys, hex (G4); ``None`` = unauthenticated
        #: (v1.9 behaviour).  The broker holds no secret, so it cannot forge.
        self.pubkeys = dict(pubkeys) if pubkeys is not None else None
        if self.pubkeys is not None:
            missing = sorted(a for a in self.agents if a not in self.pubkeys)
            if missing:
                raise ValueError(f"authenticated broker: no public key for agents {missing}")
            if any(len(bytes.fromhex(k)) != 32 for k in self.pubkeys.values()):
                raise ValueError("agent public keys must be raw 32-byte Ed25519 keys (hex)")
        self.clock = clock
        self.lease_ns = lease_ns
        self.fingerprinter = fingerprinter
        self.board = Blackboard(board or self.root / "research" / "agents" / "blackboard.jsonl")

    # -- state ----------------------------------------------------------
    def state(self, now: int | None = None) -> dict[str, Any]:
        """Replay the log: tasks, live leases, findings, pre-registrations."""
        now = self.clock() if now is None else now
        tasks: dict[str, Any] = {}
        leases: dict[str, tuple[str, int]] = {}
        findings: list[dict[str, Any]] = []
        preregs: dict[str, Any] = {}
        for e in self.board.entries():
            b = e["body"]
            if e["kind"] == "task":
                tasks[b["task_id"]] = {**b, "created_by": e["agent"]}
            elif e["kind"] == "claim":
                leases[b["task_id"]] = (e["agent"], e["ts"] + b["lease_ns"])
            elif e["kind"] == "release":
                leases.pop(b["task_id"], None)
            elif e["kind"] == "finding":
                findings.append({**b, "agent": e["agent"], "seq": e["seq"]})
            elif e["kind"] == "prereg":
                # the latest entry for an id wins (a superseding re-registration)
                preregs[b["prereg_id"]] = {**b, "agent": e["agent"], "seq": e["seq"], "ts": e["ts"]}
        live = {t: {"agent": a, "expires": x} for t, (a, x) in leases.items() if x > now}
        return {"tasks": tasks, "leases": live, "findings": findings, "preregs": preregs}

    # -- identity -------------------------------------------------------
    def authenticate(
        self, agent: str, op: str, args: Mapping[str, Any], auth: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        """Check ``agent`` is registered and, on an authenticated broker, that
        ``auth`` is its valid, unused signature over (op, args).  Returns the
        ``auth`` record to store on the entry (``None`` when unauthenticated)."""
        if not AGENT_ID.match(str(agent)) or agent not in self.agents:
            raise BrokerError(f"unregistered agent {agent!r}")
        if self.pubkeys is None:
            return None
        if not auth or "nonce" not in auth or "sig" not in auth:
            raise BrokerError(f"{agent}: unsigned request (this broker requires signed requests)")
        if auth.get("scheme") != signing.ED25519:
            raise BrokerError(
                f"{agent}: signature scheme {auth.get('scheme')!r} refused; new requests "
                "must be ed25519 (hmac-sha256 is legacy, verify-only)"
            )
        nonce = str(auth["nonce"])
        args_digest = digest(dict(args))
        msg = signing.message(agent, op, args_digest, nonce)
        if not signing.verify_ed25519(self.pubkeys[agent], msg, str(auth["sig"])):
            raise BrokerError(f"{agent}: bad request signature for {op}")
        for e in self.board.entries():
            a = e.get("auth") or e["body"].get("request_auth") or {}
            if a.get("key_id") == agent and a.get("nonce") == nonce:
                raise BrokerError(f"{agent}: replayed request (nonce already used)")
        return {
            "scheme": signing.ED25519,
            "key_id": agent,
            "op": op,
            "args_digest": args_digest,
            "nonce": nonce,
            "sig": str(auth["sig"]),
        }

    def _write(
        self, kind: str, agent: str, body: dict[str, Any], auth: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            return self.board.append(kind, agent, self.clock(), body, auth)
        except BlackboardError as exc:
            raise BrokerError(str(exc)) from exc

    # -- writes ---------------------------------------------------------
    def write_trusted(self, writer: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """Entry for the evaluator and approval service only (reserve / approval kinds)."""
        if writer not in self.trusted or kind not in ("reserve", "approval"):
            raise BrokerError(f"{writer!r} may not write {kind!r}")
        return self._write(kind, writer, body)

    def post_task(
        self, agent: str, title: str, spec: dict[str, Any], *, auth: Mapping | None = None
    ) -> str:
        """Content-hashed: the same title+spec is the same task, posted once."""
        rec = self.authenticate(agent, "post_task", {"title": title, "spec": spec}, auth)
        task_id = digest({"title": title, "spec": spec})[:16]
        if task_id in self.state()["tasks"]:
            raise BrokerError(f"task {task_id} already posted")
        self._write("task", agent, {"task_id": task_id, "title": title, "spec": spec}, rec)
        return task_id

    def claim(self, agent: str, task_id: str, *, auth: Mapping | None = None) -> None:
        rec = self.authenticate(agent, "claim", {"task_id": task_id}, auth)
        st = self.state()
        if task_id not in st["tasks"]:
            raise BrokerError(f"unknown task {task_id}")
        held = st["leases"].get(task_id)
        if held and held["agent"] != agent:
            raise BrokerError(f"task {task_id} leased to {held['agent']}")
        self._write("claim", agent, {"task_id": task_id, "lease_ns": self.lease_ns}, rec)

    def release(self, agent: str, task_id: str, *, auth: Mapping | None = None) -> None:
        rec = self.authenticate(agent, "release", {"task_id": task_id}, auth)
        held = self.state()["leases"].get(task_id)
        if not held or held["agent"] != agent:
            raise BrokerError(f"{agent} holds no lease on {task_id}")
        self._write("release", agent, {"task_id": task_id}, rec)

    def file_finding(
        self,
        agent: str,
        task_id: str,
        title: str,
        text: str,
        refs: list[str],
        *,
        auth: Mapping | None = None,
    ) -> None:
        """A finding needs a live lease and at least one citation, all resolvable."""
        args = {"task_id": task_id, "title": title, "text": text, "refs": sorted(refs)}
        rec = self.authenticate(agent, "file_finding", args, auth)
        held = self.state()["leases"].get(task_id)
        if not held or held["agent"] != agent:
            raise BrokerError(f"{agent} holds no live lease on {task_id}")
        if not refs:
            raise BrokerError("a finding must cite at least one artefact")
        bad = citations.unresolved(refs, self.root)
        if bad:
            raise BrokerError(f"unresolvable citations: {bad}")
        self._write("finding", agent, args, rec)

    def preregister(
        self,
        agent: str,
        alpha_id: str,
        horizon: str,
        hypothesis: str,
        expected_sign: int,
        *,
        auth: Mapping | None = None,
        ledger: Path | None = None,
        dataset_version: str | None = None,
        supersede: bool = False,
    ) -> str:
        """Commit a hypothesis before any run.  One per (alpha, horizon).

        Debits one look on ``ledger`` (default ``<root>/research/experiments.json``;
        ``dataset_version`` scopes it as a dataset ledger is) and records the
        alpha's code fingerprint.  ``supersede`` re-registers an (alpha, horizon)
        whose code fingerprint has changed since its registration - a new
        hypothesis on new code, debited as a new look; it is refused when the
        fingerprint is unchanged."""
        args = {
            "alpha_id": alpha_id,
            "horizon": horizon,
            "hypothesis": hypothesis,
            "expected_sign": expected_sign,
        }
        rec = self.authenticate(agent, "preregister", args, auth)
        if expected_sign not in (-1, 1):
            raise BrokerError("expected_sign must be -1 or 1")
        if not hypothesis.strip():
            raise BrokerError("empty hypothesis")
        pid = digest({"alpha": alpha_id, "horizon": horizon})[:16]
        code = self.fingerprinter(alpha_id)
        prior = self.state()["preregs"].get(pid)
        if prior is not None:
            if not supersede:
                raise BrokerError(f"{alpha_id}/{horizon} already pre-registered")
            if prior.get("code") == code:
                raise BrokerError(
                    f"{alpha_id}/{horizon}: code fingerprint unchanged; nothing to supersede"
                )
        body: dict[str, Any] = {**args, "prereg_id": pid, "format": PREREG_FORMAT}
        if code is not None:
            body["code"] = code
        if prior is not None:
            body["supersedes_seq"] = prior["seq"]
        body["ledger_total"] = self._debit_look(
            pid, alpha_id, horizon, code, ledger, dataset_version
        )
        self._write("prereg", agent, body, rec)
        return pid

    def _debit_look(
        self,
        pid: str,
        alpha_id: str,
        horizon: str,
        code: dict[str, Any] | None,
        ledger: Path | None,
        dataset_version: str | None,
    ) -> int:
        from iap.validation.ledger import ExperimentLedger

        path = Path(ledger) if ledger is not None else self.root / "research" / "experiments.json"
        led = ExperimentLedger(path, dataset_version=dataset_version)
        config = {
            "horizon": horizon,
            "prereg_id": pid,
            "code_hash": (code or {}).get("code_hash"),
            "feature_hash": (code or {}).get("feature_hash"),
        }
        total = led.record(alpha_id, "prereg", config)
        led.save()
        return int(total)

    def is_preregistered(self, alpha_id: str, horizon: str) -> bool:
        return digest({"alpha": alpha_id, "horizon": horizon})[:16] in self.state()["preregs"]
