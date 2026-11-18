"""In-house tracing (iap.llm.tracing): span trees from scripted sessions, the
exporter, the graph hook and the reader.  Scripted client only; no network,
no key, no spend."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from iap.graph import Graph, Interrupt, execute
from iap.llm import evals, tracing
from iap.llm.agent import open_session, run_session
from iap.llm.budget import Budget
from iap.llm.fake import ScriptedClient, Usage, text, tool
from iap.llm.loop import ToolRefused, ToolSpec, run_tool_loop
from iap.llm.runners import planted_runner
from iap.llm.tracing import JsonlExporter, Tracer, counter_ids

SECRET = "sk-ant-test-TRACE-0123456789abcdef"
TASK = "PROMPT-SENTINEL find the thing"
TOOLS = [
    ToolSpec("probe", "a probe", {"x": {"type": "string"}}, ("x",)),
    ToolSpec("finish", "done", {"summary": {"type": "string"}}, ("summary",)),
]


def tick_clock(step: int = 1_000_000):
    t = [0]

    def clock() -> int:
        t[0] += step
        return t[0]

    return clock


def make_tracer(path):
    return Tracer(JsonlExporter(path, SECRET), clock=tick_clock(), ids=counter_ids())


def dispatch(name, args):
    if name == "probe":
        if args["x"] == "crash":
            raise KeyError("boom")
        if args["x"] == "no":
            raise ToolRefused("not allowed")
        return {"echo": args["x"], "secret_echo": SECRET}
    return {"summary": args["summary"]}


def run(tmp_path, steps, budget=None, tracer=None, usage=None):
    client = ScriptedClient(steps, usage=usage, model="claude-haiku-5-5-served")
    res = run_tool_loop(
        client,
        system="SYSTEM-SENTINEL",
        tools=TOOLS,
        dispatch=dispatch,
        task=TASK,
        budget=budget or Budget(),
        model="claude-haiku-5-5",
        out_dir=tmp_path / "out",
        session_id="s1",
        secret=SECRET,
        tracer=tracer,
    )
    return res


def spans_of(tmp_path):
    return tracing.load_spans(tmp_path / "out")


def by_name(spans):
    return {s["name"]: s for s in spans}


def test_span_tree_of_a_scripted_session(tmp_path):
    tr = make_tracer(tmp_path / "out" / "spans.jsonl")
    steps = [
        [text("COMPLETION-SENTINEL"), tool("probe", x="hello")],
        [tool("finish", summary="SUMMARY-SENTINEL")],
    ]
    res = run(tmp_path, steps, tracer=tr, usage=Usage(1000, 100))
    assert res.status == "finished"
    spans = spans_of(tmp_path)
    assert [s["name"] for s in spans] == [
        "invoke_agent research",
        "chat claude-haiku-5-5",
        "execute_tool probe",
        "chat claude-haiku-5-5",
        "execute_tool finish",
    ]
    root = spans[0]
    assert root["parent_id"] is None and root["kind"] == "INTERNAL"
    assert root["trace_id"] == f"{1:032x}" and root["span_id"] == f"{2:016x}"
    assert all(s["trace_id"] == root["trace_id"] for s in spans)
    assert all(s["parent_id"] == root["span_id"] for s in spans[1:])
    assert len({s["span_id"] for s in spans}) == 5
    assert all(s["status"] == "OK" for s in spans)
    assert all(a["end_ns"] <= b["start_ns"] for a, b in zip(spans[1:], spans[2:], strict=False))
    assert root["start_ns"] < spans[1]["start_ns"] and root["end_ns"] > spans[-1]["end_ns"]
    assert root["attributes"]["iap.session.status"] == "finished"
    assert root["attributes"]["gen_ai.conversation.id"] == "s1"
    m1, m2 = spans[1]["attributes"], spans[3]["attributes"]
    assert spans[1]["kind"] == "CLIENT"
    # ScriptedClient: first call writes 1500 cached tokens, later calls read them
    assert m1["iap.usage.uncached_input_tokens"] == 1000
    assert m1["gen_ai.usage.cache_write.input_tokens"] == 1500
    assert m1["gen_ai.usage.cache_read.input_tokens"] == 0
    assert m1["gen_ai.usage.input_tokens"] == 2500
    assert m2["gen_ai.usage.cache_read.input_tokens"] == 1500
    assert m1["gen_ai.usage.output_tokens"] == 100
    assert m1["gen_ai.request.model"] == "claude-haiku-5-5"
    assert m1["gen_ai.response.model"] == "claude-haiku-5-5-served"
    assert m1["gen_ai.response.finish_reasons"] == ["tool_use"]
    assert m1["gen_ai.provider.name"] == "anthropic"
    assert (m1["iap.turn"], m2["iap.turn"]) == (0, 1)
    assert m1["iap.cost.usd"] + m2["iap.cost.usd"] == pytest.approx(res.budget["usd"], abs=1e-6)
    t = spans[2]["attributes"]
    assert t["gen_ai.tool.name"] == "probe" and t["iap.tool.outcome"] == "ok"
    assert t["gen_ai.tool.call.id"].startswith("toolu_")
    assert t["iap.tool.args_bytes"] == len(json.dumps({"x": "hello"}).encode())
    assert t["iap.tool.result_bytes"] > 0


def test_deterministic_bytes(tmp_path):
    def steps():
        return [[tool("probe", x="a")], [tool("finish", summary="s")]]

    run(tmp_path / "a", steps(), tracer=make_tracer(tmp_path / "a" / "out" / "spans.jsonl"))
    run(tmp_path / "b", steps(), tracer=make_tracer(tmp_path / "b" / "out" / "spans.jsonl"))
    a = spans_of(tmp_path / "a")
    b = spans_of(tmp_path / "b")
    for s in a + b:  # tool-call ids come from a process-wide counter in the fake
        s["attributes"].pop("gen_ai.tool.call.id", None)
    assert a == b


def test_refused_and_crashing_tools(tmp_path):
    steps = [
        [tool("probe", x="no"), tool("probe", x="crash"), tool("nope")],
        [tool("finish", summary="s")],
    ]
    res = run(tmp_path, steps)  # default tracer: spans.jsonl in out_dir
    assert res.status == "finished"
    tools = [s for s in spans_of(tmp_path) if s["name"].startswith("execute_tool")]
    got = [
        (
            s["name"],
            s["status"],
            s["attributes"]["iap.tool.outcome"],
            s["attributes"].get("error.type"),
        )
        for s in tools
    ]
    assert got == [
        ("execute_tool probe", "ERROR", "refused", "ToolRefused"),
        ("execute_tool probe", "ERROR", "error", "KeyError"),
        ("execute_tool nope", "ERROR", "refused", "ToolRefused"),
        ("execute_tool finish", "OK", "ok", None),
    ]


def test_budget_refusals_are_events(tmp_path):
    steps = [[tool("probe", x="a"), tool("probe", x="b")]]
    res = run(tmp_path, steps, budget=Budget(max_tool_calls=1))
    assert res.status == "budget_exhausted"
    spans = spans_of(tmp_path)
    second = [s for s in spans if s["name"] == "execute_tool probe"][1]
    assert second["attributes"]["error.type"] == "BudgetExceeded"
    assert [e["name"] for e in second["events"]] == ["budget_refusal"]
    # a refused model call: an event on the root span, no chat span
    res2 = run(tmp_path / "m", [[tool("probe", x="a")]], budget=Budget(max_usd=1e-9))
    assert res2.status == "budget_exhausted"
    spans2 = spans_of(tmp_path / "m")
    assert [s["name"] for s in spans2] == ["invoke_agent research"]
    assert spans2[0]["events"][0]["name"] == "budget_refusal"
    assert spans2[0]["attributes"]["iap.budget.refusals"] == 1


def test_no_text_and_no_key_in_spans(tmp_path):
    steps = [
        [text("COMPLETION-SENTINEL"), tool("probe", x="ARG-SENTINEL")],
        [tool("probe", x="crash")],
        [tool("finish", summary="SUMMARY-SENTINEL " + SECRET)],
    ]
    run(tmp_path, steps)
    raw = (tmp_path / "out" / "spans.jsonl").read_text(encoding="utf-8")
    for needle in (SECRET, "SENTINEL", "boom", "find the thing"):
        assert needle not in raw


def test_exporter_failure_does_not_change_the_loop(tmp_path):
    class Broken:
        def export(self, span):
            raise OSError("disk full")

    def steps():
        return [
            [tool("probe", x="a"), tool("probe", x="crash")],
            [tool("finish", summary="s")],
        ]

    ref = run(tmp_path / "ref", steps(), tracer=Tracer(None))
    tr = Tracer(Broken())
    got = run(tmp_path / "bad", steps(), tracer=tr)
    assert tr.failures == 6  # every export failed, nothing raised
    a, b = ref.as_dict(), got.as_dict()
    for d in (a, b):
        d.pop("out_dir")
        for c in d["tool_calls"]:
            c.pop("t")
    assert a == b

    def bad_clock():
        raise RuntimeError("clock")

    tr2 = Tracer(None, clock=bad_clock)
    got2 = run(tmp_path / "boom", steps(), tracer=tr2)
    assert got2.status == "finished" and tr2.failures > 0


def test_run_session_traced(tmp_path):
    ws, priv = evals._workspace(tmp_path, "ws")
    s = open_session(
        ws,
        evals.AGENT,
        priv,
        planted_runner(),
        Budget(),
        fingerprinter=evals.fake_fingerprint,
        session_id="t1",
    )
    client = ScriptedClient([[tool("list_alphas")], [tool("finish", summary="ok")]])
    out = run_session(client, s, TASK, secret=SECRET)
    assert out["status"] == "finished"
    spans = tracing.load_spans(s.dir)
    assert spans[0]["name"] == f"invoke_agent {evals.AGENT}"
    assert [x["attributes"].get("gen_ai.tool.name") for x in spans if x["kind"] == "INTERNAL"][
        1:
    ] == [
        "list_alphas",
        "finish",
    ]
    assert SECRET not in (s.dir / "spans.jsonl").read_text(encoding="utf-8")


@dataclass(frozen=True)
class S:
    n: int = 0
    decision: str | None = None


def _graph():
    return Graph(
        "g",
        S,
        {
            "a": lambda s: {"n": s.n + 1},
            "b": lambda s: Interrupt("needs approval") if s.decision is None else {"n": s.n + 10},
            "c": lambda s: {"n": s.n * 2},
        },
        entry="a",
        edges={"a": "b", "b": "c"},
        terminals=["c"],
        decision_field="decision",
    )


def _clock():
    t = [0.0]

    def c():
        t[0] += 0.001
        return t[0]

    return c


def test_graph_hook(tmp_path):
    g = _graph()
    plain = execute(g, g.make_state({}), clock=_clock())
    seen = []
    tr = make_tracer(tmp_path / "spans.jsonl")
    traced = execute(
        g, g.make_state({}), clock=_clock(), tracer=tr, observer=lambda e, s, i: seen.append(e.node)
    )
    assert traced == plain and seen == ["a", "b"]
    spans = tracing.load_spans(tmp_path)
    assert [s["name"] for s in spans] == ["invoke_workflow g", "graph_node a", "graph_node b"]
    root, a, b = spans
    assert a["parent_id"] == b["parent_id"] == root["span_id"]
    assert a["attributes"]["iap.graph.input_sha256"] == plain.events[0].input_sha256
    assert a["attributes"]["iap.graph.output_sha256"] == plain.events[0].output_sha256
    assert b["attributes"]["iap.graph.outcome"] == "interrupt"
    assert b["attributes"]["iap.graph.interrupt_reason"] == "needs approval"
    assert root["attributes"]["iap.graph.status"] == "waiting"
    # resume from b with a decision: a second root
    st = g.apply(plain.state, {"decision": "yes"})
    done = execute(g, st, "b", clock=_clock(), tracer=tr)
    assert done.status == "done"
    assert tracing.load_spans(tmp_path)[-1]["attributes"]["iap.graph.outcome"] == "terminal"


def test_graph_hook_node_error(tmp_path):
    g = Graph("e", S, {"a": lambda s: 1 / 0}, entry="a", terminals=["a"])
    tr = make_tracer(tmp_path / "spans.jsonl")
    with pytest.raises(ZeroDivisionError):
        execute(g, g.make_state({}), clock=_clock(), tracer=tr)
    spans = tracing.load_spans(tmp_path)
    assert [(s["name"], s["status"], s["attributes"]["error.type"]) for s in spans] == [
        ("invoke_workflow e", "ERROR", "ZeroDivisionError"),
        ("graph_node a", "ERROR", "ZeroDivisionError"),
    ]


def test_summarize(tmp_path, capsys):
    steps = [[tool("probe", x="a"), tool("probe", x="no")], [tool("finish", summary="s")]]
    run(
        tmp_path,
        steps,
        tracer=make_tracer(tmp_path / "out" / "spans.jsonl"),
        usage=Usage(1000, 100),
    )
    assert tracing.main(["summarize", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "model calls: 2" in out
    assert "output tokens: 200" in out
    assert "tool calls: 3" in out
    assert "probe: ok x1" in out and "probe: refused x1" in out and "finish: ok x1" in out
    assert "chat: n=2" in out and "execute_tool: n=3" in out
    assert "status (invoke_agent research): finished" in out
    assert "waterfall:" in out and " execute_tool probe !" in out
    assert tracing.main(["summarize", str(tmp_path / "missing")]) == 1


def test_to_otel_without_the_package(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(RuntimeError, match="opentelemetry"):
        tracing.to_otel([])
