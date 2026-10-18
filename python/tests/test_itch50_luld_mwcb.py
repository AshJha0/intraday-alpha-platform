"""ITCH 5.0 ``J`` (LULD auction collar) and ``V`` (MWCB decline level),
v1.18: decoded and counted only with the opt-in
``Itch50Reader(circuit_breakers=True)``; by default they stay unknown types
and every output byte is unchanged (the pinned ingest hashes in
test_real_data_ingest.py are the golden proof; the equivalence test below
shows it on a day that carries both messages)."""

from __future__ import annotations

import hashlib
import struct

import pytest
from conftest import CONFIGS_DIR
from iap.marketdata.feederrors import FeedFormatError
from iap.marketdata.ingest import ingest, load_manifest
from iap.marketdata.itch50 import (
    MESSAGE_LENGTHS,
    OPT_IN_LENGTHS,
    Itch50Mapper,
    Itch50Reader,
    LuldAuctionCollar,
    MwcbDeclineLevel,
    pad_symbol,
)
from itch50_encoder import Itch50Encoder, build_session, hms_ns

DATE = "2019-12-30"


def _hdr(locate: int, ts: int) -> bytes:
    return struct.pack(">HHHI", locate, 0, ts >> 32, ts & 0xFFFFFFFF)


def luld(locate: int, ts: int, symbol: str, ref: int, up: int, lo: int, ext: int) -> bytes:
    return _hdr(locate, ts) + pad_symbol(symbol) + struct.pack(">IIII", ref, up, lo, ext)


def mwcb(ts: int, l1: int, l2: int, l3: int) -> bytes:
    return _hdr(0, ts) + struct.pack(">QQQ", l1, l2, l3)


def _fixture(tmp_path):
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.stock_directory(2, hms_ns(3, 0, 2), "MSFT")
    enc.raw("V", mwcb(hms_ns(3, 5), 3_000_00000000, 2_800_00000000, 2_600_00000000))
    enc.system_event(hms_ns(4), "S")
    enc.raw("J", luld(1, hms_ns(10), "AAPL", 1_500_000, 1_575_000, 1_425_000, 0))
    enc.raw("J", luld(2, hms_ns(10, 1), "MSFT", 3_000_000, 3_150_000, 2_850_000, 1))
    return enc.write(tmp_path / "jv.itch")


def test_layouts_are_the_spec_lengths():
    assert OPT_IN_LENGTHS == {"J": 35, "V": 35}
    assert not set(OPT_IN_LENGTHS) & set(MESSAGE_LENGTHS)  # never in the default set
    assert len(luld(1, 0, "A", 0, 0, 0, 0)) + 1 == 35 and len(mwcb(0, 0, 0, 0)) + 1 == 35


def test_default_reader_skips_and_counts_them_as_unknown(tmp_path):
    reader = Itch50Reader(_fixture(tmp_path))
    msgs = list(reader)
    assert not any(isinstance(m, (LuldAuctionCollar, MwcbDeclineLevel)) for m in msgs)
    assert reader.skipped_unknown == {"J": 2, "V": 1}
    assert "J" not in reader.counts and "V" not in reader.counts


def test_opt_in_decodes_counts_and_filters(tmp_path):
    path = _fixture(tmp_path)
    reader = Itch50Reader(path, circuit_breakers=True)
    msgs = list(reader)
    collars = [m for m in msgs if isinstance(m, LuldAuctionCollar)]
    levels = [m for m in msgs if isinstance(m, MwcbDeclineLevel)]
    assert collars == [
        LuldAuctionCollar(1, hms_ns(10), "AAPL", 1_500_000, 1_575_000, 1_425_000, 0),
        LuldAuctionCollar(2, hms_ns(10, 1), "MSFT", 3_000_000, 3_150_000, 2_850_000, 1),
    ]
    assert levels == [
        MwcbDeclineLevel(0, hms_ns(3, 5), 3_000_00000000, 2_800_00000000, 2_600_00000000)
    ]
    assert reader.counts["J"] == 2 and reader.counts["V"] == 1
    assert reader.skipped_unknown == {}
    # a symbol filter drops the other stock's collar but never the market-wide V
    only = Itch50Reader(path, symbols=["AAPL"], circuit_breakers=True)
    got = [m for m in only if isinstance(m, (LuldAuctionCollar, MwcbDeclineLevel))]
    assert [type(m).__name__ for m in got] == ["MwcbDeclineLevel", "LuldAuctionCollar"]
    assert got[1].stock == "AAPL"


def test_mapper_ignores_them(tmp_path):
    path = _fixture(tmp_path)
    plain = list(Itch50Mapper(DATE, ("AAPL", "MSFT")).events(Itch50Reader(path)))
    m = Itch50Mapper(DATE, ("AAPL", "MSFT"))
    opted = list(m.events(Itch50Reader(path, circuit_breakers=True)))
    assert opted == plain


def test_opt_in_length_check(tmp_path):
    enc = Itch50Encoder()
    enc.raw("J", b"\x00" * 30)  # 31 bytes, spec says 35
    path = enc.write(tmp_path / "bad.itch")
    assert len(list(Itch50Reader(path))) == 0  # default: unknown, skipped
    with pytest.raises(FeedFormatError, match="'J' has length 31"):
        list(Itch50Reader(path, circuit_breakers=True))


def _hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in (".iap1", ".jsonl")
    }


def test_default_ingest_output_is_unchanged_by_j_and_v(tmp_path):
    enc, _ = build_session(5, symbols=("AAPL", "MSFT"), n_actions=300)
    plain = enc.write(tmp_path / "plain.itch")
    # the same day with J / V spliced in after the directory messages
    parts = list(enc._parts)
    extra = Itch50Encoder()
    extra.raw("V", mwcb(hms_ns(3, 30), 1, 2, 3))
    extra.raw("J", luld(1, hms_ns(3, 31), "AAPL", 1_500_000, 1_575_000, 1_425_000, 0))
    enc._parts = parts[:6] + extra._parts + parts[6:]
    with_jv = enc.write(tmp_path / "jv.itch")
    for src, out in ((plain, "a"), (with_jv, "b")):
        ingest("itch50", [src], DATE, ["AAPL", "MSFT"], tmp_path / out, configs_dir=CONFIGS_DIR)
    ha, hb = _hashes(tmp_path / "a"), _hashes(tmp_path / "b")
    assert len(ha) >= 2 and ha == hb  # raw + normalized event files, byte for byte
    assert (
        load_manifest(tmp_path / "a")["normalized"] == load_manifest(tmp_path / "b")["normalized"]
    )
