"""Markout analysis: what the mid did after each fill (API_PORTFOLIO_TCA.md §2.7).

Reference implementation; ``com.iap.tca.Markout`` is the Java port and
``tests/golden/expected_markout.json`` pins both (1e-9, nulls exact).

Definitions (side sign ``s`` = +1 buy / -1 sell, fill price ``p``, fill
time ``t_f``, horizon ``h``):

- ``m_f`` — the fill's reference mid (pinned §2.4, the rule of
  ``iap.tca.fills.stamp_fill``): the state prevailing at ``t_f`` for a TAKER
  fill, the state strictly before ``t_f`` for a MAKER fill.
- ``m_h`` — the mid AT OR BEFORE ``t_f + h`` in event time
  (``timeline.prevailing(t_f + h)``): the latest two-sided state with
  ``ts <= t_f + h``. No interpolation, no look-ahead.
- ``markout(h) = s * (m_h - p)`` — the mark-to-market of the fill ``h``
  later, per unit. POSITIVE = the fill looks good after ``h`` (we bought
  and the mid is above our price), NEGATIVE = it looks bad.
- ``effective_half_spread = s * (p - m_f)`` — what the fill paid relative to
  the mid it traded against: ``+hs`` for a taker at the touch, ``-hs`` for a
  maker at the touch (a negative cost: the half-spread was EARNED).
- ``realised_half_spread(h) = s * (p - m_h) = -markout(h)`` — what was still
  paid (or kept, when negative) once the mid has moved for ``h``.
- ``price_impact(h) = s * (m_h - m_f)`` — how far the mid moved in the
  direction of our trade.
- The decomposition is exact, per fill and therefore per cell:
  ``effective_half_spread = realised_half_spread(h) + price_impact(h)``.
- ``adverse_selection(h) = -price_impact(h) = s * (m_f - m_h)`` over MAKER
  fills only: POSITIVE = the mid moved against the resting order after it
  was filled (we bought and the mid fell). A passive fill keeps
  ``half_spread_earned - adverse_selection(h)``.

Bps figures are per fill ``1e4 * x / p``; currency figures are
``x * qty * qty_unit`` in the instrument's quote currency.

A markout is UNDEFINED — reported ``null``, never zero, never a stale
carry — when any of these holds:

1. no two-sided state exists at or before ``t_f + h`` (no quote yet);
2. the timeline ends before ``t_f + h`` (session end: ``last_ts < t_f + h``);
3. a gate started inside ``(min(t_f, ts(m_h)), t_f + h]``, a gate being any
   timestamp in ``timeline.halts``. ``build_gated_timeline`` records one at
   every non-TRADING session status (HALT, AUCTION, CLOSE) of any venue and
   every time the consolidated book stops being two-sided, and appends no
   state while either lasts — so a window that contains a halt, an auction,
   or a no-quote gap is undefined, and so is one whose latest quote
   predates the gap. (``iap.tca.simulator.build_timeline`` records HALT
   only, as before; the bundled research TCA is unchanged.)

The fill's own measures need ``m_f``: a fill with no reference state — none
yet, or one that predates a gate (``ts(m_f) < gate <= reference time``: the
fill happened inside a halt or a quote gap) — has no measure at any horizon.

Aggregation (``markout_report``). Cells: ``all``, by ``liquidity`` (MAKER =
passive / TAKER = aggressive), ``venue``, ``algo``, ``side`` and
``time_bucket`` (``floor((t_f - session_start_ts) / bucket_ns)``). Per cell
and horizon over the fills DEFINED at that horizon: ``n``, the equal-weight
means of the four measures in bps with their standard errors (sample
standard deviation, ``ddof = 1``, over ``sqrt(n)``) and the currency sums. A
cell with fewer than ``min_fills`` defined fills reports ``n`` and ``null``
for every statistic. The standard error treats fills as independent; fills
of one parent order are not, so it is a lower bound.

Passive orders (``passive_order_stats``): a passive order is a LIMIT child
that came to rest. ``fill_rate_qty`` = filled / posted quantity,
``fill_rate_orders`` = share of orders with at least one fill,
``full_fill_rate`` = share completely filled, ``time_to_first_fill_ns`` /
``time_to_full_fill_ns`` = from the order's arrival at the venue to its
first / completing fill (mean and standard error over the orders that have
one), all of it again per queue-position bucket at entry
(``entry_ahead_qty``: the displayed size ahead of the order when it came to
rest, which the execution simulator exposes).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from iap.core.codec import read_jsonl
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.orderbook.book import ConsolidatedBook
from iap.tca.fills import MAKER, TAKER, MarketTimeline

#: Default markout horizons (event-time nanoseconds), in report order.
DEFAULT_HORIZONS_NS: dict[str, int] = {
    "100ms": 100_000_000,
    "1s": 1_000_000_000,
    "5s": 5_000_000_000,
    "30s": 30_000_000_000,
    "60s": 60_000_000_000,
    "5min": 300_000_000_000,
}

#: A cell with fewer defined fills than this reports null statistics.
DEFAULT_MIN_FILLS = 5

#: Default time bucket of the ``time_bucket`` split (5 minutes).
DEFAULT_BUCKET_NS = 300_000_000_000

#: Upper edges (inclusive) of the queue-position buckets, in quantity ahead.
DEFAULT_QUEUE_EDGES: tuple[int, ...] = (0, 500, 2000)

_MEASURES = ("markout", "effective_half_spread", "realised_half_spread", "price_impact")


@dataclass(frozen=True, slots=True)
class MarkoutFill:
    """One fill as the markout analysis sees it."""

    ts: int
    price: float  #: research double (ticks * tick_size)
    qty: int
    side: int  #: 0 buy / 1 sell
    liquidity: str  #: MAKER / TAKER
    venue_id: int = 0
    algo: str = ""
    qty_unit: float = 1.0  #: base units per qty unit (FX lot; 1 for equities)

    def __post_init__(self) -> None:
        if self.liquidity not in (MAKER, TAKER):
            raise ValueError(f"liquidity must be TAKER or MAKER, got {self.liquidity!r}")
        if self.side not in (0, 1):
            raise ValueError("side must be 0 (buy) or 1 (sell)")
        if self.qty <= 0 or not self.price > 0.0:
            raise ValueError("fill qty and price must be > 0")


@dataclass(frozen=True, slots=True)
class PassiveOrder:
    """One LIMIT child that came to rest (module docstring, passive orders)."""

    qty: int
    filled_qty: int
    rest_ts: int  #: arrival at the venue
    first_fill_ts: int | None
    last_fill_ts: int | None
    entry_ahead_qty: int

    def __post_init__(self) -> None:
        if self.qty <= 0 or not 0 <= self.filled_qty <= self.qty:
            raise ValueError("passive order needs qty > 0 and 0 <= filled_qty <= qty")
        if (self.filled_qty > 0) != (self.first_fill_ts is not None):
            raise ValueError("first_fill_ts must be set exactly when the order filled")


def reference_mid(timeline: MarketTimeline, ts: int, liquidity: str) -> float | None:
    """``m_f`` (pinned §2.4), or None when no state prevails or the
    prevailing state predates a gate (the fill happened with no live quote)."""
    ref_ts = ts if liquidity == TAKER else ts - 1
    i = timeline.prevailing(ref_ts)
    if i is None:
        return None
    for g in timeline.halts:
        if timeline.ts[i] < g <= ref_ts:
            return None
    return timeline.mid(i)


def markout_mid(timeline: MarketTimeline, fill_ts: int, horizon_ns: int) -> float | None:
    """``m_h``: the mid at or before ``fill_ts + horizon_ns``; None when the
    markout is undefined (module docstring, rules 1-3)."""
    if horizon_ns < 0:
        raise ValueError("horizon_ns must be >= 0")
    t = fill_ts + horizon_ns
    i = timeline.prevailing(t)
    if i is None or timeline.ts[-1] < t:
        return None
    lo = min(fill_ts, timeline.ts[i])
    for g in timeline.halts:
        if lo < g <= t:
            return None
    return timeline.mid(i)


def fill_measures(
    fill: MarkoutFill, timeline: MarketTimeline, horizon_ns: int
) -> dict[str, float] | None:
    """The four measures of one fill at one horizon, per unit of price
    (``markout``, ``effective_half_spread``, ``realised_half_spread``,
    ``price_impact``); None when the markout or the reference mid is
    undefined."""
    m_h = markout_mid(timeline, fill.ts, horizon_ns)
    m_f = reference_mid(timeline, fill.ts, fill.liquidity)
    if m_h is None or m_f is None:
        return None
    s = 1.0 if fill.side == 0 else -1.0
    return {
        "markout": s * (m_h - fill.price),
        "effective_half_spread": s * (fill.price - m_f),
        "realised_half_spread": s * (fill.price - m_h),
        "price_impact": s * (m_h - m_f),
    }


def _mean_se(values: Sequence[float]) -> tuple[float, float]:
    n = len(values)
    total = 0.0
    for v in values:
        total += v
    mean = total / n
    ss = 0.0
    for v in values:
        ss += (v - mean) * (v - mean)
    return mean, math.sqrt(ss / (n - 1) / n)


def _cell(
    fills: Sequence[MarkoutFill],
    timeline: MarketTimeline,
    horizons: Mapping[str, int],
    min_fills: int,
) -> dict[str, object]:
    qty = 0
    for f in fills:
        qty += f.qty
    out: dict[str, object] = {"n_fills": len(fills), "qty": qty, "horizons": {}}
    for name, h in horizons.items():
        bps: dict[str, list[float]] = {m: [] for m in _MEASURES}
        ccy = dict.fromkeys(_MEASURES, 0.0)
        for f in fills:
            x = fill_measures(f, timeline, h)
            if x is None:
                continue
            for m in _MEASURES:
                bps[m].append(1e4 * x[m] / f.price)
                ccy[m] += x[m] * float(f.qty) * f.qty_unit
        n = len(bps["markout"])
        row: dict[str, object] = {"n": n}
        for m in _MEASURES:
            if n < min_fills:
                row[f"{m}_bps"] = None
                row[f"{m}_se_bps"] = None
                row[f"{m}_ccy"] = None
            else:
                mean, se = _mean_se(bps[m])
                row[f"{m}_bps"] = mean
                row[f"{m}_se_bps"] = se
                row[f"{m}_ccy"] = ccy[m]
        out["horizons"][name] = row  # type: ignore[index]
    return out


def markout_report(
    fills: Sequence[MarkoutFill],
    timeline: MarketTimeline,
    *,
    horizons: Mapping[str, int] | None = None,
    min_fills: int = DEFAULT_MIN_FILLS,
    bucket_ns: int = DEFAULT_BUCKET_NS,
    session_start_ts: int | None = None,
) -> dict[str, object]:
    """Markout table of one instrument's fills (module docstring).

    Group keys are strings, in ascending order of the underlying value
    (venue id, algo name, side, bucket index); ``adverse_selection`` is the
    MAKER cell's ``-price_impact`` per horizon.
    """
    hz = dict(DEFAULT_HORIZONS_NS if horizons is None else horizons)
    if min_fills < 2:
        raise ValueError("min_fills must be >= 2 (a standard error needs two fills)")
    if bucket_ns <= 0:
        raise ValueError("bucket_ns must be > 0")
    start = session_start_ts
    if start is None:
        start = timeline.ts[0] if len(timeline) else 0

    def split(key) -> dict[str, object]:
        groups: dict = {}
        for f in fills:
            groups.setdefault(key(f), []).append(f)
        return {str(k): _cell(groups[k], timeline, hz, min_fills) for k in sorted(groups)}

    by_liquidity = split(lambda f: f.liquidity)
    adverse: dict[str, object] = {}
    maker = by_liquidity.get(MAKER)
    for name in hz:
        row = None if maker is None else maker["horizons"][name]  # type: ignore[index]
        if row is None or row["price_impact_bps"] is None:
            adverse[name] = {
                "n": 0 if row is None else row["n"],
                "bps": None,
                "se_bps": None,
                "ccy": None,
            }
        else:
            adverse[name] = {
                "n": row["n"],
                "bps": -row["price_impact_bps"],
                "se_bps": row["price_impact_se_bps"],
                "ccy": -row["price_impact_ccy"],
            }
    return {
        "horizons_ns": hz,
        "min_fills": min_fills,
        "bucket_ns": bucket_ns,
        "session_start_ts": start,
        "all": _cell(fills, timeline, hz, min_fills),
        "by_liquidity": by_liquidity,
        "by_venue": split(lambda f: f.venue_id),
        "by_algo": split(lambda f: f.algo),
        "by_side": split(lambda f: "BUY" if f.side == 0 else "SELL"),
        "by_time_bucket": split(lambda f: (f.ts - start) // bucket_ns),
        "adverse_selection": adverse,
    }


def _stat(values: Sequence[float], min_n: int) -> dict[str, object]:
    n = len(values)
    if n < min_n:
        return {"n": n, "mean": None, "se": None}
    mean, se = _mean_se(values)
    return {"n": n, "mean": mean, "se": se}


def _passive_cell(orders: Sequence[PassiveOrder], min_orders: int) -> dict[str, object]:
    n = len(orders)
    posted = filled = any_fill = full = 0
    first: list[float] = []
    complete: list[float] = []
    for o in orders:
        posted += o.qty
        filled += o.filled_qty
        if o.filled_qty > 0:
            any_fill += 1
            first.append(float(o.first_fill_ts - o.rest_ts))  # type: ignore[operator]
        if o.filled_qty == o.qty:
            full += 1
            complete.append(float(o.last_fill_ts - o.rest_ts))  # type: ignore[operator]
    enough = n >= min_orders
    return {
        "n_orders": n,
        "posted_qty": posted,
        "filled_qty": filled,
        "fill_rate_qty": filled / posted if enough else None,
        "fill_rate_orders": any_fill / n if enough else None,
        "full_fill_rate": full / n if enough else None,
        "time_to_first_fill_ns": _stat(first, min_orders),
        "time_to_full_fill_ns": _stat(complete, min_orders),
    }


def queue_bucket(entry_ahead_qty: int, edges: Sequence[int]) -> str:
    """Label of the queue-position bucket: ``"0"``, ``"1-500"``, ..., ``">2000"``."""
    lo = 0
    for e in edges:
        if entry_ahead_qty <= e:
            return str(e) if e == lo else f"{lo}-{e}"
        lo = e + 1
    return f">{edges[-1]}"


def passive_order_stats(
    orders: Sequence[PassiveOrder],
    *,
    min_orders: int = DEFAULT_MIN_FILLS,
    queue_edges: Sequence[int] = DEFAULT_QUEUE_EDGES,
) -> dict[str, object]:
    """Fill rate and time to fill of passive orders, overall and per
    queue-position bucket at entry (buckets in ascending edge order; empty
    buckets are omitted)."""
    if min_orders < 2:
        raise ValueError("min_orders must be >= 2")
    edges = tuple(queue_edges)
    if not edges or edges[0] != 0 or any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
        raise ValueError("queue_edges must start at 0 and be strictly ascending")
    labels = [queue_bucket(e, edges) for e in edges] + [f">{edges[-1]}"]
    groups: dict[str, list[PassiveOrder]] = {}
    for o in orders:
        groups.setdefault(queue_bucket(o.entry_ahead_qty, edges), []).append(o)
    return {
        "min_orders": min_orders,
        "queue_edges": list(edges),
        "all": _passive_cell(orders, min_orders),
        "by_queue_ahead_at_entry": {
            label: _passive_cell(groups[label], min_orders) for label in labels if label in groups
        },
    }


def build_gated_timeline(
    events: Sequence[MarketEvent] | str | Path, instrument_id: int, tick_size: float
) -> MarketTimeline:
    """Timeline for markouts: a state per event with a two-sided, uncrossed
    consolidated book while every venue seen so far is TRADING; a gate
    (``timeline.halts``) at every non-TRADING status and whenever the book
    stops being two-sided (module docstring, rule 3). TRADE prints feed the
    tape as in ``iap.tca.simulator.build_timeline``."""
    if tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    evs = read_jsonl(events) if isinstance(events, (str, Path)) else events
    book = ConsolidatedBook(instrument_id)
    tl = MarketTimeline()
    gated: set[int] = set()  # venues not TRADING (membership only)
    quoted = False
    for ev in evs:
        if ev.instrument_id != instrument_id:
            continue
        book.apply(ev)
        t = ev.exchange_ts
        if ev.event_type == EventType.TRADE:
            tl.add_trade(t, ev.price_ticks * tick_size, ev.qty)
        if ev.event_type == EventType.STATUS:
            if ev.qty == SessionStatus.TRADING:
                gated.discard(ev.venue_id)
            else:
                if ev.venue_id not in gated:
                    tl.add_halt(t)
                gated.add(ev.venue_id)
        bb, ba = book.best_bid(), book.best_ask()
        two_sided = bb is not None and ba is not None
        if quoted and not two_sided:
            tl.add_halt(t)
        quoted = two_sided
        if two_sided and not gated:
            tl.append_state_pinned(t, bb[0] * tick_size, ba[0] * tick_size, bb[1], ba[1])
    return tl
