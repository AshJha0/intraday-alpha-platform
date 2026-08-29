"""Reference-data service tests (configs/instruments.json, venues.json)."""

import pytest

from conftest import CONFIGS_DIR
from iap.reference.refdata import ReferenceData


def test_universe_composition(refdata):
    eqs = refdata.instruments("EQUITY")
    fxs = refdata.instruments("FX")
    assert len(eqs) == 11  # 10 equities + 1 ETF
    assert [i.symbol for i in eqs[:3]] == ["SYN.EQ.001", "SYN.EQ.002", "SYN.EQ.003"]
    assert eqs[-1].symbol == "SYN.ETF.IDX"
    assert len(fxs) == 8
    assert {i.symbol for i in fxs} == {
        "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD",
        "USD/CAD", "USD/CHF", "NZD/USD", "EUR/GBP",
    }


def test_lookup_by_symbol_and_id(refdata):
    a = refdata.instrument("SYN.EQ.001")
    b = refdata.instrument(1)
    assert a is b
    with pytest.raises(ValueError, match="unknown instrument"):
        refdata.instrument("NOPE")
    with pytest.raises(ValueError, match="unknown instrument"):
        refdata.instrument(9999)


def test_tick_and_lot_sizes(refdata):
    assert refdata.tick_size("SYN.EQ.001") == 0.01
    assert refdata.lot_size("SYN.EQ.001") == 100
    assert refdata.tick_size("EUR/USD") == 0.00001
    assert refdata.tick_size("USD/JPY") == 0.001  # pip-based JPY convention
    for fx in refdata.instruments("FX"):
        assert fx.lot_size == 1000  # 1 qty unit = 1,000 base ccy


def test_price_tick_conversion_round_trip(refdata):
    inst = refdata.instrument("EUR/USD")
    ticks = inst.price_to_ticks(1.08652)
    assert ticks == 108652
    assert inst.ticks_to_price(ticks) == pytest.approx(1.08652, abs=1e-12)
    eq = refdata.instrument("SYN.EQ.005")
    assert eq.price_to_ticks(88.10) == 8810
    assert eq.ref_price_ticks == 8810


def test_venues(refdata):
    assert [v.venue for v in refdata.venues("EQUITY")] == ["XV1", "XV2"]
    assert [v.venue for v in refdata.venues("FX")] == ["LP1", "LP2", "PRI"]
    xv1 = refdata.venue("XV1")
    assert xv1.venue_id == 1
    assert refdata.venue(1) is xv1
    assert xv1.fees["taker_fee_per_share"] == 0.0030
    assert xv1.latency_mean_ns == 150_000
    with pytest.raises(ValueError, match="unknown venue"):
        refdata.venue("XV9")


def test_sessions_and_calendar(refdata):
    assert refdata.is_trading_day("2026-08-24")
    assert not refdata.is_trading_day("2026-08-22")  # Saturday
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", "2026-08-24")
    assert (close_ns - open_ns) == 6 * 3600 * 10**9 + 1800 * 10**9  # 6.5h
    fx_open, fx_close = refdata.session_bounds_ns("FX", "2026-08-24")
    assert fx_open < open_ns  # FX opens at 00:00 UTC
    with pytest.raises(ValueError, match="not a trading day"):
        refdata.session_bounds_ns("EQUITY", "2026-08-22")
    with pytest.raises(ValueError, match="asset class"):
        refdata.session_bounds_ns("CRYPTO", "2026-08-24")


def test_corporate_actions_stub(refdata):
    """Documented out-of-scope for the synthetic universe: always empty."""
    assert refdata.corporate_actions("SYN.EQ.001") == []
    assert refdata.corporate_actions(11, "2026-01-01", "2026-12-31") == []
    with pytest.raises(ValueError):
        refdata.corporate_actions("NOPE")


def test_load_from_configs_dir():
    ref = ReferenceData.load(CONFIGS_DIR)
    assert len(ref.instruments()) == 19
    assert len(ref.trading_days) == 5
