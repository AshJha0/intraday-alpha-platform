"""EN2: the research cohort ledger, the executable ledger and the cash identity (v2.0 U0 stage 5).

Written once, family-agnostic (plan V2-C1): every structure is keyed by
``(family_id, horizon, cohort_id)``; H1 (E0 sections 7, 9, 10) is the first
client and its rules are the ones implemented, with the intraday-only ones
(flat book at the close) switched per family.

Quantity states (E0 section 7, C5), per name and decision time and per cohort::

    intended   the target change the research ledger asked for
    submitted  after the no-trade band (withheld = intended - submitted; mandatory orders are
               always submitted in full) and after netting across cohorts and families
    filled     the simulated external fill of the aggregate net order
    allocated  filled quantity (and internally crossed quantity) assigned to cohorts
    outstanding submitted - allocated for MANDATORY orders: carried to the next decision time
    cancelled  submitted - allocated for discretionary orders

Netting: all cohort intents in one name at one decision time produce one net
order.  Opposite intents are **crossed internally** at the decision mid (no
external trade, no fee, no cost) and the external fill covers the rest; the
recorded net order is the aggregate, so costs are charged once, on net
executed quantity.

Allocation rule (E0 section 7): on each side, mandatory orders first, in
expiry order (earliest scheduled expiry first, then the lower cohort key), each
up to its intent; then entering cohorts pro rata to their intended quantities,
integer shares by largest remainder, ties to the lower cohort id (then family
id, then horizon: the cross-family tie is a stage-1 default, defect register
D-30).  The rule is deterministic and every allocation is logged.

States: ``pending -> live -> closed``; ``live -> closing -> closed`` when an
expiry exit is not fully filled (the residual stays allocated to the cohort,
its mandatory exit is re-sent at every decision time, and all P&L after the
scheduled expiry is **execution residual**); ``pending -> unfilled`` when an
entry fills nothing; ``refused`` when construction skipped the cohort.  No
cohort is ``live`` after its expiry.

Primary identity (E0 section 10, E0-5.2), per session::

    NetPnL = Cash_close + sum_i q_i,close * V_i,close - Equity_open
    Equity_open = Cash_open + sum_i q_i,open * V_i,open,   V_open = previous session's V_close

and the waterfall (E0 section 11) that E2's independent reconstruction must
reproduce term by term, exactly::

    midpoint-reference P&L + latency drift - half-spread - impact - fees + execution residual
    - financing = NetPnL

where every fill is ``P = M_arrival + side * (half_spread + impact)`` (C6),
latency drift is ``-q * (M_arrival - M_decision)`` summed over fills, the
midpoint-reference P&L of a cohort is the change in its position valued at
decision mids (entries at their decision mid, marks at the scheduled expiry and
at session closes), execution residual is the same quantity after the
scheduled expiry, and financing is charged at the open on inventory carried
over a session boundary (zero when the close target is met).  A residual at
the close is carried into the next session's ``Equity_open`` at its registered
closeout valuation with a ``closeout_failure`` flag naming the cause, so a loss
is charged once, in the session whose valuation moved.  Corporate actions on
carried inventory and valuation failures are recorded events.

Money is ``float`` (conventions section 1: research layers, 1e-9 tolerance);
quantities are ``int`` shares; prices are per share.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "CohortKey",
    "CohortRecord",
    "CohortState",
    "Decision",
    "FamilyConfig",
    "Fill",
    "Intent",
    "Ledger",
    "LedgerConfig",
    "LedgerError",
    "NetOrder",
    "SessionResult",
    "TradeDecision",
    "Valuation",
    "distribute",
]

CohortKey = tuple[str, str, int]  # (family_id, horizon, cohort_id)
TOL = 1e-9


class LedgerError(ValueError):
    """An operation would break a conservation invariant or a rule of E0 sections 7-10."""


class CohortState(str, Enum):
    PENDING = "pending"
    LIVE = "live"
    CLOSING = "closing"
    CLOSED = "closed"
    UNFILLED = "unfilled"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class FamilyConfig:
    """Per-family switches.  ``flat_at_close``: every unit of inventory must be liquidated or flagged at the close."""

    family_id: str
    flat_at_close: bool = True


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    """Book-level parameters.  Rates are per session on the carried notional (0 = the E0 intraday rule)."""

    financing_rate_per_session: float = 0.0
    borrow_rate_per_session: float = 0.0
    gross_cap: float | None = None
    tol: float = TOL

    def __post_init__(self) -> None:
        for name in ("financing_rate_per_session", "borrow_rate_per_session"):
            v = getattr(self, name)
            if not math.isfinite(v) or v < 0:
                raise LedgerError(f"{name} must be a finite number >= 0")
        if self.gross_cap is not None and (
            not math.isfinite(self.gross_cap) or self.gross_cap <= 0
        ):
            raise LedgerError("gross_cap must be a positive finite notional or None")


@dataclass(frozen=True, slots=True)
class TradeDecision:
    """E0 section 7's per-proposal economic record (research ledger), optional per name."""

    signal: float
    mu_hat_bps: float
    expected_entry_cost_bps: float
    expected_exit_cost_bps: float
    risk_status: str = "pass"
    liquidity_status: str = "pass"
    reason: str = ""

    @property
    def cushion_bps(self) -> float:
        return self.mu_hat_bps - (self.expected_entry_cost_bps + self.expected_exit_cost_bps)


@dataclass(slots=True)
class CohortRecord:
    """The research cohort ledger's row for one cohort."""

    key: CohortKey
    formation_ts: int
    decision_ts: int
    expiry_ts: int
    intents: dict[str, int]
    forecasts: dict[str, float]
    state: CohortState = CohortState.PENDING
    decisions: dict[str, TradeDecision] = field(default_factory=dict)
    allocated: dict[str, int] = field(default_factory=dict)  # current position per name
    submitted: dict[str, int] = field(default_factory=dict)
    withheld: dict[str, int] = field(default_factory=dict)
    cancelled: dict[str, int] = field(default_factory=dict)
    outstanding: dict[str, int] = field(default_factory=dict)
    signal_pnl: float = 0.0
    execution_residual: float = 0.0
    attribution_closed_ts: int | None = None
    early_exit_reason: str = ""
    refusal_reason: str = ""
    closeout_failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def family_id(self) -> str:
        return self.key[0]

    @property
    def horizon(self) -> str:
        return self.key[1]

    @property
    def cohort_id(self) -> int:
        return self.key[2]

    def position(self, name: str) -> int:
        return self.allocated.get(name, 0)

    def gross_shares(self) -> int:
        return sum(abs(q) for q in self.allocated.values())


@dataclass(frozen=True, slots=True)
class Intent:
    key: CohortKey
    name: str
    qty: int
    mandatory: bool
    kind: str  # entry | exit | resend | liquidation


@dataclass(slots=True)
class NetOrder:
    """The aggregate order in one name at one decision time, with its quantity states."""

    name: str
    mid_decision: float
    intents: list[Intent]
    intended: int = 0
    mandatory: int = 0
    submitted: int = 0
    withheld: int = 0
    crossed: int = 0
    filled: int = 0
    outstanding: int = 0
    cancelled: int = 0
    fill: Fill | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """One simulated external fill under the E0 section 9 convention (C6)."""

    ts: int
    name: str
    qty: int
    price: float
    fee: float
    half_spread: float
    impact: float
    mid_decision: float
    mid_arrival: float
    column: str = "conservative"

    def __post_init__(self) -> None:
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty == 0:
            raise LedgerError("a fill has a non-zero int qty")
        for n in ("price", "fee", "half_spread", "impact", "mid_decision", "mid_arrival"):
            v = getattr(self, n)
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                raise LedgerError(f"fill {n} must be a finite number")
        if self.fee < 0 or self.half_spread < 0 or self.impact < 0:
            raise LedgerError("fee, half_spread and impact are >= 0")
        side = 1 if self.qty > 0 else -1
        expected = self.mid_arrival + side * (self.half_spread + self.impact)
        if abs(expected - self.price) > 1e-9:
            raise LedgerError(
                f"fill price {self.price} breaks the one fill-price convention: "
                f"M_arrival {self.mid_arrival} + side*(half_spread + impact) = {expected}"
            )

    @property
    def latency_drift(self) -> float:
        return -self.qty * (self.mid_arrival - self.mid_decision)

    @property
    def half_spread_cost(self) -> float:
        return abs(self.qty) * self.half_spread

    @property
    def impact_cost(self) -> float:
        return abs(self.qty) * self.impact

    @property
    def cash_flow(self) -> float:
        return -self.qty * self.price - self.fee


@dataclass(frozen=True, slots=True)
class Valuation:
    """The registered closeout valuation of a name at the close; ``cause`` names a closeout failure."""

    price: float
    cause: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.price, int | float)
            or not math.isfinite(self.price)
            or self.price <= 0
        ):
            raise LedgerError("a valuation needs a positive finite price")


@dataclass(slots=True)
class Decision:
    ts: int
    mids: dict[str, float]
    orders: dict[str, NetOrder]
    pre_close: bool = False
    settled: bool = False


@dataclass(slots=True)
class SessionResult:
    session_id: str
    equity_open: float
    cash_open: float
    cash_close: float
    marked_inventory_close: float
    net_pnl: float
    waterfall: dict[str, float]
    carried: dict[str, int]
    closeout_failures: list[dict[str, Any]]
    closing_cohorts: int
    closing_gross_shares: int
    quantity_states: dict[str, int]

    @property
    def identity_gap(self) -> float:
        """Waterfall total minus NetPnL: zero within tolerance by construction, re-checked by E2."""
        w = self.waterfall
        total = (
            w["midpoint_reference_pnl"]
            + w["latency_drift"]
            - w["half_spread"]
            - w["impact"]
            - w["fees"]
            + w["execution_residual"]
            - w["financing"]
        )
        return total - self.net_pnl


# --------------------------------------------------------------------------- allocation
def distribute(
    available: int, mandatory: Sequence[tuple[Any, int]], entering: Sequence[tuple[Any, int]]
) -> list[int]:
    """Split ``available`` (>= 0) shares over caps: mandatory first in sort order, then pro rata by largest remainder.

    ``mandatory`` and ``entering`` are ``(sort_key, cap)`` with ``cap >= 0``;
    the result lists one allocation per input, mandatory first, each ``<= cap``.
    Pro rata: floor(available * cap / total), then one share each to the
    largest fractional remainders, ties to the lower sort key.
    """
    if available < 0:
        raise LedgerError("distribute needs available >= 0")
    out_m = [0] * len(mandatory)
    for i in sorted(range(len(mandatory)), key=lambda i: mandatory[i][0]):
        take = min(available, mandatory[i][1])
        out_m[i] = take
        available -= take
    out_e = [0] * len(entering)
    total = sum(c for _, c in entering)
    if entering and total > 0 and available > 0:
        if available >= total:
            out_e = [c for _, c in entering]
        else:
            floors = [available * c // total for _, c in entering]
            rem = [(available * c) % total for _, c in entering]
            left = available - sum(floors)
            order = sorted(range(len(entering)), key=lambda i: (-rem[i], entering[i][0]))
            for i in order:
                if left == 0:
                    break
                if floors[i] < entering[i][1]:
                    floors[i] += 1
                    left -= 1
            out_e = floors
    return out_m + out_e


def _tie_key(key: CohortKey) -> tuple[int, str, str]:
    return (key[2], key[0], key[1])


# --------------------------------------------------------------------------- the ledger
class Ledger:
    """Research cohort ledger + executable ledger + session accounting (module docstring)."""

    def __init__(self, config: LedgerConfig | None = None) -> None:
        self.config = config or LedgerConfig()
        self.families: dict[str, FamilyConfig] = {}
        self.cohorts: dict[CohortKey, CohortRecord] = {}
        self.cash: float = 0.0
        self.positions: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []
        self.fills: list[Fill] = []
        self.sessions: list[SessionResult] = []
        self._basis: dict[
            tuple[CohortKey, str], float
        ] = {}  # position notional at the last reference mark
        self._last_valuation: dict[str, float] = {}
        self._session: str | None = None
        self._equity_open: float = 0.0
        self._cash_open: float = 0.0
        self._carried_open: dict[str, int] = {}
        self._decision: Decision | None = None
        self._pending_liquidation: dict[CohortKey, str] = {}
        self._wf: dict[str, float] = {}
        self._quantity_states: dict[str, int] = {}
        self._last_ts: int | None = None

    # ------------------------------------------------------------------ setup
    def register_family(self, cfg: FamilyConfig) -> None:
        if not isinstance(cfg, FamilyConfig) or not cfg.family_id:
            raise LedgerError("register_family takes a FamilyConfig with a family_id")
        prior = self.families.get(cfg.family_id)
        if prior is not None and prior != cfg:
            raise LedgerError(
                f"family {cfg.family_id!r} already registered with another configuration"
            )
        self.families[cfg.family_id] = cfg

    def deposit(self, cash: float) -> None:
        """Registered capital C enters as cash before the first session."""
        if self._session is not None:
            raise LedgerError("deposit before a session opens")
        if not math.isfinite(cash) or cash <= 0:
            raise LedgerError("deposit a positive finite amount")
        self.cash += float(cash)
        self._log("deposit", amount=float(cash))

    # ------------------------------------------------------------------ sessions
    def open_session(self, session_id: str) -> float:
        """Equity_open = Cash_open + sum q_open * V_open; charges financing on carried inventory. Returns Equity_open."""
        if self._session is not None:
            raise LedgerError(f"session {self._session} is still open")
        if self._decision is not None:
            raise LedgerError("an unsettled decision is open")
        self._session = session_id
        self._carried_open = {n: q for n, q in self.positions.items() if q != 0}
        for n in self._carried_open:
            if n not in self._last_valuation:
                raise LedgerError(
                    f"carried inventory in {n} has no closeout valuation from the previous session"
                )
        self._wf = {
            k: 0.0
            for k in (
                "midpoint_reference_pnl",
                "latency_drift",
                "half_spread",
                "impact",
                "fees",
                "execution_residual",
                "financing",
                "overnight_pnl",
            )
        }
        self._quantity_states = {
            k: 0
            for k in (
                "intended",
                "mandatory",
                "submitted",
                "withheld",
                "crossed",
                "filled",
                "outstanding",
                "cancelled",
            )
        }
        self._last_ts = None
        inventory = sum(q * self._last_valuation[n] for n, q in self._carried_open.items())
        self._cash_open = self.cash
        self._equity_open = self.cash + inventory
        self._log(
            "session_open",
            session=session_id,
            cash=self.cash,
            inventory=inventory,
            equity_open=self._equity_open,
        )
        # financing and borrow on carried notional, charged at the open (zero when the close target was met)
        financing = 0.0
        for n, q in self._carried_open.items():
            v = self._last_valuation[n]
            financing += abs(q) * v * self.config.financing_rate_per_session
            if q < 0:
                financing += abs(q) * v * self.config.borrow_rate_per_session
        if financing:
            self.cash -= financing
            self._wf["financing"] += financing
            self._log(
                "financing", session=session_id, amount=financing, carried=dict(self._carried_open)
            )
        return self._equity_open

    def apply_corporate_action(
        self, name: str, ratio_num: int, ratio_den: int, kind: str = "split"
    ) -> None:
        """Adjust carried inventory between sessions: q -> q * num / den, V_open -> V_open * den / num.

        Notional is preserved; a ratio that does not leave integer shares in
        every cohort is refused (recorded as a valuation failure, never rounded).
        """
        if self._session is not None:
            raise LedgerError("corporate actions apply between sessions")
        if ratio_num <= 0 or ratio_den <= 0:
            raise LedgerError("ratio must be positive")
        q = self.positions.get(name, 0)
        if q == 0:
            self._log(
                "corporate_action",
                name=name,
                kind=kind,
                num=ratio_num,
                den=ratio_den,
                applied=False,
            )
            return
        parts = [
            (c.key, c.allocated.get(name, 0))
            for c in self.cohorts.values()
            if c.allocated.get(name, 0)
        ]
        for _, pq in parts + [(("", "", 0), q)]:
            if (pq * ratio_num) % ratio_den:
                self._log(
                    "valuation_failure",
                    name=name,
                    cause=f"{kind} {ratio_num}:{ratio_den} leaves fractional shares",
                )
                raise LedgerError(
                    f"{kind} {ratio_num}:{ratio_den} on {name} leaves fractional shares; refused and recorded"
                )
        for key, pq in parts:
            self.cohorts[key].allocated[name] = pq * ratio_num // ratio_den
        self.positions[name] = q * ratio_num // ratio_den
        self._last_valuation[name] = self._last_valuation[name] * ratio_den / ratio_num
        self._log(
            "corporate_action", name=name, kind=kind, num=ratio_num, den=ratio_den, applied=True
        )

    def close_session(self, valuations: Mapping[str, Valuation]) -> SessionResult:
        """Mark every position at its registered closeout valuation; the identity and the waterfall for the session."""
        if self._session is None:
            raise LedgerError("no session is open")
        if self._decision is not None:
            raise LedgerError("settle the open decision before the close")
        failures: list[dict[str, Any]] = []
        held = sorted(
            {n for c in self.cohorts.values() for n, q in c.allocated.items() if q}
            | set(self.positions)
        )
        for n in held:
            if n not in valuations:
                raise LedgerError(
                    f"inventory in {n} at the close has no registered valuation: refusing a silent zero"
                )
        for n, v in valuations.items():
            if not isinstance(v, Valuation):
                raise LedgerError("valuations are Valuation records")
            for c in (
                self.cohorts.values()
            ):  # refuse before anything is marked: a refused close changes nothing
                if (
                    c.allocated.get(n, 0)
                    and self.families[c.family_id].flat_at_close
                    and v.cause is None
                ):
                    raise LedgerError(
                        f"{n}: {c.key} holds {c.allocated[n]} at the close of a flat-at-close family "
                        "without a closeout_failure cause (halt, no quote, depth exhausted)"
                    )
        for n, v in valuations.items():
            self._last_valuation[n] = v.price
            owners = [c for key, c in sorted(self.cohorts.items()) if c.allocated.get(n, 0)]
            for c in owners:
                self._mark(
                    c, n, v.price
                )  # signal P&L while the attribution is open, execution residual after
                if not self.families[c.family_id].flat_at_close:
                    continue  # a multi-session family carries inventory as a matter of course
                row = {
                    "session": self._session,
                    "cohort": c.key,
                    "name": n,
                    "qty": c.allocated[n],
                    "cause": v.cause,
                    "valuation": v.price,
                }
                c.closeout_failures.append(row)
                failures.append(row)
                self._log("closeout_failure", **row)
                if (
                    c.state is CohortState.LIVE
                ):  # never liquidated: the signal attribution closes at the close valuation
                    c.attribution_closed_ts = (
                        self._last_ts if self._last_ts is not None else c.expiry_ts
                    )
                    c.early_exit_reason = f"close: {v.cause}"
                    c.state = CohortState.CLOSING
                    self._log(
                        "attribution_closed",
                        ts=c.attribution_closed_ts,
                        cohort=c.key,
                        signal_pnl=c.signal_pnl,
                        reason=c.early_exit_reason,
                    )
        inventory = sum(q * valuations[n].price for n, q in self.positions.items() if q != 0)
        net = self.cash + inventory - self._equity_open
        closing = [c for c in self.cohorts.values() if c.state is CohortState.CLOSING]
        result = SessionResult(
            session_id=self._session,
            equity_open=self._equity_open,
            cash_open=self._cash_open,
            cash_close=self.cash,
            marked_inventory_close=inventory,
            net_pnl=net,
            waterfall=dict(self._wf),
            carried={n: q for n, q in self.positions.items() if q != 0},
            closeout_failures=failures,
            closing_cohorts=len(closing),
            closing_gross_shares=sum(c.gross_shares() for c in closing),
            quantity_states=dict(self._quantity_states),
        )
        if abs(result.identity_gap) > self.config.tol * max(1.0, abs(net), self._equity_open):
            raise LedgerError(
                f"waterfall does not reconcile to the identity: gap {result.identity_gap}"
            )
        self._log(
            "session_close",
            session=self._session,
            cash=self.cash,
            inventory=inventory,
            net_pnl=net,
            waterfall=dict(self._wf),
            valuations={n: v.price for n, v in sorted(valuations.items())},
        )
        self.sessions.append(result)
        self._session = None
        return result

    # ------------------------------------------------------------------ cohorts
    def open_cohort(
        self,
        key: CohortKey,
        *,
        formation_ts: int,
        decision_ts: int,
        expiry_ts: int,
        intents: Mapping[str, int],
        forecasts: Mapping[str, float],
        decisions: Mapping[str, TradeDecision] | None = None,
    ) -> CohortRecord:
        """Register a cohort whose entry intents are submitted at ``decision_ts``."""
        self._check_key(key)
        if key in self.cohorts:
            raise LedgerError(f"cohort {key} already exists")
        if not (formation_ts <= decision_ts < expiry_ts):
            raise LedgerError("need formation_ts <= decision_ts < expiry_ts")
        if self._last_ts is not None and decision_ts < self._last_ts:
            raise LedgerError("cohort decision_ts is before the last settled decision")
        if not intents:
            raise LedgerError("a cohort needs at least one intended position")
        for n, q in intents.items():
            if isinstance(q, bool) or not isinstance(q, int):
                raise LedgerError(f"intent for {n} must be an int")
            if q == 0:
                raise LedgerError(f"intent for {n} is zero")
            if n not in forecasts or not math.isfinite(forecasts[n]):
                raise LedgerError(
                    f"intent for {n} has no finite forecast (a missing forecast is no trade)"
                )
        rec = CohortRecord(
            key=key,
            formation_ts=formation_ts,
            decision_ts=decision_ts,
            expiry_ts=expiry_ts,
            intents=dict(intents),
            forecasts={n: float(forecasts[n]) for n in intents},
            decisions=dict(decisions or {}),
        )
        self.cohorts[key] = rec
        self._log(
            "cohort_open",
            cohort=key,
            decision_ts=decision_ts,
            expiry_ts=expiry_ts,
            intents=dict(intents),
        )
        return rec

    def refuse_cohort(
        self, key: CohortKey, *, decision_ts: int, expiry_ts: int, reason: str
    ) -> CohortRecord:
        """A cohort construction skipped (E0 section 6 item 7): recorded with the binding constraint, no orders."""
        self._check_key(key)
        if key in self.cohorts:
            raise LedgerError(f"cohort {key} already exists")
        if not reason:
            raise LedgerError("a refusal names its reason")
        rec = CohortRecord(
            key,
            decision_ts,
            decision_ts,
            expiry_ts,
            {},
            {},
            state=CohortState.REFUSED,
            refusal_reason=reason,
        )
        self.cohorts[key] = rec
        self._log("cohort_refused", cohort=key, decision_ts=decision_ts, reason=reason)
        return rec

    def request_liquidation(self, key: CohortKey, reason: str) -> None:
        """A risk exit (beta or sector breach, halt, data gap): mandatory full exit at the next decision."""
        c = self.cohorts.get(key)
        if c is None or c.state not in (CohortState.LIVE, CohortState.CLOSING):
            raise LedgerError(f"cohort {key} is not live")
        if not reason:
            raise LedgerError("a liquidation names its reason")
        self._pending_liquidation[key] = reason

    def _check_key(self, key: CohortKey) -> None:
        if not (
            isinstance(key, tuple)
            and len(key) == 3
            and isinstance(key[0], str)
            and isinstance(key[1], str)
            and isinstance(key[2], int)
        ):
            raise LedgerError("a cohort key is (family_id, horizon, cohort_id)")
        if key[0] not in self.families:
            raise LedgerError(f"family {key[0]!r} is not registered (register_family first)")

    # ------------------------------------------------------------------ decisions
    def decide(
        self,
        ts: int,
        mids: Mapping[str, float],
        *,
        withhold: Iterable[str] = (),
        pre_close: bool = False,
    ) -> Decision:
        """Build the net orders at ``ts``: entries opened at ``ts``, expiry exits, re-sends, liquidations.

        ``mids`` are the decision mids of every name with an intent (the
        reference prices of the midpoint-reference convention).  ``withhold``
        names whose discretionary (entry) intents the no-trade band holds back;
        mandatory intents are never withheld.  ``pre_close``: every position of
        a flat-at-close family becomes a mandatory liquidation.
        """
        if self._session is None:
            raise LedgerError("open a session first")
        if self._decision is not None:
            raise LedgerError(f"decision at {self._decision.ts} is not settled")
        if self._last_ts is not None and ts <= self._last_ts:
            raise LedgerError("decision times must increase within a session")
        intents: list[Intent] = []
        for key in sorted(self.cohorts):
            c = self.cohorts[key]
            if c.state is CohortState.PENDING and c.decision_ts == ts:
                for n, q in c.intents.items():
                    intents.append(Intent(key, n, q, False, "entry"))
            elif c.state is CohortState.PENDING and c.decision_ts < ts:
                raise LedgerError(f"cohort {key} missed its decision time {c.decision_ts}")
            elif c.state in (CohortState.LIVE, CohortState.CLOSING):
                liquidate = key in self._pending_liquidation or (
                    pre_close and self.families[c.family_id].flat_at_close
                )
                expiring = c.state is CohortState.LIVE and c.expiry_ts <= ts
                if c.state is CohortState.CLOSING or expiring or liquidate:
                    kind = (
                        "resend"
                        if c.state is CohortState.CLOSING
                        else ("exit" if expiring else "liquidation")
                    )
                    for n, q in c.allocated.items():
                        if q:
                            intents.append(Intent(key, n, -q, True, kind))
        names = sorted({i.name for i in intents})
        missing = [n for n in names if n not in mids]
        if missing:
            raise LedgerError(f"decision at {ts} needs mids for {missing}")
        for n, m in mids.items():
            if not isinstance(m, int | float) or not math.isfinite(m) or m <= 0:
                raise LedgerError(f"mid for {n} must be a positive finite price")
        withhold_set = set(withhold)
        orders: dict[str, NetOrder] = {}
        for n in names:
            own = [i for i in intents if i.name == n]
            o = NetOrder(name=n, mid_decision=float(mids[n]), intents=own)
            o.intended = sum(i.qty for i in own)
            o.mandatory = sum(i.qty for i in own if i.mandatory)
            if n in withhold_set:
                o.submitted = o.mandatory
                o.intents = [i for i in own if i.mandatory]
            else:
                o.submitted = o.intended
            o.withheld = o.intended - o.submitted
            orders[n] = o
        if (
            self._last_ts is None
        ):  # first decision of the session: overnight P&L of carried inventory (diagnostic line)
            for n, q in self._carried_open.items():
                if n in mids:
                    self._wf["overnight_pnl"] += q * (float(mids[n]) - self._last_valuation[n])
        self._decision = Decision(
            ts=ts, mids={n: float(m) for n, m in mids.items()}, orders=orders, pre_close=pre_close
        )
        self._log(
            "decision",
            ts=ts,
            pre_close=pre_close,
            orders={
                n: {
                    "intended": o.intended,
                    "mandatory": o.mandatory,
                    "submitted": o.submitted,
                    "withheld": o.withheld,
                }
                for n, o in orders.items()
            },
        )
        return self._decision

    def fill(self, fill: Fill) -> None:
        """Record the simulated external fill of the open decision's net order in ``fill.name``."""
        d = self._decision
        if d is None or fill.ts != d.ts:
            raise LedgerError("fill must belong to the open decision")
        o = d.orders.get(fill.name)
        if o is None or o.submitted == 0:
            raise LedgerError(f"no submitted net order in {fill.name} at {d.ts}")
        if o.fill is not None:
            raise LedgerError(f"{fill.name} already filled at {d.ts} (one net order, one fill)")
        if (fill.qty > 0) != (o.submitted > 0) or abs(fill.qty) > abs(o.submitted):
            raise LedgerError(
                f"fill {fill.qty} is not a partial fill of the net order {o.submitted} in {fill.name}"
            )
        if abs(fill.mid_decision - o.mid_decision) > 1e-9:
            raise LedgerError("fill.mid_decision must be the decision mid of the net order")
        o.fill = fill
        o.filled = fill.qty

    def settle(self) -> Decision:
        """Cross, allocate, move cash, update quantity states and cohort states; checks the invariants."""
        d = self._decision
        if d is None:
            raise LedgerError("no open decision")
        for n in sorted(d.orders):
            self._settle_name(d, d.orders[n])
        for key, c in self.cohorts.items():
            if c.state is CohortState.PENDING and c.decision_ts == d.ts:
                c.state = CohortState.LIVE if c.gross_shares() else CohortState.UNFILLED
            if c.state is CohortState.LIVE and (
                c.expiry_ts <= d.ts
                or key in self._pending_liquidation
                or (d.pre_close and self.families[c.family_id].flat_at_close)
            ):
                reason = self._pending_liquidation.get(
                    key, "pre-close liquidation" if c.expiry_ts > d.ts else ""
                )
                self._close_attribution(c, d.ts, reason)
                c.state = CohortState.CLOSED if not c.gross_shares() else CohortState.CLOSING
            elif c.state is CohortState.CLOSING and not c.gross_shares():
                c.state = CohortState.CLOSED
        self._pending_liquidation.clear()
        self._check_invariants(d)
        d.settled = True
        self._decision = None
        self._last_ts = d.ts
        return d

    # ------------------------------------------------------------------ internals
    def _settle_name(self, d: Decision, o: NetOrder) -> None:
        n, m = o.name, o.mid_decision
        pos_side = [i for i in o.intents if i.qty > 0]
        neg_side = [i for i in o.intents if i.qty < 0]
        plus, minus = sum(i.qty for i in pos_side), -sum(i.qty for i in neg_side)
        crossed = min(plus, minus)
        filled = o.filled
        if o.submitted == 0 and filled:
            raise LedgerError("a fully netted name has no external fill")
        heavy_pos = o.submitted > 0
        avail_pos = crossed + (filled if heavy_pos and filled > 0 else 0)
        avail_neg = crossed + (-filled if (not heavy_pos) and filled < 0 else 0)
        alloc: dict[CohortKey, int] = {}
        alloc.update(self._allocate_side(pos_side, avail_pos, +1))
        alloc.update(self._allocate_side(neg_side, avail_neg, -1))
        if sum(alloc.values()) != filled:
            raise LedgerError(
                "allocation does not sum to the external fill"
            )  # pragma: no cover - by construction
        for i in o.intents:
            c = self.cohorts[i.key]
            a = alloc.get(i.key, 0)
            c.submitted[n] = c.submitted.get(n, 0) + i.qty
            if a:
                c.allocated[n] = c.allocated.get(n, 0) + a
                self._basis[(i.key, n)] = self._basis.get((i.key, n), 0.0) + a * m
                if (
                    c.allocated[n] == 0
                ):  # the position is closed: realise its reference P&L at the decision mid
                    del c.allocated[n]
                    self._mark(c, n, m)
                self._log("allocation", ts=d.ts, cohort=i.key, name=n, qty=a, mid=m, kind=i.kind)
            rem = i.qty - a
            if rem:
                if i.mandatory:
                    c.outstanding[n] = c.outstanding.get(n, 0) + rem
                    o.outstanding += rem
                else:
                    c.cancelled[n] = c.cancelled.get(n, 0) + rem
                    o.cancelled += rem
        # withheld entries (not in o.intents after the band) are recorded on their cohorts
        for key in sorted(self.cohorts):
            c = self.cohorts[key]
            if (
                c.state is CohortState.PENDING
                and c.decision_ts == d.ts
                and n in c.intents
                and not any(i.key == key for i in o.intents)
            ):
                c.withheld[n] = c.withheld.get(n, 0) + c.intents[n]
        o.crossed = crossed
        if o.fill is not None:
            f = o.fill
            self.cash += f.cash_flow
            self.positions[n] = self.positions.get(n, 0) + f.qty
            if self.positions[n] == 0:
                del self.positions[n]
            self.fills.append(f)
            self._wf["latency_drift"] += f.latency_drift
            self._wf["half_spread"] += f.half_spread_cost
            self._wf["impact"] += f.impact_cost
            self._wf["fees"] += f.fee
            self._log(
                "fill",
                ts=d.ts,
                name=n,
                qty=f.qty,
                price=f.price,
                fee=f.fee,
                half_spread=f.half_spread,
                impact=f.impact,
                mid_decision=f.mid_decision,
                mid_arrival=f.mid_arrival,
                column=f.column,
            )
        if crossed:
            self._log("cross", ts=d.ts, name=n, qty=crossed, mid=m)
        for k in (
            "intended",
            "mandatory",
            "submitted",
            "withheld",
            "crossed",
            "filled",
            "outstanding",
            "cancelled",
        ):
            self._quantity_states[k] += getattr(o, k)

    def _allocate_side(
        self, intents: list[Intent], available: int, sign: int
    ) -> dict[CohortKey, int]:
        if not intents:
            return {}
        mandatory = [i for i in intents if i.mandatory]
        entering = [i for i in intents if not i.mandatory]
        m_caps = [((self.cohorts[i.key].expiry_ts, _tie_key(i.key)), abs(i.qty)) for i in mandatory]
        e_caps = [(_tie_key(i.key), abs(i.qty)) for i in entering]
        got = distribute(available, m_caps, e_caps)
        out: dict[CohortKey, int] = {}
        for i, g in zip(mandatory + entering, got, strict=True):
            if g:
                out[i.key] = out.get(i.key, 0) + sign * g
        return out

    def _mark(self, c: CohortRecord, name: str, price: float) -> None:
        """Reference P&L of the position since the last mark; the basis resets to the marked notional."""
        q = c.allocated.get(name, 0)
        basis = self._basis.get((c.key, name), 0.0)
        pnl = q * price - basis
        self._basis[(c.key, name)] = q * price
        if q == 0:
            self._basis.pop((c.key, name), None)
        if c.attribution_closed_ts is None:
            c.signal_pnl += pnl
            self._wf["midpoint_reference_pnl"] += pnl
        else:
            c.execution_residual += pnl
            self._wf["execution_residual"] += pnl

    def _close_attribution(self, c: CohortRecord, n_ts: int, reason: str = "") -> None:
        """The research ledger's signal attribution closes at the scheduled expiry (or an early exit) at the decision mid."""
        if c.attribution_closed_ts is not None:
            return
        d = self._decision
        mids = d.mids if d is not None and d.ts == n_ts else {}
        for n in list(c.allocated) + [
            k[1] for k in self._basis if k[0] == c.key and k[1] not in c.allocated
        ]:
            price = mids.get(n)
            if price is None:
                # a cohort exited entirely by cross/fill at this decision still has a basis to close at the decision mid
                price = self._last_valuation.get(n)
                if price is None:
                    raise LedgerError(
                        f"no reference mid for {n} to close the attribution of {c.key}"
                    )
            self._mark(c, n, price)
        c.attribution_closed_ts = n_ts
        c.early_exit_reason = reason if c.expiry_ts > n_ts else ""
        self._log(
            "attribution_closed", ts=n_ts, cohort=c.key, signal_pnl=c.signal_pnl, reason=reason
        )

    def _check_invariants(self, d: Decision) -> None:
        names = set(self.positions) | {n for c in self.cohorts.values() for n in c.allocated}
        for n in names:
            agg = self.positions.get(n, 0)
            cohort_sum = sum(c.allocated.get(n, 0) for c in self.cohorts.values())
            if agg != cohort_sum:
                raise LedgerError(
                    f"{n}: allocated cohort quantities {cohort_sum} != aggregate holding {agg}"
                )
        for n, o in d.orders.items():
            if o.intended != o.submitted + o.withheld:
                raise LedgerError(f"{n}: intended != submitted + withheld")
            if o.submitted != o.filled + o.outstanding + o.cancelled:
                raise LedgerError(
                    f"{n}: submitted {o.submitted} != filled {o.filled} + outstanding {o.outstanding} + cancelled {o.cancelled}"
                )
        for key, c in self.cohorts.items():
            if c.state is CohortState.LIVE and c.expiry_ts <= d.ts:
                raise LedgerError(f"cohort {key} is live after its expiry")
            if (
                c.state in (CohortState.CLOSED, CohortState.UNFILLED, CohortState.REFUSED)
                and c.gross_shares()
            ):
                raise LedgerError(f"cohort {key} is {c.state.value} with inventory")
        if self.config.gross_cap is not None:
            gross = sum(
                abs(q) * d.mids.get(n, self._last_valuation.get(n, 0.0))
                for n, q in self.positions.items()
            )
            if gross > self.config.gross_cap * (1 + self.config.tol):
                raise LedgerError(f"gross exposure {gross} exceeds the cap {self.config.gross_cap}")

    def _log(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    # ------------------------------------------------------------------ views
    def equity(self, mids: Mapping[str, float]) -> float:
        return self.cash + sum(q * mids[n] for n, q in self.positions.items())

    def gross_shares(self) -> int:
        return sum(abs(q) for q in self.positions.values())

    def cohort(self, key: CohortKey) -> CohortRecord:
        try:
            return self.cohorts[key]
        except KeyError:
            raise LedgerError(f"unknown cohort {key}") from None

    def research_rows(self) -> list[dict[str, Any]]:
        """The research cohort ledger as rows (one per cohort and name)."""
        rows: list[dict[str, Any]] = []
        for key in sorted(self.cohorts):
            c = self.cohorts[key]
            for n in sorted(set(c.intents) | set(c.allocated)):
                td = c.decisions.get(n)
                rows.append(
                    {
                        "family_id": key[0],
                        "horizon": key[1],
                        "cohort_id": key[2],
                        "name": n,
                        "formation_ts": c.formation_ts,
                        "decision_ts": c.decision_ts,
                        "expiry_ts": c.expiry_ts,
                        "state": c.state.value,
                        "forecast_bps": c.forecasts.get(n),
                        "intended": c.intents.get(n, 0),
                        "submitted": c.submitted.get(n, 0),
                        "withheld": c.withheld.get(n, 0),
                        "position": c.allocated.get(n, 0),
                        "outstanding": c.outstanding.get(n, 0),
                        "cancelled": c.cancelled.get(n, 0),
                        "cushion_bps": td.cushion_bps if td else None,
                        "risk_status": td.risk_status if td else None,
                        "liquidity_status": td.liquidity_status if td else None,
                        "refusal_reason": c.refusal_reason,
                    }
                )
        return rows
