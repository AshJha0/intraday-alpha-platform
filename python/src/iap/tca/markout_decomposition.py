"""Markout decomposition by fill type and regime, day-clustered (v1.17 WS-B B6).

:mod:`iap.tca.markout` prices every fill the same way and splits the table
by liquidity, venue, algo, side and time bucket, with an i.i.d. standard
error it labels a lower bound. This module adds the two splits the v1.17
execution work needs and the statistic the v1.17 gates use:

**Fill type** (:func:`fill_type`, :data:`FILL_TYPES`), from the fill and the
child order that produced it (``ChildOrder`` provenance fields set by the
replay):

- ``aggressive`` — a TAKER fill (the child crossed: MARKET, or a LIMIT that
  walked the opposite side);
- ``faded`` — a MAKER fill that landed AFTER a B4 / B5 pull cancel had been
  sent for the child (``cancel_rule`` set, ``fill.ts >= cancel_rule_ts``):
  the cancel lost the race (simulator rule 7); these are the fills a fade
  tried to avoid and did not;
- ``cancelled_replaced`` — a MAKER fill of a child that re-sent the
  remainder of a cancelled one (``replaces_order_id != 0``: a reprice, a
  re-post after a pull, a held quantity put back);
- ``passive`` — every other MAKER fill (a first post that rested and filled);
- ``dark`` (v1.17 B10) — a fill on a dark / midpoint venue
  (``Fill.provenance == "dark_midpoint"``, tested first): MODELLED, not
  measured (:mod:`iap.execution.dark`); its ``price_impact`` row, negated,
  is the adverse selection of the dark fill the B10 report quotes.

**Regime** (:func:`regime_of`): the child's B2 ``urgency_regime`` (``agree``
/ ``adverse`` / ``neutral`` / ``no_signal`` / ``stale``), ``plain`` for a
child of a parent without a signal opt-in.

**Statistic.** Per cell and horizon the equal-weight mean of the fills'
markout (``s * (m_h - p)`` in bps of the fill price, the
:mod:`iap.tca.markout` sign: positive = the fill looks good ``h`` later) and
of their price impact (``s * (m_h - m_f)``; its negative is the adverse
selection of a passive fill), each with the **day-clustered** standard
error of the v1.17 gate form (``metrics.gate_statistics`` / the B1
benchmark: one cluster per trading day, the CR1 sandwich ``G / (G - 1) *
sum_g (sum_{i in g} (x_i - mean))^2 / N^2``, Student-t with ``G - 1``
degrees of freedom through
:func:`iap.validation.metrics.day_cluster_inference`). Fills of one day are
never treated as independent; with one day the SE is ``None``.

Two steps so a caller can pool sessions: :func:`fill_rows` prices the fills
of ONE timeline (one session and instrument) into plain rows, each tagged
with its cluster (the fill's day on ``day_ns`` by default, or a caller
label such as the session), and :func:`decompose` aggregates any list of
rows. Plain loops throughout (the C++ port, F7, keeps the shape).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from iap.execution.types import ChildOrder, Fill, Liquidity
from iap.tca.fills import MAKER, TAKER, MarketTimeline
from iap.tca.markout import DEFAULT_HORIZONS_NS, MarkoutFill, fill_measures

FILL_TYPES: tuple[str, ...] = ("passive", "aggressive", "faded", "cancelled_replaced", "dark")
DAY_NS = 86_400_000_000_000
_MEASURES = ("markout", "price_impact")


def fill_type(fill: Fill, order: ChildOrder) -> str:
    """One of :data:`FILL_TYPES` for ``fill`` of ``order`` (module docstring)."""
    if fill.provenance == "dark_midpoint":
        return "dark"
    if fill.liquidity == Liquidity.TAKER:
        return "aggressive"
    if order.cancel_rule and fill.ts >= order.cancel_rule_ts:
        return "faded"
    if order.replaces_order_id != 0:
        return "cancelled_replaced"
    return "passive"


def regime_of(order: ChildOrder) -> str:
    """The child's B2 urgency regime, ``"plain"`` without a signal opt-in."""
    return order.urgency_regime or "plain"


def fill_rows(
    fills: Sequence[Fill],
    orders: Mapping[int, ChildOrder],
    timeline: MarketTimeline,
    tick_size: float,
    *,
    qty_unit: float = 1.0,
    horizons_ns: Mapping[str, int] | None = None,
    cluster: str | None = None,
    day_ns: int = DAY_NS,
    algo: str = "",
) -> list[dict[str, object]]:
    """One row per fill: ``fill_type``, ``regime``, ``cluster``, ``side``,
    ``ts``, ``price``, ``qty`` and, per horizon ``h``, ``markout_bps[h]`` /
    ``price_impact_bps[h]`` (``None`` where undefined: no mid yet, past the
    end of the timeline, or across a gate — :func:`iap.tca.markout.fill_measures`).
    ``cluster`` defaults to the fill's day (``ts // day_ns``)."""
    hz = dict(DEFAULT_HORIZONS_NS if horizons_ns is None else horizons_ns)
    rows: list[dict[str, object]] = []
    for f in fills:
        o = orders[f.order_id]
        price = float(f.price_ticks) * tick_size
        mf = MarkoutFill(
            ts=f.ts,
            price=price,
            qty=f.qty,
            side=f.side,
            liquidity=TAKER if f.liquidity == Liquidity.TAKER else MAKER,
            venue_id=f.venue_id,
            algo=algo,
            qty_unit=qty_unit,
        )
        mk: dict[str, float | None] = {}
        pi: dict[str, float | None] = {}
        for name, h in hz.items():
            x = fill_measures(mf, timeline, h)
            if x is None:
                mk[name] = None
                pi[name] = None
            else:
                mk[name] = 1e4 * x["markout"] / price
                pi[name] = 1e4 * x["price_impact"] / price
        rows.append(
            {
                "fill_type": fill_type(f, o),
                "regime": regime_of(o),
                "cluster": str(f.ts // day_ns) if cluster is None else cluster,
                "side": f.side,
                "ts": f.ts,
                "price": price,
                "qty": f.qty,
                "markout_bps": mk,
                "price_impact_bps": pi,
            }
        )
    return rows


def cluster_mean(values: Sequence[float], clusters: Sequence[str]) -> dict[str, object]:
    """Mean of ``values`` with the day-clustered (CR1) standard error, one
    cluster per distinct label (module docstring). ``se`` / ``t`` / ``ci95``
    are ``None`` with fewer than two clusters; ``p_student_t`` has ``G - 1``
    degrees of freedom."""
    n = len(values)
    if n != len(clusters):
        raise ValueError("values and clusters must have the same length")
    out: dict[str, object] = {
        "n": n,
        "n_clusters": 0,
        "mean": None,
        "se": None,
        "t": None,
        "df": None,
        "p_student_t": None,
        "ci95": None,
    }
    if n == 0:
        return out
    total = 0.0
    for v in values:
        total += v
    mean = total / n
    out["mean"] = mean
    sums: dict[str, float] = {}
    for v, c in zip(values, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + (v - mean)
    g = len(sums)
    out["n_clusters"] = g
    if g < 2:
        return out
    ss = 0.0
    for c_sum in sums.values():
        ss += c_sum * c_sum
    se = math.sqrt(g / (g - 1) * ss / float(n * n))
    out["se"] = se
    out["df"] = g - 1
    if se > 0.0:
        from iap.validation.metrics import day_cluster_inference

        t = mean / se
        inf = day_cluster_inference(t, g)
        out["t"] = t
        out["p_student_t"] = inf["p_student_t"]
        half = inf["crit_student_t"] * se
        out["ci95"] = [mean - half, mean + half]
    else:
        out["ci95"] = [mean, mean]
    return out


def _cell(rows: Sequence[Mapping[str, object]], horizons: Sequence[str]) -> dict[str, object]:
    qty = 0
    for r in rows:
        qty += int(r["qty"])  # type: ignore[call-overload]
    out: dict[str, object] = {"n_fills": len(rows), "qty": qty, "horizons": {}}
    for name in horizons:
        hrow: dict[str, object] = {}
        for m in _MEASURES:
            vals: list[float] = []
            cl: list[str] = []
            for r in rows:
                v = r[f"{m}_bps"][name]  # type: ignore[index]
                if v is None:
                    continue
                vals.append(float(v))
                cl.append(str(r["cluster"]))
            hrow[m] = cluster_mean(vals, cl)
        out["horizons"][name] = hrow  # type: ignore[index]
    return out


def decompose(
    rows: Sequence[Mapping[str, object]], horizons: Sequence[str] | None = None
) -> dict[str, object]:
    """Aggregate :func:`fill_rows` output: ``all``, ``by_fill_type``,
    ``by_regime`` and ``by_fill_type_and_regime`` (key ``"type|regime"``),
    each a cell of ``n_fills``, ``qty`` and per horizon the
    :func:`cluster_mean` of ``markout`` and ``price_impact`` (bps); plus the
    qty share of each fill type. Keys appear only for types / regimes seen."""
    if horizons is None:
        horizons = list(rows[0]["markout_bps"]) if rows else []  # type: ignore[arg-type]
    hz = list(horizons)
    by_type: dict[str, list] = {}
    by_regime: dict[str, list] = {}
    by_both: dict[str, list] = {}
    for r in rows:
        ft = str(r["fill_type"])
        rg = str(r["regime"])
        by_type.setdefault(ft, []).append(r)
        by_regime.setdefault(rg, []).append(r)
        by_both.setdefault(f"{ft}|{rg}", []).append(r)
    total_qty = 0
    for r in rows:
        total_qty += int(r["qty"])  # type: ignore[call-overload]
    share: dict[str, float] = {}
    for ft in FILL_TYPES:
        if ft in by_type and total_qty > 0:
            q = 0
            for r in by_type[ft]:
                q += int(r["qty"])  # type: ignore[call-overload]
            share[ft] = q / total_qty
    return {
        "horizons": hz,
        "inference": "equal-weight mean per fill, CR1 SE clustered by day, Student-t G-1 df",
        "fill_types": list(FILL_TYPES),
        "all": _cell(rows, hz),
        "fill_type_share_of_qty": share,
        "by_fill_type": {k: _cell(by_type[k], hz) for k in FILL_TYPES if k in by_type},
        "by_regime": {k: _cell(by_regime[k], hz) for k in sorted(by_regime)},
        "by_fill_type_and_regime": {k: _cell(by_both[k], hz) for k in sorted(by_both)},
    }


def markout_decomposition(
    fills: Sequence[Fill],
    orders: Mapping[int, ChildOrder],
    timeline: MarketTimeline,
    tick_size: float,
    *,
    qty_unit: float = 1.0,
    horizons_ns: Mapping[str, int] | None = None,
    day_ns: int = DAY_NS,
) -> dict[str, object]:
    """:func:`decompose` of :func:`fill_rows` for one timeline (one session)."""
    hz = dict(DEFAULT_HORIZONS_NS if horizons_ns is None else horizons_ns)
    rows = fill_rows(
        fills, orders, timeline, tick_size, qty_unit=qty_unit, horizons_ns=hz, day_ns=day_ns
    )
    return decompose(rows, list(hz))
