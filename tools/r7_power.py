#!/usr/bin/env python3
"""R7 power analysis (iap.research.r7_power): writes docs/research/R7_POWER.md
and research/r7/r7_power.json from research/r7/r7_power_inputs.json and the
committed ledger. Rerunnable; reads no market data.

    PYTHONPATH=python/src python tools/r7_power.py
    PYTHONPATH=python/src python tools/r7_power.py --h1-mean-bps 0.4 --h1-sd-bps 1.2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.research.r7_power import analyse, ledger_gate, render_markdown  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--inputs", default=str(ROOT / "research/r7/r7_power_inputs.json"))
    p.add_argument("--ledger", default=str(ROOT / "research/experiments.json"))
    p.add_argument("--out-md", default=str(ROOT / "docs/research/R7_POWER.md"))
    p.add_argument("--out-json", default=str(ROOT / "research/r7/r7_power.json"))
    p.add_argument("--h1-mean-bps", type=float, help="maker in-sample session-mean edge")
    p.add_argument("--h1-sd-bps", type=float, help="maker in-sample per-session sd")
    a = p.parse_args(argv)
    if (a.h1_mean_bps is None) != (a.h1_sd_bps is None):
        p.error("--h1-mean-bps and --h1-sd-bps go together")
    h1 = None
    if a.h1_mean_bps is not None:
        h1 = {"mean_edge_bps": a.h1_mean_bps, "session_sd_bps": a.h1_sd_bps}
    res = analyse(json.loads(Path(a.inputs).read_text()), ledger_gate(a.ledger), h1)
    Path(a.out_md).write_text(render_markdown(res), encoding="utf-8", newline="\n")
    Path(a.out_json).write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {a.out_md} and {a.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
