"""v1.18 REL-09: stale-quote protection study code on synthetic two-venue fixtures.

No real data is read. The fixture: venue A (101, where the quoter rests) and
venue B (102) both quote 100.00 / 100.02. Sniping episode: B's mid falls two
ticks, then (50 ms later) a seller hits A's stale bid, and A's book follows B
down. A benign episode: a buyer lifts A's ask with no displacement anywhere.
"""

from __future__ import annotations

import json
import math

import pytest
from iap.core.events import EventType, MarketEvent, Side
from iap.research.stale_quote import (
    HYPOTHESIS_AS_REDUCTION,
    QuoterConfig,
    StaleQuoteMonitor,
    StaleRule,
    consolidated_mid_path,
    incremental,
    mark_to_market,
    run_session,
    simulate_quoter,
    summarise_sessions,
)

A, B, IID = 101, 102, 1
MS = 1_000_000
SEC = 1_000_000_000
RULE = StaleRule(min_age_ns=10 * MS, min_displacement_ticks=1.0)


class _Tape:
    def __init__(self, lag_ns: int = 0) -> None:
        self.events: list[MarketEvent] = []
        self.seq = {A: 0, B: 0}
        self.lag = lag_ns

    def ev(self, venue, ts, et, side, px, qty, oid):
        self.seq[venue] += 1
        self.events.append(
            MarketEvent(
                event_id=len(self.events) + 1,
                instrument_id=IID,
                venue_id=venue,
                exchange_ts=ts,
                receive_ts=ts + (self.lag if venue == B else 0),
                sequence=self.seq[venue],
                event_type=int(et),
                side=int(side),
                price_ticks=px,
                qty=qty,
                order_id=oid,
                trade_id=0,
            )
        )


def session_tape(t0: int = 10 * SEC, snipe: bool = True, lag_ns: int = 0) -> list[MarketEvent]:
    tp = _Tape(lag_ns)
    for v, base in ((A, 1000), (B, 2000)):
        tp.ev(v, t0, EventType.ADD, Side.BID, 10000, 300, base + 1)
        tp.ev(v, t0, EventType.ADD, Side.ASK, 10002, 300, base + 2)
        tp.ev(v, t0, EventType.ADD, Side.BID, 9999, 300, base + 3)
        tp.ev(v, t0, EventType.ADD, Side.ASK, 10003, 300, base + 4)
    t = t0 + 2 * SEC  # quoter has posted at the t0 + 1 s refresh
    # benign: a buyer lifts A's ask through the queue ahead and into our quote
    tp.ev(A, t, EventType.EXECUTE, Side.ASK, 10002, 300, 1002)
    tp.ev(A, t + 1, EventType.ADD, Side.ASK, 10002, 300, 1005)
    t += 3 * SEC
    if snipe:
        # B moves down two ticks
        tp.ev(B, t, EventType.CANCEL, Side.BID, 10000, 300, 2001)
        tp.ev(B, t, EventType.CANCEL, Side.ASK, 10002, 300, 2002)
        tp.ev(B, t, EventType.ADD, Side.BID, 9998, 300, 2005)
        tp.ev(B, t, EventType.ADD, Side.ASK, 10000, 300, 2006)
        # 50 ms later the sniper sells through A's stale bid (queue ahead 300)
        ts = t + 50 * MS
        tp.ev(A, ts, EventType.EXECUTE, Side.BID, 10000, 300, 1001)
        # A follows: bid level now empty -> best bid 9999; ask moves down
        tp.ev(A, ts + 1, EventType.CANCEL, Side.ASK, 10002, 300, 1005)
        tp.ev(A, ts + 1, EventType.ADD, Side.ASK, 10000, 300, 1006)
        tp.ev(A, ts + 1, EventType.ADD, Side.BID, 9998, 300, 1007)
    # keep the session alive past the markout horizon
    tp.ev(A, t + 5 * SEC, EventType.ADD, Side.BID, 9990, 100, 1999)
    return tp.events


def cfg(**kw) -> QuoterConfig:
    base = dict(venue_id=A, instrument_id=IID, tick_size=0.01, lot=100, rebate_per_share=0.002)
    base.update(kw)
    return QuoterConfig(**base)


def _ahead_fix(events):
    """Our post joins behind 300; the executes above are sized 300, so make the
    benign and snipe prints 400 to reach us with 100."""
    for e in events:
        if e.event_type == EventType.EXECUTE:
            e.qty = 400
    return events


def test_baseline_fills_both_filter_misses_snipe():
    ev = _ahead_fix(session_tape())
    base = simulate_quoter(ev, cfg(), RULE, session="d1", filtered=False, venues=(A, B))
    filt = simulate_quoter(ev, cfg(), RULE, session="d1", filtered=True, venues=(A, B))
    assert sorted(f.side for f in base.fills) == [Side.BID, Side.ASK]
    assert [f.side for f in filt.fills] == [Side.ASK]
    snipe = next(f for f in base.fills if f.side == Side.BID)
    assert snipe.stale_at_fill and snipe.markout < 0
    row = incremental(base, filt)
    assert row["missed"]["fills"] == 1.0 and row["extra"]["fills"] == 0.0
    # the missed fill is priced: its spread and rebate are given up too
    assert row["missed"]["spread"] > 0 and row["missed"]["fees"] > 0
    assert row["delta"]["markout"] > 0  # adverse selection removed


def test_decomposition_sums_and_matches_mark_to_market():
    ev = _ahead_fix(session_tape())
    c = cfg()
    res = simulate_quoter(ev, c, RULE, session="d1", filtered=False, venues=(A, B))
    _, mids = consolidated_mid_path(ev, IID, (A, B))
    tot = res.totals()
    assert math.isclose(
        tot["net"], tot["spread"] + tot["fees"] + tot["markout"] + tot["inventory"], abs_tol=1e-9
    )
    mtm = mark_to_market(res.fills, mids[-1] * c.tick_size, c.rebate_per_share)
    assert math.isclose(tot["net"], mtm, abs_tol=1e-9)


def test_incremental_identity():
    ev = _ahead_fix(session_tape())
    row = run_session(ev, cfg(), RULE, session="d1", venues=(A, B))
    d = row["delta"]["net"]
    rhs = row["extra"]["net"] - row["missed"]["net"] + row["common_delta"]["net"]
    assert math.isclose(d, rhs, abs_tol=1e-9)
    for c in ("spread", "fees", "markout", "inventory"):
        assert math.isclose(
            row["delta"][c],
            row["extra"][c] - row["missed"][c] + row["common_delta"][c],
            abs_tol=1e-9,
        )


def test_indicator_is_causal_on_receive_time():
    """With B's feed received 100 ms late the flag cannot see the move when
    the snipe arrives (50 ms after B's exchange time): no pull, fill kept."""
    ev = _ahead_fix(session_tape(lag_ns=100 * MS))
    filt = simulate_quoter(ev, cfg(), RULE, session="d1", filtered=True, venues=(A, B))
    assert sorted(f.side for f in filt.fills) == [Side.BID, Side.ASK]


def test_indicator_ignores_future_events():
    ev = session_tape()
    t_probe = 10 * SEC + 5 * SEC + 20 * MS  # after B's move, before the snipe
    full = StaleQuoteMonitor(IID, (A, B), RULE)
    cut = StaleQuoteMonitor(IID, (A, B), RULE)
    for e in sorted(ev, key=lambda e: (e.receive_ts, e.event_id)):
        if e.receive_ts <= t_probe:
            full.on_event(e)
            cut.on_event(e)
    # feed the full monitor nothing more: the state at t_probe is the truncated one
    assert full.stale(A, Side.BID, t_probe) == cut.stale(A, Side.BID, t_probe) is True
    assert full.stale(A, Side.ASK, t_probe) is False


def test_receive_order_enforced():
    mon = StaleQuoteMonitor(IID, (A, B), RULE)
    ev = session_tape()
    mon.on_event(ev[-1])
    with pytest.raises(ValueError):
        mon.on_event(ev[0])


def test_summary_small_g_and_deterministic():
    rows = [
        run_session(_ahead_fix(session_tape(snipe=s)), cfg(), RULE, session=f"d{i}", venues=(A, B))
        for i, s in enumerate((True, False, True, True))
    ]
    s1 = summarise_sessions(rows)
    s2 = summarise_sessions(list(reversed(rows)))
    assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)
    assert s1["G"] == 4 and s1["df"] == 3
    assert "G session clusters" in s1["small_g"]
    assert s1["as_reduction_hypothesis"] == HYPOTHESIS_AS_REDUCTION
    assert s1["as_reduction_is_criterion"] is False
    assert s1["as_reduction"] is not None and s1["as_reduction"] > 0
    # no interval below three sessions
    assert summarise_sessions(rows[:2])["ci95_usd"] is None
