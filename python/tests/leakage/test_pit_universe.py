"""U2 point-in-time universe: as-of availability lag, no retroactive edits, survivorship, delisting retention."""

from __future__ import annotations

import pytest
from iap.data.contracts import AvailabilityRule
from iap.data.pit import ConstituentFile, PITError, PointInTimeUniverse

SESSIONS = [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
LAG2 = AvailabilityRule(
    "publication_date", 2
)  # E0 section 2: first session after D + 1 business day


def _universe(retention: int = 2) -> PointInTimeUniverse:
    files = [
        ConstituentFile("2026-01-05", ("AAA", "BBB", "CCC")),
        ConstituentFile(
            "2026-01-10", ("AAA", "CCC", "DDD")
        ),  # a Saturday file: BBB dropped, DDD added
    ]
    return PointInTimeUniverse(
        SESSIONS,
        files,
        LAG2,
        delistings={"CCC": "2026-01-08", "ZZZ": "2026-01-07"},
        retention_sessions=retention,
    )


def test_as_of_lag_exact_dates():
    u = _universe()
    assert u.available_from("2026-01-05") == "2026-01-07"
    assert u.available_from("2026-01-10") == "2026-01-14"
    for s in ("2026-01-05", "2026-01-06"):
        with pytest.raises(PITError, match="refusing to read a later file"):
            u.members(s)
    assert u.file_in_force("2026-01-07").file_date == "2026-01-05"
    assert (
        u.file_in_force("2026-01-13").file_date == "2026-01-05"
    )  # the newer file is not yet usable
    assert u.file_in_force("2026-01-14").file_date == "2026-01-10"


def test_membership_survivorship_and_retention():
    u = _universe(retention=2)
    # CCC trades through 2026-01-08 and stays a member until then (survivorship)
    assert u.members("2026-01-07") == ("AAA", "BBB", "CCC")
    assert u.members("2026-01-08") == ("AAA", "BBB", "CCC")
    assert u.members("2026-01-09") == ("AAA", "BBB")
    # retention: two sessions after the last traded session CCC is retained, not tradable
    rows = {r.symbol: r for r in u.retained("2026-01-09")}
    assert rows["CCC"].delisted and not rows["CCC"].tradable and not rows["CCC"].member
    assert rows["CCC"].delist_date == "2026-01-08" and rows["AAA"].member and rows["AAA"].tradable
    assert "CCC" in {r.symbol for r in u.retained("2026-01-12")}
    assert "CCC" not in {r.symbol for r in u.retained("2026-01-13")}
    # ZZZ was never a constituent: never retained
    assert all(r.symbol != "ZZZ" for s in SESSIONS[2:] for r in u.retained(s))
    # no retroactive removal: BBB leaves only when the 01-10 file becomes usable (01-14); DDD enters then
    assert "BBB" in u.members("2026-01-13") and "DDD" not in u.members("2026-01-13")
    assert u.members("2026-01-14") == ("AAA", "DDD")
    assert _universe(retention=0).retained("2026-01-09") == tuple(
        r for r in _universe(retention=0).retained("2026-01-09") if not r.delisted
    )


def test_membership_table_and_survivorship_check():
    u = _universe()
    table = u.membership_table()
    assert table[0] == {
        "session": "2026-01-05",
        "file_date": None,
        "members": 0,
        "retained_delisted": 0,
    }
    assert table[2]["members"] == 3 and table[4]["retained_delisted"] == 1
    full = {
        "AAA": SESSIONS,
        "BBB": SESSIONS[:8],
        "CCC": SESSIONS[:4],  # through its delisting on 01-08
        "DDD": SESSIONS,
    }
    assert u.survivorship_check(full) == []
    missing_delisted = dict(
        full, CCC=SESSIONS[:2]
    )  # a survivorship-biased panel drops the delisted name
    findings = u.survivorship_check(missing_delisted)
    assert findings and all("CCC" in f and "survivorship gap" in f for f in findings)
    after_window = dict(full, CCC=SESSIONS[:4] + ["2026-01-15"])  # a row after the retention window
    assert any("after its retention window" in f for f in u.survivorship_check(after_window))


def test_malformed_inputs_are_refused():
    with pytest.raises(PITError, match="strictly increasing"):
        PointInTimeUniverse(
            ["2026-01-06", "2026-01-05"], [ConstituentFile("2026-01-05", ("A",))], LAG2
        )
    with pytest.raises(PITError, match="publication_date"):
        PointInTimeUniverse(
            SESSIONS, [ConstituentFile("2026-01-05", ("A",))], AvailabilityRule("receive_ts", 0)
        )
    with pytest.raises(PITError, match="twice"):
        ConstituentFile("2026-01-05", ("A", "A"))
    with pytest.raises(PITError, match="ISO"):
        ConstituentFile("05/01/2026", ("A",))
    with pytest.raises(PITError, match="share a date"):
        PointInTimeUniverse(
            SESSIONS,
            [ConstituentFile("2026-01-05", ("A",)), ConstituentFile("2026-01-05", ("B",))],
            LAG2,
        )
    u = _universe()
    with pytest.raises(PITError, match="not a session"):
        u.members("2026-01-10")
    with pytest.raises(PITError, match="no constituent file dated"):
        u.available_from("2026-01-11")
    # a file beyond the calendar is never usable
    late = PointInTimeUniverse(SESSIONS, [ConstituentFile("2026-01-16", ("A",))], LAG2)
    assert late.available_from("2026-01-16") is None
    with pytest.raises(PITError):
        late.members("2026-01-16")
