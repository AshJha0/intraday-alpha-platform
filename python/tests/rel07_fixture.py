"""Synthetic multi-instrument tape for the v1.18 REL-07 tests (no real data).

Instruments (ids fixed, JSON row order configurable): 1 AAPL (EQUITY),
2 AMLP (ETF), 3 QQQ (ETF).  Every second each instrument re-quotes a
one-tick-wide book (CANCEL old bid / ask, ADD new) and prints one TRADE.
QQQ's mid is a random walk; AAPL's 1 s tick move is ``2 x`` QQQ's plus
noise; AMLP's is independent, so the AAPL beta is about 2 against QQQ and
about 0 against AMLP.  Session 09:30-16:00 America/New_York; the tape
starts 2019-01-30 09:30 EST.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from iap.core.events import EventType, MarketEvent
from iap.core.rng import SplitMix64

NS = 1_000_000_000
T0 = 1_548_858_600 * NS  # 2019-01-30 14:30 UTC = 09:30 EST
ROWS = {
    1: {"instrument_id": 1, "symbol": "AAPL", "asset_class": "EQUITY", "tick_size": 0.01},
    2: {"instrument_id": 2, "symbol": "AMLP", "asset_class": "ETF", "tick_size": 0.01},
    3: {"instrument_id": 3, "symbol": "QQQ", "asset_class": "ETF", "tick_size": 0.01},
}
SESSION = {"open": "09:30:00", "close": "16:00:00", "timezone": "America/New_York"}


def write_configs(root: Path, order: tuple[int, ...]) -> Path:
    """``root/instruments/instruments.json`` with the rows in ``order``."""
    cfg = {
        "sessions": {"EQUITY": dict(SESSION), "ETF": dict(SESSION)},
        "instruments": [ROWS[i] for i in order],
    }
    (root / "instruments").mkdir(parents=True, exist_ok=True)
    (root / "instruments" / "instruments.json").write_text(json.dumps(cfg))
    return root


def tape(
    seconds: int = 420, seed: int = 7, trade_qty: int = 40, start: int = T0
) -> list[MarketEvent]:
    rng = SplitMix64(seed)
    mid = {1: 15_000, 2: 1_600, 3: 15_000}  # ticks
    seq = {1: 0, 2: 0, 3: 0}
    live: dict[int, tuple[int, int, int, int]] = {}
    oid = [0]
    evs: list[MarketEvent] = []

    def ev(iid, ts, et, side, price, qty, order_id=0, trade_id=0):
        seq[iid] += 1
        evs.append(
            MarketEvent(
                event_id=len(evs) + 1,
                instrument_id=iid,
                venue_id=1,
                exchange_ts=ts,
                receive_ts=ts + 150_000,
                sequence=seq[iid],
                event_type=int(et),
                side=side,
                price_ticks=price,
                qty=qty,
                order_id=order_id,
                trade_id=trade_id,
            )
        )

    def step():
        return 1 if rng.uniform() < 0.5 else -1

    for s in range(seconds):
        q = step() * (1 + int(rng.uniform() * 3))
        mid[3] += q
        mid[1] += 2 * q + (step() if rng.uniform() < 0.3 else 0)
        mid[2] += step() if rng.uniform() < 0.5 else 0
        for k, iid in enumerate((3, 1, 2)):  # the reference re-quotes first within a second
            ts = start + s * NS + (k + 1) * 1_000_000
            if iid in live:
                bo, bp, ao, apx = live[iid]
                ev(iid, ts, EventType.CANCEL, 0, bp, 0, bo)
                ev(iid, ts, EventType.CANCEL, 1, apx, 0, ao)
            bp, apx = mid[iid] - 1, mid[iid] + 1
            oid[0] += 2
            ev(iid, ts, EventType.ADD, 0, bp, 500, oid[0] - 1)
            ev(iid, ts, EventType.ADD, 1, apx, 500, oid[0])
            live[iid] = (oid[0] - 1, bp, oid[0], apx)
            ev(iid, ts + 500_000, EventType.TRADE, s % 2, apx if s % 2 else bp, trade_qty, 0, s + 1)
    evs.sort(key=lambda e: (e.exchange_ts, e.instrument_id, e.sequence))
    return [replace(e, event_id=i + 1) for i, e in enumerate(evs)]
