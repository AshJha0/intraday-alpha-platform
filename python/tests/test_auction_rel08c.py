"""REL-08c: NOII extractor and close-imbalance study on synthetic data only."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.auction.rel08c_extract import (
    CROSS_FILE,
    GRID_FILE,
    META_FILE,
    NOII_FILE,
    extract_rel08c,
    load_universe,
)
from iap.auction.rel08c_study import (
    Rel08cConfig,
    decision_trades,
    load_extract,
    mde_bps,
    run_study,
)
from itch50_encoder import Itch50Encoder, hms_ns

REPO = Path(__file__).resolve().parents[2]


def _noii(enc, locate, ts, symbol, paired, imb, direction, far, near, ref, cross="C"):
    tail = struct.pack(
        ">QQc8sIIIcc",
        paired,
        imb,
        direction.encode(),
        symbol.encode().ljust(8, b" "),
        far,
        near,
        ref,
        cross.encode(),
        b"L",
    )
    enc._msg("I", locate, ts, tail)


def _file(tmp_path: Path, compress: bool = False) -> Path:
    enc = Itch50Encoder()
    enc.system_event(hms_ns(4), "O")
    enc.stock_directory(1, hms_ns(4), "AAA")
    enc.stock_directory(2, hms_ns(4), "BBB")
    enc.stock_directory(3, hms_ns(4), "ZZZ")  # not wanted
    enc.add(1, hms_ns(10), 11, "B", 300, "AAA", 1_000_000)
    enc.add(1, hms_ns(10), 12, "S", 200, "AAA", 1_000_200)
    enc.add(1, hms_ns(10), 13, "B", 100, "AAA", 999_900)
    enc.add(3, hms_ns(10), 99, "B", 100, "ZZZ", 5_000_000)
    enc.add(2, hms_ns(10), 21, "B", 100, "BBB", 500_000)
    enc.add(2, hms_ns(10), 22, "S", 100, "BBB", 500_100)
    # at exactly 15:30:01 the AAA bid is executed away: in the 15:30:01 grid point
    enc.executed(1, hms_ns(15, 30, 1), 11, 300, 1)
    # 1 ns after 15:30:02: not in the 15:30:02 grid point
    enc.cancel(1, hms_ns(15, 30, 2) + 1, 12, 50)
    enc.replace(2, hms_ns(15, 30, 3), 22, 23, 400, 500_050)
    enc.delete(1, hms_ns(15, 30, 4), 13)
    _noii(enc, 1, hms_ns(15, 50), "AAA", 1000, 1000, "B", 0, 0, 1_000_100)
    _noii(enc, 1, hms_ns(15, 50, 10), "AAA", 1000, 3000, "B", 0, 0, 1_000_100)
    _noii(enc, 1, hms_ns(15, 50, 20), "AAA", 1000, 1000, "S", 0, 0, 1_000_100)
    _noii(enc, 1, hms_ns(15, 50, 30), "AAA", 1000, 0, "N", 1_000_300, 1_000_200, 1_000_100)
    _noii(enc, 3, hms_ns(15, 50, 30), "ZZZ", 1, 1, "B", 0, 0, 5_000_000)
    enc.cross(1, hms_ns(16), 5000, "AAA", 1_000_150, 7, "C")
    enc.cross(3, hms_ns(16), 5000, "ZZZ", 5_000_000, 8, "C")
    enc.system_event(hms_ns(20), "C")
    return enc.write(tmp_path / ("f.itch.gz" if compress else "f.itch"), compress=compress)


def _read(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("compress", [False, True])
def test_extractor_streams_and_provenance(tmp_path, compress):
    src = _file(tmp_path, compress)
    out = tmp_path / "x"
    meta = extract_rel08c(src, ["AAA", "BBB", "QQQ"], out, date="2019-01-30")
    assert meta["input"]["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert meta["symbols_missing"] == ["QQQ"]
    assert meta["rows"]["noii"] == 4 and meta["rows"]["crosses"] == 1
    assert meta["message_counts"]["I"] == 5 and meta["messages_filtered"] >= 3
    noii = _read(out / NOII_FILE)
    assert {r["symbol"] for r in noii} == {"AAA"}
    assert [r["level"] for r in noii] == ["0.5", "0.75", "-0.5", "0.0"]
    assert noii[0]["change"] == "" and float(noii[1]["change"]) == pytest.approx(0.25)
    assert [r["persistence"] for r in noii] == ["1", "2", "1", "0"]
    assert float(noii[0]["distance_to_close_s"]) == 600.0
    assert noii[3]["far_px4"] == "1000300" and noii[3]["near_px4"] == "1000200"
    assert _read(out / CROSS_FILE)[0]["price_px4"] == "1000150"
    grid = {(r["symbol"], int(r["tod_ns"])): r for r in _read(out / GRID_FILE)}
    assert len(grid) == 2 * 1801
    g0 = grid[("AAA", hms_ns(15, 30))]
    assert (g0["bid_px4"], g0["bid_size"], g0["ask_size"]) == ("1000000", "300", "200")
    g1 = grid[("AAA", hms_ns(15, 30, 1))]  # message at ts == t is included
    assert g1["bid_px4"] == "999900"
    assert grid[("AAA", hms_ns(15, 30, 2))]["ask_size"] == "200"  # t + 1 ns excluded
    assert grid[("AAA", hms_ns(15, 30, 3))]["ask_size"] == "150"
    assert grid[("AAA", hms_ns(15, 30, 4))]["bid_px4"] == ""
    assert grid[("BBB", hms_ns(15, 30, 3))]["ask_px4"] == "500050"
    assert grid[("BBB", hms_ns(15, 30, 3))]["ask_size"] == "400"
    assert json.loads((out / META_FILE).read_text())["format"] == "iap.rel08c_extract"


def test_expected_sha_mismatch_refused(tmp_path):
    with pytest.raises(ValueError, match="sha256"):
        extract_rel08c(_file(tmp_path), ["AAA"], tmp_path / "x", expected_sha256="00")


def test_universe_file_has_100_names():
    assert len(load_universe(REPO / "research/breadth/universe.json")) == 100


# ---- synthetic imbalance streams for the study -------------------------------------

T1, T2 = hms_ns(15, 51), hms_ns(15, 53)
EXIT = hms_ns(15, 54, 55)


def _session(d: Path, date: str, rng, effect_bps: float, n_sym: int = 40) -> Path:
    d.mkdir(parents=True)
    noii, grid, cross = [], [], []
    for i in range(n_sym):
        sym = f"S{i:03d}"
        mid = 1_000_000.0
        sign = 1 if rng.random() < 0.5 else -1
        for k, ts in enumerate(range(hms_ns(15, 50), hms_ns(15, 55), 10 * 10**9)):
            noii.append(
                (
                    sym,
                    ts,
                    "C",
                    1000,
                    2000,
                    "B" if sign > 0 else "S",
                    2 / 3 * sign,
                    "" if k == 0 else 0.0,
                    k + 1,
                    (hms_ns(16) - ts) / 1e9,
                    1_000_000,
                    0,
                    0,
                    "L",
                )
            )
        drift = rng.normal(0, 3)  # baseline 15:30 -> t move, imbalance-independent
        move = sign * effect_bps + rng.normal(0, 2)
        for t, m in (
            (hms_ns(15, 30), mid),
            (T1, mid * (1 + drift / 1e4)),
            (T2, mid * (1 + drift / 1e4)),
            (EXIT, mid * (1 + (drift + move) / 1e4)),
        ):
            grid.append((sym, t, round(m - 50), 500, round(m + 50), 500))
        cross.append((sym, hms_ns(16), "C", 1000, round(mid * (1 + (drift + move) / 1e4))))
    for name, cols, rows in (
        (
            NOII_FILE,
            "symbol tod_ns cross_type paired_shares imbalance_shares "
            "imbalance_direction level change persistence distance_to_close_s ref_px4 "
            "far_px4 near_px4 price_variation",
            noii,
        ),
        (GRID_FILE, "symbol tod_ns bid_px4 bid_size ask_px4 ask_size", grid),
        (CROSS_FILE, "symbol tod_ns cross_type shares price_px4", cross),
    ):
        pd.DataFrame(rows, columns=cols.split()).to_csv(d / name, index=False)
    (d / META_FILE).write_text(json.dumps({"date": date}))
    return d


def _sessions(tmp_path, n, effect, seed=1):
    rng = np.random.default_rng(seed)
    return [_session(tmp_path / f"d{k}", f"2019-02-{k + 1:02d}", rng, effect) for k in range(n)]


CFG = Rel08cConfig()


def test_strong_effect_many_sessions_continues(tmp_path):
    r = run_study(_sessions(tmp_path, 8, 10.0), CFG, prereg_id=None, synthetic=True)
    assert r.summary["verdict"] == "continue"
    m = r.summary["measures"]["imbalance"]
    assert m["prediction"]["gross_taker_bps"] > 8
    # net = gross minus two half-spreads (0.5 bp each) and two 0.3 bp fees
    assert m["execution"]["net_taker_bps"] == pytest.approx(
        m["prediction"]["gross_taker_bps"] - 1.6, abs=0.05
    )
    assert m["fill"]["entry"] == 1.0


def test_no_effect_rejects(tmp_path):
    r = run_study(_sessions(tmp_path, 8, 0.0), CFG, prereg_id=None, synthetic=True)
    assert r.summary["verdict"] == "reject" and r.summary["mde_bps"] < CFG.target_bps


def test_one_session_is_underpowered(tmp_path):
    r = run_study(_sessions(tmp_path, 1, 10.0), CFG, prereg_id=None, synthetic=True)
    assert r.summary["verdict"] == "underpowered"
    assert math.isinf(r.summary["mde_bps"])
    assert any("screen only" in x for x in r.summary["reasons"])


def test_mde():
    assert math.isinf(mde_bps(1.0, 1, 0.05, 0.8))
    from scipy import stats

    q = stats.t.ppf(0.975, 3) + stats.t.ppf(0.8, 3)
    assert mde_bps(1.0, 4, 0.05, 0.8) == pytest.approx(q / 2)


def test_no_look_ahead_in_features(tmp_path):
    (d,) = _sessions(tmp_path, 1, 10.0)
    date, noii, crosses, grid = load_extract(d)
    base = decision_trades(date, noii, crosses, grid, CFG)
    late = noii["tod_ns"] > T1
    noii2 = noii.copy()
    noii2.loc[late, "level"] = -noii2.loc[late, "level"]
    noii2.loc[late, "imbalance_direction"] = "N"
    alt = decision_trades(date, noii2, crosses, grid, CFG)
    at_t1 = [x[x["decision_tod_ns"] == T1].reset_index(drop=True) for x in (base, alt)]
    pd.testing.assert_frame_equal(*at_t1)
    assert (at_t1[0]["f_noii_tod_ns"].dropna() <= T1).all()
    # before 15:50 there is no closing NOII: the conditioned policy is flat
    early = Rel08cConfig(decision_tod_ns=(hms_ns(15, 45),))
    g = grid.copy()
    g.loc[g["tod_ns"] == T1, "tod_ns"] = hms_ns(15, 45)
    t = decision_trades(date, noii, crosses, g, early)
    assert set(t["policy"]) == {"baseline"}


def test_refuses_without_signed_prereg(tmp_path):
    dirs = _sessions(tmp_path, 1, 0.0)
    with pytest.raises(PermissionError):
        run_study(dirs, CFG, prereg_id=None)
    with pytest.raises(PermissionError):  # does not exist / not signed
        run_study(dirs, CFG, prereg_id="research/preregs/rel08c_prereg.json")


def test_decision_time_after_cutoff_refused():
    with pytest.raises(ValueError):
        Rel08cConfig(decision_tod_ns=(hms_ns(15, 55),))
