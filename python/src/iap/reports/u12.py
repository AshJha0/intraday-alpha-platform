"""U12: the Track C economic report, C0-C2 on development data (v2.0 U0 stage 9; plan section 6).

One report compares the configurations that ran on the **same** development
panel and execution model:

- per configuration, the E5 session series of the whole book (R_d = NetPnL_d /
  C, no-trade sessions as zero, one denominator) and its economic screen
  (:func:`iap.reports.e5.economic_screen`, the conservative column);
- the **incremental net value** of C1 and C2 over the attribution baseline C0:
  the paired per-session difference ``R_d(Cx) - R_d(C0)`` in bps of C with its
  mean, standard error, 95 % interval and session count (sessions are paired by
  id; a session missing from either series refuses the report);
- the cost and netting lines that explain the difference: external charge,
  latency drift, crossed and external shares, fill count, refusals, flat optima,
  joint checks, shared-name gross (the cross-horizon overlap diagnostic);
- per-family net values (C0 and C1, from :mod:`iap.portfolio.netting`) and the
  conservation result of every ledger (the report refuses to build when any
  invariant fails);
- the comparison recorded as a **model-selection event** (plan section 6:
  "recorded as model selection"): partition ``development``, the
  configurations compared, their identities and the outcome exposure.

:func:`freeze_configuration` records the one frozen configuration chosen
after the comparison.  U0 gives no selection rule (open decision TC-OD-1), so
the choice and its written rationale are the caller's; the record hashes the
report it was chosen from.  Everything is canonical JSON: a rerun on the same
inputs is byte-identical.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from iap.contracts.versions import canonical_json, content_hash
from iap.portfolio.netting import as_document, check_conservation
from iap.reports.e5 import check_no_inflation, economic_screen, session_series

__all__ = [
    "U12Error",
    "build_report",
    "freeze_configuration",
    "incremental_net_value",
    "report_json",
]

BASELINE = "C0"
#: The Track C configurations (plan section 6); the runs come from :mod:`iap.alpha.combined_book`.
CONFIGURATIONS: tuple[str, ...] = ("C0", "C1", "C2")
OPEN_DECISIONS: dict[str, str] = {
    "TC-OD-1": "the rule that selects the one frozen configuration from the C0-C2 comparison",
    "TC-OD-2": (
        "the C2 protocol values (lambda, kappa, covariance estimator and window, combination "
        "weights): fixed by a written development protocol, not supplied by the software"
    ),
    "TC-OD-3": (
        "whether the incremental-net-value interval uses the iid standard error (as here) or a "
        "block bootstrap matching the U13 dependence domain"
    ),
}


class U12Error(ValueError):
    """The comparison cannot be reported (missing configuration, unpaired sessions, broken invariant)."""


def incremental_net_value(
    base: Sequence[tuple[str, float]], other: Sequence[tuple[str, float]]
) -> dict[str, Any]:
    """Paired ``other - base`` per session in bps of C: mean, se, 95 % interval, n."""
    b, o = dict(base), dict(other)
    if len(b) != len(base) or len(o) != len(other):
        raise U12Error("duplicate session ids")
    if set(b) != set(o):
        raise U12Error(
            f"configurations cover different sessions: only base {sorted(set(b) - set(o))}, "
            f"only other {sorted(set(o) - set(b))}"
        )
    ids = [s for s, _ in base]
    d = np.array([o[s] - b[s] for s in ids], dtype=float)
    n = int(d.size)
    mean = float(d.mean()) if n else 0.0
    se = float(d.std(ddof=1) / math.sqrt(n)) if n > 1 else None
    return {
        "mean_bps": mean,
        "se_bps": se,
        "interval_95_bps": [mean - 1.96 * se, mean + 1.96 * se] if se is not None else None,
        "n_sessions": n,
        "total_bps": float(d.sum()),
        "pairing": "by session id",
        "uncertainty": "iid standard error of the paired difference (TC-OD-3)",
    }


def _waterfall(run: Any) -> dict[str, float]:
    keys = (
        "midpoint_reference_pnl",
        "latency_drift",
        "half_spread",
        "impact",
        "fees",
        "execution_residual",
        "financing",
    )
    out = {k: 0.0 for k in keys}
    for _, led in sorted(run.ledgers.items()):
        for r in led.sessions:
            for k in keys:
                out[k] += r.waterfall[k]
    return out


def _config_block(run: Any, capital: float) -> dict[str, Any]:
    series = session_series("track_c", run.configuration, run.session_rows, capital)
    check_no_inflation(
        series, all_session_ids=[r.session_id for r in run.session_rows], denominators=[capital]
    )
    atts = {}
    for name, led in sorted(run.ledgers.items()):
        atts[name] = as_document(check_conservation(led))
    by_family: dict[str, dict[str, float]] = {}
    for doc in atts.values():
        for f, row in doc["by_family"].items():
            dst = by_family.setdefault(f, {k: 0.0 for k in row})
            for k, v in row.items():
                dst[k] += v
    wf = _waterfall(run)
    shared = run.shared_name_gross
    screen = economic_screen(series)
    if not math.isfinite(screen["se_bps"]):
        screen["se_bps"] = (
            None  # fewer than two sessions: no standard error (canonical JSON has no inf)
        )
    return {
        "configuration": run.configuration,
        "screen": screen,
        "r_bps": [[s, r] for s, r in zip(series.session_ids, series.r_bps, strict=True)],
        "session_counts": series.counts,
        "waterfall": wf,
        "external_charge": wf["half_spread"] + wf["impact"] + wf["fees"],
        "fills": sum(d["fills"] for d in atts.values()),
        "crossed_shares": sum(d["crossed_shares"] for d in atts.values()),
        "external_shares": sum(d["external_shares"] for d in atts.values()),
        "refusals": len(run.refusals),
        "refusal_reasons": sorted({r["reason"].split(":")[0] for r in run.refusals}),
        "flat_optima": len(run.flat_optima),
        "joint_checks": run.joint_checks,
        "shared_name_gross": {
            "mean": float(np.mean(shared)) if shared else 0.0,
            "max": float(np.max(shared)) if shared else 0.0,
            "decisions": len(shared),
            "role": "diagnostic only (overlap between horizons)",
        },
        "by_family": dict(sorted(by_family.items())),
        "conservation": "passed",
        "protocol": run.protocol,
    }


def build_report(
    runs: Mapping[str, Any],
    *,
    capital: float,
    panel_identity: str,
    partition: str = "development",
) -> dict[str, Any]:
    """The U12 document (module docstring).  C0 is required; every run must cover the same sessions."""
    if partition != "development":
        raise U12Error("the C0-C2 comparison is model selection on development data only")
    if BASELINE not in runs:
        raise U12Error("C0 (the attribution baseline) is required")
    for k, r in runs.items():
        if k not in CONFIGURATIONS or r.configuration != k:
            raise U12Error(f"run {k!r} is not a configuration of {CONFIGURATIONS}")
    blocks = {k: _config_block(runs[k], capital) for k in sorted(runs)}
    base = [tuple(x) for x in blocks[BASELINE]["r_bps"]]
    incremental = {
        k: incremental_net_value(base, [tuple(x) for x in blocks[k]["r_bps"]])
        for k in sorted(blocks)
        if k != BASELINE
    }
    return {
        "schema": "iap.reports.u12/v1",
        "title": "Track C economic report: C0-C2 on development data",
        "units": {"r_bps": "bps of registered capital C per session", "money": "currency of C"},
        "capital": capital,
        "panel_identity": panel_identity,
        "configurations": blocks,
        "incremental_net_value_vs_C0": incremental,
        "selection_event": {
            "kind": "model selection",
            "partition": partition,
            "configurations_compared": sorted(blocks),
            "outcome_exposure": "development net value of each configuration",
            "frozen_configuration": None,
        },
        "proposed_decisions": dict(sorted(runs[BASELINE].proposed_decisions.items())),
        "open_decisions": dict(OPEN_DECISIONS),
    }


def report_json(report: Mapping[str, Any]) -> str:
    return canonical_json(report)


def freeze_configuration(report: Mapping[str, Any], chosen: str, rationale: str) -> dict[str, Any]:
    """Record the one frozen configuration (TC-OD-1: the choice is the caller's, never automatic)."""
    if chosen not in report["configurations"]:
        raise U12Error(f"{chosen!r} was not compared in this report")
    if not rationale or not rationale.strip():
        raise U12Error("a frozen configuration carries its written rationale")
    block = report["configurations"][chosen]
    return {
        "kind": "frozen configuration",
        "configuration": chosen,
        "rationale": rationale,
        "report_hash": content_hash(dict(report)),
        "protocol": block["protocol"],
        "partition": report["selection_event"]["partition"],
    }
