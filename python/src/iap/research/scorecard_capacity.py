"""Capacity section for the REL-08 / 08c / 09 / 10 scorecards (v1.18 REL-17).

The study modules (``rel08_screen``, ``auction.rel08c_study``,
``stale_quote``, ``volume_clock_ofi``) are bound by sha256 into their
signed pre-registrations' analysis identity, so this module does NOT edit
them: it reads each study's raw output, picks the positive candidates by
that study's own rule, builds their :class:`~iap.portfolio.capacity.
CapacityInput` and appends :func:`~iap.portfolio.capacity.capacity_section`
to the scorecard.  Every scorecard gains the section; with no positive
candidate it says "not applicable" and why.

Positive candidate, per study:

| study | positive when | net and band | impact at base |
|---|---|---|---|
| REL-08 | decision outcome ``continue`` or ``passed_not_expanded`` | ``net_bps`` mean, its ``lower`` bound used as the band's lower edge, ``se`` for the upper | book ``impact_cost`` / (rebalances x notional); participation implied from the square-root rule and the mean turnover |
| REL-08c | ``verdict == "continue"`` | ``mean_incremental_bps``, t-based CI | from ``base_participation`` (caller; the study has no ADV) |
| REL-09 | session CI lower edge > 0 | (maker filter) | not scalable by notional: row says so |
| REL-10 | taker / maker summary with CI lower edge > 0 | per-trip mean net bps; the USD CI scaled by bps/USD | taker: from ``base_participation``; maker: passive |
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.backtest.costs import DEFAULT_SQRT_IMPACT_COEFF_BPS
from iap.portfolio.capacity import CapacityInput, capacity_curve, capacity_section

__all__ = [
    "STUDIES",
    "append_capacity",
    "rel08_inputs",
    "rel08c_inputs",
    "rel09_inputs",
    "rel10_inputs",
]

STUDIES = ("rel08", "rel08c", "rel09", "rel10")
_POSITIVE_REL08 = ("continue", "passed_not_expanded")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and math.isfinite(float(v)) else None


def rel08_inputs(
    raw: Mapping[str, Any],
    base_notional: float | None,
    impact_coeff_bps: float = DEFAULT_SQRT_IMPACT_COEFF_BPS,
) -> tuple[list[CapacityInput], str]:
    dec = {d["id"]: d for d in raw.get("decisions", [])}
    out = []
    for v in raw.get("variants", []):
        if dec.get(v["id"], {}).get("outcome") not in _POSITIVE_REL08:
            continue
        nb = v.get("net_bps") or {}
        b = v.get("book") or {}
        mean = _num(nb.get("mean"))
        if mean is None:
            continue
        i0 = p0 = None
        n_reb = v.get("n_rebalances")
        if base_notional and n_reb and _num(b.get("impact_cost")) is not None:
            i0 = float(b["impact_cost"]) / (float(n_reb) * base_notional) * 1e4
            to = _num(b.get("mean_turnover"))
            if to and to > 0 and impact_coeff_bps > 0:
                p0 = (i0 / to / impact_coeff_bps) ** 2
        lo, se = _num(nb.get("lower")), _num(nb.get("se"))
        ci = (lo, mean + 1.959963984540054 * se) if lo is not None and se is not None else None
        out.append(
            CapacityInput(
                str(v["id"]),
                mean,
                se,
                base_notional,
                i0,
                p0,
                net_bps_ci=ci,
                unit="bps of book notional per rebalance",
                note="participation implied from the square-root rule and mean turnover",
            )
        )
    why = "" if out else "no variant reached continue or passed_not_expanded"
    return out, why


def rel08c_inputs(
    summary: Mapping[str, Any],
    base_notional: float | None = None,
    base_participation: float | None = None,
) -> tuple[list[CapacityInput], str]:
    dec = summary.get("decision", summary)
    if dec.get("verdict") != "continue":
        return [], f"verdict {dec.get('verdict')!r}"
    mean = _num(dec.get("mean_incremental_bps"))
    if mean is None:
        return [], "no finite mean"
    lo, hi = _num(dec.get("ci_low_bps")), _num(dec.get("ci_high_bps"))
    return [
        CapacityInput(
            "imbalance",
            mean,
            None,
            base_notional,
            None,
            base_participation,
            net_bps_ci=(lo, hi) if lo is not None and hi is not None else None,
            unit="incremental bps per imbalance trade",
        )
    ], ""


def _positive(s: Mapping[str, Any]) -> bool:
    ci = s.get("ci95_usd")
    if not ci:
        return False
    lo = _num(ci[0])
    return lo is not None and lo > 0


def rel09_inputs(summary: Mapping[str, Any]) -> tuple[list[CapacityInput], str]:
    if not _positive(summary):
        return [], "session CI lower edge not above zero"
    return [
        CapacityInput(
            "stale_quote_filter",
            float("nan"),
            None,
            None,
            passive=True,
            unit="incremental USD per session",
            note="a maker filter: P&L is per quote, not per notional; the square-root taker rule "
            "does not apply and queue / quote-size limits are not modelled",
        )
    ], ""


def rel10_inputs(
    summary: Mapping[str, Any],
    base_notional: float | None = None,
    base_participation: float | None = None,
) -> tuple[list[CapacityInput], str]:
    out = []
    for use in ("taker", "maker"):
        s = summary.get(use)
        if not s or not _positive(s):
            continue
        bps = _num((s.get("per_trip") or {}).get("mean_net_bps"))
        usd = _num(s.get("mean_usd"))
        if bps is None or not usd:
            continue
        r = bps / usd
        ci = tuple(sorted((float(s["ci95_usd"][0]) * r, float(s["ci95_usd"][1]) * r)))
        out.append(
            CapacityInput(
                f"volume_clock_ofi_{use}",
                bps,
                None,
                base_notional,
                None,
                None if use == "maker" else base_participation,
                passive=use == "maker",
                net_bps_ci=(ci[0], ci[1]),
                unit="net bps per round trip",
                note="band = session USD CI scaled by mean bps / mean USD",
            )
        )
    return out, "" if out else "neither taker nor maker has a session CI above zero"


def append_capacity(
    study: str,
    scorecard_md: str,
    inputs: Sequence[CapacityInput],
    why_empty: str = "",
) -> str:
    """``scorecard_md`` with the capacity section appended (always present)."""
    if study not in STUDIES:
        raise ValueError(f"study must be one of {STUDIES}")
    curves = [capacity_curve(c) for c in inputs]
    return scorecard_md.rstrip("\n") + "\n\n" + capacity_section(study.upper(), curves, why_empty)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--study", choices=STUDIES, required=True)
    ap.add_argument("--raw", type=Path, required=True, help="the study's raw JSON output")
    ap.add_argument("--scorecard", type=Path, required=True, help="markdown to append to")
    ap.add_argument("--base-notional", type=float)
    ap.add_argument("--base-participation", type=float)
    a = ap.parse_args(argv)
    raw = json.loads(a.raw.read_text(encoding="utf-8"))
    if a.study == "rel08":
        inputs, why = rel08_inputs(raw, a.base_notional)
    elif a.study == "rel08c":
        inputs, why = rel08c_inputs(raw, a.base_notional, a.base_participation)
    elif a.study == "rel09":
        inputs, why = rel09_inputs(raw)
    else:
        inputs, why = rel10_inputs(raw, a.base_notional, a.base_participation)
    md = a.scorecard.read_text(encoding="utf-8")
    a.scorecard.write_text(append_capacity(a.study, md, inputs, why), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
