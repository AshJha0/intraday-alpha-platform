"""Authenticated HUMAN approvals for lifecycle edges (AL04).

Every lifecycle edge whose actor is HUMAN (``retire``, ``reset``) needs an
approval signed with that person's secret.  An agent holds no secret, so it
can request but never grant.  An approval names one alpha, one action and
one reason, expires, and is single use (its nonce is recorded on the
blackboard).  HMAC-SHA256 over the canonical body; secrets live outside the
repository (a JSON map approver id -> hex secret the operator supplies).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from typing import Any

from iap.agents.blackboard import canonical
from iap.agents.broker import WriteBroker
from iap.contracts.types import Actor

ACTIONS = ("retire", "reset")
APPROVER = "approvals"  # trusted writer id on the blackboard


class ApprovalError(ValueError):
    pass


def _mac(secret: str, body: Mapping[str, Any]) -> str:
    key = bytes.fromhex(secret)
    return hmac.new(key, canonical(dict(body)).encode("ascii"), hashlib.sha256).hexdigest()


def new_secret() -> str:
    return secrets.token_hex(32)


def issue(
    approver: str, secret: str, alpha_id: str, action: str, reason: str, now: int, ttl_ns: int
) -> dict[str, Any]:
    """Run by the human.  Returns a signed approval to hand to :func:`apply`."""
    if action not in ACTIONS:
        raise ApprovalError(f"action must be one of {ACTIONS}")
    if not reason.strip():
        raise ApprovalError("a non-empty reason is required")
    body = {
        "approver": approver,
        "alpha_id": alpha_id,
        "action": action,
        "reason": reason,
        "nonce": secrets.token_hex(8),
        "expires": now + ttl_ns,
    }
    return {**body, "mac": _mac(secret, body)}


def verify(approval: Mapping[str, Any], secrets_by_approver: Mapping[str, str], now: int) -> None:
    body = {k: v for k, v in approval.items() if k != "mac"}
    secret = secrets_by_approver.get(str(body.get("approver")))
    if secret is None:
        raise ApprovalError("unknown approver")
    if not hmac.compare_digest(_mac(secret, body), str(approval.get("mac", ""))):
        raise ApprovalError("bad signature")
    if body.get("action") not in ACTIONS:
        raise ApprovalError("bad action")
    if now >= int(body["expires"]):
        raise ApprovalError("approval expired")


def apply(
    approval: Mapping[str, Any],
    secrets_by_approver: Mapping[str, str],
    broker: WriteBroker,
    lifecycle: Any,
    event_ts: int,
) -> Any:
    """Verify, burn the nonce, then perform the HUMAN edge.  Replay is refused."""
    verify(approval, secrets_by_approver, broker.clock())
    nonce = approval["nonce"]
    used = {e["body"].get("nonce") for e in broker.board.entries() if e["kind"] == "approval"}
    if nonce in used:
        raise ApprovalError("approval already used")
    broker.write_trusted(
        APPROVER,
        "approval",
        {
            "nonce": nonce,
            "approver": approval["approver"],
            "alpha_id": approval["alpha_id"],
            "action": approval["action"],
            "reason": approval["reason"],
        },
    )
    method = lifecycle.retire if approval["action"] == "retire" else lifecycle.reset_to_research
    return method(approval["alpha_id"], event_ts, approval["reason"], actor=Actor.HUMAN)
