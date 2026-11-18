"""In-house tracing for the agent loops and the research graph.

Spans follow the field layout of the OpenTelemetry GenAI semantic
conventions (see ``docs/TRACING.md`` for the attribute table and which names
were checked against the specification).  No dependency: the tracer, the
JSONL exporter and the reader are plain Python.  :func:`to_otel` converts
finished spans to OpenTelemetry SDK spans only when ``opentelemetry`` is
importable; it is imported inside that function, never at module load.

Rules:

* a span records sizes, counts, names, statuses and hashes, never prompt,
  completion, tool-argument or tool-result text;
* tracing never changes what the loop does: every tracer method catches its
  own failures, counts them in :attr:`Tracer.failures` and returns;
* clock and id source are injected, so tests get byte-identical span files.

Reader: ``python -m iap.llm.tracing summarize <session_dir>``.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iap.llm.envfile import redact

__all__ = [
    "SPAN_FILE",
    "JsonlExporter",
    "Span",
    "Tracer",
    "counter_ids",
    "load_spans",
    "session_tracer",
    "summarize",
    "to_otel",
]

SPAN_FILE = "spans.jsonl"
PROVIDER = "anthropic"
SCHEMA = "iap.llm.tracing/1"

Clock = Callable[[], int]
IdSource = Callable[[int], str]


def _random_ids(nbytes: int) -> str:
    return os.urandom(nbytes).hex()


def counter_ids(start: int = 1) -> IdSource:
    """A deterministic id source for tests: 1, 2, 3 ... zero-padded to width."""
    n = [start - 1]

    def ids(nbytes: int) -> str:
        n[0] += 1
        return f"{n[0]:0{2 * nbytes}x}"

    return ids


def _default_clock() -> int:
    import time

    return time.time_ns()


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str  # "INTERNAL" or "CLIENT"
    start_ns: int
    end_ns: int | None = None
    status: str = "UNSET"  # "UNSET", "OK" or "ERROR"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0  # start order within one tracer

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "status": self.status,
            "attributes": dict(self.attributes),
            "events": list(self.events),
            "seq": self.seq,
        }


class JsonlExporter:
    """Appends one finished span per line to ``path``, with ``secret`` scrubbed."""

    def __init__(self, path: Path | str, secret: str | None = None) -> None:
        self.path = Path(path)
        self.secret = secret

    def export(self, span: Span) -> None:
        line = redact(json.dumps(span.to_json(), sort_keys=True, default=str), self.secret)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class Tracer:
    """A tiny tracer.  ``start`` opens a span under the innermost open span
    (or under ``parent``), ``end`` closes and exports it.  No method raises:
    a failure increments :attr:`failures` and the call returns ``None``."""

    def __init__(
        self,
        exporter: Any = None,
        *,
        clock: Clock = _default_clock,
        ids: IdSource = _random_ids,
    ) -> None:
        self.exporter = exporter
        self.clock = clock
        self.ids = ids
        self.failures = 0
        self.finished: list[Span] = []
        self._open: list[Span] = []
        self._seq = 0

    # -- core ------------------------------------------------------------------
    def start(
        self,
        name: str,
        kind: str = "INTERNAL",
        attributes: Mapping[str, Any] | None = None,
        parent: Span | None = None,
    ) -> Span | None:
        try:
            par = parent if parent is not None else (self._open[-1] if self._open else None)
            span = Span(
                trace_id=par.trace_id if par is not None else self.ids(16),
                span_id=self.ids(8),
                parent_id=par.span_id if par is not None else None,
                name=name,
                kind=kind,
                start_ns=int(self.clock()),
                attributes=dict(attributes or {}),
                seq=self._seq + 1,
            )
            self._seq += 1
            self._open.append(span)
            return span
        except Exception:
            self.failures += 1
            return None

    def end(
        self,
        span: Span | None,
        *,
        error: BaseException | str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if span is None:
            return
        try:
            if span in self._open:
                self._open.remove(span)
            if attributes:
                span.attributes.update(attributes)
            if error is not None:
                span.status = "ERROR"
                span.attributes.setdefault(
                    "error.type", error if isinstance(error, str) else type(error).__name__
                )
            elif span.status == "UNSET":
                span.status = "OK"
            span.end_ns = int(self.clock())
            self.finished.append(span)
        except Exception:
            self.failures += 1
            return
        try:
            if self.exporter is not None:
                self.exporter.export(span)
        except Exception:
            self.failures += 1

    def event(
        self, span: Span | None, name: str, attributes: Mapping[str, Any] | None = None
    ) -> None:
        if span is None:
            return
        try:
            span.events.append(
                {"name": name, "time_ns": int(self.clock()), "attributes": dict(attributes or {})}
            )
        except Exception:
            self.failures += 1

    @contextmanager
    def span(
        self, name: str, kind: str = "INTERNAL", attributes: Mapping[str, Any] | None = None
    ) -> Iterator[Span | None]:
        """Context-manager form; an exception in the body marks the span
        ``ERROR`` and is re-raised unchanged."""
        s = self.start(name, kind, attributes)
        try:
            yield s
        except BaseException as exc:
            self.end(s, error=exc)
            raise
        self.end(s)

    # -- agent-loop helpers (all failure-safe) ---------------------------------
    def session_start(self, agent: str, session_id: str, model: str) -> Span | None:
        return self.start(
            f"invoke_agent {agent}",
            "INTERNAL",
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": PROVIDER,
                "gen_ai.agent.name": agent,
                "gen_ai.conversation.id": session_id,
                "gen_ai.request.model": model,
            },
        )

    def session_end(self, span: Span | None, status: str, budget: Any) -> None:
        try:
            attrs = {
                "iap.session.status": status,
                "iap.cost.usd": round(float(getattr(budget, "usd", 0.0)), 6),
                "iap.budget.refusals": len(getattr(budget, "refusals", []) or []),
                "iap.tracing.failures": self.failures,
            }
        except Exception:
            self.failures += 1
            attrs = {}
        self.end(span, error="client_error" if status == "client_error" else None, attributes=attrs)

    def budget_refusal(self, span: Span | None, reason: str) -> None:
        self.event(span, "budget_refusal", {"iap.budget.reason": str(reason)})

    def model_start(self, model: str, turn: int, session_id: str) -> Span | None:
        return self.start(
            f"chat {model}",
            "CLIENT",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": PROVIDER,
                "gen_ai.request.model": model,
                "gen_ai.conversation.id": session_id,
                "iap.turn": turn,
            },
        )

    def model_end(self, span: Span | None, resp: Any, cost_usd: float) -> None:
        try:
            usage = getattr(resp, "usage", None)

            def n(name: str) -> int | None:
                v = getattr(usage, name, None)
                return int(v) if isinstance(v, int) else None

            uncached, out = n("input_tokens"), n("output_tokens")
            c_read, c_write = n("cache_read_input_tokens"), n("cache_creation_input_tokens")
            attrs: dict[str, Any] = {"iap.cost.usd": round(float(cost_usd), 8)}
            if uncached is not None:
                # the convention counts cached tokens inside input_tokens; the
                # Anthropic usage object reports them apart
                attrs["gen_ai.usage.input_tokens"] = uncached + (c_read or 0) + (c_write or 0)
                attrs["iap.usage.uncached_input_tokens"] = uncached
            if out is not None:
                attrs["gen_ai.usage.output_tokens"] = out
            if c_read is not None:
                attrs["gen_ai.usage.cache_read.input_tokens"] = c_read
            if c_write is not None:
                attrs["gen_ai.usage.cache_write.input_tokens"] = c_write
            served = getattr(resp, "model", None)
            if served:
                attrs["gen_ai.response.model"] = str(served)
            rid = getattr(resp, "id", None)
            if isinstance(rid, str):
                attrs["gen_ai.response.id"] = rid
            stop = getattr(resp, "stop_reason", None)
            if stop is not None:
                attrs["gen_ai.response.finish_reasons"] = [str(stop)]
        except Exception:
            self.failures += 1
            attrs = {}
        self.end(span, attributes=attrs)

    def tool_start(self, block: Any, turn: int) -> Span | None:
        try:
            name = str(getattr(block, "name", ""))
            args = _nbytes(getattr(block, "input", None))
            attrs = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.type": "function",
                "gen_ai.tool.call.id": str(getattr(block, "id", "")),
                "iap.turn": turn,
                "iap.tool.args_bytes": args,
            }
        except Exception:
            self.failures += 1
            return None
        return self.start(f"execute_tool {name}", "INTERNAL", attrs)

    def tool_end(self, span: Span | None, rec: Mapping[str, Any], refusals: Sequence[str]) -> None:
        """Close a tool span from the loop's tool-log record (``ok``,
        ``output`` or ``error``); ``refusals`` is the budget's refusal list."""
        if span is None:
            return
        err: str | None = None
        try:
            if rec.get("ok"):
                attrs: dict[str, Any] = {
                    "iap.tool.outcome": "ok",
                    "iap.tool.result_bytes": _nbytes(rec.get("output")),
                }
            else:
                msg = str(rec.get("error", ""))
                attrs = {"iap.tool.result_bytes": len(msg.encode("utf-8"))}
                if msg.startswith("tool error: "):
                    attrs["iap.tool.outcome"] = "error"
                    err = msg[len("tool error: ") :].split(":", 1)[0].strip() or "_OTHER"
                else:
                    attrs["iap.tool.outcome"] = "refused"
                    if msg in refusals:
                        err = "BudgetExceeded"
                        self.budget_refusal(span, msg)
                    else:
                        err = "ToolRefused"
        except Exception:
            self.failures += 1
            attrs = {}
        self.end(span, error=err, attributes=attrs)


def _nbytes(obj: Any) -> int:
    try:
        return len(json.dumps(obj, sort_keys=True, default=str).encode("utf-8"))
    except Exception:
        return -1


def session_tracer(out_dir: Path | str, secret: str | None, tracer: Tracer | None) -> Tracer:
    """The tracer a loop uses: ``tracer`` if given, else a default one that
    writes ``spans.jsonl`` in ``out_dir``.  Never raises."""
    if tracer is not None:
        return tracer
    try:
        return Tracer(JsonlExporter(Path(out_dir) / SPAN_FILE, secret))
    except Exception:
        t = Tracer(None)
        t.failures += 1
        return t


# -- OpenTelemetry adapter -----------------------------------------------------
def to_otel(spans: Sequence[Span | Mapping[str, Any]], otel_tracer: Any = None) -> list[Any]:
    """Re-emit finished spans through the OpenTelemetry API (parents first,
    original start and end times).  Needs ``opentelemetry`` installed; it is
    not a dependency of this project.  OpenTelemetry assigns its own ids."""
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "to_otel needs the 'opentelemetry-api' and 'opentelemetry-sdk' packages, "
            "which this project does not install; spans.jsonl stays the record"
        ) from exc
    tr = otel_tracer or trace.get_tracer("iap.llm.tracing")
    recs = [s.to_json() if isinstance(s, Span) else dict(s) for s in spans]
    recs.sort(key=lambda r: r["start_ns"])
    made: dict[str, Any] = {}
    out = []
    for r in recs:
        parent = made.get(r["parent_id"]) if r["parent_id"] else None
        ctx = trace.set_span_in_context(parent) if parent is not None else None
        s = tr.start_span(
            r["name"],
            context=ctx,
            kind=getattr(trace.SpanKind, r["kind"], trace.SpanKind.INTERNAL),
            start_time=r["start_ns"],
            attributes=r["attributes"],
        )
        for ev in r.get("events", []):
            s.add_event(ev["name"], ev.get("attributes", {}), ev["time_ns"])
        if r["status"] == "ERROR":
            s.set_status(trace.Status(trace.StatusCode.ERROR))
        elif r["status"] == "OK":
            s.set_status(trace.Status(trace.StatusCode.OK))
        s.end(end_time=r["end_ns"])
        made[r["span_id"]] = s
        out.append(s)
    return out


# -- reader --------------------------------------------------------------------
def load_spans(session_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(session_dir) / SPAN_FILE
    spans = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    # a coarse clock can give spans the same start; seq is the start order
    spans.sort(key=lambda s: (s["start_ns"], s.get("seq", 0)))
    return spans


def _ms(ns: float) -> str:
    return f"{ns / 1e6:.1f}ms"


def summarize(session_dir: Path | str, width: int = 40) -> str:
    spans = load_spans(session_dir)
    if not spans:
        return f"{session_dir}: no spans\n"
    ops = [s["attributes"].get("gen_ai.operation.name", s["kind"]) for s in spans]
    model = [s for s, o in zip(spans, ops, strict=True) if o == "chat"]
    tools = [s for s, o in zip(spans, ops, strict=True) if o == "execute_tool"]
    t0 = min(s["start_ns"] for s in spans)
    t1 = max(s["end_ns"] or s["start_ns"] for s in spans)

    def tot(key: str) -> int:
        return sum(int(s["attributes"].get(key, 0) or 0) for s in model)

    roots = [s for s in spans if s["parent_id"] is None]
    cost = sum(float(s["attributes"].get("iap.cost.usd", 0) or 0) for s in model)
    lines = [
        f"spans: {len(spans)}  traces: {len({s['trace_id'] for s in spans})}  wall: {_ms(t1 - t0)}",
        f"model calls: {len(model)}  input tokens: {tot('gen_ai.usage.input_tokens')}  "
        f"output tokens: {tot('gen_ai.usage.output_tokens')}  "
        f"cache read: {tot('gen_ai.usage.cache_read.input_tokens')}  "
        f"cache write: {tot('gen_ai.usage.cache_write.input_tokens')}  cost: ${cost:.6f}",
        f"tool calls: {len(tools)}",
    ]
    by_tool = Counter(
        (s["attributes"].get("gen_ai.tool.name"), s["attributes"].get("iap.tool.outcome"))
        for s in tools
    )
    for (name, outcome), k in sorted(
        by_tool.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
    ):
        lines.append(f"  {name}: {outcome} x{k}")
    refusals = sum(1 for s in spans for e in s["events"] if e["name"] == "budget_refusal")
    lines.append(f"budget refusals: {refusals}")
    for r in roots:
        st = r["attributes"].get("iap.session.status") or r["attributes"].get("iap.graph.status")
        if st:
            lines.append(f"status ({r['name']}): {st}")
    lat: dict[str, list[int]] = defaultdict(list)
    for s, o in zip(spans, ops, strict=True):
        if s["end_ns"] is not None:
            lat[str(o)].append(s["end_ns"] - s["start_ns"])
    lines.append("latency by operation (p50 / max):")
    for o in sorted(lat):
        v = lat[o]
        lines.append(f"  {o}: n={len(v)} p50={_ms(statistics.median(v))} max={_ms(max(v))}")
    lines.append("waterfall:")
    depth: dict[str, int] = {}
    span_total = max(t1 - t0, 1)
    for s in spans:
        d = depth.get(s["parent_id"], -1) + 1 if s["parent_id"] else 0
        depth[s["span_id"]] = d
        end = s["end_ns"] or s["start_ns"]
        a = int((s["start_ns"] - t0) * width / span_total)
        b = max(a + 1, int((end - t0) * width / span_total))
        bar = " " * a + "#" * (b - a) + " " * (width - b)
        mark = " !" if s["status"] == "ERROR" else ""
        lines.append(
            f"  |{bar}| +{_ms(s['start_ns'] - t0):>9} {_ms(end - s['start_ns']):>9} "
            f"{'  ' * d}{s['name']}{mark}"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m iap.llm.tracing")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summarize", help="totals and a waterfall for one session directory")
    s.add_argument("session_dir")
    a = p.parse_args(argv)
    try:
        sys.stdout.write(summarize(a.session_dir))
    except FileNotFoundError:
        sys.stderr.write(f"no {SPAN_FILE} in {a.session_dir}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
