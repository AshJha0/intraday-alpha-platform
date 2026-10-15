"""v1.17 F4 / F5: the committed real slice and the MVP loop latency measurement.

The slice ``benchmarks/slices/itch50_20190130_AAPL`` is a benchmark workload
with recorded provenance (``tools/extract_real_slice.py``); these tests pin
that provenance to the bytes (a silently re-cut slice would move every CI
budget) and exercise ``iap.mvp.latency`` on the tiny synthetic dataset.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from iap.core.codec import iter_iap1, read_iap1, sha256_events_iap1, sha256_file
from iap.core.events import EventType
from iap.marketdata.ingest import MANIFEST_NAME, MANIFEST_VERSION
from iap.mvp.config import REPO_ROOT
from iap.mvp.latency import PERCENTILES, measure_loop_latency
from iap.mvp.real import prepare_symbol
from iap.orderbook.book import ApplyStatus, OrderBook

SLICE = Path(REPO_ROOT) / "benchmarks" / "slices" / "itch50_20190130_AAPL"
DATE, SYMBOL = "2019-01-30", "AAPL"
MAX_BYTES = 5_000_000


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(SLICE / MANIFEST_NAME, encoding="utf-8") as fh:
        return json.load(fh)


def test_slice_provenance_matches_the_bytes(manifest: dict) -> None:
    assert manifest["x-version"] == MANIFEST_VERSION and manifest["kind"] == "real"
    files = manifest["normalized"]["files"]
    (name, sha), *rest = files.items()
    assert not rest and name.endswith(".normalized.iap1")
    path = SLICE / "normalized" / name
    assert path.stat().st_size <= MAX_BYTES
    assert sha256_file(path) == sha
    events = read_iap1(path)
    assert manifest["dataset_version"] == sha256_events_iap1(events)
    prov = manifest["sessions"][DATE]["slice"]
    assert prov["events"] == len(events) == prov["prefix_events"] + prov["window_events"]
    assert prov["bytes"] == path.stat().st_size
    assert prov["symbol"] == SYMBOL and prov["instrument_id"] == 1 and prov["venue_id"] == 101
    assert prov["source_dataset"] == "ds3_20190130"
    assert prov["source_inputs"][0]["file"] == "01302019.NASDAQ_ITCH50.gz"
    assert prov["window_original_sequence_range"][0] == prov["symbol_events_before_window"] + 1
    assert prov["window_first_ts"] >= prov["window_start_ts"]
    # The whole slice is one symbol, one venue, contiguous ids and sequences,
    # the prefix all ADDs, the window from T0 on, event time monotone.
    for i, ev in enumerate(events, start=1):
        assert ev.event_id == i and ev.sequence == i
        assert ev.instrument_id == 1 and ev.venue_id == 101
        if i <= prov["prefix_events"]:
            assert ev.event_type == EventType.ADD
        else:
            assert ev.exchange_ts >= prov["window_start_ts"]
    ts = [ev.exchange_ts for ev in events]
    assert ts == sorted(ts)


def test_slice_book_reaches_real_depth(manifest: dict) -> None:
    """The point of the slice: a deep real book, clean (no drops) all the way."""
    prov = manifest["sessions"][DATE]["slice"]
    name = next(iter(manifest["normalized"]["files"]))
    book = OrderBook(1, 101)
    dropped = 0
    for ev in iter_iap1(SLICE / "normalized" / name):
        dropped += book.apply(ev) != ApplyStatus.APPLIED
    assert book.gaps_detected == 0 and book.duplicates_dropped == 0
    assert dropped == 0
    assert book.order_count_total() == prov["resting_orders_at_end"] > 30_000
    levels = book.depth(0, 100_000), book.depth(1, 100_000)
    assert (len(levels[0]), len(levels[1])) == (
        prov["levels_at_end"]["bid"],
        prov["levels_at_end"]["ask"],
    )
    assert min(len(levels[0]), len(levels[1])) > 700


def test_slice_drives_the_real_runner(tmp_path: Path, manifest: dict) -> None:
    prep = prepare_symbol(SLICE, DATE, SYMBOL, tmp_path / "stage")
    assert prep.instrument_id == 1
    assert prep.feed.n_events_generated == manifest["sessions"][DATE]["slice"]["events"]
    assert prep.feed.data_version == manifest["dataset_version"]


def test_measure_loop_latency_on_the_slice_head(tmp_path: Path) -> None:
    prep = prepare_symbol(SLICE, DATE, SYMBOL, tmp_path / "stage")
    lat = measure_loop_latency(prep, tmp_path / "run", limit=1500)
    assert lat.events == 1500
    q = lat.quantiles_ns
    assert list(q) == [f"p{p * 100:g}" for p in PERCENTILES] + ["max"]
    assert 0 < q["p50"] <= q["p90"] <= q["p99"] <= q["p99.9"] <= q["max"]
    assert lat.mean_ns > 0 and lat.total_s > 0
    assert lat.to_dict()["events"] == 1500
