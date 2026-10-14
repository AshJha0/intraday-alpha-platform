"""v1.16 AI4 part 2: streaming train / in-sample evaluation on a LOB cache."""

from __future__ import annotations

import json

import numpy as np
import pytest
from iap.models.deeplob import torch_available
from iap.models.lob_train import _combine, net_of_cost_mid, train_eval


def _cache(tmp_path, sessions=("ds3_20190130", "ds3_20190327", "r7_20210713"), n=1500):
    rng = np.random.default_rng(1)
    tmp_path.mkdir(parents=True, exist_ok=True)
    for s in sessions:
        for sym in ("AAPL", "QQQ"):
            mid = 100 + np.cumsum(rng.normal(scale=0.01, size=n))
            x = rng.normal(size=(n, 8)).astype(np.float32)
            x[:, 0], x[:, 2] = 0.5, -0.5  # 1-tick spread
            np.save(tmp_path / f"{s}_{sym}.X.npy", x.astype(np.float16))
            np.savez(tmp_path / f"{s}_{sym}.meta.npz", ts=np.arange(n), mid=mid,
                     n_empty=np.zeros(n, np.int16), tick=np.float64(0.01))  # fmt: skip
    return tmp_path


def test_net_of_cost_mid_hand_calc():
    mid = np.array([100.0, 100.0, 101.0, 101.0, 100.0])
    spread = np.full(5, 0.02)
    r = net_of_cost_mid(
        mid, spread, np.array([0, 1, 2]), np.array([1, 1, -1]), horizon=2, fee_bp=0.5
    )
    # trades at t=0 (exit t=2) and t=2 (exit t=4); t=1 overlaps and is skipped
    g0, g1 = 1e4 * (101 / 100 - 1), -1e4 * (100 / 101 - 1)
    c0 = 0.5 * (0.02 / 100 + 0.02 / 101) * 1e4 + 0.5
    c1 = 0.5 * (0.02 / 101 + 0.02 / 100) * 1e4 + 0.5
    assert r["n_trades"] == 2
    assert r["mean_net_bp"] == pytest.approx(((g0 - c0) + (g1 - c1)) / 2)


def test_combine_pools_trades():
    a = {
        "n_trades": 2,
        "n_signals": 3,
        "mean_net_bp": 1.0,
        "std_net_bp": 0.0,
        "gross_bp": 2,
        "cost_bp": 1,
    }
    b = {
        "n_trades": 2,
        "n_signals": 2,
        "mean_net_bp": 3.0,
        "std_net_bp": 0.0,
        "gross_bp": 4,
        "cost_bp": 1,
    }
    c = _combine([a, b])
    assert c["n_trades"] == 4 and c["mean_net_bp"] == pytest.approx(2.0)
    assert c["std_net_bp"] == pytest.approx(np.sqrt(4 / 3))


def test_train_eval_baselines_end_to_end(tmp_path):
    cache = _cache(tmp_path / "c")
    out = tmp_path / "out"
    doc = train_eval(cache, out, models=("linear", "gbm"), window=10, horizon=5, stride=5,
                     max_train=500, baseline_train=300, min_trades=1)  # fmt: skip
    # default validation = the last two sessions by date
    assert doc["val_units"] == [
        "ds3_20190327_AAPL.meta.npz",
        "ds3_20190327_QQQ.meta.npz",
        "r7_20210713_AAPL.meta.npz",
        "r7_20210713_QQQ.meta.npz",
    ]
    assert set(doc["results"]["by_symbol"]["linear"]) == {"AAPL", "QQQ"}
    assert json.loads((out / "ai4_insample.json").read_text())["status"].startswith("IN-SAMPLE")


def test_holdout_units_refused(tmp_path):
    cache = _cache(tmp_path, sessions=("ds3_20190130", "r7_20251128"))
    with pytest.raises(ValueError, match="holdout"):
        train_eval(cache, tmp_path / "o", models=("linear",), window=10, horizon=5)


@pytest.mark.skipif(not torch_available(), reason="needs the optional 'deep' extra (torch)")
def test_deeplob_streaming_fit_runs(tmp_path):
    cache = _cache(tmp_path / "c", n=800)
    doc = train_eval(cache, tmp_path / "o", models=("deeplob", "linear", "gbm"), window=20, horizon=5,
                     stride=10, max_train=200, baseline_train=200, epochs=1, batch=32, min_trades=1)  # fmt: skip
    assert "deeplob" in doc["results"]["pooled"]
    assert set(doc["acceptance_in_sample"]) == {"AAPL", "QQQ"}
    assert (tmp_path / "o" / "deeplob.pt").is_file()
