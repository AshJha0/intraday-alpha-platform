"""Handling of untrusted free text (AL07).

Everything an agent reads that a person or another agent could have written
(ledger descriptions, report prose, finding text, tool output) is wrapped as
data: control characters are removed, length is capped, and likely
instruction-injection phrases are flagged.  Flagging is advisory; the real
control is that no tool the agent holds can change state except through the
broker, so an injected instruction has nothing to call.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT = 4000

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATTERNS = (
    re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(the\s+)?(system|previous|above)", re.I),
    re.compile(r"you\s+are\s+now\b", re.I),
    re.compile(r"(^|\n)\s*(system|assistant)\s*:", re.I),
    re.compile(r"</?\s*(system|tool|instructions?)\s*>", re.I),
    re.compile(r"\b(approve|promote|override|disable)\b.{0,40}\b(risk|gate|lifecycle|kill)", re.I),
)


def sanitise(text: str, limit: int = MAX_TEXT) -> str:
    """Strip control characters (keeping tab, newline) and cap the length."""
    clean = _CONTROL.sub("", str(text))
    if len(clean) > limit:
        clean = clean[:limit] + "...[truncated]"
    return clean


def injection_flags(text: str) -> list[str]:
    """Patterns ``text`` matches (empty if none)."""
    return [p.pattern for p in _PATTERNS if p.search(str(text))]


def wrap(text: str, source: str) -> dict[str, Any]:
    """``text`` as an inert, labelled value an agent must treat as data."""
    return {
        "untrusted": True,
        "source": source,
        "text": sanitise(text),
        "flags": injection_flags(text),
    }


def wrap_tree(value: Any, source: str, keys: frozenset[str] = frozenset()) -> Any:
    """Wrap every string stored under a key in ``keys`` (the free-text fields)."""
    if isinstance(value, dict):
        return {
            k: (
                wrap(v, f"{source}.{k}")
                if k in keys and isinstance(v, str)
                else wrap_tree(v, f"{source}.{k}", keys)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [wrap_tree(v, source, keys) for v in value]
    return value
