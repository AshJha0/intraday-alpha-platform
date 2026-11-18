"""The research record as a list of citable chunks.

Two sources, read from the working tree and nothing else:

* Markdown: the top-level ``*.md`` files and every ``*.md`` under ``docs/``
  and ``research/``, split at ATX headings (``#`` .. ``######``) outside
  fenced code blocks.  Each chunk keeps the file path, the heading path
  (``Top > Section > Subsection``) and the first and last line it covers.
* The experiment ledger ``research/experiments.json``: one chunk per entry,
  its ``alpha_id``, ``kind``, ``config`` and ``result`` rendered as text and
  cited as ``research/experiments.json#<key>`` at the line of its ``key``.

Any path with an ``archive`` component is excluded.  Order is deterministic
(sorted POSIX paths, then line order, then ledger order); no clock is read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEDGER = "research/experiments.json"
MARKDOWN_DIRS = ("docs", "research")
EXCLUDED_PART = "archive"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    """One citable unit of the record."""

    path: str  # repo-relative POSIX path; ledger chunks carry ``#<key>``
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive
    heading: str  # heading path, or ``<alpha_id> <kind>`` for a ledger entry
    kind: str  # "doc" (docs/ and top level), "research" (research/), "ledger"
    text: str  # what is indexed and quoted
    alpha_id: str = ""
    horizon: str = ""


def _kind_for(rel: str) -> str:
    return "research" if rel.startswith("research/") else "doc"


def markdown_files(root: Path) -> list[Path]:
    """The Markdown files of the corpus, sorted by repo-relative path."""
    files = [p for p in root.glob("*.md") if p.is_file()]
    for d in MARKDOWN_DIRS:
        base = root / d
        if base.is_dir():
            files.extend(p for p in base.rglob("*.md") if p.is_file())
    keep = [p for p in files if EXCLUDED_PART not in p.relative_to(root).parts]
    return sorted(keep, key=lambda p: p.relative_to(root).as_posix())


def chunk_markdown(text: str, rel: str) -> list[Chunk]:
    """Split one Markdown document at its headings.

    Text before the first heading becomes a chunk with an empty heading.
    Sections whose body is blank (a heading directly followed by a
    sub-heading) are still kept, so every heading is findable by name.
    """
    lines = text.splitlines()
    kind = _kind_for(rel)
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    start = 1
    heading = ""
    in_fence = False

    def close(end: int) -> None:
        body = "\n".join(lines[start - 1 : end])
        if body.strip():
            chunks.append(Chunk(rel, start, end, heading, kind, body))

    for i, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        close(i - 1)
        level = len(m.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, m.group(2).strip()))
        heading = " > ".join(t for _, t in stack)
        start = i
    close(len(lines))
    return chunks


def _flatten(value: Any, prefix: str, out: list[str]) -> None:
    if isinstance(value, dict):
        for k in sorted(value):
            _flatten(value[k], f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            out.append(f"{prefix} = {', '.join(str(v) for v in value)}")
        else:
            for j, v in enumerate(value):
                _flatten(v, f"{prefix}[{j}]", out)
    else:
        out.append(f"{prefix} = {value}")


def render_entry(entry: dict[str, Any]) -> str:
    """A ledger entry as indexable text (config and result flattened)."""
    head = [
        f"alpha_id {entry.get('alpha_id', '')}",
        f"kind {entry.get('kind', '')}",
        f"data_family {entry.get('data_family', '')}",
        f"count {entry.get('count', '')} n {entry.get('n', '')}",
    ]
    body: list[str] = []
    _flatten(entry.get("config", {}), "config", body)
    _flatten(entry.get("result", {}), "result", body)
    if "gate_looks" in entry:
        _flatten(entry["gate_looks"], "gate_looks", body)
    return "\n".join(head + body)


def _horizon(entry: dict[str, Any]) -> str:
    cfg = entry.get("config", {})
    if isinstance(cfg, dict):
        h = cfg.get("horizon")
        if h is None and isinstance(cfg.get("candidate"), dict):
            h = cfg["candidate"].get("horizon")
        if h is not None:
            return str(h)
    return ""


def ledger_chunks(root: Path) -> list[Chunk]:
    """One chunk per entry of the experiment ledger, in ledger order."""
    path = root / LEDGER
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8")
    key_line: dict[str, int] = {}
    pat = re.compile(r'^\s*"key":\s*"([0-9a-f]+)"')
    for i, line in enumerate(raw.splitlines(), start=1):
        m = pat.match(line)
        if m:
            key_line.setdefault(m.group(1), i)
    out = []
    for entry in json.loads(raw).get("entries", []):
        key = str(entry.get("key", ""))
        line = key_line.get(key, 1)
        alpha = str(entry.get("alpha_id", ""))
        out.append(
            Chunk(
                path=f"{LEDGER}#{key}",
                line_start=line,
                line_end=line,
                heading=f"{alpha} {entry.get('kind', '')}",
                kind="ledger",
                text=render_entry(entry),
                alpha_id=alpha,
                horizon=_horizon(entry),
            )
        )
    return out


def build_corpus(root: Path) -> list[Chunk]:
    """Every chunk of the record under ``root``, in deterministic order."""
    chunks: list[Chunk] = []
    for p in markdown_files(root):
        rel = p.relative_to(root).as_posix()
        chunks.extend(chunk_markdown(p.read_text(encoding="utf-8", errors="replace"), rel))
    chunks.extend(ledger_chunks(root))
    return chunks
