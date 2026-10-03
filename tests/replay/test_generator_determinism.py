"""Replay level: the same seed produces byte-identical market data.

Generates the golden EQ vector twice (fresh generator state each time) via
``iap.marketdata.generator.generate_golden_eq`` and asserts the two runs are
identical as events, as canonical JSONL bytes and as IAP1 bytes (SHA-256), and
that the IAP1 digest is the one pinned in
``tests/golden/expected_codec_sha256.json`` — so a determinism regression AND
a silent generator change both fail here.

The bundled DATASET is pinned the same way: the raw files of the default
configuration (v1.4.0, ``flow.calibration = "session"``) and of the documented
legacy configuration (``"legacy_budget"`` — the v1.3.0 dataset, ``data_version``
``203c8f54...``) are generated and compared with their recorded SHA-256, so
neither dataset can change without this file changing.
"""

from __future__ import annotations

import hashlib
import json

from iap.core.codec import encode_iap1, encode_jsonl
from iap.marketdata.generator import (
    GOLDEN_EQ_SEED,
    MarketDataGenerator,
    generate_golden_eq,
    load_generator_config,
)
from iap.reference.refdata import ReferenceData


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_golden_eq_vector_is_byte_identical_across_runs(configs_dir, golden_dir):
    refdata = ReferenceData.load(configs_dir)
    first = generate_golden_eq(refdata, seed=GOLDEN_EQ_SEED, n=2000)
    second = generate_golden_eq(ReferenceData.load(configs_dir), seed=GOLDEN_EQ_SEED, n=2000)

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


#: SHA-256 of the raw files ``python -m iap.marketdata`` writes for the
#: committed ``configs/marketdata/generator.json``. The FX files are the same
#: bytes in both datasets: the v1.4.0 change touched the equity flow only.
FX_RAW_SHA256 = {
    "fx_20260824.jsonl": "3b6221b53dc73ae7e536e23681ce3becbbf45de180f70f85a848304a21c7f100",
    "fx_20260825.jsonl": "412d3ac402c4adb7d26967bfa884173ea9c03dc1adb9c795beb09de4db11fc47",
}
#: v1.4.0 (default): equity flow reaches the close.
DATASET_RAW_SHA256 = {
    "eq_20260824.jsonl": "791078006ca783f512007b6c159706ab202da8ea9228401f0d992f181fb1fbf8",
    "eq_20260825.jsonl": "45eea0af6f9fe75c009664087dabe82205b503b430f120ef28fe4914aaddc8df",
    **FX_RAW_SHA256,
}
#: v1.3.0 and earlier (``equities.flow.calibration = "legacy_budget"``).
LEGACY_RAW_SHA256 = {
    "eq_20260824.jsonl": "c46257de9ce140244e8b53ebecf4f59edb79c665588298f351878292892693e8",
    "eq_20260825.jsonl": "91c49000f53b03aceecbce940ab45bb25eaad99ac458fecfa51ad06822f2993e",
    **FX_RAW_SHA256,
}


def _raw_hashes(configs_dir, out_dir, calibration=None):
    cfg = load_generator_config(configs_dir / "marketdata" / "generator.json")
    if calibration is not None:
        cfg["equities"]["flow"]["calibration"] = calibration
    MarketDataGenerator(ReferenceData.load(configs_dir), cfg).generate_run(out_dir)
    return {p.name: _sha256(p.read_bytes()) for p in sorted(out_dir.glob("*.jsonl"))}


def test_default_dataset_raw_files_are_the_pinned_bytes(configs_dir, tmp_path):
    cfg = load_generator_config(configs_dir / "marketdata" / "generator.json")
    assert cfg["equities"]["flow"]["calibration"] == "session"
    assert _raw_hashes(configs_dir, tmp_path / "raw") == DATASET_RAW_SHA256


def test_legacy_calibration_reproduces_the_v1_3_0_dataset(configs_dir, tmp_path):
    """The documented way back: the same config with the calibration key at
    its legacy value regenerates the v1.3.0 raw files byte for byte."""
    assert _raw_hashes(configs_dir, tmp_path / "raw", "legacy_budget") == LEGACY_RAW_SHA256
