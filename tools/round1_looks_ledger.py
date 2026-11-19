#!/usr/bin/env python3
"""Round 1 looks ledger: every real-data look of the round as hash-chained
entries, built from the committed result files.  Reads no market data.

    python tools/round1_looks_ledger.py            # write research/round1/looks_ledger.json
    python tools/round1_looks_ledger.py --check    # rebuild in memory and compare

The file uses ``iap.validation.ledger.ExperimentLedger`` (the same class and
hash chain as research/experiments.json) and is separate from it: the central
ledger's totals are quoted, with numbers derived from them, in worked examples
across the documentation, and those examples were computed at that total.
Merging these entries into the central ledger means regenerating those
examples; until that is done the central total does NOT include this round
(research/round1/DECISIONS_2026-11-18_overnight.md, decision 13).

What counts as a look here: one primary or declared-as-counted configuration on
one session's data.  Diagnostics the pre-specification says are not counted are
not entered.  F3 version 1 computed no forward return and has no entry.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.validation.ledger import ExperimentLedger  # noqa: E402

R1 = ROOT / "research" / "round1"
OUT = R1 / "looks_ledger.json"
MANIFEST = R1 / "bars_manifest.json"
#: sha256 of the 2019-01-30 vendor file the quoter comparison's dataset was ingested from
QUOTER_DAY = "2019-01-30"
REVERSAL_PARAMS = {
    "lookback": 15,
    "step": 15,
    "horizon": 15,
    "decile_q": 0.1,
    "universe": "round1/universe_20190130",
}
FAMILY_KEYS = {
    "momentum_30_d1": ("F1_momentum_30", "FAMILIES_PRESPEC section 1"),
    "open_reversal_d1": ("F2_open_reversal", "FAMILIES_PRESPEC section 2"),
    "closing_imbalance_d1": ("F3v2_closing_imbalance", "FAMILIES_PRESPEC erratum, F3 version 2"),
}


def build(path: Path) -> dict:
    manifest = {d["date"]: d for d in json.loads(MANIFEST.read_text(encoding="utf-8"))["sessions"]}
    pooled = json.loads((R1 / "breadth_bars_pooled.json").read_text(encoding="utf-8"))
    fam = json.loads((R1 / "breadth_families.json").read_text(encoding="utf-8"))
    counts = {"reversal": 0, "families": 0, "quoter": 0}
    for date in sorted(pooled["sessions"]):
        led = ExperimentLedger(
            path, dataset_version=manifest[date]["bars_sha256"], data_family="real"
        )
        s = pooled["sessions"][date]
        for delay, run in sorted(s["runs"].items()):
            for score, r in sorted(run["scores"].items()):
                led.record(
                    score,
                    "breadth_bars_dev",
                    {**REVERSAL_PARAMS, "delay": int(delay), "session": date},
                    {
                        "net_bps_per_rebalance": r["net_bps_per_rebalance"]["mean"],
                        "gross_bps_per_rebalance": r["gross_bps_per_rebalance"]["mean"],
                        "rank_ic": r["rank_ic"]["mean"],
                        "source": "research/round1/breadth_bars_pooled.json",
                        "evidence_class": "exploratory",
                    },
                )
                counts["reversal"] += 1
        for key, (alpha, spec) in FAMILY_KEYS.items():
            r = fam["sessions"][date][key]
            led.record(
                alpha,
                "breadth_bars_family",
                {
                    "prespec": spec,
                    "params": r["params"],
                    "session": date,
                    "universe": "round1/universe_20190130",
                },
                {
                    "session_net_bps": r["session_net_bps"],
                    "names_with_score": r.get("names_with_score"),
                    "source": "research/round1/breadth_families.json",
                    "evidence_class": "exploratory",
                },
            )
            counts["families"] += 1
        led.save()
    led = ExperimentLedger(
        path, dataset_version=manifest[QUOTER_DAY]["raw_sha256"], data_family="real"
    )
    for f in sorted((R1 / "quoter_event_dev").glob(f"{QUOTER_DAY}_*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        symbol = f.stem.split("_", 1)[1]
        for mode in ("grid", "events"):
            led.record(
                f"quoter_{mode}",
                "quoter_event_dev",
                {"symbol": symbol, "session": QUOTER_DAY, "trigger_mode": mode},
                {
                    "net": _net(doc, mode),
                    "source": f"research/round1/quoter_event_dev/{f.name}",
                    "evidence_class": "exploratory",
                },
            )
            counts["quoter"] += 1
    led.save()
    return counts


def _net(doc: dict, mode: str):
    """The mode's net P&L wherever the result file keeps it (None if absent)."""
    node = doc.get(mode) or doc.get("runs", {}).get(mode) or {}
    for path in (("pnl", "net"), ("net",), ("summary", "net")):
        cur = node
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, (int, float)):
            return cur
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "looks_ledger.json"
            build(p)
            ok = OUT.is_file() and json.loads(OUT.read_text(encoding="utf-8")) == json.loads(
                p.read_text(encoding="utf-8")
            )
        print("current" if ok else "stale: research/round1/looks_ledger.json")
        return 0 if ok else 1
    OUT.unlink(missing_ok=True)
    counts = build(OUT)
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "counts": counts,
                "total_experiments": doc["total_experiments"],
                "distinct": doc["distinct_experiments"],
                "chain_head": doc["chain_head"],
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
