"""TCA metric formulas (spec §19) — pure functions, golden-testable.

All decompositions follow Perold's implementation-shortfall framework with
the platform's pinned conventions:

- side sign ``s``: +1 buy, -1 sell; costs are POSITIVE when execution is
  worse than the benchmark.
- Perold decomposition of a parent order with decision mid ``m_d``, arrival
  mid ``m_a`` (prevailing mid when the first child could act), end-of-horizon
  mid ``m_e``, target ``Q``, fills ``(p_f, q_f)``, ``Q_f = sum q_f``:

      delay_cost       = s * Q_f * (m_a - m_d)
      trading_cost     = s * sum_f q_f * (p_f - m_a)
      opportunity_cost = s * (Q - Q_f) * (m_e - m_d)
      total_is         = delay + trading + opportunity
                       = s * sum_f q_f*(p_f - m_d) + s*(Q - Q_f)*(m_e - m_d)

  (identity is exact — tested to 1e-9).  Bps figures normalize by
  ``Q * m_d``.
- Spread vs impact split of trading cost per fill: the half-spread at the
  fill instant is spread cost; the remainder of the fill's cost relative to
  the fill-time mid is impact:

      spread_cost = sum_f q_f * hs_f
      impact_cost = s * sum_f q_f * (p_f - mid_f) - spread_cost

  (for a marketable buy at the ask, ``p_f - mid_f = hs_f + extra`` so the
  extra ticks are impact.)
- Adverse selection at delta: ``s * (mid(t_f + delta) - p_f)`` per filled
  unit — positive means the price kept moving against the parent after the
  fill (it was "picked off" in the passive case / momentum in the taker
  case); measured at pinned deltas {100ms, 1s, 10s}. A markout is DEFINED
  (pinned §2.5) only when the timeline extends to ``t_f + delta``
  (``last_ts >= t_f + delta``) and no HALT started in ``(t_f, t_f + delta]``;
  undefined fills are excluded and ``n_defined`` is reported per delta —
  a stale last mid is never carried past the end of the data.
- Reference state of a fill (pinned §2.4): TAKER fills use the state
  prevailing at the fill time; MAKER fills use the state strictly before the
  triggering event (``prevailing(t_f - 1)``), so a passive fill by a
  trade-through is attributed as provided liquidity (spread cost = -q*hs).
- Windows (pinned §2.3): every fill must lie in ``[arrival_ts, end_ts]`` and
  ``end_ts`` must be inside the timeline — otherwise the order is rejected
  (ValueError), never analysed against fabricated reference prices.
- Impact regression: OLS of per-fill signed cost bps on per-fill
  participation (q_f / displayed contra depth) — slope is the impact
  estimate in bps per unit participation.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from iap.tca.fills import MarketTimeline, ParentOrder

ADVERSE_DELTAS_NS: Dict[str, int] = {
    "100ms": 100_000_000,
    "1s": 1_000_000_000,
    "10s": 10_000_000_000,
}


def perold_decomposition(
    side_sign: int,
    qty_target: int,
    fills: Sequence[Tuple[float, int]],
    decision_mid: float,
    arrival_mid: float,
    end_mid: float,
) -> Dict[str, float]:
    """Perold IS decomposition (currency units + bps of Q*decision_mid)."""
    if side_sign not in (1, -1):
        raise ValueError("side_sign must be +1 or -1")
    if qty_target <= 0:
        raise ValueError("qty_target must be > 0")
    if decision_mid <= 0:
        raise ValueError("decision_mid must be > 0")
    q_f = sum(q for _, q in fills)
    if q_f > qty_target:
        raise ValueError("filled more than target")
    s = float(side_sign)
    delay = s * q_f * (arrival_mid - decision_mid)
    trading = s * sum(q * (p - arrival_mid) for p, q in fills)
    opportunity = s * (qty_target - q_f) * (end_mid - decision_mid)
    total = delay + trading + opportunity
    denom = qty_target * decision_mid
    return {
        "qty_target": float(qty_target),
        "qty_filled": float(q_f),
        "fill_rate": q_f / qty_target,
        "delay_cost": delay,
        "trading_cost": trading,
        "opportunity_cost": opportunity,
        "total_is": total,
        "delay_bps": 1e4 * delay / denom,
        "trading_bps": 1e4 * trading / denom,
        "opportunity_bps": 1e4 * opportunity / denom,
        "total_is_bps": 1e4 * total / denom,
    }


def arrival_slippage_bps(order: ParentOrder,
                         arrival_mid: float) -> Optional[float]:
    """Signed fill-VWAP slippage vs the arrival mid, in bps (None if unfilled)."""
    if order.qty_filled == 0:
        return None
    if arrival_mid <= 0:
        raise ValueError("arrival_mid must be > 0")
    return 1e4 * order.sign * (order.fill_vwap - arrival_mid) / arrival_mid


def interval_vwap(timeline: MarketTimeline, start_ts: int,
                  end_ts: int) -> Optional[float]:
    """Market VWAP of trades in [start_ts, end_ts] (None if no trades)."""
    num = 0.0
    den = 0
    for ts, price, qty in timeline.trades:
        if start_ts <= ts <= end_ts:
            num += price * qty
            den += qty
    return num / den if den > 0 else None


def interval_twap(timeline: MarketTimeline, start_ts: int,
                  end_ts: int) -> Optional[float]:
    """Time-weighted prevailing mid over [start_ts, end_ts]."""
    if end_ts <= start_ts:
        raise ValueError("end_ts must exceed start_ts")
    i = timeline.prevailing(start_ts)
    if i is None:
        return None
    total = 0.0
    t = start_ts
    while i + 1 < len(timeline) and timeline.ts[i + 1] < end_ts:
        nxt = max(timeline.ts[i + 1], start_ts)
        total += timeline.mid(i) * (nxt - t)
        t = nxt
        i += 1
    total += timeline.mid(i) * (end_ts - t)
    return total / (end_ts - start_ts)


def spread_and_impact_cost(order: ParentOrder) -> Dict[str, float]:
    """Split executed cost vs fill-time mid into spread + impact (currency)."""
    s = order.sign
    spread = sum(f.qty * f.half_spread_at_fill for f in order.fills)
    exec_vs_mid = sum(s * f.qty * (f.price - f.mid_at_fill)
                      for f in order.fills)
    return {
        "spread_cost": spread,
        "impact_cost": exec_vs_mid - spread,
        "exec_cost_vs_mid": exec_vs_mid,
    }


def adverse_selection(order: ParentOrder,
                      timeline: MarketTimeline) -> Dict[str, Optional[float]]:
    """Mean post-fill markout s*(mid(t+delta) - p_f)/p_f bps per pinned delta
    over the fills whose markout is DEFINED (see module docstring)."""
    return adverse_selection_with_counts(order, timeline)[0]


def adverse_selection_with_counts(
    order: ParentOrder, timeline: MarketTimeline,
) -> Tuple[Dict[str, Optional[float]], Dict[str, int]]:
    """(markout bps per delta or None, number of defined fills per delta)."""
    out: Dict[str, Optional[float]] = {}
    counts: Dict[str, int] = {}
    s = order.sign
    for name, delta in ADVERSE_DELTAS_NS.items():
        vals: List[float] = []
        for f in order.fills:
            t = f.ts + delta
            if f.price > 0 and timeline.mid_defined_at(t, after_ts=f.ts):
                vals.append(1e4 * s * (timeline.mid_at(t) - f.price) / f.price)
        out[name] = sum(vals) / len(vals) if vals else None
        counts[name] = len(vals)
    return out, counts


def impact_regression(
    participation: Sequence[float],
    signed_cost_bps: Sequence[float],
) -> Dict[str, float]:
    """OLS of signed cost bps on participation; slope = impact coefficient."""
    n = len(participation)
    if n != len(signed_cost_bps):
        raise ValueError("length mismatch")
    if n < 3:
        raise ValueError("need at least 3 fills for the impact regression")
    mx = sum(participation) / n
    my = sum(signed_cost_bps) / n
    sxx = sum((x - mx) ** 2 for x in participation)
    if sxx == 0.0:
        return {"slope_bps_per_participation": 0.0, "intercept_bps": my,
                "r2": 0.0, "n": float(n)}
    sxy = sum((x - mx) * (y - my)
              for x, y in zip(participation, signed_cost_bps))
    slope = sxy / sxx
    syy = sum((y - my) ** 2 for y in signed_cost_bps)
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return {"slope_bps_per_participation": slope,
            "intercept_bps": my - slope * mx, "r2": r2, "n": float(n)}


def validate_order_window(order: ParentOrder, timeline: MarketTimeline) -> None:
    """Pinned window rules: decision <= arrival <= end, end inside the
    timeline, every fill inside [arrival_ts, end_ts]."""
    if not order.decision_ts <= order.arrival_ts <= order.end_ts:
        raise ValueError("order needs decision_ts <= arrival_ts <= end_ts")
    last = timeline.last_ts
    if last is None or order.end_ts > last:
        raise ValueError(
            f"order {order.order_id}: end_ts {order.end_ts} is beyond the "
            f"timeline end {last} (end_mid would be fabricated)")
    for f in order.fills:
        if not order.arrival_ts <= f.ts <= order.end_ts:
            raise ValueError(
                f"order {order.order_id}: fill at {f.ts} outside "
                f"[{order.arrival_ts}, {order.end_ts}]")


def order_tca(order: ParentOrder, timeline: MarketTimeline) -> Dict[str, object]:
    """Full per-order TCA record (spec §19 metric table)."""
    validate_order_window(order, timeline)
    m_d = timeline.mid_at(order.decision_ts)
    m_a = timeline.mid_at(order.arrival_ts)
    m_e = timeline.mid_at(order.end_ts)
    if m_d != m_d or m_a != m_a or m_e != m_e:
        raise ValueError("order references time before the first market state")
    perold = perold_decomposition(
        order.sign, order.qty_target,
        [(f.price, f.qty) for f in order.fills], m_d, m_a, m_e)
    split = spread_and_impact_cost(order)
    vwap_mkt = interval_vwap(timeline, order.arrival_ts, order.end_ts)
    twap_mkt = interval_twap(timeline, order.arrival_ts, order.end_ts)
    fv = order.fill_vwap
    s = order.sign
    markouts, n_defined = adverse_selection_with_counts(order, timeline)
    rec: Dict[str, object] = {
        "order_id": order.order_id,
        "instrument_id": order.instrument_id,
        "side": "BUY" if order.side == 0 else "SELL",
        "decision_mid": m_d,
        "arrival_mid": m_a,
        "end_mid": m_e,
        "fill_vwap": fv if order.qty_filled else None,
        "arrival_slippage_bps": arrival_slippage_bps(order, m_a),
        "vwap_slippage_bps": (
            1e4 * s * (fv - vwap_mkt) / vwap_mkt
            if order.qty_filled and vwap_mkt else None),
        "twap_slippage_bps": (
            1e4 * s * (fv - twap_mkt) / twap_mkt
            if order.qty_filled and twap_mkt else None),
        "perold": perold,
        "spread_cost": split["spread_cost"],
        "impact_cost": split["impact_cost"],
        "adverse_selection_bps": markouts,
        "adverse_selection_n": n_defined,
        "n_fills": len(order.fills),
    }
    # execution-alpha attribution: trading cost = spread + impact + timing
    rec["timing_cost"] = perold["trading_cost"] - split["exec_cost_vs_mid"]
    rec["execution_alpha_vs_vwap_bps"] = (
        -rec["vwap_slippage_bps"] if rec["vwap_slippage_bps"] is not None
        else None)
    return rec
