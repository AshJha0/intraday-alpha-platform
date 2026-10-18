"""The only role AI has in monitoring: explaining a state the code produced.

:func:`explanation_facts` is the closed set of facts an explainer (human or
LLM, outside this package) may draw on.  :func:`check_explanation` rejects
an explanation that names a state other than the computed one or omits it;
a rejected explanation is discarded and the report stands alone.  Nothing
here, or anywhere in the machine, reads text to choose a state.
"""

from __future__ import annotations

import re
from typing import Any

from .machine import Evaluation, State

__all__ = ["check_explanation", "explanation_facts"]


def explanation_facts(ev: Evaluation) -> dict[str, Any]:
    return {
        "state": ev.state.value,
        "required_action": ev.action,
        "policy_id": ev.policy_id,
        "config_sha256": ev.config_sha256,
        "alerts": [a.to_dict() for a in ev.alerts],
        "instruction": "Explain this state. Do not propose or name a different state.",
    }


def check_explanation(text: str, ev: Evaluation) -> list[str]:
    """Problems with ``text`` as an explanation of ``ev`` (empty = acceptable)."""
    named = {s.value for s in State if re.search(rf"\b{s.value}\b", text)}
    out = []
    if ev.state.value not in named:
        out.append(f"explanation does not name the computed state {ev.state.value}")
    other = sorted(named - {ev.state.value})
    if other:
        out.append(f"explanation names other states {other}")
    return out
