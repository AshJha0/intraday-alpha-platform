"""Golden reproduction for the 6 representative alphas + the EQ01 golden
backtest (conventions §5; tests/golden/expected_alpha.json,
expected_backtest.json).

Every golden value is validated two ways:

1. **brute force** — expected_return/confidence recomputed in this file
   from the raw formulas (feature columns / embedded inputs + the params
   embedded in the golden JSON), never through the AlphaModel classes;
2. **the classes** — model.score() on the reconstructed frames.

Both must match the pinned file at 1e-9 abs/rel (counts exact).
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from iap.alpha import load_params_file
from iap.alpha.data import (
    asof_to_grid,
    load_features,
    make_grid,
    session_days,
    split_by_day,
)
from iap.alpha.fx_exposure import FX05CrossPairRelativeValue, free_exposure_matrix
from iap.alpha.goldenframes import build_golden_frame
from iap.backtest import Backtester, BacktestConfig, CostModel

from conftest import CONFIGS_DIR, GOLDEN_DIR, REPO_ROOT

EPS = 1e-12
TOL = 1e-9
FEATURES_DIR = REPO_ROOT / "data" / "features"
PARAMS_PATH = CONFIGS_DIR / "strategies" / "alpha_params.json"

FRAME_ALPHAS = ("EQ01", "EQ03", "EQ06", "FX01", "FX09")


@pytest.fixture(scope="module")
def golden():
    return json.loads((GOLDEN_DIR / "expected_alpha.json").read_text())


@pytest.fixture(scope="module")
def golden_bt():
    return json.loads((GOLDEN_DIR / "expected_backtest.json").read_text())


@pytest.fixture(scope="module")
def models():
    assert PARAMS_PATH.exists(), (
        f"{PARAMS_PATH} missing — run research/alpha_reports/run_all.py"
    )
    return load_params_file(PARAMS_PATH)


@pytest.fixture(scope="module")
def eq_frame():
    return build_golden_frame(GOLDEN_DIR / "events_eq_mbo.jsonl", CONFIGS_DIR, 1)


@pytest.fixture(scope="module")
def fx_frame():
    return build_golden_frame(GOLDEN_DIR / "events_fx_quote.jsonl", CONFIGS_DIR, 101)


def _close(got, want, tol=TOL):
    assert math.isfinite(got) and math.isfinite(want)
    assert abs(got - want) <= tol + tol * abs(want), f"{got} != {want}"


def _bf_raw(alpha_id: str, inputs: dict) -> float:
    """Brute-force raw signal from the embedded golden inputs."""
    g = {k: (float("nan") if v is None else float(v)) for k, v in inputs.items()}
    if alpha_id in ("EQ01", "FX01"):
        return g["micro_mid_dev_bps_v1"]
    if alpha_id == "EQ03":
        return (
            0.5 * g["ofi_norm_l1_w1s_v1"]
            + 0.3 * g["ofi_norm_l5_w1s_v1"]
            + 0.2 * g["ofi_norm_l5_w5s_v1"]
        )
    if alpha_id == "EQ06":
        return g["ret_vol_adj_10s_v1"]
    if alpha_id == "FX09":
        return -g["ret_vol_adj_10s_v1"] * g["vol_regime_ratio_v1"]
    raise ValueError(alpha_id)


def _bf_score(raw: float, p: dict):
    if not math.isfinite(raw):
        return 0.0, 0.0
    z = (raw - p["mu"]) / (p["sigma"] + EPS)
    z = max(-p["z_clip"], min(p["z_clip"], z))
    return p["beta"] * z, min(1.0, abs(z) / p["conf_scale"])


def _bf_pearson(x, y):
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    xd, yd = x - x.mean(), y - y.mean()
    return float(np.sum(xd * yd) / math.sqrt(np.sum(xd * xd) * np.sum(yd * yd)))


def test_golden_file_shape(golden):
    assert set(golden["alphas"]) == {"EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09"}
    for aid, blob in golden["alphas"].items():
        assert len(blob["cases"]) == 5, aid
        assert "ic" in blob["ic_window"], aid
        assert aid in golden["params"]


@pytest.mark.parametrize("aid", FRAME_ALPHAS)
def test_golden_cases_brute_force_from_embedded_inputs(aid, golden):
    """File values reproduce from embedded inputs + params alone."""
    p = golden["params"][aid]
    for case in golden["alphas"][aid]["cases"]:
        er, conf = _bf_score(_bf_raw(aid, case["inputs"]), p)
        _close(er, case["expected_return"])
        _close(conf, case["confidence"])


@pytest.mark.parametrize("aid", FRAME_ALPHAS)
def test_golden_cases_via_alpha_classes(aid, golden, models, eq_frame, fx_frame):
    """The AlphaModel classes reproduce the file on the rebuilt frames,
    and the frames agree with the embedded inputs."""
    blob = golden["alphas"][aid]
    frame = eq_frame if aid.startswith("EQ") else fx_frame
    iid = blob["instrument_id"]
    scores = models[aid].score({iid: frame})[iid]
    for case in blob["cases"]:
        r = case["event_index_1based"] - 1
        assert int(frame["exchange_ts"].iloc[r]) == case["exchange_ts"]
        for name, want in case["inputs"].items():
            got = float(frame[name].iloc[r])
            if want is None:
                assert not math.isfinite(got)
            else:
                _close(got, want)
        _close(float(scores["expected_return"].iloc[r]), case["expected_return"])
        _close(float(scores["confidence"].iloc[r]), case["confidence"])


@pytest.mark.parametrize("aid", FRAME_ALPHAS)
def test_golden_ic_windows(aid, golden, models, eq_frame, fx_frame):
    blob = golden["alphas"][aid]
    frame = eq_frame if aid.startswith("EQ") else fx_frame
    iid = blob["instrument_id"]
    lo, hi = blob["ic_window"]["rows_0based"]
    h = blob["ic_window"]["horizon"]
    scores = models[aid].score({iid: frame})[iid]
    er = scores["expected_return"].to_numpy(dtype=float)[lo:hi].copy()
    er[scores["confidence"].to_numpy(dtype=float)[lo:hi] <= 0.0] = np.nan
    lab = frame[f"label_mid_{h}"].to_numpy(dtype=float)[lo:hi].copy()
    lab[~frame[f"label_valid_{h}"].to_numpy(dtype=bool)[lo:hi]] = np.nan
    _close(_bf_pearson(er, lab), blob["ic_window"]["ic"])


def test_golden_fx05_brute_force_from_embedded_inputs(golden):
    """FX05: residual recomputed with an independent least-squares solve
    (normal equations via numpy pinv on the embedded 8-pair inputs)."""
    blob = golden["alphas"]["FX05"]
    p = golden["params"]["FX05"]
    for case in blob["cases"]:
        pair_ids = sorted(int(k) for k in case["inputs"])
        r = np.array(
            [
                float("nan") if case["inputs"][str(k)] is None
                else float(case["inputs"][str(k)])
                for k in pair_ids
            ]
        )
        a = free_exposure_matrix(pair_ids)
        ok = np.isfinite(r)
        f = np.linalg.pinv(a[ok]) @ r[ok]
        fitted = a @ f
        tgt = pair_ids.index(case["target_pair"])
        raw = -(r[tgt] - fitted[tgt])
        _close(raw, case["raw_residual_signal"], tol=1e-9)
        er, conf = _bf_score(float(raw), p)
        _close(er, case["expected_return"])
        _close(conf, case["confidence"])


def test_golden_fx05_via_class_on_bundled_data(golden, models):
    if not FEATURES_DIR.is_dir() or not list(FEATURES_DIR.glob("features_10*.parquet")):
        pytest.skip("data/features missing — regenerate via python3 -m iap.features")
    blob = golden["alphas"]["FX05"]
    frames = load_features(FEATURES_DIR, instrument_ids=list(range(101, 109)))
    days = session_days(frames)
    _, day2 = split_by_day(frames, days[1])
    model = models["FX05"]
    scores = model.score(day2)
    tgt = blob["ic_window"]["target_pair"]
    tdf = day2[tgt]
    grid = make_grid(day2, FX05CrossPairRelativeValue.GRID_STEP_NS)
    for case in blob["cases"]:
        r = case["native_row_0based"]
        assert int(tdf["exchange_ts"].iloc[r]) == case["native_exchange_ts"]
        _close(float(scores[tgt]["expected_return"].iloc[r]), case["expected_return"])
        _close(float(scores[tgt]["confidence"].iloc[r]), case["confidence"])
        # the pinned grid point really is the latest one at-or-before the row
        gi = int(np.searchsorted(grid, case["native_exchange_ts"], side="right")) - 1
        assert int(grid[gi]) == case["grid_ts"]
        # and the embedded inputs match an independent grid sampling
        sampled = asof_to_grid(
            tdf["exchange_ts"].to_numpy(),
            tdf["ret_log_1m_v1"].to_numpy(dtype=float),
            grid[gi : gi + 1],
            FX05CrossPairRelativeValue.MAX_AGE_NS,
        )[0]
        want = case["inputs"][str(tgt)]
        if want is None:
            assert not math.isfinite(sampled)
        else:
            _close(float(sampled), want)
    # pinned IC window
    lo, hi = blob["ic_window"]["grid_window_0based"]
    h = blob["ic_window"]["horizon"]
    tts = tdf["exchange_ts"].to_numpy()
    sel = (tts >= int(grid[lo])) & (tts < int(grid[hi]))
    er = scores[tgt]["expected_return"].to_numpy(dtype=float)[sel].copy()
    er[scores[tgt]["confidence"].to_numpy(dtype=float)[sel] <= 0.0] = np.nan
    lab = tdf[f"label_mid_{h}"].to_numpy(dtype=float)[sel].copy()
    lab[~tdf[f"label_valid_{h}"].to_numpy(dtype=bool)[sel]] = np.nan
    _close(_bf_pearson(er, lab), blob["ic_window"]["ic"])


def test_golden_params_match_serialized_params(golden, models):
    """expected_alpha.json embeds the exact configs/strategies params."""
    for aid, p in golden["params"].items():
        live = models[aid].params()
        for key in ("mu", "sigma", "beta", "beta_fit"):
            _close(float(live[key]), float(p[key]), tol=0.0)
        assert live["horizon"] == p["horizon"]


def test_golden_backtest_eq01(golden_bt, models, eq_frame):
    cfg = golden_bt["config"]
    inst = json.loads((CONFIGS_DIR / "instruments.json").read_text())["instruments"]
    meta = {
        int(r["instrument_id"]): {
            "tick_size": float(r["tick_size"]),
            "lot_size": int(r["lot_size"]),
            "adv": float(r["adv"]),
            "asset_class": r["asset_class"],
            "ref_price": float(r["ref_price"]),
        }
        for r in inst
    }
    cm = CostModel.load(
        CONFIGS_DIR / "execution.json", multiplier=cfg["cost_multiplier"]
    )
    bt = Backtester(
        cm,
        meta,
        BacktestConfig(
            max_pos_qty=cfg["max_pos_qty"],
            conf_min=cfg["conf_min"],
            latency_rows=cfg["latency_rows"],
        ),
    )
    scores = models["EQ01"].score({1: eq_frame})
    res = bt.run({1: eq_frame}, scores, "EQUITY").per_instrument[1]
    assert res.trade_count == golden_bt["trade_count"]      # exact
    assert res.traded_qty == golden_bt["traded_qty"]        # exact
    assert res.n_rows == golden_bt["n_rows"]                # exact
    _close(res.total_pnl, golden_bt["total_pnl"])
    _close(res.total_costs, golden_bt["total_costs"])
    _close(res.gross_pnl, golden_bt["gross_pnl"])
    # accounting identity on the golden run itself
    _close(res.total_pnl, res.gross_pnl - res.total_costs)
