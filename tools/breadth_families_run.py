#!/usr/bin/env python3
"""Run the three pre-specified bar families (research/round1/FAMILIES_PRESPEC_2026-11-18.md)
over the in-sample sessions that have bars, pooled across sessions.

    python tools/breadth_families_run.py --bars-dir data/real/bars --out research/round1/breadth_families.json

Reads ``bars_YYYYMMDD.csv`` and, for F3, ``auction_YYYYMMDD.csv`` (from
``itchbars --auction``) in ``--auction-dir``; a session without an auction file
has no F3 number.  Refuses any session dated 2025 or later (the sealed
holdout).  The universe is the one fixed on 2019-01-30.  Parameters are those
of the pre-specification and are not options.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.research import breadth_bars as bb  # noqa: E402
from iap.research import breadth_families as bf  # noqa: E402

UNIVERSE = ROOT / "research" / "round1" / "universe_20190130.json"
HOLDOUT_FROM = "2025-01-01"
DELAYS = (1, 0)  # primary first; delay 0 is a diagnostic


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def session_files(bars_dir: Path) -> dict[str, Path]:
    out = {}
    for p in sorted(bars_dir.glob("bars_*.csv")):
        m = re.fullmatch(r"bars_(\d{4})(\d{2})(\d{2})\.csv", p.name)
        if not m:
            continue
        date = "-".join(m.groups())
        if date >= HOLDOUT_FROM:
            raise SystemExit(f"refusing {p.name}: sessions from 2025 on are the sealed holdout")
        out[date] = p
    return out


def _gap_rows(prev: bb.Panel, panel: bb.Panel, rows) -> list[int]:
    index = {s: i for i, s in enumerate(prev.symbols)}
    return [index.get(panel.symbols[r], -1) for r in rows]


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bars-dir", type=Path, required=True)
    ap.add_argument("--auction-dir", type=Path, default=None)
    ap.add_argument("--universe", type=Path, default=UNIVERSE)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    auction_dir = args.auction_dir or args.bars_dir
    symbols = json.loads(args.universe.read_text(encoding="utf-8"))["symbols"]
    files = session_files(args.bars_dir)

    sessions: dict[str, dict] = {}
    prev_date, prev_panel = None, None
    for date, path in files.items():
        panel = bb.load_panel(path)
        rows, missing = bb.rows_for(panel, symbols)
        s: dict = {"names": len(rows), "missing": len(missing)}
        for d in DELAYS:
            s[f"momentum_30_d{d}"] = bf.momentum_30(panel, rows, d)
            s[f"open_reversal_d{d}"] = bf.open_reversal(panel, rows, d)
        consecutive = prev_date is not None and bf.is_previous_weekday(prev_date, date)
        prev_rows = (
            np.asarray(_gap_rows(prev_panel, panel, rows), dtype=np.intp) if consecutive else None
        )
        s["overnight_gap_d1"] = bf.overnight_gap(
            panel, rows, prev_panel if consecutive else None, prev_rows, consecutive
        )
        apath = auction_dir / f"auction_{date.replace('-', '')}.csv"
        if apath.exists():
            auction = bf.load_auction(apath)
            for d in DELAYS:
                s[f"closing_imbalance_d{d}"] = bf.closing_imbalance(panel, rows, auction, d)
            s["closing_imbalance_d1_mid_exit"] = bf.closing_imbalance(
                panel, rows, auction, 1, exit_at="mid"
            )
        for v in s.values():
            if isinstance(v, dict):
                v.pop("per_rebalance", None)
        sessions[date] = s
        prev_date, prev_panel = date, panel

    keys = sorted({k for s in sessions.values() for k in s if isinstance(s[k], dict)})
    pooled = {}
    for k in keys:
        vals = [s[k].get("session_net_bps", math.nan) for s in sessions.values() if k in s]
        vals = [v for v in vals if v is not None and math.isfinite(v)]
        pooled[k] = {"sessions": len(vals), **bb.pool_sessions(vals)} if vals else {"sessions": 0}
    doc = {
        "prespec": "research/round1/FAMILIES_PRESPEC_2026-11-18.md",
        "primary": ["momentum_30_d1", "open_reversal_d1", "closing_imbalance_d1"],
        "bonferroni_k": 7,
        "one_sided_threshold": 0.025 / 7,
        "sessions": sessions,
        "pooled": pooled,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(_clean(doc), indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return doc


if __name__ == "__main__":
    main()
