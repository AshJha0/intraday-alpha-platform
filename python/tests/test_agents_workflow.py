"""v1.15 A-P0-1 / A-P1-2 / A-P1-3: narrowed agent job, proposal looks, eval harness."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from iap.agents import eval_harness, signing
from iap.agents.broker import BrokerError, WriteBroker
from iap.agents.prereg_gate import PreregistrationError, require
from iap.agents.workflow import (
    HUMAN_ONLY_OPS,
    OUTCOME_WRITER,
    PROPOSAL_KIND,
    SIGNOFF_WRITER,
    AgentAuthorityError,
    Workflow,
    WorkflowError,
    hit_rate,
    issue_signoff,
    require_human,
)
from iap.llm import evals
from iap.llm.agent import TOOL_NAMES, ToolRefused, open_session
from iap.llm.budget import Budget
from iap.llm.runners import PLANTED_DATASET, planted_runner
from iap.validation.ledger import ExperimentLedger

SRC = Path(__file__).resolve().parents[1] / "src" / "iap"
REPO = Path(__file__).resolve().parents[2]
AGENT = "researcher"
HUMAN = "owner"
SECRET = "ab" * 32


def _fp(alpha_id):
    return {"code_hash": "c" * 64, "feature_hash": "f" * 64}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "research" / "agents").mkdir(parents=True)
    _, pub = signing.generate_keypair()
    (tmp_path / "research" / "agents" / "agent_pubkeys.json").write_text(
        json.dumps({AGENT: pub}), encoding="ascii"
    )
    return tmp_path


def _wf(ws):
    broker = WriteBroker(ws, {AGENT}, trusted={SIGNOFF_WRITER, OUTCOME_WRITER}, fingerprinter=_fp)
    return Workflow(broker)


def _proposed(ws):
    wf = _wf(ws)
    prop = wf.propose(AGENT, "EQ02", "1s", 1, "ofi pushes the mid")
    draft = wf.draft_prereg(AGENT, prop["proposal_id"], "EQ02 predicts 1s returns, +")
    return wf, prop, draft


# -- A-P1-2: each proposal debits the chained ledger -------------------------
def test_proposal_debits_one_chained_look(ws):
    wf, prop, _ = _proposed(ws)
    led = ExperimentLedger(ws / "research" / "experiments.json")
    assert led.total_experiments == prop["ledger_total"] == 1
    assert [e["kind"] for e in led.entries] == [PROPOSAL_KIND]
    assert led.chain_head == prop["ledger_head"]
    wf.propose(AGENT, "EQ03", "5s", -1, "another look")
    assert ExperimentLedger(ws / "research" / "experiments.json").total_experiments == 2
    with pytest.raises(WorkflowError, match="already made"):
        wf.propose(AGENT, "EQ02", "1s", 1, "same again")


def test_draft_is_not_a_preregistration(ws):
    wf, _, draft = _proposed(ws)
    assert draft["status"] == "awaiting_human_signoff"
    with pytest.raises(PreregistrationError, match="not pre-registered"):
        require(ws, "EQ02", "1s", fingerprinter=_fp)
    with pytest.raises(WorkflowError, match="no proposal"):
        wf.draft_prereg(AGENT, "0" * 16, "x")


# -- A-P0-1: human sign-off; agents never sign, run confirmatory or promote --
def test_human_signoff_makes_the_gate_pass_without_a_second_look(ws):
    wf, prop, draft = _proposed(ws)
    pid = wf.sign_off(issue_signoff(HUMAN, SECRET, draft["draft_hash"], "ok"), {HUMAN: SECRET})
    entry = require(ws, "EQ02", "1s", fingerprinter=_fp)
    assert entry["agent"] == SIGNOFF_WRITER and entry["body"]["prereg_id"] == pid
    assert entry["body"]["proposed_by"] == AGENT and entry["body"]["approver"] == HUMAN
    assert ExperimentLedger(ws / "research" / "experiments.json").total_experiments == 1
    so = issue_signoff(HUMAN, SECRET, draft["draft_hash"], "again")
    with pytest.raises(WorkflowError, match="already pre-registered"):
        wf.sign_off(so, {HUMAN: SECRET})


def test_signoff_refuses_agents_forgeries_and_replays(ws):
    wf, _, draft = _proposed(ws)
    with pytest.raises(AgentAuthorityError):
        wf.sign_off(issue_signoff(AGENT, SECRET, draft["draft_hash"], "me"), {AGENT: SECRET})
    forged = {**issue_signoff(HUMAN, SECRET, draft["draft_hash"], "ok"), "mac": "0" * 64}
    with pytest.raises(WorkflowError, match="signature"):
        wf.sign_off(forged, {HUMAN: SECRET})
    with pytest.raises(WorkflowError, match="no draft"):
        wf.sign_off(issue_signoff(HUMAN, SECRET, "f" * 64, "ok"), {HUMAN: SECRET})


def test_gate_refuses_a_prereg_an_agent_wrote_itself(ws):
    broker = WriteBroker(ws, {AGENT}, fingerprinter=_fp)
    broker.preregister(AGENT, "EQ02", "1s", "bypass the workflow", 1)
    with pytest.raises(PreregistrationError, match="needs human sign-off"):
        require(ws, "EQ02", "1s", fingerprinter=_fp)


def test_outcome_and_human_only_ops_refuse_agents(ws):
    wf, _, draft = _proposed(ws)
    pid = wf.sign_off(issue_signoff(HUMAN, SECRET, draft["draft_hash"], "ok"), {HUMAN: SECRET})
    with pytest.raises(AgentAuthorityError):
        wf.record_outcome(AGENT, pid, "confirmed", "report:x")
    for op in HUMAN_ONLY_OPS:
        with pytest.raises(AgentAuthorityError):
            require_human(AGENT, op, {AGENT})
        require_human(HUMAN, op, {AGENT})
    with pytest.raises(BrokerError):
        wf.broker.write_trusted(SIGNOFF_WRITER, "outcome", {})
    wf.record_outcome(HUMAN, pid, "confirmed", "report:x")


def test_hit_rate_per_look(ws):
    wf, _, draft = _proposed(ws)
    wf.propose(AGENT, "EQ03", "5s", 1, "second look")
    pid = wf.sign_off(issue_signoff(HUMAN, SECRET, draft["draft_hash"], "ok"), {HUMAN: SECRET})
    wf.record_outcome(HUMAN, pid, "confirmed", "report:x")
    r = hit_rate(wf.broker.board.entries())
    assert (r["looks"], r["signed_off"], r["confirmed"], r["hit_rate"]) == (2, 1, 1, 0.5)
    assert [x["hit_rate"] for x in r["per_look"]] == [1.0, 0.5]
    t = [x["bonferroni_t"] for x in r["per_look"]]
    assert t[1] > t[0]  # each look raises the bar


def test_agent_session_has_no_confirmatory_or_promotion_tool(tmp_path):
    assert not {n for n in TOOL_NAMES if any(w in n for w in ("promot", "sign", "confirm"))}
    ws_, priv = evals._workspace(tmp_path, "ws")
    with pytest.raises(ValueError, match="exploratory"):
        open_session(
            ws_, evals.AGENT, priv, planted_runner(), Budget(), datasets=frozenset({"real:X"})
        )
    s = open_session(
        ws_, evals.AGENT, priv, planted_runner(), Budget(), fingerprinter=evals.fake_fingerprint
    )
    s.propose_hypothesis("EQ02", "1s", 1, "ofi")
    s.preregister("d1")
    rep = s.run_gated_study("EQ02", "1s", PLANTED_DATASET)["report"]
    assert rep["confirmatory"] is False
    with pytest.raises(PreregistrationError):
        require(ws_, "EQ02", "1s", fingerprinter=evals.fake_fingerprint)
    with pytest.raises(ToolRefused):
        s.call("promote_alpha", {"alpha_id": "EQ02"})


def test_agent_code_cannot_reach_lifecycle_or_registry():
    """Only the human approval CLI may import the lifecycle / registry."""
    allowed = {SRC / "agents" / "cli.py"}
    for pkg in ("agents", "llm"):
        for py in sorted((SRC / pkg).glob("*.py")):
            if py in allowed:
                continue
            for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                bad = [n for n in names if n.startswith(("iap.lifecycle", "iap.mlops"))]
                assert not bad, f"{py.name} imports {bad}"


# -- A-P1-3: eval harness ----------------------------------------------------
def test_eval_harness_is_deterministic_and_meets_baseline(tmp_path):
    a = eval_harness.run_all(tmp_path / "a")
    b = eval_harness.run_all(tmp_path / "b")
    assert a == b
    baseline = json.loads((REPO / eval_harness.BASELINE_RELPATH).read_text(encoding="utf-8"))
    from iap.agents.wse_evals import WSE_TASKS

    assert set(baseline["tasks"]) == set(eval_harness.TASKS) | set(WSE_TASKS)
    assert eval_harness.regressions(a, baseline) == []


def test_eval_harness_flags_regressions():
    base = {"tasks": {"t": {"score": 1.0, "checks": {"x": True, "y": True}}}}
    cur = {"tasks": {"t": {"score": 0.5, "checks": {"x": True, "y": False}}}}
    assert len(eval_harness.regressions(cur, base)) == 2
    assert eval_harness.regressions({"tasks": {}}, base) == ["t: missing"]
