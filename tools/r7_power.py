#!/usr/bin/env python3
"""R7 power analysis (iap.research.r7_power): writes docs/research/R7_POWER.md
and research/r7/r7_power.json from research/r7/r7_power_inputs.json and the
committed ledger. Rerunnable; reads no market data.

    PYTHONPATH=python/src python tools/r7_power.py
    PYTHONPATH=python/src python tools/r7_power.py --h1-mean-bps 0.4 --h1-sd-bps 1.2
    PYTHONPATH=python/src python tools/r7_power.py --multi-symbol

``--multi-symbol`` writes the cross-sectional (N symbols x 7 sessions) tables
to docs/research/R7_MULTI_SYMBOL_POWER.md and research/r7/r7_multi_symbol_power.json
instead of the main report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.research.r7_power import (  # noqa: E402
    analyse,
    analyse_multi_symbol,
    ledger_gate,
    prereg_status,
    render_markdown,
    render_multi_symbol_markdown,
)


def _write(md_path: str, json_path: str, md: str, res: dict) -> None:
    Path(md_path).write_text(md, encoding="utf-8", newline="\n")
    Path(json_path).write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {md_path} and {json_path}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--inputs", default=str(ROOT / "research/r7/r7_power_inputs.json"))
    p.add_argument("--ledger", default=str(ROOT / "research/experiments.json"))
    p.add_argument("--out-md", help="default depends on --multi-symbol")
    p.add_argument("--out-json", help="default depends on --multi-symbol")
    p.add_argument("--h1-mean-bps", type=float, help="maker in-sample session-mean edge")
    p.add_argument("--h1-sd-bps", type=float, help="maker in-sample per-session sd")
    p.add_argument("--multi-symbol", action="store_true", help="cross-sectional tables")
    a = p.parse_args(argv)
    if (a.h1_mean_bps is None) != (a.h1_sd_bps is None):
        p.error("--h1-mean-bps and --h1-sd-bps go together")
    inputs = json.loads(Path(a.inputs).read_text())
    gate = ledger_gate(a.ledger)
    if a.multi_symbol:
        res = analyse_multi_symbol(inputs, gate)
        _write(
            a.out_md or str(ROOT / "docs/research/R7_MULTI_SYMBOL_POWER.md"),
            a.out_json or str(ROOT / "research/r7/r7_multi_symbol_power.json"),
            render_multi_symbol_markdown(res),
            res,
        )
        return 0
    h1 = None
    if a.h1_mean_bps is not None:
        h1 = {"mean_edge_bps": a.h1_mean_bps, "session_sd_bps": a.h1_sd_bps}
    res = analyse(inputs, gate, h1)
    res["prereg"] = prereg_status(ROOT / "research/r7")
    _write(
        a.out_md or str(ROOT / "docs/research/R7_POWER.md"),
        a.out_json or str(ROOT / "research/r7/r7_power.json"),
        render_markdown(res),
        res,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
