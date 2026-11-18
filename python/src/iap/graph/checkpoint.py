"""Hash-chained checkpoints for graph runs.

Plain idea: after every node the run writes its whole state to a numbered
JSON file.  Each file carries the sha256 of the one before it, so editing,
deleting or reordering a file breaks the chain, and a run can only be
continued from a chain that verifies.

Formal statement: ``run_dir/ckpt-NNNNNN.json`` holds ``{"record": R,
"sha256": H}`` with ``H = sha256(canonical(R))``.  ``R["seq"]`` counts from
0 without gaps and ``R["prev_sha256"]`` equals the previous file's ``H``
(64 zeros for seq 0).  ``R["status"]`` is ``running`` (``R["next"]`` is the
node to run), ``waiting`` (``R["awaiting"]`` is the interrupt) or ``done``.
:func:`resume` refuses a chain that does not verify, a graph whose
fingerprint differs from the one recorded, and a decision that does not
match the status.  Nothing is written outside ``run_dir``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iap.governance.blackboard import canonical, digest

from .core import Clock, Event, Graph, GraphError, Interrupt, RunResult, execute, state_to_json

GENESIS = "0" * 64
FORMAT = 1


class CheckpointError(GraphError):
    """The checkpoint chain is missing, broken or does not fit the graph."""


def _path(run_dir: Path, seq: int) -> Path:
    root = run_dir.resolve()
    path = (root / f"ckpt-{seq:06d}.json").resolve()
    if path.parent != root:  # pragma: no cover - the name has no separator
        raise CheckpointError(f"checkpoint path {path} escapes {root}")
    return path


class _Writer:
    def __init__(self, run_dir: Path, graph: Graph, seq: int, prev: str) -> None:
        self.run_dir, self.graph, self.seq, self.prev = run_dir, graph, seq, prev

    def write(
        self,
        state: Any,
        status: str,
        next_node: str | None,
        event: Event | None = None,
        awaiting: Interrupt | None = None,
        decision: Any = None,
    ) -> None:
        record = {
            "format": FORMAT,
            "seq": self.seq,
            "prev_sha256": self.prev,
            "graph": self.graph.name,
            "graph_fingerprint": self.graph.fingerprint(),
            "status": status,
            "next": next_node,
            "state": state_to_json(state),
            "event": None if event is None else event.to_json(),
            "awaiting": None if awaiting is None else awaiting.to_json(),
            "decision": decision,
        }
        sha = digest(record)
        _path(self.run_dir, self.seq).write_text(
            canonical({"record": record, "sha256": sha}) + "\n", encoding="ascii"
        )
        self.seq += 1
        self.prev = sha

    def observe(self, event: Event, state: Any, interrupt: Interrupt | None) -> None:
        if interrupt is not None:
            self.write(state, "waiting", event.node, event, awaiting=interrupt)
        elif event.next is None:
            self.write(state, "done", None, event)
        else:
            self.write(state, "running", event.next, event)


def verify_chain(run_dir: Path | str) -> list[dict[str, Any]]:
    """The records of ``run_dir`` in order, or :class:`CheckpointError`."""
    run_dir = Path(run_dir)
    files = sorted(run_dir.glob("ckpt-*.json"))
    if not files:
        raise CheckpointError(f"{run_dir}: no checkpoint")
    records = []
    prev = GENESIS
    for seq, path in enumerate(files):
        if path.name != f"ckpt-{seq:06d}.json":
            raise CheckpointError(f"{path.name}: expected ckpt-{seq:06d}.json (gap in the chain)")
        try:
            wrapper = json.loads(path.read_text(encoding="ascii"))
            record, sha = wrapper["record"], wrapper["sha256"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CheckpointError(f"{path.name}: unreadable checkpoint ({exc})") from exc
        if digest(record) != sha:
            raise CheckpointError(f"{path.name}: content does not match its sha256")
        if record.get("seq") != seq or record.get("prev_sha256") != prev:
            raise CheckpointError(f"{path.name}: does not chain to the previous checkpoint")
        records.append(record)
        prev = sha
    return records


def start(
    graph: Graph,
    initial: dict[str, Any],
    run_dir: Path | str,
    *,
    clock: Clock,
    max_steps: int = 100,
) -> RunResult:
    """Run ``graph`` from its entry, checkpointing into a new or empty ``run_dir``."""
    run_dir = Path(run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise CheckpointError(f"{run_dir}: run directory is not empty")
    run_dir.mkdir(parents=True, exist_ok=True)
    state = graph.make_state(initial)
    writer = _Writer(run_dir, graph, 0, GENESIS)
    writer.write(state, "running", graph.entry)
    return execute(graph, state, clock=clock, max_steps=max_steps, observer=writer.observe)


def resume(
    run_dir: Path | str,
    graph: Graph,
    *,
    decision: Any = None,
    clock: Clock,
    max_steps: int = 100,
) -> RunResult:
    """Continue the run in ``run_dir`` from its last checkpoint.

    A ``waiting`` run needs ``decision``; it is written into the graph's
    decision field and the interrupted node runs again.  A ``running`` run
    (stopped between nodes) takes no decision.  A ``done`` run is refused."""
    run_dir = Path(run_dir)
    records = verify_chain(run_dir)
    last = records[-1]
    if last["graph_fingerprint"] != graph.fingerprint():
        raise CheckpointError(f"{run_dir}: checkpoints were written by a different graph")
    state = graph.make_state(last["state"])
    writer = _Writer(run_dir, graph, last["seq"] + 1, digest(last))
    if last["status"] == "done":
        raise CheckpointError(f"{run_dir}: run is already done")
    if last["status"] == "waiting":
        if decision is None:
            raise CheckpointError(f"{run_dir}: run is waiting; a decision is required")
        if graph.decision_field is None:
            raise CheckpointError(f"graph {graph.name!r} declares no decision field")
        state = graph.apply(state, {graph.decision_field: decision})
        writer.write(
            state, "running", last["next"], decision=state_to_json(state)[graph.decision_field]
        )
    elif decision is not None:
        raise CheckpointError(f"{run_dir}: run is not waiting; no decision is expected")
    return execute(
        graph,
        state,
        last["next"],
        clock=clock,
        max_steps=max_steps,
        first_step=sum(1 for r in records if r["event"] is not None) + 1,
        observer=writer.observe,
    )


def event_log(run_dir: Path | str) -> list[dict[str, Any]]:
    """The events recorded in a verified chain, in order."""
    return [r["event"] for r in verify_chain(run_dir) if r["event"] is not None]
