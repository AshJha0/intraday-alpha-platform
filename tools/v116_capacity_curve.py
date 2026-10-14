"""v1.16 item 7: taker capacity curve for EQ10 / EQ05 (IN-SAMPLE, descriptive).

For the signal tails a taker would trade (top 10% / 1% / 0.1% of |z|), the
realised edge (mean mid move over the horizon in the signal's direction) is
set against the round-trip cost at increasing order sizes:

    net_bps(q) = edge_bps - fixed_bps - 2 * impact_bps(q / ADV)

``fixed_bps`` = spread + fees of a round trip (median over the tail rows,
``CostModel.round_trip_cost_return``); impact from the platform cost model
(square-root default). ``breakeven_shares`` = ``CostModel.breakeven_size``:
the largest trade whose edge still covers its cost (0 when the edge does not
cover spread + fee at any size).

In-sample only (the 7 multi7 sessions + 3 R7 in-sample days; holdout
refused). Descriptive: no ledger look, nothing selected. z uses the pooled
in-sample mean / std of each alpha's raw signal; the sign is the pooled
signal-label correlation, so the tail edge is the most favourable reading.

Loads one dataset and one instrument at a time, narrowed columns (memory).
Output: research/v116/capacity_curve.json and CAPACITY_CURVE.md.
"""

from __future__ import annotations

import gc
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

REAL = REPO / "data" / "real"
DATASETS = ("multi7", "r7_20191018", "r7_20210713", "r7_20210813")
HOLDS = ("1s", "10s", "1m")
TAILS = (0.10, 0.01, 0.001)
SIZES = (100, 300, 1_000, 3_000, 10_000, 30_000)
ALPHAS = ("EQ10", "EQ05")


def _data_root() -> Path:
    # worktrees have no data/; use the main checkout's (read-only)
    if (REAL / "multi7").is_dir():
        return REAL
    return REPO.parent / "intraday-alpha-platform" / "data" / "real"


def collect() -> tuple[dict, dict, object]:
    from iap.alpha import build, configure_universe
    from iap.alpha.data import load_features
    from iap.backtest.costs import CostModel
    from iap.research.runner import load_instrument_meta

    root = _data_root()
    alphas = {a: build(a) for a in ALPHAS}
    need = {"exchange_ts", "mid_price_v1", "spread_ticks_v1"}
    for a in alphas.values():
        need |= set(a.features)
    for h in HOLDS:
        need |= {f"label_mid_{h}", f"label_valid_{h}"}
    pick = lambda cols: [c for c in cols if c in need]  # noqa: E731
    # per (alpha, iid): lists of raw signal, labels per horizon, mid, half spread
    acc: dict = {}
    meta = None
    cost = None
    for name in DATASETS:
        ds = root / name
        configure_universe(ds / "configs" / "instruments" / "instruments.json")
        if meta is None:
            meta = load_instrument_meta(ds / "configs")
            cost = CostModel.load(ds / "configs" / "execution" / "execution.json")
        for iid in sorted(meta):
            fr = load_features(ds / "features", instrument_ids=[iid], columns=pick).get(iid)
            if fr is None or not len(fr):
                continue
            tick = float(meta[iid]["tick_size"])
            for aid, a in alphas.items():
                if iid not in a.universe(sorted(meta)):
                    continue
                cell = acc.setdefault(
                    (aid, iid), {"x": [], "mid": [], "hs": [], **{h: [] for h in HOLDS}}
                )
                cell["x"].append(a.raw_signal(fr).to_numpy(dtype=float))
                cell["mid"].append(fr["mid_price_v1"].to_numpy(dtype=float))
                cell["hs"].append(fr["spread_ticks_v1"].to_numpy(dtype=float) * tick / 2.0)
                for h in HOLDS:
                    y = fr[f"label_mid_{h}"].to_numpy(dtype=float, copy=True)
                    y[~fr[f"label_valid_{h}"].to_numpy(dtype=bool)] = np.nan
                    cell[h].append(y)
            del fr
            gc.collect()
        print(f"loaded {name}", flush=True)
    return acc, meta, cost


def curve(acc: dict, meta: dict, cost) -> list[dict]:
    rows = []
    for (aid, iid), cell in sorted(acc.items()):
        x = np.concatenate(cell["x"])
        mid = np.concatenate(cell["mid"])
        hs = np.concatenate(cell["hs"])
        m = meta[iid]
        adv = float(m["adv"])
        ac = "EQUITY" if m["asset_class"] in ("EQUITY", "ETF") else m["asset_class"]
        for h in HOLDS:
            y = np.concatenate(cell[h])
            ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(mid) & (mid > 0) & np.isfinite(hs)
            xs, ys, mids, hss = x[ok], y[ok], mid[ok], hs[ok]
            z = (xs - xs.mean()) / (xs.std() + 1e-12)
            sgn = float(np.sign(np.corrcoef(z, ys)[0, 1])) or 1.0
            for tail in TAILS:
                cut = np.quantile(np.abs(z), 1.0 - tail)
                k = np.abs(z) >= cut
                edge = float(np.mean(sgn * np.sign(z[k]) * ys[k]))
                mid_t, hs_t = float(np.median(mids[k])), float(np.median(hss[k]))
                fixed = float(
                    cost.round_trip_cost_return(np.array([mid_t]), np.array([hs_t]), ac)[0]
                )
                net = {
                    str(q): 1e4
                    * (
                        edge
                        - fixed
                        - 2.0
                        * cost.multiplier
                        * float(cost.impact_bps(np.array([q / adv]))[0])
                        * 1e-4
                    )
                    for q in SIZES
                }
                be = cost.breakeven_size(edge, mid_t, hs_t, ac, adv, int(m.get("lot_size", 1)))
                rows.append(
                    {
                        "alpha": aid,
                        "symbol_id": iid,
                        "hold": h,
                        "tail": tail,
                        "n": int(k.sum()),
                        "edge_bps": 1e4 * edge,
                        "fixed_cost_bps": 1e4 * fixed,
                        "net_bps_by_shares": net,
                        "breakeven_shares": None if not np.isfinite(be) else float(be),
                        "adv": adv,
                    }
                )
    return rows


def render(rows: list[dict]) -> str:
    out = [
        "# v1.16 item 7: taker capacity curve (EQ10 / EQ05, in-sample)",
        "",
        "Net bps per round trip = realised tail edge - spread and fees - 2 x impact at the order size "
        "(platform cost model, square-root impact). **Break-even shares = 0 means the edge does not "
        "cover spread + fees at any size**: there is no taker capacity. In-sample and descriptive "
        "(10 sessions, sign chosen in-sample, so the most favourable reading); see the module docstring "
        "of `tools/v116_capacity_curve.py`.",
        "",
        "| alpha | id | hold | tail | n | edge bp | fixed bp | "
        + " | ".join(f"net @{q}" for q in SIZES)
        + " | break-even sh |",
        "|---|---:|---|---:|---:|---:|---:|" + "---:|" * len(SIZES) + "---:|",
    ]
    for r in rows:
        nets = " | ".join(f"{r['net_bps_by_shares'][str(q)]:.2f}" for q in SIZES)
        be = r["breakeven_shares"]
        out.append(
            f"| {r['alpha']} | {r['symbol_id']} | {r['hold']} | {r['tail']:g} | {r['n']} | "
            f"{r['edge_bps']:.3f} | {r['fixed_cost_bps']:.3f} | {nets} | {'-' if be is None else f'{be:,.0f}'} |"
        )
    pos = [r for r in rows if r["breakeven_shares"]]
    out += [
        "",
        f"Cells with any break-even size > 0: **{len(pos)} of {len(rows)}**.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    acc, meta, cost = collect()
    rows = curve(acc, meta, cost)
    out = REPO / "research" / "v116"
    out.mkdir(parents=True, exist_ok=True)
    doc = {
        "sizes_shares": SIZES,
        "holds": HOLDS,
        "tails": TAILS,
        "datasets": DATASETS,
        "rows": rows,
    }
    (out / "capacity_curve.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    (out / "CAPACITY_CURVE.md").write_text(render(rows), encoding="utf-8")
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
