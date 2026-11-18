"""The research flow wired to memory, the holdout registry and the number verifier."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from iap.graph import wiring
from iap.graph.__main__ import main as graph_main
from iap.graph.checkpoint import resume, start
from iap.graph.research_flow import fallback_narrative
from iap.memory.search import Memory

REPO = Path(__file__).resolve().parents[2]
KEY_A = "a" * 64
KEY_B = "b" * 64

# A registry in the shape of research/holdout_registry.json (passed as a mapping,
# so the seal is not checked; the real file is used in its own test below).
REGISTRY = {
    "datasets": {
        "v" * 64: {"label": "fix_20190130", "sessions": ["2019-01-30"], "state": "in_sample"},
        "w" * 64: {"label": "fix_20251212", "sessions": ["2025-12-12"], "state": "spent"},
    },
    "sessions": {
        "2019-01-30": [{"state": "in_sample", "symbols": "*"}],
        "2025-12-12": [{"state": "spent", "symbols": "*"}],
    },
}

HYP = {
    "alpha_id": "OFI7",
    "horizon": "5m",
    "universe": {"name": "three", "symbols": ["AAPL", "MSFT", "QQQ"], "datasets": ["fix_20190130"]},
    "expected_sign": 1,
    "mechanism": "Order-flow imbalance at the touch precedes five-minute drift.",
}


def _corpus(root: Path) -> Path:
    (root / "research").mkdir(parents=True)
    entries = [
        {
            "key": KEY_A,
            "alpha_id": "OFI7",
            "kind": "screen",
            "config": {"horizon": "5m", "symbols": ["QQQ", "MSFT", "AAPL"]},
            "result": {"t_stat": 1.1},
        },
        {
            "key": KEY_B,
            "alpha_id": "OFI7",
            "kind": "screen",
            "config": {"horizon": "1m", "symbols": ["AAPL", "MSFT", "QQQ"]},
            "result": {"t_stat": 0.4},
        },
    ]
    (root / "research" / "experiments.json").write_text(
        json.dumps({"entries": entries}, indent=1), encoding="utf-8"
    )
    (root / "research" / "notes.md").write_text(
        "# OFI7 notes\n\nOrder-flow imbalance OFI7 was screened at 5m.\n", encoding="utf-8"
    )
    return root


def _graph(root: Path, **kw):
    return wiring.build_wired_flow(root=root, registry=REGISTRY, **kw)


# ---------------------------------------------------------------- same test
@pytest.mark.parametrize(
    "entry, same, reason",
    [
        (
            {"alpha_id": "ofi7", "config": {"horizon": "5m", "symbols": ["MSFT", "QQQ", "AAPL"]}},
            True,
            "symbol set",
        ),
        (
            {"alpha_id": "OFI7", "config": {"horizon": "5m", "universe": "THREE"}},
            True,
            "universe name",
        ),
        (
            {
                "alpha_id": "OFI7",
                "config": {"candidate": {"horizon": "5m", "symbols": ["AAPL", "MSFT", "QQQ"]}},
            },
            True,
            "symbol set",
        ),
        (
            {
                "alpha_id": "OFI7",
                "config": {"horizon": "5m:insample", "symbols": ["AAPL", "MSFT", "QQQ"]},
            },
            False,
            "horizon",
        ),
        (
            {"alpha_id": "OFI7", "config": {"horizon": "5m", "symbols": ["AAPL"]}},
            False,
            "universe differs",
        ),
        ({"alpha_id": "OFI7", "config": {"horizon": "5m"}}, False, "no universe"),
        (
            {"alpha_id": "OFI8", "config": {"horizon": "5m", "symbols": ["AAPL", "MSFT", "QQQ"]}},
            False,
            "alpha_id",
        ),
    ],
)
def test_same_test_rule(entry, same, reason):
    got, why = wiring.same_test(entry, HYP)
    assert got is same and reason in why


def test_duplicate_hypothesis_stops_at_prior_art(tmp_path):
    root = _corpus(tmp_path)
    res = start(_graph(root), {"hypothesis": HYP}, tmp_path / "run", clock=time.perf_counter)
    st = res.state
    assert st.outcome == "duplicate" and res.node == "duplicate_stop"
    assert st.duplicate_of == f"research/experiments.json#{KEY_A}"
    by_id = {h["id"]: h for h in st.prior_hits}
    assert by_id[f"research/experiments.json#{KEY_B}"]["same_test"] is False  # other horizon
    assert any(h["kind"] == "research" and h["same_test"] is False for h in st.prior_hits)


def test_text_similarity_alone_is_not_a_duplicate(tmp_path):
    root = _corpus(tmp_path)
    hyp = dict(HYP, horizon="15m")  # same words, no ledger entry at 15m
    res = start(_graph(root), {"hypothesis": hyp}, tmp_path / "run", clock=time.perf_counter)
    assert res.state.duplicate_of is None and res.interrupt is not None
    assert res.state.draft["prior_art"]  # hits are still listed for the person


# ------------------------------------------------------------------ holdout
def test_unknown_dataset_is_refused(tmp_path):
    root = _corpus(tmp_path)
    graph = _graph(root)
    hyp = dict(
        HYP, horizon="15m", universe=dict(HYP["universe"], datasets=["fix_20190130", "never_seen"])
    )
    run = tmp_path / "run"
    start(graph, {"hypothesis": hyp}, run, clock=time.perf_counter)
    res = resume(run, graph, decision={"approved": True, "by": "t"}, clock=time.perf_counter)
    assert res.state.outcome == "refused_holdout" and res.state.refused == ["never_seen"]
    assert res.state.run is None


def test_registry_predicate_on_the_committed_registry():
    is_holdout = wiring.registry_is_holdout(REPO)
    assert is_holdout("ds3_20190130") is False  # in_sample
    assert is_holdout("r7_20251212") is True  # spent
    assert is_holdout("no_such_dataset") is True  # unknown: refused, not allowed
    assert is_holdout("") is True


def test_registry_predicate_fails_closed_without_registry(tmp_path):
    with pytest.raises(ValueError):
        wiring.registry_is_holdout(tmp_path)


# ---------------------------------------------------------------- narrative
FACTS = {
    "alpha_id": "OFI7",
    "horizon": "15m",
    "verdict": "pass",
    "t_stat": 2.4,
    "n_sessions": 20,
    "expected_sign": 1,
    "min_abs_t": 2.0,
    "min_sessions": 10,
}


@pytest.mark.parametrize(
    "text, ok",
    [
        ("Verdict pass: t = 2.4 over 20 sessions against |t| >= 2.0.", True),
        ("Verdict pass: t = 2.40 over 20 sessions.", True),
        ("Verdict pass: t = 2.5 over 20 sessions.", False),  # wrong number
        ("Verdict pass: t = 20 over 20 sessions.", False),  # right number, wrong statistic
        ("Verdict pass over twenty sessions.", False),  # number in words
        ("Verdict pass: t = 2.4 with mean 0.8 bps.", False),  # derived / absent number
        ("t = 2.4 over 20 sessions.", False),  # verdict not stated
    ],
)
def test_narrative_verifier(text, ok):
    assert wiring.verify_narrative(text, FACTS) is ok, wiring.narrative_problems(text, FACTS)


def test_wrong_number_falls_back_to_code_text(tmp_path):
    root = _corpus(tmp_path)
    hyp = dict(HYP, horizon="15m")

    def bad_writer(facts):
        return f"Verdict {facts['verdict']}: t = 3.1 over {facts['n_sessions']} sessions."

    graph = _graph(root, write=bad_writer)
    run = tmp_path / "run"
    start(graph, {"hypothesis": hyp}, run, clock=time.perf_counter)
    res = resume(run, graph, decision={"approved": True, "by": "t"}, clock=time.perf_counter)
    st = res.state
    assert st.outcome == "done" and st.narrative_source == "fallback"
    facts = {"alpha_id": "OFI7", "horizon": "15m", **st.verdict}
    assert st.narrative == fallback_narrative(facts)


def test_template_writer_verifies(tmp_path):
    root = _corpus(tmp_path)
    graph = _graph(root)
    run = tmp_path / "run"
    start(graph, {"hypothesis": dict(HYP, horizon="15m")}, run, clock=time.perf_counter)
    res = resume(run, graph, decision={"approved": True, "by": "t"}, clock=time.perf_counter)
    assert res.state.narrative_source == "model" and res.state.run == wiring.STUB_RUN


def test_memory_is_reused_when_given(tmp_path):
    root = _corpus(tmp_path)
    mem = Memory(root)
    hits = wiring.memory_search_prior(mem)(HYP)
    assert [h["id"] for h in hits if h["same_test"]] == [f"research/experiments.json#{KEY_A}"]


# ---------------------------------------------------------------------- CLI
def test_cli_run_stops_for_approval_then_resumes(tmp_path, capsys, monkeypatch):
    root = _corpus(tmp_path / "repo")
    # the CLI reads the committed registry under --root; give the fixture one
    monkeypatch.setattr(
        wiring.holdout_registry, "load_registry", lambda path=None, root=None: REGISTRY
    )
    hyp_file = tmp_path / "h.json"
    hyp_file.write_text(json.dumps(dict(HYP, horizon="15m")), encoding="utf-8")
    out = tmp_path / "run"
    assert (
        graph_main(["run", "--hypothesis", str(hyp_file), "--out", str(out), "--root", str(root)])
        == 0
    )
    text = capsys.readouterr().out
    assert "awaiting: human approval" in text and '"signed": false' in text
    assert json.loads((out / "flow.json").read_text(encoding="utf-8"))["flow"] == "wired"
    assert graph_main(["resume", str(out), "--approve"]) == 0
    assert "outcome: done" in capsys.readouterr().out


def test_cli_run_reports_duplicate(tmp_path, capsys, monkeypatch):
    root = _corpus(tmp_path / "repo")
    monkeypatch.setattr(
        wiring.holdout_registry, "load_registry", lambda path=None, root=None: REGISTRY
    )
    hyp_file = tmp_path / "h.json"
    hyp_file.write_text(json.dumps(HYP), encoding="utf-8")
    out = tmp_path / "run"
    assert (
        graph_main(["run", "--hypothesis", str(hyp_file), "--out", str(out), "--root", str(root)])
        == 0
    )
    assert f"same test already in research/experiments.json#{KEY_A}" in capsys.readouterr().out


def test_verify_text_cli_against_an_alpha_report(capsys):
    report = REPO / "research" / "alpha_reports" / "EQ01.json"
    doc = json.loads(report.read_text(encoding="utf-8"))
    t = doc["gate_tstat"]
    good = f"EQ01 gate t = {t}."
    assert graph_main(["verify-text", "--artefact", str(report), "--text", good]) == 0
    assert "verified" in capsys.readouterr().out
    bad = f"EQ01 gate t = {round(float(t) + 1.0, 3)}."
    assert graph_main(["verify-text", "--artefact", str(report), "--text", bad]) == 1
    assert "NOT verified" in capsys.readouterr().out


def test_cli_flags_entries_without_a_universe(tmp_path, capsys, monkeypatch):
    root = _corpus(tmp_path / "repo")
    ledger = root / "research" / "experiments.json"
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    doc["entries"][0]["config"].pop("symbols")  # same alpha and horizon, no universe
    ledger.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    monkeypatch.setattr(
        wiring.holdout_registry, "load_registry", lambda path=None, root=None: REGISTRY
    )
    hyp_file = tmp_path / "h.json"
    hyp_file.write_text(json.dumps(HYP), encoding="utf-8")
    out = tmp_path / "run"
    assert (
        graph_main(["run", "--hypothesis", str(hyp_file), "--out", str(out), "--root", str(root)])
        == 0
    )
    text = capsys.readouterr().out
    assert "same test already" not in text and "awaiting: human approval" in text
    assert f"universe not recorded: research/experiments.json#{KEY_A}" in text
