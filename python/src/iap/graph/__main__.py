"""``python -m iap.graph demo --out DIR`` · ``resume DIR --approve|--reject`` · ``diagram``.

The demo runs the research flow with the built-in stubs up to the human
approval interrupt; ``resume`` records the decision and finishes the run.
Both print the event log of the run directory.  ``diagram`` prints the
Mermaid diagram generated from the graph definition.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .checkpoint import event_log, resume, start
from .core import GraphError
from .research_flow import DEMO_HYPOTHESIS, build_research_graph, stub_deps


def _print_log(run_dir: Path, status: str, node: str) -> None:
    for e in event_log(run_dir):
        print(
            f"{e['step']:>3}  {e['node']:<15} {e['outcome']:<9} -> {e['next'] or '-':<15} "
            f"in {e['input_sha256'][:12]} out {e['output_sha256'][:12]}"
        )
    print(f"status: {status} at {node}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m iap.graph")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the research flow with stubs to the approval step")
    d.add_argument("--out", required=True, type=Path)
    r = sub.add_parser("resume", help="record the approval decision and finish the run")
    r.add_argument("run_dir", type=Path)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    r.add_argument("--by", default="owner", help="who decided (recorded in the checkpoint)")
    sub.add_parser("diagram", help="print the Mermaid diagram of the research flow")
    args = p.parse_args(argv)

    graph = build_research_graph(stub_deps())
    try:
        if args.cmd == "diagram":
            print(graph.to_mermaid(), end="")
            return 0
        if args.cmd == "demo":
            result = start(
                graph, {"hypothesis": DEMO_HYPOTHESIS}, args.out, clock=time.perf_counter
            )
            _print_log(args.out, result.status, result.node)
            if result.interrupt is not None:
                print(f"awaiting: {result.interrupt.reason}")
                print(f"next: python -m iap.graph resume {args.out} --approve|--reject")
            return 0
        decision = {"approved": bool(args.approve), "by": args.by}
        result = resume(args.run_dir, graph, decision=decision, clock=time.perf_counter)
        _print_log(args.run_dir, result.status, result.node)
        print(f"outcome: {result.state.outcome}")
        if result.state.narrative:
            print(f"narrative ({result.state.narrative_source}): {result.state.narrative}")
        return 0
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
