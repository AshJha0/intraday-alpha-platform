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


def require(
    repo_root: Path,
    alpha_id: str,
    horizon: str,
    board: Path | None = None,
    fingerprinter: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict:
    """The (latest) pre-registration entry for (alpha, horizon), or
    :class:`PreregistrationError`."""
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
