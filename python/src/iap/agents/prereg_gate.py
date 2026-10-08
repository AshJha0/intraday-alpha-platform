"""The research runner's pre-registration gate (AL02).

``require`` runs before the runner touches any data: it verifies the
blackboard's hash chain and raises unless a hypothesis for (alpha, horizon)
is already on it.  Reading only - the runner never writes the blackboard.
"""

from __future__ import annotations

from pathlib import Path

from iap.agents.blackboard import Blackboard, BlackboardError, digest


class PreregistrationError(ValueError):
    pass


def board_path(repo_root: Path) -> Path:
    return Path(repo_root) / "research" / "agents" / "blackboard.jsonl"


def require(repo_root: Path, alpha_id: str, horizon: str, board: Path | None = None) -> dict:
    """The pre-registration entry for (alpha, horizon), or :class:`PreregistrationError`."""
    path = Path(board) if board else board_path(repo_root)
    bb = Blackboard(path)
    try:
        bb.verify()
    except BlackboardError as exc:
        raise PreregistrationError(f"{path}: {exc}") from exc
    pid = digest({"alpha": alpha_id, "horizon": horizon})[:16]
    for e in bb.entries():
        if e["kind"] == "prereg" and e["body"]["prereg_id"] == pid:
            return e
    raise PreregistrationError(
        f"{alpha_id}/{horizon} is not pre-registered on {path}; register the hypothesis "
        "first (WriteBroker.preregister), or pass --no-prereg for an exploratory run"
    )
