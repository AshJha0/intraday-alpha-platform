"""v1.17 A3: the breadth universe rule on a synthetic full-market day."""

from __future__ import annotations

import json

from iap.marketdata import breadth
from itch50_encoder import Itch50Encoder, hms_ns


def _day(tmp_path):
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    for loc, sym, kw in (
        (1, "AAPL", {}),
        (2, "QQQ", {"etp": "Y"}),
        (3, "ZVZZT", {"authenticity": "T"}),
        (4, "MSFT", {}),
        (5, "TINY", {}),
    ):
        enc.stock_directory(loc, hms_ns(3, 0, loc), sym, **kw)
    enc.system_event(hms_ns(4), "S")
    # pre-market displayed executions do not rank
    enc.add(1, hms_ns(8), 11, "B", 1_000, "AAPL", 1_500_000)
    enc.executed(1, hms_ns(8, 1), 11, 500, 1)
    enc.system_event(hms_ns(9, 30), "Q")
    # regular hours: MSFT 900, AAPL 300 (E) + 200 (printable C), QQQ 600, test 5000, TINY hidden only
    enc.add(4, hms_ns(10), 41, "S", 2_000, "MSFT", 1_000_000)
    enc.executed(4, hms_ns(10, 1), 41, 900, 2)
    enc.add(1, hms_ns(10, 2), 12, "S", 1_000, "AAPL", 1_500_100)
    enc.executed(1, hms_ns(10, 3), 12, 300, 3)
    enc.executed_with_price(1, hms_ns(10, 4), 12, 200, 4, 1_500_200)
    enc.add(2, hms_ns(10, 5), 21, "B", 1_000, "QQQ", 5_000_000)
    enc.executed(2, hms_ns(10, 6), 21, 600, 5)
    enc.add(3, hms_ns(10, 7), 31, "B", 9_000, "ZVZZT", 100_000)
    enc.executed(3, hms_ns(10, 8), 31, 5_000, 6)
    enc.trade(5, hms_ns(10, 9), "B", 700, "TINY", 1_000_000, 7)
    enc.cross(2, hms_ns(16), 10_000, "QQQ", 5_000_000, 8, "C")
    enc.system_event(hms_ns(16), "M")
    enc.add(4, hms_ns(16, 1), 42, "S", 100, "MSFT", 1_000_000)
    enc.executed(4, hms_ns(16, 2), 42, 100, 9)
    return enc.write(tmp_path / "day.gz", compress=True)


def test_rule_ranks_displayed_regular_hours_volume_and_flags(tmp_path):
    src = _day(tmp_path)
    measured = breadth.measure_volumes(src)
    s = measured["symbols"]
    assert s["MSFT"]["displayed_volume"] == 900 and s["MSFT"]["displayed_volume_all_hours"] == 1_000
    assert s["AAPL"]["displayed_volume"] == 500 and s["AAPL"]["displayed_volume_all_hours"] == 1_000
    assert s["QQQ"]["displayed_volume"] == 600 and s["QQQ"]["cross_volume"] == 10_000
    assert s["TINY"]["displayed_volume"] == 0 and s["TINY"]["hidden_volume"] == 700
    assert s["ZVZZT"]["test_security"] and s["QQQ"]["etp"]
    doc = breadth.select_universe(measured, 3, date="2019-01-30", source=src.name, sha256=None)
    assert doc["symbols"] == ["MSFT", "QQQ", "AAPL"]
    assert doc["etfs"] == ["QQQ"] and doc["n_etfs_in_universe"] == 1
    assert doc["n_test_securities_excluded"] == 1 and "ZVZZT" not in [
        r["symbol"] for r in doc["next_50"]
    ]
    assert [r["symbol"] for r in doc["next_50"]] == ["TINY"]
    assert doc["rule_version"] == breadth.RULE_VERSION and "Q and M" in doc["rule"]
    assert doc["universe"][0] == {"rank": 1, **s["MSFT"]}


def test_cli_writes_the_universe_and_smoke_runs_mark_limits(tmp_path, capsys):
    src = _day(tmp_path)
    out = tmp_path / "universe.json"
    assert (
        breadth.main(["--input", str(src), "--date", "2019-01-30", "--top", "2", "--out", str(out)])
        == 0
    )
    doc = json.loads(out.read_text())
    assert doc["symbols"] == ["MSFT", "QQQ"] and len(doc["source_sha256"]) == 64
    assert doc["messages_read"] > 10
    assert (
        breadth.main(
            [
                "--input",
                str(src),
                "--date",
                "2019-01-30",
                "--top",
                "2",
                "--out",
                str(out),
                "--limit-messages",
                "5",
                "--no-hash",
            ]
        )
        == 0
    )
    assert "LIMIT REACHED" in capsys.readouterr().out
    assert json.loads(out.read_text())["limit_reached"] is True
