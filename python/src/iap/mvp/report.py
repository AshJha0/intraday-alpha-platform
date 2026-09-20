"""``report.json`` (canonical, sorted keys) and ``report.md`` of one MVP run.

The report is a pure function of the finished :class:`~iap.mvp.engine.MvpEngine`
and the feed identity: no path, no wall clock, no environment fact enters
it, so a replay from the captured stream produces the identical document
and the golden pins its numbers.  Every float is checked finite
(:func:`check_finite` raises otherwise); a statistic that is *undefined*
(an IC over fewer than three samples) is ``null``, never a fabricated
number.  Money is in the reporting currency (USD), bps figures are basis
points of the relevant notional (see each key's docstring below).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from iap.contracts.types import Algo
from iap.contracts.versions import canonical_json
from iap.lifecycle.registry import AlphaRegistry
from iap.mvp.engine import IcResult, MvpEngine, OrderOutcome
from iap.mvp.feed import FeedResult
from iap.risk.events import Rules

__all__ = ["IC_DEFINITION", "REPORT_VERSION", "build_report", "check_finite",
           "render_markdown", "research_ic_of", "report_json"]

REPORT_VERSION = 2


def check_finite(obj: Any, path: str = "$") -> None:
    """Raise ``ValueError`` on any non-finite float anywhere in ``obj``."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"report: non-finite number at {path}: {obj!r}")
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            check_finite(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            check_finite(v, f"{path}[{i}]")


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _weighted(values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    total = sum(weights)
    if total <= 0.0:
        return None
    return sum(v * w for v, w in zip(values, weights)) / total


def _latency_block(values: Sequence[int]) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "min": 0, "mean": 0.0, "max": 0, "p50": 0, "p99": 0}
    s = sorted(values)
    n = len(s)

    def rank(q: float) -> int:
        return s[min(n - 1, max(0, int(math.ceil(q * n)) - 1))]

    return {"count": n, "min": s[0], "mean": sum(s) / n, "max": s[-1],
            "p50": rank(0.5), "p99": rank(0.99)}


#: How every realized IC in the report is defined (echoed in ``report.json``).
IC_DEFINITION = (
    "Pearson(expected_return, label) over confidence > 0 decisions; label = "
    "iap.labels.compute_labels at the decision timestamps on the feature-engine "
    "book-refresh mid series (mid-to-mid and cost-adjusted half-spread at both ends; "
    "valid only when observed through t+h, tradable anchor, fresh tradable forward "
    "sample, no stale/halt/auction/one-sided refresh inside (t, t+h]); ic_shifted = "
    "signal lagged one confidence > 0 decision; ic_gap = |at_research_horizon.ic - "
    "research_ic| (the registry IC is measured at the alpha's fitted horizon)"
)


def _ic_block(ic: IcResult) -> Dict[str, Any]:
    """The report keys of one :class:`IcResult`."""
    return {"horizon": ic.horizon, "realized_ic": ic.ic, "realized_ic_cost": ic.ic_cost,
            "realized_ic_shifted": ic.ic_shifted, "n_ic_samples": ic.n,
            "n_signals": ic.n_signals}


def research_ic_of(registry: AlphaRegistry, alpha_id: str) -> Optional[float]:
    """The research IC the lifecycle registry evaluated for ``alpha_id`` (the
    ``oos_ic`` gate value of its last evaluation), ``None`` when absent."""
    if alpha_id not in registry:
        return None
    record = registry.get(alpha_id)
    ev = record.last_evaluation
    if ev is None or "oos_ic" not in ev.gates:
        return None
    return ev.gates["oos_ic"].value


def _execution_block(outcomes: Sequence[OrderOutcome]) -> Dict[str, Any]:
    with_tca = [o for o in outcomes if o.tca is not None]
    qty = [float(o.tca.qty) for o in with_tca]
    filled = [float(o.tca.filled_qty) for o in with_tca]
    is_bps = [o.tca.implementation_shortfall_bps for o in with_tca]

    def qw(attr: str) -> Optional[float]:
        return _weighted([getattr(o.tca, attr) for o in with_tca], qty)

    def fw(attr: str) -> Optional[float]:
        return _weighted([getattr(o.tca, attr) for o in with_tca], filled)

    per_algo: Dict[str, Any] = {}
    for algo in Algo:
        group = [o for o in with_tca if o.parent.algo is algo]
        if not group:
            continue
        g_qty = [float(o.tca.qty) for o in group]
        per_algo[algo.value] = {
            "n_orders": len(group),
            "qty": int(sum(g_qty)),
            "filled_qty": int(sum(o.tca.filled_qty for o in group)),
            "fill_rate": sum(o.tca.filled_qty for o in group) / sum(g_qty),
            "implementation_shortfall_bps_qty_weighted": _weighted(
                [o.tca.implementation_shortfall_bps for o in group], g_qty),
            "slippage_bps_filled_weighted": _weighted(
                [o.tca.slippage_bps for o in group], [float(o.tca.filled_qty) for o in group]),
        }
    latencies = [lat for o in outcomes for lat in o.latencies_ns]
    total_qty = sum(o.parent.qty for o in outcomes)
    total_filled = sum(o.tca.filled_qty for o in with_tca)
    residuals = [o.realized_bps - o.attribution.total_bps for o in with_tca
                 if o.attribution is not None]
    return {
        "n_parent_orders": len(outcomes),
        "n_orders_with_tca": len(with_tca),
        "qty_target": int(total_qty),
        "qty_filled": int(total_filled),
        "fill_rate": (total_filled / total_qty) if total_qty else 0.0,
        "implementation_shortfall_bps": {"mean": _mean(is_bps),
                                         "qty_weighted": _weighted(is_bps, qty)},
        "delay_cost_bps_qty_weighted": qw("delay_cost_bps"),
        "trading_cost_bps_qty_weighted": qw("trading_cost_bps"),
        "opportunity_cost_bps_qty_weighted": qw("opportunity_cost_bps"),
        "spread_cost_bps_qty_weighted": qw("spread_cost_bps"),
        "impact_bps_qty_weighted": qw("impact_bps"),
        "fees_bps_qty_weighted": qw("fees_bps"),
        "timing_cost_bps_qty_weighted": qw("timing_cost_bps"),
        "slippage_bps_filled_weighted": fw("slippage_bps"),
        "participation_rate_mean": _mean([o.tca.participation_rate for o in with_tca]),
        "latency_decision_to_arrival_ns": _latency_block(latencies),
        "per_algo": per_algo,
        "attribution_residual_bps_mean": _mean(residuals),
        "realized_bps_filled_weighted": _weighted(
            [o.realized_bps for o in with_tca], [o.filled_notional for o in with_tca]),
    }


def build_report(engine: MvpEngine, feed: FeedResult, trace_digest: str,
                 registry: AlphaRegistry) -> Dict[str, Any]:
    """The report document (see module docstring).  Raises on a non-finite number."""
    if not engine.counters.events:
        raise ValueError("report: the engine processed no events")
    cfg = engine.cfg
    acct = engine.account
    counters = engine.counters
    risk_daily = engine.risk_daily_pnl()
    identity_rhs = acct.gross_pnl - acct.spread_cost
    pnl_total = risk_daily - acct.fees_net - acct.impact
    outcomes = engine.outcomes
    with_tca = [o for o in outcomes if o.tca is not None and o.attribution is not None]
    notional = [o.filled_notional for o in with_tca]
    alpha_contribution = _weighted([o.attribution.alpha_bps for o in with_tca], notional)
    cost_bps = _weighted([o.attribution.spread_bps + o.attribution.impact_bps
                          + o.attribution.fees_bps + o.attribution.timing_bps
                          for o in with_tca], notional)
    venue_fill_qty: Dict[str, int] = {name: 0 for name in engine.venue_names.values()}
    for o in outcomes:
        for vid, q in o.venue_qty.items():
            venue_fill_qty[engine.venue_names[vid]] += q
    total_filled = sum(venue_fill_qty.values())
    venue_shares = {name: (q / total_filled if total_filled else 0.0)
                    for name, q in venue_fill_qty.items()}
    by_rule = engine.risk_decisions_by_rule()
    per_alpha: Dict[str, Any] = {}
    for alpha in engine.alphas:
        at_mvp = engine.realized_ic(alpha.alpha_id)
        at_fit = engine.realized_ic(alpha.alpha_id, alpha.model.horizon)
        research = research_ic_of(registry, alpha.alpha_id)
        per_alpha[alpha.alpha_id] = {
            **_ic_block(at_mvp),
            "research_horizon": alpha.model.horizon,
            "at_research_horizon": _ic_block(at_fit),
            "research_ic": research,
            "ic_gap": (abs(at_fit.ic - research)
                       if at_fit.ic is not None and research is not None else None),
            "lifecycle_state": (registry.get(alpha.alpha_id).state.name
                                if alpha.alpha_id in registry else None),
            "beta": alpha.beta, "params_version": alpha.version,
        }
    ens = engine.realized_ic(engine.ensemble.alpha_id)
    report: Dict[str, Any] = {
        "x-version": REPORT_VERSION,
        "run": {
            "run_id": cfg.run_id, "session_id": engine.session_id, "seed": cfg.seed,
            "instrument": cfg.instrument, "instrument_id": engine.iid,
            "strategy_id": engine.strategy_id, "alphas": list(cfg.alphas),
            "venues": list(cfg.venues), "config_version": engine.config_version,
            "data_version": engine.data_version, "events_sha256": feed.events_sha256,
            "feature_version": engine.feature_version, "model_version": engine.model_version,
            "trace_digest": trace_digest, "n_traces": len(engine.trace_ids),
            "first_ts": engine.first_ts, "last_ts": engine.last_ts,
            "horizon_ns": cfg.horizon_ns, "decision_cadence_ns": cfg.decision_cadence_ns,
        },
        "counts": {
            "n_events": counters.events, "n_events_generated": feed.n_events_generated,
            "n_decisions": counters.decisions, "n_parent_orders": counters.parent_orders,
            "n_child_orders_generated": counters.child_orders_generated,
            "n_child_orders_submitted": counters.child_orders_submitted,
            "n_fills": counters.fills,
            "fill_rate": ((sum(o.tca.filled_qty for o in outcomes if o.tca is not None)
                           / sum(o.parent.qty for o in outcomes)) if outcomes else 0.0),
            "counters": counters.to_dict(),
            "qc_totals": dict(sorted(feed.qc_totals.items())),
            "simulator": engine.sim.simulator.counters.to_dict(),
            "features": {
                "events_dropped": engine.features.events_dropped,
                "ts_regressions_dropped": engine.features.ts_regressions_dropped,
                "vectors_emitted": engine.features.vectors_emitted,
            },
        },
        "risk": {
            "decisions_by_rule": by_rule,
            "allowed": counters.risk_allowed, "rejected": counters.risk_rejected,
            "kill_events": engine.n_kill_events(),
            "kill_switch_engaged": engine.risk_engine.kill_switch_engaged(),
            "audit_events": engine.risk_engine.audit_len(),
            "sequence_gaps": counters.sequence_gaps, "feed_recoveries": counters.feed_recoveries,
            "market_regressions_dropped": engine.risk_engine.metrics.counter_value(
                "risk_market_regressions_dropped_total"),
            "final_position": engine.risk_engine.position(engine.iid),
            "open_orders": engine.risk_engine.open_order_count(),
        },
        "routing": {
            "no_route": counters.sor_no_route,
            "venue_fill_qty": venue_fill_qty,
            "venue_shares": venue_shares,
        },
        "controls": {
            "max_child_qty": engine.controls.max_child_qty,
            "max_participation": engine.controls.max_participation,
            "min_slice_interval_ns": engine.controls.min_slice_interval_ns,
            "latency_budget_ns": engine.controls.latency_budget_ns,
            "participation_capped": counters.participation_capped,
            "participation_blocked": counters.participation_blocked,
            "slice_interval_blocked": counters.slice_interval_blocked,
            "latency_budget_blocked": counters.latency_budget_blocked,
        },
        "pnl": {
            "total": pnl_total,
            "risk_daily": risk_daily,
            "gross": acct.gross_pnl,
            "spread_cost": acct.spread_cost,
            "fees_net": acct.fees_net,
            "fees": acct.fees, "rebates": acct.rebates,
            "impact": acct.impact,
            "execution_cost": acct.spread_cost + acct.fees_net + acct.impact,
            "identity_lhs_risk_daily": risk_daily,
            "identity_rhs_gross_minus_spread": identity_rhs,
            "identity_abs_diff": abs(risk_daily - identity_rhs),
            "realized": engine.risk_engine.realized_pnl(),
            "unrealized": engine.risk_engine.unrealized_pnl(),
            "equity": acct.equity(),
            "max_drawdown": acct.max_drawdown,
            "final_position": acct.position,
            "final_mark": acct.mark,
            "traded_qty": acct.filled_qty,
            "session_volume": acct.session_volume,
            "reporting_ccy": "USD",
        },
        "alpha": {
            "contribution_bps_notional_weighted": alpha_contribution,
            "cost_bps_notional_weighted": cost_bps,
            "net_bps_notional_weighted": (alpha_contribution + cost_bps
                                          if alpha_contribution is not None and cost_bps is not None
                                          else None),
            "per_alpha": per_alpha,
            "ensemble": {"alpha_id": engine.ensemble.alpha_id, **_ic_block(ens),
                         "model_version": engine.model_version},
            "ic_definition": IC_DEFINITION,
            "cost_negative": (pnl_total < 0.0),
        },
        "execution": _execution_block(outcomes),
        "portfolio": {
            "solves": engine.portfolio.solves,
            "infeasible_solves": engine.portfolio.infeasible_solves,
            "bars": len(engine.bar_returns),
            "portfolio_version": engine.portfolio.portfolio_version(engine.constraints),
        },
        "trace": {
            "digest": trace_digest,
            "n_traces": len(engine.trace_ids),
            "first_trace_ids": engine.trace_ids[:3],
            "last_trace_ids": engine.trace_ids[-3:],
        },
    }
    check_finite(report)
    if abs(pnl_total - (identity_rhs - acct.fees_net - acct.impact)) > 1e-9 * max(
            1.0, abs(pnl_total)):
        raise ValueError("report: pnl.total != (gross - spread) - fees_net - impact")
    if Rules.ALLOW in by_rule and by_rule[Rules.ALLOW] != counters.risk_allowed:
        raise ValueError("report: allowed risk decisions disagree with the audit")
    return report


def report_json(report: Mapping[str, Any]) -> str:
    """Canonical (sorted keys, compact) JSON + newline."""
    return canonical_json(dict(report)) + "\n"


def _fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.{digits}f}"
    if isinstance(v, int):
        return f"{v:,d}"
    return str(v)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           align: str = "right") -> List[str]:
    """A Markdown table: header row, alignment row, one line per row."""
    sep = "---:" if align == "right" else "---"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join([sep] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    """Human-readable ``report.md`` (numbers rounded for reading only)."""
    r = report["run"]
    c = report["counts"]
    p = report["pnl"]
    e = report["execution"]
    a = report["alpha"]
    k = report["risk"]
    rt = report["routing"]
    ct = report["controls"]
    t = report["trace"]
    lat = e["latency_decision_to_arrival_ns"]
    is_ = e["implementation_shortfall_bps"]
    ens = a["ensemble"]
    verdict = "COST-NEGATIVE" if a["cost_negative"] else "net positive"
    out: List[str] = []
    out += [f"# MVP run `{r['run_id']}` — {r['instrument']} (instrument {r['instrument_id']})", ""]
    out += [f"Session `{r['session_id']}`, seed {r['seed']}, alphas {', '.join(r['alphas'])}, "
            f"venues {', '.join(r['venues'])}, holding horizon {r['horizon_ns'] / 1e9:g} s, "
            f"decision cadence {r['decision_cadence_ns'] / 1e9:g} s.", ""]
    out += ["## Versions", ""]
    out += _table(["what", "value"], [
        [key, f"`{r[key]}`"] for key in ("config_version", "data_version", "events_sha256",
                                         "feature_version", "model_version", "trace_digest")],
        align="left")
    out += ["", "## Counts", ""]
    out += _table(["events", "decisions", "parent orders", "children generated",
                   "children submitted", "fills", "fill rate"],
                  [[_fmt(c["n_events"]), _fmt(c["n_decisions"]), _fmt(c["n_parent_orders"]),
                    _fmt(c["n_child_orders_generated"]), _fmt(c["n_child_orders_submitted"]),
                    _fmt(c["n_fills"]), _fmt(c["fill_rate"])]])
    out += ["", "Decision outcomes: " + ", ".join(
        f"{key} = {c['counters'][key]}" for key in (
            "decisions_without_covariance", "decisions_flat", "decisions_parent_live",
            "decisions_window_beyond_session")), ""]
    out += ["## Risk", ""]
    out += _table(["rule", "decisions"],
                  [[rule, str(n)] for rule, n in k["decisions_by_rule"].items()])
    out += ["", f"Allowed {k['allowed']}, rejected {k['rejected']}, kill events "
            f"{k['kill_events']}, kill switch engaged: {_fmt(k['kill_switch_engaged'])}, "
            f"sequence gaps "
            f"{k['sequence_gaps']}, feed recoveries {k['feed_recoveries']}, "
            f"final position {k['final_position']}, open orders {k['open_orders']}.", ""]
    out += ["## Routing and controls", ""]
    out += _table(["venue", "filled qty", "share"],
                  [[name, _fmt(q), f"{rt['venue_shares'][name] * 100:.1f}%"]
                   for name, q in rt["venue_fill_qty"].items()])
    out += ["", f"NO_ROUTE: {rt['no_route']}; participation capped {ct['participation_capped']}, "
            f"blocked {ct['participation_blocked']}; slice-interval blocked "
            f"{ct['slice_interval_blocked']}; latency-budget blocked "
            f"{ct['latency_budget_blocked']} (max_participation {ct['max_participation']}, "
            f"min_slice_interval {ct['min_slice_interval_ns'] / 1e6:g} ms, latency budget "
            f"{ct['latency_budget_ns'] / 1e6:g} ms, max_child_qty {ct['max_child_qty']}).", ""]
    out += ["## P&L (USD, PLATFORM_CONVENTIONS §12.1)", ""]
    out += _table(["total", "risk daily (realized + unrealized)", "gross", "spread cost",
                   "fees net", "impact", "execution cost", "identity abs diff"],
                  [[_fmt(p["total"], 2), _fmt(p["risk_daily"], 2), _fmt(p["gross"], 2),
                    _fmt(p["spread_cost"], 2), _fmt(p["fees_net"], 2), _fmt(p["impact"], 2),
                    _fmt(p["execution_cost"], 2), f"{p['identity_abs_diff']:.2e}"]])
    out += ["", f"`pnl.total == (gross - spread_cost) - fees_net - impact` holds; realized "
            f"{_fmt(p['realized'], 2)}, unrealized {_fmt(p['unrealized'], 2)}, max drawdown "
            f"{_fmt(p['max_drawdown'], 2)}, traded {_fmt(p['traded_qty'])} shares against a "
            f"session volume of {_fmt(p['session_volume'])}, final position "
            f"{p['final_position']}.", "",
            f"**Honest result: the session is {verdict}** (total {_fmt(p['total'], 2)} USD).", ""]
    out += ["## Alpha", ""]
    out += [f"Alpha contribution (notional-weighted, bps): "
            f"{_fmt(a['contribution_bps_notional_weighted'])}; execution cost (bps): "
            f"{_fmt(a['cost_bps_notional_weighted'])}; net (bps): "
            f"{_fmt(a['net_bps_notional_weighted'])}.", ""]
    h = ens["horizon"]
    alpha_rows = [[aid, str(row["lifecycle_state"]), _fmt(row["realized_ic"]),
                   _fmt(row["realized_ic_cost"]), _fmt(row["realized_ic_shifted"]),
                   str(row["n_ic_samples"]), row["research_horizon"],
                   _fmt(row["at_research_horizon"]["realized_ic"]),
                   _fmt(row["at_research_horizon"]["realized_ic_cost"]),
                   str(row["at_research_horizon"]["n_ic_samples"]),
                   _fmt(row["research_ic"]), _fmt(row["ic_gap"])]
                  for aid, row in a["per_alpha"].items()]
    alpha_rows.append([f"{ens['alpha_id']} (ensemble)", "—", _fmt(ens["realized_ic"]),
                       _fmt(ens["realized_ic_cost"]), _fmt(ens["realized_ic_shifted"]),
                       str(ens["n_ic_samples"]), "—", "—", "—", "—", "—", "—"])
    out += _table(["alpha", "lifecycle", f"IC@{h} mid", f"IC@{h} cost", f"IC@{h} shift-1", "n",
                   "fitted h", "IC@h mid", "IC@h cost", "n@h", "research IC@h", "gap"],
                  alpha_rows)
    out += ["", "Realized IC definition: " + a["ic_definition"] + ".", ""]
    out += ["", "## Execution (TCA)", ""]
    out += [f"Orders with TCA {e['n_orders_with_tca']} / {e['n_parent_orders']}; target "
            f"{_fmt(e['qty_target'])}, filled {_fmt(e['qty_filled'])} "
            f"(fill rate {_fmt(e['fill_rate'])}).", ""]
    out += _table(["IS mean", "IS qty-weighted", "delay", "trading", "opportunity", "spread",
                   "impact", "fees", "timing", "slippage"],
                  [[_fmt(is_["mean"]), _fmt(is_["qty_weighted"]),
                    _fmt(e["delay_cost_bps_qty_weighted"]),
                    _fmt(e["trading_cost_bps_qty_weighted"]),
                    _fmt(e["opportunity_cost_bps_qty_weighted"]),
                    _fmt(e["spread_cost_bps_qty_weighted"]), _fmt(e["impact_bps_qty_weighted"]),
                    _fmt(e["fees_bps_qty_weighted"]), _fmt(e["timing_cost_bps_qty_weighted"]),
                    _fmt(e["slippage_bps_filled_weighted"])]])
    out += ["", f"Participation (mean) {_fmt(e['participation_rate_mean'])}; decision->arrival "
            f"latency over {lat['count']} children: min {lat['min']} ns, p50 {lat['p50']} ns, "
            f"p99 {lat['p99']} ns, max {lat['max']} ns; attribution residual (mean bps) "
            f"{_fmt(e['attribution_residual_bps_mean'])}.", ""]
    out += _table(["algo", "orders", "qty", "filled", "fill rate", "IS qty-weighted"],
                  [[algo, str(row["n_orders"]), _fmt(row["qty"]), _fmt(row["filled_qty"]),
                    _fmt(row["fill_rate"]), _fmt(row["implementation_shortfall_bps_qty_weighted"])]
                   for algo, row in e["per_algo"].items()])
    out += ["", "## Decision trace", ""]
    out += [f"{t['n_traces']} traces, digest `{t['digest']}`; first ids "
            f"{', '.join(t['first_trace_ids'])}; last ids {', '.join(t['last_trace_ids'])}.", ""]
    return "\n".join(out)
