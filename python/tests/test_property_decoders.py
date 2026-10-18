"""Property tests (v1.18 REL-12): the real-data decoders on mutated and
truncated bytes.

Every decoder failure must be a typed reader error (``FeedFormatError`` /
``FeedTruncatedError`` / ``IngestError``, docs/REAL_DATA.md) — never a bare
``struct.error`` / ``IndexError`` / ``UnicodeDecodeError`` — and no input may
hang the reader.  The seeds are synthetic (tests/itch50_encoder.py,
tests/iex_encoder.py, the official DBN codec); no vendor byte is read.

Hypothesis runs are small and derandomized (CI time barely moves, the
examples are the same on every run); ``IAP_HYPOTHESIS_EXAMPLES`` raises the
count for a local soak.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from iap.marketdata import iex
from iap.marketdata.feederrors import FeedFormatError, IngestError
from iap.marketdata.itch50 import Itch50Mapper, Itch50Reader, session_midnight_ns
from itch50_encoder import build_session

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

EXAMPLES = int(os.environ.get("IAP_HYPOTHESIS_EXAMPLES", "40"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=timedelta(seconds=5),  # a hang shows up as a deadline failure
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: Every error a reader may raise on bad input (all ValueError subclasses).
TYPED = (FeedFormatError, IngestError)
DATE = "2025-01-15"
_MID = session_midnight_ns(DATE)

_ITCH_SEED: bytes | None = None


def _itch_seed() -> bytes:
    """A valid synthetic ITCH day, cut to a few hundred messages."""
    global _ITCH_SEED
    if _ITCH_SEED is None:
        enc, _ = build_session(seed=3, n_actions=150)
        _ITCH_SEED = enc.to_bytes()
    return _ITCH_SEED


@st.composite
def mutated(draw, seed: bytes, max_flips: int = 12) -> bytes:
    """``seed`` with random byte overwrites, an optional splice and a cut."""
    buf = bytearray(seed)
    for _ in range(draw(st.integers(0, max_flips))):
        if not buf:
            break
        buf[draw(st.integers(0, len(buf) - 1))] = draw(st.integers(0, 255))
    if draw(st.booleans()) and buf:
        at = draw(st.integers(0, len(buf)))
        buf[at:at] = draw(st.binary(max_size=64))
    cut = draw(st.integers(0, len(buf)))
    return bytes(buf[:cut])


def _drain_itch(path) -> None:
    reader = Itch50Reader(path, noii=True)
    mapper = Itch50Mapper(DATE, ("AAPL", "MSFT"))
    for _ in mapper.events(reader):
        pass


@SETTINGS
@given(data=st.data())
def test_itch_reader_and_mapper_raise_only_typed_errors(tmp_path, data):
    blob = data.draw(mutated(_itch_seed()[:6000]))
    path = tmp_path / "day.itch"
    path.write_bytes(blob)
    try:
        _drain_itch(path)
    except TYPED:
        pass


@SETTINGS
@given(blob=st.binary(max_size=400))
def test_itch_reader_on_random_bytes(tmp_path, blob):
    path = tmp_path / "noise.itch"
    path.write_bytes(blob)
    try:
        _drain_itch(path)
    except TYPED:
        pass


@SETTINGS
@given(cut=st.integers(min_value=1, max_value=4000))
def test_itch_truncation_is_reported_with_an_offset(tmp_path, cut):
    seed = _itch_seed()[:4000]
    path = tmp_path / "cut.itch"
    path.write_bytes(seed[:cut])
    try:
        list(Itch50Reader(path))
    except FeedFormatError as exc:
        assert exc.offset is not None and 0 <= exc.offset <= cut
    # a clean cut on a message boundary is a valid (short) file


def test_itch_non_ascii_add_side_is_counted_not_raised(tmp_path):
    """Found by the fuzz above (v1.18 REL-12): a non-ASCII side byte on an
    ``A`` / ``F`` message raised a bare UnicodeDecodeError; it is now an
    invalid order, counted like any other unknown side."""
    from itch50_encoder import Itch50Encoder, hms_ns

    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.system_event(hms_ns(4), "S")
    enc.system_event(hms_ns(9, 30), "Q")
    enc.add(1, hms_ns(9, 31), 11, "B", 100, "AAPL", 1_500_000)
    blob = bytearray(enc.to_bytes())
    side_at = len(blob) - 36 + 19  # last A message: type(1) header(10) ref(8) side
    assert blob[side_at] == ord("B")
    blob[side_at] = 0xC3
    path = tmp_path / "side.itch"
    path.write_bytes(bytes(blob))
    mapper = Itch50Mapper(DATE, ("AAPL",))
    assert list(mapper.events(Itch50Reader(path))) is not None
    assert mapper.counters["invalid_orders"] == 1


# ------------------------------------------------------------------ IEX


@SETTINGS
@given(
    protocol=st.sampled_from([iex.PROTOCOL_TOPS, iex.PROTOCOL_DEEP, iex.PROTOCOL_DEEP_PLUS]),
    data=st.binary(max_size=80),
)
def test_iex_decode_message_never_raises(protocol, data):
    """A short or unknown block is an ``UnknownMessage``, never an exception."""
    msg = iex.decode_message(protocol, data)
    assert isinstance(msg, tuple)


@SETTINGS
@given(payload=st.binary(max_size=200))
def test_iex_tp_segment_never_raises(payload):
    seg = iex.tp_segment(payload)
    assert seg is None or isinstance(seg, iex.Segment)
    if seg is not None:
        list(iex.tp_segments([payload]))


def _iex_seed(pcapng: bool) -> bytes:
    from iex_encoder import IexPcapWriter, add_order, delete_order, system_event, trade

    w = IexPcapWriter(iex.PROTOCOL_DEEP_PLUS, pcapng=pcapng)
    t = _MID + 10 * 3600 * 10**9
    w.segment([system_event("O", t), add_order("AAPL", t + 1, "B", 1, 100, 1_500_000)], send_time=t)
    w.segment(
        [trade("AAPL", t + 2, 50, 1_500_000, 9), delete_order("AAPL", t + 3, 1)], send_time=t + 3
    )
    return w.bytes()


@SETTINGS
@given(pcapng=st.booleans(), data=st.data())
def test_iex_reader_raises_only_typed_errors(tmp_path, pcapng, data):
    blob = data.draw(mutated(_iex_seed(pcapng)))
    path = tmp_path / "day.pcap"
    path.write_bytes(blob)
    mapper = Itch50Mapper(DATE, ("AAPL",))
    reader = iex.IexReader(path, DATE, ("AAPL",), mapper=mapper)
    try:
        for _ in mapper.events(reader):
            pass
    except TYPED:
        pass


# ------------------------------------------------------------------ DBN


def _dbn_seed() -> bytes:
    import datetime as dt
    import types

    dbn = pytest.importorskip("databento_dbn")
    mid = _MID
    day = dt.date.fromisoformat(DATE)
    md = dbn.Metadata(
        dataset="XNAS.ITCH",
        start=mid,
        end=mid + 86_400 * 10**9,
        stype_in=dbn.SType.RAW_SYMBOL,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.MBO,
        symbols=["AAPL"],
        mappings=[
            types.SimpleNamespace(
                raw_symbol="AAPL",
                intervals=[
                    types.SimpleNamespace(
                        start_date=day, end_date=day + dt.timedelta(days=1), symbol="7"
                    )
                ],
            )
        ],
    )
    recs = []
    for i, (action, side) in enumerate([("ADD", "BID"), ("ADD", "ASK"), ("CANCEL", "BID")]):
        recs.append(
            bytes(
                dbn.MBOMsg(
                    publisher_id=2,
                    instrument_id=7,
                    ts_event=mid + (10 + i) * 3600 * 10**9,
                    order_id=1 + (i % 2),
                    price=(1_500_000 + i) * 100_000,
                    size=100,
                    action=getattr(dbn.Action, action),
                    side=getattr(dbn.Side, side),
                    ts_recv=mid + (10 + i) * 3600 * 10**9 + 1,
                    flags=0,
                    sequence=i + 1,
                )
            )
        )
    return md.encode() + b"".join(recs)


def _dbn_typed() -> tuple:
    """Errors a DBN read may raise today: the typed reader errors plus the
    codec's own ``DBNError``, which ``DbnMboReader`` does not wrap yet (found
    by this test, v1.18 REL-12; see the strict xfail below)."""
    dbn = pytest.importorskip("databento_dbn")
    return (*TYPED, dbn.DBNError)


@SETTINGS
@given(data=st.data())
def test_dbn_reader_raises_only_typed_errors(tmp_path, data):
    from iap.marketdata.dbn_mbo import DbnMboReader

    blob = data.draw(mutated(_dbn_seed()))
    path = tmp_path / "day.dbn"
    path.write_bytes(blob)
    mapper = Itch50Mapper(DATE, ("AAPL",))
    reader = DbnMboReader(path, DATE, ("AAPL",), mapper=mapper)
    try:
        for _ in mapper.events(reader):
            pass
    except _dbn_typed():
        pass


@pytest.mark.xfail(
    strict=True,
    reason="DbnMboReader lets the codec's DBNError escape unwrapped (REL-12 finding; "
    "fix in iap.marketdata.dbn_mbo: wrap it in FeedFormatError with bytes_read as offset)",
)
def test_dbn_corrupt_header_is_a_typed_error(tmp_path):
    from iap.marketdata.dbn_mbo import DbnMboReader

    path = tmp_path / "bad.dbn"
    path.write_bytes(bytes.fromhex("00424e0360010000"))
    with pytest.raises(TYPED):
        list(DbnMboReader(path, DATE, ("AAPL",)))
