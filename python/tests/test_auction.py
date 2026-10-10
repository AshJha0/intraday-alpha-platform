"""Auction imbalance (NOII) research, plan item A1: opt-in extraction,
features, targets, strategy, backtest and walk-forward.

All input bytes are synthesised (``itch50_encoder`` framing, NOII payloads
packed here); no vendor data.
"""

from __future__ import annotations

import datetime as dt
import json
import struct

import numpy as np
import pandas as pd
import pytest
from iap.auction import (
    AuctionStrategyConfig,
    AuctionStream,
    auction_features,
    auction_targets,
    backtest,
    extract_auction_stream,
    read_auction_stream,
    walk_forward,
)
from iap.auction.__main__ import main as auction_main
from iap.auction.strategy import decision_rows
from iap.marketdata.itch50 import Itch50Reader, NetOrderImbalance
from itch50_encoder import Itch50Encoder, hms_ns

SYMBOLS = ("AAPL", "MSFT")


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


def _session(seed: int, effect_bps: float = 8.0) -> Itch50Encoder:
    """One day: R messages, an opening NOII window + cross, a closing NOII
    window (every 30 s from 15:50) + cross; the cross moves with the imbalance."""
    rng = np.random.default_rng(seed)
    enc = Itch50Encoder()
    loc = {s: 10 + i for i, s in enumerate((*SYMBOLS, "ZZZZ"))}
    for s, lc in loc.items():
        enc.stock_directory(lc, hms_ns(4), s)
    match = 1
    for s, lc in loc.items():
        ref = 1_500_000 + 100_000 * lc
        _noii(enc, lc, hms_ns(9, 28), s, 5000, 2000, "B", 0, 0, ref, "O")
        enc.cross(lc, hms_ns(9, 30), 7000, s, ref + 150, match, "O")
        match += 1
    for s, lc in loc.items():
        ref = 1_500_000 + 100_000 * lc
        sign = 1 if rng.random() < 0.5 else -1
        direction = "B" if sign > 0 else "S"
        ts = hms_ns(15, 50)
        for k in range(20):
            imb = int(4000 + rng.integers(0, 1000))
            far = ref + sign * 300 if k >= 10 else 0
            near = ref + sign * 100 if k >= 10 else 0
            _noii(enc, lc, ts + k * 30 * 10**9, s, 20_000, imb, direction, far, near, ref)
            ref += sign * int(rng.integers(0, 30))
        cross_px = int(round(ref * (1 + sign * effect_bps * 1e-4)))
        enc.cross(lc, hms_ns(16), 50_000, s, cross_px, match, "C")
        match += 1
    return enc


def _days(n: int) -> list[str]:
    d0 = dt.date(2026, 3, 2)
    out, d = [], d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def stream(tmp_path_factory) -> AuctionStream:
    root = tmp_path_factory.mktemp("auction")
    parts = []
    for i, day in enumerate(_days(10)):
        p = _session(100 + i).write(root / f"{day}.itch")
        parts.append(extract_auction_stream(p, symbols=SYMBOLS, date=day))
    return AuctionStream.concat(parts)


def test_reader_default_skips_noii_and_opt_in_decodes(tmp_path):
    p = _session(1).write(tmp_path / "d.itch")
    default = list(Itch50Reader(p))
    assert not any(isinstance(m, NetOrderImbalance) for m in default)
    r = Itch50Reader(p, symbols=["AAPL"], noii=True)
    msgs = [m for m in r if isinstance(m, NetOrderImbalance)]
    assert len(msgs) == 21 and {m.stock for m in msgs} == {"AAPL"}
    assert r.counts["I"] == 63
    first_close = msgs[1]
    assert first_close.paired_shares == 20_000 and first_close.cross_type == "C"
    assert first_close.imbalance_direction in ("B", "S") and first_close.price_variation == "L"
    # opting in changes nothing else the reader yields
    others = [m for m in Itch50Reader(p, noii=True) if not isinstance(m, NetOrderImbalance)]
    assert others == default


def test_stream_roundtrip_and_refuses_dataset_dir(stream, tmp_path):
    assert set(stream.noii["symbol"]) == set(SYMBOLS)
    out = stream.write(tmp_path / "s")
    back = read_auction_stream(out)
    pd.testing.assert_frame_equal(back.noii, stream.noii, check_dtype=False)
    assert len(back.crosses) == len(stream.crosses) == 10 * 2 * 2
    (tmp_path / "ds").mkdir()
    (tmp_path / "ds" / "manifest.json").write_text("{}")
    with pytest.raises(ValueError, match="dataset directory"):
        stream.write(tmp_path / "ds")


def test_features_are_causal_and_bounded(stream):
    f = auction_features(stream.noii, "C")
    assert len(f) == 10 * 2 * 20
    assert f["imbalance_ratio"].abs().max() <= 1.0
    assert (f["time_to_cross_s"] > 0).all() and f["time_to_cross_s"].max() == 600
    first = f.groupby(["date", "symbol"]).head(1)
    assert (first["ref_drift_bps"] == 0).all()
    assert f["far_near_bps"].isna().sum() == 10 * 2 * 10
    # causality: features of a truncated stream equal the prefix of the full one
    cut = stream.noii[stream.noii["tod_ns"] < hms_ns(15, 55)]
    g = auction_features(cut, "C")
    key = ["date", "symbol", "ts"]
    m = g.merge(f, on=key, suffixes=("", "_full"))
    for c in ("imbalance_ratio", "ref_drift_bps", "time_to_cross_s"):
        np.testing.assert_allclose(m[c], m[f"{c}_full"])


def test_targets_close_and_open(stream):
    lab = auction_targets(auction_features(stream.noii, "C"), stream.crosses)
    assert (lab["mid_source"] == "noii_reference").all()
    assert lab["cross_vs_mid_bps"].notna().all()
    # planted: the cross moves with the imbalance
    assert (np.sign(lab["cross_vs_mid_bps"]) == np.sign(lab["imbalance_ratio"])).all()
    op = auction_targets(auction_features(stream.noii, "O"), stream.crosses)
    assert len(op) == 20
    np.testing.assert_allclose(op["cross_vs_mid_bps"], 1e4 * 150 / (op["ref"] * 1e4), rtol=1e-9)


def test_targets_with_mids(stream):
    f = auction_features(stream.noii, "C")
    mids = f[["symbol", "ts"]].assign(mid=f["ref"] + 0.01, half_spread=0.005)
    lab = auction_targets(f, stream.crosses, mids)
    assert (lab["mid_source"] == "mids").all()
    np.testing.assert_allclose(lab["mid_t"], f["ref"] + 0.01)
    assert lab["mid_pre_cross"].notna().all()


def test_backtest_follow_beats_fade_and_costs_apply(stream):
    lab = auction_targets(auction_features(stream.noii, "C"), stream.crosses)
    cfg = AuctionStrategyConfig(decision_s=300, threshold=0.05)
    dec = decision_rows(lab, cfg)
    assert len(dec) == 20 and (dec["time_to_cross_s"] <= 300).all()
    follow = backtest(lab, cfg).summary()
    fade = backtest(lab, AuctionStrategyConfig(decision_s=300, threshold=0.05, orientation=-1))
    assert follow["n_trades"] == 20
    assert follow["mean_net_bps"] > 0 > fade.summary()["mean_net_bps"]
    assert follow["mean_cost_bps"] > 0
    taker = backtest(lab, AuctionStrategyConfig(decision_s=300, threshold=0.05, exit="taker"))
    assert taker.summary()["mean_cost_bps"] > follow["mean_cost_bps"]
    assert backtest(lab, AuctionStrategyConfig(threshold=0.99)).summary()["n_trades"] == 0


def test_walk_forward_learns_orientation_out_of_sample(stream):
    lab = auction_targets(auction_features(stream.noii, "C"), stream.crosses)
    res = walk_forward(lab, AuctionStrategyConfig(decision_s=300), n_folds=4)
    assert len(res.folds) == 4
    assert all(f["orientation"] == 1 for f in res.folds)
    s = res.summary()
    assert s["n_trades"] > 0 and s["mean_net_bps"] > 0
    assert set(res.trades["fold"]) == {1, 2, 3, 4}
    json.dumps(res.to_dict(), default=str)


def test_cli_extract_and_backtest(tmp_path, capsys):
    paths = []
    for i, day in enumerate(_days(4)):
        p = _session(7 + i).write(tmp_path / f"{day}.itch")
        out = tmp_path / f"s{i}"
        assert auction_main(["extract", "--itch", str(p), "--date", day, "--out", str(out)]) == 0
        paths.append(str(out))
    rep = tmp_path / "r.json"
    args = [
        "backtest",
        "--stream",
        *paths,
        "--threshold",
        "0.1",
        "--walk-forward",
        "--folds",
        "2",
        "--out",
        str(rep),
    ]
    board = tmp_path / "empty_board.jsonl"
    board.write_text("")
    assert auction_main([*args, "--prereg-board", str(board)]) == 2
    assert auction_main([*args, "--no-prereg"]) == 0
    doc = json.loads(rep.read_text())
    assert doc["alpha"] == "AUC01" and doc["preregistered"] is False
    assert doc["in_sample"]["summary"]["n_trades"] > 0
    assert len(doc["walk_forward"]["folds"]) == 2
