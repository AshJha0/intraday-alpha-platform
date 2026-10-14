"""Record the M5 quoting v2 outcomes on the blackboard (HUMAN-ONLY, run by the OWNER).

::

    python tools/quoting_v2_record_outcome.py --i-am-the-owner --all [--dry-run]

Thin CLI over ``iap.agents.workflow.Workflow.record_outcome`` (actor ``ashjha0``,
writer ``outcomes``), one outcome per registered cell (board seq 16-19). The
outcome is DERIVED from the pre-registered verdict in
research/quoting_real/v2_summary.json, never typed: "EXISTS ..." -> confirmed,
"SKEW INERT ..." -> inconclusive, otherwise rejected. Refuses a cell that
already has an outcome.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

OWNER = "ashjha0"
PREREG = "research/quoting_real/prereg_v2.json"
SUMMARY = "research/quoting_real/v2_summary.json"


def outcome_of(verdict: str) -> str:
    if verdict.startswith("EXISTS"):
        return "confirmed"
    if verdict.startswith("SKEW INERT"):
        return "inconclusive"
    return "rejected"


def plan(root: Path) -> list[tuple[str, str, str]]:
    reg = json.loads((root / PREREG).read_text(encoding="utf-8"))["registration"]["entries"]
    cells = json.loads((root / SUMMARY).read_text(encoding="utf-8"))["cells"]
    return [
        (e["prereg_id"], outcome_of(cells[e["alpha_id"]]["verdict"]), f"{SUMMARY}#{e['alpha_id']}")
        for e in reg
    ]


def main(argv: list[str] | None = None) -> int:
    from iap.agents.broker import WriteBroker
    from iap.agents.workflow import OUTCOME_WRITER, Workflow, registered_agents

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-am-the-owner", action="store_true", required=True)
    ap.add_argument("--all", action="store_true", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", type=Path, default=REPO)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    agents = set(registered_agents(root))
    broker = WriteBroker(root, agents, trusted={OUTCOME_WRITER})
    done = {e["body"]["prereg_id"] for e in broker.board.entries() if e["kind"] == "outcome"}
    for pid, _outcome, _ref in plan(root):
        if pid in done:
            raise SystemExit(f"prereg {pid} already has an outcome; nothing written")
    for pid, outcome, ref in plan(root):
        if a.dry_run:
            print(f"[dry-run] would record {outcome} for {pid} ({ref})")
            continue
        Workflow(broker).record_outcome(OWNER, pid, outcome, ref)
        print(f"recorded {outcome} for prereg {pid} (ref {ref})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
