"""Smart-order-router tests (pinned rules: iap.execution.sor / cpp sor.hpp).

Ports the SorRouting groups of cpp/tests/test_exec_algos.cpp and the Java
ExecutionScenarioTest SOR rows, plus the config loaders the router and the
simulator resolve their inputs from.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.execution import (
    NO_ROUTE,
    ExecConfig,
    InstrumentSpec,
    LatencyConfig,
    SmartOrderRouter,
    SorOptions,
    VenueSpec,
    load_exec_config,
    load_instruments,
    load_sor_options,
    load_venues,
)
from iap.orderbook.book import ConsolidatedBook

T0 = 1_700_000_000_000_000_000
I64_MAX = (1 << 63) - 1


def sor_venues():
    v1 = VenueSpec(
        venue_id=1, name="TST", taker_fee_per_share=0.003,
        maker_rebate_per_share=0.002, latency_mean_ns=100_000, latency_jitter_ns=0,
    )
    v2 = replace(v1, venue_id=2, taker_fee_per_share=0.001, maker_rebate_per_share=0.0025)
    return {1: v1, 2: v2}


class Book:
    """Per-venue sequenced feeder into one ConsolidatedBook."""

    def __init__(self) -> None:
        self.book = ConsolidatedBook(7)
        self.seq = {1: 0, 2: 0}

    def push(self, vid, etype, side, px, qty, oid, gap=False):
        if gap:
            self.seq[vid] += 1
        self.seq[vid] += 1
        s = self.seq[vid]
        self.book.apply(MarketEvent(
            event_id=s, instrument_id=7, venue_id=vid, exchange_ts=T0 + s,
            receive_ts=T0 + s, sequence=s, event_type=int(etype), side=side,
            price_ticks=px, qty=qty, order_id=oid, trade_id=0,
        ))

    def add(self, vid, side, px, qty, oid, gap=False):
        self.push(vid, EventType.ADD, side, px, qty, oid, gap)

    def status(self, vid, st: SessionStatus):
        self.push(vid, EventType.STATUS, 0, 0, int(st), 0)


def test_aggressive_prefers_price_then_fee():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    # Venue 1 asks 101; venue 2 asks 100 (better) -> buy routes to 2.
    b.add(1, 1, 101, 500, 11)
    b.add(2, 1, 100, 500, 21)
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2
    # Equal best ask: tie broken by lower taker fee (venue 2 at 0.001).
    b.add(2, 1, 101, 100, 22)
    b.add(1, 1, 100, 100, 12)  # now both quote 100
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2
    # A sell routes to the best (highest) bid.
    b.add(1, 0, 99, 500, 13)
    b.add(2, 0, 98, 500, 23)
    assert sor.route_aggressive(b.book, 1, [1, 2]) == 1
    # Candidate order does not matter: iteration is by ascending venue_id.
    assert sor.route_aggressive(b.book, 1, [2, 1]) == 1
    # A candidate outside the list is never chosen.
    assert sor.route_aggressive(b.book, 1, [2]) == 2


def test_aggressive_ties_break_on_venue_id_last():
    venues = sor_venues()
    venues[2] = replace(venues[2], taker_fee_per_share=0.003)  # equal fees
    sor = SmartOrderRouter(venues)
    b = Book()
    b.add(1, 1, 100, 500, 11)
    b.add(2, 1, 100, 500, 21)
    assert sor.route_aggressive(b.book, 0, [2, 1]) == 1


def test_passive_prefers_rebate_and_reports_no_route():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    # No venue quotes anything: no route (0), never a blind fallback.
    assert sor.route_passive(b.book, 0, [2, 1]) == NO_ROUTE
    assert sor.route_aggressive(b.book, 0, [2, 1]) == NO_ROUTE
    with pytest.raises(ValueError):
        sor.route_passive(b.book, 0, [])
    with pytest.raises(ValueError):
        sor.route_aggressive(b.book, 0, [])
    # Both venues quote the bid side: venue 2 pays the higher rebate.
    b.add(1, 0, 99, 100, 11)
    b.add(2, 0, 99, 100, 21)
    assert sor.route_passive(b.book, 0, [1, 2]) == 2
    # Only venue 1 quotes the ask side.
    b.add(1, 1, 101, 100, 12)
    assert sor.route_passive(b.book, 1, [1, 2]) == 1
    # Rebate ties: lower commission, then lower venue id.
    venues = sor_venues()
    venues[1] = replace(venues[1], commission_per_million=1.0)
    venues[2] = replace(venues[2], maker_rebate_per_share=0.002, commission_per_million=0.5)
    assert SmartOrderRouter(venues).route_passive(b.book, 0, [1, 2]) == 2
    venues[2] = replace(venues[2], commission_per_million=1.0)
    assert SmartOrderRouter(venues).route_passive(b.book, 0, [2, 1]) == 1


def test_passive_without_rebate_preference_takes_lowest_venue_id():
    plain = SmartOrderRouter(sor_venues(), SorOptions(prefer_rebate=False))
    b = Book()
    b.add(2, 0, 99, 100, 21)
    assert plain.route_passive(b.book, 0, [1, 2]) == 2  # only venue quoting
    b.add(1, 0, 99, 100, 11)
    assert plain.route_passive(b.book, 0, [1, 2]) == 1  # lowest id, rebate ignored
    assert plain.options == SorOptions(False, I64_MAX)


def test_never_routes_to_stale_or_halted_venues():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    b.add(1, 1, 100, 500, 11)  # venue 1 ask 100 (best)
    b.add(2, 1, 101, 500, 21)  # venue 2 ask 101
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 1
    b.add(1, 1, 100, 100, 12, gap=True)  # venue 1 gaps -> stale
    assert b.book.books[1].stale
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2  # skip stale
    b.status(2, SessionStatus.HALT)
    assert sor.route_aggressive(b.book, 0, [1, 2]) == NO_ROUTE  # all gated
    b.status(2, SessionStatus.TRADING)
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2
    # A venue with no book at all is not eligible either.
    assert sor.route_aggressive(b.book, 0, [2, 3]) == 2
    assert sor.route_aggressive(b.book, 0, [3]) == NO_ROUTE
    # Latency budget: a venue slower than max_venue_latency_ns is skipped.
    strict = SmartOrderRouter(sor_venues(), SorOptions(max_venue_latency_ns=50_000))
    assert strict.route_aggressive(b.book, 0, [1, 2]) == NO_ROUTE
    exact = SmartOrderRouter(sor_venues(), SorOptions(max_venue_latency_ns=100_000))
    assert exact.route_aggressive(b.book, 0, [1, 2]) == 2  # <= budget is fine
    plain = SmartOrderRouter(sor_venues(), SorOptions(prefer_rebate=False))
    b.add(2, 0, 99, 100, 22)
    assert plain.route_passive(b.book, 0, [1, 2]) == 2  # only eligible


def test_stale_venue_recovers_after_a_complete_snapshot_burst():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    b.add(1, 1, 100, 500, 11)
    b.add(2, 1, 101, 500, 21)
    b.add(1, 1, 100, 100, 12, gap=True)
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2
    s = b.seq[1]
    for countdown, (side, px, qty, oid) in enumerate(
        [(0, 99, 100, 31), (1, 100, 500, 32)], start=0
    ):
        s += 1
        b.book.apply(MarketEvent(
            event_id=s, instrument_id=7, venue_id=1, exchange_ts=T0 + s,
            receive_ts=T0 + s, sequence=s, event_type=int(EventType.SNAPSHOT),
            side=side, price_ticks=px, qty=qty, order_id=oid, trade_id=1 - countdown,
        ))
    b.seq[1] = s
    assert not b.book.books[1].stale
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 1  # best ask again


def test_aggressive_tie_breaks_on_commission_for_fx():
    venues = sor_venues()
    venues[1] = replace(venues[1], is_fx=True, taker_fee_per_share=0.0, commission_per_million=4.0)
    venues[2] = replace(venues[2], is_fx=True, taker_fee_per_share=0.0, commission_per_million=2.5)
    sor = SmartOrderRouter(venues)
    b = Book()
    b.add(1, 1, 100, 500, 11)
    b.add(2, 1, 100, 500, 21)
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2  # 2.5/M beats 4.0/M


def test_venue_without_a_profile_is_routable_with_zero_fees():
    # The router only consults the profile map for fees and the latency
    # budget; a candidate missing from it counts as fee 0 / no budget.
    sor = SmartOrderRouter({1: sor_venues()[1]}, SorOptions(max_venue_latency_ns=10))
    b = Book()
    b.add(1, 1, 100, 500, 11)
    b.add(2, 1, 100, 500, 21)
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 2  # venue 1 over budget


# ------------------------------------------------------------ config I/O --


def test_load_venues_from_configs(golden_dir):
    venues = load_venues(golden_dir.parents[1] / "configs" / "venues" / "venues.json")
    assert list(venues) == [1, 2, 10, 11, 12]
    xv1 = venues[1]
    assert xv1 == VenueSpec(1, "XV1", False, 0.003, 0.002, 0.0, 150_000, 50_000)
    pri = venues[12]
    assert pri.is_fx and pri.commission_per_million == 2.5
    assert pri.taker_fee_per_share == 0.0 and pri.maker_rebate_per_share == 0.0
    assert (pri.latency_mean_ns, pri.latency_jitter_ns) == (200_000, 60_000)


def test_load_instruments_from_configs(golden_dir):
    ins = load_instruments(golden_dir.parents[1] / "configs" / "instruments" / "instruments.json")
    assert ins[1] == InstrumentSpec(1, 0.01, 1.0, 38_000_000.0, "USD")
    assert ins[101] == InstrumentSpec(101, 1e-05, 1000.0, 4e9, "USD")
    assert ins[103].quote_ccy == "JPY" and ins[103].qty_unit == 1000.0
    assert ins[11].qty_unit == 1.0  # ETF: qty already in shares


def test_load_exec_config_and_sor_options(golden_dir):
    configs = golden_dir.parents[1] / "configs"
    cfg = load_exec_config(configs)
    assert cfg.seed == 20260829
    assert cfg.impact_coeff_bps_per_pct_adv == 2.0
    assert cfg.latency == LatencyConfig(50_000, 50_000, 100_000)
    assert cfg.latency.internal_ns == 200_000
    assert set(cfg.venues) == {1, 2, 10, 11, 12}
    assert 1 in cfg.instruments and 108 in cfg.instruments
    assert cfg.venue(1).name == "XV1"
    assert cfg.instrument(101).qty_unit == 1000.0
    with pytest.raises(ValueError):
        cfg.venue(999)
    with pytest.raises(ValueError):
        cfg.instrument(999)
    opts = load_sor_options(configs / "execution" / "execution.json")
    assert opts == SorOptions(True, 1_000_000)


def test_config_loaders_fail_fast_on_bad_data(tmp_path):
    bad = tmp_path / "venues.json"
    bad.write_text('{"venues": []}')
    with pytest.raises(ValueError, match="no venues"):
        load_venues(bad)
    bad.write_text('{"venues": [{"venue": "X", "venue_id": 1, "asset_class": "EQUITY"}]}')
    with pytest.raises(ValueError, match="latency"):
        load_venues(bad)
    with pytest.raises(ValueError, match="missing config file"):
        load_venues(tmp_path / "nope.json")
    ins = tmp_path / "instruments.json"
    ins.write_text(
        '{"instruments": [{"instrument_id": 1, "asset_class": "EQUITY", "currency": "USD",'
        ' "tick_size": 0.01, "lot_size": 0, "adv": 1}]}'
    )
    with pytest.raises(ValueError, match="lot_size must be > 0"):
        load_instruments(ins)
    ins.write_text(
        '{"instruments": [{"instrument_id": 1, "asset_class": "CRYPTO", "currency": "USD",'
        ' "tick_size": 0.01, "lot_size": 1, "adv": 1}]}'
    )
    with pytest.raises(ValueError, match="unknown asset_class"):
        load_instruments(ins)
    ins.write_text(
        '{"instruments": [{"instrument_id": 1, "asset_class": "FX",'
        ' "tick_size": 0.01, "lot_size": 1000, "adv": 1}]}'
    )
    with pytest.raises(ValueError, match="missing currency"):
        load_instruments(ins)
    ex = tmp_path / "execution.json"
    ex.write_text('{"defaults": {"seed": 1}, "cost_model": {}}')
    with pytest.raises(ValueError, match="prefer_rebate|sor"):
        load_sor_options(ex)
    with pytest.raises(ValueError):
        InstrumentSpec(1, 0.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        VenueSpec(70_000)
    with pytest.raises(ValueError):
        ExecConfig(seed=-1)
