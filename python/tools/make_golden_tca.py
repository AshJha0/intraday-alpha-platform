#!/usr/bin/env python3
"""Generate the timeline cases of tests/golden/expected_tca.json (x-version 2).

The Perold `cases` block of the file is pinned by hand (v1) and is copied
through untouched; this tool (re)computes the `timeline_cases` block from
the Python TCA reference (iap.tca): timelines built from raw BBO states with
the pinned crossed/locked rule, MAKER/TAKER fill stamping, the markout
"defined" rule (timeline end / HALT), the window validation and the
spread/impact split. Every port consumes the same inputs and must match to
`tolerance` (nulls exactly, errors by their pinned category).

Usage: PYTHONPATH=src python3 tools/make_golden_tca.py [--force]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from iap.tca.fills import MAKER, TAKER, MarketTimeline, ParentOrder, stamp_fill
from iap.tca.tca import (
    adverse_selection_with_counts,
    order_tca,
    spread_and_impact_cost,
)

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "expected_tca.json"
T0 = 1_787_578_200_000_000_000  # golden session t0
SEC = 1_000_000_000


def _flat_timeline(n: int) -> list:
    return [[T0 + k * SEC, 99.99, 100.01, 100, 100] for k in range(n)]


def build(raw_states, halts):
    tl = MarketTimeline()
    for ts, bid, ask, bsz, asz in raw_states:
        tl.append_state_pinned(ts, bid, ask, bsz, asz)
    for h in halts:
        tl.add_halt(h)
    return tl


def timeline_cases() -> list:
    cases = []

    # 1. buy at the session end: every markout undefined (n_defined 0)
    states = _flat_timeline(61)
    end = states[-1][0]
    fills = [[end, 100.01, 100, TAKER]]
    cases.append({"name": "buy_at_session_end", "states": states, "halts": [],
                  "order": {"side": 0, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": end, "fills": fills}})

    # 2. fill 5 s before the end: 100ms and 1s defined, 10s undefined
    fills = [[end - 5 * SEC, 100.01, 100, TAKER]]
    cases.append({"name": "buy_five_seconds_before_end", "states": states,
                  "halts": [],
                  "order": {"side": 0, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": end, "fills": fills}})

    # 3. a HALT 3 s after the fill: the 10 s markout is undefined
    fills = [[T0, 100.01, 100, TAKER]]
    cases.append({"name": "halt_inside_markout_window", "states": states,
                  "halts": [T0 + 3 * SEC],
                  "order": {"side": 0, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": end, "fills": fills}})

    # 4. passive fill by a trade-through: pre-event reference state
    states = [[T0, 99.98, 100.02, 500, 400],
              [T0 + 1_000, 99.90, 100.02, 500, 400]] + [
        [T0 + k * SEC, 99.90, 100.02, 500, 400] for k in range(1, 61)]
    fills = [[T0 + 1_000, 99.98, 100, MAKER]]
    cases.append({"name": "passive_fill_pre_event_mid", "states": states,
                  "halts": [],
                  "order": {"side": 0, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": T0 + 60 * SEC,
                            "fills": fills}})

    # 5. locked and crossed raw states: crossed skipped + counted, locked kept
    states = [[T0, 100.00, 100.02, 100, 100],
              [T0 + 1, 100.02, 100.02, 100, 50],     # locked, hs 0
              [T0 + 2, 100.03, 100.02, 100, 50],     # crossed: skipped
              [T0 + 3, 100.04, 100.02, 100, 50],     # crossed: skipped
              [T0 + 4, 100.00, 100.02, 100, 100]] + [
        [T0 + k * SEC, 100.00, 100.02, 100, 100] for k in range(1, 61)]
    fills = [[T0 + 3, 100.02, 100, TAKER]]  # prevailing = the locked state
    cases.append({"name": "locked_kept_crossed_skipped", "states": states,
                  "halts": [],
                  "order": {"side": 0, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": T0 + 60 * SEC,
                            "fills": fills}})

    # 6. fill outside [arrival_ts, end_ts]: rejected
    states = _flat_timeline(61)
    fills = [[T0 + 30 * SEC + 1, 100.01, 100, TAKER]]
    cases.append({"name": "fill_outside_window_rejected", "states": states,
                  "halts": [],
                  "order": {"side": 0, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": T0 + 30 * SEC,
                            "fills": fills}, "expect_error": "outside"})

    # 7. end_ts beyond the timeline: rejected (no fabricated end_mid)
    cases.append({"name": "end_beyond_timeline_rejected", "states": states,
                  "halts": [],
                  "order": {"side": 1, "qty_target": 100, "decision_ts": T0,
                            "arrival_ts": T0, "end_ts": end + 1,
                            "fills": []}, "expect_error": "beyond"})

    for c in cases:
        tl = build(c["states"], c["halts"])
        o = c["order"]
        order = ParentOrder(order_id=1, instrument_id=1, side=o["side"],
                            qty_target=o["qty_target"],
                            decision_ts=o["decision_ts"],
                            arrival_ts=o["arrival_ts"], end_ts=o["end_ts"])
        for ts, price, qty, liq in o["fills"]:
            order.fills.append(stamp_fill(tl, ts, price, qty, o["side"], liq))
        expected = {"n_states": len(tl),
                    "crossed_states_skipped": tl.crossed_states_skipped}
        if "expect_error" in c:
            try:
                order_tca(order, tl)
            except ValueError as e:
                assert c["expect_error"] in str(e), (c["name"], str(e))
            else:
                raise AssertionError(f"{c['name']} must raise")
            c["expected"] = expected
            continue
        rec = order_tca(order, tl)
        markouts, n = adverse_selection_with_counts(order, tl)
        split = spread_and_impact_cost(order)
        expected.update({
            "fill_ref": [[f.mid_at_fill, f.half_spread_at_fill] for f in order.fills],
            "spread_cost": split["spread_cost"],
            "impact_cost": split["impact_cost"],
            "exec_cost_vs_mid": split["exec_cost_vs_mid"],
            "adverse_selection_bps": markouts,
            "adverse_selection_n": n,
            "total_is": rec["perold"]["total_is"],
            "end_mid": rec["end_mid"],
        })
        c["expected"] = expected
    return cases


def main() -> int:
    force = "--force" in sys.argv
    doc = json.loads(GOLDEN.read_text())
    if doc.get("x-version", 1) >= 2 and not force:
        print("expected_tca.json already at x-version 2 — pinned; pass --force "
              "after a schemas/MIGRATIONS.md entry")
        return 1
    doc["x-version"] = 2
    doc["description"] = (
        "Pinned TCA goldens (API_PORTFOLIO_TCA.md §2, iap.tca is the reference). "
        "`cases`: parent-order fill sets with the expected Perold implementation-"
        "shortfall decomposition (delay + trading + opportunity == total_is "
        "exactly; 1e-9). `timeline_cases` (v2, generated by "
        "python/tools/make_golden_tca.py): raw BBO states [ts, bid, ask, bid_sz, "
        "ask_sz] fed through the pinned builder rule (crossed states skipped + "
        "counted, locked kept), HALT starts, one parent order whose fills are "
        "[ts, price, qty, liquidity] stamped by the pinned §2.4 rule (TAKER: "
        "state prevailing at ts; MAKER: state strictly before ts); expected "
        "fill reference states, spread/impact split, markouts per pinned delta "
        "(null = undefined per §2.5: timeline end or HALT inside the window) "
        "with n_defined, total_is and end_mid — or `expect_error` (window / "
        "timeline-end validation, pinned to raise). Every port must match "
        "to `tolerance`.")
    doc["timeline_cases"] = timeline_cases()
    GOLDEN.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {GOLDEN} ({len(doc['timeline_cases'])} timeline cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
