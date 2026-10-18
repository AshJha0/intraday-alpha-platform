"""REL-04c: the pre-specified single-venue execution universe (v1.18).

The rule is fixed in code and in ``research/execution/rel04c/UNIVERSE_RULE.md``
BEFORE any byte of the scan is read; the scan itself runs inside the job
(:mod:`iap.research.rel04c_job`, stage ``select``), never by hand.

Rule ``rel04c_universe/v1`` (selection day 2019-10-18, file
``S101819-v50.txt.gz``, the earliest of the three full-market days on disk):

1. One streaming pass over the day (:func:`measure_day`): per symbol the
   displayed regular-hours volume (ITCH ``E`` + printable ``C`` between system
   events ``Q`` and ``M``, the breadth rule's measure), the closing-cross price
   (``Q`` message, cross type ``C``) and the last non-cross print price
   (``P`` / printable ``C``) as fallback.
2. Eligible: not a test security, not an ETP, Nasdaq-listed (market category
   Q, G or S), issue classification ``C`` (common stock), price >= $5.00,
   displayed regular-hours volume > 0, and a Stock Directory row on each of
   the two later days (2021-07-13, 2021-08-13), read by
   :func:`directory_symbols` from each file's pre-open prefix only (a
   multi-session dataset has one universe and the ingest refuses a symbol
   with no directory row on a session date).
3. Liquidity rank = rank by displayed regular-hours volume among the eligible
   (ties by symbol).  Buckets ``L1`` ranks 1-100, ``L2`` 101-500, ``L3``
   501-1500; ranks beyond 1500 are out.
4. Price buckets on the step-1 price: ``P1`` [$5, $20), ``P2`` [$20, $100),
   ``P3`` >= $100.
5. Anchors AAPL and MSFT are always in (both must be eligible on the day, or
   the selection refuses).  In each of the nine (L, P) cells, excluding the
   anchors, sort by rank and take the names at 0-based positions
   ``floor(n/3)`` and ``floor(2n/3)`` (one name if n == 1, none if n == 0; a
   shortfall is recorded, never filled from another cell).  So at most
   2 + 9 x 2 = 20 names.

Limitations (also in the rule document): one selection day; requiring a
directory row on the later days conditions on survival (and on an unchanged
ticker) to 2021-08-13, a look-ahead that favours survivors -- tolerable for
an execution-cost benchmark, never for an alpha claim; the
liquidity measure is Nasdaq displayed volume, not consolidated volume, so a
name's bucket reflects Nasdaq's share of its trading; the price is one day's
close; the cut points (100/500/1500, $20/$100) are round numbers fixed by
hand, not tuned; the selection day is also one of the three benchmark days
(all three are in_sample, the benchmark is a SECONDARY result).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "ANCHORS",
    "LIQ_BUCKETS",
    "PRICE_BUCKETS",
    "RULE",
    "RULE_VERSION",
    "SELECTION_DATE",
    "SELECTION_FILE",
    "UniverseRefused",
    "directory_symbols",
    "measure_day",
    "price_bucket",
    "liq_bucket",
    "select",
]

RULE_VERSION = "rel04c_universe/v1"
SELECTION_DATE = "2019-10-18"
SELECTION_FILE = "S101819-v50.txt.gz"
ANCHORS = ("AAPL", "MSFT")
#: (label, first rank, last rank), inclusive
LIQ_BUCKETS = (("L1", 1, 100), ("L2", 101, 500), ("L3", 501, 1500))
#: (label, low $ inclusive, high $ exclusive or None)
PRICE_BUCKETS = (("P1", 5.0, 20.0), ("P2", 20.0, 100.0), ("P3", 100.0, None))
MIN_PRICE = 5.0
NASDAQ_CATEGORIES = frozenset("QGS")
PER_CELL = 2
RULE = (
    "one pass over the 2019-10-18 full-market ITCH day; eligible = common stock (issue "
    "classification C), Nasdaq-listed (category Q/G/S), not test, not ETP, price >= $5 "
    "(closing cross, else last print), displayed regular-hours volume > 0; liquidity rank by "
    "displayed regular-hours volume; L1 1-100, L2 101-500, L3 501-1500; P1 [$5,$20), "
    "P2 [$20,$100), P3 >= $100; AAPL and MSFT anchors; per (L,P) cell the names at 0-based "
    "positions floor(n/3) and floor(2n/3) by rank, anchors excluded; at most 20 names; "
    "shortfalls recorded, never back-filled"
)


class UniverseRefused(ValueError):
    """The measurement cannot produce the pre-specified universe."""


def measure_day(path: str | Path, limit_messages: int | None = None) -> dict[str, Any]:
    """One streaming pass: ``{"symbols": {sym: row}, "messages_read": n, ...}``."""
    from iap.marketdata.itch50 import (
        CrossTrade,
        Itch50Reader,
        OrderExecuted,
        OrderExecutedWithPrice,
        StockDirectory,
        SystemEvent,
        Trade,
    )

    reader = Itch50Reader(path, symbols=None, limit_messages=limit_messages)
    by_locate: dict[int, dict[str, Any]] = {}
    regular = False
    m: Any
    for m in reader:
        kind = type(m)
        if kind is SystemEvent:
            if m.event_code == "Q":
                regular = True
            elif m.event_code == "M":
                regular = False
            continue
        if kind is StockDirectory:
            by_locate[m.locate] = {
                "symbol": m.stock.strip(),
                "displayed_volume": 0,
                "close_cross_price_e4": None,
                "last_print_price_e4": None,
                "etp": m.etp_flag == "Y",
                "test_security": m.authenticity == "T",
                "market_category": m.market_category,
                "issue_classification": m.issue_classification,
            }
            continue
        row = by_locate.get(m.locate)
        if row is None:
            continue
        if kind is OrderExecuted:
            if regular:
                row["displayed_volume"] += m.shares
        elif kind is OrderExecutedWithPrice:
            if m.printable == "Y":
                row["last_print_price_e4"] = m.price
                if regular:
                    row["displayed_volume"] += m.shares
        elif kind is Trade:
            row["last_print_price_e4"] = m.price
        elif kind is CrossTrade and m.cross_type == "C":
            row["close_cross_price_e4"] = m.price
    return {
        "symbols": {r["symbol"]: r for r in by_locate.values()},
        "messages_read": reader.messages_read,
        "limit_reached": reader.limit_reached,
    }


def directory_symbols(path: str | Path) -> set[str]:
    """Symbols with a Stock Directory row, reading only up to the first
    system event ``Q`` (start of market hours): the directory is sent before
    the open, so no trading message of the day is examined."""
    from iap.marketdata.itch50 import Itch50Reader, StockDirectory, SystemEvent

    out: set[str] = set()
    m: Any
    for m in Itch50Reader(path, symbols=None):
        if type(m) is StockDirectory:
            out.add(m.stock.strip())
        elif type(m) is SystemEvent and m.event_code == "Q":
            break
    return out


def _price(row: Mapping[str, Any]) -> float | None:
    p = row.get("close_cross_price_e4")
    if p is None:
        p = row.get("last_print_price_e4")
    return None if p is None else float(p) / 10_000.0


def price_bucket(price: float | None) -> str | None:
    if price is None:
        return None
    for label, lo, hi in PRICE_BUCKETS:
        if price >= lo and (hi is None or price < hi):
            return label
    return None


def liq_bucket(rank: int) -> str | None:
    for label, lo, hi in LIQ_BUCKETS:
        if lo <= rank <= hi:
            return label
    return None


def _eligible(row: Mapping[str, Any]) -> bool:
    p = _price(row)
    return (
        not row["test_security"]
        and not row["etp"]
        and row["market_category"] in NASDAQ_CATEGORIES
        and row["issue_classification"] == "C"
        and p is not None
        and p >= MIN_PRICE
        and row["displayed_volume"] > 0
    )


def _pick(cell: list[dict[str, Any]]) -> list[dict[str, Any]]:
    n = len(cell)
    if n == 0:
        return []
    if n == 1:
        return [cell[0]]
    i, j = n // 3, (2 * n) // 3
    return [cell[i], cell[j]] if i != j else [cell[i]]


def select(
    measured: Mapping[str, Any],
    *,
    source_file: str = SELECTION_FILE,
    source_sha256: str | None = None,
    anchors: Iterable[str] = ANCHORS,
    present_later: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Apply rule v1 to a :func:`measure_day` result; the universe document.

    ``present_later`` maps each later session date to the symbols with a
    directory row that day (:func:`directory_symbols`); required by the rule."""
    if measured.get("limit_reached"):
        raise UniverseRefused("the measurement hit --limit-messages: a smoke pass, not a selection")
    if not present_later:
        raise UniverseRefused("present_later (directory rows of the later days) is required")
    later = {d: set(v) for d, v in present_later.items()}
    absent = sorted(
        r["symbol"]
        for r in measured["symbols"].values()
        if _eligible(r) and not all(r["symbol"] in v for v in later.values())
    )
    rows = sorted(
        (
            dict(r)
            for r in measured["symbols"].values()
            if _eligible(r) and all(r["symbol"] in v for v in later.values())
        ),
        key=lambda r: (-int(r["displayed_volume"]), r["symbol"]),
    )
    for rank, r in enumerate(rows, start=1):
        r["rank"] = rank
        r["price"] = _price(r)
        r["liq_bucket"] = liq_bucket(rank)
        r["price_bucket"] = price_bucket(r["price"])
    by_sym = {r["symbol"]: r for r in rows}
    anchors = tuple(anchors)
    missing = [a for a in anchors if a not in by_sym or by_sym[a]["liq_bucket"] is None]
    if missing:
        raise UniverseRefused(f"anchor(s) {missing} not eligible / not ranked on the selection day")
    chosen: list[dict[str, Any]] = [{**by_sym[a], "role": "anchor"} for a in anchors]
    cells: dict[str, dict[str, Any]] = {}
    for lq, _, _ in LIQ_BUCKETS:
        for pb, _, _ in PRICE_BUCKETS:
            cell = [
                r
                for r in rows
                if r["liq_bucket"] == lq and r["price_bucket"] == pb and r["symbol"] not in anchors
            ]
            picked = _pick(cell)
            cells[f"{lq}{pb}"] = {
                "n_candidates": len(cell),
                "picked": [r["symbol"] for r in picked],
                "shortfall": PER_CELL - len(picked),
            }
            chosen.extend({**r, "role": "cell"} for r in picked)
    keep = (
        "symbol",
        "rank",
        "price",
        "liq_bucket",
        "price_bucket",
        "displayed_volume",
        "role",
        "market_category",
    )
    universe = [{k: r[k] for k in keep} for r in chosen]
    return {
        "schema": "iap.rel04c_universe/v1",
        "rule_version": RULE_VERSION,
        "rule": RULE,
        "selection_date": SELECTION_DATE,
        "source_file": source_file,
        "source_sha256": source_sha256,
        "measured_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "messages_read": measured.get("messages_read"),
        "n_symbols_with_directory": len(measured["symbols"]),
        "n_eligible": len(rows),
        "later_sessions": sorted(later),
        "n_dropped_absent_later": len(absent),
        "cells": cells,
        "symbols": [r["symbol"] for r in universe],
        "buckets": {
            lq: [r["symbol"] for r in universe if r["liq_bucket"] == lq] for lq, _, _ in LIQ_BUCKETS
        },
        "universe": universe,
        "survivorship_note": (
            "fixed on 2019-10-18; a name missing on 2021-07-13 or 2021-08-13 is reported "
            "missing for that day and never replaced"
        ),
    }
