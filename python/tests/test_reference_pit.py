"""Point-in-time security master and the corporate-actions adjustment API."""

from __future__ import annotations

from fractions import Fraction

import pytest
from iap.reference.corpactions import (
    CSV_COLUMNS,
    CorporateActionError,
    CorporateActions,
)
from iap.reference.secmaster import SecurityMaster, SecurityMasterError, SecurityRecord


def _rec(symbol: str, date: str, **kw) -> SecurityRecord:
    base = {
        "symbol": symbol,
        "effective_date": date,
        "instrument_id": 1,
        "venue": "XNAS",
        "asset_class": "EQUITY",
        "tick_size": 0.01,
        "round_lot_size": 100,
        "source": "itch50",
    }
    base.update(kw)
    return SecurityRecord(**base)


def test_as_of_returns_the_record_valid_on_the_date_and_never_a_later_one():
    master = SecurityMaster(
        [
            _rec("AAPL", "2019-12-30", round_lot_size=100, locate=13),
            _rec("AAPL", "2020-08-31", round_lot_size=10, locate=14),
            _rec("MSFT", "2020-01-02", instrument_id=2),
        ]
    )
    assert master.as_of("AAPL", "2019-12-30").round_lot_size == 100
    assert master.as_of("AAPL", "2020-08-30").round_lot_size == 100  # the day before the change
    assert master.as_of("AAPL", "2020-08-31").round_lot_size == 10
    assert master.as_of("AAPL", "2031-01-01").locate == 14
    # no lookahead: a date before the first record is an error, not the first record
    with pytest.raises(SecurityMasterError, match="refusing to read a later date"):
        master.as_of("AAPL", "2019-12-27")
    with pytest.raises(SecurityMasterError, match="refusing to read a later date"):
        master.as_of("MSFT", "2019-12-30")
    with pytest.raises(SecurityMasterError, match="no security-master record"):
        master.as_of("TSLA", "2020-01-01")
    with pytest.raises(ValueError):
        master.as_of("AAPL", "not-a-date")
    assert master.symbols() == ["AAPL", "MSFT"]
    assert master.dates("AAPL") == ["2019-12-30", "2020-08-31"] and master.dates("TSLA") == []
    assert master.symbols_on("2020-01-02") == ["MSFT"]


def test_security_master_round_trips_byte_identically(tmp_path):
    records = [
        _rec("MSFT", "2020-01-02", instrument_id=2),
        _rec("AAPL", "2019-12-30", etp_flag="N"),
    ]
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    SecurityMaster(records).save(a)
    SecurityMaster(list(reversed(records))).save(b)  # insertion order does not matter
    assert a.read_bytes() == b.read_bytes() and b"\r" not in a.read_bytes()
    loaded = SecurityMaster.load(a)
    assert loaded.records() == sorted(records, key=lambda r: r.symbol)
    loaded.upsert(_rec("AAPL", "2019-12-30", round_lot_size=50))  # same key: replaced
    assert len(loaded.records()) == 2 and loaded.as_of("AAPL", "2020-01-01").round_lot_size == 50


def test_security_record_validation(tmp_path):
    with pytest.raises(ValueError, match="round_lot_size"):
        _rec("AAPL", "2019-12-30", round_lot_size=0)
    with pytest.raises(ValueError, match="tick_size"):
        _rec("AAPL", "2019-12-30", tick_size=0.0)
    with pytest.raises(ValueError):
        _rec("AAPL", "2019-13-45")
    with pytest.raises(ValueError, match="unknown fields"):
        SecurityRecord.from_dict({**_rec("AAPL", "2019-12-30").to_dict(), "surprise": 1})
    bad = tmp_path / "v.json"
    bad.write_text('{"x-version": 99, "records": []}')
    with pytest.raises(ValueError, match="x-version"):
        SecurityMaster.load(bad)


def _table(tmp_path, rows: list[str]) -> CorporateActions:
    path = tmp_path / "corporate_actions.csv"
    path.write_text(",".join(CSV_COLUMNS) + "\n" + "\n".join(rows) + "\n")
    return CorporateActions.load_csv(path)


def test_split_adjustment_is_exact_and_point_in_time(tmp_path):
    table = _table(
        tmp_path,
        [
            "2020-08-31,AAPL,SPLIT,4,1,,",
            "2014-06-09,AAPL,SPLIT,7,1,,",
            "2020-08-31,TSLA,SPLIT,5,1,,",
            "2021-03-01,XYZ,SPLIT,1,10,,",
        ],
    )
    assert len(table) == 4
    assert table.split_factor("AAPL", "2020-08-28", "2020-08-31") == 4
    assert table.split_factor("AAPL", "2014-06-06", "2020-12-31") == 28
    # ex-date itself is already in the new basis; an earlier as_of does not know the split
    assert table.split_factor("AAPL", "2020-08-31", "2020-12-31") == 1
    assert table.split_factor("AAPL", "2020-08-01", "2020-08-28") == 1
    assert table.split_factor("MSFT", "2010-01-01", "2030-01-01") == 1
    assert table.adjust_price(Fraction(50000, 100), "AAPL", "2020-08-28", "2020-09-01") == 125
    # ticks round half up, quantities round down (pinned)
    assert table.adjust_price_ticks(49_950, "AAPL", "2020-08-28", "2020-09-01") == 12_488
    assert table.adjust_price_ticks(49_949, "AAPL", "2020-08-28", "2020-09-01") == 12_487
    assert table.adjust_qty(100, "AAPL", "2020-08-28", "2020-09-01") == 400
    assert table.split_factor("XYZ", "2021-02-26", "2021-03-01") == Fraction(1, 10)
    assert table.adjust_qty(105, "XYZ", "2021-02-26", "2021-03-01") == 10
    assert table.adjust_price_ticks(123, "XYZ", "2021-02-26", "2021-03-01") == 1230
    with pytest.raises(CorporateActionError, match="before the observation date"):
        table.split_factor("AAPL", "2020-09-01", "2020-08-01")
    # adjusted then un-adjusted equals the original (re-basing is exact)
    factor = table.split_factor("AAPL", "2014-06-06", "2020-12-31")
    assert table.adjust_price(645, "AAPL", "2014-06-06", "2020-12-31") * factor == 645


def test_dividend_and_total_return_adjustment(tmp_path):
    table = _table(
        tmp_path,
        ["2020-08-07,AAPL,DIVIDEND,,,0.82,", "2020-08-31,AAPL,SPLIT,4,1,,"],
    )
    closes = {"2020-08-05": 440.25, "2020-08-06": 455.61, "2020-08-07": 444.45}
    factor = table.dividend_factor("AAPL", "2020-08-06", "2020-08-10", closes)
    assert factor == pytest.approx(1 - 0.82 / 455.61, abs=1e-15)
    assert table.dividend_factor("AAPL", "2020-08-07", "2020-08-10", closes) == 1.0
    total = table.total_return_price(455.61, "AAPL", "2020-08-06", "2020-09-01", closes)
    assert total == pytest.approx((455.61 - 0.82) / 4, abs=1e-12)
    with pytest.raises(CorporateActionError, match="no close before the ex-date"):
        table.dividend_factor("AAPL", "2020-08-06", "2020-08-10", {"2020-08-07": 444.45})
    with pytest.raises(CorporateActionError, match="not below the previous close"):
        table.dividend_factor("AAPL", "2020-08-06", "2020-08-10", {"2020-08-06": 0.5})
    assert [a.action for a in table.for_symbol("AAPL")] == ["DIVIDEND", "SPLIT"]
    assert [a.ex_date for a in table.for_symbol("AAPL", "2020-08-08", "2020-12-31")] == [
        "2020-08-31"
    ]
    assert table.actions()[0].to_dict()["cash_amount"] == "41/50"


def test_symbol_changes_resolve_in_both_directions(tmp_path):
    table = _table(
        tmp_path,
        ["2022-06-09,FB,SYMBOL_CHANGE,,,,META", "2030-01-02,META,SYMBOL_CHANGE,,,,MVRS"],
    )
    assert table.symbol_as_of("FB", "2022-06-08", "2022-06-09") == "META"
    assert table.symbol_as_of("FB", "2022-06-08", "2022-06-08") == "FB"  # not known yet
    assert table.symbol_as_of("FB", "2020-01-01", "2031-01-01") == "MVRS"
    assert table.symbol_on("META", "2022-12-30", "2022-06-08") == "FB"
    assert table.symbol_on("META", "2022-12-30", "2022-06-09") == "META"
    assert table.symbol_on("MVRS", "2031-01-01", "2021-01-01") == "FB"
    assert table.symbol_as_of("AAPL", "2020-01-01", "2031-01-01") == "AAPL"


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ("2020-08-31,AAPL,SPLIT,4,,,", "ratio_old must be a positive integer"),
        ("2020-08-31,AAPL,SPLIT,0,1,,", "ratio_new must be a positive integer"),
        ("2020-08-31,AAPL,SPLIT,4,1,0.5,", "SPLIT does not take"),
        ("2020-08-31,AAPL,DIVIDEND,,,abc,", "cash_amount must be a decimal"),
        ("2020-08-31,AAPL,DIVIDEND,,,-1,", "cash_amount must be > 0"),
        ("2020-08-31,AAPL,SYMBOL_CHANGE,,,,AAPL", "different symbol"),
        ("2020-08-31,AAPL,MERGER,,,,", "unknown action"),
        ("31/08/2020,AAPL,SPLIT,4,1,,", "ex_date must be YYYY-MM-DD"),
        ("2020-08-31,,SPLIT,4,1,,", "symbol is empty"),
    ],
)
def test_malformed_tables_are_rejected_with_the_line(tmp_path, row, match):
    with pytest.raises(CorporateActionError, match=match) as err:
        _table(tmp_path, [row])
    assert "line 2" in str(err.value)


def test_header_and_duplicates_are_checked(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("date,symbol,action\n2020-08-31,AAPL,SPLIT\n")
    with pytest.raises(CorporateActionError, match="header must be exactly"):
        CorporateActions.load_csv(path)
    with pytest.raises(CorporateActionError, match="duplicate corporate action"):
        _table(tmp_path, ["2020-08-31,AAPL,SPLIT,4,1,,", "2020-08-31,AAPL,SPLIT,2,1,,"])
    assert len(_table(tmp_path, [])) == 0
