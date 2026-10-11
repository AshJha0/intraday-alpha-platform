#!/usr/bin/env python3
"""(Re)generate tests/golden/expected_regulatory_fees.json (v1.15, X-P1-1).

Run from python/ with PYTHONPATH=src:

    PYTHONPATH=src python3 tools/make_golden_regulatory_fees.py

Pins the dated SEC Section 31 + FINRA TAF schedule arithmetic shared by the
Python (``iap.execution.regulatory``), C++ (``iap/execution/
regulatory_fees.hpp``) and Rust (``venue::regfees``) engines. The golden
embeds a byte-for-byte copy of ``configs/reference/regulatory_fee_schedule.json``
(``schedule``; a Python test checks they agree) and per-fill cases:
``notional = qty * qty_unit * price_ticks * tick_size`` and the expected
fee (tolerance 1e-12 absolute), or ``"error": true`` where no period covers
the fill time (fail closed).
"""

from __future__ import annotations

import json
from pathlib import Path

from iap.execution.regulatory import RegulatoryFeeSchedule

ROOT = Path(__file__).resolve().parents[2]
SCHEDULE = ROOT / "configs" / "reference" / "regulatory_fee_schedule.json"
OUT = ROOT / "tests" / "golden" / "expected_regulatory_fees.json"

NS_DAY = 86_400 * 10**9
# 14:30 UTC (09:30/10:30 New York) on the given UTC midnight.
OPEN = 14 * 3600 * 10**9 + 30 * 60 * 10**9


def cases() -> list[dict]:
    t = {
        "2019-07-01": 1561939200000000000,
        "2019-04-15": 1555286400000000000,
        "2024-03-01": 1709251200000000000,
        "2024-05-21": 1716249600000000000,
        "2024-05-22": 1716336000000000000,
        "2025-05-13": 1747094400000000000,
        "2025-05-14": 1747180800000000000,
        "2025-11-28": 1764288000000000000,
        "2026-01-02": 1767312000000000000,
        "2026-04-03": 1775174400000000000,
        "2026-04-04": 1775260800000000000,
        "2026-06-12": 1781222400000000000,
        "2027-06-01": 1811808000000000000,
        "2030-01-02": 1893542400000000000,
        "2021-06-01": 1622505600000000000,
    }
    out = []
    rows = [
        # (date, side, qty, price_ticks, tick_size, qty_unit)
        ("2019-07-01", 1, 100, 18_725, 0.01, 1.0),
        ("2019-07-01", 0, 100, 18_725, 0.01, 1.0),
        ("2019-07-01", 1, 60_000, 4_210, 0.01, 1.0),  # TAF cap binds
        ("2024-03-01", 1, 500, 25_000, 0.01, 1.0),
        ("2024-05-21", 1, 500, 25_000, 0.01, 1.0),
        ("2024-05-22", 1, 500, 25_000, 0.01, 1.0),
        ("2025-05-13", 1, 1_000, 51_234, 0.01, 1.0),
        ("2025-05-14", 1, 1_000, 51_234, 0.01, 1.0),
        ("2025-11-28", 1, 300, 50_000, 0.01, 1.0),
        ("2026-01-02", 1, 300, 50_000, 0.01, 1.0),
        ("2026-04-03", 1, 300, 50_000, 0.01, 1.0),
        ("2026-04-04", 1, 300, 50_000, 0.01, 1.0),
        ("2026-06-12", 1, 50_217, 59_999, 0.01, 1.0),  # TAF cap binds
        ("2026-06-12", 0, 50_217, 59_999, 0.01, 1.0),
        ("2027-06-01", 1, 1, 10_000, 0.01, 1.0),
        ("2030-01-02", 1, 1_000, 30_000, 0.01, 1.0),  # open-ended periods
        ("2019-04-15", 1, 100, 18_725, 0.01, 1.0),  # before SEC coverage
        ("2021-06-01", 1, 100, 18_725, 0.01, 1.0),  # coverage gap
    ]
    sched = RegulatoryFeeSchedule.load(SCHEDULE)
    for date, side, qty, px, tick, unit in rows:
        ts = t[date] + OPEN
        notional = float(qty) * unit * float(px) * tick
        case = {
            "label": f"{date} {'sell' if side else 'buy'} {qty}",
            "ts": ts,
            "side": side,
            "qty": qty,
            "price_ticks": px,
            "tick_size": tick,
            "qty_unit": unit,
        }
        try:
            case["fee"] = sched.fee(side, float(qty), notional, ts)
        except ValueError:
            case["error"] = True
        out.append(case)
    return out


def main() -> None:
    schedule = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    doc = {
        "x-version": 1,
        "description": (
            "v1.15 X-P1-1 regulatory fee golden (python/tools/"
            "make_golden_regulatory_fees.py). schedule = copy of "
            "configs/reference/regulatory_fee_schedule.json; per case "
            "notional = qty*qty_unit*price_ticks*tick_size, fee = sell ? "
            "usd_per_million*notional/1e6 + min(per_share*qty, max_per_trade) : 0; "
            "error = no period covers ts. Tolerance 1e-12 absolute."
        ),
        "tolerance": 1e-12,
        "schedule": schedule,
        "cases": cases(),
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
