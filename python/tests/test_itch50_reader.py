"""ITCH 5.0 reader: framing, every supported message type, filtering, errors.

All input bytes are synthesised by ``itch50_encoder`` (no vendor data).
"""

from __future__ import annotations

import gzip
import struct

import pytest
from iap.marketdata import itch50
from iap.marketdata.feederrors import FeedFormatError, FeedTruncatedError
from iap.marketdata.itch50 import (
    MESSAGE_LENGTHS,
    AddOrder,
    BrokenTrade,
    CrossTrade,
    Itch50Reader,
    MarketParticipantPosition,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    RegShoRestriction,
    StockDirectory,
    SystemEvent,
    Trade,
    TradingAction,
)
from itch50_encoder import Itch50Encoder, build_session, hms_ns

T0 = hms_ns(9, 30)


def _one_of_each() -> Itch50Encoder:
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(7, hms_ns(3, 0, 1), "AAPL", round_lot_size=100, etp="N")
    enc.trading_action(7, hms_ns(3, 0, 2), "AAPL", "T")
    enc.reg_sho(7, hms_ns(3, 0, 3), "AAPL", "0")
    enc.market_participant(7, hms_ns(3, 0, 4), "AAPL", "MMKR")
    enc.add(7, T0, 1001, "B", 300, "AAPL", 1_500_000)
    enc.add(7, T0 + 1, 1002, "S", 200, "AAPL", 1_500_100, mpid="MMKR")
    enc.executed(7, T0 + 2, 1001, 100, 9001)
    enc.executed_with_price(7, T0 + 3, 1002, 50, 9002, 1_500_050, "Y")
    enc.cancel(7, T0 + 4, 1001, 100)
    enc.replace(7, T0 + 5, 1002, 1003, 150, 1_500_200)
    enc.delete(7, T0 + 6, 1001)
    enc.trade(7, T0 + 7, "B", 40, "AAPL", 1_500_025, 9003)
    enc.cross(7, T0 + 8, 12_345, "AAPL", 1_500_000, 9004, "O")
    enc.broken(7, T0 + 9, 9001)
    enc.noii(7, T0 + 10, "AAPL")
    return enc


def test_layout_sizes_match_the_specification():
    assert MESSAGE_LENGTHS == {
        "S": 12, "R": 39, "H": 25, "Y": 20, "L": 26, "A": 36, "F": 40, "E": 31,
        "C": 36, "X": 23, "D": 19, "U": 35, "P": 44, "Q": 40, "B": 19, "I": 50,
    }  # fmt: skip
    assert set(itch50.SUPPORTED_TYPES) == set("SRHYLAFECXDUPQBI")


def test_round_trip_of_every_supported_type(tmp_path):
    path = _one_of_each().write(tmp_path / "day.itch")
    reader = Itch50Reader(path)
    msgs = list(reader)
    assert msgs == [
        SystemEvent(hms_ns(3), "O"),
        StockDirectory(
            7,
            hms_ns(3, 0, 1),
            "AAPL",
            "Q",
            "N",
            100,
            "N",
            "C",
            "Z",
            "P",
            "N",
            "N",
            "1",
            "N",
            0,
            "N",
        ),
        TradingAction(7, hms_ns(3, 0, 2), "AAPL", "T", ""),
        RegShoRestriction(7, hms_ns(3, 0, 3), "AAPL", "0"),
        MarketParticipantPosition(7, hms_ns(3, 0, 4), "MMKR", "AAPL", "Y", "N", "A"),
        AddOrder(7, T0, 1001, "B", 300, "AAPL", 1_500_000, ""),
        AddOrder(7, T0 + 1, 1002, "S", 200, "AAPL", 1_500_100, "MMKR"),
        OrderExecuted(7, T0 + 2, 1001, 100, 9001),
        OrderExecutedWithPrice(7, T0 + 3, 1002, 50, 9002, "Y", 1_500_050),
        OrderCancel(7, T0 + 4, 1001, 100),
        OrderReplace(7, T0 + 5, 1002, 1003, 150, 1_500_200),
        OrderDelete(7, T0 + 6, 1001),
        Trade(7, T0 + 7, 0, "B", 40, "AAPL", 1_500_025, 9003),
        CrossTrade(7, T0 + 8, 12_345, "AAPL", 1_500_000, 9004, "O"),
        BrokenTrade(7, T0 + 9, 9001),
    ]
    assert reader.messages_read == 16  # the NOII is read and counted, not yielded
    assert reader.counts["I"] == 1 and reader.counts["A"] == 1 and reader.counts["F"] == 1
    assert sum(reader.counts.values()) == 16 and reader.skipped_unknown == {}
    assert reader.bytes_read == path.stat().st_size
    assert reader.directory["AAPL"].round_lot_size == 100


def test_prices_are_integers_and_timestamps_use_all_48_bits(tmp_path):
    enc = Itch50Encoder()
    big = (1 << 48) - 1
    enc.stock_directory(1, 0, "X")
    enc.add(1, big, 5, "B", 1, "X", 0xFFFFFFFF)
    msgs = list(Itch50Reader(enc.write(tmp_path / "f")))
    assert msgs[1].ts == big and msgs[1].price == 0xFFFFFFFF
    assert all(type(v) is int for v in (msgs[1].ts, msgs[1].price, msgs[1].shares))


def test_gzip_is_transparent_and_detected_by_magic(tmp_path):
    enc = _one_of_each()
    plain = enc.write(tmp_path / "a.bin")
    packed = enc.write(tmp_path / "b.dat", compress=True)  # no .gz suffix on purpose
    assert packed.read_bytes()[:2] == b"\x1f\x8b"
    a, b = Itch50Reader(plain), Itch50Reader(packed)
    assert list(a) == list(b)
    assert a.counts == b.counts and a.bytes_read == b.bytes_read


def test_unknown_types_are_skipped_by_length_and_counted(tmp_path):
    enc = Itch50Encoder()
    enc.stock_directory(1, 1, "AAPL")
    enc.raw("V", b"\x00" * 34)  # MWCB decline level: real type, not mapped
    enc.raw("z", b"\x01\x02\x03")  # not an ITCH type at all
    enc.raw("z", b"")
    enc.add(1, 2, 9, "B", 100, "AAPL", 10_000)
    reader = Itch50Reader(enc.write(tmp_path / "f"))
    msgs = list(reader)
    assert [type(m).__name__ for m in msgs] == ["StockDirectory", "AddOrder"]
    assert reader.skipped_unknown == {"V": 1, "z": 2}
    assert reader.messages_read == 5


def test_symbol_filter_keeps_only_the_universe(tmp_path):
    enc, _ = build_session(3, symbols=("AAPL", "MSFT"), n_actions=600)
    path = enc.write(tmp_path / "day.itch")
    everything = Itch50Reader(path)
    all_msgs = list(everything)
    only = Itch50Reader(path, symbols=("MSFT",))
    msgs = list(only)
    locate = only.directory["MSFT"].locate
    assert set(only.directory) == {"MSFT"}
    assert all(isinstance(m, SystemEvent) or m.locate == locate for m in msgs)
    expected = [m for m in all_msgs if isinstance(m, SystemEvent) or m.locate == locate]
    assert msgs == expected
    assert only.filtered > 0
    # every message is accounted for: yielded, filtered or a counted NOII
    assert len(msgs) + only.filtered + only.counts["I"] == only.messages_read == enc.messages
    assert only.counts == everything.counts  # counts are of the file, before filtering


def test_chunk_boundaries_do_not_change_the_result(tmp_path):
    enc, _ = build_session(5, n_actions=400)
    path = enc.write(tmp_path / "day.itch")
    assert list(Itch50Reader(path, chunk_size=64)) == list(Itch50Reader(path))


def test_limit_messages_stops_cleanly(tmp_path):
    enc, _ = build_session(5, n_actions=200)
    path = enc.write(tmp_path / "day.itch")
    reader = Itch50Reader(path, limit_messages=25)
    list(reader)
    assert reader.messages_read == 25 and reader.limit_reached
    full = Itch50Reader(path)
    list(full)
    assert not full.limit_reached and full.messages_read == enc.messages


@pytest.mark.parametrize("cut", [1, 2, 3, 13, 37])
def test_truncated_tail_raises_with_the_offset_of_the_cut_message(tmp_path, cut):
    enc = Itch50Encoder()
    enc.stock_directory(1, 1, "AAPL")
    enc.add(1, 2, 9, "B", 100, "AAPL", 10_000)
    data = enc.to_bytes()
    path = tmp_path / "cut.itch"
    path.write_bytes(data[: len(data) - 38 + cut])  # the add record is 2 + 36 bytes
    reader = Itch50Reader(path)
    with pytest.raises(FeedTruncatedError) as err:
        list(reader)
    assert err.value.offset == len(data) - 38
    assert "byte offset" in str(err.value)
    assert reader.messages_read == 1  # the complete message before the cut was delivered


def test_truncated_gzip_raises_a_typed_error(tmp_path):
    enc, _ = build_session(1, n_actions=300)
    packed = enc.write(tmp_path / "day.gz", compress=True).read_bytes()
    path = tmp_path / "cut.gz"
    path.write_bytes(packed[: len(packed) // 2])
    with pytest.raises(FeedTruncatedError, match="end-of-stream"):
        list(Itch50Reader(path))


def test_corrupt_gzip_raises_a_typed_error(tmp_path):
    enc, _ = build_session(1, n_actions=300)
    packed = bytearray(enc.write(tmp_path / "day.gz", compress=True).read_bytes())
    for i in range(40, 60):
        packed[i] ^= 0xFF
    path = tmp_path / "bad.gz"
    path.write_bytes(bytes(packed))
    with pytest.raises(FeedFormatError):
        list(Itch50Reader(path))


def test_zero_length_and_wrong_length_messages_are_rejected(tmp_path):
    good = Itch50Encoder()
    good.stock_directory(1, 1, "AAPL")
    head = good.to_bytes()

    zero = tmp_path / "zero.itch"
    zero.write_bytes(head + b"\x00\x00")
    with pytest.raises(FeedFormatError, match="zero-length") as err:
        list(Itch50Reader(zero))
    assert err.value.offset == len(head)

    wrong = tmp_path / "wrong.itch"
    body = b"A" + b"\x00" * 30  # an add order must be 36 bytes
    wrong.write_bytes(head + struct.pack(">H", len(body)) + body)
    with pytest.raises(FeedFormatError, match="'A' has length 31") as err:
        list(Itch50Reader(wrong))
    assert err.value.offset == len(head)
    assert not isinstance(err.value, FeedTruncatedError)


def test_empty_file_is_an_empty_stream(tmp_path):
    path = tmp_path / "empty.itch"
    path.write_bytes(b"")
    reader = Itch50Reader(path)
    assert list(reader) == [] and reader.messages_read == 0
    packed = tmp_path / "empty.gz"
    with gzip.open(packed, "wb"):
        pass
    assert list(Itch50Reader(packed)) == []


def test_reader_argument_validation(tmp_path):
    with pytest.raises(ValueError, match="1..8 ASCII"):
        Itch50Reader(tmp_path / "x", symbols=("TOOLONGSYM",))
    with pytest.raises(ValueError, match="limit_messages"):
        Itch50Reader(tmp_path / "x", limit_messages=-1)
