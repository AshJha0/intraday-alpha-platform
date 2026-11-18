"""Robustness of the generic tool loop (:func:`iap.llm.loop.run_tool_loop`):
a tool that crashes, a client that raises, a truncated tool call and a
paused turn.  All driven by the scripted client; no network."""

from __future__ import annotations

import json

import pytest
from iap.llm.budget import Budget
from iap.llm.fake import Response, ScriptedClient, text, tool
from iap.llm.loop import ToolSpec, run_tool_loop

SECRET = "sk-ant-test-0123456789abcdef"
TOOLS = [
    ToolSpec("probe", "a probe", {"x": {"type": "string"}}, ("x",)),
    ToolSpec("finish", "done", {"summary": {"type": "string"}}, ("summary",)),
]


class StopClient(ScriptedClient):
    """A scripted client whose steps may force a ``stop_reason``: a step is
    ``(blocks, stop_reason)``."""

    def __init__(self, steps):
        super().__init__([b for b, _ in steps])
        self.stops = [s for _, s in steps]
        inner = self.messages.create

        def create(**kw):
            i = self.i
            resp = inner(**kw)
            if i < len(self.stops) and self.stops[i]:
                return Response(resp.content, self.stops[i], resp.usage, resp.model)
            return resp

        self.messages.create = create


def _run(client, tmp_path, dispatch, **kw):
    return run_tool_loop(
        client,
        system="sys",
        tools=TOOLS,
        dispatch=dispatch,
        task="go",
        budget=Budget(),
        model="claude-haiku-5-5",
        out_dir=tmp_path / "out",
        session_id="s1",
        secret=SECRET,
        **kw,
    )


def _session(tmp_path):
    return json.loads((tmp_path / "out" / "session.json").read_text(encoding="utf-8"))


def _finish_dispatch(calls):
    def dispatch(name, args):
        calls.append((name, dict(args)))
        if name == "probe":
            raise RuntimeError(f"boom with {SECRET}")
        return {"summary": args["summary"]}

    return dispatch


def test_tool_exception_becomes_error_result_and_loop_continues(tmp_path):
    calls: list = []
    seen: list = []

    def second(results):
        seen.extend(results)
        return [tool("finish", summary="ok")]

    client = ScriptedClient([[tool("probe", x="a")], second])
    res = _run(client, tmp_path, _finish_dispatch(calls))
    assert res.status == "finished" and res.finished == "ok"
    # the model was told: exception type and text, key scrubbed
    # history: task, probe call, its result, finish call, its result
    last_user = client.requests[1]["messages"][2]["content"][0]
    assert last_user["is_error"] is True
    assert "RuntimeError" in last_user["content"] and "boom" in last_user["content"]
    assert SECRET not in last_user["content"]
    logged = [
        json.loads(line)
        for line in (tmp_path / "out" / "tool_log.jsonl").read_text("utf-8").splitlines()
    ]
    assert logged[0]["ok"] is False and "RuntimeError" in logged[0]["error"]
    assert SECRET not in (tmp_path / "out" / "tool_log.jsonl").read_text("utf-8")
    assert _session(tmp_path)["status"] == "finished"


def test_client_error_still_writes_session_then_reraises(tmp_path):
    client = ScriptedClient([[tool("probe", x="a")]])

    def broken(**kw):
        raise ConnectionError("no network")

    client.messages.create = broken
    with pytest.raises(ConnectionError):
        _run(client, tmp_path, lambda n, a: {})
    assert _session(tmp_path)["status"] == "client_error"


def test_truncated_tool_call_is_not_dispatched(tmp_path):
    calls: list = []
    client = StopClient([([tool("finish", summary="cut")], "max_tokens")])
    res = _run(client, tmp_path, _finish_dispatch(calls))
    assert res.status == "max_tokens" and calls == [] and res.finished is None
    assert not (tmp_path / "out" / "tool_log.jsonl").exists()
    lines = (tmp_path / "out" / "transcript.jsonl").read_text("utf-8").splitlines()
    assert json.loads(lines[-1])["stop_reason"] == "max_tokens"  # the turn is logged
    assert _session(tmp_path)["status"] == "max_tokens"


def test_paused_turn_continues(tmp_path):
    calls: list = []
    client = StopClient(
        [([text("thinking...")], "pause_turn"), ([tool("finish", summary="done")], None)]
    )
    res = _run(client, tmp_path, _finish_dispatch(calls))
    assert res.status == "finished" and calls == [("finish", {"summary": "done"})]
    assert len(client.requests) == 2
    msgs = client.requests[1]["messages"]  # task, paused turn, finish call, result
    assert [m["role"] for m in msgs[:3]] == ["user", "assistant", "assistant"]


def test_paused_turns_count_against_max_turns(tmp_path):
    client = StopClient([([text("..")], "pause_turn")] * 5)
    res = _run(client, tmp_path, lambda n, a: {}, max_turns=3)
    assert res.status == "max_turns" and len(client.requests) == 3
