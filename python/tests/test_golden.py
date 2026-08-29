"""Golden-vector tests (conventions section 5): the cross-language contract.

The Python reference GENERATED these files (python/tools/make_golden.py);
this suite proves the current code still reproduces them exactly, and that
the expected book states hold under BOTH the reference OrderBook and an
independent brute-force rebuild.
"""

import json

import pytest

from bruteforce_book import BruteForceBook
from iap.core.codec import (
    encode_iap1,
    read_jsonl,
    sha256_bytes,
    sha256_events_iap1,
    write_jsonl,
)
from iap.core.events import validation_error
from iap.core.rng import SplitMix64
from iap.marketdata.generator import generate_golden_eq, generate_golden_fx
from iap.orderbook.book import OrderBook

PINNED_INDICES = [100, 500, 1000, 1500, 2000]


@pytest.fixture(scope="module")
def eq_vector(golden_dir):
    return read_jsonl(golden_dir / "events_eq_mbo.jsonl")


@pytest.fixture(scope="module")
def fx_vector(golden_dir):
    return read_jsonl(golden_dir / "events_fx_quote.jsonl")


def test_eq_vector_shape(eq_vector):
    assert len(eq_vector) == 2000
    assert all(validation_error(e) is None for e in eq_vector)


def test_fx_vector_shape(fx_vector):
    assert len(fx_vector) == 800
    assert all(validation_error(e) is None for e in fx_vector)


def test_generator_still_reproduces_golden_vectors(refdata, eq_vector, fx_vector):
    assert generate_golden_eq(refdata) == eq_vector
    assert generate_golden_fx(refdata) == fx_vector


def test_golden_jsonl_files_byte_stable(golden_dir, eq_vector, fx_vector, tmp_path):
    """Re-encoding the decoded vectors reproduces the files byte-for-byte."""
    for name, vec in (
        ("events_eq_mbo.jsonl", eq_vector),
        ("events_fx_quote.jsonl", fx_vector),
    ):
        out = tmp_path / name
        write_jsonl(out, vec)
        assert out.read_bytes() == (golden_dir / name).read_bytes()


def test_codec_sha256_matches_expected(golden_dir, eq_vector, fx_vector):
    with open(golden_dir / "expected_codec_sha256.json") as f:
        expected = json.load(f)
    assert sha256_events_iap1(eq_vector) == expected["events_eq_mbo.iap1"]
    assert sha256_events_iap1(fx_vector) == expected["events_fx_quote.iap1"]
    # and stability of the raw encoder output
    assert sha256_bytes(encode_iap1(eq_vector)) == expected["events_eq_mbo.iap1"]


def test_expected_book_states_reference_book(golden_dir, eq_vector):
    with open(golden_dir / "expected_book_states.json") as f:
        expected = json.load(f)
    assert expected["instrument_id"] == 1 and expected["venue_id"] == 1
    book = OrderBook(1, 1)
    states = {}
    for i, ev in enumerate(eq_vector, start=1):
        book.apply(ev)
        if i in PINNED_INDICES:
            states[str(i)] = book.state_summary()
    assert states == expected["states"]
    assert book.gaps_detected == 0 and book.duplicates_dropped == 0
    assert book.unknown_order_events == 0 and not book.stale


def test_expected_book_states_brute_force_independent(golden_dir, eq_vector):
    """Independent naive rebuild must agree exactly (validates the goldens)."""
    with open(golden_dir / "expected_book_states.json") as f:
        expected = json.load(f)
    brute = BruteForceBook()
    for i, ev in enumerate(eq_vector, start=1):
        brute.apply(ev)
        if str(i) in expected["states"]:
            assert brute.state_summary() == expected["states"][str(i)], f"event {i}"


def test_splitmix64_golden(golden_dir):
    with open(golden_dir / "splitmix64.json") as f:
        golden = json.load(f)
    rng = SplitMix64(golden["seed"])
    assert [rng.next_u64() for _ in range(5)] == golden["first_5_u64"]
