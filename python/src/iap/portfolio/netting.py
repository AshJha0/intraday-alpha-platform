"""U11: netting across families, attribution and the conservation checks (v2.0 U0 stage 9; plan section 6).

The executable ledger (:class:`iap.portfolio.ledger.Ledger`) already nets every
cohort intent in one name at one decision time into one aggregate order,
crosses opposite intents internally at the decision mid and allocates the one
external fill by its deterministic rule.  This module reads that ledger's
event log and answers the Track C questions without changing a number in it:

- **one external charge**: every external fill is charged once (half-spread,
  impact, fees; latency drift is attribution, not cost) and the charge is split
  across the cohorts on the fill's side of the book, pro rata to their
  allocated quantity on that side, the last cohort in key order taking the
  float remainder so the split sums to the charge exactly (proposed decision
  TC-PD-3: crossed shares on the heavy side share the external charge);
- **internal crossing without fictional fills**: a cross is recorded as a
  quantity matched between cohorts at the decision mid; it creates no
  :class:`~iap.portfolio.ledger.Fill`, no fee and no cost, and the list of
  external fills is exactly the ledger's;
- **family and cohort attribution**: a cohort's net value is its
  midpoint-reference P&L plus execution residual (the ledger's
  ``signal_pnl + execution_residual``) plus its share of latency drift minus
  its share of the external charge; the families' net values sum to the book's
  NetPnL plus financing (financing is a book-level line, charged on carried
  inventory and not allocated: TC-PD-4).

:func:`check_conservation` is the fail-closed invariant set run after every
Track C book: the cash identity rebuilt from deposits, fills and financing
alone; aggregate position = sum of fills = sum of cohort allocations, per
name; per (decision, name) the allocations sum to the external fill and each
side's allocations equal crossed + that side's fill; every fill appears once
in the attribution; charges and drift split to the cohorts sum to the
waterfall's; the families' net values sum to NetPnL + financing.  Any breach
raises :class:`ConservationError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iap.portfolio.ledger import CohortKey, Ledger

__all__ = [
    "Attribution",
    "ConservationError",
    "as_document",
    "attribute",
    "check_conservation",
    "sum_documents",
]

TOL = 1e-9
_LINES = ("half_spread", "impact", "fees", "latency_drift")


class ConservationError(ValueError):
    """A Track C book broke a conservation invariant (a fill created or lost, cash not reconciled)."""


@dataclass(slots=True)
class Attribution:
    """Per-cohort and per-family split of one ledger's external charges, with the crossing record."""

    by_cohort: dict[CohortKey, dict[str, float]]
    by_family: dict[str, dict[str, float]]
    crosses: list[dict[str, Any]]
    fills: int
    crossed_shares: int
    external_shares: int
    book: dict[str, float] = field(default_factory=dict)


def _split(total: float, weights: list[tuple[CohortKey, int]]) -> dict[CohortKey, float]:
    """``total`` pro rata to ``|w|``; the last key takes the remainder so the parts sum to ``total``."""
    ws = sorted(weights)
    denom = sum(abs(w) for _, w in ws)
    out: dict[CohortKey, float] = {}
    if denom == 0:
        raise ConservationError("an external fill has no cohort allocation on its side")
    acc = 0.0
    for k, (key, w) in enumerate(ws):
        if k == len(ws) - 1:
            part = total - acc
        else:
            part = total * abs(w) / denom
            acc += part
        out[key] = out.get(key, 0.0) + part
    return out


def attribute(led: Ledger) -> Attribution:
    """Split every external fill's charge and drift over the cohorts that received it (module docstring)."""
    allocs: dict[tuple[int, str], list[tuple[CohortKey, int]]] = {}
    fills: dict[tuple[int, str], dict[str, Any]] = {}
    crosses: dict[tuple[int, str], int] = {}
    for e in led.events:
        k = e["event"]
        if k == "allocation":
            allocs.setdefault((e["ts"], e["name"]), []).append((tuple(e["cohort"]), e["qty"]))
        elif k == "fill":
            key = (e["ts"], e["name"])
            if key in fills:
                raise ConservationError(f"two external fills for {key}: one net order, one fill")
            fills[key] = e
        elif k == "cross":
            crosses[(e["ts"], e["name"])] = e["qty"]
    zero = {ln: 0.0 for ln in _LINES}
    by_cohort: dict[CohortKey, dict[str, float]] = {
        key: {**zero, "reference_pnl": c.signal_pnl, "execution_residual": c.execution_residual}
        for key, c in sorted(led.cohorts.items())
    }
    for tn in sorted(fills):
        f = fills[tn]
        side = 1 if f["qty"] > 0 else -1
        heavy = [(key, q) for key, q in allocs.get(tn, []) if q * side > 0]
        charge = {
            "half_spread": abs(f["qty"]) * f["half_spread"],
            "impact": abs(f["qty"]) * f["impact"],
            "fees": f["fee"],
            "latency_drift": -f["qty"] * (f["mid_arrival"] - f["mid_decision"]),
        }
        for ln, total in charge.items():
            for key, part in _split(total, heavy).items():
                by_cohort[key][ln] += part
    cross_rows: list[dict[str, Any]] = []
    for tn in sorted(crosses):
        legs = sorted(allocs.get(tn, []))
        cross_rows.append(
            {
                "ts": tn[0],
                "name": tn[1],
                "crossed": crosses[tn],
                "buy_cohorts": [list(k) for k, q in legs if q > 0],
                "sell_cohorts": [list(k) for k, q in legs if q < 0],
                "external_fill": fills[tn]["qty"] if tn in fills else 0,
            }
        )
    for row in by_cohort.values():
        row["net"] = (
            row["reference_pnl"]
            + row["execution_residual"]
            + row["latency_drift"]
            - row["half_spread"]
            - row["impact"]
            - row["fees"]
        )
    by_family: dict[str, dict[str, float]] = {}
    for key, row in by_cohort.items():
        fam = by_family.setdefault(key[0], {k: 0.0 for k in row})
        for k, v in row.items():
            fam[k] += v
    book = {
        "net_pnl": sum(r.net_pnl for r in led.sessions),
        "financing": sum(r.waterfall["financing"] for r in led.sessions),
    }
    return Attribution(
        by_cohort,
        dict(sorted(by_family.items())),
        cross_rows,
        len(fills),
        sum(crosses.values()),
        sum(abs(f["qty"]) for f in fills.values()),
        book,
    )


def _close(a: float, b: float, scale: float = 1.0) -> bool:
    return abs(a - b) <= TOL * max(1.0, abs(scale), abs(a), abs(b))


def check_conservation(led: Ledger, attribution: Attribution | None = None) -> Attribution:
    """Fail-closed invariants of one Track C book (module docstring); returns the attribution it checked."""
    att = attribution if attribution is not None else attribute(led)
    cash = 0.0
    pos: dict[str, int] = {}
    alloc_sum: dict[tuple[int, str], dict[str, int]] = {}
    fills: dict[tuple[int, str], int] = {}
    crosses: dict[tuple[int, str], int] = {}
    for e in led.events:
        k = e["event"]
        if k == "deposit":
            cash += e["amount"]
        elif k == "financing":
            cash -= e["amount"]
        elif k == "fill":
            cash += -e["qty"] * e["price"] - e["fee"]
            pos[e["name"]] = pos.get(e["name"], 0) + e["qty"]
            fills[(e["ts"], e["name"])] = e["qty"]
        elif k == "allocation":
            a = alloc_sum.setdefault((e["ts"], e["name"]), {"pos": 0, "neg": 0})
            a["pos" if e["qty"] > 0 else "neg"] += abs(e["qty"])
        elif k == "cross":
            crosses[(e["ts"], e["name"])] = e["qty"]
    if not _close(cash, led.cash, led.cash):
        raise ConservationError(
            f"cash identity: rebuilt {cash} != ledger {led.cash} (deposits + fills - financing)"
        )
    if len(led.fills) != len(fills) or att.fills != len(fills):
        raise ConservationError(
            f"fill count: ledger {len(led.fills)}, events {len(fills)}, attribution {att.fills}"
        )
    held = {n: q for n, q in pos.items() if q}
    if held != {n: q for n, q in led.positions.items() if q}:
        raise ConservationError(f"positions {led.positions} != sum of fills {held}")
    cohort_pos: dict[str, int] = {}
    for c in led.cohorts.values():
        for n, q in c.allocated.items():
            cohort_pos[n] = cohort_pos.get(n, 0) + q
    if {n: q for n, q in cohort_pos.items() if q} != held:
        raise ConservationError(f"cohort allocations {cohort_pos} != aggregate holdings {held}")
    for tn in sorted(set(alloc_sum) | set(fills)):
        a = alloc_sum.get(tn, {"pos": 0, "neg": 0})
        f = fills.get(tn, 0)
        x = crosses.get(tn, 0)
        if a["pos"] - a["neg"] != f:
            raise ConservationError(f"{tn}: allocations {a['pos'] - a['neg']} != external fill {f}")
        if a["pos"] != x + max(f, 0) or a["neg"] != x + max(-f, 0):
            raise ConservationError(f"{tn}: side allocations {a} != crossed {x} + fill {f}")
    wf = {ln: sum(r.waterfall[ln] for r in led.sessions) for ln in _LINES}
    for ln in _LINES:
        split = sum(row[ln] for row in att.by_cohort.values())
        if not _close(split, wf[ln], wf[ln]):
            raise ConservationError(f"{ln}: split {split} != waterfall {wf[ln]}")
    fam_net = sum(row["net"] for row in att.by_family.values())
    target = att.book["net_pnl"] + att.book["financing"]
    if not _close(fam_net, target, max(abs(target), 1.0)):
        raise ConservationError(f"family net values {fam_net} != NetPnL + financing {target}")
    for r in led.sessions:
        if abs(r.identity_gap) > TOL * max(1.0, abs(r.net_pnl), r.equity_open):
            raise ConservationError(f"session {r.session_id}: identity gap {r.identity_gap}")
    return att


def as_document(att: Attribution) -> dict[str, Any]:
    """Canonical-JSON-ready form (string keys, lists for tuples)."""

    def key(k: CohortKey) -> str:
        return f"{k[0]}|{k[1]}|{k[2]}"

    return {
        "by_cohort": {key(k): dict(sorted(v.items())) for k, v in sorted(att.by_cohort.items())},
        "by_family": {f: dict(sorted(v.items())) for f, v in att.by_family.items()},
        "crosses": att.crosses,
        "fills": att.fills,
        "crossed_shares": att.crossed_shares,
        "external_shares": att.external_shares,
        "book": dict(att.book),
    }


def sum_documents(parts: Mapping[str, Attribution]) -> dict[str, float]:
    """Totals across independent sleeves (C0): every line summed, sleeves in key order."""
    out: dict[str, float] = {}
    for _, att in sorted(parts.items()):
        for row in att.by_family.values():
            for k, v in row.items():
                out[k] = out.get(k, 0.0) + v
    return out
