"""``TraceBuilder`` — accumulate the stages of one decision into a
validated :class:`~iap.contracts.types.DecisionTrace`.

The builder is the only mutable object on the trace path: the loop calls
one ``add_*`` / ``set_*`` per stage output in the order the stages ran,
then :meth:`TraceBuilder.build` produces the frozen, schema-validated
trace whose id is ``make_trace_id(session_id, instrument_id, event_ts,
sequence)``.  Nothing here reads a clock or an RNG; ``build`` is a pure
function of what was added, so the same loop replays to the same trace.
"""

from __future__ import annotations

from typing import List, Optional

from iap.contracts.ids import make_trace_id
from iap.contracts.types import (
    AlphaSignal,
    Attribution,
    ChildOrder,
    DecisionTrace,
    ExecutionReport,
    ParentOrder,
    PortfolioTarget,
    RiskDecision,
    TCAResult,
    TraceStages,
    VenueDecision,
)
from iap.contracts.validate import validate_typed

__all__ = ["TraceBuilder"]


def _expect(value: object, cls: type, what: str) -> None:
    if not isinstance(value, cls):
        raise TypeError(f"TraceBuilder.{what}: expected {cls.__name__}, "
                        f"got {type(value).__name__}")


class TraceBuilder:
    """Collects stage outputs for the decision at ``(session_id,
    instrument_id, event_ts, sequence)`` under the four version hashes.

    Every ``add_*`` returns the builder (fluent).  Stages keep insertion
    order; ``build`` may be called any number of times and always returns
    an equal trace.
    """

    def __init__(self, session_id: str, instrument_id: int, event_ts: int,
                 sequence: int, data_version: str, feature_version: str,
                 model_version: str, config_version: str) -> None:
        self.session_id = session_id
        self.instrument_id = instrument_id
        self.event_ts = event_ts
        self.sequence = sequence
        self.data_version = data_version
        self.feature_version = feature_version
        self.model_version = model_version
        self.config_version = config_version
        self._signal: List[AlphaSignal] = []
        self._portfolio: Optional[PortfolioTarget] = None
        self._risk: List[RiskDecision] = []
        self._parent_orders: List[ParentOrder] = []
        self._child_orders: List[ChildOrder] = []
        self._routing: List[VenueDecision] = []
        self._fills: List[ExecutionReport] = []
        self._tca: List[TCAResult] = []
        self._attribution: Optional[Attribution] = None

    @property
    def trace_id(self) -> str:
        """The id the built trace will carry."""
        return make_trace_id(self.session_id, self.instrument_id,
                             self.event_ts, self.sequence)

    # -- stages -------------------------------------------------------------

    def add_signal(self, signal: AlphaSignal) -> "TraceBuilder":
        _expect(signal, AlphaSignal, "add_signal")
        self._signal.append(signal)
        return self

    def set_portfolio(self, target: Optional[PortfolioTarget]) -> "TraceBuilder":
        if target is not None:
            _expect(target, PortfolioTarget, "set_portfolio")
        self._portfolio = target
        return self

    def add_risk(self, decision: RiskDecision) -> "TraceBuilder":
        _expect(decision, RiskDecision, "add_risk")
        self._risk.append(decision)
        return self

    def add_parent_order(self, order: ParentOrder) -> "TraceBuilder":
        _expect(order, ParentOrder, "add_parent_order")
        self._parent_orders.append(order)
        return self

    def add_child_order(self, order: ChildOrder) -> "TraceBuilder":
        _expect(order, ChildOrder, "add_child_order")
        self._child_orders.append(order)
        return self

    def add_routing(self, decision: VenueDecision) -> "TraceBuilder":
        _expect(decision, VenueDecision, "add_routing")
        self._routing.append(decision)
        return self

    def add_fill(self, report: ExecutionReport) -> "TraceBuilder":
        _expect(report, ExecutionReport, "add_fill")
        self._fills.append(report)
        return self

    def add_tca(self, result: TCAResult) -> "TraceBuilder":
        _expect(result, TCAResult, "add_tca")
        self._tca.append(result)
        return self

    def set_attribution(self, attribution: Optional[Attribution]) -> "TraceBuilder":
        if attribution is not None:
            _expect(attribution, Attribution, "set_attribution")
        self._attribution = attribution
        return self

    # -- result -------------------------------------------------------------

    def stages(self) -> TraceStages:
        """The stages accumulated so far (frozen copy)."""
        return TraceStages(
            signal=tuple(self._signal), portfolio=self._portfolio,
            risk=tuple(self._risk), parent_orders=tuple(self._parent_orders),
            child_orders=tuple(self._child_orders), routing=tuple(self._routing),
            fills=tuple(self._fills), tca=tuple(self._tca),
            attribution=self._attribution)

    def build(self) -> DecisionTrace:
        """The validated trace (type invariants and JSON schema).  Raises
        ``ContractError`` / ``ContractValidationError`` on a violation."""
        trace = DecisionTrace(
            trace_id=self.trace_id, session_id=self.session_id,
            instrument_id=self.instrument_id, event_ts=self.event_ts,
            sequence=self.sequence, data_version=self.data_version,
            feature_version=self.feature_version, model_version=self.model_version,
            config_version=self.config_version, stages=self.stages())
        validate_typed(trace)
        return trace
