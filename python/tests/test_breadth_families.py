"""Pre-specified bar families (iap.research.breadth_families): planted effects
each family must find, no use of data after the decision bar, the exact cost
arithmetic, and F2's undefined sessions.  Synthetic panels only."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.research import breadth_bars as bb
from iap.research import breadth_families as bf

N = 200


def _panel(mid: np.ndarray, hs: float = 0.0001, vol: np.ndarray | None = None) -> bb.Panel:
    n = mid.shape[0]
    v = np.full(mid.shape, 100.0) if vol is None else vol
    return bb.Panel(
        symbols=tuple(f"S{i:03d}" for i in range(n)),
        etp=np.zeros(n, dtype=bool),
        mid=mid,
        half_spread=np.where(np.isfinite(mid), hs, np.nan),
        vol=v,
        notional=v * np.nan_to_num(mid),
        valid_ns=np.where(np.isfinite(mid), float(bb.MINUTE_NS), 0.0),
    )


def _rows(n: int = N) -> np.ndarray:
    return np.arange(n, dtype=np.intp)


def _walk(rng, drift: np.ndarray, noise: float = 2e-4) -> np.ndarray:
    steps = drift[:, None] + rng.normal(0, noise, (len(drift), bb.BARS))
    return 50.0 * np.exp(np.cumsum(steps, axis=1))


def test_f1_finds_planted_continuation_and_f2_does_not_reward_it():
    rng = np.random.default_rng(1)
    panel = _panel(_walk(rng, rng.normal(0, 3e-4, N)))
    out = bf.momentum_30(panel, _rows())
    assert out["rebalances"] == 11
    assert out["rank_ic"]["mean"] > 0.3
    assert out["gross_bps_per_rebalance"]["mean"] > 20
    assert out["session_net_bps"] > 0
    assert bf.open_reversal(panel, _rows())["gross_bps_per_rebalance"]["mean"] < 0


def test_f2_finds_planted_opening_reversal():
    rng = np.random.default_rng(2)
    mid = np.full((N, bb.BARS), 50.0)
    move = rng.normal(0, 0.01, N)  # 09:31 -> 09:45
    mid[:, 1:15] = 50.0 * (1 + np.linspace(0, 1, 14)[None, :] * move[:, None])
    after = 50.0 * (1 + move)
    # by 10:15 half the move has reversed, then the price stays
    path = np.linspace(0, 1, 30)[None, :]
    mid[:, 15:45] = after[:, None] * (1 - 0.5 * path * move[:, None])
    mid[:, 45:] = mid[:, 44:45]
    mid *= 1 + rng.normal(0, 1e-5, mid.shape)
    out = bf.open_reversal(_panel(mid), _rows())
    assert out["rebalances"] == 1
    assert out["rank_ic"]["mean"] > 0.8
    assert out["session_net_bps"] > 30


def _auction(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "symbol",
        "kind",
        "ts_ns",
        "paired_shares",
        "imbalance_shares",
        "direction",
        "far",
        "near",
        "reference",
        "cross_type",
        "price",
        "shares",
    ]
    return pd.DataFrame([{c: r.get(c, np.nan) for c in cols} for r in rows], columns=cols)


def _f3_case(rng, n: int = N):
    mid = np.full((n, bb.BARS), 40.0) * (1 + rng.normal(0, 1e-5, (n, bb.BARS)))
    imb = rng.normal(0, 1, n)
    vol = np.full((n, bb.BARS), 1000.0)
    rows = []
    for i in range(n):
        sym = f"S{i:03d}"
        direction = "B" if imb[i] > 0 else "S"
        rows.append(
            dict(
                symbol=sym,
                kind="I",
                ts_ns=bf.F3_DECISION_NS - 10**9,
                paired_shares=1e4,
                imbalance_shares=abs(imb[i]) * 1e4,
                direction=direction,
                cross_type="C",
            )
        )
        # the cross moves 20 bps per unit of signed imbalance from the 15:56 mid
        rows.append(
            dict(
                symbol=sym,
                kind="Q",
                ts_ns=bf.F3_DECISION_NS + 6 * 60 * 10**9,
                cross_type="C",
                price=mid[i, 385] * (1 + 0.002 * imb[i]),
                shares=5e4,
            )
        )
    return _panel(mid, vol=vol), _auction(rows), imb


def test_f3_finds_planted_imbalance_pressure_and_reports_coverage():
    panel, auction, _ = _f3_case(np.random.default_rng(3))
    out = bf.closing_imbalance(panel, _rows(), auction)
    assert out["rank_ic"]["mean"] > 0.9
    assert out["session_net_bps"] > 20
    assert out["coverage"] == 1.0 and out["names_with_score"] == N
    # the continuous-market diagnostic exit sees no move (the mid is flat)
    mid_exit = bf.closing_imbalance(panel, _rows(), auction, exit_at="mid")
    assert abs(mid_exit["gross_bps_per_rebalance"]["mean"]) < 1


def test_f3_score_uses_last_message_by_decision_and_handles_directions():
    mid = np.full((4, bb.BARS), 10.0)
    vol = np.full((4, bb.BARS), 10.0)  # 3,850 shares over bars 0..384
    panel = _panel(mid, vol=vol)
    t = bf.F3_DECISION_NS
    auction = _auction(
        [
            dict(
                symbol="S000",
                kind="I",
                ts_ns=t - 5,
                imbalance_shares=100,
                direction="S",
                cross_type="C",
            ),
            dict(
                symbol="S000",
                kind="I",
                ts_ns=t,
                imbalance_shares=385,
                direction="B",
                cross_type="C",
            ),
            dict(
                symbol="S000",
                kind="I",
                ts_ns=t + 1,
                imbalance_shares=9e9,
                direction="S",
                cross_type="C",
            ),  # after the decision: ignored
            dict(
                symbol="S001",
                kind="I",
                ts_ns=t - 1,
                imbalance_shares=7,
                direction="N",
                cross_type="C",
            ),
            dict(
                symbol="S002",
                kind="I",
                ts_ns=t - 1,
                imbalance_shares=7,
                direction="O",
                cross_type="C",
            ),
        ]
    )
    s = bf.f3_score(panel, _rows(4), auction)
    assert s[0] == pytest.approx(385 / 3850)
    assert s[1] == 0.0
    assert np.isnan(s[2]) and np.isnan(s[3])


def test_no_score_uses_anything_after_its_decision_bar():
    rng = np.random.default_rng(4)
    mid = _walk(rng, rng.normal(0, 3e-4, N))
    base = _panel(mid)
    for k in bb.decision_bars(30, 30, 1, 30):
        spoiled = mid.copy()
        spoiled[:, k + 1 :] = rng.uniform(1, 100, spoiled[:, k + 1 :].shape)
        np.testing.assert_array_equal(
            bf.f1_score(base, _rows(), k), bf.f1_score(_panel(spoiled), _rows(), k)
        )
    spoiled = mid.copy()
    spoiled[:, bf.F2_DECISION + 1 :] = 1.0
    np.testing.assert_array_equal(bf.f2_score(base, _rows()), bf.f2_score(_panel(spoiled), _rows()))
    prev = _panel(mid)
    np.testing.assert_array_equal(
        bf.f2_gap_score(base, _rows(), prev, _rows()),
        bf.f2_gap_score(_panel(spoiled), _rows(), prev, _rows()),
    )
    # F3: volume and quotes after bar 384 and messages after 15:55:05 do not move the score
    panel, auction, _ = _f3_case(np.random.default_rng(5))
    s0 = bf.f3_score(panel, _rows(), auction)
    vol = panel.vol.copy()
    vol[:, bf.F3_VOLUME_LAST + 1 :] = 1e9
    later = pd.concat(
        [
            auction,
            _auction(
                [
                    dict(
                        symbol=f"S{i:03d}",
                        kind="I",
                        ts_ns=bf.F3_DECISION_NS + 1,
                        imbalance_shares=1e9,
                        direction="S",
                        cross_type="C",
                    )
                    for i in range(N)
                ]
            ),
        ],
        ignore_index=True,
    )
    np.testing.assert_array_equal(s0, bf.f3_score(_panel(panel.mid, vol=vol), _rows(), later))


def _ten_names():
    """Ten names: the 09:31 -> 09:45 move rises with the index, so the F2 book
    is long name 0 and short name 9 (one name a side)."""
    mid = np.full((10, bb.BARS), 100.0)
    mid[:, 14] = 100.0 + np.arange(10)
    mid[:, 15] = 100.0
    mid[:, 44] = 100.0
    mid[0, 44], mid[9, 44] = 103.0, 98.0
    hs = np.full((10, bb.BARS), 0.0005)
    hs[0, 15], hs[0, 44], hs[9, 15], hs[9, 44] = 0.001, 0.002, 0.0003, 0.0007
    p = _panel(mid)
    return bb.Panel(p.symbols, p.etp, p.mid, hs, p.vol, p.notional, p.valid_ns)


def test_cost_arithmetic_is_exact():
    out = bf.open_reversal(_ten_names(), _rows(10))
    gross = 0.5 * (0.03 - (-0.02))
    net = gross - 0.5 * ((0.001 + 0.002) + (0.0003 + 0.0007))
    assert out["gross_bps_per_rebalance"]["mean"] == pytest.approx(gross * 1e4, abs=1e-9)
    assert out["session_net_bps"] == pytest.approx(net * 1e4, abs=1e-9)


def test_f3_cost_is_the_entry_half_spread_only():
    n = 10
    mid = np.full((n, bb.BARS), 20.0)
    hs = np.full((n, bb.BARS), 0.0004)
    hs[0, 385], hs[9, 385] = 0.001, 0.0002
    p = _panel(mid)
    panel = bb.Panel(p.symbols, p.etp, p.mid, hs, p.vol, p.notional, p.valid_ns)
    rows = []
    for i in range(n):
        rows.append(
            dict(
                symbol=f"S{i:03d}",
                kind="I",
                ts_ns=bf.F3_DECISION_NS,
                imbalance_shares=float(i),
                direction="S",
                cross_type="C",
            )
        )
        rows.append(dict(symbol=f"S{i:03d}", kind="Q", ts_ns=0, cross_type="C", price=20.0))
    rows[-1]["price"] = 19.9  # short leg (largest sell imbalance is name 9) gains 50 bps
    out = bf.closing_imbalance(panel, _rows(n), _auction(rows))
    gross = 0.5 * (0.0 - (19.9 / 20.0 - 1))
    net = gross - 0.5 * (0.001 + 0.0002)
    assert out["session_net_bps"] == pytest.approx(net * 1e4, abs=1e-9)


def test_overnight_gap_is_undefined_without_the_previous_session():
    rng = np.random.default_rng(6)
    panel = _panel(_walk(rng, np.zeros(N)))
    assert bf.overnight_gap(panel, _rows(), None, None, True) == {
        "family": "overnight_gap",
        "defined": False,
    }
    assert not bf.overnight_gap(panel, _rows(), panel, _rows(), False)["defined"]
    out = bf.overnight_gap(panel, _rows(), panel, _rows(), True)
    assert out["defined"] and out["rebalances"] == 1


def test_previous_weekday_rule():
    assert bf.is_previous_weekday("2019-01-29", "2019-01-30")
    assert bf.is_previous_weekday("2019-02-01", "2019-02-04")  # Friday -> Monday
    assert not bf.is_previous_weekday("2019-01-28", "2019-01-30")
    assert not bf.is_previous_weekday("2019-02-02", "2019-02-04")


def test_gap_score_reads_previous_last_valid_and_today_first_valid():
    prev = np.full((10, bb.BARS), 100.0)
    prev[:, -3:] = np.nan  # last valid mid is bar 386
    prev[:, 386] = 100.0 + np.arange(10)
    today = np.full((10, bb.BARS), np.nan)
    today[:, 6:] = 110.0  # first valid from 09:35 is bar 6
    s = bf.f2_gap_score(_panel(today), _rows(10), _panel(prev), _rows(10))
    g = 110.0 / (100.0 + np.arange(10)) - 1
    np.testing.assert_allclose(s, -(g - g.mean()))
    missing = _rows(10).copy()
    missing[3] = -1
    assert np.isnan(bf.f2_gap_score(_panel(today), _rows(10), _panel(prev), missing)[3])


# --------------------------------------------------------------------------- the run tool


def _tool():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "tools" / "breadth_families_run.py"
    spec = importlib.util.spec_from_file_location("breadth_families_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_bars(path, n: int, seed: int) -> None:
    """A synthetic itchbars CSV (no real data): n names, every bar quoted."""
    rng = np.random.default_rng(seed)
    lines = ["# itchbars/1: synthetic test file", bb_header()]
    mid = 1_000_000 * np.exp(np.cumsum(rng.normal(0, 3e-4, (n, bb.BARS)), axis=1))
    for i in range(n):
        for m in range(bb.BARS):
            bid = int(mid[i, m]) - 100
            lines.append(
                f"N{i:02d},N,{m},{bid},{bid + 200},100,100,200.0000,60000000000,"
                f"50,{50 * (bid + 100)},1,0,0"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bb_header() -> str:
    return (
        "symbol,etp,minute,bid,ask,bid_sz,ask_sz,tw_spread,tw_valid_ns,vol,notional,trades,"
        "hidden_vol,hidden_notional"
    )


def test_run_tool_on_synthetic_sessions(tmp_path):
    import json

    tool = _tool()
    n = 30
    bars = tmp_path / "bars"
    bars.mkdir()
    _write_bars(bars / "bars_20190131.csv", n, 7)
    _write_bars(bars / "bars_20190201.csv", n, 8)
    (bars / "auction_20190201.csv").write_text(
        "# itchbars-auction/1: synthetic\n"
        "symbol,kind,ts_ns,paired_shares,imbalance_shares,direction,far,near,reference,"
        "cross_type,price,shares\n"
        + "".join(
            f"N{i:02d},I,{bf.F3_DECISION_NS - 1},100,{10 * i},{'B' if i % 2 else 'S'},0,0,0,C,,\n"
            f"N{i:02d},Q,{bf.F3_DECISION_NS + 10**11},,,,,,,C,1000000,500\n"
            for i in range(n)
        ),
        encoding="utf-8",
    )
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps({"symbols": [f"N{i:02d}" for i in range(n)] + ["GONE"]}))
    out = tmp_path / "out.json"
    doc = tool.main(["--bars-dir", str(bars), "--universe", str(universe), "--out", str(out)])
    assert set(doc["sessions"]) == {"2019-01-31", "2019-02-01"}
    first, second = doc["sessions"]["2019-01-31"], doc["sessions"]["2019-02-01"]
    assert first["missing"] == 1 and first["names"] == n
    assert not first["overnight_gap_d1"]["defined"]
    assert second["overnight_gap_d1"]["defined"]  # Thursday -> Friday
    assert "closing_imbalance_d1" in second and "closing_imbalance_d1" not in first
    assert "closing_imbalance_d0" not in second and "closing_imbalance_d1_mid_exit" in second
    assert doc["pooled"]["momentum_30_d1"]["sessions"] == 2
    assert doc["pooled"]["closing_imbalance_d1"]["sessions"] == 1
    assert json.loads(out.read_text(encoding="utf-8"))["bonferroni_k"] == 8


def test_run_tool_refuses_holdout_sessions(tmp_path):
    tool = _tool()
    (tmp_path / "bars_20250102.csv").write_text("# x\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="sealed holdout"):
        tool.session_files(tmp_path)


# --------------------------------------------------------------------------- F3 version 2


def test_f3_decision_instant_is_15_55_05_exactly():
    assert bf.F3_DECISION_NS == 57_305 * 10**9
    assert (bf.F3_VOLUME_LAST, bf.F3_ENTRY, bf.LAST_BAR) == (384, 385, 389)


def test_f3_message_boundary_at_15_55_05():
    panel = _panel(np.full((2, bb.BARS), 10.0))
    auction = _auction(
        [
            dict(
                symbol="S000",
                kind="I",
                ts_ns=57_305 * 10**9,
                imbalance_shares=50,
                direction="B",
                cross_type="C",
            ),
            dict(
                symbol="S001",
                kind="I",
                ts_ns=57_305 * 10**9 + 1,
                imbalance_shares=50,
                direction="B",
                cross_type="C",
            ),
        ]
    )
    s = bf.f3_score(panel, _rows(2), auction)
    assert np.isfinite(s[0]) and np.isnan(s[1])


def test_f3_never_enters_at_the_bar_384_quote_and_has_no_delay_argument():
    panel, auction, _ = _f3_case(np.random.default_rng(9))
    base = bf.closing_imbalance(panel, _rows(), auction)
    mid, hs = panel.mid.copy(), panel.half_spread.copy()
    mid[:, 384] = 1.0
    hs[:, 384] = 0.5
    spoiled = bb.Panel(panel.symbols, panel.etp, mid, hs, panel.vol, panel.notional, panel.valid_ns)
    again = bf.closing_imbalance(spoiled, _rows(), auction)
    assert again["session_net_bps"] == base["session_net_bps"]
    assert base["params"]["entry"] == 385 and base["params"]["version"] == 2
    with pytest.raises(TypeError):
        bf.closing_imbalance(panel, _rows(), auction, 0)


# Outputs of F1 and F2 as committed before F3 version 2 (commit 8a7544c5), on
# a seeded synthetic panel: version 2 must not change them.
_F1F2_REFERENCE = {
    "momentum_30_d1": (-9.720400417610273, -2.4731193162438023, -0.07328593306944363),
    "open_reversal_d1": (0.014219300160258188, 6.706398543784631, 0.08763287529167747),
    "momentum_30_d0": (-7.130207157159415, -0.4843157355119471, -0.04996040551956537),
    "open_reversal_d0": (-0.9537790997546672, 6.432780153687756, 0.04211141530037836),
}


def test_f1_and_f2_outputs_are_unchanged_by_f3_version_2():
    rng = np.random.default_rng(11)
    n = 60
    mid = 50 * np.exp(np.cumsum(rng.normal(0, 4e-4, (n, bb.BARS)), axis=1))
    mid[rng.random(mid.shape) < 0.02] = np.nan
    hs = np.where(np.isfinite(mid), rng.uniform(1e-4, 6e-4, mid.shape), np.nan)
    v = np.full(mid.shape, 100.0)
    panel = bb.Panel(
        tuple(f"S{i:03d}" for i in range(n)),
        np.zeros(n, bool),
        mid,
        hs,
        v,
        v * np.nan_to_num(mid),
        np.where(np.isfinite(mid), 6e10, 0.0),
    )
    for d in (1, 0):
        for name, fn in (("momentum_30", bf.momentum_30), ("open_reversal", bf.open_reversal)):
            o = fn(panel, _rows(n), d)
            got = (o["session_net_bps"], o["gross_bps_per_rebalance"]["mean"], o["rank_ic"]["mean"])
            assert got == _F1F2_REFERENCE[f"{name}_d{d}"], name
