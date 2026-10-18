"""v1.18 REL-10: volume-clock OFI study code on a synthetic one-venue tape.

No real data is read. The tape: ten minutes of an uptrend (every 10 s a buyer
takes the 200-share ask, the book steps up one tick, plus a 50-share hidden
buy print), then ten minutes flat (buyers and sellers alternate, the book
replenishes at the same prices).
"""

from __future__ import annotations

import json
import math

import pytest
from iap.core.events import EventType, MarketEvent, Side
from iap.research.volume_clock_ofi import (
    MIN,
    build_tape,
    evaluate_maker,
    evaluate_taker,
    label_row,
    ofi_persistence,
    spread_netted_label,
    summarise,
    volume_clock_rows,
)

V, IID, TICK = 101, 1, 0.01
SEC = 1_000_000_000


class _Gen:
    def __init__(self) -> None:
        self.events: list[MarketEvent] = []
        self.oid = 0

    def ev(self, ts, et, side, px, qty, oid=0):
        self.events.append(
            MarketEvent(
                event_id=len(self.events) + 1,
                instrument_id=IID,
                venue_id=V,
                exchange_ts=ts,
                receive_ts=ts,
                sequence=len(self.events) + 1,
                event_type=int(et),
                side=int(side),
                price_ticks=px,
                qty=qty,
                order_id=oid,
                trade_id=0,
            )
        )

    def add(self, ts, side, px, qty=200):
        self.oid += 1
        self.ev(ts, EventType.ADD, side, px, qty, self.oid)
        return self.oid


def session_events(t0: int = 10 * 3600 * SEC) -> list[MarketEvent]:
    g = _Gen()
    bid, ask = 10000, 10002
    b_id, a_id = g.add(t0, Side.BID, bid), g.add(t0, Side.ASK, ask)
    t = t0
    for _ in range(60):  # uptrend
        t += 10 * SEC
        g.ev(t, EventType.EXECUTE, Side.ASK, ask, 200, a_id)
        g.ev(t, EventType.TRADE, Side.ASK, ask, 50)
        g.ev(t + 1, EventType.CANCEL, Side.BID, bid, 200, b_id)
        bid, ask = bid + 1, ask + 1
        b_id, a_id = g.add(t + 1, Side.BID, bid), g.add(t + 1, Side.ASK, ask)
    for k in range(60):  # flat
        t += 10 * SEC
        if k % 2 == 0:
            g.ev(t, EventType.EXECUTE, Side.ASK, ask, 200, a_id)
            a_id = g.add(t + 1, Side.ASK, ask)
        else:
            g.ev(t, EventType.EXECUTE, Side.BID, bid, 200, b_id)
            b_id = g.add(t + 1, Side.BID, bid)
    return g.events


@pytest.fixture(scope="module")
def tape():
    return build_tape(session_events(), IID)


def test_rows_are_causal_under_truncation(tape):
    ev = session_events()
    rows = volume_clock_rows(tape, 400, session="d1")
    assert len(rows) > 10
    for r in rows[::7]:
        cut = build_tape([e for e in ev if e.exchange_ts <= r["ts"]], IID)
        rc = [x for x in volume_clock_rows(cut, 400, session="d1") if x["ts"] == r["ts"]]
        assert rc == [r]


def test_volume_clock_and_features(tape):
    rows = volume_clock_rows(tape, 500, session="d1")
    # 250 shares per uptrend step: one row every second step; flat: 200 per step
    assert all(b["ts"] > a["ts"] for a, b in zip(rows, rows[1:], strict=False))
    up = rows[5]
    assert up["flow_1m"] > 0 and up["ofi_1m"] > 0 and up["hid_imb_1m"] == 1.0
    flat = rows[-1]
    assert flat["hid_imb_1m"] is None  # no hidden prints in the flat phase
    assert ofi_persistence(rows, 1) is not None
    with pytest.raises(ValueError):
        volume_clock_rows(tape, 0)


def test_labels_and_spread_netting(tape):
    lab = label_row(tape, tape.q_ts[0], 1 * MIN)
    assert lab["complete"] and lab["gross"] > 0 and lab["cost"] > 0
    assert label_row(tape, tape.last_ts - SEC, MIN)["complete"] is False
    assert spread_netted_label(10.0, 2.0, +0.5) == 8.0
    assert spread_netted_label(10.0, 2.0, -0.5) == -12.0
    assert spread_netted_label(10.0, 2.0, 0.0) == 0.0


def test_taker_decomposition_partial_and_missed(tape):
    rows = volume_clock_rows(tape, 500, session="d1")
    trips = evaluate_taker(
        tape, rows, "flow_1m", h_ns=MIN, threshold=1, qty=300, tick=TICK, session="d1"
    )
    assert trips
    for t in trips:
        assert math.isclose(t["net_usd"], t["gross_usd"] - t["cost_usd"], abs_tol=1e-9)
        assert t["filled"] <= 200 and t["unfilled"] == t["qty"] - t["filled"]
    up = trips[0]
    assert up["side"] == 1 and up["filled"] == 200 and up["missed_usd"] > 0
    # non-overlapping
    assert all(b["ts"] > a["ts"] + MIN for a, b in zip(trips, trips[1:], strict=False))


def test_maker_passive_entry_exit_and_missed(tape):
    rows = volume_clock_rows(tape, 500, session="d1")
    trips = evaluate_maker(
        tape,
        rows,
        "flow_1m",
        h_ns=MIN,
        threshold=1,
        qty=100,
        tick=TICK,
        session="d1",
        entry_window_ns=30 * SEC,
        exit_window_ns=30 * SEC,
    )
    assert trips
    for t in trips:
        assert math.isclose(t["net_usd"], t["gross_usd"] - t["cost_usd"], abs_tol=1e-9)
    # in the uptrend a passive bid never trades (the book walks away): missed, priced
    up = trips[0]
    assert up["filled"] == 0 and up["exit"] == "none" and up["missed_usd"] > 0
    assert any(t["filled"] > 0 for t in trips)


def test_summary_separate_uses_and_deterministic(tape):
    rows = volume_clock_rows(tape, 500, session="d1")
    trips = evaluate_taker(
        tape, rows, "flow_1m", h_ns=MIN, threshold=1, qty=100, tick=TICK, session="d1"
    )
    trips += evaluate_maker(
        tape,
        rows,
        "flow_1m",
        h_ns=MIN,
        threshold=1,
        qty=100,
        tick=TICK,
        session="d1",
        entry_window_ns=30 * SEC,
        exit_window_ns=30 * SEC,
    )
    s = summarise(trips, ["d1", "d2", "d3"])
    assert set(s) >= {"taker", "maker", "small_g"}
    assert s["taker"]["n_sessions"] == 3 and s["taker"]["n_sessions_without_trips"] == 2
    assert s["maker"]["opportunities"] > 0
    assert json.dumps(s, sort_keys=True) == json.dumps(
        summarise(trips, ["d1", "d2", "d3"]), sort_keys=True
    )
