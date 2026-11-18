"""Optional tracing of a research-graph run (see ``docs/TRACING.md``).

:func:`iap.graph.core.execute` calls :func:`traced_execute` only when it is
given a tracer.  The run itself is the same: the same node functions run in
the same order on the same states.  The only difference is that each node
function is wrapped so a span opens before it runs, and the observer closes
that span with the event's hashes and outcome.  Spans hold node names,
state hashes, outcomes and interrupt reasons, never state contents.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any


def traced_execute(
    execute: Callable[..., Any],
    graph: Any,
    state: Any,
    node: str | None,
    tracer: Any,
    **kw: Any,
) -> Any:
    user_observer = kw.pop("observer", None)
    root = tracer.start(
        f"invoke_workflow {graph.name}",
        "INTERNAL",
        {
            "gen_ai.operation.name": "invoke_workflow",
            "gen_ai.workflow.name": graph.name,
            "iap.graph.fingerprint": graph.fingerprint(),
            "iap.graph.start_node": graph.entry if node is None else node,
        },
    )
    open_spans: list[Any] = []

    def wrap(name: str, fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def run(st: Any) -> Any:
            open_spans.append(
                tracer.start(
                    f"graph_node {name}",
                    "INTERNAL",
                    {"iap.graph.node": name},
                    parent=root,
                )
            )
            return fn(st)

        return run

    def observe(event: Any, st: Any, interrupt: Any) -> None:
        span = open_spans.pop() if open_spans else None
        attrs: dict[str, Any] = {
            "iap.graph.step": event.step,
            "iap.graph.input_sha256": event.input_sha256,
            "iap.graph.output_sha256": event.output_sha256,
            "iap.graph.outcome": event.outcome,
        }
        if event.next is not None:
            attrs["iap.graph.next"] = event.next
        if interrupt is not None:
            attrs["iap.graph.interrupt_reason"] = interrupt.reason
        tracer.end(span, attributes=attrs)
        if user_observer is not None:
            user_observer(event, st, interrupt)

    traced = copy.copy(graph)
    traced.nodes = {k: wrap(k, fn) for k, fn in graph.nodes.items()}
    try:
        result = execute(traced, state, node, observer=observe, **kw)
    except BaseException as exc:
        while open_spans:
            tracer.end(open_spans.pop(), error=exc)
        tracer.end(root, error=exc)
        raise
    attrs = {"iap.graph.status": result.status, "iap.graph.steps": len(result.events)}
    if result.interrupt is not None:
        attrs["iap.graph.interrupt_reason"] = result.interrupt.reason
    tracer.end(root, attributes=attrs)
    return result
