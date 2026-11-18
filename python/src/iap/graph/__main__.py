"""``python -m iap.graph demo|run|resume|diagram``.

``demo --out DIR`` runs the research flow with the built-in stubs up to the
human approval interrupt.  ``run --hypothesis FILE.json --out DIR`` runs the
wired flow (:mod:`iap.graph.wiring`: real memory, holdout registry and number
verifier; a stub in-sample runner unless ``--runner module:function`` is
given) to the same stop and records how it was wired in ``DIR/flow.json``.
``resume DIR --approve|--reject`` records the decision and finishes either
kind of run with the graph that started it.  ``verify-text --artefact
FILE.json (--text T | --text-file F)`` checks every number in a summary
against the stored numbers of the artefact(s) and exits 1 on any problem.
Both print the event log of the run directory.  ``diagram`` prints the
Mermaid diagram generated from the graph definition.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from iap.memory.search import REPO_ROOT

from .checkpoint import event_log, resume, start
from .core import GraphError
from .research_flow import DEMO_HYPOTHESIS, build_research_graph, stub_deps

FLOW_FILE = "flow.json"


def _wired_graph(root: Path, runner: str | None):
    from .wiring import build_wired_flow, load_runner

    return build_wired_flow(root=root, run_in_sample=load_runner(runner) if runner else None)


def _graph_for(run_dir: Path):
    """The graph that started ``run_dir``: wired when ``flow.json`` says so."""
    flow = run_dir / FLOW_FILE
    if not flow.is_file():
        return build_research_graph(stub_deps())
    meta = json.loads(flow.read_text(encoding="utf-8"))
    return _wired_graph(Path(meta["root"]), meta.get("runner"))


def _print_log(run_dir: Path, status: str, node: str) -> None:
    for e in event_log(run_dir):
        print(
            f"{e['step']:>3}  {e['node']:<15} {e['outcome']:<9} -> {e['next'] or '-':<15} "
            f"in {e['input_sha256'][:12]} out {e['output_sha256'][:12]}"
        )
    print(f"status: {status} at {node}")


def _verify_text(args: argparse.Namespace) -> int:
    from .wiring import artefact_evidence, text_problems

    text = args.text if args.text is not None else args.text_file.read_text(encoding="utf-8")
    evidence = []
    for path in args.artefact:
        evidence.extend(artefact_evidence(json.loads(path.read_text(encoding="utf-8"))))
    problems = text_problems(text, evidence)
    for p in problems:
        print(f"problem: {p}")
    print("verified: every number matches a stored value" if not problems else "NOT verified")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m iap.graph")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run the research flow with stubs to the approval step")
    d.add_argument("--out", required=True, type=Path)
    w = sub.add_parser("run", help="run the wired flow on a hypothesis to the approval step")
    w.add_argument("--hypothesis", required=True, type=Path, help="hypothesis JSON file")
    w.add_argument("--out", required=True, type=Path)
    w.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    w.add_argument("--runner", help="in-sample runner as module:function (default: a stub)")
    r = sub.add_parser("resume", help="record the approval decision and finish the run")
    r.add_argument("run_dir", type=Path)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    r.add_argument("--by", default="owner", help="who decided (recorded in the checkpoint)")
    v = sub.add_parser("verify-text", help="check a summary's numbers against JSON artefacts")
    v.add_argument("--artefact", action="append", required=True, type=Path)
    t = v.add_mutually_exclusive_group(required=True)
    t.add_argument("--text")
    t.add_argument("--text-file", type=Path)
    sub.add_parser("diagram", help="print the Mermaid diagram of the research flow")
    args = p.parse_args(argv)

    try:
        if args.cmd == "verify-text":
            return _verify_text(args)
        if args.cmd == "diagram":
            graph = build_research_graph(stub_deps())
            print(graph.to_mermaid(), end="")
            return 0
        if args.cmd in ("demo", "run"):
            if args.cmd == "demo":
                graph, hyp = build_research_graph(stub_deps()), DEMO_HYPOTHESIS
            else:
                hyp = json.loads(args.hypothesis.read_text(encoding="utf-8"))
                graph = _wired_graph(args.root.resolve(), args.runner)
            result = start(graph, {"hypothesis": hyp}, args.out, clock=time.perf_counter)
            if args.cmd == "run":
                meta = {"flow": "wired", "root": str(args.root.resolve()), "runner": args.runner}
                (args.out / FLOW_FILE).write_text(json.dumps(meta, indent=2) + "\n", "utf-8")
            _print_log(args.out, result.status, result.node)
            st = result.state
            if st.duplicate_of:
                print(f"prior art: same test already in {st.duplicate_of}")
            for h in st.prior_hits:
                if h.get("related") and not h.get("same_test"):
                    print(f"check by hand: same alpha and horizon, {h.get('reason')}: {h['id']}")
            if st.draft is not None and result.interrupt is not None:
                print("draft (unsigned):")
                print(json.dumps(st.draft, indent=2, sort_keys=True))
            if result.interrupt is not None:
                print(f"awaiting: {result.interrupt.reason}")
                print(f"next: python -m iap.graph resume {args.out} --approve|--reject")
            return 0
        decision = {"approved": bool(args.approve), "by": args.by}
        graph = _graph_for(args.run_dir)
        result = resume(args.run_dir, graph, decision=decision, clock=time.perf_counter)
        _print_log(args.run_dir, result.status, result.node)
        print(f"outcome: {result.state.outcome}")
        if result.state.refused:
            print(f"refused datasets (holdout registry): {', '.join(result.state.refused)}")
        if result.state.narrative:
            print(f"narrative ({result.state.narrative_source}): {result.state.narrative}")
        return 0
    except (GraphError, OSError, ValueError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
