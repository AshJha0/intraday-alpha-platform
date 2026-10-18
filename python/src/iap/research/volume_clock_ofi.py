"""Volume-clock OFI and hidden-print imbalance at 1-5 minutes (v1.18 REL-10, hypothesis 5).

Study code only, treated as an EXECUTION INPUT first (a maker's side
choice / a taker's timing), not as a stand-alone alpha. Nothing here reads
real data: the study read is a ledger look that needs the signed
pre-declaration (``research/v118/REL10_PREDECLARATION_DRAFT.md``, a DRAFT
with open parameters). Tests run on synthetic fixtures.

**Rows on a volume clock** (:func:`volume_clock_rows`). Events of one
instrument (one venue, or a merged stream) are applied in exchange order to
an :class:`OrderBook`. A row is emitted when cumulative traded quantity
(displayed EXECUTE plus non-displayed TRADE prints) crosses the next
multiple of ``n_shares``; the row's ``ts`` is the end of that event's
same-timestamp group, as in the REL-07 volume-clock store. Every feature of
a row reads only events with ``exchange_ts <= ts`` (tested by truncation).

**Features**, per window ``w`` (default 1, 2, 3, 4, 5 minutes):

- ``ofi_<w>``: Cont-Kukanov-Stoikov order-flow imbalance, summed over
  ``(ts - w, ts]`` from best-level changes (shares);
- ``ofi_prev_<w>``: the same over ``(ts - 2w, ts - w]`` (for persistence);
- ``flow_<w>``: signed traded quantity (aggressor buy +), displayed and
  hidden;
- ``hid_imb_<w>``: hidden-print imbalance ``(buy - sell) / (buy + sell)``
  over the window's TRADE prints (None when there are none).

Trade signs: an EXECUTE's resting side comes from the book (aggressor is
the other side); a TRADE print's ``side`` is taken as the RESTING side (the
ITCH ``P`` buy/sell indicator convention), so a print on the ASK is a buy.

**Labels** (:func:`label_row`): ``gross_<h>`` the log mid return over
``[ts, ts + h]`` in bps (forward mid = last mid at or before ``ts + h``),
``cost_<h>`` the half spreads at entry and exit in bps, ``complete_<h>``
False when ``ts + h`` is past the session's last event. The REGISTERED
target is the spread-netted label ``sign(signal) * gross - cost`` (a taker's
round trip at the touch); the maker and taker uses are evaluated separately:

- :func:`evaluate_taker`: enter at the touch when ``|signal| >= threshold``,
  size capped at the displayed touch depth (partial fill; the unfilled part
  is a MISSED opportunity priced at its gross move, reported beside the
  P&L, never in it), exit at the far touch after ``h``; non-overlapping;
- :func:`evaluate_maker`: post at the near touch on the signal's side
  behind the displayed queue; fills from aggressor prints at the price
  (queue first) or through it within ``entry_window_ns`` (partial fills
  kept); after ``h`` a PASSIVE exit at the far touch behind its queue for
  ``exit_window_ns``, the remainder crossed at the touch; unfilled entry
  quantity is a missed opportunity, priced as above.

Each trip reports ``gross_usd`` (mid to mid), ``cost_usd`` (touch versus
mid at entry and exit, negative when earned), ``fees_usd`` (taker fees paid
negative, maker rebates positive; zero unless the per-share rates are
passed) and ``net_usd = gross - cost + fees`` (tested). :func:`summarise` gives per-session clustered statistics
(:func:`iap.research.study_stats.summarise_study`: every session counts,
no interval below 3 sessions, MDE) with the small-G statement.

Python reference, plain loops, no RNG: deterministic output.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from iap.core.events import EventType, MarketEvent, Side
from iap.orderbook.book import OrderBook
from iap.research.study_stats import summarise_study

MIN = 60_000_000_000
DEFAULT_WINDOWS_MIN: tuple[int, ...] = (1, 2, 3, 4, 5)
BOOK_EVENTS = (
    EventType.ADD,
    EventType.MODIFY,
    EventType.CANCEL,
    EventType.EXECUTE,
    EventType.QUOTE,
    EventType.SNAPSHOT,
)
SMALL_G_STATEMENT = (
    "inference rests on G session clusters; for G <= 5 the result is a size statement, "
    "not a power statement, and no generalisation beyond these sessions is claimed"
)


@dataclass(frozen=True)
class Tape:
    """Exchange-clock book and trade timeline of one instrument."""

    q_ts: list[int]  # end of each same-ts group with a two-sided book
    bid: list[int]
    bid_qty: list[int]
    ask: list[int]
    ask_qty: list[int]
    t_ts: list[int]  # trades
    t_px: list[int]
    t_qty: list[int]
    t_sign: list[int]  # +1 buyer aggressor
    t_hidden: list[bool]
    ofi_ts: list[int]
    ofi: list[int]
    last_ts: int


def build_tape(events: Iterable[MarketEvent], instrument_id: int) -> Tape:
    evs = sorted(
        (e for e in events if e.instrument_id == instrument_id),
        key=lambda e: (e.exchange_ts, e.event_id),
    )
    books: dict[int, OrderBook] = {}
    tp = Tape([], [], [], [], [], [], [], [], [], [], [], [], 0)
    prev: tuple[int, int, int, int] | None = None

    def top() -> tuple[int, int, int, int] | None:
        bids = [b for b in (bk.best_bid() for bk in books.values()) if b is not None]
        asks = [a for a in (bk.best_ask() for bk in books.values()) if a is not None]
        if not bids or not asks:
            return None
        bp = max(b[0] for b in bids)
        ap = min(a[0] for a in asks)
        if bp >= ap:
            return None
        return (
            bp,
            sum(b[1] for b in bids if b[0] == bp),
            ap,
            sum(a[1] for a in asks if a[0] == ap),
        )

    for i, ev in enumerate(evs):
        bk = books.get(ev.venue_id)
        if bk is None:
            bk = books[ev.venue_id] = OrderBook(instrument_id, ev.venue_id)
        et = ev.event_type
        if et == EventType.EXECUTE:
            o = bk.order(ev.order_id)
            if o is not None:
                px = ev.price_ticks if ev.price_ticks > 0 else int(o[1])
                sign = 1 if int(o[0]) == Side.ASK else -1
                tp.t_ts.append(ev.exchange_ts)
                tp.t_px.append(px)
                tp.t_qty.append(min(ev.qty, int(o[2])))
                tp.t_sign.append(sign)
                tp.t_hidden.append(False)
        elif et == EventType.TRADE and ev.qty > 0 and ev.price_ticks > 0:
            tp.t_ts.append(ev.exchange_ts)
            tp.t_px.append(ev.price_ticks)
            tp.t_qty.append(ev.qty)
            tp.t_sign.append(1 if ev.side == Side.ASK else -1)
            tp.t_hidden.append(True)
        bk.apply(ev)  # every event: the book's sequence check sees no holes
        group_end = i + 1 == len(evs) or evs[i + 1].exchange_ts != ev.exchange_ts
        if not group_end:
            continue
        cur = top()
        if cur is None:
            continue
        if prev is not None and cur != prev:
            pb, pbq, pa, paq = prev
            b, bq, a, aq = cur
            e = (bq if b >= pb else 0) - (pbq if b <= pb else 0)
            e += (paq if a >= pa else 0) - (aq if a <= pa else 0)
            if e:
                tp.ofi_ts.append(ev.exchange_ts)
                tp.ofi.append(e)
        prev = cur
        tp.q_ts.append(ev.exchange_ts)
        tp.bid.append(cur[0])
        tp.bid_qty.append(cur[1])
        tp.ask.append(cur[2])
        tp.ask_qty.append(cur[3])
    return Tape(**{**tp.__dict__, "last_ts": evs[-1].exchange_ts if evs else 0})


def _at(ts: Sequence[int], t: int) -> int | None:
    """Index of the last element ``<= t`` (None if none)."""
    k = bisect.bisect_right(ts, t)
    return None if k == 0 else k - 1


def _window(ts: Sequence[int], lo: int, hi: int) -> range:
    """Indices with ``lo < ts <= hi``."""
    return range(bisect.bisect_right(ts, lo), bisect.bisect_right(ts, hi))


def volume_clock_rows(
    tape: Tape,
    n_shares: int,
    *,
    windows_min: Sequence[int] = DEFAULT_WINDOWS_MIN,
    session: str = "",
) -> list[dict[str, Any]]:
    """One feature row per ``n_shares`` traded (module docstring)."""
    if n_shares <= 0:
        raise ValueError("n_shares must be positive")
    stamps: list[int] = []
    cum = 0
    nxt = n_shares
    for k, (t, q) in enumerate(zip(tape.t_ts, tape.t_qty, strict=True)):
        cum += q
        group_end = k + 1 == len(tape.t_ts) or tape.t_ts[k + 1] != t
        if cum >= nxt and group_end:
            stamps.append(t)
            while nxt <= cum:
                nxt += n_shares
    rows: list[dict[str, Any]] = []
    for ts in stamps:
        qi = _at(tape.q_ts, ts)
        if qi is None:
            continue
        r: dict[str, Any] = {
            "session": session,
            "ts": ts,
            "bid": tape.bid[qi],
            "ask": tape.ask[qi],
            "bid_qty": tape.bid_qty[qi],
            "ask_qty": tape.ask_qty[qi],
        }
        for w in windows_min:
            wn = w * MIN
            r[f"ofi_{w}m"] = sum(tape.ofi[i] for i in _window(tape.ofi_ts, ts - wn, ts))
            r[f"ofi_prev_{w}m"] = sum(
                tape.ofi[i] for i in _window(tape.ofi_ts, ts - 2 * wn, ts - wn)
            )
            idx = _window(tape.t_ts, ts - wn, ts)
            r[f"flow_{w}m"] = sum(tape.t_sign[i] * tape.t_qty[i] for i in idx)
            hb = sum(tape.t_qty[i] for i in idx if tape.t_hidden[i] and tape.t_sign[i] > 0)
            hs = sum(tape.t_qty[i] for i in idx if tape.t_hidden[i] and tape.t_sign[i] < 0)
            r[f"hid_imb_{w}m"] = None if hb + hs == 0 else (hb - hs) / (hb + hs)
        rows.append(r)
    return rows


def label_row(tape: Tape, ts: int, h_ns: int) -> dict[str, Any]:
    """Forward label at ``ts`` (gross log-mid bps, half-spread cost bps, complete)."""
    i0, i1 = _at(tape.q_ts, ts), _at(tape.q_ts, ts + h_ns)
    if i0 is None or i1 is None:
        return {"gross": None, "cost": None, "complete": False}
    m0 = 0.5 * (tape.bid[i0] + tape.ask[i0])
    m1 = 0.5 * (tape.bid[i1] + tape.ask[i1])
    cost = 1e4 * (
        0.5 * (tape.ask[i0] - tape.bid[i0]) / m0 + 0.5 * (tape.ask[i1] - tape.bid[i1]) / m1
    )
    return {
        "gross": 1e4 * math.log(m1 / m0),
        "cost": cost,
        "complete": ts + h_ns <= tape.last_ts,
    }


def spread_netted_label(gross: float, cost: float, signal: float) -> float:
    """The registered target: ``sign(signal) * gross - cost`` (bps)."""
    s = 1.0 if signal > 0 else -1.0 if signal < 0 else 0.0
    return s * gross - (cost if s else 0.0)


# ---------------------------------------------------------- execution --


def _mid(tape: Tape, i: int) -> float:
    return 0.5 * (tape.bid[i] + tape.ask[i])


def _trip(
    session: str,
    use: str,
    ts: int,
    s: int,
    qty: int,
    filled: int,
    entry_px: float,
    entry_mid: float,
    exit_px: float,
    exit_mid: float,
    tick: float,
    missed_gross_ticks: float,
    exit_kind: str,
    fees: float = 0.0,
) -> dict[str, Any]:
    gross = s * filled * (exit_mid - entry_mid) * tick
    cost = filled * (s * (entry_px - entry_mid) - s * (exit_px - exit_mid)) * tick
    net = gross - cost + fees
    notional = filled * entry_mid * tick
    return {
        "session": session,
        "use": use,
        "ts": ts,
        "side": s,
        "qty": qty,
        "filled": filled,
        "unfilled": qty - filled,
        "gross_usd": gross,
        "cost_usd": cost,
        "fees_usd": fees,
        "net_usd": net,
        "net_bps": 1e4 * net / notional if notional else 0.0,
        "missed_usd": (qty - filled) * missed_gross_ticks * tick,
        "exit": exit_kind,
    }


def evaluate_taker(
    tape: Tape,
    rows: Sequence[Mapping[str, Any]],
    signal_key: str,
    *,
    h_ns: int,
    threshold: float,
    qty: int,
    tick: float,
    session: str,
    taker_fee_per_share: float = 0.0,
) -> list[dict[str, Any]]:
    """Taker round trips at the touch (module docstring); both legs pay the taker fee."""
    trips: list[dict[str, Any]] = []
    busy_until = -1
    for r in rows:
        sig = r.get(signal_key)
        ts = int(r["ts"])
        if sig is None or abs(sig) < threshold or sig == 0 or ts <= busy_until:
            continue
        if ts + h_ns > tape.last_ts:
            continue  # incomplete label: no trip
        i0, i1 = _at(tape.q_ts, ts), _at(tape.q_ts, ts + h_ns)
        if i0 is None or i1 is None:
            continue
        s = 1 if sig > 0 else -1
        depth = tape.ask_qty[i0] if s > 0 else tape.bid_qty[i0]
        filled = min(qty, depth)
        entry = tape.ask[i0] if s > 0 else tape.bid[i0]
        exit_ = tape.bid[i1] if s > 0 else tape.ask[i1]
        m0, m1 = _mid(tape, i0), _mid(tape, i1)
        trips.append(
            _trip(
                session,
                "taker",
                ts,
                s,
                qty,
                filled,
                entry,
                m0,
                exit_,
                m1,
                tick,
                s * (m1 - m0),
                "cross",
                -2.0 * taker_fee_per_share * filled,
            )
        )
        busy_until = ts + h_ns
    return trips


def _passive_fill(
    tape: Tape, t0: int, t1: int, s: int, px: int, ahead: int, qty: int
) -> tuple[int, int]:
    """Fill a passive order of side ``s`` (+1 bid) at ``px`` from aggressor
    prints in ``(t0, t1]``: (filled qty, time of the last fill or t1)."""
    filled = 0
    last = t1
    for i in _window(tape.t_ts, t0, t1):
        if tape.t_sign[i] != -s:
            continue  # need a seller hitting our bid (s=+1), a buyer lifting our ask
        p = tape.t_px[i]
        through = p < px if s > 0 else p > px
        if through:
            filled = qty
        elif p == px:
            take = tape.t_qty[i]
            used = min(ahead, take)
            ahead -= used
            filled = min(qty, filled + take - used)
        else:
            continue
        last = tape.t_ts[i]
        if filled >= qty:
            break
    return filled, last


def evaluate_maker(
    tape: Tape,
    rows: Sequence[Mapping[str, Any]],
    signal_key: str,
    *,
    h_ns: int,
    threshold: float,
    qty: int,
    tick: float,
    session: str,
    entry_window_ns: int,
    exit_window_ns: int,
    taker_fee_per_share: float = 0.0,
    maker_rebate_per_share: float = 0.0,
) -> list[dict[str, Any]]:
    """Maker round trips with passive entry and exit (module docstring); passive
    fills earn the rebate, the crossed remainder of the exit pays the taker fee."""
    trips: list[dict[str, Any]] = []
    busy_until = -1
    for r in rows:
        sig = r.get(signal_key)
        ts = int(r["ts"])
        if sig is None or abs(sig) < threshold or sig == 0 or ts <= busy_until:
            continue
        if ts + entry_window_ns + h_ns + exit_window_ns > tape.last_ts:
            continue
        i0 = _at(tape.q_ts, ts)
        if i0 is None:
            continue
        s = 1 if sig > 0 else -1
        px = tape.bid[i0] if s > 0 else tape.ask[i0]
        ahead = tape.bid_qty[i0] if s > 0 else tape.ask_qty[i0]
        filled, tf = _passive_fill(tape, ts, ts + entry_window_ns, s, px, ahead, qty)
        m0 = _mid(tape, i0)
        ih = _at(tape.q_ts, ts + h_ns)
        missed_ticks = 0.0 if ih is None else s * (_mid(tape, ih) - m0)
        if filled == 0:
            trips.append(
                _trip(session, "maker", ts, s, qty, 0, px, m0, px, m0, tick, missed_ticks, "none")
            )
            busy_until = ts + entry_window_ns
            continue
        tx = tf + h_ns
        ix = _at(tape.q_ts, tx)
        assert ix is not None
        xpx = tape.ask[ix] if s > 0 else tape.bid[ix]
        xahead = tape.ask_qty[ix] if s > 0 else tape.bid_qty[ix]
        xfill, _ = _passive_fill(tape, tx, tx + exit_window_ns, -s, xpx, xahead, filled)
        iend = _at(tape.q_ts, tx + exit_window_ns)
        assert iend is not None
        # passive part at xpx, the remainder crossed at the touch at the window end
        cross_px = tape.bid[iend] if s > 0 else tape.ask[iend]
        exit_px = (xfill * xpx + (filled - xfill) * cross_px) / filled
        # exit mid: the mid at the window end (one mark for both parts)
        exit_mid = _mid(tape, iend)
        kind = "passive" if xfill == filled else "mixed" if xfill else "cross"
        trips.append(
            _trip(
                session,
                "maker",
                ts,
                s,
                qty,
                filled,
                px,
                m0,
                exit_px,
                exit_mid,
                tick,
                missed_ticks,
                kind,
                maker_rebate_per_share * (filled + xfill) - taker_fee_per_share * (filled - xfill),
            )
        )
        busy_until = tx + exit_window_ns
    return trips


def ofi_persistence(rows: Sequence[Mapping[str, Any]], w: int) -> float | None:
    """Descriptive Pearson correlation of ``ofi_prev_<w>m`` and ``ofi_<w>m``."""
    xs = [float(r[f"ofi_prev_{w}m"]) for r in rows]
    ys = [float(r[f"ofi_{w}m"]) for r in rows]
    n = len(xs)
    if n < 3:
        return None
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def summarise(trips: Sequence[Mapping[str, Any]], sessions: Sequence[str]) -> dict[str, Any]:
    """Taker and maker summarised SEPARATELY, session-clustered (module docs)."""
    out: dict[str, Any] = {"small_g": SMALL_G_STATEMENT, "execution_input_first": True}
    for use in ("taker", "maker"):
        tt = [t for t in trips if t["use"] == use]
        s = summarise_study([t for t in tt if t["filled"] > 0], sessions)
        s["missed_usd"] = math.fsum(float(t["missed_usd"]) for t in tt)
        s["unfilled_qty"] = sum(int(t["unfilled"]) for t in tt)
        s["opportunities"] = len(tt)
        out[use] = s
    return out
