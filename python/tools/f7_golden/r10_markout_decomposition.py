"""F7 rule 10: B6 markout decomposition (``iap.tca.markout_decomposition``:
``fill_type``, ``regime_of``, ``fill_rows``, ``cluster_mean`` with the
day-clustered CR1 standard error, ``decompose``, ``markout_decomposition``).

The Student-t readings of ``cluster_mean`` (``p_student_t``, ``ci95``) come
from SciPy in the Python reference; they are kept under ``expected_tol`` and
compared to ``tolerance``, every other field byte for byte."""

from __future__ import annotations

import copy

from iap.execution.types import ChildOrder, Fill, Liquidity, OrderType
from iap.tca.fills import MarketTimeline
from iap.tca.markout_decomposition import (
    cluster_mean,
    decompose,
    fill_rows,
    fill_type,
    markout_decomposition,
    regime_of,
)

from . import SEC, SLICE_PROVENANCE, SLICE_TICK, base_document
from ._slice import TIMELINE_RULE, samples, timeline
from .r08_fading import build_timeline, fixed_timeline

NAME = "expected_f7_markout_decomposition.json"
T0 = 1_787_578_200_000_000_000
TOL_KEYS = ("p_student_t", "ci95")
HORIZONS = {"100ms": 100_000_000, "1s": SEC, "5s": 5 * SEC, "30s": 30 * SEC}

CLUSTER_CASES = [
    {"values": [1.0, 2.0, 3.0, 4.0], "clusters": ["a", "a", "b", "b"]},
    {"values": [1.0, 2.0], "clusters": ["a", "a"]},
    {"values": [], "clusters": []},
    {"values": [5.0, 5.0, 5.0, 5.0], "clusters": ["a", "b", "c", "d"]},
    {
        "values": [0.3, -0.2, 1.7, 2.2, -3.1, 0.05, 0.9],
        "clusters": ["d1", "d2", "d1", "d3", "d2", "d3", "d4"],
    },
    {"values": [-1e-4, 2e-4, 3e-5], "clusters": ["x", "y", "z"]},
    {"values": [1.0, 2.0, 3.0], "clusters": ["a", "b"]},  # error
]


def split_tol(doc):
    """``(doc without TOL_KEYS, the TOL_KEYS subtree)``, recursively."""
    if isinstance(doc, dict):
        plain, tol = {}, {}
        for k, v in doc.items():
            if k in TOL_KEYS:
                tol[k] = v
                continue
            p, t = split_tol(v)
            plain[k] = p
            if t not in ({}, [], None):
                tol[k] = t
        return plain, tol
    if isinstance(doc, list):
        plain, tol = [], []
        any_tol = False
        for v in doc:
            p, t = split_tol(v)
            plain.append(p)
            tol.append(t)
            if t not in ({}, [], None):
                any_tol = True
        return plain, (tol if any_tol else [])
    return doc, None


def order_row(o: ChildOrder) -> dict:
    return {
        "order_id": o.order_id,
        "cancel_rule": o.cancel_rule,
        "cancel_rule_ts": o.cancel_rule_ts,
        "replaces_order_id": o.replaces_order_id,
        "urgency_regime": o.urgency_regime,
    }


def fill_row(f: Fill) -> list:
    return [
        f.fill_id,
        f.order_id,
        f.parent_id,
        f.instrument_id,
        f.venue_id,
        f.side,
        f.price_ticks,
        f.qty,
        f.ts,
        f.liquidity.name,
        f.fee,
        f.impact_cost,
    ]


def fill_from_row(r: list) -> Fill:
    return Fill(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], Liquidity[r[9]], r[10], r[11])


def order_from_row(r: dict) -> ChildOrder:
    o = ChildOrder(type=OrderType.LIMIT)
    o.order_id = r["order_id"]
    o.cancel_rule = r["cancel_rule"]
    o.cancel_rule_ts = r["cancel_rule_ts"]
    o.replaces_order_id = r["replaces_order_id"]
    o.urgency_regime = r["urgency_regime"]
    return o


def fixed_orders_and_fills() -> tuple[list[dict], list[list]]:
    regimes = ("", "agree", "adverse", "neutral", "no_signal", "stale", "plain")
    orders = []
    fills = []
    fid = 1
    for i in range(1, 31):
        o = ChildOrder(type=OrderType.LIMIT)
        o.order_id = i
        o.urgency_regime = regimes[i % 7]
        if i % 4 == 1:
            o.cancel_rule = "fade:signal" if i % 8 == 1 else "queue:ahead"
            o.cancel_rule_ts = T0 + (3 * i + 1) * SEC
        if i % 5 == 2:
            o.replaces_order_id = i - 1
        orders.append(order_row(o))
        side = i % 2
        ts = T0 + 3 * i * SEC + (1 if i % 3 else 0)
        maker = i % 3 != 0
        px = 10000 + (i % 5) - 2
        fills.append(
            fill_row(
                Fill(
                    fid,
                    i,
                    1 + i % 2,
                    7,
                    1,
                    side,
                    px,
                    100 * (1 + i % 4),
                    ts,
                    Liquidity.MAKER if maker else Liquidity.TAKER,
                    0.0,
                    0.0,
                )
            )
        )
        fid += 1
        if i % 4 == 1:  # a second fill after the pull cancel was sent (faded)
            fills.append(
                fill_row(
                    Fill(
                        fid,
                        i,
                        1 + i % 2,
                        7,
                        1,
                        side,
                        px,
                        50,
                        o.cancel_rule_ts + 500,
                        Liquidity.MAKER,
                        0.0,
                        0.0,
                    )
                )
            )
            fid += 1
    # a fill before the first quote and one past the timeline end (undefined measures)
    fills.append(fill_row(Fill(fid, 1, 1, 7, 1, 0, 10001, 10, T0 - SEC, Liquidity.TAKER, 0.0, 0.0)))
    fills.append(
        fill_row(Fill(fid + 1, 2, 2, 7, 1, 1, 10001, 10, T0 + 199 * SEC, Liquidity.MAKER, 0.0, 0.0))
    )
    return orders, fills


def run(
    tl: MarketTimeline,
    orders_doc,
    fills_doc,
    tick,
    qty_unit,
    horizons,
    day_ns,
    cluster=None,
    default_horizons=True,
) -> dict:
    orders = {r["order_id"]: order_from_row(r) for r in orders_doc}
    fills = [fill_from_row(r) for r in fills_doc]
    rows = fill_rows(
        fills,
        orders,
        tl,
        tick,
        qty_unit=qty_unit,
        horizons_ns=horizons,
        cluster=cluster,
        day_ns=day_ns,
    )
    out = {
        "types": [fill_type(f, orders[f.order_id]) for f in fills],
        "regimes": [regime_of(orders[f.order_id]) for f in fills],
        "rows": rows,
        "markout_decomposition": markout_decomposition(
            fills, orders, tl, tick, qty_unit=qty_unit, horizons_ns=horizons, day_ns=day_ns
        ),
    }
    if default_horizons:
        out["decomposition_default_horizons"] = decompose(rows)
    assert out["markout_decomposition"] == decompose(rows, list(horizons))
    return out


def expected_fixed(tl_doc, orders_doc, fills_doc) -> tuple[dict, dict]:
    tl = build_timeline(tl_doc)
    cl = []
    for c in CLUSTER_CASES:
        try:
            cl.append(cluster_mean(c["values"], c["clusters"]))
        except ValueError as exc:
            cl.append({"error": str(exc)})
    full = {
        "cluster_mean": cl,
        "day_clusters": run(tl, orders_doc, fills_doc, 0.01, 1.0, HORIZONS, 100 * SEC),
        "one_cluster": run(
            tl, orders_doc, fills_doc, 0.01, 100.0, HORIZONS, 86_400 * SEC, cluster="s"
        ),
    }
    return split_tol(copy.deepcopy(full))


def slice_block() -> tuple[dict, dict, dict]:
    tl = timeline()
    regimes = ("plain", "agree", "adverse", "neutral", "", "stale")
    orders = []
    fills = []
    for i, s in enumerate(samples(), start=1):
        o = ChildOrder(type=OrderType.LIMIT)
        o.order_id = i
        o.urgency_regime = regimes[i % 6]
        if i % 5 == 0:
            o.cancel_rule = "fade:pressure"
            o.cancel_rule_ts = s["ts"] - 1
        if i % 7 == 3:
            o.replaces_order_id = i - 1
        orders.append(order_row(o))
        side = i % 2
        maker = i % 3 != 0
        if maker:
            px = s["bid"] if side == 0 else s["ask"]
        else:
            px = s["ask"] if side == 0 else s["bid"]
        fills.append(
            fill_row(
                Fill(
                    i,
                    i,
                    1,
                    1,
                    101,
                    side,
                    px,
                    100 * (1 + i % 3),
                    s["ts"] + (1 if maker else 0),
                    Liquidity.MAKER if maker else Liquidity.TAKER,
                    0.0,
                    0.0,
                )
            )
        )
    day_ns = 30 * SEC
    full = run(tl, orders, fills, SLICE_TICK, 1.0, HORIZONS, day_ns, default_horizons=False)
    plain, tol = split_tol(full)
    inputs = dict(
        SLICE_PROVENANCE,
        timeline_rule=TIMELINE_RULE,
        orders=orders,
        fills=fills,
        horizons=HORIZONS,
        tick_size=SLICE_TICK,
        qty_unit=1.0,
        day_ns=day_ns,
        rule="fill_rows / decompose / markout_decomposition over the planted fills with clusters of 30 s (day_ns)",
    )
    return inputs, plain, tol


def document() -> dict:
    doc = base_document(
        "F7 rule 10 golden: B6 markout decomposition (iap.tca.markout_decomposition; "
        "API_TRADING.md section 2.16). cluster_mean cases; `day_clusters` = fill_rows / "
        "decompose / markout_decomposition over the fixed fills ([fill_id, order_id, "
        "parent_id, instrument_id, venue_id, side, price_ticks, qty, ts, liquidity, fee, "
        "impact_cost]) and orders (B4/B5/B2 provenance fields) on the fixed timeline of "
        "expected_f7_fading.json, tick 0.01, qty_unit 1, day_ns 100 s; `one_cluster` = the "
        "same at qty_unit 100 with cluster 's' and day_ns one day. The SciPy-derived "
        "Student-t fields (p_student_t, ci95) live under expected_tol (same tree shape, "
        "compared to tolerance); everything else byte for byte.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r10_markout_decomposition --force",
        tolerance=1e-9,
    )
    tl_doc = fixed_timeline()
    orders_doc, fills_doc = fixed_orders_and_fills()
    plain, tol = expected_fixed(tl_doc, orders_doc, fills_doc)
    sl_inputs, sl_plain, sl_tol = slice_block()
    doc["inputs"] = {
        "cluster_mean": CLUSTER_CASES,
        "timeline": tl_doc,
        "orders": orders_doc,
        "fills": fills_doc,
        "horizons": HORIZONS,
        "slice": sl_inputs,
    }
    doc["expected"] = dict(plain, slice=sl_plain)
    doc["expected_tol"] = dict(tol, slice=sl_tol)
    return doc
