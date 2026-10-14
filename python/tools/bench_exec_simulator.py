"""Benchmark the Python execution simulator's per-event cost.

Usage (from python/ with PYTHONPATH=src):

    python tools/bench_exec_simulator.py [--events 200000] [--open 2000]
        [--repeat 3]

Replays a long synthetic one-venue stream (adds, cancels, executes around a
moving mid) while keeping ``--open`` child orders in flight: LIMITs with
time-in-force expiries, a share of them cancelled or amended down, and a
trickle of IOCs. Reports events/s (best of ``--repeat``) and a SHA-256 of
the fills + counters, so a before/after run also checks the fills are
byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import time

from iap.core.events import EventType, MarketEvent
from iap.core.rng import SplitMix64
from iap.execution import (
    ChildOrder,
    ExecConfig,
    ExecutionSimulator,
    InstrumentSpec,
    OrderType,
    VenueSpec,
)

T0 = 1_700_000_000_000_000_000
INS = 7
VEN = 1
STEP = 100_000  # ns between market events


def make_config() -> ExecConfig:
    return ExecConfig(
        seed=42,
        impact_coeff_bps_per_pct_adv=2.0,
        venues={
            VEN: VenueSpec(
                venue_id=VEN,
                name="TST",
                is_fx=False,
                taker_fee_per_share=0.003,
                maker_rebate_per_share=0.002,
                latency_mean_ns=150_000,
                latency_jitter_ns=20_000,
            )
        },
        instruments={INS: InstrumentSpec(INS, 0.01, 1.0, 1_000_000.0)},
    )


def run(n_events: int, n_open: int) -> tuple[float, str]:
    sim = ExecutionSimulator(make_config())
    rng = SplitMix64(7)
    seq = 0
    live: dict[int, tuple[int, int, int]] = {}  # market order id -> side, px, qty
    next_mid = 1
    children: list[int] = []

    def ev(ts: int, et: EventType, side: int, px: int, qty: int, oid: int) -> MarketEvent:
        nonlocal seq
        seq += 1
        return MarketEvent(
            event_id=seq,
            instrument_id=INS,
            venue_id=VEN,
            exchange_ts=ts,
            receive_ts=ts,
            sequence=seq,
            event_type=int(et),
            side=side,
            price_ticks=px,
            qty=qty,
            order_id=oid,
            trade_id=0,
        )

    events: list[MarketEvent] = []
    for lvl in range(10):
        for side, px in ((0, 1000 - lvl), (1, 1001 + lvl)):
            live[next_mid] = (side, px, 500)
            events.append(ev(T0 + len(events), EventType.ADD, side, px, 500, next_mid))
            next_mid += 1
    t0 = time.perf_counter()
    for e in events:
        sim.on_event(e)
    ts = T0 + 1_000_000
    for i in range(n_events):
        ts += STEP
        # Keep ~n_open children in flight: submit one LIMIT per event.
        if i % 4 == 0 or len(children) < n_open:
            side = rng.below(2)
            px = 1000 - rng.below(5) if side == 0 else 1001 + rng.below(5)
            expire = ts + (1 + rng.below(4 * n_open)) * STEP
            oid = sim.submit(
                ChildOrder(
                    parent_id=1 + rng.below(50),
                    instrument_id=INS,
                    venue_id=VEN,
                    side=side,
                    type=OrderType.LIMIT,
                    limit_ticks=px,
                    qty=10 + rng.below(90),
                    decision_ts=ts,
                    expire_ts=expire,
                )
            )
            children.append(oid)
            if len(children) > 4 * n_open:
                children = children[-n_open:]
        if i % 7 == 0 and children:
            sim.cancel(children[rng.below(len(children))], ts)
        if i % 11 == 0 and children:
            sim.amend_down(children[rng.below(len(children))], 5, ts)
        if i % 13 == 0:
            sim.submit(
                ChildOrder(
                    parent_id=999,
                    instrument_id=INS,
                    venue_id=VEN,
                    side=rng.below(2),
                    type=OrderType.IOC,
                    limit_ticks=1003 if rng.below(2) else 998,
                    qty=20,
                    decision_ts=ts,
                )
            )
        r = rng.below(10)
        if r < 5 or not live:
            side = rng.below(2)
            px = 1000 - rng.below(10) if side == 0 else 1001 + rng.below(10)
            q = 50 + rng.below(450)
            live[next_mid] = (side, px, q)
            sim.on_event(ev(ts, EventType.ADD, side, px, q, next_mid))
            next_mid += 1
        else:
            mid = next(iter(live))
            side, px, q = live.pop(mid)
            et = EventType.CANCEL if r < 8 else EventType.EXECUTE
            sim.on_event(ev(ts, et, side, px, q, mid))
    sim.cancel_all()
    elapsed = time.perf_counter() - t0
    h = hashlib.sha256()
    for f in sim.fills:
        h.update(repr(f).encode())
    h.update(repr(sim.counters).encode())
    for oid in sorted(sim.orders):
        o = sim.orders[oid]
        h.update(repr((oid, o.state, o.cancel_reason, o.remaining)).encode())
    return elapsed, h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=int, default=200_000)
    ap.add_argument("--open", type=int, default=2_000)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    best = float("inf")
    digest = ""
    for _ in range(args.repeat):
        el, digest = run(args.events, args.open)
        best = min(best, el)
    print(f"events={args.events} open~{args.open} best={best:.2f}s")
    print(f"events/s={args.events / best:,.0f}")
    print(f"digest={digest}")


if __name__ == "__main__":
    main()
