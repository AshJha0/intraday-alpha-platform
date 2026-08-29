#!/usr/bin/env python3
"""(Re)generate tests/golden/expected_alpha.json + expected_backtest.json.

Run from python/ with PYTHONPATH=src:

    PYTHONPATH=src python3 tools/make_golden_alpha.py

Golden alphas (6 representative): EQ01, EQ03, EQ06 (equities, golden vector
events_eq_mbo.jsonl / instrument 1), FX01, FX09 (FX, events_fx_quote.jsonl
/ instrument 101), FX05 (cross-pair — the golden vector carries a single
pair, so its cases come from a pinned window of the deterministic bundled
day-2 feature data with ALL inputs embedded in the JSON; deviation
documented in /API_ALPHA.md).

For each golden alpha the file pins, using the day-1-fitted parameters from
configs/strategies/alpha_params.json (regenerate via
research/alpha_reports/run_all.py first):

- score values (expected_return, confidence) at 5 pinned rows, with every
  input feature value embedded so ports can validate scoring math without
  a feature engine;
- the IC over a pinned row window at the alpha's pinned horizon (tol 1e-9).

expected_backtest.json pins the EQ01 research backtest on the golden EQ
frame (pinned config): total P&L and cost total at 1e-9, trade count /
traded qty exact.

BEFORE writing, every score is re-derived by an independent brute-force
recomputation (straight formulas on the frame columns, no AlphaModel code)
and compared at 1e-12 — the file is only written when all agree.  Golden
values are pinned: regenerate only on a deliberate versioned change
(schemas/MIGRATIONS.md).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iap.alpha import build, load_params_file  # noqa: E402
from iap.alpha.data import asof_to_grid, load_features, make_grid, split_by_day, session_days  # noqa: E402
from iap.alpha.fx_exposure import FX05CrossPairRelativeValue, solve_factor_returns  # noqa: E402
from iap.alpha.goldenframes import build_golden_frame  # noqa: E402
from iap.backtest import Backtester, BacktestConfig, CostModel  # noqa: E402
from iap.validation.metrics import ic  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden"
CONFIGS = REPO / "configs"
PARAMS_PATH = CONFIGS / "strategies" / "alpha_params.json"

EPS = 1e-12
#: pinned 1-based event indices (= row index + 1) for the golden cases
EQ_ROWS = (500, 800, 1200, 1600, 2000)
FX_ROWS = (160, 320, 480, 640, 800)
#: pinned IC windows [start_row, end_row) (0-based, half-open)
EQ_IC_WINDOW = (100, 1900)
FX_IC_WINDOW = (100, 760)
#: FX05: pinned 0-based native rows of the target pair's day-2 frame; the
#: mapped 30s grid point + all pair inputs there are embedded per case
FX05_NATIVE_ROWS = (300, 800, 1400, 2000, 2600)
FX05_TARGET_PAIR = 102
FX05_IC_GRID_WINDOW = (100, 2500)

#: pinned golden-backtest config (conf_min lower than the research default
#: so the golden vector's tamer microprice deviations still produce trades)
BT_CONFIG = {"max_pos_qty": 1000, "conf_min": 0.2, "latency_rows": 1,
             "cost_multiplier": 1.0}


def brute_force_z(raw: float, p: dict) -> tuple:
    """(expected_return, confidence) from a raw signal + linear_z_v1 params."""
    if raw is None or not math.isfinite(raw):
        return 0.0, 0.0
    z = (raw - p["mu"]) / (p["sigma"] + EPS)
    z = max(-p["z_clip"], min(p["z_clip"], z))
    return p["beta"] * z, min(1.0, abs(z) / p["conf_scale"])


def brute_force_raw(alpha_id: str, row) -> float:
    """Independent per-row raw-signal recomputation (single-frame alphas)."""
    def g(name):
        v = float(row[name])
        return v
    if alpha_id == "EQ01":
        return g("micro_mid_dev_bps_v1")
    if alpha_id == "EQ03":
        return (0.5 * g("ofi_norm_l1_w1s_v1") + 0.3 * g("ofi_norm_l5_w1s_v1")
                + 0.2 * g("ofi_norm_l5_w5s_v1"))
    if alpha_id == "EQ06":
        return g("ret_vol_adj_10s_v1")
    if alpha_id == "FX01":
        return g("micro_mid_dev_bps_v1")
    if alpha_id == "FX09":
        return -g("ret_vol_adj_10s_v1") * g("vol_regime_ratio_v1")
    raise ValueError(alpha_id)


#: pinned golden-IC horizons: the golden FX vector's event spacing (~39s)
#: makes sub-second labels identically zero, so FX parity ICs pin to 1m —
#: a numeric parity check, not a research claim (research ICs live in
#: research/alpha_reports/)
GOLDEN_IC_HORIZON = {"FX01": "1m", "FX09": "1m"}


def frame_cases(alpha_id: str, frame, model, params: dict, rows, ic_window):
    """Golden cases + pinned IC for a single-frame alpha."""
    iid = int(frame["instrument_id"].iloc[0])
    scores = model.score({iid: frame})[iid]
    p = params[alpha_id]
    cases = []
    for ev_index in rows:
        r = ev_index - 1
        row = frame.iloc[r]
        raw = brute_force_raw(alpha_id, row)
        er_bf, conf_bf = brute_force_z(raw, p)
        er = float(scores["expected_return"].iloc[r])
        conf = float(scores["confidence"].iloc[r])
        if not (abs(er - er_bf) <= 1e-12 and abs(conf - conf_bf) <= 1e-12):
            raise SystemExit(
                f"{alpha_id} row {r}: class ({er}, {conf}) != brute force "
                f"({er_bf}, {conf_bf})"
            )
        cases.append({
            "event_index_1based": ev_index,
            "exchange_ts": int(frame["exchange_ts"].iloc[r]),
            "inputs": {
                name: (float(row[name]) if math.isfinite(float(row[name]))
                       else None)
                for name in model.features
            },
            "expected_return": er,
            "confidence": conf,
        })
    lo, hi = ic_window
    h = GOLDEN_IC_HORIZON.get(alpha_id, model.horizon)
    er = scores["expected_return"].to_numpy(dtype=float)[lo:hi].copy()
    er[scores["confidence"].to_numpy(dtype=float)[lo:hi] <= 0.0] = np.nan
    lab = frame[f"label_mid_{h}"].to_numpy(dtype=float)[lo:hi].copy()
    lab[~frame[f"label_valid_{h}"].to_numpy(dtype=bool)[lo:hi]] = np.nan
    window_ic = ic(er, lab)
    if not np.isfinite(window_ic):
        raise SystemExit(f"{alpha_id}: pinned IC window degenerate")
    n_active = int(np.sum([c["confidence"] > 0 for c in cases]))
    if n_active < 4:
        raise SystemExit(f"{alpha_id}: only {n_active}/5 golden cases active")
    return cases, {"rows_0based": [lo, hi], "horizon": h,
                   "ic": float(window_ic)}


def fx05_cases(params: dict, models):
    """FX05 golden cases from the bundled day-2 features (inputs embedded)."""
    model = models["FX05"]
    frames = load_features(REPO / "data" / "features",
                           instrument_ids=list(range(101, 109)))
    days = session_days(frames)
    _, day2 = split_by_day(frames, days[1])
    pair_ids = sorted(day2)
    grid = make_grid(day2, FX05CrossPairRelativeValue.GRID_STEP_NS)
    mat = np.vstack([
        asof_to_grid(day2[i]["exchange_ts"].to_numpy(),
                     day2[i]["ret_log_1m_v1"].to_numpy(dtype=float), grid,
                     FX05CrossPairRelativeValue.MAX_AGE_NS)
        for i in pair_ids
    ])
    scores = model.score(day2)
    p = params["FX05"]
    tgt = pair_ids.index(FX05_TARGET_PAIR)
    tdf = day2[FX05_TARGET_PAIR]
    tts = tdf["exchange_ts"].to_numpy()
    cases = []
    for rrow in FX05_NATIVE_ROWS:
        if rrow >= len(tts):
            raise SystemExit(f"FX05 native row {rrow} out of range")
        gi = int(np.searchsorted(grid, tts[rrow], side="right")) - 1
        if gi < 0:
            raise SystemExit(f"FX05 native row {rrow}: before first grid point")
        r_vec = mat[:, gi]
        ok = np.isfinite(r_vec)
        if ok.sum() < 2 or not np.isfinite(r_vec[tgt]):
            raise SystemExit(f"FX05 grid index {gi}: cross-section degenerate")
        _, fitted = solve_factor_returns(pair_ids, r_vec)
        raw = -(r_vec[tgt] - fitted[tgt])
        er_bf, conf_bf = brute_force_z(float(raw), p)
        er = float(scores[FX05_TARGET_PAIR]["expected_return"].iloc[rrow])
        conf = float(scores[FX05_TARGET_PAIR]["confidence"].iloc[rrow])
        if not (abs(er - er_bf) <= 1e-12 and abs(conf - conf_bf) <= 1e-12):
            raise SystemExit(
                f"FX05 grid {gi}: class ({er}, {conf}) != brute force "
                f"({er_bf}, {conf_bf})"
            )
        cases.append({
            "grid_index_0based": gi,
            "grid_ts": int(grid[gi]),
            "native_row_0based": rrow,
            "native_exchange_ts": int(tts[rrow]),
            "target_pair": FX05_TARGET_PAIR,
            "inputs": {
                str(pid): (float(v) if math.isfinite(v) else None)
                for pid, v in zip(pair_ids, r_vec)
            },
            "raw_residual_signal": float(raw),
            "expected_return": er,
            "confidence": conf,
        })
    # pinned IC over a grid-window of target-pair native rows
    lo, hi = FX05_IC_GRID_WINDOW
    m_lo, m_hi = int(grid[lo]), int(grid[hi])
    sel = (tts >= m_lo) & (tts < m_hi)
    h = model.horizon
    er = scores[FX05_TARGET_PAIR]["expected_return"].to_numpy(dtype=float)[sel].copy()
    er[scores[FX05_TARGET_PAIR]["confidence"].to_numpy(dtype=float)[sel] <= 0.0] = np.nan
    lab = tdf[f"label_mid_{h}"].to_numpy(dtype=float)[sel].copy()
    lab[~tdf[f"label_valid_{h}"].to_numpy(dtype=bool)[sel]] = np.nan
    window_ic = ic(er, lab)
    if not np.isfinite(window_ic):
        raise SystemExit("FX05: pinned IC window degenerate")
    return cases, {
        "grid_window_0based": [lo, hi],
        "target_pair": FX05_TARGET_PAIR,
        "horizon": h,
        "ic": float(window_ic),
        "grid_step_ns": FX05CrossPairRelativeValue.GRID_STEP_NS,
        "source": "bundled day-2 features (golden vector has a single pair)",
    }


def main() -> int:
    if not PARAMS_PATH.exists():
        raise SystemExit(
            f"{PARAMS_PATH} missing — run research/alpha_reports/run_all.py first"
        )
    models = load_params_file(PARAMS_PATH)
    params = {aid: m.params() for aid, m in models.items()}

    eq_frame = build_golden_frame(GOLDEN / "events_eq_mbo.jsonl", CONFIGS, 1)
    fx_frame = build_golden_frame(GOLDEN / "events_fx_quote.jsonl", CONFIGS, 101)

    out = {
        "x-version": 1,
        "description": (
            "Alpha golden cases (6 representative alphas). Scores use "
            "linear_z_v1 with the embedded params; tolerance 1e-9 abs/rel "
            "on expected_return/confidence/ic; counts exact. Frames: golden "
            "vectors replayed through the feature engine at cadence 0 "
            "(row k = emission after 1-based event k); FX05 from bundled "
            "day-2 features with inputs embedded."
        ),
        "params": {aid: params[aid] for aid in
                   ("EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09")},
        "alphas": {},
    }
    for aid, frame, rows, win, src in (
        ("EQ01", eq_frame, EQ_ROWS, EQ_IC_WINDOW, "events_eq_mbo.jsonl"),
        ("EQ03", eq_frame, EQ_ROWS, EQ_IC_WINDOW, "events_eq_mbo.jsonl"),
        ("EQ06", eq_frame, EQ_ROWS, EQ_IC_WINDOW, "events_eq_mbo.jsonl"),
        ("FX01", fx_frame, FX_ROWS, FX_IC_WINDOW, "events_fx_quote.jsonl"),
        ("FX09", fx_frame, FX_ROWS, FX_IC_WINDOW, "events_fx_quote.jsonl"),
    ):
        cases, icw = frame_cases(aid, frame, models[aid], params, rows, win)
        out["alphas"][aid] = {
            "source": src,
            "instrument_id": int(frame["instrument_id"].iloc[0]),
            "cases": cases,
            "ic_window": icw,
        }
        print(f"{aid}: 5 cases ok, ic={icw['ic']:+.6f}")

    cases, icw = fx05_cases(params, models)
    out["alphas"]["FX05"] = {
        "source": "data/features (day 2)",
        "instrument_id": FX05_TARGET_PAIR,
        "cases": cases,
        "ic_window": icw,
    }
    print(f"FX05: 5 cases ok, ic={icw['ic']:+.6f}")

    (GOLDEN / "expected_alpha.json").write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n"
    )

    # -- expected_backtest.json: EQ01 on the golden EQ frame --------------
    inst = json.loads((CONFIGS / "instruments.json").read_text())["instruments"]
    meta = {int(r["instrument_id"]): {
        "tick_size": float(r["tick_size"]), "lot_size": int(r["lot_size"]),
        "adv": float(r["adv"]), "asset_class": r["asset_class"],
        "ref_price": float(r["ref_price"]),
    } for r in inst}
    cm = CostModel.load(CONFIGS / "execution.json",
                        multiplier=BT_CONFIG["cost_multiplier"])
    bt = Backtester(cm, meta, BacktestConfig(
        max_pos_qty=BT_CONFIG["max_pos_qty"],
        conf_min=BT_CONFIG["conf_min"],
        latency_rows=BT_CONFIG["latency_rows"],
    ))
    scores = models["EQ01"].score({1: eq_frame})
    res = bt.run({1: eq_frame}, scores, "EQUITY")
    r1 = res.per_instrument[1]
    bt_out = {
        "x-version": 1,
        "description": (
            "Research-backtester golden: EQ01 (day-1-fitted params from "
            "expected_alpha.json) on the golden EQ frame. total_pnl / "
            "total_costs at 1e-9 abs/rel; counts exact."
        ),
        "alpha_id": "EQ01",
        "source": "events_eq_mbo.jsonl",
        "instrument_id": 1,
        "config": BT_CONFIG,
        "total_pnl": r1.total_pnl,
        "gross_pnl": r1.gross_pnl,
        "total_costs": r1.total_costs,
        "spread_cost": r1.spread_cost,
        "fee_cost": r1.fee_cost,
        "impact_cost": r1.impact_cost,
        "trade_count": r1.trade_count,
        "traded_qty": r1.traded_qty,
        "n_rows": r1.n_rows,
    }
    if r1.trade_count < 5:
        raise SystemExit(f"EQ01 golden backtest nearly empty: {r1.trade_count}")
    (GOLDEN / "expected_backtest.json").write_text(
        json.dumps(bt_out, indent=2, sort_keys=True) + "\n"
    )
    print(f"backtest golden: pnl={r1.total_pnl:+.4f} trades={r1.trade_count} "
          f"costs={r1.total_costs:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
