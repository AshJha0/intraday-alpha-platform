"""v1.16 P0-6: in-sample monetisation study for EQ10 / EQ05 (owner-approved grid).

IN-SAMPLE ONLY: the 7 multi7 sessions + the 3 R7 in-sample days. The R7
holdout sessions are spent for selection and are refused here.

For every session ``d`` the alphas are fitted on the OTHER sessions (leave
one session out) at the label horizon of the holding period, then scored on
``d`` and run through the research ``Backtester`` (methods v4 rules:
cost-aware positions, fills capped at L1, causal row block, session flatten)
under every cell of the grid:

    holding  {1s, 5s, 10s, 30s, 1m}      label horizon = hold (BacktestConfig.horizon_ns)
    band     {1, 1.5, 2, 3} x round trip  entry needs |er| > band * cost (er / band)
    signal   {EQ10, EQ05, ENS}            ENS = ensemble_scores of both (equal z weight)
    sizing   {flat, confidence}           flat: conf_min 0.5; confidence: conf_min 0.8
    cost     {1x, 1.5x}                   CostModel.with_multiplier
    latency  primary 400 us (v3 prereg legs); the 1 s protocol default reported too

Output: ``research/v116/monetisation_cells.csv`` (one row per cell x session x
instrument) and ``monetisation_summary.json`` (per cell: daily net P&L mean,
sd, one-sided 95% day-clustered lower bound, trades, gross, costs). Grid size
is recorded so the selection can be deflated later.

Usage (from python/, PYTHONPATH=src):
    python ../tools/v116_monetisation_study.py [--sessions-limit N] [--out DIR]
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
REAL = REPO / "data" / "real"

#: In-sample datasets (holdout refused below).
DATASETS = ("multi7", "r7_20191018", "r7_20210713", "r7_20210813")
HOLDOUT_MARKERS = ("r7_holdout", "holdout2026", "r7_2025", "r7_2026")

HOLDS = ("1s", "5s", "10s", "30s", "1m")
BANDS = (1.0, 1.5, 2.0, 3.0)
SIGNALS = ("EQ10", "EQ05", "ENS")
SIZINGS = {"flat": 0.5, "confidence": 0.8}
COSTS = (1.0, 1.5)
LATENCIES_NS = {"v3_400us": 400_000, "protocol_1s": 1_000_000_000}
NS_DAY = 86_400 * 10**9

BASE_COLS = (
    "exchange_ts",
    "mid_price_v1",
    "spread_ticks_v1",
    "depth_bid_l1_v1",
    "depth_ask_l1_v1",
)


def _columns(feature_cols: set[str]):
    want = set(BASE_COLS) | feature_cols
    for h in HOLDS:
        want |= {f"label_mid_{h}", f"label_valid_{h}", f"label_reason_{h}"}

    def pick(names):
        return [c for c in names if c in want]

    return pick


def load_sessions(
    limit: int | None, datasets=DATASETS
) -> tuple[dict[str, dict[int, pd.DataFrame]], dict, Path]:
    """``{session_date: {iid: frame}}`` across the in-sample datasets."""
    from iap.alpha import build, configure_universe
    from iap.alpha.data import load_features

    feats = set(build("EQ10").features) | set(build("EQ05").features)
    pick = _columns(feats)
    sessions: dict[str, dict[int, pd.DataFrame]] = {}
    configs = None
    for name in datasets:
        if any(m in name for m in HOLDOUT_MARKERS):
            raise SystemExit(f"refusing holdout dataset {name}")
        ds = REAL / name
        if not (ds / "features" / "features_summary.json").is_file():
            raise SystemExit(f"{ds}: features missing (build them first)")
        configure_universe(ds / "configs" / "instruments" / "instruments.json")
        configs = configs or ds / "configs"
        frames = load_features(ds / "features", columns=pick)
        for iid, df in frames.items():
            day = df["exchange_ts"].to_numpy(dtype=np.int64) // NS_DAY
            for d in np.unique(day):
                key = pd.Timestamp(int(d) * NS_DAY).strftime("%Y-%m-%d")
                part = df.loc[day == d].reset_index(drop=True)
                sessions.setdefault(key, {})[iid] = part
        del frames
        gc.collect()
        print(f"loaded {name}: {len(sessions)} sessions so far", flush=True)
    keys = sorted(sessions)
    if limit:
        keys = keys[:limit]
    return {k: sessions[k] for k in keys}, {}, configs


def fit_alpha(alpha_id: str, hold: str, train: dict[int, pd.DataFrame]):
    from iap.alpha import build

    a = build(alpha_id)
    a.horizon = hold  # fit the linear scale at the holding horizon's label
    a.fit({i: f for i, f in train.items() if i in a.universe(sorted(train))})
    return a


def pooled_train(sessions, exclude: str) -> dict[int, pd.DataFrame]:
    out: dict[int, list[pd.DataFrame]] = {}
    for k, frames in sessions.items():
        if k == exclude:
            continue
        for iid, f in frames.items():
            out.setdefault(iid, []).append(f)
    return {i: pd.concat(v, ignore_index=True) for i, v in out.items()}


def lower_bound_95(x: np.ndarray) -> float | None:
    from scipy import stats

    n = len(x)
    if n < 2:
        return None
    return float(x.mean() - stats.t.ppf(0.95, n - 1) * x.std(ddof=1) / math.sqrt(n))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sessions-limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=REPO / "research" / "v116")
    ap.add_argument("--datasets", nargs="+", default=list(DATASETS), help="smoke tests only")
    args = ap.parse_args(argv)

    from iap.backtest.costs import CostModel
    from iap.backtest.engine import Backtester, ensemble_scores
    from iap.research.runner import load_instrument_meta
    from iap.validation.methods import methods

    t0 = time.time()
    sessions, _, configs = load_sessions(args.sessions_limit, tuple(args.datasets))
    meta = load_instrument_meta(configs)
    bundle = methods("v4")
    base_cost = bundle.cost_model(CostModel.load(configs / "execution" / "execution.json"))
    hold_ns = {"1s": 10**9, "5s": 5 * 10**9, "10s": 10 * 10**9, "30s": 30 * 10**9, "1m": 60 * 10**9}

    rows: list[dict] = []
    n_cells = len(HOLDS) * len(BANDS) * len(SIGNALS) * len(SIZINGS) * len(COSTS) * len(LATENCIES_NS)
    for d in sessions:
        train = pooled_train(sessions, d)
        test = sessions[d]
        for hold in HOLDS:
            alphas = {a: fit_alpha(a, hold, train) for a in ("EQ10", "EQ05")}
            scored = {
                a: m.score({i: f for i, f in test.items() if i in m.universe(sorted(test))})
                for a, m in alphas.items()
            }
            scored["ENS"] = ensemble_scores(
                {"EQ10": scored["EQ10"], "EQ05": scored["EQ05"]},
                {a: m.beta for a, m in alphas.items()},
            )
            for sig, band, (sz, cmin), cm, (lat_name, lat) in itertools.product(
                SIGNALS, BANDS, SIZINGS.items(), COSTS, LATENCIES_NS.items()
            ):
                cfg = bundle.backtest_config(
                    latency_ns=lat,
                    max_decision_age_ns=60 * 10**9,
                    flatten_at_session_end=True,
                )
                from dataclasses import replace

                cfg = replace(cfg, conf_min=cmin, horizon_ns=hold_ns[hold])
                bt = Backtester(base_cost.with_multiplier(cm), meta, cfg)
                for iid, sc in scored[sig].items():
                    sc = sc.copy()
                    sc["expected_return"] = sc["expected_return"] / band
                    r = bt.run_instrument(iid, test[iid], sc)
                    rows.append(
                        {
                            "session": d,
                            "instrument_id": iid,
                            "hold": hold,
                            "band": band,
                            "signal": sig,
                            "sizing": sz,
                            "cost": cm,
                            "latency": lat_name,
                            "net_pnl": r.total_pnl,
                            "gross_pnl": r.gross_pnl,
                            "costs": r.total_costs,
                            "spread_cost": r.spread_cost,
                            "fee_cost": r.fee_cost,
                            "impact_cost": r.impact_cost,
                            "trades": r.trade_count,
                            "traded_qty": r.traded_qty,
                        }
                    )
            del alphas, scored
            gc.collect()
            print(f"{d} {hold} done ({time.time() - t0:.0f}s)", flush=True)
        del train
        gc.collect()

    args.out.mkdir(parents=True, exist_ok=True)
    cells = pd.DataFrame(rows)
    cells.to_csv(args.out / "monetisation_cells.csv", index=False)
    keys = ["hold", "band", "signal", "sizing", "cost", "latency"]
    daily = cells.groupby([*keys, "session"], as_index=False)[
        ["net_pnl", "gross_pnl", "costs", "trades"]
    ].sum()
    summary = []
    for k, g in daily.groupby(keys):
        x = g["net_pnl"].to_numpy(dtype=float)
        summary.append(
            {
                **dict(zip(keys, k, strict=True)),
                "n_days": len(x),
                "net_per_day_mean": float(x.mean()),
                "net_per_day_sd": float(x.std(ddof=1)) if len(x) > 1 else None,
                "net_per_day_lb95": lower_bound_95(x),
                "gross_per_day": float(g["gross_pnl"].mean()),
                "costs_per_day": float(g["costs"].mean()),
                "trades_per_day": float(g["trades"].mean()),
                "days_positive": int((x > 0).sum()),
            }
        )
    summary.sort(
        key=lambda s: -(s["net_per_day_lb95"] if s["net_per_day_lb95"] is not None else -1e18)
    )
    doc = {
        "study": "v1.16 P0-6 in-sample monetisation (EQ10/EQ05)",
        "sessions": sorted(sessions),
        "n_cells": n_cells,
        "grid": {
            "hold": HOLDS,
            "band": BANDS,
            "signal": SIGNALS,
            "sizing": SIZINGS,
            "cost": COSTS,
            "latency_ns": LATENCIES_NS,
        },
        "fit": "leave-one-session-out, LinearAlpha refitted at the holding horizon label",
        "gate_reference": "one-sided 95% LB of daily net P&L > 0 at 1.5x cost (owner-accepted)",
        "max_pos_qty": int(
            Backtester(base_cost, meta, bundle.backtest_config()).config.max_pos_qty
        ),
        "runtime_s": round(time.time() - t0, 1),
        "cells": summary,
    }
    (args.out / "monetisation_summary.json").write_text(
        json.dumps(doc, indent=1) + "\n", encoding="utf-8"
    )
    best = [s for s in summary if s["cost"] == 1.5 and s["latency"] == "v3_400us"][:10]
    print(json.dumps(best, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
