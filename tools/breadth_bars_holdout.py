#!/usr/bin/env python3
"""The one pre-registered look at the sealed nine-session breadth holdout, on bars.

    # refusal checks only (reads no market data, downloads nothing)
    python tools/breadth_bars_holdout.py check --prereg research/preregs/breadth_bars_holdout_prereg.json
    # the planted dry run the signer binds (R4); planted numbers only
    python tools/breadth_bars_holdout.py planted-dry-run --alpha 0.025 --min-sessions 9 \\
        --out research/round1/holdout_look/planted_dry_run.json
    # the look itself: once, by a non-signing actor (IAP_RUN_ACTOR), after the owner's go
    python tools/breadth_bars_holdout.py run --prereg ... --i-have-approval

Procedure and rules: research/round1/HOLDOUT_LOOK_PROCEDURE.md.  Library:
iap.research.breadth_holdout (every precondition is checked there, before any
download).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from iap.research import breadth_bars as bb  # noqa: E402
from iap.research import breadth_holdout as bh  # noqa: E402


def family_known(name: str) -> bool:
    """A family runs if breadth_bars scores it, or breadth_families (round 1,
    another session) exposes it through ``FAMILIES`` and ``run_family``."""
    if name in bb.SCORES:
        return True
    try:
        from iap.research import breadth_families as bf  # type: ignore[attr-defined]
    except ImportError:
        return False
    return name in getattr(bf, "FAMILIES", ()) and hasattr(bf, "run_family")


def default_study(
    bars: Path, symbols: Sequence[str], family: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    panel = bb.load_panel(bars)
    rows, missing = bb.rows_for(panel, symbols)
    if family in bb.SCORES:
        res = bb.run_study(panel, rows, **params)
        net = res["scores"][family]["net_bps_per_rebalance"]
        extra = {
            "rebalances": res["rebalances"],
            "median_round_trip_cost_bps": res["median_round_trip_cost_bps"],
        }
    else:
        from iap.research import breadth_families as bf  # type: ignore[attr-defined]

        res = bf.run_family(panel, rows, family=family, **params)
        net = res["net_bps_per_rebalance"]
        extra = {}
    return {
        "value": net["mean"],
        "net_bps_per_rebalance": net,
        "names": len(rows),
        "missing_names": len(missing),
        **extra,
    }


def default_download(raw_file: Mapping[str, Any], vendor_dir: Path) -> Path:
    from r7_download import download_one

    vendor_dir.mkdir(parents=True, exist_ok=True)
    rec = download_one(dict(raw_file), vendor_dir, verify_sha=True)
    if rec["status"] == "failed":
        raise RuntimeError(f"download failed: {rec.get('reason')}")
    return vendor_dir / raw_file["file"]


def default_build(raw: Path, out: Path) -> dict[str, Any]:
    from itch_bars_chain import run_itchbars

    out.parent.mkdir(parents=True, exist_ok=True)
    return run_itchbars(raw, out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run"):
        p = sub.add_parser(name)
        p.add_argument("--prereg", type=Path, required=True)
        p.add_argument("--root", type=Path, default=ROOT)
        p.add_argument("--vendor-dir", type=Path, default=ROOT / "data" / "vendor" / "holdout_look")
        p.add_argument("--bars-dir", type=Path, default=ROOT / "data" / "real" / "bars_holdout")
        p.add_argument("--i-have-approval", action="store_true")
    d = sub.add_parser("planted-dry-run")
    d.add_argument("--alpha", type=float, required=True)
    d.add_argument("--min-sessions", type=int, required=True)
    d.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "planted-dry-run":
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(
            json.dumps(bh.planted_dry_run(a.alpha, a.min_sessions), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {a.out}")
        return 0
    try:
        plan = bh.preflight(a.root, a.prereg, family_known=family_known)
    except bh.LookRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"preflight OK: {plan.prereg_rel}, family {plan.doc['family']['name']}, "
        f"{len(plan.symbols)} names, result -> {plan.result_path}"
    )
    if a.cmd == "check":
        return 0
    if not a.i_have_approval:
        print(
            "REFUSED: pass --i-have-approval once the owner approved this one look", file=sys.stderr
        )
        return 2
    res = bh.run_look(
        a.root,
        plan,
        bh.Hooks(default_download, default_build, default_study),
        vendor_dir=a.vendor_dir,
        bars_dir=a.bars_dir,
    )
    print(json.dumps({k: res[k] for k in ("verdict", "sessions_used", "pooled")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
