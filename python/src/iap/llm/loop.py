"""One generic tool loop for the v1.17 WS-E agents (research assistant, ops,
docs-sync): the model calls tools from a fixed allowlist, every call is
budgeted and ledgered, every request and tool call is persisted, and the
caller's ``dispatch`` holds the whole governance (what a tool may do).

:func:`iap.llm.agent.run_session` is the v1.11 research agent's loop and
stays as it is (the agent-evals baseline pins it); this module is the same
contract for the agents that do not sit on the blackboard: the same
:class:`~iap.llm.budget.Budget` (monthly cap on the shared ledger, per-run
USD, tokens, tool calls), the same scripted client for tests
(:mod:`iap.llm.fake`), the same key scrubbing.  No tool here ever edits a
tracked file, signs, registers or applies anything: the agents write drafts,
alerts documents and patch files that a human reads.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iap.llm.budget import Budget, BudgetExceeded
from iap.llm.envfile import redact
from iap.llm.prices import load_llm_config

__all__ = ["LoopResult", "ToolRefused", "ToolSpec", "run_tool_loop", "schema", "wire_spend"]

Dispatch = Callable[[str, Mapping[str, Any]], dict[str, Any]]
_S = {"type": "string"}


class ToolRefused(ValueError):
    """A tool call the governance refuses (the model is told why)."""


def schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def as_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema(dict(self.properties), list(self.required)),
        }


def check_args(spec: ToolSpec, args: Mapping[str, Any]) -> None:
    """Refuse unknown or missing arguments before the dispatcher sees them."""
    if not isinstance(args, Mapping):
        raise ToolRefused("arguments must be an object")
    extra = set(args) - set(spec.properties)
    missing = [k for k in spec.required if k not in args]
    if extra or missing:
        raise ToolRefused(
            f"bad arguments for {spec.name}: extra {sorted(extra)}, missing {missing}"
        )


@dataclass
class LoopResult:
    status: str
    model: str
    pin: dict[str, str] | None
    served_models: list[str]
    budget: dict[str, Any]
    tool_log: list[dict[str, Any]]
    closing_text: str
    finished: str | None
    out_dir: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "iap.llm.loop/1",
            "status": self.status,
            "model": self.model,
            "pin": self.pin,
            "served_models": self.served_models,
            "budget": self.budget,
            "tool_calls": [
                {k: v for k, v in r.items() if k != "output"} | {"ok": r["ok"]}
                for r in self.tool_log
            ],
            "closing_text": self.closing_text,
            "finished": self.finished,
            "out_dir": str(self.out_dir),
        }


def _is_live_client(client: Any) -> bool:
    return type(client).__module__.split(".")[0] == "anthropic"


def wire_spend(client: Any, budget: Budget, model: str, session_id: str) -> None:
    """E1: a live client always records to the operator ledger; the budget
    carries the dated pin the model runs under."""
    if budget.ledger is None and _is_live_client(client):
        from iap.llm.spend import SpendLedger

        budget.ledger = SpendLedger.default()
    if not budget.session_id:
        budget.session_id = session_id
    if budget.pin is None:
        cfg = load_llm_config()
        pins = [p for p in cfg.pins.values() if p.model == model]
        pref = [p for p in pins if p.role == budget.role]
        chosen = pref or pins
        budget.pin = chosen[0] if chosen else None


def _dump(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block


class _Log:
    def __init__(self, out_dir: Path, secret: str | None) -> None:
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secret = secret

    def append(self, name: str, rec: Mapping[str, Any]) -> None:
        line = redact(json.dumps(rec, sort_keys=True, default=str), self.secret)
        with (self.dir / name).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def write(self, name: str, doc: Mapping[str, Any]) -> None:
        text = redact(json.dumps(doc, indent=2, sort_keys=True, default=str), self.secret)
        (self.dir / name).write_text(text + "\n", encoding="utf-8")


def run_tool_loop(
    client: Any,
    *,
    system: str,
    tools: list[ToolSpec],
    dispatch: Dispatch,
    task: str,
    budget: Budget,
    model: str,
    out_dir: Path,
    session_id: str,
    finish_tool: str = "finish",
    effort: str | None = "medium",
    max_turns: int = 40,
    secret: str | None = None,
    clock: Callable[[], float] = time.time,
) -> LoopResult:
    """Drive ``client`` until it calls ``finish_tool``, stops calling tools or
    a cap is hit.  ``dispatch(name, args)`` runs a tool (raise
    :class:`ToolRefused` to refuse); the loop checks the allowlist and the
    argument schema first.  Transcript, tool log and summary go to
    ``out_dir`` with ``secret`` scrubbed."""
    wire_spend(client, budget, model, session_id)
    log = _Log(Path(out_dir), secret)
    by_name = {t.name: t for t in tools}
    api_tools = [t.as_api() for t in tools]
    sys_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    log.append("transcript.jsonl", {"t": clock(), "role": "user", "content": task})
    status = "max_turns"
    served: set[str] = set()
    tool_log: list[dict[str, Any]] = []
    closing_text = ""
    finished: str | None = None
    for turn in range(max_turns):
        try:
            budget.check_model_call(model)
        except BudgetExceeded as exc:
            budget.refusals.append(str(exc))
            status = "budget_exhausted"
            break
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": budget.max_tokens_per_call,
            "system": sys_blocks,
            "tools": api_tools,
            "messages": messages,
        }
        if effort:
            kwargs["output_config"] = {"effort": effort}
        resp = client.messages.create(**kwargs)
        if getattr(resp, "model", None):
            served.add(str(resp.model))
        budget.charge(model, resp.usage)
        content = list(resp.content)
        texts = [
            str(getattr(b, "text", "") or "") for b in content if getattr(b, "type", None) == "text"
        ]
        if any(texts):
            closing_text = " ".join(t for t in texts if t)
        log.append(
            "transcript.jsonl",
            {
                "t": clock(),
                "turn": turn,
                "role": "assistant",
                "stop_reason": resp.stop_reason,
                "usage": _dump(resp.usage),
                "content": [_dump(b) for b in content],
            },
        )
        messages.append({"role": "assistant", "content": content})
        if resp.stop_reason == "refusal":
            status = "model_refusal"
            break
        uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        if not uses:
            status = "end_turn" if resp.stop_reason != "max_tokens" else "max_tokens"
            break
        results = []
        exhausted = False
        for b in uses:
            rec: dict[str, Any] = {"t": clock(), "turn": turn, "tool": b.name, "input": b.input}
            try:
                budget.check_tool_call()
                spec = by_name.get(str(b.name))
                if spec is None:
                    raise ToolRefused(f"unknown tool {b.name!r}; allowed: {sorted(by_name)}")
                check_args(spec, b.input)
                out = dispatch(spec.name, b.input)
                rec["ok"] = True
                rec["output"] = out
                results.append(
                    {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)}
                )
                if spec.name == finish_tool:
                    finished = str(out.get("summary", "")) if isinstance(out, dict) else ""
            except (ToolRefused, BudgetExceeded) as exc:
                if isinstance(exc, BudgetExceeded):
                    budget.refusals.append(str(exc))
                    exhausted = exhausted or "tool-call cap" in str(exc)
                rec["ok"] = False
                rec["error"] = str(exc)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": f"refused: {exc}",
                        "is_error": True,
                    }
                )
            log.append("tool_log.jsonl", rec)
            tool_log.append(rec)
        messages.append({"role": "user", "content": results})
        log.append("transcript.jsonl", {"t": clock(), "role": "user", "content": results})
        if finished is not None:
            status = "finished"
            break
        if exhausted:
            status = "budget_exhausted"
            break
    result = LoopResult(
        status=status,
        model=model,
        pin=budget.pin.as_dict() if budget.pin else None,
        served_models=sorted(served),
        budget=budget.summary(),
        tool_log=tool_log,
        closing_text=closing_text,
        finished=finished,
        out_dir=Path(out_dir),
    )
    log.write("session.json", result.as_dict())
    return result
