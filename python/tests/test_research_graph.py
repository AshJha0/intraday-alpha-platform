"""The research graph (iap.graph): engine, checkpoints, interrupts, the flow, the CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

import pytest
from conftest import REPO_ROOT
from iap.graph import (
    CheckpointError,
    Graph,
    GraphError,
    Interrupt,
    Router,
    StepLimitError,
    event_log,
    execute,
    resume,
    start,
    verify_chain,
)
from iap.graph.__main__ import main as cli_main
from iap.graph.core import state_sha256
from iap.graph.research_flow import (
    DEMO_HYPOTHESIS,
    build_research_graph,
    fallback_narrative,
    stub_deps,
    triage,
    validate_hypothesis,
)


class TickClock:
    """A deterministic clock: each call advances by one millisecond."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.001
        return self.t


@dataclass(frozen=True)
class S:
    n: int = 0
    decision: dict | None = None

    def check(self) -> None:
        if self.n < 0:
            raise ValueError("n must be non-negative")


@dataclass(frozen=True)
class Plain:  # no check()
    n: int = 0


@dataclass(frozen=True)
class Required:
    n: int


def inc(s):
    return {"n": s.n + 1}


def same(s):
    return {}


def small(**kw):
    base = dict(
        name="g",
        schema=S,
        nodes={"a": inc, "b": inc, "end": same},
        entry="a",
        edges={"a": "b", "b": "end"},
        terminals={"end"},
    )
    base.update(kw)
    return Graph(**base)


# -- build-time validation -----------------------------------------------------
@pytest.mark.parametrize(
    "kw, message",
    [
        ({"entry": "zz"}, "entry 'zz' is not a node"),
        ({"terminals": set()}, "no terminal"),
        ({"terminals": {"end", "zz"}}, "terminal 'zz' is not a node"),
        ({"edges": {"a": "b", "b": "end", "zz": "a"}}, "edge from unknown node 'zz'"),
        ({"edges": {"a": "zz", "b": "end"}}, "unknown node 'zz'"),
        ({"edges": {"a": "b", "b": "end", "end": "a"}}, "terminal 'end' has an outgoing edge"),
        ({"edges": {"a": "b"}}, "node 'b' has no outgoing edge"),
        (
            {"routers": {"a": Router(lambda s: "b", frozenset({"b"}))}},
            "both a static and a conditional edge",
        ),
        (
            {"edges": {"b": "end"}, "routers": {"a": Router(lambda s: "b", frozenset())}},
            "declares no target",
        ),
        (
            {"edges": {"b": "end"}, "routers": {"a": Router(lambda s: "b", frozenset({"b", "q"}))}},
            "unknown node 'q'",
        ),
        (
            {
                "nodes": {"a": inc, "b": inc, "end": same, "orphan": same},
                "edges": {"a": "b", "b": "end", "orphan": "end"},
            },
            "unreachable node(s): orphan",
        ),
        ({"schema": dict}, "frozen dataclass"),
        ({"decision_field": "nope"}, "decision field 'nope'"),
    ],
)
def test_build_time_validation(kw, message):
    with pytest.raises(GraphError, match=message.replace("(", r"\(").replace(")", r"\)")):
        small(**kw)


def test_unfrozen_schema_refused():
    @dataclass
    class Loose:
        n: int = 0

    with pytest.raises(GraphError, match="frozen"):
        small(schema=Loose)


def test_research_graph_builds_and_has_expected_shape():
    g = build_research_graph(stub_deps())
    assert g.reachable() == set(g.nodes)
    assert g.terminals == {"invalid", "duplicate_stop", "rejected", "refused", "done"}
    assert len(g.fingerprint()) == 64


# -- transitions and execution -------------------------------------------------
def test_execute_runs_to_terminal_with_event_log():
    g = small()
    r = execute(g, g.make_state({}), clock=TickClock())
    assert r.status == "done" and r.node == "end" and r.state.n == 2
    assert [e.node for e in r.events] == ["a", "b", "end"]
    assert [e.outcome for e in r.events] == ["ok", "ok", "terminal"]
    assert r.events[0].output_sha256 == r.events[1].input_sha256
    assert all(e.duration == pytest.approx(0.001) for e in r.events)


def test_state_validation_on_every_transition():
    g = small(nodes={"a": lambda s: {"n": -1}, "b": inc, "end": same})
    with pytest.raises(GraphError, match="state check failed"):
        execute(g, g.make_state({}), clock=TickClock())
    g = small(nodes={"a": lambda s: {"bogus": 1}, "b": inc, "end": same})
    with pytest.raises(GraphError, match="unknown state field"):
        execute(g, g.make_state({}), clock=TickClock())
    g = small(nodes={"a": lambda s: ["n"], "b": inc, "end": same})
    with pytest.raises(GraphError, match="not a mapping"):
        execute(g, g.make_state({}), clock=TickClock())
    g = small(nodes={"a": lambda s: {"n": object()}, "b": inc, "end": same})
    with pytest.raises(GraphError, match="canonical JSON"):
        execute(g, g.make_state({}), clock=TickClock())


def test_schema_without_check_and_schema_mismatch():
    g = small(schema=Plain)
    assert execute(g, g.make_state({}), clock=TickClock()).state.n == 2
    g = small(schema=Required)
    with pytest.raises(GraphError, match="does not fit the schema"):
        g.make_state({})


def test_router_returning_undeclared_target_is_an_error():
    g = small(edges={"b": "end"}, routers={"a": Router(lambda s: "end", frozenset({"b"}))})
    with pytest.raises(GraphError, match="returned 'end', not one of"):
        execute(g, g.make_state({}), clock=TickClock())


def test_step_limit_on_a_cycle():
    g = small(edges={"b": "end"}, routers={"a": Router(lambda s: "a", frozenset({"a", "b"}))})
    with pytest.raises(StepLimitError, match="step limit 5"):
        execute(g, g.make_state({}), clock=TickClock(), max_steps=5)
    with pytest.raises(GraphError, match="at least 1"):
        execute(g, g.make_state({}), clock=TickClock(), max_steps=0)
    with pytest.raises(GraphError, match="unknown node 'zz'"):
        execute(g, g.make_state({}), "zz", clock=TickClock())


def test_interrupt_without_observer():
    g = small(nodes={"a": lambda s: Interrupt("wait"), "b": inc, "end": same})
    r = execute(g, g.make_state({}), clock=TickClock())
    assert r.status == "waiting" and r.node == "a" and r.interrupt.reason == "wait"
    assert r.events[-1].outcome == "interrupt"


def test_mermaid_is_generated_from_the_definition_and_matches_the_doc():
    text = build_research_graph(stub_deps()).to_mermaid()
    assert text.startswith("flowchart TD\n")
    assert "prior_art -.-> duplicate_stop" in text and "triage --> narrative" in text
    doc = (REPO_ROOT / "docs" / "RESEARCH_GRAPH.md").read_text(encoding="utf-8")
    assert "```mermaid\n" + text + "```" in doc


# -- checkpoints ---------------------------------------------------------------
def approval_graph():
    def gate(s):
        return Interrupt("approve?", {"n": s.n}) if s.decision is None else {}

    return small(
        nodes={"a": inc, "gate": gate, "b": inc, "end": same},
        edges={"a": "gate", "gate": "b", "b": "end"},
        decision_field="decision",
    )


def test_checkpoint_chain_and_resume(tmp_path):
    g = approval_graph()
    r = start(g, {}, tmp_path / "run", clock=TickClock())
    assert r.status == "waiting"
    records = verify_chain(tmp_path / "run")
    assert [x["status"] for x in records] == ["running", "running", "waiting"]
    assert records[-1]["awaiting"] == {"reason": "approve?", "payload": {"n": 1}}
    with pytest.raises(CheckpointError, match="decision is required"):
        resume(tmp_path / "run", g, clock=TickClock())
    r = resume(tmp_path / "run", g, decision={"ok": True}, clock=TickClock())
    assert r.status == "done" and r.state.n == 2
    records = verify_chain(tmp_path / "run")
    assert records[3]["decision"] == {"ok": True}
    assert [e["step"] for e in event_log(tmp_path / "run")] == [1, 2, 3, 4, 5]
    with pytest.raises(CheckpointError, match="already done"):
        resume(tmp_path / "run", g, clock=TickClock())


def test_resume_after_a_stop_between_nodes(tmp_path):
    g = small()
    run = tmp_path / "run"
    start(g, {}, run, clock=TickClock())
    full = [p.read_text() for p in sorted(run.glob("ckpt-*.json"))]
    for p in sorted(run.glob("ckpt-*.json"))[2:]:
        p.unlink()  # as if the process died after node "a"
    with pytest.raises(CheckpointError, match="not waiting"):
        resume(run, g, decision={"x": 1}, clock=TickClock())
    r = resume(run, g, clock=TickClock())
    assert r.status == "done" and r.state.n == 2
    assert [p.read_text() for p in sorted(run.glob("ckpt-*.json"))] == full


def test_deterministic_replay_gives_identical_checkpoints(tmp_path):
    hashes = []
    for name in ("one", "two"):
        g = build_research_graph(stub_deps())
        start(g, {"hypothesis": DEMO_HYPOTHESIS}, tmp_path / name, clock=TickClock())
        resume(tmp_path / name, g, decision={"approved": True, "by": "t"}, clock=TickClock())
        hashes.append([p.read_bytes() for p in sorted((tmp_path / name).glob("ckpt-*.json"))])
    assert hashes[0] == hashes[1] and len(hashes[0]) == 11


def test_run_dir_must_be_empty(tmp_path):
    (tmp_path / "x").write_text("x")
    with pytest.raises(CheckpointError, match="not empty"):
        start(small(), {}, tmp_path, clock=TickClock())


def _waiting_run(tmp_path):
    run = tmp_path / "run"
    start(approval_graph(), {}, run, clock=TickClock())
    return run


def test_tampered_checkpoint_is_refused(tmp_path):
    run = _waiting_run(tmp_path)
    path = run / "ckpt-000001.json"
    wrapper = json.loads(path.read_text())
    wrapper["record"]["state"]["n"] = 99
    path.write_text(json.dumps(wrapper))
    with pytest.raises(CheckpointError, match="does not match its sha256"):
        resume(run, approval_graph(), decision={}, clock=TickClock())


def test_rechained_checkpoint_is_refused(tmp_path):
    from iap.governance.blackboard import digest

    run = _waiting_run(tmp_path)
    path = run / "ckpt-000001.json"
    wrapper = json.loads(path.read_text())
    wrapper["record"]["prev_sha256"] = "f" * 64
    wrapper["sha256"] = digest(wrapper["record"])
    path.write_text(json.dumps(wrapper))
    with pytest.raises(CheckpointError, match="does not chain"):
        verify_chain(run)


def test_gap_unreadable_and_empty_chains_are_refused(tmp_path):
    with pytest.raises(CheckpointError, match="no checkpoint"):
        verify_chain(tmp_path)
    run = _waiting_run(tmp_path)
    (run / "ckpt-000001.json").unlink()
    with pytest.raises(CheckpointError, match="gap"):
        verify_chain(run)
    run2 = tmp_path / "two"
    start(approval_graph(), {}, run2, clock=TickClock())
    (run2 / "ckpt-000002.json").write_text("{not json")
    with pytest.raises(CheckpointError, match="unreadable"):
        verify_chain(run2)


def test_resume_refuses_a_different_graph_and_a_graph_without_decision_field(tmp_path):
    run = _waiting_run(tmp_path)
    with pytest.raises(CheckpointError, match="different graph"):
        resume(run, small(), decision={}, clock=TickClock())
    g = approval_graph()
    g.decision_field = None  # same shape, no decision field
    with pytest.raises(CheckpointError, match="no decision field"):
        resume(run, g, decision={}, clock=TickClock())


def test_nothing_is_written_outside_the_run_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g = build_research_graph(stub_deps())
    start(g, {"hypothesis": DEMO_HYPOTHESIS}, tmp_path / "run", clock=TickClock())
    resume(tmp_path / "run", g, decision={"approved": True}, clock=TickClock())
    assert [p.name for p in tmp_path.iterdir()] == ["run"]
    assert all(p.name.startswith("ckpt-") for p in (tmp_path / "run").iterdir())


# -- the research flow ---------------------------------------------------------
def run_flow(tmp_path, deps=None, hypothesis=DEMO_HYPOTHESIS, decision=None):
    g = build_research_graph(deps or stub_deps())
    r = start(g, {"hypothesis": hypothesis}, tmp_path / "run", clock=TickClock())
    if r.status == "waiting" and decision is not None:
        r = resume(tmp_path / "run", g, decision=decision, clock=TickClock())
    return r


def test_flow_stops_for_approval_with_an_unsigned_draft(tmp_path):
    r = run_flow(tmp_path)
    assert r.status == "waiting" and r.node == "human_approval"
    draft = r.state.draft
    assert draft["signed"] is False and draft["kind"] == "prereg_draft"
    assert draft["analysis_identity"]["data_versions"] == ["demo_bars_2025q1"]
    assert r.interrupt.payload["prereg_id"] == draft["prereg_id"]


def test_flow_approved_runs_to_done(tmp_path):
    r = run_flow(tmp_path, decision={"approved": True, "by": "owner"})
    assert r.state.outcome == "done" and r.state.verdict["verdict"] == "pass"
    assert r.state.narrative_source == "model"


def test_flow_rejected(tmp_path):
    r = run_flow(tmp_path, decision={"approved": False, "by": "owner"})
    assert r.node == "rejected" and r.state.outcome == "rejected" and r.state.run is None


def test_malformed_decision_is_refused(tmp_path):
    run_flow(tmp_path)
    with pytest.raises(GraphError, match="decision.approved"):
        resume(
            tmp_path / "run",
            build_research_graph(stub_deps()),
            decision={"approved": "yes"},
            clock=TickClock(),
        )


def test_duplicate_stop_route(tmp_path):
    deps = stub_deps(
        search_prior=lambda h: [
            {"id": "exp-2", "same_test": False},
            {"id": "exp-1", "same_test": True},
        ]
    )
    r = run_flow(tmp_path, deps)
    assert r.status == "done" and r.node == "duplicate_stop"
    assert r.state.duplicate_of == "exp-1" and r.state.draft is None


def test_invalid_hypothesis_route(tmp_path):
    r = run_flow(tmp_path, hypothesis={"alpha_id": "x", "horizon": "7m"})
    assert r.node == "invalid" and len(r.state.errors) == 4


def test_holdout_dataset_is_refused_and_runner_not_called(tmp_path):
    calls = []
    h = {
        **DEMO_HYPOTHESIS,
        "universe": {"name": "u", "datasets": ["bars_2025", "bars_2026_holdout"]},
    }
    deps = stub_deps(run_in_sample=lambda d: calls.append(d) or {})
    r = run_flow(tmp_path, deps, hypothesis=h, decision={"approved": True})
    assert r.node == "refused" and r.state.refused == ["bars_2026_holdout"]
    assert calls == [] and r.state.run is None


@pytest.mark.parametrize(
    "verify, write",
    [
        (lambda text, facts: False, None),
        (lambda text, facts: 1 / 0, None),
        (None, lambda facts: ""),
    ],
)
def test_narrative_falls_back_when_verification_fails(tmp_path, verify, write):
    over = {}
    if verify:
        over["verify"] = verify
    if write:
        over["write"] = write
    r = run_flow(tmp_path, stub_deps(**over), decision={"approved": True})
    assert r.state.narrative_source == "fallback"
    assert r.state.narrative.startswith("demo_order_imbalance at horizon 5m: in-sample t = 2.40")


@pytest.mark.parametrize(
    "run, verdict",
    [
        ({"t_stat": 2.5, "n_sessions": 20}, "pass"),
        ({"t_stat": 1.5, "n_sessions": 20}, "fail"),
        ({"t_stat": -3.0, "n_sessions": 20}, "wrong_sign"),
        ({"t_stat": 5.0, "n_sessions": 3}, "insufficient_data"),
    ],
)
def test_triage_is_code(run, verdict):
    draft = {
        "expected_sign": 1,
        "analysis_identity": {"thresholds": {"min_abs_t": 2.0, "min_sessions": 10}},
    }
    assert triage(run, draft)["verdict"] == verdict


def test_validate_hypothesis_and_fallback_text():
    assert validate_hypothesis(DEMO_HYPOTHESIS) == []
    bad = {**DEMO_HYPOTHESIS, "universe": {"name": "u", "datasets": []}, "mechanism": "short"}
    assert len(validate_hypothesis(bad)) == 2
    facts = {
        "alpha_id": "a",
        "horizon": "5m",
        **triage(
            {"t_stat": 1, "n_sessions": 1},
            {
                "expected_sign": -1,
                "analysis_identity": {"thresholds": {"min_abs_t": 2, "min_sessions": 5}},
            },
        ),
    }
    assert "verdict insufficient_data" in fallback_narrative(facts)


def test_same_state_same_hash():
    g = build_research_graph(stub_deps())
    a = g.make_state({"hypothesis": DEMO_HYPOTHESIS})
    b = g.make_state({"hypothesis": dict(reversed(list(DEMO_HYPOTHESIS.items())))})
    assert state_sha256(a) == state_sha256(b)


# -- LangGraph adapter ---------------------------------------------------------
def test_adapter_import_does_not_import_langgraph():
    code = (
        "import sys, iap.graph, iap.graph.langgraph_adapter, iap.graph.research_flow;"
        "assert not any(m == 'langgraph' or m.startswith('langgraph.') for m in sys.modules)"
    )
    env = {"PYTHONPATH": str(REPO_ROOT / "python" / "src")}
    import os

    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, **env})


def test_adapter_missing_dependency_error(monkeypatch):
    from iap.graph import langgraph_adapter

    monkeypatch.setitem(sys.modules, "langgraph", None)  # import of langgraph now fails
    monkeypatch.setitem(sys.modules, "langgraph.graph", None)
    with pytest.raises(langgraph_adapter.LangGraphUnavailable, match="optional 'langgraph'"):
        langgraph_adapter.to_langgraph(small())


# -- CLI -----------------------------------------------------------------------
def test_cli_demo_and_resume_end_to_end(tmp_path, capsys):
    out = tmp_path / "demo"
    assert cli_main(["demo", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "status: waiting at human_approval" in text and "awaiting:" in text
    assert cli_main(["resume", str(out), "--approve", "--by", "tester"]) == 0
    text = capsys.readouterr().out
    assert (
        "status: done at done" in text and "outcome: done" in text and "narrative (model)" in text
    )
    assert verify_chain(out)[5]["decision"] == {"approved": True, "by": "tester"}
    assert cli_main(["resume", str(out), "--reject"]) == 2
    assert "already done" in capsys.readouterr().err


def test_cli_reject_and_diagram(tmp_path, capsys):
    out = tmp_path / "demo"
    cli_main(["demo", "--out", str(out)])
    capsys.readouterr()
    assert cli_main(["resume", str(out), "--reject"]) == 0
    assert "outcome: rejected" in capsys.readouterr().out
    assert cli_main(["diagram"]) == 0
    assert capsys.readouterr().out == build_research_graph(stub_deps()).to_mermaid()


def test_cli_as_a_module(tmp_path):
    import os

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "python" / "src")}
    done = subprocess.run(
        [sys.executable, "-m", "iap.graph", "demo", "--out", str(tmp_path / "m")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "human_approval" in done.stdout
