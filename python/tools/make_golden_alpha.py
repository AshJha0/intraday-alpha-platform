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

expected_backtest.json (x-version 3) pins the research backtest on the
golden EQ frame under BOTH rule sets, each replayed by Python and Java:
EQ01 under the legacy rules (top level), EQ06 under the v1.5.0 defaults
(``default_rules``, with the scored-row mask and every position change
embedded; ``default_rules_1x`` is the same run at full costs, which makes
no trade) and scalar cost-model cases for both impact rules
(``cost_model_cases``).  Money at 1e-9, counts and positions exact.

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
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iap.alpha import load_params_file  # noqa: E402
from iap.alpha.data import (  # noqa: E402
    asof_to_grid,
    load_features,
    make_grid,
    session_days,
    split_by_day,
)
from iap.alpha.fx_exposure import FX05CrossPairRelativeValue, solve_factor_returns  # noqa: E402
from iap.alpha.goldenframes import build_golden_frame  # noqa: E402
from iap.backtest import BacktestConfig, Backtester, CostModel  # noqa: E402
from iap.validation.metrics import capacity_breakeven, ic  # noqa: E402

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
#: The first cross-language backtest vector keeps the LEGACY research rules
#: (the v1.4.0 defaults), named: ``BacktestConfig.legacy()`` and
#: ``CostModel.with_linear_impact()`` in Python, ``Config.legacy`` and
#: ``CostModel.withLinearImpact`` in Java.
BT_CONFIG = {
    "max_pos_qty": 1000,
    "conf_min": 0.2,
    "latency_rows": 1,
    "cost_multiplier": 1.0,
    "position_policy": "sign",
    "cap_fills_at_l1": False,
    "block_rows_column": None,
    "impact_model": "linear",
}
#: The cross-language vector under the DEFAULT rules (v1.5.0; Python and
#: Java since x-version 3).  EQ06 at 1 % of the pinned costs: the golden
#: frame's spread is about 19 bps round trip and no flagship alpha forecasts
#: that much, so at 1x costs the cost-aware policy makes no trade here
#: (pinned as ``default_rules_1x``); the vector exists to pin the policy,
#: the fill cap, the row block and the square-root impact on a run that
#: trades.
BT_DEFAULT_ALPHA = "EQ06"
BT_DEFAULT_CONFIG = {
    "max_pos_qty": 1000,
    "conf_min": 0.2,
    "latency_rows": 1,
    "cost_multiplier": 0.01,
    "position_policy": "cost_aware",
    "cap_fills_at_l1": True,
    "block_rows_column": "auto",
    "impact_model": "sqrt",
}


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
        return (
            0.5 * g("ofi_norm_l1_w1s_v1")
            + 0.3 * g("ofi_norm_l5_w1s_v1")
            + 0.2 * g("ofi_norm_l5_w5s_v1")
        )
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
                f"{alpha_id} row {r}: class ({er}, {conf}) != brute force ({er_bf}, {conf_bf})"
            )
        cases.append(
            {
                "event_index_1based": ev_index,
                "exchange_ts": int(frame["exchange_ts"].iloc[r]),
                "inputs": {
                    name: (float(row[name]) if math.isfinite(float(row[name])) else None)
                    for name in model.features
                },
                "expected_return": er,
                "confidence": conf,
            }
        )
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
    return cases, {"rows_0based": [lo, hi], "horizon": h, "ic": float(window_ic)}


def fx05_cases(params: dict, models):
    """FX05 golden cases from the bundled day-2 features (inputs embedded)."""
    model = models["FX05"]
    frames = load_features(REPO / "data" / "features", instrument_ids=list(range(101, 109)))
    days = session_days(frames)
    _, day2 = split_by_day(frames, days[1])
    pair_ids = sorted(day2)
    grid = make_grid(day2, FX05CrossPairRelativeValue.GRID_STEP_NS)
    mat = np.vstack(
        [
            asof_to_grid(
                day2[i]["exchange_ts"].to_numpy(),
                day2[i]["ret_log_1m_v1"].to_numpy(dtype=float),
                grid,
                FX05CrossPairRelativeValue.MAX_AGE_NS,
            )
            for i in pair_ids
        ]
    )
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
                f"FX05 grid {gi}: class ({er}, {conf}) != brute force ({er_bf}, {conf_bf})"
            )
        cases.append(
            {
                "grid_index_0based": gi,
                "grid_ts": int(grid[gi]),
                "native_row_0based": rrow,
                "native_exchange_ts": int(tts[rrow]),
                "target_pair": FX05_TARGET_PAIR,
                "inputs": {
                    str(pid): (float(v) if math.isfinite(v) else None)
                    for pid, v in zip(pair_ids, r_vec, strict=False)
                },
                "raw_residual_signal": float(raw),
                "expected_return": er,
                "confidence": conf,
            }
        )
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
        raise SystemExit(f"{PARAMS_PATH} missing — run research/alpha_reports/run_all.py first")
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
        "params": {aid: params[aid] for aid in ("EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09")},
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

    (GOLDEN / "expected_alpha.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    # -- expected_backtest.json: EQ01 on the golden EQ frame --------------
    inst = json.loads((CONFIGS / "instruments" / "instruments.json").read_text())["instruments"]
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

    def golden_run(alpha_id: str, cfg: dict):
        """One instrument-1 backtest of ``alpha_id`` under ``cfg`` (every
        rule named: nothing is left to a default)."""
        cm = CostModel.load(
            CONFIGS / "execution" / "execution.json", multiplier=cfg["cost_multiplier"]
        )
        cm = replace(cm, impact_model=cfg["impact_model"])
        bt = Backtester(
            cm,
            meta,
            BacktestConfig(
                max_pos_qty=cfg["max_pos_qty"],
                conf_min=cfg["conf_min"],
                latency_rows=cfg["latency_rows"],
                position_policy=cfg["position_policy"],
                cap_fills_at_l1=cfg["cap_fills_at_l1"],
                block_rows_column=cfg["block_rows_column"],
            ),
        ).for_horizon(models[alpha_id].horizon)
        scores = models[alpha_id].score({1: eq_frame})
        return bt.run({1: eq_frame}, scores, "EQUITY").per_instrument[1], bt.config

    def default_block(cfg: dict) -> tuple[dict, int]:
        """One default-rules block: the numbers, the scored-row mask the
        ``"auto"`` row block used (a port without a label engine takes it as
        an input) and every position change (row, position after the row)."""
        res, bt_cfg = golden_run(BT_DEFAULT_ALPHA, cfg)
        allowed = bt_cfg.allowed_rows(eq_frame)
        pos = res.positions
        changed = np.flatnonzero(np.diff(pos, prepend=0.0) != 0.0)
        block = {
            "alpha_id": BT_DEFAULT_ALPHA,
            "horizon": models[BT_DEFAULT_ALPHA].horizon,
            "horizon_ns": int(bt_cfg.horizon_ns),
            "hysteresis": bt_cfg.hysteresis,
            "config": cfg,
            **numbers(res),
            "scored_rows": {
                "source": bt_cfg.block_rows_label(),
                "n_rows": len(allowed),
                "n_scored": int(allowed.sum()),
                "blocked_rows": [int(i) for i in np.flatnonzero(~allowed)],
            },
            "position_changes": [[int(i), float(pos[i])] for i in changed],
        }
        return block, res.trade_count

    def cost_model_cases() -> dict:
        """Scalar cost-model vectors under both impact rules: the cost
        components of one execution, the round-trip hurdle and the breakeven
        capacity (finite cases only: JSON has no infinity)."""
        base = CostModel.load(CONFIGS / "execution" / "execution.json")
        cases = []
        for model in ("sqrt", "linear"):
            for asset_class, qty, mid, hs, adv, lot, edge, mult in (
                ("EQUITY", 300.0, 187.37, 0.005, 38_000_000.0, 100, 4.0e-4, 1.0),
                ("EQUITY", -1000.0, 52.115, 0.015, 5_000_000.0, 100, 9.0e-4, 2.0),
                ("ETF", 40.0, 431.02, 0.01, 60_000_000.0, 1, 1.0e-5, 0.5),
                ("FX", 3.0, 1.08655, 0.00001, 4.0e9, 1000, 6.0e-5, 1.0),
                ("FX", -25.0, 149.432, 0.0015, 2.5e9, 1000, 8.0e-5, 0.5),
            ):
                cm = replace(base, impact_model=model, multiplier=mult)
                comp = cm.cost_components(
                    np.array([qty]), np.array([mid]), np.array([hs]), asset_class, adv, lot
                )
                unit = float(lot) if asset_class == "FX" else 1.0
                threshold = cm.round_trip_cost_return(np.array([mid]), np.array([hs]), asset_class)
                cases.append(
                    {
                        "impact_model": model,
                        "asset_class": asset_class,
                        "multiplier": mult,
                        "qty": qty,
                        "mid": mid,
                        "half_spread": hs,
                        "adv": adv,
                        "lot_size": lot,
                        "edge_return": edge,
                        "spread": float(comp["spread"][0]),
                        "fee": float(comp["fee"][0]),
                        "impact": float(comp["impact"][0]),
                        "impact_bps": float(cm.impact_bps(np.array([abs(qty) * unit / adv]))[0]),
                        "round_trip_cost_return": float(threshold[0]),
                        "breakeven_size": float(
                            cm.breakeven_size(edge, mid, hs, asset_class, adv, lot)
                        ),
                        "capacity": capacity_breakeven(cm, edge, mid, hs, asset_class, adv, lot),
                    }
                )
        if not any(c["breakeven_size"] > 0 for c in cases):
            raise SystemExit("cost-model cases pin no positive breakeven size")
        if not any(c["breakeven_size"] == 0 for c in cases):
            raise SystemExit("cost-model cases pin no zero breakeven size")
        return {
            "impact_coeff_bps_per_pct_adv": base.impact_coeff_bps_per_pct_adv,
            "equity_taker_fee_per_share": base.equity_taker_fee_per_share,
            "fx_commission_per_million": base.fx_commission_per_million,
            "sqrt_impact_coeff_bps": base.sqrt_impact_coeff_bps,
            "cases": cases,
        }

    def numbers(r) -> dict:
        return {
            "total_pnl": r.total_pnl,
            "gross_pnl": r.gross_pnl,
            "total_costs": r.total_costs,
            "spread_cost": r.spread_cost,
            "fee_cost": r.fee_cost,
            "impact_cost": r.impact_cost,
            "trade_count": r.trade_count,
            "traded_qty": r.traded_qty,
            "n_rows": r.n_rows,
        }

    r1, _ = golden_run("EQ01", BT_CONFIG)
    default_rules, default_trades = default_block(BT_DEFAULT_CONFIG)
    default_rules_1x, trades_1x = default_block({**BT_DEFAULT_CONFIG, "cost_multiplier": 1.0})
    # one mask serves both default-rules runs (same alpha, same horizon)
    if default_rules_1x.pop("scored_rows") != default_rules["scored_rows"]:
        raise SystemExit("the two default-rules runs disagree on the scored rows")
    bt_out = {
        "x-version": 3,
        "description": (
            "Research-backtester golden on the golden EQ frame (day-1-fitted "
            "params from expected_alpha.json), replayed by Python and Java. "
            "The top-level numbers are EQ01 under the LEGACY research rules "
            "that `config` names (sign position policy, fills not capped, "
            "every row traded, linear impact — the defaults up to v1.4.0). "
            "`default_rules` is the run under the v1.5.0 defaults "
            "(cost-aware positions, L1 fill cap, scored rows only, "
            "square-root impact) at the cost multiplier its config names: "
            "`scored_rows.blocked_rows` are the rows the 'auto' row block "
            "removes (a port without a label engine takes the mask as an "
            "input) and `position_changes` lists [row, position after the "
            "row] for every trade. `default_rules_1x` is the same run at "
            "full costs (same mask), where no forecast clears the round-trip "
            "cost. `cost_model_cases` are scalar cost-model vectors under "
            "both impact rules (cost components, round-trip cost return, "
            "breakeven size and capacity). Money at 1e-9 abs/rel; counts and "
            "positions exact."
        ),
        "alpha_id": "EQ01",
        "source": "events_eq_mbo.jsonl",
        "instrument_id": 1,
        "config": BT_CONFIG,
        **numbers(r1),
        "default_rules": default_rules,
        "default_rules_1x": default_rules_1x,
        "cost_model_cases": cost_model_cases(),
    }
    if r1.trade_count < 5:
        raise SystemExit(f"EQ01 golden backtest nearly empty: {r1.trade_count}")
    if default_trades < 5:
        raise SystemExit(
            f"{BT_DEFAULT_ALPHA} default-rules golden backtest nearly empty: {default_trades}"
        )
    if not default_rules["scored_rows"]["blocked_rows"]:
        raise SystemExit("the default-rules vector blocks no row: the row block is not pinned")
    (GOLDEN / "expected_backtest.json").write_text(
        json.dumps(bt_out, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"backtest golden: legacy EQ01 pnl={r1.total_pnl:+.4f} trades={r1.trade_count} "
        f"costs={r1.total_costs:.4f}; default {BT_DEFAULT_ALPHA} "
        f"pnl={default_rules['total_pnl']:+.4f} trades={default_trades} "
        f"(1x: {trades_1x} trades)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
