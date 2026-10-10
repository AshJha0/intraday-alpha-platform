"""Real-data study drivers iap.research.auction_real / quoting_real: pure helpers."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from iap.core.events import EventType, MarketEvent
from iap.research import auction_real as ar
from iap.research import quoting_real as qr


def _add(eid: int, ts: int, side: int, px: int, oid: int, iid: int = 1) -> MarketEvent:
    return MarketEvent(eid, iid, 101, ts, ts, eid, EventType.ADD, side, px, 100, oid, 0)


def test_book_mids_prevailing_book_and_nan_when_one_sided_or_crossed():
    evs = [
        _add(1, 100, 0, 10000, 1),  # bid 100.00
        _add(2, 200, 1, 10002, 2),  # ask 100.02
        _add(3, 300, 1, 9999, 3),  # crossing ask 99.99
    ]
    q = {1: np.array([50, 150, 200, 250, 300, 400], dtype=np.int64)}
    mid, hs = ar.book_mids(iter(evs), q, {1: 0.01})[1]
    assert math.isnan(mid[0]) and math.isnan(mid[1])  # nothing / bid only
    assert mid[2] == pytest.approx(100.01) and hs[2] == pytest.approx(0.01)  # ts <= t
    assert mid[3] == pytest.approx(100.01)
    assert math.isnan(mid[4]) and math.isnan(mid[5])  # crossed


def test_query_times_adds_one_ns_before_the_scheduled_cross():
    midnight = 1_000_000 * 10**9
    tod = 16 * 3600 * 10**9 - 5 * 10**9
    noii = pd.DataFrame(
        {"symbol": ["AAPL"], "ts": [midnight + tod], "tod_ns": [tod], "cross_type": ["C"]}
    )
    qs = ar.query_times(noii)["AAPL"]
    assert list(qs) == [midnight + tod, midnight + 16 * 3600 * 10**9 - 1]


def test_auction_verdicts():
    good = {"n_sessions": 5, "mean": 2.0, "ci_bonf": [0.5, 3.5]}
    assert ar.verdict(good).startswith("EXISTS")
    assert ar.verdict({**good, "n_sessions": 3}).startswith("NO")
    assert ar.verdict({**good, "ci_bonf": [-0.1, 3.0]}).startswith("NO")
    assert ar.holdout_verdict(True, np.array([5.0, 6.0, 7.0]))["verdict"].startswith("CONF")
    assert ar.holdout_verdict(False, np.array([5.0, 6.0, 7.0]))["verdict"] == "NOT CONFIRMED"
    assert ar.holdout_verdict(True, np.array([5.0, 6.0]))["verdict"] == "NOT CONFIRMED"
    assert ar.holdout_verdict(True, np.zeros(0))["n_trades"] == 0


def test_second_grid_takes_the_first_row_of_each_second_in_window():
    sec = 10**9
    feats = pd.DataFrame({"exchange_ts": [sec, sec + 5, 2 * sec + 1, 3 * sec, 4 * sec]})
    g = qr.second_grid(feats, sec, 4 * sec)
    assert list(g["exchange_ts"]) == [sec, 2 * sec + 1, 3 * sec]


def _unit(session, symbol, nets, fills=10, warm=False):
    res = {
        cell: {
            **dict.fromkeys(qr.PARTS, 0.0),
            "net": net,
            "n_quote_fills": fills,
            "max_abs_inventory": 100,
        }
        for cell, net in nets.items()
    }
    return {"session": session, "symbol": symbol, "warmup_only": warm, "results": res}


def test_summarise_pools_symbols_per_session_and_pairs_with_control(tmp_path):
    import json

    prereg = {
        "cells": [
            {"id": "EQ01", "alpha": "EQ01"},
            {"id": "QNOSKEW", "alpha": None},
        ]
    }
    units = [_unit("s0", "AAPL", {}, warm=True)]
    for i, s in enumerate(["s1", "s2", "s3", "s4", "s5"]):
        units.append(_unit(s, "AAPL", {"EQ01": 10.0 + i, "QNOSKEW": 1.0}))
        units.append(_unit(s, "QQQ", {"QNOSKEW": 100.0}))
    (tmp_path / "u").mkdir()
    for k, u in enumerate(units):
        (tmp_path / "u" / f"{k}.json").write_text(json.dumps(u))
    out = qr.summarise(tmp_path / "u", prereg)
    eq = out["cells"]["EQ01"]
    assert eq["n_sessions"] == 5 and eq["mean"] == pytest.approx(12.0)
    assert eq["verdict"].startswith("EXISTS")
    # control paired on EQ01's symbols only (AAPL), not QQQ
    assert eq["skew_minus_control"]["mean"] == pytest.approx(11.0)
    assert out["cells"]["QNOSKEW"]["mean"] == pytest.approx(101.0)


class _FakeStream:
    meta = {"noii_messages": 1, "cross_messages": 0}

    def write(self, out):
        from pathlib import Path

        Path(out).mkdir(parents=True)
        (Path(out) / "auction_noii.csv").write_text("x\n")


def test_extract_resumable_skips_completed_and_redoes_partial(tmp_path):
    itch = tmp_path / "f.gz"
    itch.write_bytes(b"abc")
    out = tmp_path / "auction" / "2019-01-30"
    calls = []

    def fake(path, symbols, date):
        calls.append(date)
        return _FakeStream()

    out.mkdir(parents=True)
    (out / "auction_noii.csv").write_text("partial")  # killed run: no marker
    assert ar.extract_resumable(itch, "2019-01-30", ["AAPL"], out, extract=fake)
    assert (out / ar.DONE_FILE).is_file() and (out / "auction_noii.csv").read_text() == "x\n"
    assert not ar.extract_resumable(itch, "2019-01-30", ["AAPL"], out, extract=fake)
    assert calls == ["2019-01-30"]
    itch.write_bytes(b"abcd")  # a different input is not 'done'
    assert ar.extract_resumable(itch, "2019-01-30", ["AAPL"], out, extract=fake)
    assert not list(out.parent.glob(".*tmp*"))


def test_atomic_text_leaves_no_temp(tmp_path):
    p = tmp_path / "a" / "s.json"
    ar.atomic_text(p, "1")
    ar.atomic_text(p, "2")
    assert p.read_text() == "2" and [x.name for x in p.parent.iterdir()] == ["s.json"]


def test_quoting_run_unit_skips_a_completed_unit(tmp_path, monkeypatch):
    study = qr.Study.__new__(qr.Study)
    study.out = tmp_path
    (tmp_path / "units").mkdir()
    (tmp_path / "units" / "ds3_20190130_AAPL.json").write_text("{}")
    monkeypatch.setattr(qr, "load_events", lambda *a, **k: pytest.fail("reloaded a done unit"))
    study.run_unit(0, "AAPL")
