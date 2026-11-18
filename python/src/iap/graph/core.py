"""A small typed state-graph engine.

Plain idea: a workflow is a set of named steps (nodes) joined by arrows
(edges).  Each node is a pure function that reads the current state and
returns the fields it changes.  The engine applies the change, checks the
new state, picks the next node and records what happened.

Formal statement:

* A *state* is an instance of a frozen dataclass (the graph's ``schema``)
  whose fields hold JSON values.  If the schema defines ``check(self)``,
  the engine calls it after every transition; it raises to reject a state.
* A *node* is ``fn(state) -> Mapping[str, Any] | Interrupt``.  A mapping
  names schema fields only; anything else is a :class:`GraphError`.
* An *edge* is either static (``a -> b``) or conditional: a router
  ``route(state) -> str`` with a declared target set.  A router that
  returns a name outside its set is a :class:`GraphError` at run time.
* *Terminal* nodes run like any node and then the run ends.
* :meth:`Graph.__init__` validates the definition: an unknown entry or edge
  target, a node with no outgoing edge that is not terminal, a terminal with
  an outgoing edge, no terminal at all, or a node unreachable from the entry
  are all :class:`GraphError`.

Execution is deterministic: the same graph, initial state and injected
callables give the same sequence of states.  Durations come from an
injected clock so tests can make them deterministic too.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from iap.governance.blackboard import canonical, digest

Clock = Callable[[], float]


class GraphError(ValueError):
    """A graph definition or a transition is invalid."""


class StepLimitError(GraphError):
    """A run executed more nodes than its step limit allows."""


@dataclass(frozen=True)
class Interrupt:
    """Returned by a node that must wait for an outside decision."""

    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"reason": self.reason, "payload": _jsonify(dict(self.payload))}


@dataclass(frozen=True)
class Router:
    """A conditional edge: ``route(state)`` names one of ``targets``."""

    route: Callable[[Any], str]
    targets: frozenset[str]


@dataclass(frozen=True)
class Event:
    """One node execution."""

    step: int
    node: str
    input_sha256: str
    output_sha256: str
    duration: float
    outcome: str  # "ok", "interrupt" or "terminal"
    next: str | None

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class RunResult:
    status: str  # "done" or "waiting"
    state: Any
    node: str  # the terminal node (done) or the interrupted node (waiting)
    events: tuple[Event, ...]
    interrupt: Interrupt | None = None


def _jsonify(obj: Any) -> Any:
    """A deep copy of ``obj`` through canonical JSON; non-JSON values raise."""
    try:
        return json.loads(canonical(obj))
    except (TypeError, ValueError) as exc:
        raise GraphError(f"value is not canonical JSON: {exc}") from exc


def state_to_json(state: Any) -> dict[str, Any]:
    return _jsonify({f.name: getattr(state, f.name) for f in dataclasses.fields(state)})


def state_sha256(state: Any) -> str:
    return digest(state_to_json(state))


class Graph:
    """A validated state graph.  See the module docstring for the rules."""

    def __init__(
        self,
        name: str,
        schema: type,
        nodes: Mapping[str, Callable[[Any], Any]],
        entry: str,
        edges: Mapping[str, str] | None = None,
        routers: Mapping[str, Router] | None = None,
        terminals: Iterable[str] = (),
        decision_field: str | None = None,
    ) -> None:
        if not (dataclasses.is_dataclass(schema) and schema.__dataclass_params__.frozen):  # type: ignore[attr-defined]
            raise GraphError("schema must be a frozen dataclass")
        self.name = name
        self.schema = schema
        self.fields = frozenset(f.name for f in dataclasses.fields(schema))
        self.nodes = dict(nodes)
        self.entry = entry
        self.edges = dict(edges or {})
        self.routers = dict(routers or {})
        self.terminals = frozenset(terminals)
        if decision_field is not None and decision_field not in self.fields:
            raise GraphError(f"decision field {decision_field!r} is not a schema field")
        self.decision_field = decision_field
        self._validate()

    # -- build-time validation -------------------------------------------------
    def _validate(self) -> None:
        known = set(self.nodes)
        if self.entry not in known:
            raise GraphError(f"entry {self.entry!r} is not a node")
        if not self.terminals:
            raise GraphError("graph has no terminal node")
        for name in sorted(self.terminals - known):
            raise GraphError(f"terminal {name!r} is not a node")
        for src in sorted(set(self.edges) | set(self.routers)):
            if src not in known:
                raise GraphError(f"edge from unknown node {src!r}")
            if src in self.edges and src in self.routers:
                raise GraphError(f"node {src!r} has both a static and a conditional edge")
            if src in self.terminals:
                raise GraphError(f"terminal {src!r} has an outgoing edge")
        for src, dst in self.edges.items():
            if dst not in known:
                raise GraphError(f"edge {src!r} -> unknown node {dst!r}")
        for src, router in self.routers.items():
            if not router.targets:
                raise GraphError(f"router at {src!r} declares no target")
            for dst in sorted(router.targets - known):
                raise GraphError(f"router at {src!r} -> unknown node {dst!r}")
        for name in sorted(known - self.terminals):
            if name not in self.edges and name not in self.routers:
                raise GraphError(f"node {name!r} has no outgoing edge and is not terminal")
        unreachable = known - self.reachable()
        if unreachable:
            raise GraphError(f"unreachable node(s): {', '.join(sorted(unreachable))}")

    def successors(self, name: str) -> list[str]:
        if name in self.edges:
            return [self.edges[name]]
        if name in self.routers:
            return sorted(self.routers[name].targets)
        return []

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        todo = [self.entry]
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                todo.extend(self.successors(name))
        return seen

    def fingerprint(self) -> str:
        """sha256 of the graph's shape (names and edges, not function bodies)."""
        return digest(
            {
                "name": self.name,
                "schema": sorted(self.fields),
                "nodes": sorted(self.nodes),
                "entry": self.entry,
                "edges": self.edges,
                "routers": {k: sorted(r.targets) for k, r in self.routers.items()},
                "terminals": sorted(self.terminals),
            }
        )

    def to_mermaid(self) -> str:
        """A Mermaid flowchart generated from the definition (sorted, stable)."""
        lines = ["flowchart TD"]
        for name in sorted(self.nodes):
            lines.append(
                f"    {name}([{name}])" if name in self.terminals else f"    {name}[{name}]"
            )
        lines.append(f"    START((start)) --> {self.entry}")
        for src in sorted(self.edges):
            lines.append(f"    {src} --> {self.edges[src]}")
        for src in sorted(self.routers):
            for dst in sorted(self.routers[src].targets):
                lines.append(f"    {src} -.-> {dst}")
        return "\n".join(lines) + "\n"

    # -- transitions -----------------------------------------------------------
    def make_state(self, values: Mapping[str, Any]) -> Any:
        unknown = set(values) - self.fields
        if unknown:
            raise GraphError(f"unknown state field(s): {', '.join(sorted(unknown))}")
        try:
            state = self.schema(**_jsonify(dict(values)))
        except TypeError as exc:
            raise GraphError(f"state does not fit the schema: {exc}") from exc
        check = getattr(state, "check", None)
        if check is not None:
            try:
                check()
            except (TypeError, ValueError) as exc:
                raise GraphError(f"state check failed: {exc}") from exc
        return state

    def apply(self, state: Any, update: Mapping[str, Any]) -> Any:
        if not isinstance(update, Mapping):
            raise GraphError(f"node returned {type(update).__name__}, not a mapping or Interrupt")
        return self.make_state({**state_to_json(state), **dict(update)})

    def next_node(self, name: str, state: Any) -> str | None:
        if name in self.terminals:
            return None
        if name in self.edges:
            return self.edges[name]
        router = self.routers[name]
        target = router.route(state)
        if target not in router.targets:
            raise GraphError(
                f"router at {name!r} returned {target!r}, not one of {sorted(router.targets)}"
            )
        return target


Observer = Callable[[Event, Any, "Interrupt | None"], None]


def execute(
    graph: Graph,
    state: Any,
    node: str | None = None,
    *,
    clock: Clock,
    max_steps: int = 100,
    first_step: int = 1,
    observer: Observer | None = None,
) -> RunResult:
    """Run ``graph`` from ``node`` (default: the entry) until a terminal
    node finishes or a node interrupts.  ``observer(event, state, interrupt)``
    is called after every node (the checkpoint writer hooks in here).
    More than ``max_steps`` node executions raise :class:`StepLimitError`."""
    if max_steps < 1:
        raise GraphError("max_steps must be at least 1")
    current = graph.entry if node is None else node
    if current not in graph.nodes:
        raise GraphError(f"cannot start at unknown node {current!r}")
    events: list[Event] = []
    step = first_step
    while True:
        if len(events) >= max_steps:
            raise StepLimitError(f"step limit {max_steps} reached before node {current!r}")
        in_hash = state_sha256(state)
        start = clock()
        out = graph.nodes[current](state)
        if isinstance(out, Interrupt):
            duration = clock() - start
            event = Event(
                step, current, in_hash, digest(out.to_json()), duration, "interrupt", None
            )
            events.append(event)
            if observer is not None:
                observer(event, state, out)
            return RunResult("waiting", state, current, tuple(events), out)
        state = graph.apply(state, out)
        nxt = graph.next_node(current, state)
        duration = clock() - start
        outcome = "terminal" if nxt is None else "ok"
        event = Event(step, current, in_hash, state_sha256(state), duration, outcome, nxt)
        events.append(event)
        if observer is not None:
            observer(event, state, None)
        if nxt is None:
            return RunResult("done", state, current, tuple(events))
        current = nxt
        step += 1
