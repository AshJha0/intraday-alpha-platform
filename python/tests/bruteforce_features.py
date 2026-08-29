"""Independent brute-force feature recomputation helpers (test-only).

Deliberately NOT built on the feature engine: book state comes straight from
the reference OrderBook, window statistics from pandas over per-event
frames.  Used to validate the engine's incremental arithmetic at 1e-9.
"""

from __future__ import annotations

import math
from typing import List

import pandas as pd

from iap.core.events import EventType, MarketEvent
from iap.orderbook.book import OrderBook

NS = 1_000_000_000

_TOUCH = (EventType.ADD, EventType.MODIFY, EventType.CANCEL,
          EventType.EXECUTE, EventType.QUOTE)


def is_book_touch(ev: MarketEvent) -> bool:
    """Book-refresh trigger, mirroring the pinned engine rule."""
    return ev.event_type in _TOUCH or (
        ev.event_type == EventType.SNAPSHOT and ev.trade_id == 0
    )


def book_frames(events: List[MarketEvent], depth_levels: int = 10):
    """(book_df, trade_df) built from a single-venue reference OrderBook.

    book_df has one row per book-refresh event: ts, best bid/ask price+size,
    per-level depth sums (b1,b3,b5,b10 / a1,a3,a5,a10), spread, and the
    pinned per-event L1/L3/L5/L10 OFI contributions and L1 queue deltas.
    trade_df has one row per TRADE: ts, signed qty, qty, price_ticks, and
    the mid prevailing at the trade (NaN if one-sided book).
    """
    book = OrderBook(events[0].instrument_id, 0)
    rows, trows = [], []
    prev_bid: List = []
    prev_ask: List = []
    have_prev = False
    for n, ev in enumerate(events, start=1):
        if is_book_touch(ev):
            book.apply(ev)
            bid = book.depth(0, depth_levels)
            ask = book.depth(1, depth_levels)
            row = {"ts": ev.exchange_ts, "n": n}
            row["bp"] = bid[0][0] if bid else None
            row["bq"] = bid[0][1] if bid else None
            row["ap"] = ask[0][0] if ask else None
            row["aq"] = ask[0][1] if ask else None
            for k in (1, 3, 5, 10):
                row[f"b{k}"] = sum(q for _, q in bid[:k])
                row[f"a{k}"] = sum(q for _, q in ask[:k])
            for i, k in enumerate((1, 3, 5, 10)):
                row[f"ofi{k}"] = (
                    _delta(prev_bid, bid, k) - _delta(prev_ask, ask, k)
                    if have_prev else 0
                )
            dep_b, rep_b = _queue_delta(prev_bid, bid, True)
            dep_a, rep_a = _queue_delta(prev_ask, ask, False)
            row.update(dep_b=dep_b, rep_b=rep_b, dep_a=dep_a, rep_a=rep_a,
                       has_prev=have_prev)
            rows.append(row)
            prev_bid, prev_ask = bid, ask
            have_prev = True
        else:
            book.apply(ev)
            if ev.event_type == EventType.TRADE:
                bb, ba = book.best_bid(), book.best_ask()
                mid2 = (bb[0] + ba[0]) if (bb and ba) else None
                trows.append({
                    "ts": ev.exchange_ts,
                    "n": n,
                    "qty": ev.qty,
                    "signed": ev.qty if ev.side == 0 else -ev.qty,
                    "price_ticks": ev.price_ticks,
                    "mid2": mid2,
                })
    return pd.DataFrame(rows), pd.DataFrame(trows)


def _delta(prev, curr, k: int) -> int:
    pk = {p: q for p, q in prev[:k]}
    ck = {p: q for p, q in curr[:k]}
    return sum(ck.get(p, 0) - pk.get(p, 0) for p in set(pk) | set(ck))


def _queue_delta(prev, curr, is_bid: bool):
    if not prev and not curr:
        return 0, 0
    if not prev:
        return 0, curr[0][1]
    if not curr:
        return prev[0][1], 0
    (p0, q0), (p1, q1) = prev[0], curr[0]
    if p1 == p0:
        d = q1 - q0
        return (-d, 0) if d < 0 else (0, d)
    improved = p1 > p0 if is_bid else p1 < p0
    return (0, q1) if improved else (q0, 0)


def mid_change_frame(book_df: pd.DataFrame) -> pd.DataFrame:
    """Mid samples (ts, n, mid2, logmid, dlm) from a book frame.

    Mirrors the PINNED sampling semantics: a sample is recorded at every
    two-sided refresh where the mid changed — or where the book just became
    two-sided again (re-baseline, possibly with an unchanged mid2) — and
    ``dlm`` is only defined when the PREVIOUS refresh was two-sided: the
    return chain breaks across one-sided periods (dlm = NaN there, so no
    return ever bridges a one-sided gap).
    """
    rows = []
    hist_mid2 = None      # last recorded sample value
    last_ok_mid2 = None   # mid2 at the last two-sided refresh
    prev_ok = False
    for r in book_df.itertuples():
        ok = pd.notna(r.bp) and pd.notna(r.ap)
        if ok:
            mid2 = r.bp + r.ap
            if not prev_ok or mid2 != last_ok_mid2:
                dlm = (math.log(mid2) - math.log(hist_mid2)
                       if prev_ok and hist_mid2 is not None
                       else float("nan"))
                rows.append({"ts": r.ts, "n": r.n, "mid2": mid2,
                             "logmid": math.log(mid2), "dlm": dlm})
                hist_mid2 = mid2
            last_ok_mid2 = mid2
        prev_ok = ok
    return pd.DataFrame(rows)


def window(df: pd.DataFrame, t: int, w_ns: int, n: int = None) -> pd.DataFrame:
    """Rows in the pinned half-open event-time window (t-w, t].

    ``n`` (1-based event index of the probe) additionally drops rows from
    later events that share the probe's exchange_ts — the engine at event n
    has not seen them yet.
    """
    sub = df[(df.ts > t - w_ns) & (df.ts <= t)]
    if n is not None and "n" in sub.columns:
        sub = sub[sub.n <= n]
    return sub


def at_or_before(df: pd.DataFrame, t: int, n: int = None):
    """Last row with ts <= t (and event index <= n, if given); None if none."""
    sub = df[df.ts <= t]
    if n is not None and "n" in sub.columns:
        sub = sub[sub.n <= n]
    return None if sub.empty else sub.iloc[-1]


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    """Golden tolerance: abs 1e-9 or rel 1e-9."""
    return abs(a - b) <= tol + tol * abs(b)
