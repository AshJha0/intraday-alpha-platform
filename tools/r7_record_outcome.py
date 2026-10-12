"""Record the R7-CONFIRM-1 outcome on the blackboard (HUMAN-ONLY, run by the OWNER).

::

    python tools/r7_record_outcome.py --i-am-the-owner --prereg-id ID \
        --outcome confirmed|rejected --ref research/r7/r7_confirm_result.json#EQ10@1s

Thin CLI over ``iap.agents.workflow.Workflow.record_outcome`` (actor ``ashjha0``,
writer ``outcomes``).  Refuses unless the outcome equals the verdict that
``tools/r7_confirm.py`` wrote for that prereg in research/r7/r7_confirm_result.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

OWNER = "ashjha0"
RESULT_REL = "research/r7/r7_confirm_result.json"


def expected_verdict(root: Path, prereg_id: str) -> str:
    doc = json.loads((root / RESULT_REL).read_text(encoding="utf-8"))
    for r in doc["results"]:
        if r.get("prereg_id") == prereg_id:
            return r["verdict"]
    raise SystemExit(f"{prereg_id} not in {RESULT_REL}")


def main(argv: list[str] | None = None) -> int:
    from iap.agents.broker import WriteBroker
    from iap.agents.workflow import OUTCOME_WRITER, OUTCOMES, Workflow, registered_agents

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--i-am-the-owner", action="store_true", required=True)
    ap.add_argument("--prereg-id", required=True)
    ap.add_argument("--outcome", required=True, choices=OUTCOMES)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--root", type=Path, default=REPO)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    want = expected_verdict(root, a.prereg_id)
    if a.outcome != want:
        raise SystemExit(f"--outcome {a.outcome} != the computed verdict {want}")
    agents = set(registered_agents(root))
    broker = WriteBroker(root, agents, trusted={OUTCOME_WRITER})
    Workflow(broker).record_outcome(OWNER, a.prereg_id, a.outcome, a.ref)
    print(f"recorded {a.outcome} for prereg {a.prereg_id} (ref {a.ref})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
