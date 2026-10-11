"""Live-order bookkeeping of the MVP engine: the declared block controls, the
PASSIVE child state machine states and the live parent / decision records
(split out of :mod:`iap.mvp.engine`, v1.13 E4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iap.contracts.types import (
    AlphaSignal,
    ChildOrder,
    ExecutionReport,
    ParentOrder,
)
from iap.mvp.adapters import (
    SimulatorAdapter,
)
from iap.trace.builder import TraceBuilder

#: The declared controls a generated child can be blocked by, in check order
#: (Java BacktestEngine.decide).  A blocked child never leaves the strategy:
#: it gets no routing, no risk decision and no report, so it is NOT written
#: into the trace's child_orders / routing stages — only counted here and in
#: ``ParentOrder.params["children_blocked_<control>"]`` at finalisation.
BLOCK_CONTROLS = ("slice_interval", "latency_budget", "participation")


#: States of a posted child in the PASSIVE state machine
#: (``iap.execution.passive``; the same machine as ``ExecutionReplay``).
_REST = 0
_CANCEL_REPRICE = 1
_CANCEL_CROSS_TIMEOUT = 2
_CANCEL_CROSS_BEHIND = 3
_DONE = 4


@dataclass
class _Working:
    child_order_id: int
    deadline: int
    reprices: int
    state: int = _REST


@dataclass
class _LiveParent:
    parent: ParentOrder
    signal: AlphaSignal
    decision: _Decision
    children: dict[int, ChildOrder] = field(default_factory=dict)  #: submitted, by id
    reports: list[ExecutionReport] = field(default_factory=list)
    rejected: int = 0  #: generated but not submitted (any reason)
    #: control-blocked children by control (never routed to risk or a venue;
    #: written into ParentOrder.params as children_blocked_<control>)
    blocked: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BLOCK_CONTROLS, 0))
    fill_impact: float = 0.0
    fill_fees: float = 0.0
    #: PASSIVE policy: the posted children being worked (posting order).
    working: list[_Working] = field(default_factory=list)

    def all_terminal(self, sim: SimulatorAdapter) -> bool:
        return all(sim.open_qty(cid) == 0 for cid in self.children)

    def committed(self, sim: SimulatorAdapter) -> int:
        """Filled + open qty over the parent's submitted children."""
        filled = sum(r.filled_qty for r in self.reports)
        return filled + sum(sim.open_qty(cid) for cid in self.children)


@dataclass
class _Decision:
    builder: TraceBuilder
    ready: bool = False
