"""v1.11 AI1/AI2: the LLM research agent and its behaviour evals.

Everything here uses the scripted client (iap.llm.fake): no network, no API
key, no spend."""

from __future__ import annotations

import io
import json
import sys

import pytest
from iap.agents import citations, mcp_server
from iap.agents.blackboard import Blackboard
from iap.llm import evals, verify
from iap.llm.agent import SYSTEM_PROMPT, TOOL_NAMES, ToolRefused, open_session, run_session
from iap.llm.budget import Budget, BudgetExceeded, usage_cost
from iap.llm.envfile import ApiKeyError, load_api_key, parse_env_file, redact
from iap.llm.fake import ScriptedClient, Usage, tool
from iap.llm.runners import PLANTED_DATASET, planted_runner, verdict

SECRET = "sk-ant-test-0000-NOT-A-REAL-KEY"


def _session(tmp_path, **budget):
    ws, priv = evals._workspace(tmp_path, "ws")
    s = open_session(
        ws,
        evals.AGENT,
        priv,
        planted_runner(),
        Budget(**budget),
        fingerprinter=evals.fake_fingerprint,
        session_id="t1",
    )
    return s


def _study(s):
    s.propose_hypothesis("EQ02", "1s", 1, "ofi")
    pre = s.preregister("d1")
    out = s.run_gated_study("EQ02", "1s", PLANTED_DATASET)
    return pre, out


# -- key handling ---------------------------------------------------------


def test_env_file_parsing_and_errors_never_echo_the_key(tmp_path, monkeypatch):
    f = tmp_path / "x.env"
    f.write_text(f'# c\nexport ANTHROPIC_API_KEY="{SECRET}"\nOTHER=1\n', encoding="utf-8")
    assert parse_env_file(f)["OTHER"] == "1"
    assert load_api_key(f) == SECRET
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ApiKeyError) as e:
        load_api_key(None)
    assert SECRET not in str(e.value)
    (tmp_path / "empty.env").write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    with pytest.raises(ApiKeyError):
        load_api_key(tmp_path / "empty.env")
    assert redact(f"a {SECRET} b", SECRET) == "a [REDACTED] b"


def test_gitignore_covers_secrets():
    from conftest import REPO_ROOT

    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", "*.env", ".iap_keys/"} <= set(lines)


def test_sdk_is_optional():
    import importlib

    for m in ("iap.llm.agent", "iap.llm.evals", "iap.llm.verify"):
        importlib.import_module(m)
    assert "anthropic" not in sys.modules or sys.modules["anthropic"] is not None


# -- verification ---------------------------------------------------------


def test_numbers_identifiers_and_rounding():
    toks = [t for t, _ in verify.numbers_in_text("EQ02 at 10s: IC 0.0412, t=5.3. FX07 12%")]
    assert toks == ["0.0412", "5.3", "12"]
    assert verify.matches("0.041", False, [0.04118])
    assert not verify.matches("0.042", False, [0.04118])
    assert verify.matches("12", True, [0.12])


def test_free_text_numbers_are_not_evidence():
    doc = {"body": {"hypothesis": "t = 9.87", "ledger_total": 4}}
    assert 9.87 not in verify.leaves(doc) and 4.0 in verify.leaves(doc)


def test_report_and_board_citations_resolve(tmp_path):
    s = _session(tmp_path)
    pre, out = _study(s)
    assert citations.resolve(out["ref"], s.workspace)
    assert citations.resolve(pre["board_ref"], s.workspace)
    for bad in ("report:t1.r9", "report:..", "report:t1", "board:deadbeefdeadbeef", "board:12"):
        assert not citations.resolve(bad, s.workspace)


# -- the governed tools -----------------------------------------------------


def test_full_study_is_signed_debited_and_cited(tmp_path):
    s = _session(tmp_path)
    pre, out = _study(s)
    rep = out["report"]
    assert rep["verdict"] == "confirmed" and rep["dataset_version"] == PLANTED_DATASET
    assert pre["ledger_total"] == 1
    m = rep["metrics"]
    s.file_finding("EQ02", f"IC {m['gate_ic']} t {m['t_stat']}", [out["ref"], pre["board_ref"]])
    board = Blackboard(s.workspace / "research" / "agents" / "blackboard.jsonl")
    reg = json.loads((s.workspace / evals.PUBKEYS_RELPATH).read_text())
    assert board.verify_signatures(reg) == []
    assert [e["kind"] for e in board.entries()] == ["prereg", "task", "claim", "finding", "release"]


def test_refusals(tmp_path):
    s = _session(tmp_path, max_preregs=1)
    with pytest.raises(ToolRefused, match="not pre-registered"):
        s.run_gated_study("EQ02", "1s", PLANTED_DATASET)
    with pytest.raises(ToolRefused, match="not allowed"):
        s.run_gated_study("EQ02", "1s", "real:XNAS-2026")
    with pytest.raises(ToolRefused, match="unknown tool"):
        s.call("promote_alpha", {"alpha_id": "EQ02"})
    with pytest.raises(ToolRefused, match="bad arguments"):
        s.call("preregister", {"draft_id": "d1", "x": 1})
    with pytest.raises(ToolRefused, match="unknown alpha"):
        s.propose_hypothesis("EQ99", "1s", 1, "x")
    pre, out = _study(s)
    s.propose_hypothesis("EQ03", "1s", 1, "again")
    with pytest.raises(BudgetExceeded, match="look budget"):
        s.preregister("d2")
    with pytest.raises(ToolRefused, match="numbers not found"):
        s.file_finding("EQ02", "t = 12.5", [out["ref"]])
    with pytest.raises(ToolRefused, match="report"):
        s.file_finding("EQ02", "confirmed", [pre["board_ref"]])
    assert verdict({"gate_ic": -0.1, "t_stat": -5, "leakage_passed": True}, 1, 3.0) == "rejected"


def test_usd_cap_stops_the_session(tmp_path):
    s = _session(tmp_path, max_usd=0.2)
    steps = [[tool("list_alphas")]] * 50
    out = run_session(ScriptedClient(steps, Usage(20_000, 2_000)), s, "go")
    assert out["status"] == "budget_exhausted"
    assert out["budget"]["usd"] <= 0.2 and "usd cap" in out["budget"]["refusals"][0]


def test_tool_call_cap(tmp_path):
    s = _session(tmp_path, max_tool_calls=3)
    out = run_session(ScriptedClient([[tool("list_alphas")] * 5]), s, "go")
    assert out["status"] == "budget_exhausted" and out["budget"]["tool_calls"] == 3


def test_transcript_persisted_cached_prompt_and_no_secret(tmp_path):
    s = _session(tmp_path)
    client = ScriptedClient([[tool("list_alphas")], [tool("finish", summary="ok")]])
    out = run_session(client, s, "look around", secret=SECRET)
    assert out["status"] == "finished"
    req = client.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["text"] == SYSTEM_PROMPT
    assert {t["name"] for t in req["tools"]} == TOOL_NAMES
    files = {p.name: p.read_text(encoding="utf-8") for p in s.dir.iterdir() if p.is_file()}
    assert {"transcript.jsonl", "tool_log.jsonl", "session.json"} <= set(files)
    assert all(SECRET not in v for v in files.values())
    assert usage_cost("claude-haiku-5-5", Usage(1_000_000, 0)) == pytest.approx(0.10)


# -- AI2: evals ---------------------------------------------------------------


def test_mocked_behaviour_evals_all_pass(tmp_path):
    rows = evals.run_all(tmp_path)
    assert {r["eval"] for r in rows} == set(evals.SCENARIOS)
    assert all(r["ok"] for r in rows), rows
    assert all(r.get("missed_without_control", True) for r in rows)
    inj = next(r for r in rows if r["eval"] == "prompt_injection")
    assert inj["followed_injection"] and inj["executed_off_allowlist_tools"] == 0


def test_mcp_server_wraps_injected_board_text(tmp_path):
    ws, _ = evals._workspace(tmp_path, "m")
    ctx = {"fingerprinter": evals.fake_fingerprint}
    evals._inject_setup(ws, ctx)
    out = io.StringIO()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    req["params"] = {"name": "blackboard_state", "arguments": {}}
    mcp_server.serve(ws, io.StringIO(json.dumps(req) + "\n"), out)
    data = json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])["data"]
    hyp = data["log"][0]["body"]["hypothesis"]
    assert hyp["untrusted"] is True and hyp["flags"]
