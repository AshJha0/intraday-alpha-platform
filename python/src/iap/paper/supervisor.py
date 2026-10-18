"""Paper-path supervisor: REL-16 monitoring x REL-17 limits x REL-18 order path.

Wires the three v1.18 live-path pieces together for a paper run (D4(a): no
broker).  One :class:`PaperSupervisor` is at the same time

* the REL-18 feed :class:`~iap.paper.feed.HealthSink` (``set_state``): a feed
  in ``UNKNOWN`` (stale, or resyncing after a gap) removes the heartbeat from
  the next monitoring snapshot, so REL-16 evaluates ``UNKNOWN``;
* the REL-18 :class:`~iap.paper.reconcile.ReconSink` (``on_recon_exceptions``):
  the exception count becomes ``OrdersSnapshot.reconciliation_exceptions``, so
  REL-16 raises ``REVIEW`` (and ``HALT`` if it persists, per its policy);
* the REL-17 :class:`~iap.risk.portfolio_limits.HaltSink` (``report_halt``):
  kill-switch trips and resets are kept in ``halt_log``;
* the REL-18 order manager's ``pre_trade`` gate: every new order goes through
  :meth:`PortfolioGuard.check_order` (a breaching order, or any order while
  the kill switch is latched, raises :class:`LimitRefused` before it is
  journaled or sent) and, while the monitoring state does not permit new risk
  (REVIEW, HALT, UNKNOWN), an order that increases an absolute position is
  refused with :class:`MonitoringRefused`; reducing orders pass.

:meth:`PaperSupervisor.evaluate` runs one REL-16 evaluation; a ``HALT`` calls
:meth:`PortfolioGuard.halt`, which latches until an explicit
:meth:`PortfolioGuard.reset` by a named operator (and REL-16 itself leaves HALT
only on an :class:`~iap.monitoring.Acknowledgement`, to REVIEW).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from iap.monitoring import (
    Acknowledgement,
    Evaluation,
    Expectation,
    MonitoringConfig,
    MonitorMemory,
    OrdersSnapshot,
    PositionsSnapshot,
    SessionObs,
    Snapshot,
    State,
    evaluate,
    permits_new_risk,
)
from iap.paper.feed import UNKNOWN
from iap.paper.oms import OmsError, OrderManager
from iap.paper.reconcile import ReconReport
from iap.risk.portfolio_limits import PortfolioGuard


class MonitoringRefused(OmsError):
    """A risk-increasing order while REL-16 does not permit new risk."""


@dataclass
class PaperSupervisor:
    cfg: MonitoringConfig
    guard: PortfolioGuard
    oms: OrderManager
    #: currency value of one price tick per share, per instrument (default 1.0)
    tick_value: Mapping[int, float] = field(default_factory=dict)
    #: last mark in ticks per instrument (positions are valued at it)
    marks: dict[int, int] = field(default_factory=dict)
    check_order_kw: dict[str, Any] = field(default_factory=dict)
    feed_state: dict[str, str] = field(default_factory=dict)
    recon_exceptions: int = 0
    last_feed_ok_ns: int | None = None
    memory: MonitorMemory = field(default_factory=MonitorMemory)
    state: State = State.ACTIVE
    last: Evaluation | None = None
    halt_log: list[tuple[str, str, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.guard.sink = self
        self.oms.pre_trade = self.pre_trade

    # ---------------------------------------------------------------- sinks
    def set_state(self, component: str, state: str, reason: str) -> None:
        self.feed_state[component] = state

    def feed_heartbeat(self, now_ns: int) -> None:
        """Call on each delivered message (the paper loop's liveness)."""
        self.last_feed_ok_ns = now_ns

    def on_recon_exceptions(self, report: ReconReport) -> None:
        self.recon_exceptions = len(report.exceptions)

    def report_halt(self, source: str, reason: str, active: bool) -> None:
        self.halt_log.append((source, reason, active))

    # ------------------------------------------------------------ valuation
    def _value(self, inst: int, qty: int, px_ticks: int | None = None) -> float:
        px = self.marks.get(inst, 0) if px_ticks is None else px_ticks
        return qty * px * float(self.tick_value.get(inst, 1.0))

    def position_notionals(self) -> dict[int, float]:
        return {i: self._value(i, q) for i, q in self.oms.positions.items()}

    # -------------------------------------------------------------- gate
    def pre_trade(
        self, oms: OrderManager, cl_ord_id: str, instrument_id: int, side: int, qty: int, px: int
    ) -> None:
        self.marks.setdefault(instrument_id, px)
        pos = oms.positions.get(instrument_id, 0)
        # open orders count against the book as if filled (worst case)
        pending = sum(
            o.side * o.leaves_qty for o in oms.open_orders() if o.instrument_id == instrument_id
        )
        increases = abs(pos + pending + side * qty) > abs(pos + pending)
        if increases and not permits_new_risk(self.state):
            raise MonitoringRefused(
                f"order {cl_ord_id!r} adds risk while monitoring state is {self.state.value}"
            )
        notionals = self.position_notionals()
        self.guard.check_order(
            instrument_id,
            self._value(instrument_id, side * qty, px),
            notionals,
            **self.check_order_kw,
        )

    # ----------------------------------------------------------- evaluation
    def snapshot(
        self, now_ns: int, expectation: Expectation | None, sessions: Sequence[SessionObs]
    ) -> Snapshot:
        notional = self.position_notionals()
        cap = self.guard.capital
        lim = self.guard.limits
        feed_ok = all(s != UNKNOWN for s in self.feed_state.values())
        return Snapshot(
            evaluated_at_ns=now_ns,
            expectation=expectation,
            sessions=tuple(sessions),
            positions=PositionsSnapshot(
                as_of_ns=now_ns,
                gross_exposure=sum(abs(v) for v in notional.values()),
                net_exposure=sum(notional.values()),
                gross_limit=lim.gross_max * cap if lim.gross_max else 1e300,
                net_limit=lim.net_max * cap if lim.net_max else 1e300,
            ),
            orders=OrdersSnapshot(
                as_of_ns=now_ns,
                open_orders=len(self.oms.open_orders()),
                reconciliation_exceptions=self.recon_exceptions,
            ),
            heartbeat_ns=(self.last_feed_ok_ns if feed_ok else None),
        )

    def evaluate(
        self,
        now_ns: int,
        expectation: Expectation | None,
        sessions: Sequence[SessionObs],
        ack: Acknowledgement | None = None,
    ) -> Evaluation:
        ev = evaluate(self.cfg, self.snapshot(now_ns, expectation, sessions), self.memory, ack)
        self.memory, self.state, self.last = ev.memory, ev.state, ev
        if ev.state is State.HALT:
            worst = [a.metric for a in ev.alerts if a.level is State.HALT] or ["state"]
            self.guard.halt(f"REL-16 monitoring HALT ({', '.join(worst)})")
        return ev
