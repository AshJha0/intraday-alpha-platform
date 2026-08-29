"""Codec byte-exactness and round-trip tests (schemas/FORMAT.md)."""

import hashlib
import struct

import pytest

from conftest import mkev
from iap.core import codec
from iap.core.events import EventType, MarketEvent, Side


def _sample_events(n=20):
    evs = []
    for i in range(1, n + 1):
        evs.append(
            mkev(i, EventType.ADD, i % 2, 2400 + i, 100 * i, order_id=1000 + i)
        )
    return evs


EV = MarketEvent(
    event_id=1,
    instrument_id=2,
    venue_id=3,
    exchange_ts=4,
    receive_ts=5,
    sequence=6,
    event_type=7,
    side=1,
    price_ticks=-8,
    qty=9,
    order_id=10,
    trade_id=11,
)


def test_jsonl_line_exact_bytes():
    assert codec.encode_jsonl_line(EV) == (
        '{"event_id":1,"instrument_id":2,"venue_id":3,"exchange_ts":4,'
        '"receive_ts":5,"sequence":6,"event_type":7,"side":1,'
        '"price_ticks":-8,"qty":9,"order_id":10,"trade_id":11}'
    )


def test_jsonl_round_trip():
    for ev in _sample_events():
        assert codec.decode_jsonl_line(codec.encode_jsonl_line(ev)) == ev


def test_jsonl_rejects_missing_key():
    line = codec.encode_jsonl_line(EV).replace('"trade_id":11', '"trade_id":11}')[:-2]
    line = '{"event_id":1}'
    with pytest.raises(ValueError, match="missing"):
        codec.decode_jsonl_line(line)


def test_jsonl_rejects_extra_key():
    line = codec.encode_jsonl_line(EV)[:-1] + ',"extra":1}'
    with pytest.raises(ValueError, match="extra"):
        codec.decode_jsonl_line(line)


def test_jsonl_rejects_wrong_key_order():
    d = EV.to_dict()
    keys = list(d)
    keys[0], keys[1] = keys[1], keys[0]
    import json

    line = json.dumps({k: d[k] for k in keys}, separators=(",", ":"))
    with pytest.raises(ValueError):
        codec.decode_jsonl_line(line)


def test_jsonl_rejects_non_integer_values():
    line = codec.encode_jsonl_line(EV).replace('"qty":9', '"qty":9.0')
    with pytest.raises(ValueError, match="integer"):
        codec.decode_jsonl_line(line)
    line = codec.encode_jsonl_line(EV).replace('"qty":9', '"qty":true')
    with pytest.raises(ValueError, match="integer"):
        codec.decode_jsonl_line(line)


def test_jsonl_rejects_malformed_line():
    with pytest.raises(ValueError):
        codec.decode_jsonl_line("not json at all {")


def test_iap1_sizes_pinned():
    assert codec.IAP1_HEADER_SIZE == 16
    assert codec.IAP1_RECORD_SIZE == 72
    data = codec.encode_iap1(_sample_events(5))
    assert len(data) == 16 + 5 * 72


def test_iap1_header_bytes_exact():
    data = codec.encode_iap1([])
    assert data == struct.pack("<IIQ", 0x49415031, 1, 0)
    assert data[:4] == b"1PAI"  # little-endian magic spells IAP1 reversed


def test_iap1_record_bytes_exact():
    data = codec.encode_iap1([EV])
    expected = struct.pack("<IIQ", 0x49415031, 1, 1) + struct.pack(
        "<QIHBBqqQqqQQ", 1, 2, 3, 7, 1, 4, 5, 6, -8, 9, 10, 11
    )
    assert data == expected


def test_iap1_round_trip():
    evs = _sample_events(50) + [EV]
    assert codec.decode_iap1(codec.encode_iap1(evs)) == evs


def test_iap1_rejects_bad_magic():
    data = bytearray(codec.encode_iap1([EV]))
    data[0] ^= 0xFF
    with pytest.raises(ValueError, match="magic"):
        codec.decode_iap1(bytes(data))


def test_iap1_rejects_bad_version():
    data = struct.pack("<IIQ", 0x49415031, 2, 0)
    with pytest.raises(ValueError, match="version"):
        codec.decode_iap1(data)


def test_iap1_rejects_truncation_and_count_mismatch():
    data = codec.encode_iap1(_sample_events(3))
    with pytest.raises(ValueError):
        codec.decode_iap1(data[:-1])
    with pytest.raises(ValueError):
        codec.decode_iap1(data + b"\x00" * 72)
    with pytest.raises(ValueError):
        codec.decode_iap1(data[:10])


def test_file_round_trips(tmp_path):
    evs = _sample_events(30)
    jl = tmp_path / "x.jsonl"
    bi = tmp_path / "x.iap1"
    assert codec.write_jsonl(jl, evs) == 30
    assert codec.write_iap1(bi, evs) == 30
    assert codec.read_jsonl(jl) == evs
    assert codec.read_iap1(bi) == evs


def test_jsonl_and_iap1_carry_identical_events():
    evs = _sample_events(10)
    via_json = [codec.decode_jsonl_line(codec.encode_jsonl_line(e)) for e in evs]
    via_bin = codec.decode_iap1(codec.encode_iap1(evs))
    assert via_json == via_bin == evs


def test_sha256_helpers(tmp_path):
    evs = _sample_events(5)
    data = codec.encode_iap1(evs)
    assert codec.sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    assert codec.sha256_events_iap1(evs) == hashlib.sha256(data).hexdigest()
    p = tmp_path / "f.iap1"
    p.write_bytes(data)
    assert codec.sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_encode_jsonl_bytes_lf_terminated():
    data = codec.encode_jsonl(_sample_events(3))
    assert data.endswith(b"\n") and data.count(b"\n") == 3
