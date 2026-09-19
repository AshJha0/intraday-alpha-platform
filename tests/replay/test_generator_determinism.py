"""Replay level: the same seed produces byte-identical market data.

Generates the golden EQ vector twice (fresh generator state each time) via
``iap.marketdata.generator.generate_golden_eq`` and asserts the two runs are
identical as events, as canonical JSONL bytes and as IAP1 bytes (SHA-256), and
that the IAP1 digest is the one pinned in
``tests/golden/expected_codec_sha256.json`` — so a determinism regression AND
a silent generator change both fail here.
"""
from __future__ import annotations

import hashlib
import json

from iap.core.codec import encode_iap1, encode_jsonl
from iap.marketdata.generator import GOLDEN_EQ_SEED, generate_golden_eq
from iap.reference.refdata import ReferenceData


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_golden_eq_vector_is_byte_identical_across_runs(configs_dir, golden_dir):
    refdata = ReferenceData.load(configs_dir)
    first = generate_golden_eq(refdata, seed=GOLDEN_EQ_SEED, n=2000)
    second = generate_golden_eq(ReferenceData.load(configs_dir),
                                seed=GOLDEN_EQ_SEED, n=2000)

    assert len(first) == 2000 and first == second

    jsonl_a, jsonl_b = encode_jsonl(first), encode_jsonl(second)
    assert jsonl_a == jsonl_b
    assert _sha256(jsonl_a) == _sha256(jsonl_b)

    iap1_a, iap1_b = encode_iap1(first), encode_iap1(second)
    assert iap1_a == iap1_b
    assert _sha256(iap1_a) == _sha256(iap1_b)

    # ... and the bytes are the ones every language must reproduce.
    with open(golden_dir / "expected_codec_sha256.json") as f:
        expected = json.load(f)
    assert _sha256(iap1_a) == expected["events_eq_mbo.iap1"]
    # the committed golden vector is that same JSONL, byte for byte
    assert jsonl_a == (golden_dir / "events_eq_mbo.jsonl").read_bytes()


def test_a_different_seed_changes_the_bytes(configs_dir):
    """Guard against a generator that ignores its seed (which would make the
    test above pass vacuously)."""
    refdata = ReferenceData.load(configs_dir)
    base = encode_iap1(generate_golden_eq(refdata, seed=GOLDEN_EQ_SEED, n=200))
    other = encode_iap1(generate_golden_eq(refdata, seed=GOLDEN_EQ_SEED + 1, n=200))
    assert base != other
