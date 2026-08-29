"""Validity / warmup semantics: NaN never leaks into valid=True (conventions §6)."""

from __future__ import annotations

import math

import pytest

from conftest import GOLDEN_DIR, REPO_ROOT, mkev
from iap.core.codec import read_jsonl
from iap.core.events import EventType
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine, FeatureVector
from iap.features.registry import build_registry, feature_index, registry_hash


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


@pytest.fixture(scope="module")
def eq_vecs(contexts):
    engine = FeatureEngine(contexts, cadence_ns=0)
    return [engine.apply(ev)
            for ev in read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")]


@pytest.fixture(scope="module")
def fx_vecs(contexts):
    engine = FeatureEngine(contexts, cadence_ns=0)
    return [engine.apply(ev)
            for ev in read_jsonl(GOLDEN_DIR / "events_fx_quote.jsonl")]


def _assert_no_valid_nan(vecs):
    n = len(build_registry())
    for vec in vecs:
        assert len(vec.values) == n and len(vec.validity) == n
        for x, ok in zip(vec.values, vec.validity):
            if ok:
                assert math.isfinite(x)
            else:
                assert math.isnan(x)


def test_property_no_valid_nan_eq(eq_vecs):
    """Property over every emission of the EQ golden vector."""
    _assert_no_valid_nan(eq_vecs)


def test_property_no_valid_nan_fx(fx_vecs):
    _assert_no_valid_nan(fx_vecs)


def test_vector_contract_fields(eq_vecs):
    vec = eq_vecs[-1]
    assert vec.instrument_id == 1
    assert vec.feature_version == registry_hash()
    assert vec.timestamp > 0


def test_warmup_semantics(eq_vecs):
    """Window features are invalid until their window has fully elapsed."""
    idx = feature_index()
    first_ts = eq_vecs[0].timestamp
    for vec in eq_vecs:
        elapsed = vec.timestamp - first_ts
        for name, w_ns in (("ofi_l1_w1s_v1", 1_000_000_000),
                           ("rvol_w1m_v1", 60_000_000_000),
                           ("signed_volume_w10s_v1", 10_000_000_000)):
            if elapsed < w_ns:
                assert not vec.validity[idx[name]], (
                    f"{name} valid before warmup at +{elapsed}ns")
    # and they do eventually become valid
    last = eq_vecs[-1]
    for name in ("ofi_l1_w1s_v1", "rvol_w1m_v1", "signed_volume_w10s_v1"):
        assert last.validity[idx[name]]


def test_stale_book_invalidates_book_features(contexts):
    """A sequence gap marks the venue stale; book features must go invalid."""
    engine = FeatureEngine(contexts, cadence_ns=0)
    idx = feature_index()
    seq = 0
    vec = None

    def ev(event_type, **kw):
        nonlocal seq, vec
        seq += 1
        vec = engine.apply(mkev(seq, event_type, **kw))

    ev(EventType.ADD, side=0, price=1000, qty=100, order_id=1)
    ev(EventType.ADD, side=1, price=1002, qty=150, order_id=2)
    assert vec.validity[idx["mid_price_v1"]]
    assert vec.validity[idx["spread_ticks_v1"]]
    # gap: jump the sequence by 10 -> stale book
    seq += 10
    vec = engine.apply(mkev(seq, EventType.ADD, side=0, price=1001, qty=50,
                            order_id=3))
    for name in ("mid_price_v1", "spread_bps_v1", "imbalance_l1_v1",
                 "depth_bid_l1_v1", "half_spread_cost_bps_v1"):
        assert not vec.validity[idx[name]], f"{name} valid on stale book"
    # clock features stay valid
    for name in ("minute_of_day_v1", "is_trading_v1",
                 "venue_staleness_max_ms_v1"):
        assert vec.validity[idx[name]], f"{name} invalid on stale book"
    # SNAPSHOT recovery burst restores validity
    seq += 1
    engine.apply(mkev(seq, EventType.SNAPSHOT, side=0, price=1000, qty=100,
                      order_id=11, trade_id=1))
    seq += 1
    vec = engine.apply(mkev(seq, EventType.SNAPSHOT, side=1, price=1002,
                            qty=150, order_id=12, trade_id=0))
    assert vec.validity[idx["mid_price_v1"]]


def test_halt_status_flags(contexts):
    engine = FeatureEngine(contexts, cadence_ns=0)
    idx = feature_index()
    vec = engine.apply(mkev(1, EventType.STATUS, qty=2))  # HALT
    assert vec.values[idx["is_halt_v1"]] == 1.0
    assert vec.values[idx["is_trading_v1"]] == 0.0
    vec = engine.apply(mkev(2, EventType.STATUS, qty=1))  # TRADING
    assert vec.values[idx["is_halt_v1"]] == 0.0
    assert vec.values[idx["is_trading_v1"]] == 1.0


def test_validity_bitset_roundtrip(eq_vecs):
    vec = eq_vecs[-1]
    bits = vec.validity_bits()
    assert len(bits) == (len(vec.validity) + 7) // 8
    assert FeatureVector.unpack_bits(bits, len(vec.validity)) == vec.validity


def test_cadence_throttles_emissions(contexts):
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    every = FeatureEngine(contexts, cadence_ns=0)
    throttled = FeatureEngine(contexts, cadence_ns=1_000_000_000)
    n_every = sum(1 for ev in events if every.apply(ev) is not None)
    n_thr = sum(1 for ev in events if throttled.apply(ev) is not None)
    assert n_every == len(events)
    assert 0 < n_thr < n_every
    # emitted timestamps at least cadence apart (per instrument = the only one)
    engine = FeatureEngine(contexts, cadence_ns=1_000_000_000)
    ts = [v.timestamp for v in map(engine.apply, events) if v is not None]
    assert all(b - a >= 1_000_000_000 for a, b in zip(ts, ts[1:]))


def test_unknown_instrument_raises(contexts):
    engine = FeatureEngine(contexts, cadence_ns=0)
    with pytest.raises(ValueError, match="no InstrumentContext"):
        engine.apply(mkev(1, EventType.HEARTBEAT, instrument_id=99999))
