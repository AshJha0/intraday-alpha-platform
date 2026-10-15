"""v1.17 WS-E: the research assistant (E2), the morning ops agent (E3), the
docs-sync agent (E4) and the docs checks they rest on.

Scripted client only (iap.llm.fake): no network, no key, no spend; every
spend ledger is a tmp path."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap.agents import __main__ as agents_main
from iap.agents import docs_sync, ops_agent
from iap.agents import research_assistant as ra
from iap.agents.eval_harness import run_wse_task
from iap.agents.wse_evals import WSE_TASKS, fake_dataset, fake_paper_report, fake_repo
from iap.llm.budget import Budget
from iap.llm.fake import ScriptedClient, text, tool
from iap.llm.loop import ToolRefused, ToolSpec, run_tool_loop
from iap.llm.spend import SpendLedger, SpendPolicy

SECRET = "sk-ant-test-0000-NOT-A-REAL-KEY"


def _ledger(tmp_path: Path) -> SpendLedger:
    return SpendLedger(tmp_path / "ledger.jsonl", policy=SpendPolicy(30.0, 1.0, 0.8))


# -- the loop ---------------------------------------------------------------
def test_loop_allowlist_schema_and_ledger(tmp_path):
    calls = []

    def dispatch(name, args):
        calls.append((name, dict(args)))
        if name == "boom":
            raise ToolRefused("no")
        return {"ok": 1, "summary": "s"}

    tools = [ToolSpec("ping", "p", {"x": {"type": "string"}}, ("x",)), ToolSpec("finish", "f")]
    client = ScriptedClient(
        [
            [tool("nope"), tool("ping"), tool("ping", x="1", y="2"), tool("ping", x="1")],
            [text("thinking"), tool("finish")],
        ]
    )
    ledger = _ledger(tmp_path)
    b = Budget(ledger=ledger, role="ops", session_id="s1", run_id="r1")
    res = run_tool_loop(
        client,
        system="sys",
        tools=tools,
        dispatch=dispatch,
        task="t",
        budget=b,
        model="claude-haiku-5-5",
        out_dir=tmp_path / "out",
        session_id="s1",
        secret=SECRET,
    )
    assert res.status == "finished" and res.finished == "s"
    errs = [r["error"] for r in res.tool_log if not r["ok"]]
    assert len(errs) == 3 and "unknown tool" in errs[0] and "missing ['x']" in errs[1]
    assert "extra ['y']" in errs[2]
    assert calls == [("ping", {"x": "1"}), ("finish", {})]
    rows = list(ledger.rows())
    assert len(rows) == 2 == b.model_calls and {r.role for r in rows} == {"ops"}
    assert rows[0].session == "s1" and rows[0].run == "r1" and rows[0].usd > 0
    assert b.pin is not None and b.pin.role == "ops"
    assert (tmp_path / "out" / "session.json").is_file()
    assert SECRET not in (tmp_path / "out" / "transcript.jsonl").read_text(encoding="utf-8")


# -- E2 ----------------------------------------------------------------------
@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "ws"
    (w / "research" / "experiments").mkdir(parents=True)
    (w / "configs" / "paper").mkdir(parents=True)
    (w / "configs" / "paper" / "paper_trade.json").write_text(
        json.dumps({"forbidden_datasets": ["r7_holdout", "r7_2025*", "holdout2026"]}),
        encoding="utf-8",
    )
    fake_dataset(w / "data" / "real", "ds3_20190130")
    fake_dataset(w / "data" / "real", "r7_20250115")
    return w


def _runner(alpha, horizon, ds):
    return {"experiment_id": "e1", "gate_ic": 0.0412, "t_stat": 3.71, "n_obs": 4000}


def test_holdout_patterns_and_refusal(ws):
    pats = ra.holdout_patterns(ws)
    assert ra.is_holdout("r7_holdout", pats) and ra.is_holdout("r7_20250115", pats)
    assert ra.is_holdout("holdout2026", pats) and ra.is_holdout("my_holdout_x", pats)
    assert not ra.is_holdout("ds3_20190130", pats)
    a = ra.ResearchAssistant(ws, "s1", _runner, ws / "data" / "real", holdout=pats)
    with pytest.raises(ToolRefused, match="holdout"):
        a.run_in_sample("EQ02", "1s", "r7_20250115")
    with pytest.raises(ToolRefused, match="no ingested dataset"):
        a.run_in_sample("EQ02", "1s", "nope")
    with pytest.raises(ToolRefused, match="bad dataset"):
        a.run_in_sample("EQ02", "1s", "../etc")
    rows = a.list_datasets()["datasets"]
    assert {r["name"]: r["refused_holdout"] for r in rows} == {
        "ds3_20190130": False,
        "r7_20250115": True,
    }


def test_drafts_verified_and_unsigned(ws):
    a = ra.ResearchAssistant(
        ws, "s1", _runner, ws / "data" / "real", holdout=ra.holdout_patterns(ws)
    )
    out = a.run_in_sample("EQ02", "1s", "ds3_20190130")
    assert out["ref"] == "report:s1.r1" and out["report"]["confirmatory"] is False
    assert out["report"]["dataset_version"] == "real:ds3_20190130"
    again = a.run_in_sample("EQ02", "1s", "ds3_20190130")
    assert again["note"].startswith("already run")
    # verbatim numbers pass; a derived / invented number is refused
    d = a.draft_prereg("EQ02", "1s", 1, "In-sample IC 0.0412, t 3.71.", [out["ref"]])
    doc = json.loads(Path(d["path"]).read_text(encoding="utf-8"))
    assert doc["status"] == "DRAFT" and doc["signed"] is False and doc["signature"] is None
    assert doc["registered"] is False and Path(d["path"]).parent == ws / ra.DRAFTS_RELPATH
    with pytest.raises(ToolRefused, match="not found in any cited artefact"):
        a.draft_prereg("EQ02", "1s", 1, "IC 0.05, t 3.71.", [out["ref"]])
    with pytest.raises(ToolRefused, match="draft rejected"):
        a.draft_report("EQ02 looks good", "t is 3.71 and IC 0.0412.", [])  # no citation
    r = a.draft_report(
        "EQ02 in-sample", "Gate IC 0.0412 with t 3.71 on the in-sample day.", [out["ref"]]
    )
    body = Path(r["path"]).read_text(encoding="utf-8")
    assert body.startswith("# DRAFT:") and "report:s1.r1" in body
    # a hypothesis without numbers needs no citation
    d2 = a.draft_prereg("EQ03", "5s", -1, "Expect mean reversion after a sweep.", [])
    assert d2["status"] == "DRAFT"
    assert a.unverified_numbers("t was 3.71", "two runs") == ["two"]
    assert a.unverified_numbers("t was 4.2") == ["4.2"]


def test_research_run_end_to_end_with_scripted_client(ws, tmp_path):
    def drafts(res):
        rep = next(r for r in res if isinstance(r, dict) and "report" in r)
        m = rep["report"]["metrics"]
        return [
            tool(
                "draft_prereg",
                alpha_id="EQ02",
                horizon="1s",
                expected_sign=1,
                hypothesis=f"IC {m['gate_ic']}.",
                refs=[rep["ref"]],
            )
        ]

    client = ScriptedClient(
        [
            [tool("list_alphas"), tool("list_experiments")],
            [tool("run_in_sample", alpha_id="EQ02", horizon="1s", dataset="ds3_20190130")],
            drafts,
            [
                text("Done; IC was 0.0412 which is 2 times higher than before."),
                tool("finish", summary="done"),
            ],
        ]
    )
    ledger = _ledger(tmp_path)
    summary, agent = ra.run(
        client,
        "explore",
        workspace=ws,
        runner=_runner,
        budget=Budget(ledger=ledger),
        model="claude-sonnet-5-5",
        session_id="s2",
        secret=SECRET,
    )
    assert summary["status"] == "finished" and summary["runs"] == {
        "EQ02/1s/ds3_20190130": "report:s2.r1"
    }
    assert len(summary["prereg_drafts"]) == 1 and summary["rejected_drafts"] == []
    assert summary["unverified_numbers"] == ["2"] and summary["closing_verified"] is False
    assert summary["budget"]["role"] == "research" and len(list(ledger.rows())) == 4
    assert (agent.dir / "session.json").is_file()


def test_research_cli_dry_run(ws, capsys):
    rc = agents_main.main(
        [
            "research",
            "--task",
            "look",
            "--workspace",
            str(ws),
            "--runner",
            "planted",
            "--dry-run",
            "--session-id",
            "cli1",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"status": "end_turn"' in out and "artefacts:" in out


# -- E3 ----------------------------------------------------------------------
def test_ops_rules_in_code():
    rep = {
        "date": "2026-10-10",
        "runs": {"AAPL": {"pnl_equity": -4.0, "fees": 3.0, "final_position": 100}},
        "tca": {
            "aggregate": {"is": {"gap_bps": -2.5}, "spread": {"gap_bps": 1.5}},
            "threshold": {"is_gap_alert_bps": 2.0},
        },
        "monitors": {
            "ic": {
                "EQ05": {
                    "realised_ic": None,
                    "ratio_to_holdout": None,
                    "rolling": {"ratio_to_holdout": 0.2},
                }
            }
        },
        "drill": {"status": "fail", "symbol": "AAPL"},
    }
    alerts = ops_agent.evaluate(
        rep,
        {"drift": {"alerts": ["feature drift [pooled]: 3/10 features"]}},
        None,
        {"month_to_date_usd": 1.0, "monthly_cap_usd": 30.0},
        today="2026-10-15",
    )
    rules = [a.rule for a in alerts]
    assert rules == [
        "paper.report_stale",
        "cost.is_gap",
        "cost.spread_gap",
        "cost.fees_share",
        "paper.open_position",
        "drift.feature",
        "decay.rolling_ic",
        "decay.no_ic",
        "drill.failed",
    ]
    by = {a.rule: a for a in alerts}
    assert by["cost.is_gap"].value == -2.5 and by["cost.is_gap"].threshold == 2.0
    assert by["paper.report_stale"].value == 5.0
    assert ops_agent.evaluate(None, None, None, None) == [
        ops_agent.Alert("paper.report_missing", "alert", "no complete paper-trade report found")
    ]
    assert (
        ops_agent.evaluate({"date": "x", "runs": {}, "tca": {"aggregate": {}}}, None, None, None)
        == []
    )


def test_ops_narrative_verified_or_fallback(tmp_path):
    fake_paper_report(tmp_path / "paper", is_gap=0.5, ratio=0.9)  # only fees_share fires
    client = ScriptedClient(
        [[tool("read_alerts")], [tool("write_narrative", text="Fees were 12 percent of pnl.")]]
    )
    ledger = _ledger(tmp_path)
    doc, md = ops_agent.run(
        client,
        out_root=tmp_path / "paper",
        out_dir=tmp_path / "alerts",
        budget=Budget(ledger=ledger),
        model="claude-haiku-5-5",
        session_id="o1",
    )
    assert [a["rule"] for a in doc["alerts"]] == ["cost.fees_share"]
    assert doc["narrative_by"] == "code (fallback)" and doc["unverified_numbers"] == ["12"]
    assert doc["narrative"].startswith("Paper trade 2026-10-14: 0 alert(s), 1 warning(s).")
    assert md.read_text(encoding="utf-8").startswith("# Morning ops alerts 2026-10-14")
    assert len(list(ledger.rows())) == 3 and all(r.role == "ops" for r in ledger.rows())
    # no report at all: the document says so and the markdown still renders
    doc2, md2 = ops_agent.run(
        ScriptedClient([]),
        out_root=tmp_path / "none",
        out_dir=tmp_path / "alerts2",
        budget=Budget(),
        model="claude-haiku-5-5",
        today="2026-10-15",
    )
    assert doc2["alerts"][0]["rule"] == "paper.report_missing" and md2.is_file()


def test_ops_cli_dry_run(tmp_path, capsys):
    fake_paper_report(tmp_path / "paper")
    rc = agents_main.main(
        [
            "ops",
            "--paper-out-root",
            str(tmp_path / "paper"),
            "--out",
            str(tmp_path / "out"),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == ops_agent.EXIT_ALERTS
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "iap.agents.ops_alerts/1" and doc["narrative_by"] == "code (fallback)"


# -- E4 ----------------------------------------------------------------------
def test_docs_sync_gather_and_patch(tmp_path):
    repo = fake_repo(tmp_path)
    inputs = docs_sync.gather(repo, "v0.1.0", "HEAD")
    assert inputs["code_files"] == ["python/src/iap/demo.py"] and inputs["modules"] == ["iap.demo"]
    assert "--new-flag" in inputs["diff"] and inputs["docs"][0] in ("README.md", "docs/GUIDE.md")
    ds = docs_sync.DocsSync(repo, inputs, {"rows": []})
    with pytest.raises(ToolRefused, match="not a tracked documentation"):
        ds.propose_edit("python/src/iap/demo.py", "a", "b", "r")
    with pytest.raises(ToolRefused, match="not a tracked documentation"):
        ds.read_doc("../outside.md")
    with pytest.raises(ToolRefused, match="occurs 0 times"):
        ds.propose_edit("README.md", "missing text", "x", "r")
    with pytest.raises(ToolRefused, match="identical"):
        ds.propose_edit("README.md", "# Demo", "# Demo", "r")
    assert ds.propose_edit("README.md", "--old-flag", "--new-flag", "renamed")["accepted"]
    with pytest.raises(ToolRefused, match="overlaps"):
        ds.propose_edit("README.md", "iap.demo --old-flag", "iap.demo --new-flag", "again")
    patch = ds.patch()
    assert patch.startswith("--- a/README.md") and "+Run `python -m iap.demo --new-flag`" in patch
    assert (repo / "README.md").read_text(encoding="utf-8").count("--old-flag") == 1  # not applied


def test_docs_sync_cli_dry_run(tmp_path, capsys):
    repo = fake_repo(tmp_path)
    rc = agents_main.main(
        [
            "docs-sync",
            "--repo",
            str(repo),
            "--base",
            "v0.1.0",
            "--out",
            str(tmp_path / "o"),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert '"applied": false' in out and "patch (NOT applied): none" in out


def test_check_docs_json_and_api_names(tmp_path):
    script = REPO_ROOT / "tests" / "harness" / "check_docs.py"
    sys.path.insert(0, str(script.parent))
    try:
        import check_docs
    finally:
        sys.path.pop(0)
    assert check_docs._resolves("iap.llm.verify.verify_finding")
    assert check_docs._resolves("iap.agents.ops_agent.evaluate")
    assert not check_docs._resolves("iap.llm.verify.no_such_name")
    assert not check_docs._resolves("iap.nope.thing")
    assert check_docs.STALE_PHRASES.search(
        "this is in progress"
    ) and not check_docs.STALE_PHRASES.search("progress bar")
    assert check_docs.DATED.search("in progress as of 2026-10-15")
    f = check_docs.Findings()
    check_docs.check_api_names(f)
    assert f.failed("api_names") == [], f.failed("api_names")
    check_docs.check_diagram_index(f)
    assert f.failed("diagram_index") == []


# -- the evals -----------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(WSE_TASKS))
def test_wse_eval_scores_one(name, tmp_path):
    row = run_wse_task(name, tmp_path)
    assert row["score"] == 1.0, row["checks"]


def test_wse_evals_in_committed_baseline():
    base = json.loads(
        (REPO_ROOT / "research" / "agents" / "eval_baseline.json").read_text(encoding="utf-8")
    )
    for name in WSE_TASKS:
        assert base["tasks"][name]["score"] == 1.0


def test_agents_main_help_lists_commands():
    p = subprocess.run(
        [sys.executable, "-m", "iap.agents", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "python",
        env={**dict(__import__("os").environ), "PYTHONPATH": "src"},
    )
    assert p.returncode == 0 and "research" in p.stdout and "docs-sync" in p.stdout
