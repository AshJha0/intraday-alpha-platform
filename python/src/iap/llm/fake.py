"""A scripted stand-in for ``anthropic.Anthropic`` (tests, CI, mocked evals).

``ScriptedClient(steps)`` answers ``client.messages.create(**kw)`` with the
next step.  A step is a list of blocks, or a callable ``(results) -> blocks``
that sees the parsed tool results of the previous turn (so a script can copy
a number from a report, as a well-behaved model must).  Blocks are built
with :func:`tool` and :func:`text`.  Usage is synthetic but realistic enough
for the budget arithmetic; no network, no key, no spend.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

Step = list[Any] | Callable[[list[Any]], list[Any]]


@dataclass
class Usage:
    input_tokens: int = 1200
    output_tokens: int = 150
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Response:
    content: list[Block]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)


_counter = [0]


def tool(name: str, **inp: Any) -> Block:
    _counter[0] += 1
    return Block(type="tool_use", id=f"toolu_{_counter[0]:06d}", name=name, input=inp)


def text(s: str) -> Block:
    return Block(type="text", text=s)


def last_results(messages: Sequence[dict[str, Any]]) -> list[Any]:
    """The previous turn's tool results, JSON-decoded where possible."""
    if not messages or messages[-1]["role"] != "user" or isinstance(messages[-1]["content"], str):
        return []
    out = []
    for r in messages[-1]["content"]:
        try:
            out.append(json.loads(r["content"]))
        except (TypeError, ValueError):
            out.append(r["content"])
    return out


class _Messages:
    def __init__(self, owner: ScriptedClient) -> None:
        self.owner = owner

    def create(self, **kw: Any) -> Response:
        o = self.owner
        o.requests.append(kw)
        if o.i >= len(o.steps):
            return Response([text("done")], "end_turn", o._usage())
        step = o.steps[o.i]
        o.i += 1
        blocks = step(last_results(kw["messages"])) if callable(step) else list(step)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return Response(blocks, stop, o._usage())


class ScriptedClient:
    def __init__(self, steps: Sequence[Step], usage: Usage | None = None) -> None:
        self.steps = list(steps)
        self.i = 0
        self.requests: list[dict[str, Any]] = []
        self.usage = usage or Usage()
        self.messages = _Messages(self)

    def _usage(self) -> Usage:
        # first call writes the cached system prompt, later calls read it
        cached = 1500
        if len(self.requests) == 1:
            return Usage(self.usage.input_tokens, self.usage.output_tokens, 0, cached)
        return Usage(self.usage.input_tokens, self.usage.output_tokens, cached, 0)
