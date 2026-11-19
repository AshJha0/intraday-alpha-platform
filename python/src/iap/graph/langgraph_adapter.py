"""Optional LangGraph adapter.

``langgraph`` is not a dependency of this platform and is never imported by
default: it is imported only inside :func:`to_langgraph`, so importing this
module (or :mod:`iap.graph`) costs nothing and works without it installed.
The in-house engine (:mod:`iap.graph.core`) stays the source of truth; the
adapter translates nodes and edges only.  Checkpoints, the hash chain and
interrupts are not translated: a node that returns an
:class:`~iap.graph.core.Interrupt` raises inside the LangGraph run.
"""

from __future__ import annotations

from typing import Any

from .core import Graph, GraphError, Interrupt


class LangGraphUnavailable(ImportError):
    """``langgraph`` is not installed."""


def to_langgraph(graph: Graph) -> Any:
    """A compiled LangGraph ``StateGraph`` equivalent to ``graph`` (state as a dict)."""
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailable(
            "to_langgraph needs the optional 'langgraph' package, which is not installed "
            "and is not part of the required lock; the in-house engine iap.graph.core "
            "runs the same graph without it"
        ) from exc

    def wrap(name: str):
        fn = graph.nodes[name]

        def node(values: dict) -> dict:
            out = fn(graph.make_state(values))
            if isinstance(out, Interrupt):
                raise GraphError(f"{name}: interrupts are not supported by the LangGraph adapter")
            return dict(out)

        return node

    sg = StateGraph(dict)
    for name in sorted(graph.nodes):
        sg.add_node(name, wrap(name))
    sg.set_entry_point(graph.entry)
    for src, dst in sorted(graph.edges.items()):
        sg.add_edge(src, dst)
    for src, router in sorted(graph.routers.items()):
        sg.add_conditional_edges(
            src, lambda v, r=router: r.route(graph.make_state(v)), sorted(router.targets)
        )
    for name in sorted(graph.terminals):
        sg.add_edge(name, END)
    return sg.compile()
