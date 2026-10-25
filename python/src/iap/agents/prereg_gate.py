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

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iap.agents.blackboard import Blackboard, BlackboardError, digest

#: v1.19 R3 (defect D-08): the ledger family dependency map, ``tools/ledger_family_map.py``
FAMILY_MAP_REL = "research/validation/LEDGER_FAMILY_MAP.json"


class PreregistrationError(ValueError):
    pass


def board_path(repo_root: Path) -> Path:
    return Path(repo_root) / "research" / "agents" / "blackboard.jsonl"


def family_map_path(repo_root: Path) -> Path:
    return Path(repo_root) / FAMILY_MAP_REL


def selection_history(
    repo_root: Path, alpha_id: str, map_path: Path | None = None
) -> dict[str, Any]:
    """The selection-history fields of ``alpha_id``'s family from the committed ledger
    family dependency map (v1.19 R3, defect D-08): the real-data looks the central
    ledger records, the real looks external ledgers hold unmerged, their sum as the
    lower bound on the family's search breadth, the families dependent on it through
    looks on the same real dataset version and their looks, and the map's sha256.  A
    family the map does not know (a new hypothesis) returns ``known: False`` and zero
    counts.  :class:`PreregistrationError` when the map is missing or unreadable: the
    gate never guesses a selection history."""
    p = Path(map_path) if map_path else family_map_path(repo_root)
    try:
        raw = p.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        fams = doc["families"]
    except (OSError, ValueError, KeyError) as exc:
        raise PreregistrationError(f"{p}: ledger family map missing or unreadable ({exc})") from exc
    fam = fams.get(alpha_id)
    sha = hashlib.sha256(raw).hexdigest()
    if fam is None:
        return {
            "family_id": alpha_id,
            "known": False,
            "central_real_looks": 0,
            "central_synthetic_looks": 0,
            "external_unmerged_looks": 0,
            "real_looks_lower_bound": 0,
            "dependent_families": [],
            "dependent_real_looks_lower_bound": 0,
            "map_sha256": sha,
        }
    dependent = list(fam.get("dependent_families", []))
    return {
        "family_id": alpha_id,
        "known": True,
        "central_real_looks": int(fam["central"]["real"]["looks"]),
        "central_synthetic_looks": int(fam["central"]["synthetic"]["looks"]),
        "external_unmerged_looks": int(fam["external_unmerged"]["looks"]),
        "real_looks_lower_bound": int(fam["real_looks_lower_bound"]),
        "dependent_families": dependent,
        "dependent_real_looks_lower_bound": sum(
            int(fams[b]["real_looks_lower_bound"]) for b in dependent if b in fams
        ),
        "map_sha256": sha,
    }


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
    identity_actual: dict[str, Any] | None = None,
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
    bound = found["body"].get("analysis_identity")
    if bound is not None:
        # v1.18 REL-02a: a prereg that binds the analysis identity refuses a
        # run whose scripts, code tree, commit, data, family, metrics or
        # thresholds differ; a run that declares nothing is refused too.
        from iap.agents.analysis_identity import IdentityError, verify_identity

        if identity_actual is None:
            raise PreregistrationError(
                f"{alpha_id}/{horizon}: the pre-registration binds an analysis identity "
                "(entry {}); the runner must declare its data versions, family, metrics "
                "and thresholds".format(found["seq"])
            )
        try:
            verify_identity(
                repo_root,
                bound,
                data_versions=identity_actual.get("data_versions", []),
                family=identity_actual.get("family", ""),
                metrics=identity_actual.get("metrics", []),
                thresholds=identity_actual.get("thresholds", {}),
            )
        except IdentityError as exc:
            raise PreregistrationError(f"{alpha_id}/{horizon}: {exc}") from exc
    # v1.19 R3 (defect D-08): the selection history of the family is attached from the
    # ledger family dependency map when the repository carries one; a prereg that binds
    # the map by hash (``ledger_family_map_sha256``) is refused when the committed map
    # differs (the denominator it was registered against has changed).
    bound_map = found["body"].get("ledger_family_map_sha256")
    if bound_map is not None or family_map_path(repo_root).is_file():
        history = selection_history(repo_root, alpha_id)
        if bound_map is not None and bound_map != history["map_sha256"]:
            raise PreregistrationError(
                f"{alpha_id}/{horizon}: the pre-registration binds ledger family map "
                f"{str(bound_map)[:12]} but the committed map is {history['map_sha256'][:12]}; "
                "the selection history it was registered against has changed"
            )
        found = {**found, "selection_history": history}
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
