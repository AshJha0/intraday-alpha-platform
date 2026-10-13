"""The research runner's pre-registration gate (AL02, G1).

``require`` runs before the runner touches any data: it verifies the
blackboard's hash chain and raises unless a hypothesis for (alpha, horizon)
is already on it.  Since v1.10 it also refuses when the pre-registration
carries a code fingerprint (``format`` 2, :mod:`iap.agents.fingerprint`)
that no longer matches the alpha's current code or features: the hypothesis
was registered for different code.  Pre-registrations written before v1.10
carry no fingerprint and are accepted as before (their code is anchored by
the git commit that added them, see :mod:`iap.agents.anchor`).  Reading
only - the runner never writes the blackboard.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from iap.agents.blackboard import Blackboard, BlackboardError, digest


class PreregistrationError(ValueError):
    pass


def board_path(repo_root: Path) -> Path:
    return Path(repo_root) / "research" / "agents" / "blackboard.jsonl"


def _current(alpha_id: str) -> dict[str, Any] | None:
    from iap.agents.fingerprint import fingerprint

    return fingerprint(alpha_id)


def _closing_outcome(bb: Blackboard, prereg_id: str, prereg_seq: int) -> dict | None:
    """The latest outcome entry recorded for ``prereg_id`` after its
    registration by the outcomes writer, or ``None`` (the prereg is open)."""
    from iap.agents.workflow import OUTCOME_WRITER

    found = None
    for e in bb.entries():
        if (
            e["kind"] == "outcome"
            and e.get("agent") == OUTCOME_WRITER
            and e["seq"] > prereg_seq
            and e["body"].get("prereg_id") == prereg_id
        ):
            found = e
    return found


def is_closed(entry: dict) -> bool:
    """True for an entry :func:`require` returned for a closed prereg."""
    return bool(entry.get("closed"))


def require(
    repo_root: Path,
    alpha_id: str,
    horizon: str,
    board: Path | None = None,
    fingerprinter: Callable[[str], dict[str, Any] | None] | None = None,
    agents: frozenset[str] | set[str] | None = None,
) -> dict:
    """The (latest) pre-registration entry for (alpha, horizon), or
    :class:`PreregistrationError`.  ``agents`` (default: the ids in
    ``research/agents/agent_pubkeys.json`` under ``repo_root``) are the agent
    ids whose own pre-registrations are refused (A-P0-1)."""
    path = Path(board) if board else board_path(repo_root)
    bb = Blackboard(path)
    try:
        bb.verify()
    except BlackboardError as exc:
        raise PreregistrationError(f"{path}: {exc}") from exc
    pid = digest({"alpha": alpha_id, "horizon": horizon})[:16]
    found = None
    for e in bb.entries():
        if e["kind"] == "prereg" and e["body"]["prereg_id"] == pid:
            found = e
    if found is None:
        raise PreregistrationError(
            f"{alpha_id}/{horizon} is not pre-registered on {path}; register the hypothesis "
            "first (WriteBroker.preregister), or pass --no-prereg for an exploratory run"
        )
    # v1.15 A-P0-1: an agent may draft a pre-registration but never make one;
    # a prereg written by a registered agent id is refused until a human signs
    # off its draft (iap.agents.workflow, written by the ``signoff`` writer).
    from iap.agents.workflow import registered_agents

    if found["agent"] in (agents if agents is not None else registered_agents(repo_root)):
        raise PreregistrationError(
            f"{alpha_id}/{horizon}: pre-registered by agent {found['agent']!r} (entry "
            f"{found['seq']}); an agent pre-registration needs human sign-off "
            "(iap.agents.workflow.Workflow.sign_off) before a confirmatory run"
        )
    # v1.15 (owner decision 2026-10-12, option A): a prereg whose confirmatory
    # outcome is recorded on the board (kind "outcome", written by the
    # outcomes writer, after the prereg) is CLOSED.  Its look is spent, so the
    # code / deps fingerprint no longer blocks later runs of the pair; they are
    # post-confirmation runs, labelled as such, never evidence for that look.
    outcome = _closing_outcome(bb, pid, found["seq"])
    if outcome is not None:
        return {
            **found,
            "closed": {
                "outcome": outcome["body"]["outcome"],
                "outcome_seq": outcome["seq"],
                "ref": outcome["body"].get("ref"),
            },
        }
    registered = found["body"].get("code")
    if registered is not None:
        now = (fingerprinter or _current)(alpha_id)
        if now is None:
            raise PreregistrationError(
                f"{alpha_id}/{horizon}: registered with a code fingerprint but the alpha "
                "no longer exists"
            )
        keys = ("code_hash", "feature_hash", "deps")
        changed = [k for k in keys if k in registered and registered.get(k) != now.get(k)]
        if changed:
            raise PreregistrationError(
                f"{alpha_id}/{horizon}: {' and '.join(changed)} changed since pre-registration "
                f"(entry {found['seq']}); the hypothesis was registered for different code. "
                "Re-register (WriteBroker.preregister(..., supersede=True), a new look) or "
                "pass --no-prereg for an exploratory run"
            )
    return found
