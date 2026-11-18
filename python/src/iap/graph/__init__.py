"""The research workflow as one explicit, tested state graph (docs/RESEARCH_GRAPH.md).

- ``core``             - typed state-graph engine: validated build, deterministic run, event log.
- ``checkpoint``       - hash-chained JSON checkpoints, interrupts and resume.
- ``research_flow``    - hypothesis -> prior art -> draft -> human approval -> run -> triage
  -> narrative, every outside step an injected callable.
- ``langgraph_adapter`` - optional; imports ``langgraph`` only when called.

No network, no model client, no new dependency.
"""

from .checkpoint import CheckpointError, event_log, resume, start, verify_chain
from .core import Event, Graph, GraphError, Interrupt, Router, RunResult, StepLimitError, execute

__all__ = [
    "CheckpointError",
    "Event",
    "Graph",
    "GraphError",
    "Interrupt",
    "Router",
    "RunResult",
    "StepLimitError",
    "event_log",
    "execute",
    "resume",
    "start",
    "verify_chain",
]
