"""Raw->normalized pipeline and QC-report tests."""

import json

import pytest

from conftest import add, mkev
from iap.core.codec import read_iap1, read_jsonl, write_jsonl
from iap.core.events import EventType, Side
from iap.marketdata.generator import MarketDataGenerator
from iap.marketdata.normalize import normalize_run

CFG = {
    "seed": 4321,
    "sessions": 1,
    "equities": {"slots_per_stream": 120},
    "fx": {"slots_per_pair": 100},
    "anomalies": {
        "gap_prob": 0.004, "gap_max_events": 3, "dup_prob": 0.01,
        "ooo_prob": 0.006, "invalid_prob": 0.004, "ts_violation_prob": 0.004,
    },
}


@pytest.fixture(scope="module")
def pipeline(refdata, tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    gen = MarketDataGenerator(refdata, CFG)
    gen_stats = gen.generate_run(root / "raw")
    report = normalize_run(root / "raw", root / "normalized")
    return root, gen_stats, report


def test_qc_exact_counts_on_handcrafted_stream(tmp_path):
    """Every QC class detected with exact counts on a hand-built raw file."""
    evs = [
        add(1, Side.BID, 100, 10, 1),
        add(2, Side.ASK, 102, 10, 2),
        add(2, Side.ASK, 102, 10, 2),          # duplicate (seq 2 again)
        add(5, Side.BID, 99, 10, 3),           # gap (3,4 missing)
        add(4, Side.BID, 98, 10, 4),           # out-of-order late arrival
        mkev(5, EventType.ADD, 9, 101, 10, 5),  # invalid: side 9 (reuses seq 5)
        add(6, Side.ASK, 103, 10, 6),
    ]
    evs[6].receive_ts = evs[6].exchange_ts - 5_000  # ts violation on seq 6
    for i, e in enumerate(evs):
        e.event_id = i + 1
    raw = tmp_path / "raw"
    raw.mkdir()
    write_jsonl(raw / "stream.jsonl", evs)
    report = normalize_run(raw, tmp_path / "norm")
    c = report["per_stream"]["1:1"]
    assert c == {
        "events_in": 7,
        "events_out": 5,
        "gaps": 1,
        "gap_missing_events": 2,
        "duplicates": 1,
        "out_of_order": 1,
        "sequence_resets": 0,
        "ts_regression_dropped": 0,
        "invalid": 1,
        "ts_clamped": 1,
    }
    out = read_jsonl(tmp_path / "norm" / "stream.normalized.jsonl")
    assert [e.sequence for e in out] == [1, 2, 4, 5, 6]  # exchange-time order
    assert all(e.receive_ts >= e.exchange_ts for e in out)


def test_qc_report_matches_injected_truth(pipeline):
    _, gen_stats, report = pipeline
    inj = gen_stats["injected_anomalies"]
    tot = report["totals"]
    assert tot["duplicates"] == inj["duplicates"]
    assert tot["invalid"] == inj["invalid"]
    assert tot["ts_clamped"] == inj["ts_violations"]
    # A 2s spike may (rarely) fail to pass another event of the same sparse
    # stream; every detected late arrival adds exactly one apparent gap.
    assert 0 < tot["out_of_order"] <= inj["out_of_order"]
    # Every detected late arrival shows up as one apparent sequence jump too
    # (two adjacent spiked events can share a single jump, hence the bounds).
    assert inj["gaps"] <= tot["gaps"] <= inj["gaps"] + tot["out_of_order"]
    assert (
        inj["gap_missing_events"]
        <= tot["gap_missing_events"]
        <= inj["gap_missing_events"] + tot["out_of_order"]
    )
    assert tot["events_out"] == tot["events_in"] - tot["duplicates"] - tot["invalid"]


def test_normalized_outputs_clean_and_ordered(pipeline):
    from iap.core.events import validation_error

    root, _, report = pipeline
    seen_streams = set()
    for fname, meta in report["files"].items():
        out = read_jsonl(root / "normalized" / meta["normalized_jsonl"])
        assert len(out) == meta["events_out"]
        assert [e.event_id for e in out] == list(range(1, len(out) + 1))
        assert all(validation_error(e) is None for e in out)
        assert all(
            a.exchange_ts <= b.exchange_ts for a, b in zip(out, out[1:])
        )
        # no duplicates survive
        per_stream = {}
        for e in out:
            key = (e.venue_id, e.instrument_id)
            assert e.sequence not in per_stream.setdefault(key, set())
            per_stream[key].add(e.sequence)
            seen_streams.add(key)
    assert len(seen_streams) == 22 + 24  # 11 eq instruments x2 venues + 8 fx x3


def test_normalized_iap1_matches_jsonl(pipeline):
    root, _, report = pipeline
    for meta in report["files"].values():
        jl = read_jsonl(root / "normalized" / meta["normalized_jsonl"])
        bi = read_iap1(root / "normalized" / meta["normalized_iap1"])
        assert jl == bi


def test_parquet_dataset_row_count_and_columns(pipeline):
    import pyarrow.parquet as pq

    root, _, report = pipeline
    table = pq.read_table(root / "normalized" / "events.parquet")
    assert table.num_rows == report["totals"]["events_out"]
    assert table.num_rows == report["parquet"]["rows"]
    assert table.column_names == [
        "event_id", "instrument_id", "venue_id", "exchange_ts", "receive_ts",
        "sequence", "event_type", "side", "price_ticks", "qty", "order_id",
        "trade_id", "source",
    ]


def test_qc_report_written_with_per_stream_counts(pipeline):
    root, _, report = pipeline
    with open(root / "normalized" / "qc_report.json") as f:
        on_disk = json.load(f)
    assert on_disk["per_stream"] == report["per_stream"]
    assert on_disk["totals"] == report["totals"]
    for counters in on_disk["per_stream"].values():
        assert set(counters) == {
            "events_in", "events_out", "gaps", "gap_missing_events",
            "duplicates", "out_of_order", "sequence_resets",
            "ts_regression_dropped", "invalid", "ts_clamped",
        }


def test_normalize_empty_dir_raises(tmp_path):
    (tmp_path / "raw").mkdir()
    with pytest.raises(ValueError, match="no raw"):
        normalize_run(tmp_path / "raw", tmp_path / "norm")
