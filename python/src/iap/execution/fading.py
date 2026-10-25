"""Quote fading and queue-aware cancels for resting children (v1.17 WS-B B4 / B5).

Both rules are opt-ins on a :class:`~iap.execution.algos.ParentOrder` whose
resting children run through the PASSIVE state machine of
:mod:`iap.execution.replay` (``policy=PASSIVE``, or the *agree* posts of a
B2 ``urgency_policy="alpha"`` parent). A parent without them replays
byte-identically; the rules submit ordinary cancels, so simulator rule 7
applies: the cancel travels the latency path and **can lose the race** to a
fill that lands first.

**B4 — quote fading (:class:`FadeParams`).** A resting child is pulled when
the opposite-side signal or the book pressure crosses a threshold:

- *signal*: the parent's ``signal_source`` read as of ``t - latency_ns``
  (causal, stale after ``max_age_ns``) says the price is about to run
  AGAINST the resting order — the same alignment test as B2
  (:func:`iap.execution.urgency.regime_from_z` ``== "adverse"`` at
  ``threshold_z``, in signal units);
- *pressure*: :func:`book_pressure` — the opposite side's share of the two
  L1 sizes on the child's venue — is at least ``pressure_threshold`` (a buy
  resting at the bid is pulled when the ask side is heavy: the next move is
  likelier down, and a fill now would be adversely selected).

What happens to the pulled remainder is ``action``: ``"hold"`` (default)
keeps it off the book and re-posts it as soon as the condition clears (or
crosses it at the PASSIVE end margin, like any remainder), ``"repost"``
posts it again at once at the current post price, ``"cross"`` sends it as
MARKET. A chain of posts fades at most ``max_fades`` times.

**B5 — queue-aware cancels (:class:`QueueCancelParams`).** A resting child
is cancelled when its queue is hopeless: ``ahead_qty > max_ahead_qty``
(the simulator's deterministic queue position, rule 4), or — with a
:class:`~iap.execution.fill_hazard.FillHazardModel` — ``P(fill within
horizon_ns | ahead, spread, imbalance, depletion) < min_p_fill``. The
depletion feature should be the EXECUTION depletion rate
(:meth:`~iap.execution.calibration.ExecCalibration.queue_exec_depletion_hazard`,
review X-4): a level that empties because the orders ahead of us cancelled
did not fill anyone. ``action`` is ``"cross"`` (default: the queue was the
problem, take liquidity) or ``"repost"``.

**Accounting.** Every pull is a :class:`PullRecord` (decision, effect,
qty pulled, qty that filled while the cancel was in flight) on
``ExecReplayResult.pulls``; :func:`pull_outcomes` prices the fades after
the fact against a market timeline — the "fades saved vs fills missed"
report of the B4 benchmark variant: for each pulled quantity, whether the
market traded THROUGH the pulled price inside the horizon (the touch-through
proxy for "it would have filled"), and if so the counterfactual markout of
that fill, split into *saved* (it would have been adverse: the fade saved
``|markout|``) and *missed* (it would have been favourable: the fade forwent
it). A counterfactual, labelled as one: our resting order never moved the
replayed book, so nothing here is a measured reaction.

Every rule function below is plain arithmetic on scalars (no numpy), the
shape the C++ port (F7) reproduces line for line.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from iap.execution.urgency import regime_from_z

if TYPE_CHECKING:  # pragma: no cover
    from iap.execution.fill_hazard import FillHazardModel
    from iap.execution.market_timeline import MarketTimeline

#: ``ChildOrder.cancel_rule`` values: why a resting child was pulled.
CANCEL_RULES: tuple[str, ...] = (
    "fade:signal",  #: B4: the signal turned adverse by ``threshold_z``
    "fade:pressure",  #: B4: opposite-side book pressure at or above the threshold
    "queue:ahead",  #: B5: more than ``max_ahead_qty`` ahead in the queue
    "queue:p_fill",  #: B5: hazard ``P(fill within horizon)`` below ``min_p_fill``
)
FADE_ACTIONS: tuple[str, ...] = ("hold", "repost", "cross")
QUEUE_ACTIONS: tuple[str, ...] = ("cross", "repost")
#: ``PullRecord.outcome`` values once the cancel has resolved.
PULL_OUTCOMES: tuple[str, ...] = (
    "pulled",  #: the cancel won: the whole remainder came off the book
    "partial",  #: part filled before the cancel arrived, the rest was pulled
    "lost_race",  #: the child filled completely before the cancel arrived
)


@dataclass(frozen=True, slots=True)
class FadeParams:
    """B4 quote-fading knobs (module docstring)."""

    threshold_z: float = 1.0  #: signal leg, in signal units; ``0`` = off
    pressure_threshold: float = 0.0  #: book-pressure leg, in ``(0.5, 1]``; ``0`` = off
    action: str = "hold"
    max_fades: int = 3  #: fades per chain of posts of one step
    max_age_ns: int = 10_000_000_000  #: a reading older than this is no signal
    latency_ns: int = 0  #: the signal is read as of ``t - latency_ns``

    def __post_init__(self) -> None:
        if self.threshold_z < 0.0:
            raise ValueError("FadeParams.threshold_z must be >= 0 (0 = off)")
        if self.pressure_threshold != 0.0 and not (0.5 < self.pressure_threshold <= 1.0):
            raise ValueError("FadeParams.pressure_threshold must be in (0.5, 1] or 0 (off)")
        if self.threshold_z == 0.0 and self.pressure_threshold == 0.0:
            raise ValueError("FadeParams needs a signal threshold or a pressure threshold")
        if self.action not in FADE_ACTIONS:
            raise ValueError(f"FadeParams.action must be one of {FADE_ACTIONS}")
        if self.max_fades < 1:
            raise ValueError("FadeParams.max_fades must be >= 1")
        if self.max_age_ns < 0 or self.latency_ns < 0:
            raise ValueError("FadeParams.max_age_ns / latency_ns must be >= 0")


@dataclass(frozen=True, slots=True)
class QueueCancelParams:
    """B5 queue-aware cancel knobs (module docstring)."""

    max_ahead_qty: int = 0  #: ``0`` = off
    min_p_fill: float = 0.0  #: ``0`` = off; needs ``hazard``
    horizon_ns: int = 1_000_000_000  #: the ``p_fill_within`` horizon
    action: str = "cross"
    hazard: FillHazardModel | None = None
    #: ``(side, queue_ahead) -> depletion rate per second`` for the hazard's
    #: ``depletion_per_s`` feature, e.g. ``cal.queue_exec_depletion_hazard``
    #: (X-4: executions only). ``None`` feeds 0.
    depletion_per_s: Callable[[int, int], float | None] | None = None
    min_rest_ns: int = 0  #: grace period after arrival before the rule applies

    def __post_init__(self) -> None:
        if self.max_ahead_qty < 0:
            raise ValueError("QueueCancelParams.max_ahead_qty must be >= 0 (0 = off)")
        if not 0.0 <= self.min_p_fill <= 1.0:
            raise ValueError("QueueCancelParams.min_p_fill must be in [0, 1] (0 = off)")
        if self.max_ahead_qty == 0 and self.min_p_fill == 0.0:
            raise ValueError("QueueCancelParams needs max_ahead_qty or min_p_fill")
        if self.min_p_fill > 0.0 and self.hazard is None:
            raise ValueError("QueueCancelParams.min_p_fill needs a fitted hazard model")
        if self.horizon_ns <= 0:
            raise ValueError("QueueCancelParams.horizon_ns must be > 0")
        if self.action not in QUEUE_ACTIONS:
            raise ValueError(f"QueueCancelParams.action must be one of {QUEUE_ACTIONS}")
        if self.min_rest_ns < 0:
            raise ValueError("QueueCancelParams.min_rest_ns must be >= 0")


# ------------------------------------------------------------------ rules --


def book_pressure(own_qty: int, opp_qty: int) -> float:
    """The opposite side's share of the two L1 sizes, ``opp / (own + opp)``;
    ``0.5`` when both are empty. ``1`` = nothing displayed on our side."""
    total = own_qty + opp_qty
    if total <= 0:
        return 0.5
    return float(opp_qty) / float(total)


def fade_rule(
    side: int,
    z: float | None,
    threshold_z: float,
    pressure: float | None,
    pressure_threshold: float,
) -> str:
    """``"fade:signal"``, ``"fade:pressure"`` or ``""`` (keep resting).

    The signal leg fires first: ``z`` (signal units, the alpha's sign) is
    adverse for ``side`` at ``threshold_z`` (:func:`regime_from_z`); then the
    pressure leg: ``pressure >= pressure_threshold``. A ``None`` input or a
    zero threshold turns that leg off.
    """
    if z is not None and threshold_z > 0.0 and regime_from_z(side, z, threshold_z) == "adverse":
        return "fade:signal"
    if pressure is not None and pressure_threshold > 0.0 and pressure >= pressure_threshold:
        return "fade:pressure"
    return ""


def queue_cancel_rule(
    ahead_qty: int, max_ahead_qty: int, p_fill: float | None, min_p_fill: float
) -> str:
    """``"queue:ahead"``, ``"queue:p_fill"`` or ``""`` (keep resting).

    The queue-position leg fires first (``ahead_qty > max_ahead_qty`` with a
    positive cap), then the probability leg (``p_fill < min_p_fill`` with a
    positive floor and a defined probability).
    """
    if max_ahead_qty > 0 and ahead_qty > max_ahead_qty:
        return "queue:ahead"
    if p_fill is not None and min_p_fill > 0.0 and p_fill < min_p_fill:
        return "queue:p_fill"
    return ""


# ------------------------------------------------------------- accounting --


@dataclass(slots=True)
class PullRecord:
    """One B4 / B5 pull of a resting child (``ExecReplayResult.pulls``)."""

    parent_id: int
    order_id: int
    side: int  #: 0 buy / 1 sell
    price_ticks: int  #: the pulled child's limit
    qty_at_decision: int  #: remaining when the cancel was sent
    decision_ts: int
    rule: str  #: one of :data:`CANCEL_RULES`
    ahead_qty: int = 0  #: queue ahead at the decision
    signal_z: float = 0.0  #: the reading the decision used (0 when none)
    pressure: float = 0.0  #: :func:`book_pressure` at the decision (0 when unread)
    p_fill: float = -1.0  #: hazard probability at the decision (-1 when unread)
    effect_ts: int = 0  #: when the cancel resolved (0 while in flight)
    pulled_qty: int = 0  #: qty the cancel took off the book
    filled_in_flight: int = 0  #: qty filled between decision and effect
    outcome: str = ""  #: one of :data:`PULL_OUTCOMES` once resolved

    def to_dict(self) -> dict:
        return {
            "parent_id": self.parent_id,
            "order_id": self.order_id,
            "side": self.side,
            "price_ticks": self.price_ticks,
            "qty_at_decision": self.qty_at_decision,
            "decision_ts": self.decision_ts,
            "rule": self.rule,
            "ahead_qty": self.ahead_qty,
            "signal_z": self.signal_z,
            "pressure": self.pressure,
            "p_fill": self.p_fill,
            "effect_ts": self.effect_ts,
            "pulled_qty": self.pulled_qty,
            "filled_in_flight": self.filled_in_flight,
            "outcome": self.outcome,
        }


def would_have_filled(
    timeline: MarketTimeline, side: int, price: float, start_ts: int, end_ts: int
) -> bool:
    """Touch-through proxy: did the opposite best reach ``price`` in any
    state with ``start_ts <= ts <= end_ts``? (A buy at ``price`` would have
    been swept when the ask came down to it.)"""
    ts = timeline.ts
    i = bisect.bisect_left(ts, start_ts)
    j = bisect.bisect_right(ts, end_ts)
    k = i
    while k < j:
        if side == 0:
            if timeline.ask[k] <= price:
                return True
        elif timeline.bid[k] >= price:
            return True
        k += 1
    return False


def pull_rows(
    pulls: Sequence[PullRecord],
    timeline: MarketTimeline,
    tick_size: float,
    *,
    horizons_ns: Mapping[str, int] | None = None,
    qty_unit: float = 1.0,
    rules: Sequence[str] | None = None,
    cluster: str = "",
) -> list[dict[str, object]]:
    """One row per resolved pull with ``pulled_qty > 0`` under ``rules``
    (default: the two fade rules), priced against ``timeline``: ``rule``,
    ``side``, ``price``, ``qty``, ``cluster`` and per horizon ``h`` a
    ``counterfactual[h]`` of ``None`` (undefined markout) or ``{"would_fill",
    "markout_bps"}`` — the touch-through proxy and ``s * (m_h - p) / p`` of
    the fill the pull forwent (:func:`iap.tca.markout.markout_mid` at
    ``effect_ts + h``). Rows of several sessions pool in :func:`pull_summary`.
    """
    from iap.execution.markout import DEFAULT_HORIZONS_NS, markout_mid

    hz = dict(DEFAULT_HORIZONS_NS if horizons_ns is None else horizons_ns)
    want = ("fade:signal", "fade:pressure") if rules is None else tuple(rules)
    rows: list[dict[str, object]] = []
    for r in pulls:
        if r.rule not in want or not r.outcome or r.pulled_qty <= 0:
            continue
        price = float(r.price_ticks) * tick_size
        cf: dict[str, object] = {}
        for name, h in hz.items():
            m_h = markout_mid(timeline, r.effect_ts, h)
            if m_h is None or price <= 0.0:
                cf[name] = None
                continue
            s = 1.0 if r.side == 0 else -1.0
            cf[name] = {
                "would_fill": would_have_filled(
                    timeline, r.side, price, r.effect_ts, r.effect_ts + h
                ),
                "markout_bps": 1e4 * s * (m_h - price) / price,
            }
        rows.append(
            {
                "rule": r.rule,
                "side": r.side,
                "price": price,
                "qty": r.pulled_qty,
                "qty_unit": qty_unit,
                "cluster": cluster,
                "counterfactual": cf,
            }
        )
    return rows


def pull_summary(
    rows: Sequence[Mapping[str, object]],
    horizons: Sequence[str] | None = None,
    *,
    lost_race: int = 0,
    rules: Sequence[str] | None = None,
) -> dict[str, object]:
    """ "Fades saved vs fills missed" over :func:`pull_rows` output.

    Per horizon ``h``: ``n`` / ``qty`` with a defined counterfactual,
    ``would_fill`` / ``would_fill_qty`` (touch-through proxy), ``saved``
    (counterfactual markout < 0: the forgone fill would have been adverse;
    ``bps_mean`` of ``|markout|`` equal-weighted over those pulls, ``ccy`` =
    sum of ``|markout| * price * qty * qty_unit / 1e4``, ``qty``), ``missed``
    (markout >= 0: favourable fills forgone, same shape), ``net_ccy =
    saved.ccy - missed.ccy`` and ``net_bps`` = that over the pulled notional
    that would have filled. Labelled a counterfactual.
    """
    if horizons is None:
        first = rows[0]["counterfactual"] if rows else {}
        horizons = list(first) if isinstance(first, dict) else []
    want = ("fade:signal", "fade:pressure") if rules is None else tuple(rules)
    total_qty = 0
    for r in rows:
        total_qty += int(r["qty"])  # type: ignore[call-overload]
    out: dict[str, object] = {
        "label": "counterfactual: touch-through proxy on the replayed book, not a measurement",
        "rules": list(want),
        "n_pulls": len(rows),
        "pulled_qty": total_qty,
        "lost_race": lost_race,
        "horizons": {},
    }
    for name in horizons:
        n = 0
        qty = 0
        wf_n = 0
        wf_qty = 0
        wf_notional = 0.0
        saved_bps: list[float] = []
        saved_ccy = 0.0
        saved_qty = 0
        missed_bps: list[float] = []
        missed_ccy = 0.0
        missed_qty = 0
        for r in rows:
            cf = r["counterfactual"][name]  # type: ignore[index]
            if cf is None:
                continue
            q = int(r["qty"])  # type: ignore[call-overload]
            price = float(r["price"])  # type: ignore[arg-type]
            unit = float(r["qty_unit"])  # type: ignore[arg-type]
            n += 1
            qty += q
            if not cf["would_fill"]:
                continue
            notional = price * float(q) * unit
            wf_n += 1
            wf_qty += q
            wf_notional += notional
            bps = float(cf["markout_bps"])
            ccy = bps * 1e-4 * notional
            if bps < 0.0:
                saved_bps.append(-bps)
                saved_ccy += -ccy
                saved_qty += q
            else:
                missed_bps.append(bps)
                missed_ccy += ccy
                missed_qty += q
        net = saved_ccy - missed_ccy
        out["horizons"][name] = {  # type: ignore[index]
            "n": n,
            "qty": qty,
            "would_fill": wf_n,
            "would_fill_qty": wf_qty,
            "saved": {
                "n": len(saved_bps),
                "qty": saved_qty,
                "bps_mean": (sum(saved_bps) / len(saved_bps)) if saved_bps else None,
                "ccy": saved_ccy,
            },
            "missed": {
                "n": len(missed_bps),
                "qty": missed_qty,
                "bps_mean": (sum(missed_bps) / len(missed_bps)) if missed_bps else None,
                "ccy": missed_ccy,
            },
            "net_ccy": net,
            "net_bps": (1e4 * net / wf_notional) if wf_notional > 0.0 else None,
        }
    return out


def pull_outcomes(
    pulls: Sequence[PullRecord],
    timeline: MarketTimeline,
    tick_size: float,
    *,
    horizons_ns: Mapping[str, int] | None = None,
    qty_unit: float = 1.0,
    rules: Sequence[str] | None = None,
) -> dict[str, object]:
    """:func:`pull_summary` of :func:`pull_rows` for one timeline (one session)."""
    from iap.execution.markout import DEFAULT_HORIZONS_NS

    hz = dict(DEFAULT_HORIZONS_NS if horizons_ns is None else horizons_ns)
    want = ("fade:signal", "fade:pressure") if rules is None else tuple(rules)
    rows = pull_rows(pulls, timeline, tick_size, horizons_ns=hz, qty_unit=qty_unit, rules=want)
    lost = 0
    for r in pulls:
        if r.rule in want and r.outcome == "lost_race":
            lost += 1
    return pull_summary(rows, list(hz), lost_race=lost, rules=want)
