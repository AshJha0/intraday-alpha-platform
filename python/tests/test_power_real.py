"""iap.research.power_real: label planting and the shifted background."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.alpha import configure_universe, universe_ids
from iap.research import power_real
from iap.research.power_stats import DAY_NS

DAY0 = 18_000  # a UTC day index


def _frames(n_per_day: int = 4000, days: int = 2, seed: int = 7) -> dict[int, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    out = {}
    for iid in (1, 2):
        ts = np.concatenate(
            [
                (DAY0 + d) * DAY_NS + 14 * 3600 * 10**9 + np.arange(n_per_day) * 10**8
                for d in range(days)
            ]
        )
        n = len(ts)
        df = pd.DataFrame({"instrument_id": iid, "exchange_ts": ts})
        df["trade_imbalance_w10s_v1"] = rng.standard_normal(n)
        df["ref_ret_1s_v1"] = rng.standard_normal(n)
        for h in ("1s", "5s"):
            y = rng.standard_t(4, n) * 1e-4  # fat-tailed, independent of the signals
            df[f"label_mid_{h}"] = y
            df[f"label_cost_{h}"] = y - 1e-5
            df[f"label_valid_{h}"] = True
            df[f"label_reason_{h}"] = 0
            df[f"label_reopen_{h}"] = np.nan
        out[iid] = df
    return out


@pytest.fixture(autouse=True)
def _default_universe():
    configure_universe(None)
    yield
    configure_universe(None)


def _corr(df: pd.DataFrame, xcol: str, h: str) -> float:
    return float(np.corrcoef(df[xcol], df[f"label_mid_{h}"])[0, 1])


def test_level_zero_is_identity():
    frames = _frames()
    out = power_real.plant(frames, power_real.detectors(), 0.0)
    for iid in frames:
        pd.testing.assert_frame_equal(out[iid], frames[iid])


def test_planted_ic_matches_level_on_independent_labels():
    frames = _frames(n_per_day=20000)
    dets = power_real.detectors()
    out = power_real.plant(frames, dets, 0.1)
    by_alpha = {d["alpha_id"]: d for d in dets}
    eq04, eq10 = by_alpha["EQ04"], by_alpha["EQ10"]
    # EQ10 trades constituents only, so the ETF-free default universe plants both ids
    for iid in (1, 2):
        assert _corr(out[iid], "trade_imbalance_w10s_v1", eq04["horizon"]) == pytest.approx(
            0.1, abs=0.02
        )
        assert _corr(out[iid], "ref_ret_1s_v1", eq10["horizon"]) == pytest.approx(0.1, abs=0.02)
        # cost label moved by the same amount as the mid label
        h = eq04["horizon"]
        diff = out[iid][f"label_mid_{h}"] - out[iid][f"label_cost_{h}"]
        assert np.allclose(diff, 1e-5)


def test_break_reverses_sign_from_the_break_day():
    frames = _frames(n_per_day=20000)
    dets = [d for d in power_real.detectors() if d["alpha_id"] == "EQ04"]
    out = power_real.plant(frames, dets, 0.2, break_from_day=DAY0 + 1)
    h = dets[0]["horizon"]
    df = out[1]
    day = df["exchange_ts"] // DAY_NS
    first, second = df[day == DAY0], df[day == DAY0 + 1]
    assert _corr(first, "trade_imbalance_w10s_v1", h) > 0.15
    assert _corr(second, "trade_imbalance_w10s_v1", h) < -0.15


def test_shift_keeps_each_session_multiset_and_is_seeded():
    frames = _frames()
    a = power_real.shift_labels(frames, 11)
    b = power_real.shift_labels(frames, 11)
    c = power_real.shift_labels(frames, 12)
    for iid in frames:
        pd.testing.assert_frame_equal(a[iid], b[iid])
        day = frames[iid]["exchange_ts"] // DAY_NS
        for d in day.unique():
            m = day == d
            assert sorted(a[iid].loc[m, "label_mid_5s"]) == sorted(
                frames[iid].loc[m, "label_mid_5s"]
            )
        # whole label rows travel together
        assert np.allclose(a[iid]["label_mid_5s"] - a[iid]["label_cost_5s"], 1e-5)
        # features are untouched
        pd.testing.assert_series_equal(
            a[iid]["trade_imbalance_w10s_v1"], frames[iid]["trade_imbalance_w10s_v1"]
        )
        assert not a[iid]["label_mid_5s"].equals(c[iid]["label_mid_5s"])


def test_shift_destroys_a_real_relationship():
    frames = _frames(n_per_day=20000)
    for df in frames.values():
        df["label_mid_5s"] = 1e-4 * df["trade_imbalance_w10s_v1"]
    shifted = power_real.shift_labels(frames, 3)
    assert abs(_corr(shifted[1], "trade_imbalance_w10s_v1", "5s")) < 0.03


def test_configure_universe_reads_the_etf(tmp_path):
    cfg = tmp_path / "instruments.json"
    cfg.write_text(
        '{"instruments": ['
        '{"instrument_id": 1, "asset_class": "EQUITY"},'
        '{"instrument_id": 2, "asset_class": "EQUITY"},'
        '{"instrument_id": 3, "asset_class": "ETF"}]}'
    )
    configure_universe(cfg)
    assert universe_ids("etf") == (3,)
    assert universe_ids("constituents") == (1, 2)
    assert universe_ids("equity") == (1, 2, 3)
    assert universe_ids("fx") == ()
    configure_universe(None)
    assert universe_ids("etf") == (11,)


def test_bad_level_is_refused():
    with pytest.raises(Exception, match="level"):
        power_real.plant(_frames(n_per_day=10), power_real.detectors(), 1.0)


def _stub_study(monkeypatch, tmp_path, calls):
    """A tiny study whose validation chain is a counting stub."""
    ds = tmp_path / "ds"
    ds.mkdir(exist_ok=True)
    (ds / "dataset.json").write_text('{"dataset_version": "v1"}', encoding="ascii")
    frames = _frames(n_per_day=50, days=2)
    monkeypatch.setattr(power_real, "configure_universe", lambda *_: None)
    monkeypatch.setattr(power_real, "load_features", lambda *_a, **_k: frames)

    def stub(fr, configs_dir, t, dets, seed=0, **_):
        calls.append(seed)
        return {d["id"]: {"t_pooled": 1.0 + seed % 7} for d in dets}

    monkeypatch.setattr(power_real, "evaluate_run", stub)
    monkeypatch.setattr(power_real, "summarise", lambda runs, thresholds: {"n": len(runs)})
    return ds


def _study(ds, checkpoint, **kw):
    return power_real.run_real_power_study(
        ds, levels=(0.0, 0.02), break_levels=(0.02,), n_seeds=2, gate_looks=100,
        checkpoint=checkpoint, **kw,
    )  # fmt: skip


def test_checkpoint_resume_reproduces_the_uninterrupted_study(monkeypatch, tmp_path):
    calls: list[int] = []
    ds = _stub_study(monkeypatch, tmp_path, calls)
    ck = tmp_path / "ck.jsonl"
    full = _study(ds, ck)
    n_runs = len(calls)
    assert n_runs == len(full["runs"]) > 0

    # simulate a kill after 3 runs, mid-write of the 4th
    lines = ck.read_text(encoding="utf-8").splitlines()
    ck.write_text("\n".join(lines[:4]) + "\n" + lines[4][:20], encoding="utf-8")
    calls.clear()
    resumed = _study(ds, ck)
    assert len(calls) == n_runs - 3
    assert resumed == full

    calls.clear()
    assert _study(ds, ck) == full and calls == []  # complete checkpoint: nothing recomputed


def test_checkpoint_from_another_study_is_refused_unless_restart(monkeypatch, tmp_path):
    calls: list[int] = []
    ds = _stub_study(monkeypatch, tmp_path, calls)
    ck = tmp_path / "ck.jsonl"
    _study(ds, ck)
    with pytest.raises(power_real.ResearchError, match="different study"):
        power_real.run_real_power_study(
            ds, levels=(0.0, 0.04), n_seeds=2, gate_looks=100, checkpoint=ck
        )
    calls.clear()
    _study(ds, ck, restart=True)
    assert calls  # recomputed from scratch
