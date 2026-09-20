"""Alpha promotion lifecycle (iap.lifecycle).

Covers: config loading (fail-fast, defaults equal the pinned validate.py
gates), every gate positive / negative / absent, every edge of the
transition table, demotion counters and silence, the live sub-machine
delegation (breach, hysteresis, re-activation, retirement, terminal
RETIRED), HUMAN-only manual edges, registry / transition-log byte
determinism and round trips, the bootstrap on the real reports (0 alphas
beyond CANDIDATE; failed-gate sets agree with REPORT.md verdicts), and two
SplitMix64-driven property tests (RETIRED never moves by SYSTEM; the state
index never jumps more than one step forward except a manual retire).
"""

from __future__ import annotations

import json
import math

import pytest

from iap.adaptive.lifecycle import LifecycleConfig, LifecycleTracker
from iap.contracts.protocols import AlphaLifecycle as AlphaLifecycleProtocol
from iap.contracts.protocols import LifecycleGate as LifecycleGateProtocol
from iap.contracts.types import (
    Actor,
    GateResult,
    LifecycleState,
    LifecycleTransition,
    Verdict,
)
from iap.contracts.validate import validate_typed
from iap.core.rng import SplitMix64
from iap.lifecycle import (
    ALLOWED_TRANSITIONS,
    GATE_SPECS,
    PROMOTION_EDGES,
    STATE_COUNT,
    AlphaLifecycle,
    AlphaRecord,
    AlphaRegistry,
    EdgeKind,
    Evidence,
    GateEvaluation,
    LifecycleTransitionLog,
    LiveEvidence,
    Outcome,
    PaperEvidence,
    PolicyConfig,
    ValidationEvidence,
    build_gates,
    edge_for,
    ic_rank_gap,
    load_policy_config,
)
from iap.lifecycle.bootstrap import (
    REFERENCE_NOTIONAL_USD,
    capacity_from_report,
    load_ledger_entries,
    load_params_document,
    load_report,
    render_status,
    research_evidence,
    run_bootstrap,
)
from iap.lifecycle.config import repo_root
from iap.lifecycle.golden import research as golden_research
from iap.validation.validate import GATES as VALIDATE_GATES

S = LifecycleState
T0 = 1_700_000_000_000_000_000
STEP = 900_000_000_000
ROOT = repo_root()


@pytest.fixture(scope="module")
def config() -> PolicyConfig:
    return load_policy_config()


def _machine(config: PolicyConfig, alpha_id: str = "LCX", log=None) -> AlphaLifecycle:
    registry = AlphaRegistry(config.policy)
    machine = AlphaLifecycle(config, registry, log)
    machine.register(alpha_id, T0)
    return machine


def _ev(**kw) -> Evidence:
    base = dict(research=None, capacity_usd=None, validation=None, paper=None, live=None)
    base.update(kw)
    return Evidence(**base)


def _live(ic, informative=True, n_buckets=8, eval_index=1) -> LiveEvidence:
    return LiveEvidence(rolling_ic=ic, n_buckets=n_buckets, eval_index=eval_index,
                        informative=informative)


GOOD_VALIDATION = ValidationEvidence(holdout_ic=0.018, research_ic=0.02,
                                     replay_hash_match=True, parity=True)
GOOD_PAPER = PaperEvidence(n_sessions=5, realized_ic=0.015, research_ic=0.02,
                           net_pnl=100.0, n_kill_events=0, tracking_error=0.001)


def _to_state(machine: AlphaLifecycle, alpha_id: str, target: LifecycleState,
              ts: int = T0) -> int:
    """Drive a fresh alpha to ``target`` along the happy path; returns the
    next free event time."""
    good = golden_research(alpha_id)
    plan = [
        (S.CANDIDATE, _ev(research=good)),
        (S.VALIDATING, _ev(research=good, capacity_usd=5e6)),
        (S.PAPER, _ev(validation=GOOD_VALIDATION)),
        (S.ACTIVE, _ev(paper=GOOD_PAPER)),
    ]
    for state, evidence in plan:
        if int(target) < int(state):
            break
        ts += STEP
        tr = machine.advance(alpha_id, ts, evidence)
        assert tr is not None and tr.to_state is state
    if target is S.WATCH:
        ts += STEP
        tr = machine.advance(alpha_id, ts, _ev(live=_live(-0.01)))
        assert tr is not None and tr.to_state is S.WATCH
    assert machine.state(alpha_id) is target
    return ts


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_defaults_equal_pinned_validate_gates(config):
    g = config.gates
    assert g.min_oos_ic == VALIDATE_GATES["min_oos_ic"]
    assert g.min_nw_tstat == VALIDATE_GATES["min_nw_tstat"]
    assert g.min_fold_sign_consistency == VALIDATE_GATES["min_fold_sign_consistency"]
    assert g.min_folds == VALIDATE_GATES["min_nondegenerate_folds"]
    assert g.min_net_return_bps == 0.0
    assert config.policy == "lifecycle_v1"
    assert config.max_consecutive_failures == 3
    with open(ROOT / "configs" / "strategies" / "strategies.json") as fh:
        live = json.load(fh)["adaptive"]["lifecycle"]
    assert config.live == LifecycleConfig.from_config(live)


def test_config_round_trip(config):
    assert PolicyConfig.from_dict(config.to_dict()) == config


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.__setitem__("x-version", 2), "x-version"),
    (lambda d: d["gates"].pop("min_oos_ic"), "missing keys"),
    (lambda d: d["gates"].__setitem__("extra", 1.0), "unknown keys"),
    (lambda d: d["gates"].__setitem__("min_oos_ic", "0.01"), "expected a number"),
    (lambda d: d["gates"].__setitem__("min_folds", 0), "< 1"),
    (lambda d: d["gates"].__setitem__("min_folds", 2.0), "expected an integer"),
    (lambda d: d["gates"].__setitem__("min_fold_sign_consistency", 1.5), "[0, 1]"),
    (lambda d: d["gates"].__setitem__("ic_rank_gap_eps", 0.0), "> 0"),
    (lambda d: d["demotion"].__setitem__("max_consecutive_failures", 0), "< 1"),
    (lambda d: d.__setitem__("policy", ""), "policy"),
])
def test_config_fail_fast(tmp_path, mutate, message):
    with open(ROOT / "configs" / "strategies" / "lifecycle.json") as fh:
        doc = json.load(fh)
    mutate(doc)
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=message.replace("[", r"\[")):
        load_policy_config(path)


def test_config_rejects_non_finite_and_missing_strategies(tmp_path):
    with open(ROOT / "configs" / "strategies" / "lifecycle.json") as fh:
        doc = json.load(fh)
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps(doc).replace("0.01,", "NaN,", 1))
    with pytest.raises(ValueError, match="non-finite"):
        load_policy_config(path)
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="not found"):
        load_policy_config(path, tmp_path / "missing.json")
    (tmp_path / "strategies.json").write_text(json.dumps({"adaptive": {}}))
    with pytest.raises(ValueError, match="adaptive.lifecycle"):
        load_policy_config(path, tmp_path / "strategies.json")


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_evidence_rejects_nan_and_wrong_types():
    with pytest.raises(ValueError, match="non-finite"):
        ValidationEvidence(holdout_ic=float("nan"), research_ic=0.0,
                           replay_hash_match=True, parity=True)
    with pytest.raises(ValueError, match="expected a bool"):
        ValidationEvidence(holdout_ic=0.0, research_ic=0.0, replay_hash_match=1, parity=True)
    with pytest.raises(ValueError, match="expected an integer"):
        PaperEvidence(n_sessions=2.0, realized_ic=0.0, research_ic=0.0, net_pnl=0.0,
                      n_kill_events=0, tracking_error=0.0)
    with pytest.raises(ValueError, match=">= 0"):
        PaperEvidence(n_sessions=2, realized_ic=0.0, research_ic=0.0, net_pnl=0.0,
                      n_kill_events=0, tracking_error=-1.0)
    with pytest.raises(ValueError, match="capacity_usd"):
        _ev(capacity_usd=-1.0)
    with pytest.raises(ValueError, match="ExperimentResult"):
        _ev(research={"ic": 1.0})
    with pytest.raises(ValueError, match="evidence.live"):
        _ev(live=GOOD_PAPER)


def test_evidence_round_trip_and_strict_keys():
    ev = Evidence(research=golden_research("LC01"), capacity_usd=5e6,
                  validation=GOOD_VALIDATION, paper=GOOD_PAPER, live=_live(None))
    doc = json.loads(json.dumps(ev.to_dict()))
    assert Evidence.from_dict(doc) == ev
    assert Evidence.from_dict(Evidence.empty().to_dict()) == Evidence.empty()
    doc["extra"] = 1
    with pytest.raises(ValueError, match="unknown keys"):
        Evidence.from_dict(doc)
    del doc["extra"]
    del doc["paper"]
    with pytest.raises(ValueError, match="missing keys"):
        Evidence.from_dict(doc)
    with pytest.raises(ValueError, match="validation: unknown keys"):
        ValidationEvidence.from_dict({**GOOD_VALIDATION.to_dict(), "x": 1})


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def test_gate_table_is_complete_and_protocol_conformant(config):
    gates = build_gates(config)
    assert list(gates) == [s.name for s in GATE_SPECS]
    for gate in gates.values():
        assert isinstance(gate, LifecycleGateProtocol)
    edge_gates = {name for e in ALLOWED_TRANSITIONS for name in e.gates}
    assert edge_gates == set(gates), "every gate is used by an edge and vice versa"
    assert gates["rolling_ic"].threshold == config.live.watch_ic_gate
    with pytest.raises(TypeError):
        gates["oos_ic"].evaluate("X", {"ic": 1.0})


def _research_gate_cases(config):
    g = config.gates
    ok = golden_research("LCX")
    return [
        ("ledger_entry_exists", ok, True, float(ok.n_experiments_in_ledger)),
        ("ledger_entry_exists", golden_research("LCX", n_ledger=0), False, 0.0),
        ("leakage_clean", ok, True, None),
        ("leakage_clean", golden_research("LCX", leakage_passed=False,
                                          verdict=Verdict.REJECT), False, None),
        ("oos_ic", golden_research("LCX", ic=g.min_oos_ic), True, g.min_oos_ic),
        ("oos_ic", golden_research("LCX", ic=0.009), False, 0.009),
        ("statistical_significance", golden_research("LCX", t_stat=3.0), True, 3.0),
        ("statistical_significance", golden_research("LCX", t_stat=2.99), False, 2.99),
        ("fold_consistency", golden_research("LCX", fold_consistency=0.7), True, 0.7),
        ("fold_consistency", golden_research("LCX", fold_consistency=0.5), False, 0.5),
        ("fold_count", golden_research("LCX", n_folds=3), True, 3.0),
        ("fold_count", golden_research("LCX", n_folds=2), False, 2.0),
        ("hypothesis_sign", ok, True, None),
        ("hypothesis_sign", golden_research("LCX", hypothesis=False), False, None),
        ("hypothesis_sign", golden_research("LCX", hypothesis=None), False, None),
        ("net_pnl_after_costs", golden_research("LCX", net_bps=0.5), True, 0.5),
        ("net_pnl_after_costs", golden_research("LCX", net_bps=0.0), False, 0.0),
        ("net_pnl_after_costs", golden_research("LCX", net_bps=-1.0), False, -1.0),
        ("stability", golden_research("LCX", ic=0.02, rank_ic=0.04), True, 1.0),
        ("stability", golden_research("LCX", ic=0.02, rank_ic=0.05), False, 1.5),
        ("stability", golden_research("LCX", ic=0.02, rank_ic=-0.01), False, 1.5),
    ]


def test_research_gates_positive_negative(config):
    gates = build_gates(config)
    for name, result, expect_pass, expect_value in _research_gate_cases(config):
        out = gates[name].evaluate("LCX", _ev(research=result))
        assert out.passed is expect_pass, (name, result.to_dict())
        if expect_value is None:
            assert out.value is None and out.threshold is None
        else:
            assert out.value == pytest.approx(expect_value, abs=1e-12)
            assert out.threshold == gates[name].threshold


def test_gates_fail_with_null_value_when_evidence_absent(config):
    gates = build_gates(config)
    for name, gate in gates.items():
        out = gate.evaluate("LCX", Evidence.empty())
        assert out.passed is False and out.value is None, name
        validate_typed(out)


def test_capacity_and_validation_and_paper_gates(config):
    gates = build_gates(config)
    g = config.gates
    assert gates["capacity"].evaluate("X", _ev(capacity_usd=g.min_capacity_usd)).passed
    assert not gates["capacity"].evaluate("X", _ev(capacity_usd=g.min_capacity_usd - 1)).passed

    v = _ev(validation=GOOD_VALIDATION)
    assert gates["holdout_ic_tracks_research"].evaluate("X", v).value == pytest.approx(0.002)
    assert gates["holdout_ic_tracks_research"].evaluate("X", v).passed
    far = _ev(validation=ValidationEvidence(holdout_ic=0.0, research_ic=0.02,
                                            replay_hash_match=False, parity=False))
    assert not gates["holdout_ic_tracks_research"].evaluate("X", far).passed
    assert not gates["replay_reproducible"].evaluate("X", far).passed
    assert not gates["cross_language_parity"].evaluate("X", far).passed
    assert gates["replay_reproducible"].evaluate("X", v).passed
    assert gates["cross_language_parity"].evaluate("X", v).passed

    p = _ev(paper=GOOD_PAPER)
    for name in ("paper_min_sessions", "paper_ic_tracking", "paper_net_pnl", "no_kill_events"):
        assert gates[name].evaluate("X", p).passed, name
    bad = _ev(paper=PaperEvidence(n_sessions=4, realized_ic=0.0, research_ic=0.02,
                                  net_pnl=-0.5, n_kill_events=1, tracking_error=0.0))
    for name in ("paper_min_sessions", "paper_ic_tracking", "paper_net_pnl", "no_kill_events"):
        assert not gates[name].evaluate("X", bad).passed, name
    zero = _ev(paper=PaperEvidence(n_sessions=5, realized_ic=0.01, research_ic=0.02,
                                   net_pnl=0.0, n_kill_events=0, tracking_error=0.0))
    assert gates["paper_net_pnl"].evaluate("X", zero).passed, "paper net P&L >= 0 (inclusive)"
    assert gates["paper_ic_tracking"].evaluate("X", zero).passed, "gap 0.01 <= 0.01"


def test_rolling_ic_gate_and_ic_rank_gap(config):
    gates = build_gates(config)
    assert gates["rolling_ic"].evaluate("X", _ev(live=_live(0.0))).passed
    assert not gates["rolling_ic"].evaluate("X", _ev(live=_live(-1e-9))).passed
    assert gates["rolling_ic"].evaluate("X", _ev(live=_live(None))).value is None
    assert gates["rolling_ic"].evaluate("X", _ev(live=_live(0.5, informative=False))).value is None
    assert ic_rank_gap(0.0, 0.5, 1e-12) == pytest.approx(0.5 / 1e-12)
    assert ic_rank_gap(-0.05, -0.06, 1e-12) == pytest.approx(0.2)


# --------------------------------------------------------------------------
# Transition table
# --------------------------------------------------------------------------


def test_transition_table_shape():
    assert STATE_COUNT == 7
    assert [int(s) for s in LifecycleState] == list(range(7))
    pairs = {(e.from_state, e.to_state, e.kind) for e in ALLOWED_TRANSITIONS}
    assert len(pairs) == len(ALLOWED_TRANSITIONS)
    for e in ALLOWED_TRANSITIONS:
        if e.kind is EdgeKind.PROMOTION:
            assert int(e.to_state) == int(e.from_state) + 1 and e.gates
        if e.kind is EdgeKind.MANUAL:
            assert e.actor is Actor.HUMAN and not e.gates
        else:
            assert e.actor is Actor.SYSTEM
    assert set(PROMOTION_EDGES) == {S.RESEARCH, S.CANDIDATE, S.VALIDATING, S.PAPER}
    manual_retire = {e.from_state for e in ALLOWED_TRANSITIONS
                     if e.kind is EdgeKind.MANUAL and e.to_state is S.RETIRED}
    assert manual_retire == set(LifecycleState) - {S.RETIRED}
    assert edge_for(S.RETIRED, S.RESEARCH, EdgeKind.MANUAL).actor is Actor.HUMAN
    with pytest.raises(KeyError):
        edge_for(S.RETIRED, S.WATCH, EdgeKind.LIVE)
    live = {(e.from_state, e.to_state) for e in ALLOWED_TRANSITIONS if e.kind is EdgeKind.LIVE}
    assert live == {(S.ACTIVE, S.WATCH), (S.WATCH, S.ACTIVE), (S.WATCH, S.RETIRED)}


# --------------------------------------------------------------------------
# Machine: promotion / demotion edges
# --------------------------------------------------------------------------


def test_machine_satisfies_protocol_and_registration(config):
    m = _machine(config)
    assert isinstance(m, AlphaLifecycleProtocol)
    assert m.state("LCX") is S.RESEARCH
    with pytest.raises(ValueError, match="already registered"):
        m.register("LCX", T0)
    with pytest.raises(KeyError):
        m.state("nope")
    with pytest.raises(TypeError):
        m.advance("LCX", T0, {"research": None})
    with pytest.raises(ValueError, match="policy"):
        AlphaLifecycle(config, AlphaRegistry("other"))


def test_happy_path_every_promotion_edge(config):
    m = _machine(config)
    _to_state(m, "LCX", S.ACTIVE)
    states = [(t.from_state, t.to_state) for t in m.transitions]
    assert states == [(S.RESEARCH, S.CANDIDATE), (S.CANDIDATE, S.VALIDATING),
                      (S.VALIDATING, S.PAPER), (S.PAPER, S.ACTIVE)]
    for t, edge_state in zip(m.transitions, (S.RESEARCH, S.CANDIDATE, S.VALIDATING, S.PAPER)):
        assert list(t.gates) == list(PROMOTION_EDGES[edge_state].gates)
        assert all(g.passed for g in t.gates.values())
        assert t.actor is Actor.SYSTEM and t.policy == config.policy
        validate_typed(t)
    assert [e.outcome for e in m.evaluations] == [Outcome.TRANSITION] * 4
    rec = m.record("LCX")
    assert rec.since_ts == m.transitions[-1].event_ts
    assert rec.last_transition == m.transitions[-1]


def test_research_presence_gate_and_hold(config):
    m = _machine(config)
    assert m.advance("LCX", T0 + 1, Evidence.empty()) is None
    ev = m.evaluations[-1]
    assert ev.outcome == Outcome.HOLD and ev.failed_gates == ["ledger_entry_exists", "leakage_clean"]
    assert ev.gates["ledger_entry_exists"].value is None
    assert m.advance("LCX", T0 + 2, _ev(research=golden_research("LCX", n_ledger=0))) is None
    assert m.evaluations[-1].failed_gates == ["ledger_entry_exists"]
    assert m.state("LCX") is S.RESEARCH and m.record("LCX").consecutive_failures == 0


def test_candidate_leakage_demotion_and_hold(config):
    m = _machine(config)
    _to_state(m, "LCX", S.CANDIDATE)
    leaking = golden_research("LCX", leakage_passed=False, verdict=Verdict.REJECT)
    tr = m.advance("LCX", T0 + 10, _ev(research=leaking, capacity_usd=5e6))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.CANDIDATE, S.RESEARCH)
    assert tr.actor is Actor.SYSTEM and "leakage" in tr.reason
    assert not tr.gates["leakage_clean"].passed
    assert list(tr.gates) == list(PROMOTION_EDGES[S.CANDIDATE].gates)
    # a non-leakage failure at CANDIDATE holds, no counter
    m2 = _machine(config)
    _to_state(m2, "LCX", S.CANDIDATE)
    weak = golden_research("LCX", t_stat=1.0)
    for k in range(5):
        assert m2.advance("LCX", T0 + 10 + k, _ev(research=weak, capacity_usd=5e6)) is None
        assert m2.evaluations[-1].failed_gates == ["statistical_significance"]
    assert m2.state("LCX") is S.CANDIDATE and m2.record("LCX").consecutive_failures == 0


def test_candidate_capacity_missing_fails_capacity_gate_only(config):
    m = _machine(config)
    _to_state(m, "LCX", S.CANDIDATE)
    assert m.advance("LCX", T0 + 10, _ev(research=golden_research("LCX"))) is None
    assert m.evaluations[-1].failed_gates == ["capacity"]


@pytest.mark.parametrize("state", [S.VALIDATING, S.PAPER])
def test_persistent_failure_demotes_to_candidate(config, state):
    m = _machine(config)
    ts = _to_state(m, "LCX", state)
    bad = (_ev(validation=ValidationEvidence(0.0, 0.02, False, False)) if state is S.VALIDATING
           else _ev(paper=PaperEvidence(1, 0.0, 0.02, -1.0, 2, 0.0)))
    n = config.max_consecutive_failures
    for k in range(1, n):
        ts += STEP
        assert m.advance("LCX", ts, bad) is None
        assert m.record("LCX").consecutive_failures == k
        assert m.evaluations[-1].outcome == Outcome.HOLD
    ts += STEP
    tr = m.advance("LCX", ts, bad)
    assert tr is not None and (tr.from_state, tr.to_state) == (state, S.CANDIDATE)
    assert f"{n} consecutive" in tr.reason
    assert m.record("LCX").consecutive_failures == 0
    assert m.record("LCX").since_ts == ts
    validate_typed(tr)


@pytest.mark.parametrize("state", [S.VALIDATING, S.PAPER])
def test_silence_and_success_reset_counter(config, state):
    m = _machine(config)
    ts = _to_state(m, "LCX", state)
    bad = (_ev(validation=ValidationEvidence(0.0, 0.02, True, True)) if state is S.VALIDATING
           else _ev(paper=PaperEvidence(1, 0.015, 0.02, 1.0, 0, 0.0)))
    good = _ev(validation=GOOD_VALIDATION) if state is S.VALIDATING else _ev(paper=GOOD_PAPER)
    assert m.advance("LCX", ts + 1, bad) is None
    assert m.record("LCX").consecutive_failures == 1
    assert m.advance("LCX", ts + 2, Evidence.empty()) is None
    assert m.evaluations[-1].outcome == Outcome.NO_EVIDENCE and m.evaluations[-1].gates == {}
    assert m.record("LCX").consecutive_failures == 1, "silence moves nothing"
    assert m.advance("LCX", ts + 3, bad) is None
    assert m.record("LCX").consecutive_failures == 2
    tr = m.advance("LCX", ts + 4, good)
    assert tr is not None and int(tr.to_state) == int(state) + 1
    assert m.record("LCX").consecutive_failures == 0


# --------------------------------------------------------------------------
# Machine: live sub-machine delegation
# --------------------------------------------------------------------------


def test_live_edges_match_the_adaptive_tracker(config):
    """The wrapped machine reproduces LifecycleTracker state-for-state on
    the golden adaptive ic_path, up to the (terminal) retirement."""
    with open(ROOT / "tests" / "golden" / "expected_adaptive.json") as fh:
        lc = json.load(fh)["lifecycle"]
    tracker = LifecycleTracker("EQ01", LifecycleConfig.from_config(lc["config"]), policy="p")
    m = _machine(config, "EQ01")
    ts = _to_state(m, "EQ01", S.ACTIVE)
    for k, (ic, informative) in enumerate(zip(lc["ic_path"], lc["ic_informative"])):
        ts += lc["ts_step_ns"]
        expected = tracker.update(ts, ic, informative)
        if expected == "RETIRED":
            break
        m.advance("EQ01", ts, _ev(live=_live(ic, informative, eval_index=k)))
        assert m.state("EQ01").name == expected == lc["expected_states"][k]
        rec = m.record("EQ01")
        assert (rec.breach_count, rec.recovery_count) == (tracker.breach_count,
                                                          tracker.recovery_count)


def test_live_breach_watch_retire_and_wrapping(config):
    m = _machine(config)
    ts = _to_state(m, "LCX", S.ACTIVE)
    assert m.advance("LCX", ts + 1, _ev(live=_live(0.01))) is None
    assert m.evaluations[-1].outcome == Outcome.HOLD
    assert m.advance("LCX", ts + 2, _ev(live=_live(None))) is None
    assert m.evaluations[-1].outcome == Outcome.NO_EVIDENCE
    assert m.advance("LCX", ts + 3, _ev(live=_live(-0.5, informative=False))) is None
    assert m.evaluations[-1].outcome == Outcome.NO_EVIDENCE
    assert m.advance("LCX", ts + 4, Evidence.empty()) is None
    assert m.state("LCX") is S.ACTIVE
    tr = m.advance("LCX", ts + 5, _ev(live=_live(-0.01)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.ACTIVE, S.WATCH)
    assert tr.gates == {"rolling_ic": GateResult(passed=False, value=-0.01,
                                                 threshold=config.live.watch_ic_gate)}
    assert tr.policy == config.policy and tr.actor is Actor.SYSTEM
    assert "watch gate" in tr.reason
    assert m.record("LCX").breach_count == 1, "the entering breach counts"
    n = config.live.retire_breach_evals
    for k in range(2, n):
        assert m.advance("LCX", ts + 5 + k, _ev(live=_live(-0.01))) is None
        assert m.record("LCX").breach_count == k
    tr = m.advance("LCX", ts + 5 + n, _ev(live=_live(-0.01)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.RETIRED)
    assert "persistent breach" in tr.reason
    validate_typed(tr)
    assert m.record("LCX").breach_count == 0


def test_live_neutral_zone_and_reactivation(config):
    m = _machine(config)
    ts = _to_state(m, "LCX", S.WATCH)
    assert m.advance("LCX", ts + 1, _ev(live=_live(-0.01))) is None
    assert m.record("LCX").breach_count == 2
    assert m.advance("LCX", ts + 2, _ev(live=_live(0.002))) is None
    assert (m.record("LCX").breach_count, m.record("LCX").recovery_count) == (0, 0)
    n = config.live.reactivate_evals
    for k in range(1, n):
        assert m.advance("LCX", ts + 2 + k, _ev(live=_live(0.005))) is None
        assert m.record("LCX").recovery_count == k
    tr = m.advance("LCX", ts + 2 + n, _ev(live=_live(0.005)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.ACTIVE)
    assert tr.gates["rolling_ic"] == GateResult(passed=True, value=0.005,
                                                threshold=config.live.reactivate_ic_gate)


def test_retired_is_terminal_for_system(config):
    m = _machine(config)
    ts = _to_state(m, "LCX", S.CANDIDATE)
    m.retire("LCX", ts + 1, "desk decision")
    for ev in (_ev(live=_live(0.5)), _ev(research=golden_research("LCX"), capacity_usd=1e9),
               Evidence.empty()):
        assert m.advance("LCX", ts + 2, ev) is None
        assert m.evaluations[-1].outcome == Outcome.TERMINAL
    assert m.state("LCX") is S.RETIRED


# --------------------------------------------------------------------------
# Manual edges
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", [s for s in LifecycleState if s is not S.RETIRED])
def test_manual_retire_from_every_state(config, state):
    m = _machine(config)
    ts = _to_state(m, "LCX", state)
    tr = m.retire("LCX", ts + 1, "portfolio manager withdrew the strategy")
    assert (tr.from_state, tr.to_state, tr.actor) == (state, S.RETIRED, Actor.HUMAN)
    assert tr.gates == {} and tr.policy == config.policy
    validate_typed(tr)
    assert m.state("LCX") is S.RETIRED and m.record("LCX").since_ts == ts + 1
    with pytest.raises(ValueError, match="already RETIRED"):
        m.retire("LCX", ts + 2, "again")
    back = m.reset_to_research("LCX", ts + 3, "re-research")
    assert (back.from_state, back.to_state) == (S.RETIRED, S.RESEARCH)
    assert m.state("LCX") is S.RESEARCH


def test_human_only_edges_reject_system_and_empty_reason(config):
    m = _machine(config)
    with pytest.raises(ValueError, match="HUMAN"):
        m.retire("LCX", T0 + 1, "reason", actor=Actor.SYSTEM)
    with pytest.raises(ValueError, match="reason"):
        m.retire("LCX", T0 + 1, "   ")
    with pytest.raises(TypeError):
        m.retire("LCX", T0 + 1, "reason", actor="HUMAN")
    with pytest.raises(ValueError, match="not RETIRED"):
        m.reset_to_research("LCX", T0 + 1, "reason")
    m.retire("LCX", T0 + 2, "reason")
    with pytest.raises(ValueError, match="HUMAN"):
        m.reset_to_research("LCX", T0 + 3, "reason", actor=Actor.SYSTEM)
    assert m.state("LCX") is S.RETIRED


# --------------------------------------------------------------------------
# Registry / log
# --------------------------------------------------------------------------


def test_registry_and_log_round_trip_and_bytes(config, tmp_path):
    log_path = tmp_path / "transitions.jsonl"
    m = _machine(config, "LC01", LifecycleTransitionLog(log_path))
    m.register("LC02", T0)
    ts = _to_state(m, "LC01", S.WATCH)
    m.advance("LC02", ts, _ev(research=golden_research("LC02", t_stat=1.0)))
    m.retire("LC02", ts + 1, "manual")
    reg_path = tmp_path / "registry.json"
    m.registry.save(reg_path)
    text = reg_path.read_text()
    assert text.endswith("\n") and text == m.registry.render()
    assert json.loads(text)["x-version"] == 1
    loaded = AlphaRegistry.load(reg_path)
    assert loaded.render() == text
    assert loaded.alpha_ids() == ["LC01", "LC02"]
    for aid in loaded.alpha_ids():
        assert loaded.get(aid) == m.registry.get(aid)
    logged = LifecycleTransitionLog(log_path).read_all()
    assert logged == m.transitions
    for line in log_path.read_text().splitlines():
        assert line == json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))

    # a reloaded registry resumes the live counters exactly
    m2 = AlphaLifecycle(config, loaded)
    assert loaded.get("LC01").breach_count == 1
    assert m2.advance("LC01", ts + 5, _ev(live=_live(-0.01))) is None
    assert loaded.get("LC01").breach_count == 2


def test_registry_rejects_bad_documents(tmp_path):
    reg = AlphaRegistry("p")
    reg.add(AlphaRecord.new("A1", T0))
    doc = reg.to_dict()
    with pytest.raises(ValueError, match="x-version"):
        AlphaRegistry.from_dict({**doc, "x-version": 2})
    bad = json.loads(json.dumps(doc))
    bad["alphas"]["A1"]["state_index"] = 3
    with pytest.raises(ValueError, match="state_index"):
        AlphaRegistry.from_dict(bad)
    bad = json.loads(json.dumps(doc))
    bad["alphas"]["ZZ"] = bad["alphas"].pop("A1")
    with pytest.raises(ValueError, match="key"):
        AlphaRegistry.from_dict(bad)
    with pytest.raises(ValueError, match="sha256"):
        AlphaRecord.new("A1", T0, data_version="abc")
    with pytest.raises(ValueError, match="already registered"):
        reg.add(AlphaRecord.new("A1", T0))
    with pytest.raises(ValueError, match="outcome"):
        GateEvaluation("A1", T0, S.RESEARCH, "WHAT", {}, 0, None)


# --------------------------------------------------------------------------
# Bootstrap on the real research artefacts
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bootstrap():
    return run_bootstrap(ROOT, write=False)


def test_bootstrap_no_alpha_beyond_candidate(bootstrap):
    assert len(bootstrap.rows) == 24
    assert bootstrap.count_by_state() == {"CANDIDATE": 24}
    assert bootstrap.event_ts == 1787691480577291027
    for row in bootstrap.rows:
        assert row.missing_metrics == ()
        assert "net_pnl_after_costs" in row.failed_gates, "no alpha survives 1x costs"
    assert len(bootstrap.machine.transitions) == 24
    assert all(t.to_state is S.CANDIDATE for t in bootstrap.machine.transitions)


def test_bootstrap_failed_gates_agree_with_report_verdicts(bootstrap):
    """ITERATE alphas fail exactly the PROMOTE gates the report says they fail;
    REJECT alphas (all leakage-clean here) fail the ITERATE thresholds."""
    ledger = load_ledger_entries(ROOT)
    for row in bootstrap.rows:
        rep = load_report(ROOT, row.alpha_id)
        ev = bootstrap.registry.get(row.alpha_id).last_evaluation
        assert ev is not None and ev.state is S.CANDIDATE
        expected_failed = []
        gate_ic, gate_t = rep["gate_ic"], rep["nw_tstat_uncrossed"]
        if not rep["leakage"]["passed"]:
            expected_failed.append("leakage_clean")
        if gate_ic < VALIDATE_GATES["min_oos_ic"]:
            expected_failed.append("oos_ic")
        if gate_t < VALIDATE_GATES["min_nw_tstat"]:
            expected_failed.append("statistical_significance")
        if rep["fold_sign_consistency"] < VALIDATE_GATES["min_fold_sign_consistency"]:
            expected_failed.append("fold_consistency")
        if rep["n_folds_run"] < VALIDATE_GATES["min_nondegenerate_folds"]:
            expected_failed.append("fold_count")
        if not rep["hypothesis_confirmed"]:
            expected_failed.append("hypothesis_sign")
        if not rep["net_pnl_1x_cost"] > 0.0:
            expected_failed.append("net_pnl_after_costs")
        report_gates = [g for g in ev.failed_gates if g not in ("capacity", "stability")]
        assert report_gates == expected_failed, row.alpha_id
        assert row.verdict == rep["verdict"]
        if rep["verdict"] == "ITERATE":
            assert ev.gates["oos_ic"].value >= VALIDATE_GATES["iterate_min_ic"]
            assert ev.gates["statistical_significance"].value >= VALIDATE_GATES["iterate_min_tstat"]
        else:
            assert (ev.gates["oos_ic"].value < VALIDATE_GATES["iterate_min_ic"]
                    or ev.gates["statistical_significance"].value < VALIDATE_GATES["iterate_min_tstat"])
        assert ev.gates["ledger_entry_exists"].value == float(ledger[row.alpha_id]["n"]) \
            if "ledger_entry_exists" in ev.gates else True
    by_id = {r.alpha_id: r.failed_gates for r in bootstrap.rows}
    # Hand cross-checks against REPORT.md. Updated 2026-09-20: several gates
    # moved when fold_sign_consistency stopped scoring the beta-SIGNED signal
    # (an alpha backwards in every fold used to report 1.00 consistency and
    # pass this gate) and when the walk-forward stopped training inside its
    # own declared holdout. EQ05 and FX01 now clear statistical_significance
    # on the pair-count-weighted Newey-West t; EQ07, FX01 and FX03 now fail
    # fold_consistency, which they previously passed falsely.
    assert by_id["EQ01"] == ("net_pnl_after_costs",)
    assert by_id["EQ05"] == ("net_pnl_after_costs",)
    assert by_id["FX01"] == ("fold_consistency", "net_pnl_after_costs")
    assert by_id["EQ07"] == ("oos_ic", "statistical_significance", "fold_consistency",
                             "hypothesis_sign", "net_pnl_after_costs")
    assert set(by_id["FX03"]) >= {"statistical_significance", "hypothesis_sign",
                                  "fold_consistency"}


def test_bootstrap_research_mapping(bootstrap):
    rep = load_report(ROOT, "EQ03")
    ledger = load_ledger_entries(ROOT)
    params = load_params_document(ROOT)
    result, missing = research_evidence("EQ03", rep, ledger["EQ03"], params)
    assert missing == [] and result is not None
    assert result.ic == rep["gate_ic"] == rep["oos_ic_uncrossed"]
    assert result.t_stat == rep["nw_tstat_uncrossed"]
    assert result.rank_ic == rep["oos_rank_ic"]
    assert result.experiment_id == ledger["EQ03"]["key"][:16]
    assert result.n_experiments_in_ledger == ledger["EQ03"]["n"]
    assert result.created_ts == max(f["test_end"] for f in rep["folds"])
    scale = 1e4 / REFERENCE_NOTIONAL_USD
    assert result.net_return_bps == pytest.approx(rep["stress"]["cost"]["x1"]["total_pnl"] * scale)
    assert result.transaction_cost_bps == pytest.approx(
        rep["stress"]["cost"]["x1"]["total_costs"] * scale)
    assert result.verdict is Verdict.ITERATE and result.leakage_passed
    assert result.dataset_version == params["data_version"]
    assert capacity_from_report(rep) == pytest.approx(sum(rep["capacity_usd_by_instrument"].values()))
    rec = bootstrap.registry.get("EQ03")
    assert (rec.experiment_id, rec.data_version, rec.feature_version, rec.model_version) == (
        result.experiment_id, result.dataset_version, result.feature_version,
        result.model_version)
    validate_typed(result)


def test_bootstrap_nan_metric_is_reported_not_crashed(tmp_path):
    rep = load_report(ROOT, "EQ03")
    rep["gate_ic"] = None
    rep["oos_rank_ic"] = float("nan")
    ledger = load_ledger_entries(ROOT)
    result, missing = research_evidence("EQ03", rep, ledger["EQ03"], load_params_document(ROOT))
    assert result is None and missing == ["gate_ic", "oos_rank_ic"]
    unledgered, _ = research_evidence("EQ03", load_report(ROOT, "EQ03"), None,
                                      load_params_document(ROOT))
    assert unledgered is not None and unledgered.n_experiments_in_ledger == 0
    assert unledgered.experiment_id == "EQ03-unledgered"

    # end to end: a copy of the tree with one NaN report stays at RESEARCH
    root = tmp_path / "repo"
    for rel in ("configs/strategies", "research/alpha_reports"):
        (root / rel).mkdir(parents=True)
    for name in ("lifecycle.json", "strategies.json", "alpha_params.json"):
        (root / "configs" / "strategies" / name).write_bytes(
            (ROOT / "configs" / "strategies" / name).read_bytes())
    (root / "research" / "experiments.json").write_bytes(
        (ROOT / "research" / "experiments.json").read_bytes())
    for aid in ("EQ01", "EQ03"):
        doc = load_report(ROOT, aid)
        if aid == "EQ03":
            doc["nw_tstat_uncrossed"] = None
            doc["nw_tstat"] = None
        (root / "research" / "alpha_reports" / f"{aid}.json").write_text(json.dumps(doc))
    out = run_bootstrap(root, write=True, alpha_ids=["EQ01", "EQ03"])
    states = {r.alpha_id: r.state for r in out.rows}
    assert states == {"EQ01": S.CANDIDATE, "EQ03": S.RESEARCH}
    eq03 = out.registry.get("EQ03")
    assert eq03.last_evaluation.failed_gates == ["ledger_entry_exists", "leakage_clean"]
    assert eq03.last_evaluation.gates["ledger_entry_exists"].value is None
    assert [r.missing_metrics for r in out.rows if r.alpha_id == "EQ03"] == [("nw_tstat",)]
    assert (root / "research" / "alpha_registry.json").is_file()
    assert len((root / "research" / "lifecycle_transitions.jsonl").read_text().splitlines()) == 1
    table = render_status(out.registry)
    assert "EQ03 | RESEARCH" in table and "EQ01 | CANDIDATE" in table


def test_bootstrap_refuses_to_truncate_the_transition_log_without_force(tmp_path, capsys):
    """``research/lifecycle_transitions.jsonl`` is an append-only audit: a
    HUMAN retire/reset line must survive a routine bootstrap.  A write over a
    non-empty log raises ``TransitionLogExists`` (CLI exit 3) unless forced;
    ``--dry-run`` never touches it; a forced rerun rewrites identical bytes."""
    from iap.lifecycle.__main__ import main as lifecycle_main
    from iap.lifecycle.bootstrap import TransitionLogExists

    root = tmp_path / "repo"
    for rel in ("configs/strategies", "research/alpha_reports"):
        (root / rel).mkdir(parents=True)
    for name in ("lifecycle.json", "strategies.json", "alpha_params.json"):
        (root / "configs" / "strategies" / name).write_bytes(
            (ROOT / "configs" / "strategies" / name).read_bytes())
    (root / "research" / "experiments.json").write_bytes(
        (ROOT / "research" / "experiments.json").read_bytes())
    for path in sorted((ROOT / "research" / "alpha_reports").glob("*.json")):
        (root / "research" / "alpha_reports" / path.name).write_bytes(path.read_bytes())
    log_path = root / "research" / "lifecycle_transitions.jsonl"
    registry_path = root / "research" / "alpha_registry.json"

    # First bootstrap: no log yet, nothing to protect.
    run_bootstrap(root, write=True)
    first_log = log_path.read_bytes()
    first_registry = registry_path.read_bytes()
    assert len(first_log.splitlines()) == 24

    # A HUMAN action appends to the audit.
    _, registry, machine = __import__("iap.lifecycle.__main__", fromlist=["_load"])._load(root)
    machine.retire("EQ03", 1_800_000_000_000_000_000, "review 2026-09-20", actor=Actor.HUMAN)
    registry.save(registry_path)
    audited = log_path.read_bytes()
    assert len(audited.splitlines()) == 25 and audited.startswith(first_log)

    # A routine bootstrap must not destroy it.
    with pytest.raises(TransitionLogExists, match="25 transition"):
        run_bootstrap(root, write=True)
    assert log_path.read_bytes() == audited
    assert registry_path.read_bytes() != first_registry     # the retire is still there
    rc = lifecycle_main(["--root", str(root), "bootstrap"])
    assert rc == 3 and "append-only" in capsys.readouterr().err
    assert log_path.read_bytes() == audited
    assert lifecycle_main(["--root", str(root), "bootstrap", "--dry-run"]) == 0
    assert log_path.read_bytes() == audited

    # Forced: rebuilt from research/, byte-identical to the first bootstrap.
    run_bootstrap(root, write=True, force=True)
    assert log_path.read_bytes() == first_log
    assert registry_path.read_bytes() == first_registry
    # An empty log is not an audit: no force needed.
    log_path.write_text("")
    run_bootstrap(root, write=True)
    assert log_path.read_bytes() == first_log


# --------------------------------------------------------------------------
# Property tests (SplitMix64-driven, pinned seed)
# --------------------------------------------------------------------------


_BLOCK_FOR_STATE = {S.RESEARCH: 1, S.CANDIDATE: 1, S.VALIDATING: 2, S.PAPER: 3,
                    S.ACTIVE: 4, S.WATCH: 4, S.RETIRED: 4}


def _random_evidence(rng: SplitMix64, alpha_id: str, state: LifecycleState) -> Evidence:
    """Random evidence; half the time the block the current state reads,
    otherwise any block (including none), so every state is exercised."""
    pick = _BLOCK_FOR_STATE[state] if rng.below(2) == 0 else rng.below(5)
    if pick == 0:
        return Evidence.empty()
    if pick == 1:
        leak = rng.below(8) == 0
        ic = rng.uniform() * 0.04
        return _ev(research=golden_research(
            alpha_id, ic=ic, rank_ic=ic * (0.5 + rng.uniform()),
            t_stat=2.0 + rng.uniform() * 4.0,
            fold_consistency=round(0.5 + rng.uniform() * 0.5, 2),
            net_bps=rng.uniform() * 10.0 - 1.0, leakage_passed=not leak,
            verdict=Verdict.REJECT if leak else Verdict.PROMOTE),
            capacity_usd=5e5 + rng.uniform() * 4e6)
    if pick == 2:
        return _ev(validation=ValidationEvidence(
            holdout_ic=0.005 + rng.uniform() * 0.03, research_ic=0.02,
            replay_hash_match=rng.below(4) > 0, parity=rng.below(4) > 0))
    if pick == 3:
        return _ev(paper=PaperEvidence(
            n_sessions=3 + rng.below(6), realized_ic=0.005 + rng.uniform() * 0.03,
            research_ic=0.02, net_pnl=rng.uniform() * 200.0 - 20.0,
            n_kill_events=int(rng.below(4) == 0), tracking_error=rng.uniform()))
    ic = None if rng.below(5) == 0 else rng.uniform() * 0.04 - 0.02
    return _ev(live=_live(ic, informative=rng.below(6) > 0, eval_index=rng.below(1000)))


def test_property_state_index_steps_and_retired_terminal(config):
    rng = SplitMix64(20260919)
    m = _machine(config, "P1")
    m.register("P2", T0)
    ts = T0
    reached = set()
    for _ in range(3000):
        ts += STEP
        aid = "P1" if rng.below(2) == 0 else "P2"
        before = m.state(aid)
        action = rng.below(40)
        if action == 0:
            if before is S.RETIRED:
                tr = m.reset_to_research(aid, ts, "property reset")
                assert tr.to_state is S.RESEARCH
            else:
                tr = m.retire(aid, ts, "property retire")
                assert tr.to_state is S.RETIRED and tr.actor is Actor.HUMAN
            continue
        tr = m.advance(aid, ts, _random_evidence(rng, aid, before))
        after = m.state(aid)
        reached.add(after)
        if before is S.RETIRED:
            assert tr is None and after is S.RETIRED, "RETIRED never moves by SYSTEM"
            assert m.evaluations[-1].outcome == Outcome.TERMINAL
            continue
        assert int(after) - int(before) <= 1, "never more than one step forward"
        if tr is not None:
            assert tr.actor is Actor.SYSTEM and (tr.from_state, tr.to_state) == (before, after)
            kind = EdgeKind.LIVE if before in (S.ACTIVE, S.WATCH) else (
                EdgeKind.PROMOTION if int(after) > int(before) else EdgeKind.DEMOTION)
            edge_for(before, after, kind)
            validate_typed(tr)
        else:
            assert after is before
        assert m.record(aid).consecutive_failures < config.max_consecutive_failures
    assert reached >= {S.CANDIDATE, S.VALIDATING, S.PAPER, S.ACTIVE, S.WATCH, S.RETIRED}
    assert m.transitions and all(isinstance(t, LifecycleTransition) for t in m.transitions)


def test_property_transition_log_replays_to_registry(config, tmp_path):
    rng = SplitMix64(7)
    log = LifecycleTransitionLog(tmp_path / "t.jsonl")
    m = _machine(config, "P1", log)
    ts = T0
    for _ in range(600):
        ts += STEP
        if rng.below(50) == 0 and m.state("P1") is not S.RETIRED:
            m.retire("P1", ts, "r")
        elif m.state("P1") is S.RETIRED and rng.below(3) == 0:
            m.reset_to_research("P1", ts, "reset")
        else:
            m.advance("P1", ts, _random_evidence(rng, "P1", m.state("P1")))
    replay = S.RESEARCH
    for t in log.read_all():
        assert t.from_state is replay
        replay = t.to_state
    assert replay is m.state("P1")
    assert not any(math.isnan(g.value) for t in m.transitions
                   for g in t.gates.values() if g.value is not None)
