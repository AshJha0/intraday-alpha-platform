"""Test helper (v1.19 R4): attach a complete planted dry run to a prereg in a fixture repo.

Since R4 (`iap.agents.planted_dry_run`) the sign helpers refuse a prereg without a bound
``dry_run`` block, so the runner tests that sign a historical-style prereg in a temporary
repository call :func:`attach_planted_dry_run` between ``bind`` and ``sign``.  It writes a
committed report that demonstrates every verdict value, binds it into the prereg document
and commits both.  Nothing here reads data.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from iap.agents.planted_dry_run import build_dry_run_block

REPORT_REL = "research/dryrun/planted_dry_run.json"


def dry_run_report(verdict_values: Sequence[str], rule: str = "fixture rule") -> dict:
    return {
        "kind": "planted_dry_run",
        "rule": rule,
        "verdict_values": list(verdict_values),
        "demonstrations": [
            {
                "planted": {"input": f"planted for {v}", "expected": v, "observations": 20},
                "observed": v,
            }
            for v in verdict_values
        ],
        "statistics_planted": True,
        "confirmation_data_read": False,
    }


def attach_planted_dry_run(
    root: Path,
    prereg_rel: str,
    verdict_values: Sequence[str] = ("confirmed", "rejected", "underpowered"),
    *,
    report_rel: str = REPORT_REL,
) -> dict:
    """Write and commit the report, bind it into ``prereg_rel`` and commit; return the block."""
    root = Path(root)
    report = root / report_rel
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(dry_run_report(verdict_values), indent=1) + "\n", encoding="utf-8")
    block = build_dry_run_block(root, report_rel)
    p = root / prereg_rel
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["dry_run"] = block
    with p.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "planted dry run (R4)"],
        check=True,
        capture_output=True,
    )
    return block
