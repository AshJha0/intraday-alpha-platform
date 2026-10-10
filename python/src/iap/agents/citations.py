"""Citation resolution: every claim an agent files must point at something real.

Reference forms: ``experiment:<id>`` (a directory under ``research/experiments``
holding ``spec.json``), ``alpha:<id>`` (a report ``research/alpha_reports/<id>.json``
or an entry in ``research/alpha_registry.json``), ``lifecycle:<n>`` (1-based line
of ``research/lifecycle_transitions.jsonl``), ``ledger:<key>`` (an entry key in
``research/experiments.json``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REF = re.compile(r"^(experiment|alpha|lifecycle|ledger|report|board):([A-Za-z0-9_.-]{1,128})$")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def resolve(ref: str, root: Path) -> bool:
    """True iff ``ref`` is well formed and names an artefact under ``root``."""
    m = _REF.match(ref) if isinstance(ref, str) else None
    if not m:
        return False
    kind, ident = m.groups()
    research = Path(root) / "research"
    if kind in ("report", "board"):
        return artefact(ref, root) is not None
    try:
        if kind == "experiment":
            return (research / "experiments" / ident / "spec.json").is_file()
        if kind == "alpha":
            if (research / "alpha_reports" / f"{ident}.json").is_file():
                return True
            path = research / "alpha_registry.json"
            if not path.is_file():
                return False
            doc = _json(path)
            pool = doc.get("alphas", doc) if isinstance(doc, dict) else doc
            if isinstance(pool, dict):
                return ident in pool
            return any(isinstance(a, dict) and a.get("alpha_id") == ident for a in pool)
        if kind == "lifecycle":
            path = research / "lifecycle_transitions.jsonl"
            if not (ident.isdigit() and path.is_file()):
                return False
            with path.open(encoding="utf-8") as fh:
                return 1 <= int(ident) <= sum(1 for line in fh if line.strip())
        path = research / "experiments.json"
        if not path.is_file():
            return False
        return any(e.get("key") == ident for e in _json(path).get("entries", []))
    except (OSError, ValueError, AttributeError):
        return False


def unresolved(refs, root: Path) -> list[str]:
    """The references in ``refs`` that do not resolve."""
    return [r for r in refs if not resolve(r, root)]


_HEX = re.compile(r"^[0-9a-f]{16,64}$")


def report_path(root: Path, ident: str) -> Path | None:
    """``<session>.<run>`` -> the report file, or None if malformed."""
    parts = ident.split(".")
    if len(parts) != 2 or any(not p or p.startswith("-") for p in parts):
        return None
    session, run = parts
    return Path(root) / "research" / "agents" / "llm_sessions" / session / "reports" / f"{run}.json"


def artefact(ref: str, root: Path):
    """The JSON a ``report:`` or ``board:`` reference names (a board entry as a
    whole dict), or None if it does not resolve."""
    m = _REF.match(ref) if isinstance(ref, str) else None
    if not m:
        return None
    kind, ident = m.groups()
    try:
        if kind == "report":
            path = report_path(root, ident)
            return _json(path) if path is not None and path.is_file() else None
        if kind == "board":
            if not _HEX.match(ident):
                return None
            from iap.agents.blackboard import Blackboard

            board = Blackboard(Path(root) / "research" / "agents" / "blackboard.jsonl")
            board.verify()
            hits = [e for e in board.entries() if str(e.get("hash", "")).startswith(ident)]
            return hits[0] if len(hits) == 1 else None
    except (OSError, ValueError, AttributeError):
        return None
    return None
