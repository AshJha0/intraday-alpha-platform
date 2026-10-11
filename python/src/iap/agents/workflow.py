"""The narrowed agent job (v1.15 A-P0-1, A-P1-2): propose -> draft prereg -> HUMAN sign-off.

An agent may only

1. **propose** a hypothesis (:meth:`Workflow.propose`) - one look debited on the
   chained multiple-testing ledger (``kind="agent_proposal"``, ledger format v4
   hash chain), whether or not the proposal is ever tested (A-P1-2);
2. **draft** the pre-registration of its own proposal
   (:meth:`Workflow.draft_prereg`) - a ``prereg_draft`` board entry carrying
   the alpha's code fingerprint.  A draft is NOT a pre-registration: the
   confirmatory gate (:func:`iap.agents.prereg_gate.require`) ignores it.

A HUMAN then signs the draft off (:func:`issue_signoff` with a secret the
agent never holds, :meth:`Workflow.sign_off`), which writes the ``prereg``
entry the gate accepts - written by the trusted ``signoff`` writer, never an
agent id, and without a second ledger debit (the proposal already paid the
look).  Confirmatory runs and their outcomes are human-only
(:func:`require_human`, :meth:`Workflow.record_outcome`); nothing in this
module or in :mod:`iap.llm` can promote an alpha.

:func:`hit_rate` reports, per look, what became of every proposal (signed
off? confirmed?) beside the Bonferroni t threshold that look raised the
ledger to.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from iap.agents.blackboard import canonical, digest
from iap.agents.broker import BrokerError, WriteBroker, board_id

SIGNOFF_WRITER = "signoff"
OUTCOME_WRITER = "outcomes"
PROPOSAL_KIND = "agent_proposal"  # ledger kind of a proposal look
PREREG_FORMAT_SIGNED_OFF = 3
OUTCOMES = ("confirmed", "rejected", "inconclusive")
#: operations an agent can never perform (checked by :func:`require_human`)
HUMAN_ONLY_OPS = frozenset({"sign_off", "confirmatory_run", "record_outcome", "promote"})


class WorkflowError(ValueError):
    pass


class AgentAuthorityError(WorkflowError):
    """An agent tried a human-only operation."""


def registered_agents(root: Path) -> frozenset[str]:
    """Agent ids with a key in ``research/agents/agent_pubkeys.json`` under ``root``."""
    path = Path(root) / "research" / "agents" / "agent_pubkeys.json"
    if not path.is_file():
        return frozenset()
    return frozenset(json.loads(path.read_text(encoding="ascii")))


def require_human(actor: str, op: str, agents: frozenset[str] | set[str]) -> None:
    """Raise :class:`AgentAuthorityError` when ``actor`` is an agent and ``op``
    is human-only (sign-off, confirmatory run, outcome, promotion)."""
    if op not in HUMAN_ONLY_OPS:
        raise WorkflowError(f"unknown human-only op {op!r}")
    if not str(actor).strip() or actor in agents:
        raise AgentAuthorityError(f"{actor!r} is an agent; {op} is human-only")


def proposal_id(alpha_id: str, horizon: str, expected_sign: int, agent: str) -> str:
    return digest({"a": alpha_id, "h": horizon, "s": int(expected_sign), "g": agent})[:16]


# -- human sign-off (HMAC over the draft; the agent holds no secret) -------
def _mac(secret: str, body: Mapping[str, Any]) -> str:
    key = bytes.fromhex(secret)
    return hmac.new(key, canonical(dict(body)).encode("ascii"), hashlib.sha256).hexdigest()


def issue_signoff(approver: str, secret: str, draft_hash: str, reason: str) -> dict[str, Any]:
    """Run by the human: sign off the draft board entry ``draft_hash``."""
    if not reason.strip():
        raise WorkflowError("a sign-off needs a reason")
    body = {
        "approver": approver,
        "draft_hash": draft_hash,
        "reason": reason,
        "nonce": secrets.token_hex(8),
    }
    return {**body, "mac": _mac(secret, body)}


class Workflow:
    """The agent-facing proposal workflow over a :class:`WriteBroker`."""

    def __init__(self, broker: WriteBroker, *, ledger: Path | None = None) -> None:
        self.broker = broker
        self.ledger = Path(ledger) if ledger else broker.root / "research" / "experiments.json"

    # -- agent steps ----------------------------------------------------
    def propose(
        self,
        agent: str,
        alpha_id: str,
        horizon: str,
        expected_sign: int,
        rationale: str,
        *,
        auth: Mapping | None = None,
    ) -> dict[str, Any]:
        """Record a proposal and debit one look on the chained ledger."""
        args = {
            "alpha_id": alpha_id,
            "horizon": horizon,
            "expected_sign": expected_sign,
            "rationale": rationale,
        }
        rec = self.broker.authenticate(agent, "propose", args, auth)
        if expected_sign not in (-1, 1):
            raise WorkflowError("expected_sign must be -1 or 1")
        if not str(rationale).strip():
            raise WorkflowError("empty rationale")
        pid = proposal_id(alpha_id, horizon, expected_sign, agent)
        if pid in self.proposals():
            raise WorkflowError(f"proposal {pid} already made (no second look)")
        from iap.validation.ledger import ExperimentLedger

        led = ExperimentLedger(self.ledger)
        total = int(
            led.record(
                alpha_id,
                PROPOSAL_KIND,
                {"horizon": horizon, "expected_sign": int(expected_sign), "proposal_id": pid},
            )
        )
        led.save()
        body = {
            **args,
            "proposal_id": pid,
            "ledger_total": total,
            "ledger_head": led.chain_head,
            "ledger": board_id(self.broker.root, self.ledger),
        }
        self.broker._write("proposal", agent, body, rec)
        return {"proposal_id": pid, "ledger_total": total, "ledger_head": led.chain_head}

    def draft_prereg(
        self, agent: str, pid: str, hypothesis: str, *, auth: Mapping | None = None
    ) -> dict[str, Any]:
        """Draft the pre-registration of the agent's OWN proposal (no look)."""
        args = {"proposal_id": pid, "hypothesis": hypothesis}
        rec = self.broker.authenticate(agent, "draft_prereg", args, auth)
        prop = self.proposals().get(pid)
        if prop is None or prop["agent"] != agent:
            raise WorkflowError(f"{agent} has no proposal {pid}")
        if not str(hypothesis).strip():
            raise WorkflowError("empty hypothesis")
        if pid in self.drafts():
            raise WorkflowError(f"proposal {pid} already drafted")
        body: dict[str, Any] = {
            "proposal_id": pid,
            "alpha_id": prop["alpha_id"],
            "horizon": prop["horizon"],
            "expected_sign": prop["expected_sign"],
            "hypothesis": hypothesis,
            "status": "awaiting_human_signoff",
        }
        code = self.broker.fingerprinter(prop["alpha_id"])
        if code is not None:
            body["code"] = code
        entry = self.broker._write("prereg_draft", agent, body, rec)
        return {"draft_hash": entry["hash"], "status": body["status"]}

    # -- human steps ----------------------------------------------------
    def sign_off(self, signoff: Mapping[str, Any], secrets_by_approver: Mapping[str, str]) -> str:
        """Verify a human sign-off and write the ``prereg`` the gate accepts."""
        body = {k: v for k, v in signoff.items() if k != "mac"}
        approver = str(body.get("approver"))
        require_human(approver, "sign_off", self.broker.agents)
        secret = secrets_by_approver.get(approver)
        if secret is None or not hmac.compare_digest(
            _mac(secret, body), str(signoff.get("mac", ""))
        ):
            raise WorkflowError("bad or unknown sign-off signature")
        used = {e["body"].get("signoff_nonce") for e in self.broker.board.entries()}
        if body.get("nonce") in used:
            raise WorkflowError("sign-off already used")
        draft = next(
            (e for e in self.broker.board.entries() if e["hash"] == body.get("draft_hash")),
            None,
        )
        if draft is None or draft["kind"] != "prereg_draft":
            raise WorkflowError("sign-off names no draft on this board")
        d = draft["body"]
        prereg_id = digest({"alpha": d["alpha_id"], "horizon": d["horizon"]})[:16]
        if prereg_id in self.broker.state()["preregs"]:
            raise WorkflowError(f"{d['alpha_id']}/{d['horizon']} already pre-registered")
        prop = self.proposals()[d["proposal_id"]]
        out: dict[str, Any] = {
            "alpha_id": d["alpha_id"],
            "horizon": d["horizon"],
            "hypothesis": d["hypothesis"],
            "expected_sign": d["expected_sign"],
            "prereg_id": prereg_id,
            "format": PREREG_FORMAT_SIGNED_OFF,
            "proposal_id": d["proposal_id"],
            "proposed_by": draft["agent"],
            "draft_seq": draft["seq"],
            "approver": approver,
            "signoff_reason": body["reason"],
            "signoff_nonce": body["nonce"],
            # the look was debited by the proposal; no second debit
            "ledger_total": prop["ledger_total"],
            "ledger_head": prop["ledger_head"],
            "ledger": prop["ledger"],
        }
        if "code" in d:
            out["code"] = d["code"]
        self.broker.write_trusted(SIGNOFF_WRITER, "prereg", out)
        return prereg_id

    def record_outcome(self, actor: str, prereg_id: str, outcome: str, ref: str) -> None:
        """Human-only: the verdict of the confirmatory run of a signed-off prereg."""
        require_human(actor, "record_outcome", self.broker.agents)
        if outcome not in OUTCOMES:
            raise WorkflowError(f"outcome must be one of {OUTCOMES}")
        if prereg_id not in self.broker.state()["preregs"]:
            raise WorkflowError(f"unknown prereg {prereg_id}")
        body = {"prereg_id": prereg_id, "outcome": outcome, "ref": ref, "recorded_by": actor}
        self.broker.write_trusted(OUTCOME_WRITER, "outcome", body)

    # -- views ----------------------------------------------------------
    def proposals(self) -> dict[str, dict[str, Any]]:
        return {
            e["body"]["proposal_id"]: {**e["body"], "agent": e["agent"], "seq": e["seq"]}
            for e in self.broker.board.entries()
            if e["kind"] == "proposal"
        }

    def drafts(self) -> dict[str, dict[str, Any]]:
        return {
            e["body"]["proposal_id"]: {**e["body"], "hash": e["hash"], "seq": e["seq"]}
            for e in self.broker.board.entries()
            if e["kind"] == "prereg_draft"
        }


def hit_rate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-look report over a board's entries: each agent proposal (a look),
    whether it was signed off, its confirmatory outcome, the cumulative hit
    rate (confirmed / looks) and the Bonferroni t threshold at that look."""
    from iap.validation.ledger import ExperimentLedger

    signed: dict[str, str] = {}
    outcome_by_prereg: dict[str, str] = {}
    for e in entries:
        if e["kind"] == "prereg" and e["body"].get("format") == PREREG_FORMAT_SIGNED_OFF:
            signed[e["body"]["proposal_id"]] = e["body"]["prereg_id"]
        elif e["kind"] == "outcome":
            outcome_by_prereg[e["body"]["prereg_id"]] = e["body"]["outcome"]
    rows = []
    hits = 0
    for n, e in enumerate((x for x in entries if x["kind"] == "proposal"), start=1):
        b = e["body"]
        prereg = signed.get(b["proposal_id"])
        outcome = outcome_by_prereg.get(prereg) if prereg else None
        hits += outcome == "confirmed"
        rows.append(
            {
                "look": n,
                "agent": e["agent"],
                "alpha_id": b["alpha_id"],
                "horizon": b["horizon"],
                "ledger_total": b["ledger_total"],
                "bonferroni_t": round(
                    ExperimentLedger.bonferroni_t_threshold_at(int(b["ledger_total"])), 4
                ),
                "signed_off": prereg is not None,
                "outcome": outcome,
                "hit_rate": round(hits / n, 4),
            }
        )
    looks = len(rows)
    return {
        "looks": looks,
        "signed_off": sum(r["signed_off"] for r in rows),
        "confirmed": hits,
        "hit_rate": round(hits / looks, 4) if looks else None,
        "per_look": rows,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    from iap.agents.blackboard import Blackboard

    ap = argparse.ArgumentParser(prog="iap.agents.workflow", description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--board", type=Path, default=None)
    a = ap.parse_args(argv)
    board = Blackboard(a.board or a.root / "research" / "agents" / "blackboard.jsonl")
    board.verify()
    print(json.dumps(hit_rate(board.entries()), indent=2, sort_keys=True))
    return 0


__all__ = [
    "AgentAuthorityError",
    "BrokerError",
    "HUMAN_ONLY_OPS",
    "Workflow",
    "WorkflowError",
    "hit_rate",
    "issue_signoff",
    "registered_agents",
    "require_human",
]

if __name__ == "__main__":
    raise SystemExit(main())
