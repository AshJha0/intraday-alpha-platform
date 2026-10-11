"""v1.13 governance: CI checks (G-P1-1, G-P1-2), approval-gated lifecycle CLI
(G-P1-3), locked blackboard append (G-P2-1), stricter numbers rule (G-P2-2)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest
from conftest import REPO_ROOT
from iap.agents.blackboard import Blackboard, BlackboardError
from iap.llm import verify


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


keys_check = _load("check_agent_keys", "tests/harness/check_agent_keys.py")
monotone = _load("check_ledger_monotone", "tests/harness/check_ledger_monotone.py")
merge_ledger = _load("merge_ledger", "tools/merge_ledger.py")

PUB = "ab" * 32


# -- G-P1-2: registry covers every signed writer ------------------------------


def test_empty_registry_ok_only_without_signed_entries():
    unsigned = [{"seq": 1, "agent": "operator"}]
    assert keys_check.check(unsigned, {}) == []
    signed = [*unsigned, {"seq": 2, "agent": "llm", "auth": {"scheme": "ed25519"}}]
    assert any("is empty" in p for p in keys_check.check(signed, {}))
    assert any("'llm'" in p for p in keys_check.check(signed, {"other": PUB}))
    assert keys_check.check(signed, {"llm": PUB}) == []
    assert any("hex" in p for p in keys_check.check(unsigned, {"llm": "zz"}))
    assert keys_check.check(unsigned, []) != []


def test_committed_registry_passes():
    assert keys_check.main(["--root", str(REPO_ROOT)]) == 0


# -- G-P1-1: monotone ledger / append-only board ------------------------------


def _ledger(entries):
    from iap.validation.ledger import chain_head

    return {
        "total_experiments": sum(e["count"] for e in entries),
        "entries": entries,
        "chain_head": chain_head(entries),  # v1.14 (G-P1-5)
        "chain_length": len(entries),
    }


def test_ledger_monotone_rules():
    a = {"key": "a", "count": 2}
    b = {"key": "b", "count": 1}
    assert monotone.compare_ledger(_ledger([a]), _ledger([a, b])) == []
    shrunk = monotone.compare_ledger(_ledger([a, b]), _ledger([a]))
    assert any("total_experiments" in p for p in shrunk)
    assert any("missing" in p for p in shrunk)
    # same total, but an entry replaced: identity lost
    swapped = monotone.compare_ledger(_ledger([a]), _ledger([{"key": "c", "count": 2}]))
    assert any("missing" in p for p in swapped) and any("descend" in p for p in swapped)
    assert monotone.compare_models_ledger({"experiment_count": 5}, {"experiment_count": 4})


def test_board_append_only_prefix():
    base = b'{"seq":1}\n{"seq":2}\n'
    assert monotone.compare_board(base, base + b'{"seq":3}\n') == []
    assert monotone.compare_board(base, base.replace(b"\n", b"\r\n")) == []
    assert any("entries" in p for p in monotone.compare_board(base, b'{"seq":1}\n'))
    edited = monotone.compare_board(base, b'{"seq":1}\n{"seq":9}\n{"seq":3}\n')
    assert any("prefix" in p for p in edited)


def test_monotone_against_git_base(tmp_path):
    repo = tmp_path / "r"
    (repo / "research" / "agents").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    led = repo / "research" / "experiments.json"
    board = repo / "research" / "agents" / "blackboard.jsonl"
    led.write_text(json.dumps(_ledger([{"key": "a", "count": 3}])))
    board.write_bytes(b'{"seq":1}\n')
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    assert monotone.main(["--root", str(repo), "--base", "main"]) == 0
    board.write_bytes(b'{"seq":1}\n{"seq":2}\n')
    led.write_text(json.dumps(_ledger([{"key": "a", "count": 3}, {"key": "b", "count": 1}])))
    assert monotone.main(["--root", str(repo), "--base", "main"]) == 0
    board.write_bytes(b'{"seq":2}\n')
    assert monotone.main(["--root", str(repo), "--base", "main"]) == 1
    assert monotone.main(["--root", str(repo), "--base", "nope"]) == 2


def test_merge_ledger_by_identity(tmp_path):
    from iap.validation.ledger import ExperimentLedger

    ours = ExperimentLedger(tmp_path / "ours.json")
    ours.record("EQ01", "prereg", {"h": "1s"}, {"t": 1.0})
    ours.record("EQ02", "scan", {"h": "1s"}, count=4)
    ours.save()
    theirs = ExperimentLedger(tmp_path / "theirs.json")
    theirs.record("EQ01", "prereg", {"h": "1s"}, {"t": 2.0})  # same identity, other result
    theirs.record("EQ03", "prereg", {"h": "5s"}, count=2)  # new on the branch
    theirs.save()
    out = tmp_path / "merged.json"
    assert (
        merge_ledger.main(
            ["--ours", str(ours.path), "--theirs", str(theirs.path), "--out", str(out)]
        )
        == 0
    )
    merged = json.loads(out.read_text())
    assert merged["total_experiments"] == 5 + 2
    assert [e["alpha_id"] for e in merged["entries"]] == ["EQ01", "EQ02", "EQ03"]
    assert merged["entries"][-1]["n"] == 7
    _, report = merge_ledger.merge(ours.path, theirs.path)
    assert report["appended_looks"] == 2 and len(report["result_conflicts"]) == 1
    # merge is idempotent, and merging a ledger with itself changes nothing
    led, rep = merge_ledger.merge(out, out)
    assert rep["appended_entries"] == 0 and led._render() == out.read_text()
    # the merged ledger loses nothing from either input; it DESCENDS from
    # ours (main, the CI base) only - v1.14 G-P1-5: theirs' entries are
    # appended after main's, so the branch chain is not a prefix
    assert monotone.compare_ledger(json.loads(ours.path.read_text()), merged) == []
    vs_theirs = monotone.compare_ledger(json.loads(theirs.path.read_text()), merged)
    assert vs_theirs and all("does not descend" in p for p in vs_theirs)


# -- G-P1-3: lifecycle CLI needs an approval -----------------------------------


def _lifecycle_repo(tmp_path):
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "configs" / "strategies", repo / "configs" / "strategies")
    (repo / "research").mkdir()
    for n in ("alpha_registry.json", "lifecycle_transitions.jsonl"):
        shutil.copy(REPO_ROOT / "research" / n, repo / "research" / n)
    return repo


def test_lifecycle_retire_requires_approval(tmp_path, capsys):
    from iap.agents import cli
    from iap.lifecycle.__main__ import cmd_manual
    from iap.lifecycle.__main__ import main as lifecycle_main

    repo = _lifecycle_repo(tmp_path)
    log = repo / "research" / "lifecycle_transitions.jsonl"
    before = log.read_bytes()
    base = ["--root", str(repo)]
    assert lifecycle_main([*base, "retire", "EQ01", "--reason", "decayed"]) == 2
    assert "approval" in capsys.readouterr().err and log.read_bytes() == before

    keyfile = tmp_path / "keys" / "k.json"
    assert cli.main([*base, "keygen", "--approver", "carol", "--keyfile", str(keyfile)]) == 0
    appr = tmp_path / "a.json"
    issue = ["issue", "--approver", "carol", "--keyfile", str(keyfile), "--alpha", "EQ01"]
    assert (
        cli.main([*base, *issue, "--action", "retire", "--reason", "decayed", "--out", str(appr)])
        == 0
    )
    with_appr = ["--approval", str(appr), "--keyfile", str(keyfile)]
    # mismatched alpha / action / reason are refused before anything is written
    assert lifecycle_main([*base, "retire", "EQ02", "--reason", "decayed", *with_appr]) == 2
    assert lifecycle_main([*base, "reset", "EQ01", "--reason", "decayed", *with_appr]) == 2
    assert lifecycle_main([*base, "retire", "EQ01", "--reason", "other", *with_appr]) == 2
    assert log.read_bytes() == before
    # a forged approval is refused by the signature check
    forged = tmp_path / "f.json"
    doc = json.loads(appr.read_text())
    forged.write_text(json.dumps({**doc, "mac": "00" * 32}))
    bad = ["--approval", str(forged), "--keyfile", str(keyfile)]
    assert lifecycle_main([*base, "retire", "EQ01", "--reason", "decayed", *bad]) == 1
    # the real one works once, then its nonce is burned
    assert lifecycle_main([*base, "retire", "EQ01", "--reason", "decayed", *with_appr]) == 0
    assert '"to_state":"RETIRED"' in log.read_text()
    assert lifecycle_main([*base, "retire", "EQ01", "--reason", "decayed", *with_appr]) == 1
    board = Blackboard(repo / "research" / "agents" / "blackboard.jsonl")
    assert [e["kind"] for e in board.entries()] == ["approval"]
    # library callers holding their own proof can opt out explicitly
    assert cmd_manual(repo, "EQ02", "test", None, "retire", require_approval=False) == 0


# -- G-P2-1: locked append + monotone ts ---------------------------------------

_WRITER = """
import sys
sys.path.insert(0, {src!r})
from iap.agents.blackboard import Blackboard
b = Blackboard({path!r})
for i in range({n}):
    b.append("finding", "w{w}", 1, {{"i": i}})
"""


def test_concurrent_appends_keep_the_chain(tmp_path):
    path = tmp_path / "board.jsonl"
    src = str(REPO_ROOT / "python" / "src")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER.format(src=src, path=str(path), n=15, w=w)],
            env={**os.environ, "PYTHONPATH": src},
        )
        for w in range(4)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    board = Blackboard(path)
    assert board.verify() == 60
    assert sorted(e["seq"] for e in board.entries()) == list(range(1, 61))
    assert not (tmp_path / "board.jsonl.lock").exists()


def test_append_refuses_earlier_ts(tmp_path):
    b = Blackboard(tmp_path / "b.jsonl")
    b.append("task", "a", 100, {})
    b.append("task", "a", 100, {"x": 1})  # equal is fine
    with pytest.raises(BlackboardError, match="non-decreasing"):
        b.append("task", "a", 99, {})
    assert b.verify() == 2


# -- G-P2-2: numbers rule -----------------------------------------------------


def test_integers_must_match_exactly():
    assert not verify.matches("3", False, [2.6])
    assert not verify.matches("3", False, [3.4])
    assert verify.matches("3", False, [3])
    assert verify.matches("3", False, [3.0])
    assert not verify.matches("12", True, [0.1234])
    assert verify.matches("12.34", True, [0.1234])


def test_exact_decimal_rounding():
    assert verify.matches("0.412", False, [0.41187])
    assert not verify.matches("0.42", False, [0.41187])
    assert verify.matches("0.125", False, [0.1245])  # half-up
    assert verify.matches("0.12", False, [0.125])  # half-even
    assert verify.matches("0.13", False, [0.125])  # half-up
    assert not verify.matches("0.1", False, [0.1500001])
    assert verify.matches("-0.04", False, [-0.0412])
    assert verify.matches("1.2e-05", False, [1.23e-05])


def test_metadata_is_not_evidence():
    report = {"seq": 3, "ts": 2, "expected_sign": 1, "metrics": {"gate_ic": 0.0412, "t_stat": 5.3}}
    keys = {k for k, _ in verify.evidence(report)}
    vals = [v for _, v in verify.evidence(report)]
    assert keys == {"gate_ic", "t_stat"} and 3 not in vals and 1 not in vals


def test_number_words_and_labels():
    assert verify.number_words("IC tripled; three of five horizons") == ["five", "three", "tripled"]
    assert verify.number_words("EQ02 confirmed") == []
    labelled = verify._numbers_with_labels("Gate IC 0.04 with t = 5.3, Sharpe 1.2, 7 obs")
    assert [(t, f) for t, _, f in labelled] == [
        ("0.04", "ic"),
        ("5.3", "t"),
        ("1.2", "sharpe"),
        ("7", None),
    ]
    assert verify.field_in_family("t_stat", "t") and not verify.field_in_family("t_threshold", "t")
    assert verify.field_in_family("gate_ic", "ic") and not verify.field_in_family("n_obs", "ic")


def _report(tmp_path, sid="s1", metrics=None):
    path = tmp_path / "research" / "agents" / "llm_sessions" / sid / "reports" / "r1.json"
    path.parent.mkdir(parents=True)
    doc = {
        "schema": "iap.llm.report/1",
        "session": sid,
        "run_id": "r1",
        "expected_sign": 1,
        "t_threshold": 3.0,
        "metrics": metrics or {"n_obs": 4000, "gate_ic": 0.0412, "t_stat": 2.6031},
    }
    path.write_text(json.dumps(doc))
    return f"report:{sid}.r1"


@pytest.mark.parametrize(
    "text",
    [
        "IC 0.0412 at seq 1",  # metadata integer (expected_sign / run seq)
        "t = 3 after rounding",  # +/-0.5 on an integer: 2.6031 is not 3
        "IC 2.6031",  # right number, wrong statistic (it is the t-stat)
        "t 0.0412",  # the IC labelled as t
        "the IC doubled",  # a number word
        "three horizons confirm",
        "IC 0.0412 versus 0.0400 before",  # derived / invented comparison number
    ],
)
def test_bypass_attempts_are_rejected(tmp_path, text):
    ref = _report(tmp_path)
    assert verify.verify_finding("EQ02", text, [ref], tmp_path, "s1")


def test_honest_finding_passes(tmp_path):
    ref = _report(tmp_path)
    text = "Gate IC 0.0412 with t 2.6031 (threshold 3.0) on 4000 observations; t 2.60."
    assert verify.verify_finding("EQ02 at 1s: weaker", text, [ref], tmp_path, "s1") == []


# -- extras: MCP read cap + streamed JSONL tail ---------------------------------


def test_mcp_read_cap_and_tail(tmp_path, monkeypatch):
    from iap.agents import mcp_server

    research = tmp_path / "research"
    (research / "alpha_reports").mkdir(parents=True)
    rows = [json.dumps({"alpha_id": f"A{i}"}) for i in range(1, 31)]
    (research / "lifecycle_transitions.jsonl").write_text("\n".join(rows) + "\n\n")
    tools = mcp_server.Tools(tmp_path)
    out = tools.lifecycle_log(tail=5)["data"]
    assert out["total"] == 30 and [r["line"] for r in out["rows"]] == [26, 27, 28, 29, 30]
    (research / "alpha_reports" / "EQ01.json").write_text(json.dumps({"x": "y" * 64}))
    monkeypatch.setattr(mcp_server, "MAX_READ_BYTES", 32)
    with pytest.raises(mcp_server.ToolError, match="too large"):
        tools.alpha_report("EQ01")
