"""Cost-aware router + venue model tests (v1.12 X5; iap.execution.sor_v2).

Synthetic multi-venue books only: the real data in this repo is Nasdaq
(one venue), so these pin the capability, not a measured result.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from iap.backtest.maker import MakerBacktester, MakerConfig
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.execution import NO_ROUTE, ExecConfig, InstrumentSpec, VenueSpec, load_venues
from iap.execution.sor_v2 import CostAwareRouter, RouterOptions
from iap.execution.venues_model import (
    FeeLedger,
    FeeTier,
    VenueModel,
    build_venue_models,
    load_venue_model_config,
    month_key,
    toxicity_from_markouts,
)
from iap.orderbook.book import ConsolidatedBook
from iap.tca.fills import MarketTimeline
from iap.tca.markout import MarkoutFill
from test_maker_economics import make_scores, synth_events

REPO = Path(__file__).resolve().parents[2]
T0 = 1_700_000_000_000_000_000  # 2023-11-14 UTC
INS = 7
TICK = 0.01


def specs():
    v1 = VenueSpec(1, "A", False, 0.003, 0.002, 0.0, 100_000, 0)
    v2 = replace(v1, venue_id=2, name="B", latency_mean_ns=400_000)
    v3 = replace(v1, venue_id=3, name="C", latency_mean_ns=250_000)
    return {1: v1, 2: v2, 3: v3}


def models(**over):
    out = {v: VenueModel(spec=s) for v, s in specs().items()}
    for vid, kw in over.items():
        out[int(vid[1:])] = replace(out[int(vid[1:])], **kw)
    return out


class Feed:
    def __init__(self) -> None:
        self.book = ConsolidatedBook(INS)
        self.seq: dict[int, int] = {}
        self.oid = 0

    def add(self, vid, side, px, qty):
        s = self.seq[vid] = self.seq.get(vid, 0) + 1
        self.oid += 1
        self.book.apply(
            MarketEvent(
                s, INS, vid, T0 + s, T0 + s, s, int(EventType.ADD), side, px, qty, self.oid, 0
            )
        )


def three_venue_book() -> Feed:
    f = Feed()
    # venue 1: ask 101 x100, 102 x100; venue 2: ask 100 x50, 103 x100; venue 3: ask 101 x30
    for vid, side, px, q in (
        (1, 0, 99, 100),
        (1, 1, 101, 100),
        (1, 1, 102, 100),
        (2, 0, 98, 100),
        (2, 1, 100, 50),
        (2, 1, 103, 100),
        (3, 0, 99, 100),
        (3, 1, 101, 30),
    ):
        f.add(vid, side, px, q)
    return f


# ------------------------------------------------------------------ sweep
def test_sweep_fills_best_prices_first_and_respects_limit():
    f = three_venue_book()
    r = CostAwareRouter(models())
    kids = r.plan_sweep(f.book, 0, 200, T0)
    assert [(k.venue_id, k.price_ticks, k.qty) for k in kids] == [
        (2, 100, 50),
        (1, 101, 100),
        (3, 101, 30),
        (1, 102, 20),
    ]
    limited = r.plan_sweep(f.book, 0, 500, T0, limit_ticks=101)
    assert sum(k.qty for k in limited) == 180
    assert max(k.price_ticks for k in limited) == 101


def test_sweep_price_tie_breaks_on_tiered_taker_fee():
    f = three_venue_book()
    m = models(v3={"tiers": (FeeTier(0, 0.001, 0.0),)})
    kids = CostAwareRouter(m).plan_sweep(f.book, 0, 200, T0)
    # at 101 venue 3 (cheaper taker fee) comes before venue 1
    assert [(k.venue_id, k.price_ticks) for k in kids][:3] == [(2, 100), (3, 101), (1, 101)]


def test_sweep_latency_staggering_synchronises_arrival():
    f = three_venue_book()
    kids = CostAwareRouter(models()).plan_sweep(f.book, 0, 200, T0)
    arrivals = {k.arrival_offset_ns for k in kids}
    assert arrivals == {400_000}  # slowest used venue (2) sets the arrival
    send = {k.venue_id: k.send_offset_ns for k in kids}
    assert send == {2: 0, 3: 150_000, 1: 300_000}  # slow venue sent first
    none = CostAwareRouter(models(), RouterOptions(stagger="none")).plan_sweep(f.book, 0, 200, T0)
    assert all(k.send_offset_ns == 0 for k in none)
    assert {k.venue_id: k.arrival_offset_ns for k in none} == {2: 400_000, 1: 100_000, 3: 250_000}


def test_sweep_sell_side_and_halted_venue_skipped():
    f = three_venue_book()
    kids = CostAwareRouter(models()).plan_sweep(f.book, 1, 150, T0, candidates=[1, 2])
    assert [(k.venue_id, k.price_ticks, k.qty) for k in kids] == [(1, 99, 100), (2, 98, 50)]
    s = f.seq[1] = f.seq[1] + 1
    f.book.apply(
        MarketEvent(
            s, INS, 1, T0 + s, T0 + s, s, int(EventType.STATUS), 0, 0, int(SessionStatus.HALT), 0, 0
        )
    )
    kids = CostAwareRouter(models()).plan_sweep(f.book, 1, 150, T0, candidates=[1, 2])
    assert {k.venue_id for k in kids} == {2}


# ------------------------------------------------------------------ single venue / passive
def test_aggressive_route_is_argmin_all_in_cost():
    f = three_venue_book()
    r = CostAwareRouter(models())
    assert r.route_aggressive(f.book, 0, T0, TICK) == 2  # best ask 100
    # a big latency penalty makes the 400 us venue worse than paying a tick more
    slow = CostAwareRouter(models(), RouterOptions(latency_penalty_bps_per_ms=1000.0))
    assert slow.route_aggressive(f.book, 0, T0, TICK) == 1
    assert r.route_aggressive(ConsolidatedBook(INS), 0, T0, TICK, [1, 2]) == NO_ROUTE


def test_toxic_venue_avoided_for_passive():
    f = three_venue_book()
    # equal touches on venues 1 and 3 (bid 99); venue 1 is toxic
    m = models(v1={"toxicity_bps": {"1s": 5.0}, "p_fill_touch": 0.6}, v3={"p_fill_touch": 0.6})
    r = CostAwareRouter(m)
    assert r.route_passive(f.book, 0, T0, TICK, [1, 3]) == 3
    alloc = CostAwareRouter(m, RouterOptions(max_toxicity_bps=2.0)).allocate_passive(
        f.book, 0, 1000, [1, 2, 3]
    )
    assert 1 not in alloc and sum(alloc.values()) == 1000
    # without toxicity venue 1 (tie) wins on lower id
    assert CostAwareRouter(models()).route_passive(f.book, 0, T0, TICK, [1, 3]) == 1


def test_passive_allocation_by_fill_probability_and_lots():
    f = three_venue_book()
    m = models(v1={"p_fill_touch": 0.6}, v2={"p_fill_touch": 0.3}, v3={"p_fill_touch": 0.1})
    alloc = CostAwareRouter(m, RouterOptions(lot=100)).allocate_passive(f.book, 0, 1000)
    assert alloc == {1: 600, 2: 300, 3: 100}
    tox = models(v1={"p_fill_touch": 0.5, "toxicity_bps": {"1s": 1.0}}, v2={"p_fill_touch": 0.5})
    a = CostAwareRouter(tox).allocate_passive(f.book, 0, 90, [1, 2])
    assert a[2] > a[1] and sum(a.values()) == 90


# ------------------------------------------------------------------ tiers
def test_tier_transitions_monthly_and_deterministic():
    m = models(
        v1={
            "tiers": (
                FeeTier(0, 0.003, 0.002),
                FeeTier(1000, 0.0025, 0.0028),
                FeeTier(5000, 0.002, 0.003),
            )
        }
    )
    led = FeeLedger(m)
    assert led.record(1, 600, T0, maker=False) == pytest.approx(0.003 * 600)
    assert led.tier(1, T0) == 0
    # priced at tier 0 (volume before the fill is 600), then crosses 1000
    assert led.record(1, 600, T0, maker=True) == pytest.approx(-0.002 * 600)
    assert led.tier(1, T0) == 1
    assert led.record(1, 100, T0, maker=True) == pytest.approx(-0.0028 * 100)
    led.record(1, 4000, T0, maker=False)
    assert led.tier(1, T0) == 2 and led.quote(1, T0) == (0.002, 0.003)
    nxt = T0 + 40 * 86_400 * 10**9
    assert month_key(nxt) != month_key(T0)
    assert led.tier(1, nxt) == 0  # month rollover resets the accumulator
    led2 = FeeLedger(m)
    for q, mk in ((600, False), (600, True), (100, True), (4000, False)):
        led2.record(1, q, T0, maker=mk)
    assert led2.total_fees == led.total_fees
    with pytest.raises(ValueError):
        VenueModel(spec=specs()[1], tiers=(FeeTier(10, 0.0, 0.0),))


def test_venue_model_config_loads_and_precedence():
    venues = load_venues(REPO / "configs" / "venues" / "venues.json")
    cfg = load_venue_model_config(REPO / "research" / "execution" / "venue_model.json")
    ms = build_venue_models(venues, cfg)
    xv1 = next(m for m in ms.values() if m.spec.name == "XV1")
    assert xv1.p_fill_touch == 0.45 and len(xv1.tiers) == 3
    assert xv1.fees(0) == (0.003, 0.002) and xv1.fees(60_000_000)[1] == 0.003

    class Cal:
        doc = {"fill_rates": {"all": {"p_any_fill": 0.2}, "by_venue": {"1": {"p_any_fill": 0.7}}}}

    ms2 = build_venue_models(venues, cfg, calibration=Cal(), toxicity={1: {"1s": 9.0}})
    assert ms2[1].p_fill_touch == 0.7 and ms2[2].p_fill_touch == 0.2
    assert ms2[1].toxicity("1s") == 9.0 and ms2[1].toxicity("100ms") == 0.3
    # untiered venues fall back to the flat VenueSpec schedule
    lp = next(m for m in ms.values() if m.spec.name == "LP1")
    assert lp.tiers == () and lp.fees(10**9) == (lp.spec.taker_fee_per_share, 0.0)


def test_toxicity_from_markouts_per_venue():
    ts = [T0 + i * 10**8 for i in range(40)]
    mids = [100.0 - 0.01 * i for i in range(40)]  # falling mid
    tl = MarketTimeline()
    for t, m in zip(ts, mids, strict=True):
        tl.append(t, m - 0.005, m + 0.005, 100, 100)
    fills = [MarkoutFill(ts[i], mids[i], 100, 0, "MAKER", 1 + i % 2) for i in range(0, 20)]
    tox = toxicity_from_markouts(fills, tl)
    assert set(tox) == {1, 2}
    assert tox[1]["100ms"] > 0 and tox[1]["1s"] > tox[1]["100ms"]  # buys into a falling mid


# ------------------------------------------------------------------ backtest integration
def two_venue_events():
    """The maker-economics synthetic stream on venue 1, mirrored on venue 2
    (1 ns later, its own sequence numbers): two identical books."""
    out = []
    for k, e in enumerate(synth_events(n_steps=1500)):
        out.append(replace(e, event_id=2 * k + 1))
        out.append(
            replace(
                e,
                event_id=2 * k + 2,
                venue_id=2,
                exchange_ts=e.exchange_ts + 1,
                receive_ts=e.receive_ts + 1,
            )
        )
    return out


def two_venue_config():
    v1 = VenueSpec(1, "A", False, 0.003, 0.002, 0.0, 150_000, 0)
    return ExecConfig(
        seed=42,
        venues={1: v1, 2: replace(v1, venue_id=2, name="B")},
        instruments={INS: InstrumentSpec(INS, TICK, 1.0, 1_000_000.0)},
    )


def scores_for(evs):
    return make_scores([e for e in evs if e.venue_id == 1])


def test_maker_backtest_router_single_venue_is_identical_to_default():
    evs = [e for e in two_venue_events() if e.venue_id == 1]
    cfg = two_venue_config()
    sc = scores_for(evs)
    base = MakerBacktester(cfg, MakerConfig()).run_instrument(evs, sc, INS)
    routed = MakerBacktester(
        cfg, MakerConfig(), router=CostAwareRouter(build_venue_models(cfg.venues))
    ).run_instrument(evs, sc, INS)
    assert len(base.trips) > 3
    assert (routed.trips["venue_id"] == 1).all()
    pd.testing.assert_frame_equal(base.trips, routed.trips.drop(columns="venue_id"))
    assert base.counters == routed.counters
    assert "venue_id" not in base.trips.columns


def test_maker_backtest_router_avoids_toxic_venue():
    evs = two_venue_events()
    cfg = two_venue_config()
    sc = scores_for(evs)
    ms = build_venue_models(cfg.venues)
    ms[1] = replace(ms[1], toxicity_bps={"1s": 3.0})
    res = MakerBacktester(cfg, MakerConfig(), router=CostAwareRouter(ms)).run_instrument(
        evs, sc, INS
    )
    assert len(res.trips) > 3 and (res.trips["venue_id"] == 2).all()
    ms[1] = replace(ms[1], toxicity_bps={})
    ms[2] = replace(ms[2], toxicity_bps={"1s": 3.0})
    res2 = MakerBacktester(cfg, MakerConfig(), router=CostAwareRouter(ms)).run_instrument(
        evs, sc, INS
    )
    assert (res2.trips["venue_id"] == 1).all()
