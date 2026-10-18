"""Monitoring report: replaces the naive "live Sharpe / backtest Sharpe" ratio.

A ratio of two Sharpes hides everything that matters at paper or live
stage: whether the inputs are fresh and complete, the period covered,
whether costs match what was expected, how much of each limit is used,
whether orders reconcile, and whether the sample can tell a decay from
noise at all.  :func:`build_report` lays those out from an
:class:`~iap.monitoring.machine.Evaluation` and its snapshot;
:func:`render_markdown` prints it.  Both are pure functions.
"""

from __future__ import annotations

from typing import Any

from .config import MonitoringConfig
from .inputs import NS_PER_S, Snapshot
from .machine import Evaluation

__all__ = ["REPORT_VERSION", "build_report", "render_markdown"]

REPORT_VERSION = 1


def _age_s(as_of: int | None, now: int) -> float | None:
    return None if as_of is None else (now - as_of) / NS_PER_S


def build_report(cfg: MonitoringConfig, snap: Snapshot, ev: Evaluation) -> dict[str, Any]:
    now = snap.evaluated_at_ns
    s = snap.sessions
    exp = snap.expectation
    fresh = {
        "sessions": _age_s(s[-1].as_of_ns if s else None, now),
        "positions": _age_s(snap.positions.as_of_ns if snap.positions else None, now),
        "orders": _age_s(snap.orders.as_of_ns if snap.orders else None, now),
        "heartbeat": _age_s(snap.heartbeat_ns, now),
    }
    freshness = {
        c: {"age_s": a, "limit_s": cfg.freshness_s[c], "ok": not ev.input_problems.get(c)}
        for c, a in fresh.items()
    }
    pnl: dict[str, Any] = {}
    costs: dict[str, Any] = {}
    if s:
        try:
            pnl = {
                "gross": float(sum(o.gross_pnl for o in s)),
                "net": float(sum(o.net_pnl for o in s)),
                "net_per_session": float(sum(o.net_pnl for o in s)) / len(s),
                "expected_net_per_session": exp.net_pnl_mean if exp else None,
                "window": ev.metrics.get("net_pnl"),
            }
            costs = {
                "realised_bps_mean": float(sum(o.realised_cost_bps for o in s)) / len(s),
                "expected_bps_mean": float(sum(o.expected_cost_bps for o in s)) / len(s),
                "window": ev.metrics.get("costs"),
            }
        except TypeError:
            pnl = {"error": "session values invalid; see input problems"}
    orders = None
    if snap.orders is not None:
        orders = {
            "open_orders": snap.orders.open_orders,
            "reconciliation_exceptions": snap.orders.reconciliation_exceptions,
            "source": "REL-18 reconciliation hook",
        }
    return {
        "report_version": REPORT_VERSION,
        "policy_id": cfg.policy_id,
        "config_sha256": cfg.sha256,
        "evaluated_at_ns": now,
        "state": ev.state.value,
        "required_action": ev.action,
        "expectation_source": exp.source_id if exp else None,
        "data": {
            "freshness": freshness,
            "completeness_min": min((o.completeness for o in s), default=None) if s else None,
            "input_problems": {k: v for k, v in ev.input_problems.items() if v},
        },
        "period": {
            "first_session": s[0].session if s else None,
            "last_session": s[-1].session if s else None,
            "n_sessions": len(s),
            "min_sessions": cfg.min_sessions,
            "window_sessions": cfg.window_sessions,
        },
        "pnl": pnl,
        "costs": costs,
        "turnover": ev.metrics.get("turnover"),
        "exposure": ev.metrics.get("exposure"),
        "drawdown": ev.metrics.get("drawdown"),
        "signal_strength": ev.metrics.get("signal_strength"),
        "fill_quality": ev.metrics.get("fill_quality"),
        "orders": orders,
        "alerts": [a.to_dict() for a in ev.alerts],
    }


def _f(v: Any, nd: int = 3) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [
        f"# Monitoring report: state {rep['state']}",
        "",
        f"- Required action: {rep['required_action']}",
        f"- Policy: {rep['policy_id']} (sha256 {rep['config_sha256'][:12]})",
        f"- Expectation source: {_f(rep['expectation_source'])}",
        f"- Period: {_f(rep['period']['first_session'])} .. {_f(rep['period']['last_session'])}"
        f" ({rep['period']['n_sessions']} sessions; min {rep['period']['min_sessions']})",
        "",
        "## Data freshness and completeness",
        "",
        "| channel | age s | limit s | ok |",
        "|---|---|---|---|",
    ]
    for c, f in rep["data"]["freshness"].items():
        lines.append(
            f"| {c} | {_f(f['age_s'], 0)} | {_f(f['limit_s'], 0)} | {'yes' if f['ok'] else 'NO'} |"
        )
    lines.append(f"\nMinimum completeness: {_f(rep['data']['completeness_min'])}")
    for chan, reasons in rep["data"]["input_problems"].items():
        lines.append(f"- {chan}: " + "; ".join(reasons))
    p = rep["pnl"]
    lines += ["", "## P&L and costs", ""]
    if p and "error" not in p:
        w = p.get("window") or {}
        lines += [
            f"- Gross {_f(p['gross'], 2)}, net {_f(p['net'], 2)}; net per session {_f(p['net_per_session'], 2)}"
            f" vs registered {_f(p['expected_net_per_session'], 2)}",
            f"- Window mean {_f(w.get('window_mean'), 2)}, 95 % CI {_f(w.get('ci95'))},"
            f" MDE {_f(w.get('mde'), 2)} (a decay smaller than the MDE cannot be told from noise)",
        ]
        c = rep["costs"]
        lines.append(
            f"- Costs: realised {_f(c['realised_bps_mean'], 2)} bps vs expected {_f(c['expected_bps_mean'], 2)} bps"
        )
    else:
        lines.append("- not available")
    lines += ["", "## Exposure, drawdown, orders", ""]
    for key in ("exposure", "drawdown", "turnover", "signal_strength", "fill_quality"):
        m = rep[key]
        lines.append(
            f"- {key}: "
            + (
                "n/a"
                if m is None
                else f"level {m['level']}, "
                + ", ".join(
                    f"{k} {_f(v)}"
                    for k, v in m.items()
                    if k not in ("level", "persisted_level") and not isinstance(v, dict)
                )
            )
        )
    o = rep["orders"]
    lines.append(
        "- orders: n/a"
        if o is None
        else f"- orders: {o['open_orders']} open, {o['reconciliation_exceptions']} reconciliation exceptions"
    )
    lines += ["", "## Alerts", ""]
    if not rep["alerts"]:
        lines.append("None.")
    for a in rep["alerts"]:
        lines.append(f"- **{a['severity']}** {a['metric']}: {a['action']}")
    return "\n".join(lines) + "\n"
