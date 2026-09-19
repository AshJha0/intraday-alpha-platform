"""Runtime-checkable interfaces for every stage of the platform loop.

These :class:`typing.Protocol` classes are the structural contracts the
MVP, lifecycle and store agents code against.  They deliberately pin only
what a stage MUST expose; a concrete class may offer more.

Determinism (PLATFORM_CONVENTIONS.md §3) is part of every interface: an
implementation is a pure function of its explicit inputs and its own state,
takes time from the events it is given (``exchange_ts`` / ``receive_ts`` /
``timestamp_ns`` arguments), never reads the wall clock, never iterates an
unordered container on a path that affects output, and draws randomness
only from an explicitly seeded SplitMix64.  Identical inputs in identical
order therefore produce identical outputs in every language.

Where an existing reference class already exposes the concept under a
pinned name, the protocol uses that name (``exchange_ts`` rather than
``timestamp_ns`` on a market event; ``state_summary`` / ``checkpoint``
rather than ``snapshot`` on a book, which would collide with the SNAPSHOT
event type).  ``mid_price`` / ``spread`` are intentionally absent from the
book interface: the book exposes integer ``best_bid`` / ``best_ask`` ticks
and callers derive mid / spread themselves (a float price on the book would
violate conventions §1).
"""

from __future__ import annotations

from typing import (
    Any,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from iap.contracts.types import (
    AlphaSignal,
    ChildOrder,
    DecisionTrace,
    ExecutionReport,
    ExperimentResult,
    ExperimentSpec,
    GateResult,
    LifecycleState,
    LifecycleTransition,
    ParentOrder,
    PortfolioTarget,
    RiskDecision,
    TCAResult,
    VenueDecision,
)

__all__ = [
    "Alpha",
    "AlphaLifecycle",
    "BookViewLike",
    "ExecutionAlgorithm",
    "ExecutionSimulatorLike",
    "ExperimentRunner",
    "Feature",
    "FeatureEngineLike",
    "FeatureVectorLike",
    "LifecycleGate",
    "MarketDataSource",
    "MarketEventLike",
    "OrderBookLike",
    "PortfolioConstructor",
    "RiskEngineLike",
    "SmartOrderRouterLike",
    "TCAEngine",
    "TraceSink",
]


@runtime_checkable
class MarketEventLike(Protocol):
    """A canonical market event (``iap.core.events.MarketEvent`` satisfies
    it).  Times are ``int`` ns: ``exchange_ts`` is event time (the replay
    clock), ``receive_ts >= exchange_ts`` the capture time."""

    event_id: int
    instrument_id: int
    venue_id: int
    exchange_ts: int
    receive_ts: int
    sequence: int
    event_type: int


@runtime_checkable
class MarketDataSource(Protocol):
    """A replayable event stream.

    ``events(start_ns, end_ns)`` yields events with
    ``start_ns <= exchange_ts < end_ns`` in canonical order (exchange_ts,
    then per-stream sequence, then event_id).  Two calls with the same
    window yield the same events; the source never blocks on a clock.
    """

    def events(self, start_ns: int, end_ns: int) -> Iterator[MarketEventLike]:
        """Yield the events of ``[start_ns, end_ns)`` in canonical order."""
        ...


@runtime_checkable
class BookViewLike(Protocol):
    """Read-only best/depth view of a book — the part shared by a venue
    book and the consolidated merge (``iap.orderbook.OrderBook`` and
    ``ConsolidatedBook`` both satisfy it).

    ``best_bid`` / ``best_ask`` return ``(price_ticks, total_size)`` or
    ``None``; ``depth`` and ``order_count`` are best-first lists of
    ``(price_ticks, value)``.  Mid and spread are derived by the caller
    from the integer best levels.
    """

    def best_bid(self) -> Optional[Tuple[int, int]]:
        ...

    def best_ask(self) -> Optional[Tuple[int, int]]:
        ...

    def depth(self, side: int, levels: int = 10) -> Sequence[Tuple[int, int]]:
        ...

    def order_count(self, side: int, levels: int = 10) -> Sequence[Tuple[int, int]]:
        ...

    def is_crossed(self) -> bool:
        ...

    def is_locked(self) -> bool:
        ...


@runtime_checkable
class OrderBookLike(BookViewLike, Protocol):
    """One venue book (``iap.orderbook.OrderBook`` satisfies it; API_CORE.md
    §4 pins the semantics).

    ``apply`` returns the pinned ``ApplyStatus`` (APPLIED / DROPPED / HELD)
    and never raises on malformed payloads.  ``is_fresh`` takes the
    caller's event clock — the book has no clock of its own.
    ``state_summary`` is the golden-comparable state dict and
    ``checkpoint`` the cross-language restore document (``x-version`` 2).
    """

    def apply(self, event: Any) -> Any:
        """Apply one event; returns the ``ApplyStatus``."""
        ...

    def is_fresh(self, now_ns: int, max_age_ns: int) -> bool:
        ...

    def state_summary(self) -> Mapping[str, Any]:
        ...

    def checkpoint(self) -> Mapping[str, Any]:
        ...


@runtime_checkable
class FeatureVectorLike(Protocol):
    """``feature_vector.schema.json`` shape (``iap.features.engine.
    FeatureVector`` satisfies it): ``values`` in registry order, a parallel
    ``validity`` mask, ``feature_version`` = the registry hash."""

    instrument_id: int
    timestamp: int
    feature_version: str
    values: Sequence[float]
    validity: Sequence[bool]


@runtime_checkable
class Feature(Protocol):
    """One registered feature definition.

    ``feature_id`` is the registry name (``ofi_l5_w1s_v1``), ``version``
    the definition version.  ``calculate(context)`` is a pure function of
    the per-instrument rolling context it is handed (book views, windows,
    session profile) and returns the value or ``None`` when invalid
    (warmup, stale book, unreliable denominator) — NaN never leaves a
    feature.
    """

    @property
    def feature_id(self) -> str:
        ...

    @property
    def version(self) -> str:
        ...

    def calculate(self, context: Any) -> Optional[float]:
        ...


@runtime_checkable
class FeatureEngineLike(Protocol):
    """Event-driven feature engine (``iap.features.engine.FeatureEngine``
    satisfies it).  ``apply(event)`` feeds one event and returns the
    emitted vector or ``None``; only APPLIED book events feed rolling
    state (API_FEATURES.md §2).  ``feature_version`` is the registry hash
    every emitted vector carries."""

    feature_version: str

    def apply(self, event: Any) -> Optional[FeatureVectorLike]:
        ...


@runtime_checkable
class Alpha(Protocol):
    """A streaming alpha: ``generate(features)`` maps one feature vector to
    one :class:`AlphaSignal` for that vector's instrument and timestamp.

    ``alpha_id`` is the pinned id (``EQ03``); ``version`` identifies the
    scoring model and parameter set (the ``model_version`` written into the
    signal).  Stateless per row unless the alpha's spec says otherwise
    (API_ALPHA.md §3); no RNG on the scoring path; an invalid input scores
    ``(0.0, confidence 0)``, never NaN.

    The research batch scorer ``iap.alpha.base.AlphaModel`` (pandas frames
    in, frames out) is a different interface and does not satisfy this
    protocol; a production port wraps it.
    """

    @property
    def alpha_id(self) -> str:
        ...

    @property
    def version(self) -> str:
        ...

    def generate(self, features: FeatureVectorLike) -> AlphaSignal:
        ...


@runtime_checkable
class PortfolioConstructor(Protocol):
    """Portfolio construction (API_PORTFOLIO_TCA.md §1).

    ``construct(signals, portfolio_state, constraints)`` maps the current
    signals, the held positions / covariance state and the constraint set
    to one :class:`PortfolioTarget`.  Fully deterministic: same inputs,
    same solver parameters ⇒ identical weights and objective; an
    infeasible problem returns the previous holding with status
    INFEASIBLE, never NaN.
    """

    def construct(self, signals: Sequence[AlphaSignal], portfolio_state: Any,
                  constraints: Any) -> PortfolioTarget:
        ...


@runtime_checkable
class RiskEngineLike(Protocol):
    """Hard pre-trade risk (spec §16, ``configs/risk/risk.json``).

    ``evaluate(order, state)`` returns exactly one :class:`RiskDecision`
    per order.  Fail-closed: any missing input (config, FX rate, bootstrap)
    is a REJECT with the pinned ``rule_id``; checks run in the pinned index
    order and the first breach decides.  This path is pure integer /
    fixed-point arithmetic over the order and the engine's replicated
    state — no wall clock, no I/O, no model inference and no LLM or other
    non-deterministic component may sit on it; a decision must be
    reproducible bit-for-bit from the audit log.
    """

    def evaluate(self, order: Any, state: Any) -> RiskDecision:
        ...


@runtime_checkable
class ExecutionAlgorithm(Protocol):
    """Parent-order slicing (TWAP / VWAP / POV / IS).

    ``generate_child_orders(parent, market)`` returns the child orders the
    algorithm wants live given the market view at the parent's current
    event time.  Child ids are assigned deterministically (parent id and
    slice index); schedules are functions of the parent's window and the
    market's event clock only.
    """

    def generate_child_orders(self, parent: ParentOrder,
                              market: Any) -> Sequence[ChildOrder]:
        ...


@runtime_checkable
class SmartOrderRouterLike(Protocol):
    """Venue selection for one child order.

    ``route(order, venues)`` scores every candidate venue and returns a
    :class:`VenueDecision` whose ``candidates`` are sorted by ``venue_id``
    and whose ``venue_id`` is 0 (NO_ROUTE) when nothing is eligible.
    Ties break on ``venue_id`` ascending — never on iteration order.
    """

    def route(self, order: ChildOrder, venues: Any) -> VenueDecision:
        ...


@runtime_checkable
class ExecutionSimulatorLike(Protocol):
    """Matching / fill simulation against the replayed book.

    ``submit(order)`` accepts a child order and returns the immediate
    reports (NEW, or an immediate fill / reject); ``on_market_event(event)``
    advances the simulator's clock to the event's ``exchange_ts`` and
    returns the reports it triggers (fills of resting orders, expiries).
    Fill prices are integer ticks; latency is the configured deterministic
    profile; any randomness comes from the seeded SplitMix64 in the
    simulator's config.
    """

    def submit(self, order: ChildOrder) -> Sequence[ExecutionReport]:
        ...

    def on_market_event(self, event: MarketEventLike) -> Sequence[ExecutionReport]:
        ...


@runtime_checkable
class TCAEngine(Protocol):
    """Transaction-cost analysis (API_PORTFOLIO_TCA.md §2).

    ``analyse(parent_order, executions, market)`` computes the pinned
    Perold decomposition and benchmarks from the parent, its execution
    reports and the market timeline over the order window, and returns a
    :class:`TCAResult`.  Raises when the window is not covered by the
    timeline; never fabricates a benchmark.
    """

    def analyse(self, parent_order: ParentOrder,
                executions: Sequence[ExecutionReport], market: Any) -> TCAResult:
        ...


@runtime_checkable
class ExperimentRunner(Protocol):
    """Runs one :class:`ExperimentSpec` to one :class:`ExperimentResult`.

    Reproducible: the same spec against the same ``dataset_version`` /
    ``feature_version`` yields the same result document (``created_ts`` is
    event / ledger time, never wall clock) and writes it under
    ``research/experiments/<experiment_id>/``.
    """

    def run(self, spec: ExperimentSpec) -> ExperimentResult:
        ...


@runtime_checkable
class LifecycleGate(Protocol):
    """One promotion / demotion gate.  ``evaluate(alpha_id, evidence)``
    compares the evidence (an ``ExperimentResult``, rolling-IC window,
    drift statistics, ...) with the gate's threshold and returns a
    :class:`GateResult`.  Pure function of its arguments."""

    @property
    def name(self) -> str:
        ...

    def evaluate(self, alpha_id: str, evidence: Any) -> GateResult:
        ...


@runtime_checkable
class AlphaLifecycle(Protocol):
    """State machine over :class:`LifecycleState`.

    ``state(alpha_id)`` is the current state; ``advance(alpha_id, event_ts,
    evidence)`` applies the policy's gates at event time ``event_ts`` and
    returns the :class:`LifecycleTransition` it made, or ``None`` when the
    alpha stays put.  Transitions are appended to a log whose replay
    reproduces the states.  ``iap.adaptive.lifecycle.LifecycleTracker`` is
    the ACTIVE / WATCH / RETIRED sub-machine (``update(ts, rolling_ic)``);
    it does not satisfy this protocol and is wrapped by the lifecycle
    service.
    """

    def state(self, alpha_id: str) -> LifecycleState:
        ...

    def advance(self, alpha_id: str, event_ts: int,
                evidence: Any) -> Optional[LifecycleTransition]:
        ...


@runtime_checkable
class TraceSink(Protocol):
    """Receives every :class:`DecisionTrace`.  ``emit`` must be
    non-blocking with respect to the decision path and must not mutate the
    trace; ordering by ``(event_ts, sequence)`` is the sink's contract."""

    def emit(self, trace: DecisionTrace) -> None:
        ...
