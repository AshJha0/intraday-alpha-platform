"""TCA_REPORT.md generation from the bundled simulated order set (spec §19).

``python3 -m iap.tca`` (with PYTHONPATH=src) writes
``research/tca/TCA_REPORT.md`` plus ``tca_orders.json`` (the full per-order
metric records) from the pinned deterministic parent-order simulation in
:mod:`iap.tca.simulator`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from iap.tca.simulator import SIM_SEED, bundled_order_set
from iap.tca.tca import impact_regression, order_tca

_REPO = Path(__file__).resolve().parents[4]


def _fmt(x: Any, nd: int = 3) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _mean(vals: List[Optional[float]]) -> Optional[float]:
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def compute_tca_records(golden_dir: Optional[Path] = None) -> Dict[int, dict]:
    """Run the bundled simulation and full TCA; {instrument_id: payload}."""
    out: Dict[int, dict] = {}
    for iid, (tl, orders, tick) in bundled_order_set(golden_dir).items():
        recs = [order_tca(o, tl) for o in orders]
        # impact regression across ALL child fills of the instrument
        part: List[float] = []
        cost_bps: List[float] = []
        for o in orders:
            s = o.sign
            for f in o.fills:
                part.append(f.qty / f.opp_depth_at_fill)
                cost_bps.append(1e4 * s * (f.price - f.mid_at_fill)
                                / f.mid_at_fill)
        impact = impact_regression(part, cost_bps) if len(part) >= 3 else None
        out[iid] = {"orders": recs, "impact_regression": impact,
                    "n_market_trades": len(tl.trades),
                    "n_states": len(tl),
                    "crossed_states_skipped": tl.crossed_states_skipped,
                    "n_halts": len(tl.halts)}
    return out


def render_report(records: Dict[int, dict]) -> str:
    """Render the TCA markdown report."""
    lines: List[str] = []
    lines.append("# TCA Report — Simulated Parent-Order Set (research)")
    lines.append("")
    lines.append(
        f"Deterministic simulation (SplitMix64 seed `{SIM_SEED}`) over the "
        "cross-language golden vectors (`tests/golden/events_eq_mbo.jsonl`, "
        "`events_fx_quote.jsonl`), replayed through the reference "
        "consolidated book. Execution model: 4 child slices 15s apart, "
        "marketable at the touch plus depth-dependent impact ticks, 15% "
        "per-child unfill probability. This is a research TCA harness, not "
        "the production backtester.")
    lines.append("")
    skipped = {iid: p["crossed_states_skipped"] for iid, p in records.items()}
    n_states = {iid: p["n_states"] for iid, p in records.items()}
    lines.append(
        "**Timeline rule (pinned, API_PORTFOLIO_TCA.md §2.1)**: crossed "
        "consolidated states (cross-venue bid > ask, a synthetic-generator "
        "artifact) are SKIPPED and counted, locked states (half-spread 0) "
        "are kept; markouts past the timeline end or across a HALT are "
        "undefined (excluded, never a stale mid); every fill must lie in "
        "[arrival, end]. Crossed states skipped per instrument: "
        + ", ".join(f"{iid}: {skipped[iid]} of {n_states[iid] + skipped[iid]}"
                    for iid in sorted(skipped))
        + ". Spread-cost lines are therefore never negative by "
        "construction (conventions §7: honest, not hidden).")
    lines.append("")

    for iid, payload in sorted(records.items()):
        recs = payload["orders"]
        lines.append(f"## Instrument {iid}")
        lines.append("")
        lines.append(f"Parent orders: {len(recs)} | market trades on tape: "
                     f"{payload['n_market_trades']} | timeline states: "
                     f"{payload['n_states']} | crossed skipped: "
                     f"{payload['crossed_states_skipped']} | halts: "
                     f"{payload['n_halts']}")
        lines.append("")
        lines.append("### Per-order implementation shortfall (Perold)")
        lines.append("")
        lines.append(
            "| order | side | fill rate | delay bps | trading bps | "
            "opportunity bps | total IS bps | arrival slip bps | "
            "VWAP slip bps | TWAP slip bps |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in recs:
            p = r["perold"]
            lines.append(
                f"| {r['order_id']} | {r['side']} | {p['fill_rate']:.2f} | "
                f"{_fmt(p['delay_bps'])} | {_fmt(p['trading_bps'])} | "
                f"{_fmt(p['opportunity_bps'])} | {_fmt(p['total_is_bps'])} | "
                f"{_fmt(r['arrival_slippage_bps'])} | "
                f"{_fmt(r['vwap_slippage_bps'])} | "
                f"{_fmt(r['twap_slippage_bps'])} |")
        lines.append("")

        lines.append("### Aggregates")
        lines.append("")
        agg = {
            "mean fill rate": _mean([r["perold"]["fill_rate"] for r in recs]),
            "mean total IS bps": _mean(
                [r["perold"]["total_is_bps"] for r in recs]),
            "mean delay bps": _mean([r["perold"]["delay_bps"] for r in recs]),
            "mean trading bps": _mean(
                [r["perold"]["trading_bps"] for r in recs]),
            "mean opportunity bps": _mean(
                [r["perold"]["opportunity_bps"] for r in recs]),
            "mean arrival slippage bps": _mean(
                [r["arrival_slippage_bps"] for r in recs]),
            "mean exec alpha vs VWAP bps": _mean(
                [r["execution_alpha_vs_vwap_bps"] for r in recs]),
        }
        lines.append("| metric | value |")
        lines.append("|---|---|")
        for k, v in agg.items():
            lines.append(f"| {k} | {_fmt(v)} |")
        lines.append("")

        lines.append("### Adverse selection (post-fill markout, mean bps)")
        lines.append("")
        lines.append("| delta | mean markout bps | fills defined |")
        lines.append("|---|---|---|")
        for delta in ("100ms", "1s", "10s"):
            n_def = sum(r["adverse_selection_n"][delta] for r in recs)
            n_all = sum(r["n_fills"] for r in recs)
            lines.append(
                f"| {delta} | "
                f"{_fmt(_mean([r['adverse_selection_bps'][delta] for r in recs]))} "
                f"| {n_def} / {n_all} |")
        lines.append("")
        lines.append(
            "Markout = side * (mid(t_fill + delta) - fill px) / fill px over "
            "the fills whose markout is defined (timeline reaches t_fill + "
            "delta, no HALT inside the window — pinned §2.5); negative "
            "values mean the price reverted after our marketable fills (we "
            "paid temporary impact), positive means continued adverse "
            "drift.")
        lines.append("")

        imp = payload["impact_regression"]
        lines.append("### Impact estimate (signed fill cost vs participation)")
        lines.append("")
        if imp is None:
            lines.append("Not enough fills for the regression.")
        else:
            lines.append("| slope (bps per unit participation) | intercept "
                         "bps | R^2 | n fills |")
            lines.append("|---|---|---|---|")
            lines.append(
                f"| {_fmt(imp['slope_bps_per_participation'])} | "
                f"{_fmt(imp['intercept_bps'])} | {_fmt(imp['r2'])} | "
                f"{int(imp['n'])} |")
        lines.append("")

    lines.append("## Execution-alpha attribution note")
    lines.append("")
    lines.append(
        "Per order, trading cost is attributed as trading = spread + impact "
        "+ timing (timing = trading cost minus executed cost vs fill-time "
        "mid); total IS = delay + trading + opportunity (identity, tested "
        "to 1e-9). Execution alpha vs VWAP is the negative of VWAP "
        "slippage: positive means the schedule beat the market VWAP over "
        "its own interval.")
    lines.append("")
    return "\n".join(lines)


def generate_report(out_dir: Optional[Path] = None,
                    golden_dir: Optional[Path] = None) -> Path:
    """Write TCA_REPORT.md (+ tca_orders.json); returns the report path."""
    odir = Path(out_dir) if out_dir is not None else _REPO / "research" / "tca"
    odir.mkdir(parents=True, exist_ok=True)
    records = compute_tca_records(golden_dir)
    report = render_report(records)
    path = odir / "TCA_REPORT.md"
    path.write_text(report)
    with open(odir / "tca_orders.json", "w") as f:
        json.dump({str(k): v for k, v in records.items()}, f, indent=2,
                  sort_keys=True)
        f.write("\n")
    return path
