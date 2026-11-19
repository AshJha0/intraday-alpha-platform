#!/usr/bin/env python3
"""Sessions needed against net Sharpe (iap.research.pnl_power): writes
docs/research/PNL_POWER_TABLE.md and research/round0/pnl_power_table.json.
Rerunnable; reads no market data.

    python tools/pnl_power_table.py
    python tools/pnl_power_table.py --check    # fail if the committed files are stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.research.pnl_power import render_markdown, table  # noqa: E402

MD = ROOT / "docs" / "research" / "PNL_POWER_TABLE.md"
JS = ROOT / "research" / "round0" / "pnl_power_table.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed files")
    args = ap.parse_args(argv)
    res = table()
    md = render_markdown(res)
    js = json.dumps(res, indent=2, sort_keys=True) + "\n"
    if args.check:
        stale = [
            str(p.relative_to(ROOT))
            for p, text in ((MD, md), (JS, js))
            if not p.is_file() or p.read_text(encoding="utf-8") != text
        ]
        if stale:
            print("stale: " + ", ".join(stale))
            return 1
        return 0
    JS.parent.mkdir(parents=True, exist_ok=True)
    MD.write_text(md, encoding="utf-8", newline="\n")
    JS.write_text(js, encoding="utf-8", newline="\n")
    print(f"wrote {MD.relative_to(ROOT)} and {JS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
