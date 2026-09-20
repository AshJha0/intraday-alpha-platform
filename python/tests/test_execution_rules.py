"""Execution-simulator rule tests (pinned rules 1-9, execution.hpp).

Ports every scenario of cpp/tests/test_execution.cpp and the Java
ExecutionSimTest one-for-one (same books, same event sequences, same
expected fills), plus property tests over randomised order flow.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.core.rng import SplitMix64
from iap.execution import (
    CancelReason,
    ChildOrder,
    ExecConfig,
    ExecutionSimulator,
    InstrumentSpec,
    Liquidity,
    OrderState,
    OrderType,
    VenueSpec,
)

T0 = 1_700_000_000_000_000_000
INS = 7
VEN = 1
#: Total internal + venue-mean latency of make_config (jitter 0).
LAT = 50_000 + 50_000 + 100_000 + 150_000


def make_config(jitter_ns: int = 0) -> ExecConfig:
    """Equity-style config: one venue, jitter 0 unless a test asks otherwise."""
    return ExecConfig(
        seed=42,
        impact_coeff_bps_per_pct_adv=2.0,
        venues={
            VEN: VenueSpec(
                venue_id=VEN, name="TST", is_fx=False,
                taker_fee_per_share=0.003, maker_rebate_per_share=0.002,
                latency_mean_ns=150_000, latency_jitter_ns=jitter_ns,
            )
        },
        instruments={INS: InstrumentSpec(INS, 0.01, 1.0, 1_000_000.0)},
    )


def fx_config(adv: float = 1_000_000.0) -> ExecConfig:
    cfg = make_config()
    return replace(
        cfg,
        venues={VEN: replace(cfg.venues[VEN], is_fx=True, commission_per_million=2.5)},
        instruments={INS: InstrumentSpec(INS, 1e-05, 1000.0, adv)},
    )


class Feeder:
    """Sequenced venue-stream event factory (mirrors the C++ EventFeeder)."""

    def __init__(self) -> None:
        self.seq = 0

    def ev(self, ts, etype, side, px, qty, oid, tid=0) -> MarketEvent:
        self.seq += 1
        return MarketEvent(
            event_id=self.seq, instrument_id=INS, venue_id=VEN,
            exchange_ts=ts, receive_ts=ts, sequence=self.seq,
            event_type=int(etype), side=side, price_ticks=px, qty=qty,
            order_id=oid, trade_id=tid,
        )

    def add(self, ts, side, px, qty, oid):
        return self.ev(ts, EventType.ADD, side, px, qty, oid)

    def cancel(self, ts, side, px, qty, oid):
        return self.ev(ts, EventType.CANCEL, side, px, qty, oid)

    def exec(self, ts, side, px, qty, oid):
        return self.ev(ts, EventType.EXECUTE, side, px, qty, oid)

    def modify(self, ts, side, px, qty, oid):
        return self.ev(ts, EventType.MODIFY, side, px, qty, oid)

    def quote(self, ts, side, px, qty):
        return self.ev(ts, EventType.QUOTE, side, px, qty, 0)

    def heartbeat(self, ts):
        return self.ev(ts, EventType.HEARTBEAT, 0, 0, 0, 0)

    def status(self, ts, st: SessionStatus):
        return self.ev(ts, EventType.STATUS, 0, 0, int(st), 0)

    def gap_add(self, ts, side, px, qty, oid):
        """An ADD after a skipped sequence number (venue stream gap)."""
        self.seq += 1
        return self.add(ts, side, px, qty, oid)

    def snapshot(self, ts, side, px, qty, oid, countdown):
        self.seq += 1
        return MarketEvent(
            event_id=self.seq, instrument_id=INS, venue_id=VEN,
            exchange_ts=ts, receive_ts=ts, sequence=self.seq,
            event_type=int(EventType.SNAPSHOT), side=side, price_ticks=px,
            qty=qty, order_id=oid, trade_id=countdown,
        )


def seed_book(sim: ExecutionSimulator, f: Feeder) -> None:
    """Bids 100x300 (order 11), 99x400 (12); asks 101x200 (21), 102x500 (22)."""
    sim.on_event(f.add(T0, 0, 100, 300, 11))
    sim.on_event(f.add(T0 + 1, 0, 99, 400, 12))
    sim.on_event(f.add(T0 + 2, 1, 101, 200, 21))
    sim.on_event(f.add(T0 + 3, 1, 102, 500, 22))


def child(side, otype, px, qty, decision_ts, **kw) -> ChildOrder:
    return ChildOrder(
        parent_id=99, instrument_id=INS, venue_id=VEN, side=side, type=otype,
        limit_ticks=px, qty=qty, decision_ts=decision_ts, **kw,
    )


# ----------------------------------------------------------------- rule 4 --


def test_queue_entry_ahead_equals_displayed_depth():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))  # activates the order
    o = sim.orders[oid]
    assert o.state == OrderState.ACTIVE
    assert o.resting
    assert o.ahead_qty == 300
    assert sim.fills == []


def test_queue_execute_depletes_ahead_then_fills():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    # EXECUTE 200 at our level: all ahead (300 -> 100), no fill yet.
    sim.on_event(f.exec(T0 + 2_000_000, 0, 100, 200, 11))
    assert sim.orders[oid].ahead_qty == 100
    assert sim.fills == []
    # EXECUTE 130: 100 depletes the queue ahead, leftover 30 fills us.
    sim.on_event(f.exec(T0 + 3_000_000, 0, 100, 130, 12))
    assert len(sim.fills) == 1
    fill = sim.fills[0]
    assert (fill.qty, fill.price_ticks, fill.ts) == (30, 100, T0 + 3_000_000)
    assert fill.liquidity == Liquidity.MAKER
    assert sim.orders[oid].remaining == 20  # partial fill
    assert sim.orders[oid].state == OrderState.ACTIVE
    # Next EXECUTE fills the remainder (leftover capped at our remaining).
    sim.on_event(f.exec(T0 + 4_000_000, 0, 100, 500, 13))
    assert len(sim.fills) == 2
    assert sim.fills[1].qty == 20
    assert sim.orders[oid].state == OrderState.FILLED


def test_queue_cancel_ahead_reduces_position_deterministically():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    # An observed CANCEL at our level reduces ahead by its FULL qty.
    sim.on_event(f.cancel(T0 + 2_000_000, 0, 100, 250, 11))
    assert sim.orders[oid].ahead_qty == 50
    # A cancel at another level does nothing.
    sim.on_event(f.cancel(T0 + 2_100_000, 0, 99, 400, 12))
    assert sim.orders[oid].ahead_qty == 50
    # MODIFY events never change queue position (pinned).
    sim.on_event(f.modify(T0 + 2_200_000, 0, 100, 10, 11))
    assert sim.orders[oid].ahead_qty == 50
    # A 60-EXECUTE: 50 ahead, 10 to us.
    sim.on_event(f.exec(T0 + 3_000_000, 0, 100, 60, 11))
    assert len(sim.fills) == 1
    assert sim.fills[0].qty == 10


def test_queue_cancel_decrement_floors_at_zero():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    sim.on_event(f.cancel(T0 + 2_000_000, 0, 100, 1_000, 11))  # > ahead
    assert sim.orders[oid].ahead_qty == 0
    assert sim.fills == []  # cancels never fill us


def test_queue_trade_through_fills_at_our_price_bounded_by_traded_volume():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    # EXECUTE on the bid side BELOW our price: the market traded through us,
    # but only for what it actually traded and only behind the 300 ahead.
    sim.on_event(f.exec(T0 + 2_000_000, 0, 99, 100, 12))
    assert sim.fills == []
    assert sim.orders[oid].ahead_qty == 200
    # 240 through our level: 200 clears the queue, 40 reaches us — at OUR
    # limit, never at the (better) 99 print price.
    sim.on_event(f.exec(T0 + 3_000_000, 0, 99, 240, 12))
    assert len(sim.fills) == 1
    assert (sim.fills[0].qty, sim.fills[0].price_ticks) == (40, 100)
    assert sim.fills[0].liquidity == Liquidity.MAKER
    assert sim.orders[oid].state == OrderState.ACTIVE
    assert sim.orders[oid].remaining == 10


def test_queue_single_share_trade_through_cannot_fill_a_million():
    """DEFECT B repro: one share through a huge order fills at most one."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    sim.on_event(f.add(T0, 0, 100, 500, 11))
    sim.on_event(f.add(T0 + 1, 1, 101, 200, 21))
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 1_000_000, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 500
    # ONE share executes at 99. Old rule: the whole 1,000,000 filled.
    sim.on_event(f.exec(T0 + 2_000_000, 0, 99, 1, 12))
    assert sim.fills == []
    assert sim.orders[oid].ahead_qty == 499
    assert sim.orders[oid].remaining == 1_000_000
    # Even with the queue cleared, a 1-share print gives at most 1 share.
    sim.on_event(f.exec(T0 + 3_000_000, 0, 99, 499, 12))
    assert sim.fills == []
    sim.on_event(f.exec(T0 + 4_000_000, 0, 99, 1, 12))
    assert len(sim.fills) == 1
    assert (sim.fills[0].qty, sim.fills[0].price_ticks) == (1, 100)
    assert sim.orders[oid].remaining == 999_999


def test_queue_same_price_children_share_one_print_and_queue_behind_each_other():
    """DEFECT A repro: four same-price children share ONE 400-share print."""
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)  # bid 100 displayed 300
    ids = [
        sim.submit(child(0, OrderType.LIMIT, 100, 1000, T0 + 10 + k))
        for k in range(4)
    ]
    sim.on_event(f.heartbeat(T0 + 20 + LAT + 1))  # all four rest
    # Queue position: 300 displayed, then each earlier sibling's 1000.
    assert [sim.orders[i].ahead_qty for i in ids] == [300, 1300, 2300, 3300]
    # ONE print of 400 at 100: 300 clears the display, 100 reaches the FIRST
    # child and nobody else. Old rule: 4 x 100 = 400 filled.
    sim.on_event(f.exec(T0 + 2_000_000, 0, 100, 400, 11))
    assert len(sim.fills) == 1
    assert sim.fills[0].order_id == ids[0]
    assert (sim.fills[0].qty, sim.fills[0].price_ticks) == (100, 100)
    assert sum(x.qty for x in sim.fills) == 100
    # The budget was spent, so the siblings' queue positions are untouched:
    # the print was never cloned for them.
    assert [sim.orders[i].ahead_qty for i in ids[1:]] == [1300, 2300, 3300]


def test_queue_marketable_add_consumes_queue_ahead():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    # A marketable sell ADD at 100 for 80 executes against the book with no
    # EXECUTE events; the expansion consumes 80 of the 300 ahead of us.
    sim.on_event(f.add(T0 + 2_000_000, 1, 100, 80, 23))
    assert sim.fills == []
    assert sim.orders[oid].ahead_qty == 220
    # A deep marketable sell ADD (limit 99, qty 300): consumes the remaining
    # 220 displayed at 100, then walks on to 99 — through our 100 bid.
    sim.on_event(f.add(T0 + 3_000_000, 1, 99, 300, 24))
    assert len(sim.fills) == 1
    assert (sim.fills[0].price_ticks, sim.fills[0].qty) == (100, 50)
    assert sim.fills[0].liquidity == Liquidity.MAKER
    assert sim.orders[oid].state == OrderState.FILLED


def test_queue_marketable_add_trading_through_fills_in_full():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    # We bid 101 inside the spread (level not displayed): ahead_qty is 0.
    oid = sim.submit(child(0, OrderType.LIMIT, 101, 50, T0 + 10))
    # Cancel the displayed ask at 101 so the limit rests inside the spread.
    sim.on_event(f.cancel(T0 + 1_000, 1, 101, 200, 21))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].state == OrderState.ACTIVE
    assert sim.orders[oid].ahead_qty == 0
    # Marketable sell ADD at 100 consumes the displayed 100-bid level —
    # strictly through our 101 bid, which must have filled first (in full).
    sim.on_event(f.add(T0 + 2_000_000, 1, 100, 120, 24))
    assert len(sim.fills) == 1
    assert (sim.fills[0].price_ticks, sim.fills[0].qty) == (101, 50)
    assert sim.orders[oid].state == OrderState.FILLED


def test_queue_crossing_quote_fills_bounded_by_the_crossing_display():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].state == OrderState.ACTIVE
    assert sim.orders[oid].ahead_qty == 300
    # QUOTE: ask replaced at 100 <= our bid 100 -> crossed, but the 250 it
    # displays cannot even clear the 300 queued ahead of us.
    sim.on_event(f.quote(T0 + 2_000_000, 1, 100, 250))
    assert sim.fills == []
    assert sim.orders[oid].ahead_qty == 50
    # A bigger crossing display: 50 clears the queue, 50 fills us at our own
    # limit (the crossing rule never gives price improvement).
    sim.on_event(f.quote(T0 + 3_000_000, 1, 100, 400))
    assert len(sim.fills) == 1
    assert (sim.fills[0].price_ticks, sim.fills[0].qty) == (100, 50)
    assert sim.orders[oid].state == OrderState.FILLED


# --------------------------------------------------------------- rule 3 --


def test_market_walks_displayed_depth_one_fill_per_level():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.MARKET, 0, 250, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert [(x.price_ticks, x.qty) for x in sim.fills] == [(101, 200), (102, 50)]
    assert sim.fills[0].liquidity == Liquidity.TAKER
    # Aggressive fills are stamped with the order's arrival_ts.
    assert sim.fills[0].ts == sim.orders[oid].arrival_ts
    assert sim.orders[oid].state == OrderState.FILLED


def test_market_partial_remainder_cancelled():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 2
    assert sim.fills[0].qty + sim.fills[1].qty == 700
    assert sim.orders[oid].state == OrderState.CANCELLED
    assert sim.orders[oid].cancel_reason == CancelReason.UNFILLED_REMAINDER
    assert sim.orders[oid].remaining == 300


def test_simulated_fills_never_mutate_the_replayed_book():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    before = sim.venue_book(INS, VEN).checkpoint()
    sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 2
    after = sim.venue_book(INS, VEN).checkpoint()
    for key in ("levels", "arrival_order", "trade_flow"):
        assert after[key] == before[key]
    assert sim.venue_book(INS, VEN).depth(1) == [(101, 200), (102, 500)]


def test_marketable_limit_takes_then_rests_with_crossing_exemption():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    # Buy limit 101 for 300: takes the 200 displayed at 101, remainder 100
    # rests at 101 with nothing ahead (we cleared the displayed level).
    oid = sim.submit(child(0, OrderType.LIMIT, 101, 300, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 1
    assert (sim.fills[0].price_ticks, sim.fills[0].qty) == (101, 200)
    assert sim.fills[0].liquidity == Liquidity.TAKER
    o = sim.orders[oid]
    assert o.state == OrderState.ACTIVE
    assert o.remaining == 100
    assert o.ahead_qty == 0
    # The display still shows the ask liquidity we just consumed => the
    # order is crossing-exempt and must NOT be re-filled from it.
    assert o.cross_exempt
    sim.on_event(f.heartbeat(T0 + 20_000_000))
    assert len(sim.fills) == 1
    # Once the display goes uncrossed the exemption ends: cancel the stale
    # 101 ask (order 21), then a fresh crossing quote fills us.
    sim.on_event(f.cancel(T0 + 21_000_000, 1, 101, 200, 21))
    assert not sim.orders[oid].cross_exempt
    sim.on_event(f.quote(T0 + 22_000_000, 1, 100, 300))
    assert len(sim.fills) == 2
    assert (sim.fills[1].qty, sim.fills[1].price_ticks) == (100, 101)
    assert sim.orders[oid].state == OrderState.FILLED


def test_ioc_fills_what_it_can_then_cancels():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.IOC, 101, 300, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 1
    assert sim.fills[0].qty == 200
    assert sim.orders[oid].state == OrderState.CANCELLED
    assert sim.orders[oid].remaining == 100


def test_fok_all_or_none():
    # Kill branch: 300 wanted within limit 101 but only 200 displayed.
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.FOK, 101, 300, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.fills == []
    assert sim.orders[oid].state == OrderState.CANCELLED
    assert sim.orders[oid].remaining == 300
    # Fill branch: limit 102 spans 200 + 500 displayed >= 300.
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.FOK, 102, 300, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 2
    assert sim.fills[0].qty + sim.fills[1].qty == 300
    assert sim.orders[oid].state == OrderState.FILLED


# ------------------------------------------------------------ rules 5, 6 --


def test_fees_taker_maker_and_impact_arithmetic():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    # Taker: sell 100 into the 100 bid.
    sim.submit(child(1, OrderType.MARKET, 0, 100, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 1
    taker = sim.fills[0]
    assert taker.price_ticks == 100
    assert taker.fee == 0.003 * 100.0
    # impact_bps = 2.0 * (100 / 1e6 * 100) = 0.02 bps over notional
    # 100 * 100 ticks * 0.01 = 100.0 => 0.02e-4 * 100 = 2e-4.
    assert math.isclose(taker.impact_cost, 2e-4, abs_tol=1e-15)
    # Maker: passive buy at 100 joining 300 displayed, filled by a
    # trade-through print big enough to clear the queue (300) and reach us.
    sim.submit(child(0, OrderType.LIMIT, 100, 40, T0 + 5_000_000))
    sim.on_event(f.heartbeat(T0 + 5_000_000 + LAT + 1))
    sim.on_event(f.exec(T0 + 8_000_000, 0, 99, 340, 12))
    assert len(sim.fills) == 2
    maker = sim.fills[1]
    assert maker.liquidity == Liquidity.MAKER
    assert maker.fee == -0.002 * 40.0  # rebate: negative fee
    assert maker.impact_cost == 0.0  # passive: no impact


def test_fees_fx_commission_per_million_notional():
    sim = ExecutionSimulator(fx_config())
    f = Feeder()
    sim.on_event(f.add(T0, 0, 108650, 500, 11))
    sim.on_event(f.add(T0 + 1, 1, 108660, 500, 21))
    sim.submit(child(0, OrderType.MARKET, 0, 100, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 1
    # notional = 100 * 1000 * 108660 * 1e-5 = 108660.0
    assert math.isclose(sim.fills[0].fee, 2.5 * 108660.0 / 1e6, abs_tol=1e-12)


def test_fx_impact_scales_with_lot_size_like_the_research_model():
    sim = ExecutionSimulator(fx_config(adv=4e9))
    f = Feeder()
    sim.on_event(f.add(T0, 0, 108650, 5000, 11))
    sim.on_event(f.add(T0 + 1, 1, 108660, 5000, 21))
    sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 1
    fl = sim.fills[0]
    unit = 1000.0
    price = 108660 * 1e-05
    impact_bps = 2.0 * (1000.0 * unit / 4e9 * 100.0)
    expected = impact_bps * 1e-4 * 1000.0 * unit * price
    assert math.isclose(fl.impact_cost, expected, abs_tol=1e-12)
    assert math.isclose(impact_bps, 0.05, abs_tol=1e-15)
    # FX maker fills also pay commission (no rebate on FX venues).
    assert math.isclose(fl.fee, 2.5 * (1000.0 * unit * price) / 1e6, abs_tol=1e-12)


def test_impact_formula_matches_the_research_cost_model():
    from iap.backtest.costs import CostModel

    cfg = make_config()
    cm = CostModel(2.0, 0.003, 2.5)
    sim = ExecutionSimulator(cfg)
    f = Feeder()
    seed_book(sim, f)
    sim.submit(child(0, OrderType.MARKET, 0, 250, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    for fl in sim.fills:
        impact_bps = cm.impact_coeff_bps_per_pct_adv * (250.0 * 1.0 / 1_000_000.0 * 100.0)
        want = impact_bps * 1e-4 * float(fl.qty) * 1.0 * (fl.price_ticks * 0.01)
        assert math.isclose(fl.impact_cost, want, rel_tol=1e-12)


# ------------------------------------------------------------ rules 1, 2 --


def test_latency_arrival_decomposition_and_jitter_draw():
    jitter_ns = 50_000
    sim = ExecutionSimulator(make_config(jitter_ns))
    f = Feeder()
    seed_book(sim, f)
    rng = SplitMix64(42)  # same seed as test_config
    id1 = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    id2 = sim.submit(child(1, OrderType.LIMIT, 102, 10, T0 + 20))
    j1 = rng.below(jitter_ns + 1)
    j2 = rng.below(jitter_ns + 1)
    assert sim.orders[id1].arrival_ts == T0 + 10 + LAT + j1
    assert sim.orders[id2].arrival_ts == T0 + 20 + LAT + j2
    assert 0 <= j1 <= jitter_ns


def test_latency_zero_jitter_draws_nothing_from_the_stream():
    # jitter_ns == 0 => no RNG draw at all (pinned: draws only when > 0).
    sim = ExecutionSimulator(make_config(0))
    f = Feeder()
    seed_book(sim, f)
    sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    assert sim._rng.state == SplitMix64(42).state


def test_latency_no_fill_before_arrival_and_ordered_activation():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.MARKET, 0, 50, T0 + 10))
    arrival = sim.orders[oid].arrival_ts
    # Events strictly before arrival do NOT activate the order.
    sim.on_event(f.heartbeat(arrival - 1))
    assert sim.orders[oid].state == OrderState.PENDING
    assert sim.fills == []
    # First event at/after arrival activates; fill stamped at arrival_ts.
    sim.on_event(f.heartbeat(arrival + 500))
    assert len(sim.fills) == 1
    assert sim.fills[0].ts == arrival
    for fill in sim.fills:
        assert fill.ts >= sim.orders[fill.order_id].arrival_ts


def test_activation_order_is_arrival_then_order_id():
    # Two orders decided in reverse arrival order activate by arrival_ts;
    # the first activation takes the displayed 101 level, the second sees
    # the overlay remainder.
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    late = sim.submit(child(0, OrderType.MARKET, 0, 150, T0 + 20))
    early = sim.submit(child(0, OrderType.MARKET, 0, 150, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 20 + LAT + 1))
    assert [x.order_id for x in sim.fills] == [early, late, late]
    assert [(x.price_ticks, x.qty) for x in sim.fills] == [(101, 150), (101, 50), (102, 100)]


def test_activation_happens_before_the_event_is_applied():
    # The activating event is itself a marketable ADD that would consume
    # the 101 ask: our MARKET order fills against the PRE-event display.
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.MARKET, 0, 200, T0 + 10))
    sim.on_event(f.add(T0 + 10 + LAT + 1, 0, 101, 200, 31))
    assert [(x.price_ticks, x.qty) for x in sim.fills] == [(101, 200)]
    assert sim.orders[oid].state == OrderState.FILLED
    assert sim.venue_book(INS, VEN).best_ask() == (102, 500)


# --------------------------------------------------------------- rule 3b --


def test_overlay_second_child_sees_the_thin_remainder():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)  # asks 101x200, 102x500 = 700 displayed
    c1 = sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10))
    c2 = sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))  # both activate here
    assert sum(x.qty for x in sim.fills) == 700  # total taker qty <= displayed depth
    assert sim.orders[c1].remaining == 300
    assert sim.orders[c2].remaining == 1000
    assert sim.orders[c2].state == OrderState.CANCELLED
    assert sim.counters.overlay_thinned_fills >= 1
    # The display refreshes (a new ask of 150 at 101 on top of the 200 we
    # took): exactly the NEW 150 is available (overlay = min(200, 350)).
    sim.on_event(f.add(T0 + 20, 1, 101, 150, 24))
    c3 = sim.submit(child(0, OrderType.MARKET, 0, 200, T0 + 30))
    sim.on_event(f.heartbeat(T0 + 30 + LAT + 1))
    assert sim.orders[c3].state == OrderState.CANCELLED
    assert sim.orders[c3].remaining == 50
    assert (sim.fills[-1].price_ticks, sim.fills[-1].qty) == (101, 150)
    # FOK availability also nets the overlay: 101/102 still display 350/500
    # but everything was consumed -> a 100-lot FOK at 102 misses.
    fok = sim.submit(child(0, OrderType.FOK, 102, 100, T0 + 40))
    sim.on_event(f.heartbeat(T0 + 40 + LAT + 1))
    assert sim.orders[fok].state == OrderState.CANCELLED
    assert sim.orders[fok].remaining == 100
    # An observed EXECUTE of 300 at 101 (display 350 -> 50) caps the
    # overlay at 50: still nothing available for us.
    sim.on_event(f.exec(T0 + 50, 1, 101, 300, 21))
    c4 = sim.submit(child(0, OrderType.IOC, 101, 10, T0 + 60))
    sim.on_event(f.heartbeat(T0 + 60 + LAT + 1))
    assert sim.orders[c4].remaining == 10
    # ... until the level empties and is re-posted.
    sim.on_event(f.cancel(T0 + 70, 1, 101, 50, 21))
    sim.on_event(f.add(T0 + 80, 1, 101, 40, 25))
    c5 = sim.submit(child(0, OrderType.IOC, 101, 10, T0 + 90))
    sim.on_event(f.heartbeat(T0 + 90 + LAT + 1))
    assert sim.orders[c5].state == OrderState.FILLED


def test_overlay_never_reuses_liquidity_across_events_without_refresh():
    # Ten sequential IOC children against a static 200-lot display: the
    # total taken can never exceed the displayed size.
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    for k in range(10):
        sim.submit(child(0, OrderType.IOC, 101, 60, T0 + 1_000 * (k + 1)))
        sim.on_event(f.heartbeat(T0 + 1_000 * (k + 1) + LAT + 1))
    assert sum(x.qty for x in sim.fills) == 200
    assert sim.venue_book(INS, VEN).best_ask() == (101, 200)  # book untouched


# --------------------------------------------------------------- rule 7 --


def test_cancel_and_validation():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    # The cancel travels the same latency path; applied at the first event
    # at/after its arrival (never before the order's own).
    sim.cancel(oid, T0 + 10)
    assert sim.orders[oid].state == OrderState.PENDING
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].state == OrderState.CANCELLED
    assert sim.orders[oid].cancel_reason == CancelReason.USER
    sim.cancel(oid, T0 + 20)  # idempotent on terminal states
    sim.on_event(f.exec(T0 + 2_000_000, 0, 99, 500, 12))
    assert sim.fills == []  # cancelled orders never fill
    with pytest.raises(ValueError):
        sim.cancel(9999, T0)
    with pytest.raises(ValueError):
        sim.submit(child(0, OrderType.LIMIT, 0, 10, T0))  # limit needs px
    with pytest.raises(ValueError):
        sim.submit(child(0, OrderType.MARKET, 0, 0, T0))  # qty > 0
    with pytest.raises(ValueError):
        sim.submit(replace(child(0, OrderType.MARKET, 0, 10, T0), venue_id=999))
    with pytest.raises(ValueError):
        sim.submit(child(2, OrderType.MARKET, 0, 10, T0))  # side domain
    with pytest.raises(ValueError):
        sim.submit(child(0, OrderType.MARKET, 0, 10, T0, expire_ts=-1))


def test_submit_does_not_mutate_the_callers_record():
    sim = ExecutionSimulator(make_config())
    req = child(0, OrderType.LIMIT, 99, 10, T0 + 10)
    oid = sim.submit(req)
    assert req.order_id == 0 and req.arrival_ts == 0 and req.remaining == 0
    assert sim.orders[oid] is not req
    assert sim.orders[oid].remaining == 10


def test_cancel_cannot_undo_an_earlier_fill():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))  # resting, 300 ahead
    sim.cancel(oid, T0 + 1_000_000)
    assert sim.orders[oid].cancel_arrival_ts == T0 + 1_000_000 + LAT
    # The market trades through our level BEFORE the cancel arrives.
    sim.on_event(f.exec(T0 + 1_100_000, 0, 99, 400, 12))
    assert len(sim.fills) == 1
    assert sim.orders[oid].state == OrderState.FILLED
    # The cancel's arrival is then a no-op (order already terminal).
    sim.on_event(f.heartbeat(T0 + 2_000_000))
    assert sim.orders[oid].state == OrderState.FILLED
    assert sim.counters.user_cancels == 0
    # Control: a cancel that arrives first prevents the later fill.
    id2 = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 3_000_000))
    sim.on_event(f.heartbeat(T0 + 3_000_000 + LAT + 1))
    sim.cancel(id2, T0 + 3_100_000)
    sim.on_event(f.heartbeat(T0 + 3_100_000 + LAT + 1))  # cancel lands
    assert sim.orders[id2].state == OrderState.CANCELLED
    sim.on_event(f.exec(T0 + 4_000_000, 0, 99, 400, 12))
    assert len(sim.fills) == 1  # cancelled order never fills
    assert sim.counters.user_cancels == 1


def test_cancel_never_overtakes_its_order():
    sim = ExecutionSimulator(make_config(50_000))  # jittered venue
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.MARKET, 0, 50, T0 + 10))
    sim.cancel(oid, T0 + 10)  # same decision time, different jitter draw
    o = sim.orders[oid]
    assert o.cancel_arrival_ts >= o.arrival_ts
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 100_000))
    # The MARKET order activated (and filled) before the cancel applied.
    assert o.state == OrderState.FILLED
    assert len(sim.fills) == 1


def test_cancel_consumes_one_jitter_draw_and_ties_activate_first():
    jitter = 50_000
    sim = ExecutionSimulator(make_config(jitter))
    f = Feeder()
    seed_book(sim, f)
    rng = SplitMix64(42)
    oid = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    j1 = rng.below(jitter + 1)
    sim.cancel(oid, T0 + 10)
    j2 = rng.below(jitter + 1)
    o = sim.orders[oid]
    assert o.arrival_ts == T0 + 10 + LAT + j1
    assert o.cancel_arrival_ts == max(T0 + 10 + LAT + j2, o.arrival_ts)
    # Activation and cancel both due at the same event: the order rests
    # first (activation wins the tie / precedes), then the cancel lands.
    sim.on_event(f.heartbeat(T0 + 10 + LAT + jitter + 1))
    assert o.state == OrderState.CANCELLED
    assert o.cancel_reason == CancelReason.USER
    assert sim.counters.user_cancels == 1


def test_cancel_arrivals_are_applied_in_effective_time_order():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    a = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    b = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    sim.cancel(b, T0 + 2_000_000)  # decided first ...
    sim.cancel(a, T0 + 1_000_000)  # ... but a's cancel arrives earlier
    sim.on_event(f.heartbeat(T0 + 1_000_000 + LAT))
    assert sim.orders[a].state == OrderState.CANCELLED
    assert sim.orders[b].state == OrderState.ACTIVE
    sim.on_event(f.heartbeat(T0 + 2_000_000 + LAT))
    assert sim.orders[b].state == OrderState.CANCELLED


def test_expiry_expires_pending_and_resting_orders_before_activation():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    r = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10, expire_ts=T0 + 5_000_000))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[r].state == OrderState.ACTIVE
    late = sim.submit(
        child(0, OrderType.MARKET, 0, 50, T0 + 4_900_000, expire_ts=T0 + 5_000_000)
    )
    sim.on_event(f.exec(T0 + 5_000_000, 0, 99, 400, 12))  # trade-through!
    assert sim.orders[r].state == OrderState.CANCELLED
    assert sim.orders[r].cancel_reason == CancelReason.EXPIRED
    assert sim.orders[late].state == OrderState.CANCELLED
    assert sim.orders[late].cancel_reason == CancelReason.EXPIRED
    assert sim.fills == []  # expired before the trade-through
    assert sim.counters.expired_orders == 2


def test_cancel_all_is_the_end_of_stream_sweep():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    resting = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    pending = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 5_000_000))
    filled = sim.submit(child(1, OrderType.MARKET, 0, 10, T0 + 20))
    sim.on_event(f.heartbeat(T0 + 20 + LAT + 1))
    sim.cancel(resting, T0 + 30)  # in flight when the stream ends
    sim.cancel_all()
    assert sim.orders[resting].state == OrderState.CANCELLED
    assert sim.orders[resting].cancel_reason == CancelReason.END_OF_STREAM
    assert sim.orders[pending].state == OrderState.CANCELLED
    assert sim.orders[pending].cancel_reason == CancelReason.END_OF_STREAM
    assert sim.orders[filled].state == OrderState.FILLED
    assert sim.counters.user_cancels == 0  # the sweep is not a user cancel
    # Nothing left in flight: a later trade-through fills nobody.
    sim.on_event(f.exec(T0 + 9_000_000, 0, 98, 400, 12))
    assert len(sim.fills) == 1


# --------------------------------------------------------------- rule 8 --


def test_halt_then_reopen_auction_no_fill_during_halt_uncross_at_touch():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    sim.on_event(f.status(T0 + 100, SessionStatus.HALT))
    assert not ExecutionSimulator.venue_open(sim.venue_book(INS, VEN))
    mkt = sim.submit(child(0, OrderType.MARKET, 0, 50, T0 + 200))
    lim = sim.submit(child(0, OrderType.LIMIT, 103, 60, T0 + 200))
    sim.on_event(f.heartbeat(T0 + 200 + LAT + 1))  # both arrive: halted
    assert sim.orders[mkt].state == OrderState.CANCELLED
    assert sim.orders[mkt].cancel_reason == CancelReason.VENUE_NOT_TRADING
    assert sim.counters.venue_not_trading_cancels == 1
    assert sim.orders[lim].state == OrderState.ACTIVE
    assert not sim.orders[lim].cross_exempt  # gated: nothing consumed
    assert sim.fills == []  # no fill through a halt
    # Observed executions at our level during the halt do not touch us.
    sim.on_event(f.exec(T0 + 300, 1, 101, 200, 21))
    assert sim.fills == []
    # Auction call: a new ask posts at 100 (the uncross price), still no fill.
    sim.on_event(f.status(T0 + 400, SessionStatus.AUCTION))
    sim.on_event(f.add(T0 + 500, 1, 100, 500, 23))
    assert sim.fills == []
    # Uncross: TRADING again -> the crossed resting buy fills at the touch.
    sim.on_event(f.status(T0 + 600, SessionStatus.TRADING))
    assert len(sim.fills) == 1
    assert sim.fills[0].price_ticks == 100  # touch, not the 103 limit
    assert sim.fills[0].qty == 60
    assert sim.fills[0].ts == T0 + 600
    assert sim.fills[0].liquidity == Liquidity.MAKER
    assert sim.counters.reopen_touch_fills == 1
    assert sim.orders[lim].state == OrderState.FILLED


@pytest.mark.parametrize("otype", [OrderType.MARKET, OrderType.IOC, OrderType.FOK])
def test_venue_not_trading_rejects_every_aggressive_type(otype):
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    sim.on_event(f.status(T0 + 100, SessionStatus.CLOSE))
    oid = sim.submit(child(0, otype, 102, 50, T0 + 200))
    sim.on_event(f.heartbeat(T0 + 200 + LAT + 1))
    o = sim.orders[oid]
    assert o.state == OrderState.CANCELLED
    assert o.cancel_reason == CancelReason.VENUE_NOT_TRADING
    assert o.remaining == 50
    assert sim.fills == []
    assert sim.counters.venue_not_trading_cancels == 1


def test_missing_venue_book_gates_like_a_halt():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    # Both arrive before the first event of the venue stream ever lands.
    mkt = sim.submit(child(0, OrderType.MARKET, 0, 50, T0 - LAT - 100))
    lim = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 - LAT - 100))
    seed_book(sim, f)  # the first event (ts T0) activates both: no book yet
    assert sim.orders[mkt].state == OrderState.CANCELLED
    assert sim.orders[mkt].cancel_reason == CancelReason.VENUE_NOT_TRADING
    assert sim.orders[lim].state == OrderState.ACTIVE
    assert sim.orders[lim].ahead_qty == 0


def test_stale_book_no_fill_then_snapshot_recovery():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    sim.on_event(f.gap_add(T0 + 100, 0, 98, 100, 13))  # gap -> stale
    assert sim.venue_book(INS, VEN).stale
    a = sim.submit(child(1, OrderType.MARKET, 0, 50, T0 + 200))
    sim.on_event(f.heartbeat(T0 + 200 + LAT + 1))
    assert sim.orders[a].state == OrderState.CANCELLED
    assert sim.orders[a].cancel_reason == CancelReason.VENUE_NOT_TRADING
    assert sim.fills == []
    # Snapshot burst (countdown 2, 1, 0) rebuilds the book and clears stale.
    sim.on_event(f.snapshot(T0 + 300, 0, 100, 300, 31, 2))
    sim.on_event(f.snapshot(T0 + 301, 1, 101, 200, 32, 1))
    sim.on_event(f.snapshot(T0 + 302, 1, 102, 500, 33, 0))
    assert not sim.venue_book(INS, VEN).stale
    b = sim.submit(child(1, OrderType.MARKET, 0, 50, T0 + 400))
    sim.on_event(f.heartbeat(T0 + 400 + LAT + 1))
    assert len(sim.fills) == 1
    assert sim.fills[0].order_id == b
    assert sim.fills[0].price_ticks == 100


def test_resting_order_is_not_consumed_while_gated_and_not_crossed_on_reopen_if_uncrossed():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    sim.on_event(f.status(T0 + 100, SessionStatus.HALT))
    sim.on_event(f.exec(T0 + 200, 0, 100, 300, 11))  # would fill us if open
    sim.on_event(f.exec(T0 + 300, 0, 99, 100, 12))  # trade-through, gated
    assert sim.fills == []
    assert sim.orders[oid].ahead_qty == 300  # untouched while gated
    sim.on_event(f.status(T0 + 400, SessionStatus.TRADING))
    # Reopen with an uncrossed display (ask 101 > our 100): no touch fill.
    assert sim.fills == []
    assert sim.counters.reopen_touch_fills == 0
    assert sim.orders[oid].state == OrderState.ACTIVE


# --------------------------------------------------------------- rule 9 --


def test_processing_order_expiry_precedes_activation_and_tracking_precedes_apply():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    # (a) an order expiring at the very event that would activate it never
    #     activates (expiries first).
    e = sim.submit(child(0, OrderType.MARKET, 0, 10, T0 + 10, expire_ts=T0 + 10 + LAT + 1))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[e].state == OrderState.CANCELLED
    assert sim.orders[e].cancel_reason == CancelReason.EXPIRED
    assert sim.fills == []
    # (b) a resting order tracks the raw EXECUTE before the book update:
    #     an EXECUTE that empties the 100 level fills us from its leftover
    #     and the crossing check afterwards sees no double fill.
    r = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 20))
    sim.on_event(f.heartbeat(T0 + 20 + LAT + 1))
    sim.on_event(f.exec(T0 + 1_000_000, 0, 100, 300, 11))  # ahead 300 -> 0
    assert sim.fills == []
    sim.on_event(f.exec(T0 + 1_000_001, 0, 100, 20, 12))  # unknown at level: leftover
    assert [(x.qty, x.price_ticks) for x in sim.fills] == [(20, 100)]
    assert sim.orders[r].remaining == 30
    assert sim.orders[r].state == OrderState.ACTIVE


def test_processing_order_marketable_limit_activation_takes_pre_event_display():
    sim = ExecutionSimulator(make_config())
    f = Feeder()
    seed_book(sim, f)
    lim = sim.submit(child(0, OrderType.LIMIT, 101, 250, T0 + 10))
    # The activating event removes the 101 ask: the aggressive leg still
    # takes the pre-event 200 (activation precedes the book update), the
    # remainder rests crossing-exempt, and the post-event display (ask 102)
    # is uncrossed so the exemption ends immediately.
    sim.on_event(f.cancel(T0 + 10 + LAT + 1, 1, 101, 200, 21))
    o = sim.orders[lim]
    assert [(x.price_ticks, x.qty) for x in sim.fills] == [(101, 200)]
    assert o.state == OrderState.ACTIVE and o.remaining == 50
    assert not o.cross_exempt


# --------------------------------------------------------- determinism --


def test_determinism_same_config_same_fills():
    def run(seed: int):
        cfg = replace(make_config(50_000), seed=seed)
        sim = ExecutionSimulator(cfg)
        f = Feeder()
        seed_book(sim, f)
        sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
        sim.submit(child(1, OrderType.MARKET, 0, 120, T0 + 20))
        sim.on_event(f.heartbeat(T0 + 1_000_000))
        sim.on_event(f.exec(T0 + 2_000_000, 0, 100, 320, 11))
        sim.on_event(f.exec(T0 + 3_000_000, 0, 100, 100, 12))
        sim.cancel_all()
        return [x.to_dict() for x in sim.fills]

    a = run(42)
    b = run(42)
    assert a == b
    assert a


# ------------------------------------------------------- property tests --


def _random_session(seed: int):
    """Random order flow against a random-walking two-sided book."""
    rng = SplitMix64(seed)
    sim = ExecutionSimulator(make_config(jitter_ns=20_000))
    f = Feeder()
    seed_book(sim, f)
    ts = T0 + 100
    next_oid = 100
    resting = {11: (0, 100), 12: (0, 99), 21: (1, 101), 22: (1, 102)}
    submitted = []
    book_before = []
    for _ in range(300):
        ts += 1_000 + rng.below(400_000)
        roll = rng.below(10)
        if roll < 3:
            side = rng.below(2)
            base = 100 if side == 0 else 101
            px = base + (rng.below(4) - 3 if side == 0 else -(rng.below(4) - 3))
            px = max(px, 90)
            qty = 10 + rng.below(300)
            resting[next_oid] = (side, px)
            sim.on_event(f.add(ts, side, px, qty, next_oid))
            next_oid += 1
        elif roll < 5 and resting:
            oid = sorted(resting)[rng.below(len(resting))]
            side, px = resting.pop(oid)
            sim.on_event(f.cancel(ts, side, px, 1 + rng.below(200), oid))
        elif roll < 7 and resting:
            oid = sorted(resting)[rng.below(len(resting))]
            side, px = resting[oid]
            sim.on_event(f.exec(ts, side, px, 1 + rng.below(150), oid))
        elif roll < 9:
            otype = [OrderType.MARKET, OrderType.LIMIT, OrderType.IOC, OrderType.FOK][rng.below(4)]
            side = rng.below(2)
            px = 100 + rng.below(4) - 1 if side == 0 else 101 - rng.below(4) + 1
            expire = ts + 5_000_000 + rng.below(50_000_000) if rng.below(2) else 0
            snap = sim.venue_book(INS, VEN).checkpoint()
            oid = sim.submit(child(side, otype, px, 1 + rng.below(400), ts, expire_ts=expire))
            submitted.append(oid)
            book_before.append((oid, snap))
            if rng.below(4) == 0:
                sim.cancel(oid, ts + rng.below(2_000_000))
        else:
            sim.on_event(f.heartbeat(ts))
    sim.on_event(f.heartbeat(ts + 10 * LAT))
    sim.cancel_all()
    return sim, submitted


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_property_filled_qty_never_exceeds_submitted_qty(seed):
    sim, submitted = _random_session(seed)
    assert submitted
    filled = {}
    for fl in sim.fills:
        assert fl.qty > 0
        filled[fl.order_id] = filled.get(fl.order_id, 0) + fl.qty
    for oid, o in sim.orders.items():
        got = filled.get(oid, 0)
        assert got <= o.qty, oid
        assert o.remaining == o.qty - got, oid
        assert o.is_terminal, oid  # cancel_all swept everything
        if o.state == OrderState.FILLED:
            assert o.remaining == 0
        else:
            assert o.cancel_reason != CancelReason.NONE


@pytest.mark.parametrize("seed", [11, 12, 13, 14])
def test_property_fill_never_outside_arrival_and_expiry(seed):
    sim, _ = _random_session(seed)
    assert sim.fills
    for fl in sim.fills:
        o = sim.orders[fl.order_id]
        assert fl.ts >= o.arrival_ts
        if o.expire_ts:
            assert fl.ts <= o.expire_ts
        if fl.liquidity == Liquidity.TAKER:
            assert fl.ts == o.arrival_ts
            assert fl.impact_cost > 0.0
        else:
            assert fl.impact_cost == 0.0
            assert fl.fee < 0.0
    for i in range(1, len(sim.fills)):
        assert sim.fills[i].fill_id == sim.fills[i - 1].fill_id + 1
        assert sim.fills[i].ts >= sim.fills[i - 1].ts


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_property_simulated_fills_never_mutate_the_replayed_book(seed):
    # Replay the same venue stream through a bare book and through the
    # simulator: the two books must be identical after every event.
    from iap.orderbook.book import OrderBook

    rng = SplitMix64(seed)
    sim = ExecutionSimulator(make_config())
    bare = OrderBook(INS, VEN)
    f = Feeder()
    events = [f.add(T0, 0, 100, 300, 11), f.add(T0 + 1, 0, 99, 400, 12),
              f.add(T0 + 2, 1, 101, 200, 21), f.add(T0 + 3, 1, 102, 500, 22)]
    ts = T0 + 10
    for k in range(200):
        ts += 1_000 + rng.below(300_000)
        roll = rng.below(4)
        if roll == 0:
            events.append(f.add(ts, rng.below(2), 98 + rng.below(6), 1 + rng.below(300), 1000 + k))
        elif roll == 1:
            events.append(f.exec(ts, 0, 100, 1 + rng.below(100), 11))
        elif roll == 2:
            events.append(f.quote(ts, 1, 99 + rng.below(4), 1 + rng.below(300)))
        else:
            events.append(f.heartbeat(ts))
    for i, ev in enumerate(events):
        if i % 5 == 0:
            otype = [OrderType.MARKET, OrderType.LIMIT, OrderType.IOC][i % 3]
            sim.submit(child(i % 2, otype, 100, 1 + (i * 7) % 300, ev.exchange_ts - LAT))
        sim.on_event(ev)
        bare.apply(ev)
        assert sim.venue_book(INS, VEN).checkpoint() == bare.checkpoint()
    assert sim.fills  # the run actually traded
