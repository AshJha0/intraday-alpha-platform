#!/usr/bin/env python3
"""Generate tests/golden/expected_markout.json (x-version 1).

The markout golden (API_PORTFOLIO_TCA.md §2.7; iap.tca.markout is the
reference, com.iap.tca.Markout the Java port). Inputs are written into the
file so every port consumes exactly the same numbers:

- ``states``: raw BBO states ``[ts, bid, ask, bid_sz, ask_sz]`` of a 400 s
  timeline built from a SplitMix64 walk (one state per second, the mid moves
  by -1 / 0 / +1 cent, the spread is 2 or 4 cents) with a quote gap of 20 s;
- ``gates``: the gate timestamps (a HALT and the start of the quote gap);
- ``fills``: 40 fills ``[ts, price, qty, side, liquidity, venue_id, algo,
  qty_unit]`` — takers at the far touch, makers at the near touch one
  nanosecond after a state, plus the edge cases: a fill before the first
  quote, a fill at the last state, a fill just before the HALT and one
  inside the quote gap;
- ``passive_orders``: 14 rested orders ``[qty, filled_qty, rest_ts,
  first_fill_ts, last_fill_ts, entry_ahead_qty]``.

``expected.report`` is ``markout_report(fills, timeline, min_fills=3,
bucket_ns=100 s, session_start_ts=T0)`` over the default horizons;
``expected.per_fill`` the four per-unit measures of every fill at every
horizon (null = undefined); ``expected.passive`` is
``passive_order_stats(orders, min_orders=3)``. Tolerance 1e-9, nulls exact.

Usage: PYTHONPATH=src python3 tools/make_golden_markout.py [--force]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from iap.core.rng import SplitMix64
from iap.tca.fills import MAKER, TAKER, MarketTimeline
from iap.tca.markout import (
    DEFAULT_HORIZONS_NS,
    MarkoutFill,
    PassiveOrder,
    fill_measures,
    markout_report,
    passive_order_stats,
)

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "expected_markout.json"
T0 = 1_787_578_200_000_000_000  # golden session t0
SEC = 1_000_000_000
SEED = 20260829
MIN_FILLS = 3
BUCKET_NS = 100 * SEC
HALT_AT = T0 + 150 * SEC
GAP = (250, 270)  # seconds without a two-sided quote


def inputs() -> dict:
    rng = SplitMix64(SEED)
    states = []
    mid_cents = 10_000
    for k in range(401):
        mid_cents += rng.randint(-1, 1)
        hs_cents = 1 if rng.uniform() < 0.7 else 2
        if GAP[0] <= k < GAP[1]:
            continue
        ts = T0 + k * SEC
        states.append(
            [
                ts,
                (mid_cents - hs_cents) / 100.0,
                (mid_cents + hs_cents) / 100.0,
                100 * rng.randint(1, 9),
                100 * rng.randint(1, 9),
            ]
        )
    by_ts = {s[0]: s for s in states}
    fills = []
    algos = ("TWAP", "VWAP", "POV", "IS")
    for j in range(36):
        k = 5 + 10 * j
        if GAP[0] <= k < GAP[1]:
            k = GAP[0] - 1
        s = by_ts[T0 + k * SEC]
        side = rng.randint(0, 1)
        maker = rng.uniform() < 0.5
        qty = 10 * rng.randint(1, 30)
        if maker:  # rested at the near touch, hit just after the state
            price = s[1] if side == 0 else s[2]
            ts = s[0] + 1
        else:  # crossed to the far touch
            price = s[2] if side == 0 else s[1]
            ts = s[0]
        fills.append(
            [ts, price, qty, side, MAKER if maker else TAKER, 1 + j % 3, algos[j % 4], 1.0]
        )
    last = states[-1]
    fills += [
        [T0 - SEC, 100.0, 100, 0, TAKER, 1, "TWAP", 1.0],  # before the first quote
        [last[0], last[2], 100, 0, TAKER, 2, "IS", 1.0],  # at the session end
        [HALT_AT - 2 * SEC, 100.0, 50, 1, MAKER, 3, "POV", 1000.0],  # halt 2 s later
        [T0 + 260 * SEC, 100.0, 50, 0, TAKER, 1, "VWAP", 1.0],  # inside the quote gap
    ]
    orders = []
    for j in range(14):
        qty = 100 * rng.randint(1, 5)
        ahead = (0, 0, 300, 500, 1200, 2000, 5000)[j % 7]
        rest = T0 + (10 + 20 * j) * SEC
        outcome = rng.randint(0, 2)  # 0 unfilled, 1 partial, 2 full
        if outcome == 0:
            orders.append([qty, 0, rest, None, None, ahead])
        else:
            first = rest + rng.randint(1, 30) * SEC
            last_ts = first + rng.randint(0, 20) * SEC
            filled = qty if outcome == 2 else qty // 2
            orders.append([qty, filled, rest, first, last_ts, ahead])
    return {
        "states": states,
        "gates": [HALT_AT, T0 + GAP[0] * SEC],
        "fills": fills,
        "passive_orders": orders,
    }


def build(doc: dict) -> tuple[MarketTimeline, list[MarkoutFill], list[PassiveOrder]]:
    """Timeline, fills and passive orders from the golden's input blocks."""
    tl = MarketTimeline()
    for ts, bid, ask, bsz, asz in doc["states"]:
        tl.append_state_pinned(ts, bid, ask, bsz, asz)
    for g in doc["gates"]:
        tl.add_halt(g)
    fills = [MarkoutFill(*row) for row in doc["fills"]]
    orders = [PassiveOrder(*row) for row in doc["passive_orders"]]
    return tl, fills, orders


def expected(doc: dict) -> dict:
    tl, fills, orders = build(doc)
    per_fill = []
    for f in fills:
        row = {}
        for name, h in DEFAULT_HORIZONS_NS.items():
            row[name] = fill_measures(f, tl, h)
        per_fill.append(row)
    return {
        "per_fill": per_fill,
        "report": markout_report(
            fills, tl, min_fills=MIN_FILLS, bucket_ns=BUCKET_NS, session_start_ts=T0
        ),
        "passive": passive_order_stats(orders, min_orders=MIN_FILLS),
    }


def document() -> dict:
    doc = {
        "x-version": 1,
        "description": (
            "Markout golden (API_PORTFOLIO_TCA.md section 2.7; iap.tca.markout is the "
            "reference, generated by python/tools/make_golden_markout.py). states = raw "
            "BBO [ts, bid, ask, bid_sz, ask_sz]; gates = timestamps at which a halt or a "
            "no-quote gap starts; fills = [ts, price, qty, side, liquidity, venue_id, "
            "algo, qty_unit]; passive_orders = [qty, filled_qty, rest_ts, first_fill_ts, "
            "last_fill_ts, entry_ahead_qty]. expected.per_fill: markout / effective / "
            "realised half-spread / price impact per unit of price for every fill and "
            "horizon (null = undefined: no quote yet, timeline end, or a gate inside "
            "the window). expected.report: markout_report(min_fills 3, bucket_ns 100 s, "
            "session_start_ts t0); expected.passive: passive_order_stats(min_orders 3). "
            "Every port must match to `tolerance`, nulls exactly."
        ),
        "tolerance": 1e-9,
        "t0": T0,
        "min_fills": MIN_FILLS,
        "bucket_ns": BUCKET_NS,
    }
    doc.update(inputs())
    doc["expected"] = expected(doc)
    return doc


def main() -> int:
    if GOLDEN.exists() and "--force" not in sys.argv:
        print(
            f"{GOLDEN.name} already exists — pinned; pass --force after a "
            "schemas/MIGRATIONS.md entry"
        )
        return 1
    doc = document()
    GOLDEN.write_bytes((json.dumps(doc, indent=1) + "\n").encode("utf-8"))
    print(f"wrote {GOLDEN} ({len(doc['fills'])} fills)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
