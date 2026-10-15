"""``iap.reference.session_adv.SessionAdvTable`` (v1.17 A5, review X-9)."""

from __future__ import annotations

import json

import pytest
from iap.reference.session_adv import SessionAdvTable

NS = 1_000_000_000
# 2026-05-18 14:00 UTC = 10:00 New York; 2026-05-19 01:00 UTC = 21:00 New York on the 18th
T_MAY18 = 1_779_112_800 * NS
T_LATE = T_MAY18 + 11 * 3600 * NS


def test_session_date_is_the_calendar_date_in_the_table_zone():
    t = SessionAdvTable({}, {}, "America/New_York")
    assert t.session_date(T_MAY18) == "2026-05-18"
    assert t.session_date(T_LATE) == "2026-05-18"  # 21:00 New York, still the 18th
    assert SessionAdvTable({}, {}, "UTC").session_date(T_LATE) == "2026-05-19"


def test_adv_prefers_the_session_then_the_static_then_the_default():
    t = SessionAdvTable({1: {"2026-05-18": 5e6}}, {1: 4e6, 2: 7e6})
    assert not t.is_static and t.instruments == [1] and t.sessions(1) == ["2026-05-18"]
    assert t.adv(1, T_MAY18) == 5e6
    assert t.adv(1, T_MAY18 + 86_400 * NS) == 4e6  # a session the table lacks
    assert t.adv(2, T_MAY18) == 7e6  # no per-session rows: static
    assert t.adv(3, T_MAY18) is None and t.adv(3, T_MAY18, 1.0) == 1.0
    assert SessionAdvTable({}, {1: 2.0}).is_static


def test_values_must_be_positive_and_zone_known():
    with pytest.raises(ValueError):
        SessionAdvTable({1: {"2026-05-18": 0.0}})
    with pytest.raises(ValueError):
        SessionAdvTable({}, {1: -1.0})
    with pytest.raises(ValueError):
        SessionAdvTable({}, {}, "Mars/Olympus")


def test_builders_from_instruments_json_and_plain_table(tmp_path):
    inst = tmp_path / "instruments.json"
    inst.write_text(
        json.dumps(
            {
                "sessions": {"EQUITY": {"timezone": "America/Chicago"}},
                "instruments": [
                    {
                        "instrument_id": 1,
                        "asset_class": "EQUITY",
                        "adv": 4e6,
                        "adv_by_session": {"2026-05-18": 5e6},
                    },
                    {"instrument_id": 2, "asset_class": "EQUITY", "adv": 9e6},
                ],
            }
        ),
        encoding="utf-8",
    )
    t = SessionAdvTable.from_path(inst)
    assert t.timezone == "America/Chicago"
    assert t.adv(1, T_MAY18) == 5e6 and t.adv(2, T_MAY18) == 9e6
    plain = tmp_path / "adv.json"
    plain.write_text(json.dumps(t.to_dict()), encoding="utf-8")
    u = SessionAdvTable.from_path(plain)
    assert u.to_dict() == t.to_dict()
    assert u.adv(1, T_MAY18) == 5e6 and u.timezone == "America/Chicago"
    with pytest.raises(ValueError):
        SessionAdvTable.from_path(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        SessionAdvTable.from_json(tmp_path / "bad.json")
