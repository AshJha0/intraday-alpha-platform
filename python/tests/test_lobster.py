"""LOBSTER reader: exact timestamps, event types 1-7, seeding / back-fill and
the level-by-level verification against the orderbook file (a matching one
and a deliberately wrong one).  Fixtures are synthesised (lobster_fixture).
"""

from __future__ import annotations

import pytest
from iap.core.events import EventType, SessionStatus, Side
from iap.core.rng import SplitMix64
from iap.marketdata.feederrors import FeedFormatError, FeedTruncatedError
from iap.marketdata.ingest import sequence_events
from iap.marketdata.itch50 import session_midnight_ns
from iap.marketdata.lobster import (
    SEED_ORDER_BASE,
    LobsterConverter,
    parse_seconds_to_ns,
    sibling_orderbook,
    symbol_from_filename,
)
from iap.orderbook.book import ApplyStatus, OrderBook
from lobster_fixture import build_lobster, write_lobster

DATE = "2012-06-21"
MID = session_midnight_ns(DATE)
BID, ASK = int(Side.BID), int(Side.ASK)
ADD, MODIFY, CANCEL, EXECUTE, TRADE, STATUS = (
    int(EventType.ADD),
    int(EventType.MODIFY),
    int(EventType.CANCEL),
    int(EventType.EXECUTE),
    int(EventType.TRADE),
    int(EventType.STATUS),
)


def test_decimal_seconds_parse_exactly_without_float_drift():
    assert parse_seconds_to_ns("34200.004241176") == 34_200_004_241_176
    assert parse_seconds_to_ns("57599.999999999") == 57_599_999_999_999
    assert parse_seconds_to_ns("34200") == 34_200 * 10**9
    assert parse_seconds_to_ns("34200.5") == 34_200_500_000_000
    rng = SplitMix64(99)
    drifted = 0
    for _ in range(5000):
        ns = 34_200 * 10**9 + rng.below(23_400 * 10**9)
        text = f"{ns // 10**9}.{ns % 10**9:09d}"
        assert parse_seconds_to_ns(text) == ns
        drifted += int(float(text) * 1e9) != ns
    assert drifted > 0  # the float route the reader avoids really does lose nanoseconds


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3", "34200.0000000001", "-1.5", "86400.0", "1e5"])
def test_bad_timestamps_are_format_errors(bad):
    with pytest.raises(FeedFormatError):
        parse_seconds_to_ns(bad, line=3)


def test_file_name_helpers(tmp_path):
    msg, book, _ = write_lobster(tmp_path, "AAPL", DATE, 1, n_messages=20)
    assert symbol_from_filename(msg) == "AAPL" and symbol_from_filename(book) == "AAPL"
    assert symbol_from_filename("whatever.csv") is None
    assert sibling_orderbook(msg) == book
    book.unlink()
    assert sibling_orderbook(msg) is None and sibling_orderbook("x.csv") is None


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 20260930])
def test_book_matches_the_orderbook_file_level_by_level(tmp_path, seed):
    msg, book_path, info = write_lobster(tmp_path, "AAPL", DATE, seed, n_messages=700, levels=5)
    conv = LobsterConverter(DATE, 0, msg, book_path)
    protos = list(conv.events())
    assert conv.verification == {"status": "match", "levels": 5, "rows_verified": 700}
    assert conv.messages_read == 700
    assert conv.message_counts == info["message_counts"]
    c = conv.counters
    assert c["seeded_levels"] == 10  # five visible levels per side at the open
    assert c["backfilled_levels"] > 0 and c["events_on_seed_orders"] > 0
    assert c["unresolved_executions"] == 0 and c["duplicate_order_ids"] == 0

    # an independent replay of the emitted stream through the real book ends on
    # the fixture's final book (every level the file ever displayed is exact)
    book = OrderBook(1, 101)
    for ev in sequence_events(protos, [100], 101):
        assert book.apply(ev) == ApplyStatus.APPLIED
        assert not book.is_crossed()
    assert [(p * 100, q) for p, q in book.depth(ASK, 5)] == info["final"]["ask"][:5]
    assert [(p * 100, q) for p, q in book.depth(BID, 5)] == info["final"]["bid"][:5]
    assert all(qty > 0 for _, _, _, qty in book.resting_orders())
    assert protos[0][2:] == (STATUS, 0, 0, int(SessionStatus.TRADING), 0, 0)
    assert all(p[1] >= MID + 34_200 * 10**9 for p in protos)
    assert any(p[6] > SEED_ORDER_BASE for p in protos if p[2] == ADD)


def test_a_wrong_orderbook_file_reports_the_first_divergence(tmp_path):
    msg, book_path, _ = write_lobster(tmp_path, "AAPL", DATE, 5, n_messages=300, levels=5)
    rows = book_path.read_text().splitlines()
    wrong_row = 137
    fields = rows[wrong_row].split(",")
    true_size = int(fields[3])
    fields[3] = str(true_size + 1)  # bid size at level 1
    rows[wrong_row] = ",".join(fields)
    wrong = tmp_path / "wrong_orderbook_5.csv"
    wrong.write_text("\n".join(rows) + "\n")
    conv = LobsterConverter(DATE, 0, msg, wrong)
    protos = list(conv.events())
    report = conv.verification
    assert report["status"] == "diverged" and report["rows_verified"] == wrong_row
    assert report["first_divergence"] == {
        "reason": "level mismatch",
        "message_index": wrong_row,
        "line": wrong_row + 1,
        "side": "bid",
        "level": 1,
        "expected": [int(fields[2]), true_size + 1],
        "actual": [int(fields[2]), true_size],
    }
    assert conv.messages_read == 300 and len(protos) > 300  # conversion still completes


def test_a_missing_level_in_the_reference_is_a_divergence_too(tmp_path):
    msg, book_path, _ = write_lobster(tmp_path, "AAPL", DATE, 6, n_messages=120, levels=5)
    rows = book_path.read_text().splitlines()
    fields = rows[40].split(",")
    fields[0:2] = ["9999999999", "0"]  # best ask claimed empty
    rows[40] = ",".join(fields)
    book_path.write_text("\n".join(rows) + "\n")
    conv = LobsterConverter(DATE, 0, msg, book_path)
    list(conv.events())
    div = conv.verification["first_divergence"]
    assert (div["message_index"], div["side"], div["level"]) == (40, "ask", 1)


def test_without_the_orderbook_file_unseen_orders_are_counted(tmp_path):
    msg, _, info = write_lobster(tmp_path, "AAPL", DATE, 2, n_messages=500)
    conv = LobsterConverter(DATE, 0, msg, None)
    protos = list(conv.events())
    assert conv.verification == {"status": "not_supplied"}
    c = conv.counters
    assert c["seeded_levels"] == 0 and c["backfilled_levels"] == 0
    assert c["unresolved_executions"] + c["unresolved_deletes"] + c["unresolved_cancels"] > 0
    # every visible execution still prints, resolved or not
    trades = sum(p[2] == TRADE for p in protos)
    assert trades == info["message_counts"]["4"] + info["message_counts"]["5"]
    book = OrderBook(1, 101)
    for ev in sequence_events(protos, [50], 101):
        assert book.apply(ev) == ApplyStatus.APPLIED


def _convert(tmp_path, lines, book_rows=None, **kw):
    msg = tmp_path / "X_2012-06-21_34200000_57600000_message_1.csv"
    msg.write_text("\n".join(lines) + "\n")
    book = None
    if book_rows is not None:
        book = tmp_path / "X_2012-06-21_34200000_57600000_orderbook_1.csv"
        book.write_text("\n".join(book_rows) + "\n")
    conv = LobsterConverter(DATE, 0, msg, book, **kw)
    return list(conv.events()), conv


def test_event_types_map_to_canonical_events(tmp_path):
    protos, conv = _convert(
        tmp_path,
        [
            "34200.000000001,1,11,300,1500000,1",
            "34200.000000002,1,12,200,1500100,-1",
            "34200.000000003,2,11,100,1500000,1",  # partial cancel: 200 left
            "34200.000000004,4,11,50,1500000,1",  # visible execution of the bid
            "34200.000000005,5,0,70,1500050,-1",  # hidden sell order executed: buyer-initiated
            "34200.000000006,6,0,900,1500000,-1",  # cross: not on the event path
            "34200.000000007,7,0,0,-1,-1",
            "34200.000000008,7,0,0,0,-1",
            "34200.000000009,7,0,0,1,-1",
            "34200.000000010,2,12,200,1500100,-1",  # cancels everything: order removed
            "34200.000000011,3,11,150,1500000,1",
            "34200.000000012,1,13,100,1500000,1",
            "34200.000000012,1,13,100,1500000,1",  # duplicate id
        ],
    )
    base = MID + 34_200 * 10**9
    assert [(p[1] - base, *p[2:]) for p in protos] == [
        (1, STATUS, 0, 0, int(SessionStatus.TRADING), 0, 0),
        (1, ADD, BID, 1_500_000, 300, 11, 0),
        (2, ADD, ASK, 1_500_100, 200, 12, 0),
        (3, MODIFY, BID, 1_500_000, 200, 11, 0),
        (4, EXECUTE, BID, 1_500_000, 50, 11, 0),
        (4, TRADE, ASK, 1_500_000, 50, 0, 1),
        (5, TRADE, BID, 1_500_050, 70, 0, 2),
        (7, STATUS, 0, 0, int(SessionStatus.HALT), 0, 0),
        (8, STATUS, 0, 0, int(SessionStatus.AUCTION), 0, 0),
        (9, STATUS, 0, 0, int(SessionStatus.TRADING), 0, 0),
        (10, CANCEL, ASK, 1_500_100, 0, 12, 0),
        (11, CANCEL, BID, 1_500_000, 0, 11, 0),
        (12, ADD, BID, 1_500_000, 100, 13, 0),
    ]
    assert conv.volume == 50 + 70 + 900 and conv.cross_volume == 900
    assert conv.counters["duplicate_order_ids"] == 1 and conv.counters["halt_messages"] == 3
    assert conv.message_counts == {"1": 4, "2": 2, "3": 1, "4": 1, "5": 1, "6": 1, "7": 3}


def test_seeding_absorbs_the_first_message_and_applies_unseen_orders_to_seeds(tmp_path):
    # the book before the file: bid 150.00 x 500 (two unseen orders), ask 150.01 x 300
    protos, conv = _convert(
        tmp_path,
        [
            "34200.1,3,901,200,1500000,1",  # an unseen bid deleted: already inside row 0
            "34200.2,4,902,100,1500000,1",  # unseen bid executed -> the seed order
            "34200.3,2,903,100,1500100,-1",  # unseen ask partially cancelled
            "34200.4,3,903,200,1500100,-1",  # and then deleted
        ],
        [
            "1500100,300,1500000,300",
            "1500100,300,1500000,200",
            "1500100,200,1500000,200",
            "9999999999,0,1500000,200",
        ],
    )
    assert conv.verification == {"status": "match", "levels": 1, "rows_verified": 4}
    assert conv.counters["absorbed_first_message"] == 1
    assert conv.counters["events_on_seed_orders"] == 3
    ask_seed, bid_seed = SEED_ORDER_BASE + 1, SEED_ORDER_BASE + 2
    assert [p[2:] for p in protos] == [
        (STATUS, 0, 0, int(SessionStatus.TRADING), 0, 0),
        (ADD, ASK, 1_500_100, 300, ask_seed, 0),
        (ADD, BID, 1_500_000, 300, bid_seed, 0),
        (EXECUTE, BID, 1_500_000, 100, bid_seed, 0),
        (TRADE, ASK, 1_500_000, 100, 0, 1),
        (MODIFY, ASK, 1_500_100, 200, ask_seed, 0),
        (CANCEL, ASK, 1_500_100, 0, ask_seed, 0),
    ]


def test_first_message_submission_is_split_out_of_its_seeded_level(tmp_path):
    protos, conv = _convert(
        tmp_path,
        ["34200.1,1,77,100,1500000,1", "34200.2,3,77,100,1500000,1"],
        ["1500100,300,1500000,400", "1500100,300,1500000,300"],
    )
    assert conv.verification["status"] == "match"
    adds = [p[3:7] for p in protos if p[2] == ADD]
    assert adds == [
        (ASK, 1_500_100, 300, SEED_ORDER_BASE + 1),
        (BID, 1_500_000, 300, SEED_ORDER_BASE + 2),  # the level minus the submitted order
        (BID, 1_500_000, 100, 77),
    ]


def test_limit_messages(tmp_path):
    msg, book_path, _ = write_lobster(tmp_path, "AAPL", DATE, 3, n_messages=200)
    conv = LobsterConverter(DATE, 0, msg, book_path, limit_messages=50)
    list(conv.events())
    assert conv.messages_read == 50
    assert conv.verification == {"status": "match", "levels": 5, "rows_verified": 50}


@pytest.mark.parametrize(
    ("lines", "match", "line"),
    [
        (["34200.1,9,1,100,1500000,1"], "unknown LOBSTER event type 9", 1),
        (["34200.1,1,1,100,1500000"], "5 columns", 1),
        (["34200.1,1,1,abc,1500000,1"], "size is not an integer", 1),
        (["34200.1,1,1,100,1500000,1", "34200.0,1,2,100,1500000,1"], "go backwards", 2),
        (["34200.1,1,1,100,1500000,0"], "direction must be 1 or -1", 1),
        (["34200.1,1,1,0,1500000,1"], "must be > 0", 1),
        (["34200.1,7,0,0,5,-1"], "unknown halt code 5", 1),
        (["34200.1,1,1,100,1500000,1", ""], "blank line", 2),
        ([f"34200.1,1,{1 << 60},100,1500000,1"], "outside", 1),
    ],
)
def test_malformed_message_files_raise_typed_errors_with_the_line(tmp_path, lines, match, line):
    with pytest.raises(FeedFormatError, match=match) as err:
        _convert(tmp_path, lines)
    assert err.value.line == line and f"at line {line}" in str(err.value)


def test_orderbook_file_shape_errors(tmp_path):
    two = ["34200.1,1,1,100,1500000,1", "34200.2,1,2,100,1499900,1"]
    row = "9999999999,0,1500000,100"
    with pytest.raises(FeedTruncatedError, match="ends before the message file") as err:
        _convert(tmp_path, two, [row])
    assert err.value.line == 2
    with pytest.raises(FeedFormatError, match="more rows than the message file"):
        _convert(tmp_path, two[:1], [row, row])
    with pytest.raises(FeedFormatError, match="columns"):
        _convert(tmp_path, two[:1], ["1,2,3"])
    with pytest.raises(FeedFormatError, match="levels, earlier rows have"):
        _convert(tmp_path, two, [row, row + "," + row])
    with pytest.raises(FeedFormatError, match="orderbook price is not an integer"):
        _convert(tmp_path, two[:1], ["x,0,1500000,100"])


def test_fixture_builder_is_deterministic():
    assert build_lobster(7, n_messages=100) == build_lobster(7, n_messages=100)
    assert build_lobster(7, n_messages=100) != build_lobster(8, n_messages=100)
