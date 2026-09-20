"""Integration level: decode -> order book -> feature engine over a golden vector.

Replays the first 500 events of ``tests/golden/events_eq_mbo.jsonl`` through
``iap.orderbook.book.OrderBook`` (the venue book, all events applied) and
``iap.features.engine.FeatureEngine`` (built from the nested configs tree) and
asserts that the chain produces a ``FeatureVector`` whose ``feature_version``
is the feature-registry hash — the contract every downstream consumer keys on
(schemas/features/feature_vector.schema.json, API_FEATURES.md §1).
"""
from __future__ import annotations

from iap.core.codec import read_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine, FeatureVector
from iap.features.registry import build_registry, registry_hash
from iap.orderbook.book import ApplyStatus, OrderBook

N_EVENTS = 500


def test_book_and_feature_engine_over_golden_eq_head(golden_dir, configs_dir):
    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")[:N_EVENTS]
    assert len(events) == N_EVENTS
    instrument_id = events[0].instrument_id
    venue_id = events[0].venue_id

    # 1. the venue book accepts the clean golden head without dropping anything
    book = OrderBook(instrument_id, venue_id)
    for ev in events:
        assert book.apply(ev) == ApplyStatus.APPLIED
    assert book.best_bid() is not None and book.best_ask() is not None
    bid, ask = book.best_bid()[0], book.best_ask()[0]
    assert bid < ask, "book must be uncrossed after a clean replay"

    # 2. the feature engine, fed the same events, emits vectors keyed on the
    #    registry hash
    contexts = build_contexts(configs_dir)
    assert instrument_id in contexts
    engine = FeatureEngine(contexts, cadence_ns=0)
    emitted = []
    for ev in events:
        vec = engine.apply(ev)
        if vec is not None:
            emitted.append(vec)

    assert engine.events_processed == N_EVENTS
    assert engine.events_dropped == 0
    assert emitted, "no FeatureVector emitted over 500 clean golden events"
    expected_version = registry_hash()
    n_features = len(build_registry())
    for vec in emitted:
        assert isinstance(vec, FeatureVector)
        assert vec.instrument_id == instrument_id
        assert vec.feature_version == expected_version
        assert len(vec.values) == n_features == len(vec.validity)
    # event-time monotone, like the input
    stamps = [v.timestamp for v in emitted]
    assert stamps == sorted(stamps)
