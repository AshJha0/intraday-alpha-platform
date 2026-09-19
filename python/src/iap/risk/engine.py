"""The FAIL-CLOSED hard risk engine — Python reference port.

``rust/risk/src/engine.rs`` is the normative implementation and
PLATFORM_CONVENTIONS.md §11.1 the pinned contract; this module is a
statement-for-statement port (same state, same check order, same float
expression order, same reason strings) proven equivalent by the shared
goldens (``tests/golden/expected_risk_{decisions,audit,snapshot}``).

Pre-trade checks run in a PINNED order — the first failing rule decides
and is emitted as the decision's ``rule_id`` (deterministic, replayable):

0. ``CONFIG_MISSING`` / ``NOT_BOOTSTRAPPED``
1. ``KILL_GLOBAL``  2. ``KILL_STRATEGY``  3. ``KILL_INSTRUMENT``
4. ``KILL_VENUE``  5. ``MALFORMED_ORDER``  6. ``UNKNOWN_INSTRUMENT``
7. ``DUPLICATE_ORDER_ID``  8. ``VENUE_DISCONNECTED``  9. ``SEQUENCE_GAP``
10. ``STALE_PRICE``  11. ``FAT_FINGER_QTY``  12. ``FX_RATE_MISSING``
13. ``FAT_FINGER_NOTIONAL``  14. ``PRICE_BAND``  15. ``RATE_THROTTLE``
16. ``SELF_MATCH``  17. ``POSITION_LIMIT``  18. ``INSTRUMENT_NOTIONAL``
19. ``GROSS_NOTIONAL``  20. ``NET_NOTIONAL``  21. ``DAILY_LOSS``
22. ``STRATEGY_LOSS`` — else ``ALLOW``.

Pinned semantics (the Rust module docs, verbatim in substance):

- **Fail-closed**: an engine built from a missing/invalid config rejects
  everything with ``CONFIG_MISSING`` (severity BREACH); an instrument with
  no reference data, an unmarked nonzero position or a position/P&L in a
  currency without a conversion rate rejects rather than guesses.
- **Reference data**: :class:`InstrumentRef` ``{tick_size, qty_unit,
  quote_ccy}``; ``qty_unit`` is the real base units per qty unit
  (``lot_size`` for FX, 1 for EQUITY/ETF — conventions §1).
- **Money**: every notional and P&L figure is in the reporting currency
  (``currency.reporting_ccy``). ``notional = qty * qty_unit * price *
  fx_rate(quote_ccy)``. The rate of a non-reporting quote currency is the
  last consolidated mid of the pair named in ``currency.conversion``
  (inverted when the pair is REPORTING/CCY). Pre-trade the rate must be
  present and not older than ``stale_feed_timeout_ns`` (else
  ``FX_RATE_MISSING``); kill evaluation on fills/marks uses the last rate
  regardless of age. A P&L bucket whose rate is missing makes the loss
  checks undeterminable: orders reject with ``FX_RATE_MISSING``, no kill
  is latched.
- **Reference price**: the last consolidated mid ``(bid + ask) / 2 *
  tick`` stamped with the market-data event time. Updates older than the
  stored one are dropped and counted (``risk_market_regressions_dropped_total``).
  Priced orders use their limit price for notionals, unpriced orders the
  mid. No mid ever seen => ``STALE_PRICE``.
- **Throttle**: per-strategy event-time token bucket (capacity
  ``order_rate_burst``, refill ``max_order_rate_per_sec``/s). A token is
  consumed by every order reaching check 15; event time never refills
  backwards: ``last_ts = max(last_ts, order.timestamp)``.
- **Open orders**: EVERY allowed order is tracked as open (remaining qty)
  regardless of type until :meth:`RiskEngine.on_order_done` or a full
  fill; fills with the order id reduce it. PEG orders are tracked at their
  pegged touch; MARKET / unpriced IOC/FOK / MID are tracked unpriced (0).
- **Self-match prevention** (firm-wide, any venue): a priced buy (sell)
  crosses an own open priced ask (bid) at price >= (<=); an unpriced order
  on either side crosses unconditionally.
- **Projections** (worst case): buys check ``pos + open_buy_qty + qty``,
  sells ``pos - open_sell_qty - qty``; gross/net include every open order
  at its limit price (unpriced: the mid).
- **Loss limits** act on daily P&L = realized (average-cost lots per
  (strategy, instrument), kept natively per (strategy, quote ccy)) +
  unrealized (``pos * (mark - avg_price) * qty_unit``), converted at the
  last rate. Evaluated after every fill AND after every mark of a held
  instrument; a breach latches STRATEGY then GLOBAL (each once per latch).
- **Re-arm precedence**: ``clear_kill`` clears only the switch (checks
  21/22 keep rejecting, the next fill/mark re-latches);
  ``override_loss_limit`` replaces the effective limit (audited) and never
  clears a latch; ``roll_session`` zeroes realized P&L, re-bases marked
  lots, clears overrides and keeps every kill switch.
- **Malformed fills** (qty <= 0, side > 1, price_ticks <= 0, unknown
  instrument) are NOT applied: ``MALFORMED_FILL`` audit record +
  ``risk_malformed_fills_total``; ``on_fill`` returns ``False``.
- **Snapshot / restore**: :meth:`RiskEngine.snapshot` serialises the full
  mutable state (x-version 1); :meth:`RiskEngine.restore` resumes it with
  bit-identical subsequent decisions and audit lines; an engine that
  ``require_bootstrap`` rejects everything with ``NOT_BOOTSTRAPPED`` until
  ``bootstrap_positions`` or ``restore``.
- **Integer domain**: every i64/u64 operation the Rust engine performs is
  emulated with an explicit range check (:func:`_i64`); an operation that
  would leave the domain raises ``OverflowError`` exactly where Rust's
  overflow-checked arithmetic (the profile ``cargo test`` proves the
  goldens under) panics — never wrapped, never silently a bigint.
- **Determinism**: no wall clock; every map the Rust engine keeps in a
  ``BTreeMap`` is iterated here in sorted key order, so float sums are
  accumulated in the identical order.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from iap.risk.events import Decision, RiskEvent, Rules, Scope, Severity, fmt_fixed
from iap.risk.limits import RiskLimits
from iap.risk.orders import Fill, OrderRequest, OrderType, order_validation_error
from iap.risk.refdata import InstrumentRef, equity_refs
from iap.risk.serialize import to_canonical_json

__all__ = ["RiskDecision", "RiskEngine", "RiskMetrics", "SNAPSHOT_VERSION"]

_NS_PER_SEC = 1e9
#: Snapshot schema version.
SNAPSHOT_VERSION = 1

_I64_MAX = (1 << 63) - 1
_I64_MIN = -(1 << 63)
_U64_MAX = (1 << 64) - 1
_U32_MAX = (1 << 32) - 1
_U16_MAX = (1 << 16) - 1
_UINT_RE = re.compile(r"\+?[0-9]+")


def _i64(v: int) -> int:
    """Checked i64 arithmetic result (``OverflowError`` outside the domain)."""
    if v < _I64_MIN or v > _I64_MAX:
        raise OverflowError(f"i64 overflow: {v}")
    return v


def _u64(v: int) -> int:
    if v < 0 or v > _U64_MAX:
        raise OverflowError(f"u64 overflow: {v}")
    return v


def _require(name: str, value: object, lo: int, hi: int) -> int:
    """Typed argument domain (the Rust signature's integer type)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if not (lo <= value <= hi):
        raise ValueError(f"{name} out of range [{lo}, {hi}]: {value}")
    return value


def _parse_uint(text: str, hi: int) -> Optional[int]:
    """``str::parse::<uN>()``: ASCII digits with an optional leading ``+``,
    within range; ``None`` otherwise."""
    if not isinstance(text, str) or _UINT_RE.fullmatch(text) is None:
        return None
    v = int(text.lstrip("+"))
    return v if v <= hi else None


@dataclass(frozen=True)
class RiskDecision:
    """The outcome of one pre-trade check."""

    #: ALLOW / REJECT (KILL never decides an order directly).
    decision: Decision
    #: The deciding rule.
    rule_id: str
    #: INFO/WARN/BREACH.
    severity: Severity
    #: Human-readable reason.
    reason: str

    def allowed(self) -> bool:
        """True when the order may proceed."""
        return self.decision == Decision.ALLOW


class RiskMetrics:
    """Engine metrics (counters and gauges) — the ``telemetry::Registry``
    surface the Rust engine exposes as ``metrics``."""

    __slots__ = ("_counters", "_gauges")

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}

    def inc(self, name: str, n: int = 1) -> None:
        """Increment a counter (created at 0 on first use)."""
        self._counters[name] = self._counters.get(name, 0) + n

    def set_gauge(self, name: str, value: float) -> None:
        """Set a gauge."""
        self._gauges[name] = float(value)

    def counter_value(self, name: str) -> int:
        """Counter value, 0 when never touched."""
        return self._counters.get(name, 0)

    def gauge_value(self, name: str) -> Optional[float]:
        """Gauge value, ``None`` when never set."""
        return self._gauges.get(name)

    def counters(self) -> Dict[str, int]:
        """Sorted copy of every counter."""
        return {k: self._counters[k] for k in sorted(self._counters)}

    def gauges(self) -> Dict[str, float]:
        """Sorted copy of every gauge."""
        return {k: self._gauges[k] for k in sorted(self._gauges)}


class _MarketState:
    __slots__ = ("bid_ticks", "ask_ticks", "ts", "gaps", "gated")

    def __init__(self, bid_ticks: int, ask_ticks: int, ts: int, gaps: int, gated: bool) -> None:
        self.bid_ticks = bid_ticks
        self.ask_ticks = ask_ticks
        self.ts = ts
        self.gaps = gaps
        self.gated = gated


class _OpenOrder:
    __slots__ = ("instrument_id", "side", "price_ticks", "qty")

    def __init__(self, instrument_id: int, side: int, price_ticks: int, qty: int) -> None:
        self.instrument_id = instrument_id
        self.side = side
        #: Limit / pegged price in ticks; 0 = unpriced (MARKET, unpriced
        #: IOC/FOK, MID).
        self.price_ticks = price_ticks
        self.qty = qty


class _Bucket:
    __slots__ = ("tokens", "last_ts", "primed")

    def __init__(self, tokens: float, last_ts: int, primed: bool) -> None:
        self.tokens = tokens
        self.last_ts = last_ts
        self.primed = primed


class _Lot:
    __slots__ = ("pos", "avg_price")

    def __init__(self, pos: int = 0, avg_price: float = 0.0) -> None:
        self.pos = pos
        #: Real price per base unit (quote ccy).
        self.avg_price = avg_price


_GLOBAL_SCOPE_RULES = frozenset({
    Rules.KILL_GLOBAL, Rules.GROSS_NOTIONAL, Rules.NET_NOTIONAL,
    Rules.DAILY_LOSS, Rules.CONFIG_MISSING, Rules.NOT_BOOTSTRAPPED,
})
_STRATEGY_SCOPE_RULES = frozenset({
    Rules.KILL_STRATEGY, Rules.MALFORMED_ORDER, Rules.DUPLICATE_ORDER_ID,
    Rules.RATE_THROTTLE, Rules.STRATEGY_LOSS, Rules.ALLOW,
})
_VENUE_SCOPE_RULES = frozenset({Rules.KILL_VENUE, Rules.VENUE_DISCONNECTED})


class RiskEngine:
    """Fail-closed hard risk engine (see the module docstring)."""

    def __init__(self, limits: RiskLimits, instruments: Mapping[int, InstrumentRef]) -> None:
        """New engine from parsed limits + per-instrument reference data."""
        if not isinstance(limits, RiskLimits):
            raise ValueError("limits must be a RiskLimits")
        self._init(limits, instruments, "")

    def _init(
        self,
        limits: Optional[RiskLimits],
        instruments: Mapping[int, InstrumentRef],
        config_error: str,
    ) -> None:
        refs: Dict[int, InstrumentRef] = {}
        for iid in sorted(instruments):
            ref = instruments[iid]
            if not isinstance(ref, InstrumentRef):
                raise ValueError(f"instrument {iid!r}: reference must be an InstrumentRef")
            refs[_require("instrument_id", iid, 0, _U32_MAX)] = ref
        self._limits: Optional[RiskLimits] = limits
        self._config_error: str = config_error
        self._instruments: Dict[int, InstrumentRef] = refs
        self._bootstrapped: bool = True
        self._kill_global: bool = bool(limits.kill_switch_engaged) if limits else False
        self._kill_strategies: Dict[str, bool] = {}
        self._kill_instruments: Dict[int, bool] = {}
        self._kill_venues: Dict[int, bool] = {}
        self._venues_down: Dict[int, bool] = {}
        self._market: Dict[int, _MarketState] = {}
        self._seen_orders: Dict[int, int] = {}
        self._buckets: Dict[str, _Bucket] = {}
        self._open: Dict[int, _OpenOrder] = {}
        self._positions: Dict[int, int] = {}
        self._lots: Dict[Tuple[str, int], _Lot] = {}
        #: Realized P&L in the instrument's quote currency per (strategy, ccy).
        self._realized: Dict[Tuple[str, str], float] = {}
        self._loss_override_global: Optional[float] = None
        self._loss_override_strategy: Dict[str, float] = {}
        self._audit: List[RiskEvent] = []
        #: Engine metrics (decision counters, PnL gauges).
        self.metrics: RiskMetrics = RiskMetrics()
        self.metrics.set_gauge("risk_kill_switch_engaged", 1.0 if self._kill_global else 0.0)

    # ------------------------------------------------------------ builders

    @classmethod
    def with_ticks(cls, limits: RiskLimits, ticks: Mapping[int, float]) -> "RiskEngine":
        """New engine from parsed limits + tick sizes only (every instrument
        a USD equity: qty_unit 1)."""
        return cls(limits, equity_refs(ticks))

    @classmethod
    def fail_closed(cls, reason: str) -> "RiskEngine":
        """New engine in FAIL-CLOSED mode: every order is rejected with
        ``CONFIG_MISSING`` carrying ``reason``. This is the mandatory
        landing state for any configuration error."""
        eng = cls.__new__(cls)
        eng._init(None, {}, str(reason))
        return eng

    @classmethod
    def from_config(cls, doc: Any, instruments: Mapping[int, InstrumentRef]) -> "RiskEngine":
        """Build from a parsed ``configs/risk/risk.json`` document: a parse
        failure lands fail-closed instead of raising (hard risk never runs
        open). The reason carries the Rust error rendering
        (``invalid argument: risk.json: ...``)."""
        try:
            limits = RiskLimits.from_json(doc)
        except ValueError as e:
            return cls.fail_closed(f"invalid argument: {e}")
        return cls(limits, instruments)

    @classmethod
    def from_config_ticks(cls, doc: Any, ticks: Mapping[int, float]) -> "RiskEngine":
        """:meth:`from_config` with tick sizes only (USD equities)."""
        return cls.from_config(doc, equity_refs(ticks))

    # ----------------------------------------------------------- bootstrap

    def require_bootstrap(self) -> None:
        """Enter the awaiting-bootstrap state: every order rejects with
        ``NOT_BOOTSTRAPPED`` until :meth:`bootstrap_positions` or
        :meth:`restore` supplies the real positions."""
        self._bootstrapped = False

    def is_bootstrapped(self) -> bool:
        """True once positions are trusted (default for a fresh engine)."""
        return self._bootstrapped

    def bootstrap_positions(self, fills: Sequence[Fill], ts: int) -> int:
        """Apply drop-copy fills through the normal fill path (P&L
        accounted, loss limits evaluated — fail-closed), then mark the
        engine bootstrapped. Returns the number of fills rejected as
        malformed."""
        _require("ts", ts, _I64_MIN, _I64_MAX)
        bad = 0
        for f in fills:
            if not self.on_fill(f):
                bad += 1
        self._bootstrapped = True
        self._emit(RiskEvent(
            timestamp=ts,
            scope=Scope.GLOBAL,
            scope_id="",
            rule_id=Rules.BOOTSTRAP_COMPLETE,
            severity=Severity.INFO,
            decision=Decision.ALLOW,
            reason=f"bootstrapped from {len(fills)} drop-copy fills ({bad} rejected)",
        ))
        return bad

    # ------------------------------------------------------------ state in

    def on_market(self, instrument_id: int, bid_ticks: int, ask_ticks: int, ts: int) -> None:
        """Consolidated market update (best bid/ask ticks at the
        market-data event time). Updates older than the stored state are
        dropped and counted. Re-evaluates the loss limits of every
        strategy holding the instrument (a mark move can latch a kill with
        no fill)."""
        _require("instrument_id", instrument_id, 0, _U32_MAX)
        _require("bid_ticks", bid_ticks, _I64_MIN, _I64_MAX)
        _require("ask_ticks", ask_ticks, _I64_MIN, _I64_MAX)
        _require("ts", ts, _I64_MIN, _I64_MAX)
        st = self._market.get(instrument_id)
        if st is None:
            st = _MarketState(bid_ticks, ask_ticks, ts, 0, False)
            self._market[instrument_id] = st
        if ts < st.ts:
            self.metrics.inc("risk_market_regressions_dropped_total")
            return
        st.bid_ticks = bid_ticks
        st.ask_ticks = ask_ticks
        st.ts = ts
        holders = [
            sid for (sid, iid), lot in sorted(self._lots.items())
            if iid == instrument_id and lot.pos != 0
        ]
        # A conversion pair's mid moves every bucket in that currency: the
        # global check covers it; strategies are re-checked only when they
        # hold the instrument (bounded work per update).
        self._evaluate_loss_limits(ts, holders)

    def on_sequence_gap(self, instrument_id: int, ts: int) -> None:
        """A sequence gap on the instrument's feed; the gate closes after
        ``max_sequence_gap_before_halt`` gaps and stays closed until
        :meth:`on_feed_recovered`."""
        _require("instrument_id", instrument_id, 0, _U32_MAX)
        _require("ts", ts, _I64_MIN, _I64_MAX)
        threshold = self._limits.max_sequence_gap_before_halt if self._limits else 0
        st = self._market.get(instrument_id)
        if st is None:
            st = _MarketState(0, 0, ts, 0, False)
            self._market[instrument_id] = st
        st.gaps = _u64(st.gaps + 1)
        if st.gaps >= threshold:
            st.gated = True

    def on_feed_recovered(self, instrument_id: int, ts: int) -> None:
        """The feed recovered (snapshot complete): the gap gate reopens."""
        _require("instrument_id", instrument_id, 0, _U32_MAX)
        _require("ts", ts, _I64_MIN, _I64_MAX)
        st = self._market.get(instrument_id)
        if st is not None:
            st.gated = False
            st.gaps = 0

    def on_venue_disconnect(self, venue_id: int, ts: int) -> None:
        """Venue disconnect: orders to the venue reject until reconnect."""
        _require("venue_id", venue_id, 0, _U16_MAX)
        _require("ts", ts, _I64_MIN, _I64_MAX)
        self._venues_down[venue_id] = True
        self._emit(RiskEvent(
            timestamp=ts,
            scope=Scope.VENUE,
            scope_id=str(venue_id),
            rule_id=Rules.VENUE_DISCONNECT,
            severity=Severity.WARN,
            decision=Decision.KILL,
            reason=f"venue {venue_id} disconnected",
        ))

    def on_venue_reconnect(self, venue_id: int, ts: int) -> None:
        """Venue reconnect."""
        _require("venue_id", venue_id, 0, _U16_MAX)
        _require("ts", ts, _I64_MIN, _I64_MAX)
        self._venues_down[venue_id] = False
        self._emit(RiskEvent(
            timestamp=ts,
            scope=Scope.VENUE,
            scope_id=str(venue_id),
            rule_id=Rules.VENUE_RECONNECT,
            severity=Severity.INFO,
            decision=Decision.ALLOW,
            reason=f"venue {venue_id} reconnected",
        ))

    def engage_kill(self, scope: Scope, scope_id: str, ts: int, reason: str) -> None:
        """Manually engage a kill switch."""
        _require("ts", ts, _I64_MIN, _I64_MAX)
        self._set_kill(scope, scope_id, True)
        self._emit(RiskEvent(
            timestamp=ts,
            scope=scope,
            scope_id=scope_id,
            rule_id=Rules.KILL_SWITCH_ENGAGED,
            severity=Severity.BREACH,
            decision=Decision.KILL,
            reason=reason,
        ))

    def clear_kill(self, scope: Scope, scope_id: str, ts: int, reason: str) -> None:
        """Clear a kill switch (the switch only — see the re-arm precedence)."""
        _require("ts", ts, _I64_MIN, _I64_MAX)
        self._set_kill(scope, scope_id, False)
        self._emit(RiskEvent(
            timestamp=ts,
            scope=scope,
            scope_id=scope_id,
            rule_id=Rules.KILL_SWITCH_CLEARED,
            severity=Severity.INFO,
            decision=Decision.ALLOW,
            reason=reason,
        ))

    def override_loss_limit(
        self, scope: Scope, scope_id: str, new_limit: float, ts: int, approver: str
    ) -> None:
        """Raise (or lower) the effective daily loss limit of the GLOBAL or
        a STRATEGY scope with written approval. Audited; never clears a
        latched kill switch. ``ValueError`` on a non-positive/non-finite
        limit or an unsupported scope (nothing changes)."""
        _require("ts", ts, _I64_MIN, _I64_MAX)
        if isinstance(new_limit, bool) or not isinstance(new_limit, (int, float)) \
                or not (math.isfinite(new_limit) and new_limit > 0.0):
            raise ValueError(f"loss limit override must be finite and > 0, got {new_limit!r}")
        new_limit = float(new_limit)
        limits = self._limits
        if limits is None:
            raise ValueError("engine is fail-closed (no limits)")
        if scope == Scope.GLOBAL:
            old = self._loss_override_global
            if old is None:
                old = limits.max_daily_loss
            self._loss_override_global = new_limit
        elif scope == Scope.STRATEGY:
            old = self._loss_override_strategy.get(scope_id, limits.strategy_max_daily_loss)
            self._loss_override_strategy[scope_id] = new_limit
        else:
            raise ValueError("loss limits exist at GLOBAL and STRATEGY scope only")
        self._emit(RiskEvent(
            timestamp=ts,
            scope=scope,
            scope_id=scope_id,
            rule_id=Rules.LOSS_LIMIT_OVERRIDE,
            severity=Severity.WARN,
            decision=Decision.ALLOW,
            reason=(
                f"daily loss limit {fmt_fixed(old, 2)} -> {fmt_fixed(new_limit, 2)} "
                f"approved by {approver}"
            ),
        ))

    def roll_session(self, ts: int, reason: str) -> None:
        """Session roll: realized P&L zeroed, every marked lot re-based to
        its mark (unrealized restarts at 0; unmarked lots keep their cost),
        loss-limit overrides cleared. Kill switches, positions, open
        orders and seen order ids are untouched. Audited."""
        _require("ts", ts, _I64_MIN, _I64_MAX)
        self._realized.clear()
        for (_, iid), lot in sorted(self._lots.items()):
            mark = self._mark_price(iid)
            if mark is not None:
                lot.avg_price = mark
        self._loss_override_global = None
        self._loss_override_strategy.clear()
        self._refresh_pnl_gauges()
        self._emit(RiskEvent(
            timestamp=ts,
            scope=Scope.GLOBAL,
            scope_id="",
            rule_id=Rules.SESSION_ROLLED,
            severity=Severity.INFO,
            decision=Decision.ALLOW,
            reason=reason,
        ))

    def _set_kill(self, scope: Scope, scope_id: str, engaged: bool) -> None:
        if not isinstance(scope, Scope):
            raise ValueError(f"scope must be a Scope, got {scope!r}")
        if not isinstance(scope_id, str):
            raise ValueError(f"scope_id must be a str, got {scope_id!r}")
        if scope == Scope.GLOBAL:
            self._kill_global = engaged
            self.metrics.set_gauge("risk_kill_switch_engaged", 1.0 if engaged else 0.0)
        elif scope == Scope.STRATEGY:
            self._kill_strategies[scope_id] = engaged
        elif scope == Scope.INSTRUMENT:
            iid = _parse_uint(scope_id, _U32_MAX)
            if iid is not None:
                self._kill_instruments[iid] = engaged
        else:
            vid = _parse_uint(scope_id, _U16_MAX)
            if vid is not None:
                self._kill_venues[vid] = engaged

    def on_order_done(self, order_id: int) -> None:
        """A terminal order state (cancel / full fill / reject / expiry
        downstream): stop tracking it as open. The OMS MUST call this for
        every terminal execution report."""
        _require("order_id", order_id, 0, _U64_MAX)
        self._open.pop(order_id, None)

    def on_fill(self, fill: Fill) -> bool:
        """Apply one fill: positions, realized PnL (average-cost, pinned),
        open order reduction, then loss-limit evaluation (strategy first,
        then global; each engages its kill switch at most once per latch).
        Returns ``False`` (and audits ``MALFORMED_FILL``) when the fill is
        invalid or unpriceable — nothing is applied."""
        if not isinstance(fill, Fill):
            raise ValueError("fill must be a Fill")
        if fill.qty <= 0:
            why: Optional[str] = f"qty must be > 0: {fill.qty}"
        elif fill.side > 1:
            why = f"side must be 0 or 1: {fill.side}"
        elif fill.price_ticks <= 0:
            why = f"price_ticks must be > 0: {fill.price_ticks}"
        elif fill.instrument_id not in self._instruments:
            why = f"no reference data for instrument {fill.instrument_id}"
        else:
            why = None
        if why is not None:
            self.metrics.inc("risk_malformed_fills_total")
            self._emit(RiskEvent(
                timestamp=fill.ts,
                scope=Scope.STRATEGY,
                scope_id=fill.strategy_id,
                rule_id=Rules.MALFORMED_FILL,
                severity=Severity.WARN,
                decision=Decision.REJECT,
                reason=f"fill for order {fill.order_id} rejected: {why}",
            ))
            return False
        ins = self._instruments[fill.instrument_id]
        ccy = ins.quote_ccy
        unit = ins.qty_unit
        price = float(fill.price_ticks) * ins.tick_size
        key = (fill.strategy_id, fill.instrument_id)
        lot = self._lots.get(key)
        if lot is None:
            lot = _Lot()
            self._lots[key] = lot
        realized = 0.0
        if fill.side == 0:
            # buy
            if lot.pos >= 0:
                new_pos = _i64(lot.pos + fill.qty)
                lot.avg_price = (
                    lot.avg_price * float(lot.pos) + price * float(fill.qty)
                ) / float(new_pos)
                lot.pos = new_pos
            else:
                closed = min(fill.qty, _i64(-lot.pos))
                realized += (lot.avg_price - price) * float(closed)
                lot.pos = _i64(lot.pos + fill.qty)
                if lot.pos > 0:
                    lot.avg_price = price
        else:
            # sell
            if lot.pos <= 0:
                new_short = _i64(_i64(-lot.pos) + fill.qty)
                lot.avg_price = (
                    lot.avg_price * float(-lot.pos) + price * float(fill.qty)
                ) / float(new_short)
                lot.pos = _i64(lot.pos - fill.qty)
            else:
                closed = min(fill.qty, lot.pos)
                realized += (price - lot.avg_price) * float(closed)
                lot.pos = _i64(lot.pos - fill.qty)
                if lot.pos < 0:
                    lot.avg_price = price
        realized *= unit
        signed = fill.qty if fill.side == 0 else -fill.qty
        self._positions[fill.instrument_id] = _i64(
            self._positions.get(fill.instrument_id, 0) + signed
        )
        rkey = (fill.strategy_id, ccy)
        self._realized[rkey] = self._realized.get(rkey, 0.0) + realized
        # open order reduction
        if fill.order_id != 0:
            r = self._open.get(fill.order_id)
            if r is not None:
                r.qty = _i64(r.qty - fill.qty)
                if r.qty <= 0:
                    del self._open[fill.order_id]
        self._evaluate_loss_limits(fill.ts, [fill.strategy_id])
        return True

    # ------------------------------------------------------------- money

    def _fx_rate(self, ccy: str) -> Optional[Tuple[float, int]]:
        """Quote-currency -> reporting-currency rate and its mark time."""
        limits = self._limits
        if limits is None:
            return None
        if ccy == limits.reporting_ccy:
            return 1.0, _I64_MAX
        conv = limits.fx_conversion.get(ccy)
        if conv is None:
            return None
        md = self._market.get(conv.instrument_id)
        if md is None:
            return None
        if md.bid_ticks <= 0 or md.ask_ticks <= 0:
            return None
        pair = self._instruments.get(conv.instrument_id)
        if pair is None:
            return None
        mid = float(_i64(md.bid_ticks + md.ask_ticks)) * pair.tick_size / 2.0
        if mid <= 0.0:
            return None
        return (1.0 / mid if conv.invert else mid), md.ts

    def _mark_price(self, instrument_id: int) -> Optional[float]:
        """Last consolidated mid as a real price (quote ccy), if two-sided."""
        md = self._market.get(instrument_id)
        if md is None:
            return None
        if md.bid_ticks <= 0 or md.ask_ticks <= 0:
            return None
        ins = self._instruments.get(instrument_id)
        if ins is None:
            return None
        return float(_i64(md.bid_ticks + md.ask_ticks)) * ins.tick_size / 2.0

    def _daily_pnl(self, sid: Optional[str]) -> Optional[float]:
        """Realized + unrealized of every marked lot in the reporting
        currency, for one strategy (``sid``) or the whole firm (``None``);
        ``None`` when a needed conversion rate is missing."""
        total = 0.0
        for (s, ccy), pnl in sorted(self._realized.items()):
            if sid is not None and s != sid:
                continue
            rate = self._fx_rate(ccy)
            if rate is None:
                return None
            total += pnl * rate[0]
        for (s, iid), lot in sorted(self._lots.items()):
            if (sid is not None and s != sid) or lot.pos == 0:
                continue
            mark = self._mark_price(iid)
            if mark is None:
                continue  # unmarked: undeterminable, contributes nothing
            ins = self._instruments[iid]
            rate = self._fx_rate(ins.quote_ccy)
            if rate is None:
                return None
            total += float(lot.pos) * (mark - lot.avg_price) * ins.qty_unit * rate[0]
        return total

    def strategy_daily_pnl(self, sid: str) -> Optional[float]:
        """Daily P&L of one strategy in the reporting currency: realized +
        unrealized of every marked lot. ``None`` when a needed conversion
        rate is missing (undeterminable)."""
        return self._daily_pnl(sid)

    def global_daily_pnl(self) -> Optional[float]:
        """Firm-wide daily P&L in the reporting currency (``None`` when a
        conversion rate is missing)."""
        return self._daily_pnl(None)

    def realized_pnl(self) -> float:
        """Firm-wide realized P&L in the reporting currency (missing rates
        contribute 0 — a gauge, not a control)."""
        return self._realized_sum(None)

    def strategy_pnl(self, sid: str) -> float:
        """One strategy's realized P&L in the reporting currency (missing
        rates contribute 0)."""
        return self._realized_sum(sid)

    def _realized_sum(self, sid: Optional[str]) -> float:
        # Rust ``Iterator::sum::<f64>()`` folds from -0.0 (its neutral
        # element since Rust 1.83), so an empty bucket set sums to -0.0.
        total = -0.0
        for (s, ccy), pnl in sorted(self._realized.items()):
            if sid is not None and s != sid:
                continue
            rate = self._fx_rate(ccy)
            total += pnl * (rate[0] if rate is not None else 0.0)
        return total

    def unrealized_pnl(self) -> float:
        """Firm-wide unrealized P&L in the reporting currency (gauge)."""
        daily = self.global_daily_pnl()
        return (daily if daily is not None else 0.0) - self.realized_pnl()

    def _effective_strategy_loss(self, limits: RiskLimits, sid: str) -> float:
        return self._loss_override_strategy.get(sid, limits.strategy_max_daily_loss)

    def _effective_global_loss(self, limits: RiskLimits) -> float:
        if self._loss_override_global is None:
            return limits.max_daily_loss
        return self._loss_override_global

    def _refresh_pnl_gauges(self) -> None:
        realized = self.realized_pnl()
        daily = self.global_daily_pnl()
        if daily is None:
            daily = realized
        self.metrics.set_gauge("risk_realized_pnl", realized)
        self.metrics.set_gauge("risk_unrealized_pnl", daily - realized)
        self.metrics.set_gauge("risk_daily_pnl", daily)

    def _evaluate_loss_limits(self, ts: int, strategies: Sequence[str]) -> None:
        """Loss-limit evaluation (strategies given first, in order, then
        global). A determinate breach latches the kill switch once."""
        self._refresh_pnl_gauges()
        limits = self._limits
        if limits is None:
            return
        for sid in strategies:
            if self._strategy_killed(sid):
                continue
            pnl = self.strategy_daily_pnl(sid)
            if pnl is None:
                continue
            limit = self._effective_strategy_loss(limits, sid)
            if pnl <= -limit:
                self._kill_strategies[sid] = True
                self._emit(RiskEvent(
                    timestamp=ts,
                    scope=Scope.STRATEGY,
                    scope_id=sid,
                    rule_id=Rules.STRATEGY_LOSS,
                    severity=Severity.BREACH,
                    decision=Decision.KILL,
                    reason=(
                        f"strategy daily pnl {fmt_fixed(pnl, 2)} breaches loss limit "
                        f"{fmt_fixed(limit, 2)}"
                    ),
                ))
        if not self._kill_global:
            pnl = self.global_daily_pnl()
            if pnl is not None:
                limit = self._effective_global_loss(limits)
                if pnl <= -limit:
                    self._set_kill(Scope.GLOBAL, "", True)
                    self._emit(RiskEvent(
                        timestamp=ts,
                        scope=Scope.GLOBAL,
                        scope_id="",
                        rule_id=Rules.DAILY_LOSS,
                        severity=Severity.BREACH,
                        decision=Decision.KILL,
                        reason=(
                            f"global daily pnl {fmt_fixed(pnl, 2)} breaches daily loss "
                            f"limit {fmt_fixed(limit, 2)}"
                        ),
                    ))

    # --------------------------------------------------------- state reads

    def _strategy_killed(self, sid: str) -> bool:
        return self._kill_strategies.get(sid, False)

    def kill_switch_engaged(self) -> bool:
        """True when the global kill switch is engaged."""
        return self._kill_global

    def position(self, instrument_id: int) -> int:
        """Aggregate position of an instrument."""
        return self._positions.get(instrument_id, 0)

    def open_order_count(self) -> int:
        """Number of open (allowed, not yet terminal) orders tracked."""
        return len(self._open)

    def audit(self) -> Tuple[RiskEvent, ...]:
        """The audit log so far (a copy)."""
        return tuple(self._audit)

    def audit_len(self) -> int:
        """Number of audit records emitted so far."""
        return len(self._audit)

    def audit_jsonl(self) -> str:
        """Full audit log as JSONL (one RiskEvent per line, trailing newline)."""
        return "".join(ev.to_json_line() + "\n" for ev in self._audit)

    def _emit(self, ev: RiskEvent) -> None:
        self.metrics.inc("risk_events_total")
        self._audit.append(ev)

    # ------------------------------------------------------ pre-trade path

    def check_order(self, order: OrderRequest) -> RiskDecision:
        """Run the pinned pre-trade check sequence for one order. Emits the
        decision as a RiskEvent and returns it."""
        if not isinstance(order, OrderRequest):
            raise ValueError("order must be an OrderRequest")
        outcome = self._evaluate(order)
        scope, scope_id = self._decision_scope(order, outcome.rule_id)
        self.metrics.inc("risk_decisions_total")
        if outcome.allowed():
            self.metrics.inc("risk_allowed_total")
        else:
            self.metrics.inc("risk_rejected_total")
        self._emit(RiskEvent(
            timestamp=order.timestamp,
            scope=scope,
            scope_id=scope_id,
            rule_id=outcome.rule_id,
            severity=outcome.severity,
            decision=outcome.decision,
            reason=outcome.reason,
        ))
        # track every allowed order as open (self-match / projections)
        if outcome.allowed():
            self._open[order.order_id] = _OpenOrder(
                order.instrument_id, order.side, self._tracked_price(order), order.qty
            )
        return outcome

    def _tracked_price(self, order: OrderRequest) -> int:
        """Price an open order is tracked at: its limit price, the pegged
        same-side touch for PEG, 0 (unpriced) otherwise."""
        if order.price_ticks > 0:
            return order.price_ticks
        if order.order_type == OrderType.PEG:
            md = self._market.get(order.instrument_id)
            if md is not None:
                return md.bid_ticks if order.side == 0 else md.ask_ticks
        return 0

    @staticmethod
    def _decision_scope(order: OrderRequest, rule_id: str) -> Tuple[Scope, str]:
        if rule_id in _GLOBAL_SCOPE_RULES:
            return Scope.GLOBAL, ""
        if rule_id in _STRATEGY_SCOPE_RULES:
            return Scope.STRATEGY, order.strategy_id
        if rule_id in _VENUE_SCOPE_RULES:
            return Scope.VENUE, str(order.venue_id)
        return Scope.INSTRUMENT, str(order.instrument_id)

    @staticmethod
    def _reject(rule_id: str, severity: Severity, reason: str) -> RiskDecision:
        return RiskDecision(Decision.REJECT, rule_id, severity, reason)

    def _pretrade_rate(self, limits: RiskLimits, ccy: str, ts: int) -> Tuple[Optional[float], str]:
        """Pre-trade conversion rate: present and fresh (age within the
        stale timeout) as ``(rate, "")``, else ``(None, reason)``."""
        found = self._fx_rate(ccy)
        if found is None:
            return None, f"no conversion rate for {ccy} -> {limits.reporting_ccy}"
        rate, mark_ts = found
        if mark_ts != _I64_MAX and limits.stale_book_reject:
            age = _i64(ts - mark_ts)
            if age > limits.stale_feed_timeout_ns:
                return None, (
                    f"conversion rate {ccy} -> {limits.reporting_ccy} age {age}ns "
                    f"exceeds {limits.stale_feed_timeout_ns}ns"
                )
        return rate, ""

    def _evaluate(self, order: OrderRequest) -> RiskDecision:
        reject = self._reject
        breach, warn = Severity.BREACH, Severity.WARN
        # 0. fail-closed configuration / bootstrap
        limits = self._limits
        if limits is None:
            return reject(Rules.CONFIG_MISSING, breach, f"fail-closed: {self._config_error}")
        if not self._bootstrapped:
            return reject(Rules.NOT_BOOTSTRAPPED, breach,
                          "positions not bootstrapped (fail-closed)")
        # 1-4. kill switches, global > strategy > instrument > venue
        if self._kill_global:
            return reject(Rules.KILL_GLOBAL, breach, "global kill switch engaged")
        if self._strategy_killed(order.strategy_id):
            return reject(Rules.KILL_STRATEGY, breach,
                          f"strategy {order.strategy_id} kill switch engaged")
        if self._kill_instruments.get(order.instrument_id, False):
            return reject(Rules.KILL_INSTRUMENT, breach,
                          f"instrument {order.instrument_id} kill switch engaged")
        if order.venue_id != 0 and self._kill_venues.get(order.venue_id, False):
            return reject(Rules.KILL_VENUE, breach,
                          f"venue {order.venue_id} kill switch engaged")
        # 5. schema-level validation
        malformed = order_validation_error(order)
        if malformed is not None:
            return reject(Rules.MALFORMED_ORDER, warn, malformed)
        # 6. reference data
        ins = self._instruments.get(order.instrument_id)
        if ins is None:
            return reject(Rules.UNKNOWN_INSTRUMENT, warn,
                          f"no reference data for instrument {order.instrument_id}")
        tick = ins.tick_size
        # 7. duplicate order id
        prev_ts = self._seen_orders.get(order.order_id)
        if prev_ts is not None:
            window = limits.duplicate_order_window_ns
            if window == 0 or _i64(order.timestamp - prev_ts) <= window:
                return reject(Rules.DUPLICATE_ORDER_ID, warn,
                              f"order_id {order.order_id} already used at ts {prev_ts}")
        if limits.duplicate_order_window_ns > 0:
            # prune ids that fell out of the window (bounded growth)
            cutoff = _i64(order.timestamp - limits.duplicate_order_window_ns)
            self._seen_orders = {
                oid: ts for oid, ts in self._seen_orders.items() if ts >= cutoff
            }
        self._seen_orders[order.order_id] = order.timestamp
        # 8. venue connectivity
        if order.venue_id != 0 and self._venues_down.get(order.venue_id, False):
            return reject(Rules.VENUE_DISCONNECTED, warn,
                          f"venue {order.venue_id} is disconnected")
        # 9-10. market-data gate
        md = self._market.get(order.instrument_id)
        if md is not None and md.gated:
            return reject(Rules.SEQUENCE_GAP, warn,
                          f"instrument {order.instrument_id} feed has an unrecovered gap")
        if md is not None and md.bid_ticks > 0 and md.ask_ticks > 0:
            age = _i64(order.timestamp - md.ts)
            if limits.stale_book_reject and age > limits.stale_feed_timeout_ns:
                return reject(Rules.STALE_PRICE, warn,
                              f"reference price age {age}ns exceeds "
                              f"{limits.stale_feed_timeout_ns}ns")
            mid = float(_i64(md.bid_ticks + md.ask_ticks)) * tick / 2.0
        else:
            return reject(Rules.STALE_PRICE, warn,
                          f"no reference price for instrument {order.instrument_id}")
        # 11. fat-finger quantity
        if order.qty > limits.max_order_qty:
            return reject(Rules.FAT_FINGER_QTY, warn,
                          f"qty {order.qty} exceeds max_order_qty {limits.max_order_qty}")
        # 12. conversion rate to the reporting currency
        fx, why = self._pretrade_rate(limits, ins.quote_ccy, order.timestamp)
        if fx is None:
            return reject(Rules.FX_RATE_MISSING, warn, why)
        # 13. fat-finger notional (priced orders use the limit price,
        # unpriced the mid); notional in the reporting currency
        ref_price = float(order.price_ticks) * tick if order.price_ticks > 0 else mid
        order_notional = float(order.qty) * ins.qty_unit * ref_price * fx
        if order_notional > limits.max_order_notional:
            return reject(Rules.FAT_FINGER_NOTIONAL, warn,
                          f"notional {fmt_fixed(order_notional, 2)} {limits.reporting_ccy} "
                          f"exceeds max_order_notional "
                          f"{fmt_fixed(limits.max_order_notional, 2)}")
        # 14. price band (priced orders only)
        if order.price_ticks > 0:
            dev_bps = abs(float(order.price_ticks) * tick - mid) / mid * 1e4
            if dev_bps > limits.price_band_bps:
                return reject(Rules.PRICE_BAND, warn,
                              f"price deviates {fmt_fixed(dev_bps, 1)}bps from mid, "
                              f"band {fmt_fixed(limits.price_band_bps, 1)}bps")
        # 15. order-rate throttle (event-time token bucket per strategy)
        bucket = self._buckets.get(order.strategy_id)
        if bucket is None:
            bucket = _Bucket(limits.order_rate_burst, order.timestamp, True)
            self._buckets[order.strategy_id] = bucket
        if not bucket.primed:
            bucket.tokens = limits.order_rate_burst
            bucket.primed = True
            bucket.last_ts = order.timestamp
        elapsed = max(_i64(order.timestamp - bucket.last_ts), 0)
        bucket.tokens = min(
            bucket.tokens + float(elapsed) * limits.max_order_rate_per_sec / _NS_PER_SEC,
            limits.order_rate_burst,
        )
        bucket.last_ts = max(bucket.last_ts, order.timestamp)
        if bucket.tokens < 1.0:
            return reject(Rules.RATE_THROTTLE, warn,
                          f"strategy {order.strategy_id} exceeded "
                          f"{fmt_fixed(limits.max_order_rate_per_sec, 2)} orders/s "
                          f"(burst {fmt_fixed(limits.order_rate_burst, 2)})")
        bucket.tokens -= 1.0
        # 16. self-match prevention (any venue; PEG at its pegged touch)
        my_price = self._tracked_price(order)
        for oid in sorted(self._open):
            r = self._open[oid]
            if r.instrument_id != order.instrument_id or r.side == order.side:
                continue
            if my_price > 0 and r.price_ticks > 0:
                crosses = my_price >= r.price_ticks if order.side == 0 \
                    else my_price <= r.price_ticks
            else:
                crosses = True  # unpriced on either side: conservative
            if crosses:
                return reject(Rules.SELF_MATCH, warn,
                              f"would cross own open order {oid} at {r.price_ticks}")
        # 17. position limit (worst-case projection incl. open orders)
        pos = self.position(order.instrument_id)
        open_same = 0
        for oid in sorted(self._open):
            r = self._open[oid]
            if r.instrument_id == order.instrument_id and r.side == order.side:
                open_same = _i64(open_same + r.qty)
        if order.side == 0:
            projected = _i64(_i64(pos + open_same) + order.qty)
        else:
            projected = _i64(_i64(pos - open_same) - order.qty)
        if _i64(abs(projected)) > limits.max_position_qty:
            return reject(Rules.POSITION_LIMIT, warn,
                          f"projected position {projected} exceeds max_position_qty "
                          f"{limits.max_position_qty}")
        # 18. per-instrument notional (projection marked at the mid)
        projected_notional = float(abs(projected)) * ins.qty_unit * mid * fx
        if projected_notional > limits.max_instrument_notional:
            return reject(Rules.INSTRUMENT_NOTIONAL, warn,
                          f"projected notional {fmt_fixed(projected_notional, 2)} exceeds "
                          f"max_instrument_notional "
                          f"{fmt_fixed(limits.max_instrument_notional, 2)}")
        # 19-20. gross / net notional (filled positions + every open order
        # + this order; fail-closed on unmarked or unconvertible positions)
        gross = 0.0
        net = 0.0
        for iid in sorted(self._positions):
            p = self._positions[iid]
            if p == 0:
                continue
            mark = self._mark_price(iid)
            if mark is None:
                return reject(Rules.GROSS_NOTIONAL, warn,
                              f"position in instrument {iid} has no mark price (fail-closed)")
            pins = self._instruments[iid]
            rate = self._fx_rate(pins.quote_ccy)
            if rate is None:
                return reject(Rules.GROSS_NOTIONAL, warn,
                              f"position in instrument {iid} has no {pins.quote_ccy} "
                              f"conversion rate (fail-closed)")
            v = float(p) * pins.qty_unit * mark * rate[0]
            gross += abs(v)
            net += v
        for oid in sorted(self._open):
            r = self._open[oid]
            oins = self._instruments.get(r.instrument_id)
            if oins is None:
                continue
            if r.price_ticks > 0:
                price = float(r.price_ticks) * oins.tick_size
            else:
                marked = self._mark_price(r.instrument_id)
                if marked is None:
                    continue  # unpriced and unmarked: cannot value
                price = marked
            rate = self._fx_rate(oins.quote_ccy)
            if rate is None:
                return reject(Rules.GROSS_NOTIONAL, warn,
                              f"open order in instrument {r.instrument_id} has no "
                              f"{oins.quote_ccy} conversion rate (fail-closed)")
            v = float(r.qty) * oins.qty_unit * price * rate[0]
            gross += v
            net += v if r.side == 0 else -v
        gross += order_notional
        if gross > limits.max_gross_notional:
            return reject(Rules.GROSS_NOTIONAL, warn,
                          f"projected gross notional {fmt_fixed(gross, 2)} exceeds "
                          f"max_gross_notional {fmt_fixed(limits.max_gross_notional, 2)}")
        net += order_notional if order.side == 0 else -order_notional
        if abs(net) > limits.max_net_notional:
            return reject(Rules.NET_NOTIONAL, warn,
                          f"projected net notional {fmt_fixed(net, 2)} exceeds "
                          f"max_net_notional {fmt_fixed(limits.max_net_notional, 2)}")
        # 21-22. loss limits on daily P&L (belt-and-braces after a cleared
        # latch; undeterminable P&L rejects fail-closed)
        global_pnl = self.global_daily_pnl()
        if global_pnl is None:
            return reject(Rules.FX_RATE_MISSING, warn,
                          "global daily pnl undeterminable: conversion rate missing")
        global_limit = self._effective_global_loss(limits)
        if global_pnl <= -global_limit:
            return reject(Rules.DAILY_LOSS, breach,
                          f"global daily pnl {fmt_fixed(global_pnl, 2)} at daily loss "
                          f"limit {fmt_fixed(global_limit, 2)}")
        strat_pnl = self.strategy_daily_pnl(order.strategy_id)
        if strat_pnl is None:
            return reject(Rules.FX_RATE_MISSING, warn,
                          "strategy daily pnl undeterminable: conversion rate missing")
        strat_limit = self._effective_strategy_loss(limits, order.strategy_id)
        if strat_pnl <= -strat_limit:
            return reject(Rules.STRATEGY_LOSS, breach,
                          f"strategy daily pnl {fmt_fixed(strat_pnl, 2)} at loss limit "
                          f"{fmt_fixed(strat_limit, 2)}")
        return RiskDecision(Decision.ALLOW, Rules.ALLOW, Severity.INFO, "")

    # ---------------------------------------------------- snapshot/restore

    def snapshot(self) -> Dict[str, Any]:
        """Serialise the full mutable state (positions, lots, realized P&L,
        kill/latch state, marks, open orders, throttle buckets, seen order
        ids, overrides, bootstrap flag) as a schema-versioned JSON-shaped
        dict (x-version 1). The audit log and metrics are NOT part of the
        snapshot. :meth:`snapshot_json` renders it byte-identically to the
        Rust ``serde_json`` output."""
        return {
            "x-version": SNAPSHOT_VERSION,
            "bootstrapped": self._bootstrapped,
            "kill_global": self._kill_global,
            "kill_strategies": {k: self._kill_strategies[k] for k in sorted(self._kill_strategies)},
            "kill_instruments": {str(k): self._kill_instruments[k]
                                 for k in sorted(self._kill_instruments)},
            "kill_venues": {str(k): self._kill_venues[k] for k in sorted(self._kill_venues)},
            "venues_down": {str(k): self._venues_down[k] for k in sorted(self._venues_down)},
            "market": {
                str(k): {
                    "bid_ticks": m.bid_ticks, "ask_ticks": m.ask_ticks, "ts": m.ts,
                    "gaps": m.gaps, "gated": m.gated,
                }
                for k, m in sorted(self._market.items())
            },
            "seen_orders": [[oid, self._seen_orders[oid]] for oid in sorted(self._seen_orders)],
            "buckets": {
                k: {"tokens": b.tokens, "last_ts": b.last_ts, "primed": b.primed}
                for k, b in sorted(self._buckets.items())
            },
            "open": {
                str(k): {
                    "instrument_id": o.instrument_id, "side": o.side,
                    "price_ticks": o.price_ticks, "qty": o.qty,
                }
                for k, o in sorted(self._open.items())
            },
            "positions": {str(k): self._positions[k] for k in sorted(self._positions)},
            "lots": [
                {"strategy_id": sid, "instrument_id": iid,
                 "pos": lot.pos, "avg_price": lot.avg_price}
                for (sid, iid), lot in sorted(self._lots.items())
            ],
            "realized": [
                {"strategy_id": sid, "ccy": ccy, "pnl": pnl}
                for (sid, ccy), pnl in sorted(self._realized.items())
            ],
            "loss_override_global": self._loss_override_global,
            "loss_override_strategy": {
                k: self._loss_override_strategy[k] for k in sorted(self._loss_override_strategy)
            },
        }

    def snapshot_json(self, pretty: bool = True) -> str:
        """:meth:`snapshot` rendered exactly as the Rust reference writes it
        (``serde_json::to_string_pretty`` by default, ``to_string`` when
        ``pretty=False``); no trailing newline."""
        return to_canonical_json(self.snapshot(), pretty=pretty)

    @classmethod
    def restore(
        cls,
        limits: RiskLimits,
        instruments: Mapping[int, InstrumentRef],
        snap: Any,
        ts: int,
    ) -> "RiskEngine":
        """Rebuild an engine from ``limits``, ``instruments`` and a
        :meth:`snapshot` document (strict: unknown version or a malformed
        field is a ``ValueError``, nothing is restored). Emits a
        ``STATE_RESTORED`` audit record stamped ``ts``."""
        _require("ts", ts, _I64_MIN, _I64_MAX)
        get = _SnapReader(snap)
        if get.u64_opt("x-version") != SNAPSHOT_VERSION:
            raise _bad("x-version")
        eng = cls(limits, instruments)
        eng._bootstrapped = get.bool("bootstrapped")
        eng._kill_global = get.bool("kill_global")
        eng.metrics.set_gauge("risk_kill_switch_engaged", 1.0 if eng._kill_global else 0.0)
        for k, v in get.obj("kill_strategies").items():
            eng._kill_strategies[k] = _as_bool(v, "kill_strategies")
        for k, v in get.obj("kill_instruments").items():
            iid = _key(k, _U32_MAX, "kill_instruments")
            eng._kill_instruments[iid] = _as_bool(v, "kill_instruments")
        for k, v in get.obj("kill_venues").items():
            vid = _key(k, _U16_MAX, "kill_venues")
            eng._kill_venues[vid] = _as_bool(v, "kill_venues")
        for k, v in get.obj("venues_down").items():
            vid = _key(k, _U16_MAX, "venues_down")
            eng._venues_down[vid] = _as_bool(v, "venues_down")
        for k, m in get.obj("market").items():
            iid = _key(k, _U32_MAX, "market")
            r = _SnapReader(m)
            eng._market[iid] = _MarketState(
                r.i64("bid_ticks", "market.bid_ticks"),
                r.i64("ask_ticks", "market.ask_ticks"),
                r.i64("ts", "market.ts"),
                r.u64("gaps", "market.gaps"),
                r.bool("gated", "market.gated"),
            )
        for pair in get.arr("seen_orders"):
            if not isinstance(pair, list) or len(pair) < 2:
                raise _bad("seen_orders")
            oid = _as_u64(pair[0], "seen_orders")
            eng._seen_orders[oid] = _as_i64(pair[1], "seen_orders")
        for k, b in get.obj("buckets").items():
            r = _SnapReader(b)
            eng._buckets[k] = _Bucket(
                r.f64("tokens", "buckets.tokens"),
                r.i64("last_ts", "buckets.last_ts"),
                r.bool("primed", "buckets.primed"),
            )
        for k, o in get.obj("open").items():
            r = _SnapReader(o)
            side = r.u64("side", "open.side")
            if side > 1:
                raise _bad("open.side")
            oid = _key(k, _U64_MAX, "open")
            iid = r.u64("instrument_id", "open.instrument_id")
            if iid > _U32_MAX:
                raise _bad("open.instrument_id")
            eng._open[oid] = _OpenOrder(
                iid, side, r.i64("price_ticks", "open.price_ticks"), r.i64("qty", "open.qty")
            )
        for k, v in get.obj("positions").items():
            iid = _key(k, _U32_MAX, "positions")
            eng._positions[iid] = _as_i64(v, "positions")
        for entry in get.arr("lots"):
            r = _SnapReader(entry)
            sid = r.str("strategy_id", "lots.strategy_id")
            iid = r.u64("instrument_id", "lots.instrument_id")
            if iid > _U32_MAX:
                raise _bad("lots.instrument_id")
            avg = r.f64("avg_price", "lots.avg_price")
            eng._lots[(sid, iid)] = _Lot(r.i64("pos", "lots.pos"), avg)
        for entry in get.arr("realized"):
            r = _SnapReader(entry)
            sid = r.str("strategy_id", "realized.strategy_id")
            ccy = r.str("ccy", "realized.ccy")
            eng._realized[(sid, ccy)] = r.f64("pnl", "realized.pnl")
        override = get.raw("loss_override_global")
        eng._loss_override_global = None if override is None \
            else _as_f64(override, "loss_override_global")
        for k, v in get.obj("loss_override_strategy").items():
            eng._loss_override_strategy[k] = _as_f64(v, "loss_override_strategy")
        eng._refresh_pnl_gauges()
        n_pos = sum(1 for p in eng._positions.values() if p != 0)
        eng._emit(RiskEvent(
            timestamp=ts,
            scope=Scope.GLOBAL,
            scope_id="",
            rule_id=Rules.STATE_RESTORED,
            severity=Severity.INFO,
            decision=Decision.ALLOW,
            reason=(
                f"restored snapshot v{SNAPSHOT_VERSION}: {n_pos} positions, "
                f"{len(eng._open)} open orders"
            ),
        ))
        return eng


# ----------------------------------------------------- snapshot accessors
# serde_json semantics: indexing a non-object / missing key yields Null,
# and every typed accessor on the wrong JSON type is an error.


def _bad(what: str) -> ValueError:
    return ValueError(f"risk snapshot: bad {what}")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _as_bool(v: Any, what: str) -> bool:
    if not isinstance(v, bool):
        raise _bad(what)
    return v


def _as_i64(v: Any, what: str) -> int:
    if not _is_int(v) or not (_I64_MIN <= v <= _I64_MAX):
        raise _bad(what)
    return v


def _as_u64(v: Any, what: str) -> int:
    if not _is_int(v) or not (0 <= v <= _U64_MAX):
        raise _bad(what)
    return v


def _as_f64(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise _bad(what)
    return float(v)


def _as_str(v: Any, what: str) -> str:
    if not isinstance(v, str):
        raise _bad(what)
    return v


def _key(k: str, hi: int, what: str) -> int:
    parsed = _parse_uint(k, hi)
    if parsed is None:
        raise _bad(what)
    return parsed


class _SnapReader:
    """Typed field access on one JSON object of the snapshot."""

    __slots__ = ("_doc",)

    def __init__(self, doc: Any) -> None:
        self._doc = doc

    def raw(self, key: str) -> Any:
        return self._doc.get(key) if isinstance(self._doc, dict) else None

    def u64_opt(self, key: str) -> Optional[int]:
        v = self.raw(key)
        return v if _is_int(v) and 0 <= v <= _U64_MAX else None

    def bool(self, key: str, what: Optional[str] = None) -> bool:
        return _as_bool(self.raw(key), what or key)

    def i64(self, key: str, what: str) -> int:
        return _as_i64(self.raw(key), what)

    def u64(self, key: str, what: str) -> int:
        return _as_u64(self.raw(key), what)

    def f64(self, key: str, what: str) -> float:
        return _as_f64(self.raw(key), what)

    def str(self, key: str, what: str) -> str:
        return _as_str(self.raw(key), what)

    def obj(self, key: str) -> Dict[str, Any]:
        v = self.raw(key)
        if not isinstance(v, dict):
            raise _bad(key)
        return {k: v[k] for k in sorted(v)}

    def arr(self, key: str) -> List[Any]:
        v = self.raw(key)
        if not isinstance(v, list):
            raise _bad(key)
        return v
