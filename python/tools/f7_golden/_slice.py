"""Per-event state of the real slice shared by the scalar-rule goldens
(rules 5-10): the consolidated L1 after every event, the sampled events, the
signal readings derived from the book (rule 6) and the BBO timeline (rules
8, 10). Everything here is what the C++ tests rebuild from the same bytes."""

from __future__ import annotations

from functools import lru_cache

from iap.execution.signal import zscore
from iap.tca.fills import MarketTimeline

from . import SLICE_INSTRUMENT, SLICE_TICK, SLICE_VENUE, build_book, is_sample, load_slice

READING_EVERY = 50  #: a signal reading after every 50th event past the prefix


@lru_cache(maxsize=1)
def states() -> list[dict]:
    """One row per event: ``k`` (1-based), ``ts``, L1 (``None`` when one side
    is empty), the TRADE qty when the event is a TRADE, and ``sample``."""
    events = load_slice()
    book = build_book(SLICE_INSTRUMENT, [])
    out = []
    for k, ev in enumerate(events, start=1):
        book.apply(ev)
        vb = book.books[SLICE_VENUE]
        bb, ba = vb.best_bid(), vb.best_ask()
        out.append(
            {
                "k": k,
                "ts": ev.exchange_ts,
                "bid": None if bb is None else bb[0],
                "bid_sz": None if bb is None else bb[1],
                "ask": None if ba is None else ba[0],
                "ask_sz": None if ba is None else ba[1],
                "trade_qty": ev.qty if ev.event_type == 5 else 0,
                "sample": is_sample(k),
            }
        )
    return out


def imbalance_er(bsz: int, asz: int) -> float:
    """The planted "alpha": ``(bid_sz - ask_sz) / (bid_sz + ask_sz) * 1e-4``
    (a dimensionless expected return, positive when the bid is heavier)."""
    return (bsz - asz) / (bsz + asz) * 1e-4


@lru_cache(maxsize=1)
def readings() -> list[tuple[int, float, float]]:
    """``(ts, z, expected_return)`` after every READING_EVERY-th event past the
    prefix with a two-sided book; ``z`` = zscore of the expected returns."""
    from . import SLICE_PREFIX

    rows = [
        s
        for s in states()
        if s["k"] > SLICE_PREFIX
        and s["k"] % READING_EVERY == 0
        and s["bid"] is not None
        and s["ask"] is not None
    ]
    er = [imbalance_er(s["bid_sz"], s["ask_sz"]) for s in rows]
    z, _mean, _std = zscore(er)
    # one reading per timestamp (ArraySignalSource needs strictly increasing ts):
    # the last of equal timestamps wins, as AlphaUrgencySource does
    out: list[tuple[int, float, float]] = []
    for s, zz, e in zip(rows, z, er, strict=True):
        if out and out[-1][0] == s["ts"]:
            out[-1] = (s["ts"], zz, e)
        else:
            out.append((s["ts"], zz, e))
    return out


READINGS_RULE = (
    "a reading after every 50th event (1-based ordinal) past the prefix whose book is "
    "two-sided: ts = the event's exchange_ts, expected_return = (bid_sz - ask_sz) / "
    "(bid_sz + ask_sz) * 1e-4 of the consolidated L1 after the event, z = "
    "iap.execution.signal.zscore over those expected returns; equal timestamps keep the "
    "last reading"
)


@lru_cache(maxsize=1)
def timeline() -> MarketTimeline:
    """BBO timeline of the slice: after every event with a two-sided book,
    ``append_state_pinned(ts, bid * tick, ask * tick, bid_sz, ask_sz)``."""
    tl = MarketTimeline()
    for s in states():
        if s["bid"] is None or s["ask"] is None:
            continue
        tl.append_state_pinned(
            s["ts"], s["bid"] * SLICE_TICK, s["ask"] * SLICE_TICK, s["bid_sz"], s["ask_sz"]
        )
    return tl


TIMELINE_RULE = (
    "after every event with a two-sided consolidated book: append_state_pinned(ts, "
    "best_bid_ticks * tick, best_ask_ticks * tick, bid_sz, ask_sz) (crossed states "
    "skipped; no halts)"
)


def samples() -> list[dict]:
    return [s for s in states() if s["sample"] and s["bid"] is not None and s["ask"] is not None]
