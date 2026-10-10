"""External anchoring of the blackboard in git history (G3, v1.10).

The hash chain proves internal consistency only: anyone with write access
can edit an entry and recompute every later hash (re-chaining).  Git history
is the external witness.  :func:`anchors` maps every entry hash to the first
commit whose copy of the board contains it (what was done by hand for the
2026 pre-registrations); :func:`verify` checks that

1. the chain is intact,
2. every committed version of the board is a PREFIX of the current one -
   an entry, once committed, is never edited, removed or reordered, and
3. optionally, a stored anchor file (``research/agents/anchors.json``) still
   matches: each anchored hash is present at its seq and its commit still
   contains it.

A re-chained board fails 2 (the committed lines differ) and 3.  Entries not
yet committed are reported as ``unanchored``, not as failures.  Uses the
``git`` executable on PATH (standard library ``subprocess`` only).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from iap.agents.blackboard import Blackboard, BlackboardError

ANCHORS_RELPATH = Path("research") / "agents" / "anchors.json"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise BlackboardError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def _versions(repo: Path, rel: str) -> list[tuple[str, list[str]]]:
    """(commit, board lines) for every commit touching the board, oldest first."""
    commits = _git(repo, "log", "--format=%H", "--reverse", "--", rel).split()
    out = []
    for c in commits:
        try:
            text = _git(repo, "show", f"{c}:{rel}")
        except BlackboardError:
            continue  # the file was deleted in that commit
        out.append((c, [ln for ln in text.splitlines() if ln.strip()]))
    return out


def _rel(repo: Path, board: Path) -> str:
    return Path(board).resolve().relative_to(Path(repo).resolve()).as_posix()


def anchors(repo: Path, board: Path) -> dict[str, Any]:
    """``{"board", "entries": [{"seq", "hash", "commit"}]}`` for committed entries."""
    rel = _rel(repo, board)
    first: dict[str, str] = {}
    for commit, lines in _versions(repo, rel):
        for ln in lines:
            h = json.loads(ln).get("hash")
            first.setdefault(h, commit)
    rows = [
        {"seq": e["seq"], "hash": e["hash"], "commit": first[e["hash"]]}
        for e in Blackboard(board).entries()
        if e.get("hash") in first
    ]
    return {"board": rel, "entries": rows}


def verify(repo: Path, board: Path, anchor_file: Path | None = None) -> dict[str, Any]:
    """``{"ok", "entries", "anchored", "unanchored", "problems"}``."""
    repo, board = Path(repo), Path(board)
    problems: list[str] = []
    bb = Blackboard(board)
    try:
        n = bb.verify()
    except BlackboardError as exc:
        return {"ok": False, "entries": 0, "anchored": 0, "unanchored": 0, "problems": [str(exc)]}
    current = [ln for ln in board.read_text(encoding="ascii").splitlines() if ln.strip()]
    rel = _rel(repo, board)
    committed = 0
    for commit, lines in _versions(repo, rel):
        if current[: len(lines)] != lines:
            problems.append(
                f"commit {commit[:12]}: committed entries differ from the current board "
                "(edited, removed or re-chained)"
            )
        committed = max(committed, len(lines))
    if anchor_file is not None and Path(anchor_file).is_file():
        by_seq = {e["seq"]: e for e in bb.entries()}
        for row in json.loads(Path(anchor_file).read_text(encoding="ascii"))["entries"]:
            e = by_seq.get(row["seq"])
            if e is None or e["hash"] != row["hash"]:
                problems.append(f"anchor seq {row['seq']}: hash no longer on the board")
                continue
            try:
                text = _git(repo, "show", f"{row['commit']}:{rel}")
            except BlackboardError:
                problems.append(f"anchor seq {row['seq']}: commit {row['commit'][:12]} missing")
                continue
            if row["hash"] not in text:
                problems.append(f"anchor seq {row['seq']}: commit does not contain the entry")
    return {
        "ok": not problems,
        "entries": n,
        "anchored": min(committed, n),
        "unanchored": max(n - committed, 0),
        "problems": problems,
    }
