"""Deterministic research execution simulator feeding TCA (spec §19).

This is NOT the production backtester — it is a pinned, self-contained
simulation that turns the cross-language golden event vectors into a
reproducible parent-order/fill set so the TCA layer has realistic input:

- The golden streams (``tests/golden/events_eq_mbo.jsonl`` for instrument 1,
  ``events_fx_quote.jsonl`` for instrument 101) are replayed through the
  reference ``ConsolidatedBook``; every event with a two-sided book appends
  a state to a :class:`MarketTimeline` — CROSSED consolidated states
  (bid > ask across venues) are skipped and counted, LOCKED states (bid ==
  ask, half-spread 0) are kept (pinned §2.1) — TRADE events feed the market
  VWAP tape and HALT statuses are recorded for the markout rule.
- Parent orders are generated from a :class:`SplitMix64` stream with pinned
  draw order (decision point, side, size, decision->arrival delay, then one
  skip-draw per child slice).  Identical seed => identical orders and fills,
  bit for bit.
- Execution model (pinned research toy): 4 child slices, 15s apart, each
  crossing the spread at the prevailing touch plus a depth-dependent impact
  of ``min(5, floor(3 * child_qty / contra_depth))`` ticks; a child is
  skipped (unfilled -> opportunity cost) when its skip-draw < 0.15.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from iap.core.codec import read_jsonl
from iap.core.events import EventType, SessionStatus
from iap.core.rng import SplitMix64
from iap.orderbook.book import ConsolidatedBook
from iap.tca.fills import TAKER, Fill, MarketTimeline, ParentOrder

_REPO = Path(__file__).resolve().parents[4]

#: Pinned simulation parameters.
SIM_SEED = 20260829
N_SLICES = 4
SLICE_NS = 15_000_000_000          # 15s between child slices
SKIP_PROB = 0.15                   # per-child unfilled probability
IMPACT_COEF = 3.0                  # ticks per unit of child participation
IMPACT_CAP_TICKS = 5
DELAY_MIN_MS = 200
DELAY_MAX_MS = 1500
QTY_LOTS_MIN = 5
QTY_LOTS_MAX = 50

#: Golden stream -> (instrument_id, tick_size, lot_size)
GOLDEN_STREAMS: Dict[str, Tuple[int, float, int]] = {
    "events_eq_mbo.jsonl": (1, 0.01, 100),
    "events_fx_quote.jsonl": (101, 1e-05, 1000),
}


def build_timeline(events_path: Path, instrument_id: int,
                   tick_size: float) -> MarketTimeline:
    """Replay a golden event file into a MarketTimeline for one instrument."""
    if tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    events = read_jsonl(events_path)
    book = ConsolidatedBook(instrument_id)
    tl = MarketTimeline()
    for ev in events:
        if ev.instrument_id != instrument_id:
            continue
        book.apply(ev)
        if ev.event_type == EventType.TRADE:
            tl.add_trade(ev.exchange_ts, ev.price_ticks * tick_size, ev.qty)
        if ev.event_type == EventType.STATUS and ev.qty == SessionStatus.HALT:
            tl.add_halt(ev.exchange_ts)
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None:
            continue
        # pinned: crossed states skipped + counted, locked states kept
        tl.append_state_pinned(ev.exchange_ts, bb[0] * tick_size,
                               ba[0] * tick_size, bb[1], ba[1])
    if len(tl) < 50:
        raise ValueError(f"timeline too short from {events_path}")
    return tl


def simulate_parent_orders(
    timeline: MarketTimeline,
    instrument_id: int,
    tick_size: float,
    lot_size: int,
    n_orders: int,
    rng: SplitMix64,
    first_order_id: int = 1,
) -> List[ParentOrder]:
    """Generate + execute parent orders against a timeline (pinned model)."""
    if n_orders < 1:
        raise ValueError("n_orders must be >= 1")
    n_states = len(timeline)
    horizon_ns = N_SLICES * SLICE_NS
    orders: List[ParentOrder] = []
    for k in range(n_orders):
        # pinned draw order per parent order
        u_decision = rng.uniform()
        u_side = rng.uniform()
        lots = rng.randint(QTY_LOTS_MIN, QTY_LOTS_MAX)
        delay_ms = rng.randint(DELAY_MIN_MS, DELAY_MAX_MS)
        skip_draws = [rng.uniform() for _ in range(N_SLICES)]

        lo = int(0.05 * n_states)
        hi = int(0.75 * n_states)
        di = lo + int(u_decision * (hi - lo))
        decision_ts = timeline.ts[di]
        side = 0 if u_side < 0.5 else 1
        qty_target = lot_size * lots
        arrival_ts = decision_ts + delay_ms * 1_000_000
        end_ts = arrival_ts + horizon_ns

        order = ParentOrder(
            order_id=first_order_id + k,
            instrument_id=instrument_id,
            side=side,
            qty_target=qty_target,
            decision_ts=decision_ts,
            arrival_ts=arrival_ts,
            end_ts=end_ts,
        )
        child_qty = qty_target // N_SLICES
        for j in range(N_SLICES):
            qty = child_qty if j < N_SLICES - 1 \
                else qty_target - child_qty * (N_SLICES - 1)
            if qty <= 0 or skip_draws[j] < SKIP_PROB:
                continue
            t_child = arrival_ts + j * SLICE_NS
            i = timeline.prevailing(t_child)
            if i is None:
                continue
            if side == 0:  # buy: cross at ask + impact ticks
                contra_depth = max(timeline.ask_sz[i], 1)
                extra = min(IMPACT_CAP_TICKS,
                            int(IMPACT_COEF * qty / contra_depth))
                price = timeline.ask[i] + extra * tick_size
            else:          # sell: hit bid - impact ticks
                contra_depth = max(timeline.bid_sz[i], 1)
                extra = min(IMPACT_CAP_TICKS,
                            int(IMPACT_COEF * qty / contra_depth))
                price = timeline.bid[i] - extra * tick_size
            order.fills.append(Fill(
                ts=t_child,
                price=price,
                qty=qty,
                mid_at_fill=timeline.mid(i),
                half_spread_at_fill=timeline.half_spread(i),
                opp_depth_at_fill=contra_depth,
                liquidity=TAKER,
            ))
        orders.append(order)
    return orders


def bundled_order_set(
    golden_dir: Optional[Path] = None,
    n_orders: Sequence[int] = (24, 12),
    seed: int = SIM_SEED,
) -> Dict[int, Tuple[MarketTimeline, List[ParentOrder], float]]:
    """The pinned bundled parent-order set: {instrument_id: (timeline, orders,
    tick_size)} built from the golden vectors (EQ then FX, pinned order)."""
    gdir = Path(golden_dir) if golden_dir is not None \
        else _REPO / "tests" / "golden"
    rng = SplitMix64(seed)
    out: Dict[int, Tuple[MarketTimeline, List[ParentOrder], float]] = {}
    next_id = 1
    for (fname, (iid, tick, lot)), n in zip(GOLDEN_STREAMS.items(), n_orders):
        tl = build_timeline(gdir / fname, iid, tick)
        orders = simulate_parent_orders(tl, iid, tick, lot, n, rng,
                                        first_order_id=next_id)
        next_id += len(orders)
        out[iid] = (tl, orders, tick)
    return out
