"""``explain`` for traces at rest: the contract renderer re-exported, plus
lookups over a trace JSONL file (the sink's output) by parent order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional, Union

from iap.contracts.types import DecisionTrace, explain

__all__ = ["explain", "explain_jsonl", "find_trace_jsonl"]


def find_trace_jsonl(path: Union[str, Path], parent_order_id: int) -> DecisionTrace:
    """The first trace in the JSONL file whose ``stages.parent_orders``
    carries ``parent_order_id``; ``KeyError`` when none does.  Lines are
    parsed through ``DecisionTrace.from_dict`` (a malformed line raises
    ``ValueError`` naming the line)."""
    with open(path, "r", encoding="ascii") as fh:
        for lineno, raw in enumerate(fh, 1):
            text = raw.strip()
            if not text:
                continue
            try:
                trace = DecisionTrace.from_dict(json.loads(text))
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: not a DecisionTrace ({exc})") from exc
            if any(po.parent_order_id == parent_order_id
                   for po in trace.stages.parent_orders):
                return trace
    raise KeyError(f"{path}: no trace carries parent order {parent_order_id}")


def explain_jsonl(path: Union[str, Path], parent_order_id: int,
                  venue_names: Optional[Mapping[int, str]] = None) -> str:
    """:func:`explain` of the trace in ``path`` that carries the order."""
    return explain(find_trace_jsonl(path, parent_order_id), venue_names)
