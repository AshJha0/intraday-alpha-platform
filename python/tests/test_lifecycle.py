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

Two policies (v1.5.0).  The ``config`` fixture is the DEFAULT policy: the
``statistical_significance`` gate reads the threshold carried by the
evidence (``tstat_threshold = "ledger"``) and the live retirement rule is
the CUSUM.  The ``legacy`` fixture is the v1.4.0 policy selected by name
(``iap.lifecycle.golden.legacy_config``: the fixed 3.0 threshold and the
consecutive-breach rule); the tests written for those rules run under it
with their assertions unchanged and say "legacy" in their names.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re

import pytest
from iap.adaptive.lifecycle import LifecycleConfig, LifecycleTracker
from iap.alpha import ALPHA_IDS
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
    BOOTSTRAP_GATE,
    GATE_SPECS,
    PROMOTION_EDGES,
    STATE_COUNT,
    AlphaLifecycle,
    AlphaRecord,
    AlphaRegistry,
    CrossAlphaEvidence,
    CrossAlphaPeer,
    EdgeKind,
    Evidence,
    GateEvaluation,
    LifecycleTransitionLog,
    LiveEvidence,
    Outcome,
    PaperEvidence,
    PnlBootstrapEvidence,
    PolicyConfig,
    ValidationEvidence,
    bootstrap_gate_reason,
    build_gates,
    edge_for,
    ic_rank_gap,
    load_policy_config,
)
from iap.lifecycle.bootstrap import (
    REFERENCE_NOTIONAL_USD,
    capacity_from_report,
    cross_alpha_evidence,
    load_ledger_entries,
    load_params_document,
    load_report,
    load_signal_correlations,
    pnl_bootstrap_from_report,
    render_status,
    research_evidence,
    run_bootstrap,
    significance_threshold_from_report,
)
from iap.lifecycle.config import repo_root
from iap.lifecycle.golden import GOOD_BOOTSTRAP, NO_PEERS, legacy_config
from iap.lifecycle.golden import bootstrap as golden_bootstrap
from iap.lifecycle.golden import research as golden_research
from iap.validation.ledger import ExperimentLedger
from iap.validation.validate import GATES as VALIDATE_GATES

S = LifecycleState
T0 = 1_700_000_000_000_000_000
STEP = 900_000_000_000
ROOT = repo_root()
#: The ledger threshold the helper evidence carries (golden t 4.0 clears it).
THRESHOLD = 3.5
#: One 15-minute block of a 2-hour rolling-IC window (``live.new_fraction``).
BLOCK_FRACTION = 0.125


@pytest.fixture(scope="module")
def config() -> PolicyConfig:
    """The default policy (ledger significance threshold, CUSUM retirement)."""
    return load_policy_config()


@pytest.fixture(scope="module")
def legacy(config) -> PolicyConfig:
    """The v1.4.0 policy by name (fixed threshold, consecutive breaches)."""
    return legacy_config(config)


def _policy(config: PolicyConfig, name: str) -> PolicyConfig:
    return config if name == "default" else legacy_config(config)


def _machine(config: PolicyConfig, alpha_id: str = "LCX", log=None) -> AlphaLifecycle:
    registry = AlphaRegistry(config.policy)
    machine = AlphaLifecycle(config, registry, log)
    machine.register(alpha_id, T0)
    return machine


def _ev(**kw) -> Evidence:
    """Evidence with the named blocks.  It carries the EMPTY cross-alpha
    block (no other alpha: the correlation gate passes vacuously) and a
    P&L bootstrap interval above zero unless a test passes its own, so
    every test below keeps testing its one rule."""
    base = dict(
        research=None,
        capacity_usd=None,
        validation=None,
        paper=None,
        live=None,
        cross_alpha=NO_PEERS,
        pnl_bootstrap=GOOD_BOOTSTRAP,
    )
    base.update(kw)
    return Evidence(**base)


def _live(
    ic, informative=True, n_buckets=8, eval_index=1, new_fraction=BLOCK_FRACTION
) -> LiveEvidence:
    return LiveEvidence(
        rolling_ic=ic,
        n_buckets=n_buckets,
        eval_index=eval_index,
        informative=informative,
        new_fraction=new_fraction,
    )


def _cusum_step(stat: float, ic: float, live: LifecycleConfig, fraction: float) -> float:
    """One CUSUM update, as pinned in ``iap.adaptive.lifecycle``."""
    stat = stat + fraction * (live.watch_ic_gate - ic - live.cusum_k)
    return stat if stat > 0.0 else 0.0


GOOD_VALIDATION = ValidationEvidence(
    holdout_ic=0.018, research_ic=0.02, replay_hash_match=True, parity=True
)
GOOD_PAPER = PaperEvidence(
    n_sessions=5,
    realized_ic=0.015,
    research_ic=0.02,
    net_pnl=100.0,
    n_kill_events=0,
    tracking_error=0.001,
)


def _to_state(machine: AlphaLifecycle, alpha_id: str, target: LifecycleState, ts: int = T0) -> int:
    """Drive a fresh alpha to ``target`` along the happy path; returns the
    next free event time."""
    good = golden_research(alpha_id)
    plan = [
        (S.CANDIDATE, _ev(research=good)),
        (S.VALIDATING, _ev(research=good, capacity_usd=5e6, significance_threshold=THRESHOLD)),
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
    # the v1.5.0 defaults: the evidence's ledger threshold, the CUSUM rule
    assert config.tstat_threshold == "ledger"
    assert (config.live.breach_rule, config.live.cusum_k, config.live.cusum_h) == (
        "cusum",
        0.0025,
        0.01,
    )


def test_legacy_config_names_the_v140_policy(config, legacy):
    """``legacy_config`` keeps every gate and selects the two v1.4.0 rules
    by name: the fixed threshold and the consecutive-breach rule."""
    assert legacy.tstat_threshold == "fixed"
    assert legacy.live == LifecycleConfig.legacy(0.0, 0.005, 6, 3)
    assert legacy.live.breach_rule == "consecutive"
    assert (legacy.policy, legacy.gates, legacy.max_consecutive_failures) == (
        config.policy,
        config.gates,
        config.max_consecutive_failures,
    )
    assert legacy != config
    assert PolicyConfig.from_dict(legacy.to_dict()) == legacy
    assert legacy.to_dict()["tstat_threshold"] == "fixed"
    assert legacy.to_dict()["live"]["breach_rule"] == "consecutive"
    with pytest.raises(ValueError, match="tstat_threshold"):
        PolicyConfig(
            config.policy,
            config.gates,
            config.max_consecutive_failures,
            config.live,
            tstat_threshold="bonferroni",
        )


def test_config_round_trip(config):
    assert PolicyConfig.from_dict(config.to_dict()) == config


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.__setitem__("x-version", 1), "x-version"),  # a v1.4.0 document
        (lambda d: d.__setitem__("x-version", 2), "x-version"),  # v1.5.0 before the gates
        (lambda d: d.__setitem__("x-version", 4), "x-version"),
        (lambda d: d.pop("cross_alpha_min_state"), "missing keys"),
        (lambda d: d.__setitem__("cross_alpha_min_state", "CANDIDATE"), "cross_alpha_min_state"),
        (lambda d: d.__setitem__("cross_alpha_min_state", "RETIRED"), "cross_alpha_min_state"),
        (lambda d: d.__setitem__("cross_alpha_min_state", 2), "cross_alpha_min_state"),
        (lambda d: d["gates"].pop("max_cross_alpha_correlation"), "missing keys"),
        (lambda d: d["gates"].__setitem__("max_cross_alpha_correlation", 1.5), "[0, 1]"),
        (lambda d: d["gates"].__setitem__("max_cross_alpha_correlation", -0.1), "[0, 1]"),
        (lambda d: d.pop("net_pnl_ci_gate"), "missing keys"),
        (lambda d: d.__setitem__("net_pnl_ci_gate", "optional"), "net_pnl_ci_gate"),
        (lambda d: d["gates"].pop("net_pnl_ci_level"), "missing keys"),
        (lambda d: d["gates"].__setitem__("net_pnl_ci_level", 1.0), "(0, 1)"),
        (lambda d: d["gates"].__setitem__("net_pnl_ci_level", 0.0), "(0, 1)"),
        (lambda d: d["gates"].pop("min_net_pnl_ci_low"), "missing keys"),
        (lambda d: d.pop("tstat_threshold"), "missing keys"),
        (lambda d: d.__setitem__("tstat_threshold", "bonferroni"), "tstat_threshold"),
        (lambda d: d.__setitem__("tstat_threshold", 3.0), "tstat_threshold"),
        (lambda d: d["gates"].pop("min_oos_ic"), "missing keys"),
        (lambda d: d["gates"].__setitem__("extra", 1.0), "unknown keys"),
        (lambda d: d["gates"].__setitem__("min_oos_ic", "0.01"), "expected a number"),
        (lambda d: d["gates"].__setitem__("min_folds", 0), "< 1"),
        (lambda d: d["gates"].__setitem__("min_folds", 2.0), "expected an integer"),
        (lambda d: d["gates"].__setitem__("min_fold_sign_consistency", 1.5), "[0, 1]"),
        (lambda d: d["gates"].__setitem__("ic_rank_gap_eps", 0.0), "> 0"),
        (lambda d: d["demotion"].__setitem__("max_consecutive_failures", 0), "< 1"),
        (lambda d: d.__setitem__("policy", ""), "policy"),
    ],
)
def test_config_fail_fast(tmp_path, mutate, message):
    with open(ROOT / "configs" / "strategies" / "lifecycle.json") as fh:
        doc = json.load(fh)
    mutate(doc)
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=re.escape(message)):
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
    # a live block written for v1.4.0 names no retirement rule: rejected, not
    # read under the new default
    v140_live = {
        "watch_ic_gate": 0.0,
        "reactivate_ic_gate": 0.005,
        "retire_breach_evals": 6,
        "reactivate_evals": 3,
    }
    (tmp_path / "strategies.json").write_text(json.dumps({"adaptive": {"lifecycle": v140_live}}))
    with pytest.raises(ValueError, match="breach_rule"):
        load_policy_config(path, tmp_path / "strategies.json")
    # ... and naming the legacy rule keeps the rule it was written for
    (tmp_path / "strategies.json").write_text(
        json.dumps({"adaptive": {"lifecycle": {**v140_live, "breach_rule": "consecutive"}}})
    )
    loaded = load_policy_config(path, tmp_path / "strategies.json")
    assert loaded.live == LifecycleConfig.legacy(0.0, 0.005, 6, 3)
    assert loaded.tstat_threshold == "ledger"


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_evidence_rejects_nan_and_wrong_types():
    with pytest.raises(ValueError, match="non-finite"):
        ValidationEvidence(
            holdout_ic=float("nan"), research_ic=0.0, replay_hash_match=True, parity=True
        )
    with pytest.raises(ValueError, match="expected a bool"):
        ValidationEvidence(holdout_ic=0.0, research_ic=0.0, replay_hash_match=1, parity=True)
    with pytest.raises(ValueError, match="expected an integer"):
        PaperEvidence(
            n_sessions=2.0,
            realized_ic=0.0,
            research_ic=0.0,
            net_pnl=0.0,
            n_kill_events=0,
            tracking_error=0.0,
        )
    with pytest.raises(ValueError, match=">= 0"):
        PaperEvidence(
            n_sessions=2,
            realized_ic=0.0,
            research_ic=0.0,
            net_pnl=0.0,
            n_kill_events=0,
            tracking_error=-1.0,
        )
    with pytest.raises(ValueError, match="capacity_usd"):
        _ev(capacity_usd=-1.0)
    with pytest.raises(ValueError, match="ExperimentResult"):
        _ev(research={"ic": 1.0})
    with pytest.raises(ValueError, match="evidence.live"):
        _ev(live=GOOD_PAPER)
    # live.new_fraction (v1.5.0) is required and lies in (0, 1]
    with pytest.raises(TypeError, match="new_fraction"):
        LiveEvidence(rolling_ic=0.0, n_buckets=8, eval_index=1, informative=True)
    for bad in (0.0, -0.125, 1.5):
        with pytest.raises(ValueError, match=r"new_fraction must be in \(0, 1\]"):
            _live(0.0, new_fraction=bad)
    with pytest.raises(ValueError, match="non-finite"):
        _live(0.0, new_fraction=float("nan"))
    with pytest.raises(ValueError, match="expected a number"):
        _live(0.0, new_fraction=True)
    assert _live(0.0, new_fraction=1).new_fraction == 1.0
    # evidence.significance_threshold (v1.5.0): None, or a finite number > 0
    for bad in (0.0, -3.0):
        with pytest.raises(ValueError, match="significance_threshold must be > 0"):
            _ev(significance_threshold=bad)
    with pytest.raises(ValueError, match="non-finite"):
        _ev(significance_threshold=float("inf"))
    with pytest.raises(ValueError, match="expected a number"):
        _ev(significance_threshold="3.5")
    assert _ev().significance_threshold is None


def test_evidence_round_trip_and_strict_keys():
    ev = Evidence(
        research=golden_research("LC01"),
        capacity_usd=5e6,
        validation=GOOD_VALIDATION,
        paper=GOOD_PAPER,
        live=_live(None),
        significance_threshold=THRESHOLD,
    )
    doc = json.loads(json.dumps(ev.to_dict()))
    assert Evidence.from_dict(doc) == ev
    assert Evidence.from_dict(Evidence.empty().to_dict()) == Evidence.empty()
    # significance_threshold is ALWAYS serialised (null allowed) and required
    assert doc["significance_threshold"] == THRESHOLD
    assert list(Evidence.empty().to_dict()) == [
        "research",
        "capacity_usd",
        "significance_threshold",
        "pnl_bootstrap",
        "cross_alpha",
        "validation",
        "paper",
        "live",
    ]
    # so are the two blocks of the v1.5.0 gates
    for key in ("pnl_bootstrap", "cross_alpha"):
        assert Evidence.empty().to_dict()[key] is None
        with pytest.raises(ValueError, match=rf"missing keys \['{key}'\]"):
            Evidence.from_dict({k: v for k, v in doc.items() if k != key})
    assert Evidence.empty().to_dict()["significance_threshold"] is None
    unjudged = {**doc, "significance_threshold": None}
    assert Evidence.from_dict(unjudged).significance_threshold is None
    with pytest.raises(ValueError, match=r"missing keys \['significance_threshold'\]"):
        Evidence.from_dict({k: v for k, v in doc.items() if k != "significance_threshold"})
    # live.new_fraction is serialised and required
    assert doc["live"]["new_fraction"] == BLOCK_FRACTION
    v140_live = {k: v for k, v in doc["live"].items() if k != "new_fraction"}
    with pytest.raises(ValueError, match=r"live: missing keys \['new_fraction'\]"):
        Evidence.from_dict({**doc, "live": v140_live})
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
        (
            "leakage_clean",
            golden_research("LCX", leakage_passed=False, verdict=Verdict.REJECT),
            False,
            None,
        ),
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


@pytest.mark.parametrize("policy", ["default", "legacy"])
def test_research_gates_positive_negative(config, policy):
    """The gate table against the configured thresholds.  Under the legacy
    fixed policy the evidence carries no significance threshold (the v1.4.0
    form of this test); under the default policy it carries one equal to the
    configured floor, so ``max(floor, floor)`` is the same threshold and the
    same table holds."""
    cfg = _policy(config, policy)
    carried = cfg.gates.min_nw_tstat if policy == "default" else None
    gates = build_gates(cfg)
    for name, result, expect_pass, expect_value in _research_gate_cases(cfg):
        out = gates[name].evaluate("LCX", _ev(research=result, significance_threshold=carried))
        assert out.passed is expect_pass, (name, result.to_dict())
        if expect_value is None:
            assert out.value is None and out.threshold is None
        else:
            assert out.value == pytest.approx(expect_value, abs=1e-12)
            assert out.threshold == gates[name].threshold


def test_significance_gate_reads_the_evidence_threshold(config, legacy):
    """Default policy: threshold = max(min_nw_tstat, evidence threshold),
    inclusive; evidence without one FAILS with threshold null (value kept).
    Legacy fixed policy: min_nw_tstat alone, the evidence field is not read."""
    gate = build_gates(config)["statistical_significance"]
    floor = config.gates.min_nw_tstat
    assert floor == 3.0 and gate.threshold == floor
    at_floor = golden_research("LCX", t_stat=3.0)

    def judge(result, threshold, cfg_gate=gate):
        out = cfg_gate.evaluate("LCX", _ev(research=result, significance_threshold=threshold))
        validate_typed(out)
        return (out.passed, out.value, out.threshold)

    # a threshold equal to the floor: inclusive pass
    assert judge(at_floor, 3.0) == (True, 3.0, 3.0)
    # a threshold below the floor is replaced by the floor: max(3.0, 2.0) = 3.0
    assert judge(at_floor, 2.0) == (True, 3.0, 3.0)
    assert judge(golden_research("LCX", t_stat=2.99), 2.0) == (False, 2.99, 3.0)
    # a threshold above the floor applies: t 3.0 < 3.5 fails, t 4.0 passes,
    # t 3.5 passes (inclusive)
    assert judge(at_floor, 3.5) == (False, 3.0, 3.5)
    assert judge(golden_research("LCX", t_stat=4.0), 3.5) == (True, 4.0, 3.5)
    assert judge(golden_research("LCX", t_stat=3.5), 3.5) == (True, 3.5, 3.5)
    # no threshold in the evidence: fails whatever the t, threshold null
    assert judge(at_floor, None) == (False, 3.0, None)
    assert judge(golden_research("LCX", t_stat=30.0), None) == (False, 30.0, None)
    # no research block: value null; the threshold is what the evidence gives
    out = gate.evaluate("LCX", Evidence.empty())
    assert (out.passed, out.value, out.threshold) == (False, None, None)
    out = gate.evaluate("LCX", _ev(significance_threshold=4.5))
    assert (out.passed, out.value, out.threshold) == (False, None, 4.5)
    # the legacy fixed policy reads the config alone
    fixed = build_gates(legacy)["statistical_significance"]
    assert judge(at_floor, None, fixed) == (True, 3.0, 3.0)
    assert judge(at_floor, 8.0, fixed) == (True, 3.0, 3.0)
    assert judge(golden_research("LCX", t_stat=2.99), None, fixed) == (False, 2.99, 3.0)
    out = fixed.evaluate("LCX", Evidence.empty())
    assert (out.passed, out.value, out.threshold) == (False, None, 3.0)
    # no other gate reads the evidence threshold
    ok = golden_research("LCX")
    for name, other in build_gates(config).items():
        if name == "statistical_significance":
            continue
        bare = other.evaluate("LCX", _ev(research=ok, capacity_usd=5e6))
        judged = other.evaluate(
            "LCX", _ev(research=ok, capacity_usd=5e6, significance_threshold=9.0)
        )
        assert bare == judged, name


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
    far = _ev(
        validation=ValidationEvidence(
            holdout_ic=0.0, research_ic=0.02, replay_hash_match=False, parity=False
        )
    )
    assert not gates["holdout_ic_tracks_research"].evaluate("X", far).passed
    assert not gates["replay_reproducible"].evaluate("X", far).passed
    assert not gates["cross_language_parity"].evaluate("X", far).passed
    assert gates["replay_reproducible"].evaluate("X", v).passed
    assert gates["cross_language_parity"].evaluate("X", v).passed

    p = _ev(paper=GOOD_PAPER)
    for name in ("paper_min_sessions", "paper_ic_tracking", "paper_net_pnl", "no_kill_events"):
        assert gates[name].evaluate("X", p).passed, name
    bad = _ev(
        paper=PaperEvidence(
            n_sessions=4,
            realized_ic=0.0,
            research_ic=0.02,
            net_pnl=-0.5,
            n_kill_events=1,
            tracking_error=0.0,
        )
    )
    for name in ("paper_min_sessions", "paper_ic_tracking", "paper_net_pnl", "no_kill_events"):
        assert not gates[name].evaluate("X", bad).passed, name
    zero = _ev(
        paper=PaperEvidence(
            n_sessions=5,
            realized_ic=0.01,
            research_ic=0.02,
            net_pnl=0.0,
            n_kill_events=0,
            tracking_error=0.0,
        )
    )
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
    manual_retire = {
        e.from_state
        for e in ALLOWED_TRANSITIONS
        if e.kind is EdgeKind.MANUAL and e.to_state is S.RETIRED
    }
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
    assert states == [
        (S.RESEARCH, S.CANDIDATE),
        (S.CANDIDATE, S.VALIDATING),
        (S.VALIDATING, S.PAPER),
        (S.PAPER, S.ACTIVE),
    ]
    for t, edge_state in zip(
        m.transitions, (S.RESEARCH, S.CANDIDATE, S.VALIDATING, S.PAPER), strict=False
    ):
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
    assert ev.outcome == Outcome.HOLD and ev.failed_gates == [
        "ledger_entry_exists",
        "leakage_clean",
    ]
    assert ev.gates["ledger_entry_exists"].value is None
    assert m.advance("LCX", T0 + 2, _ev(research=golden_research("LCX", n_ledger=0))) is None
    assert m.evaluations[-1].failed_gates == ["ledger_entry_exists"]
    assert m.state("LCX") is S.RESEARCH and m.record("LCX").consecutive_failures == 0


def test_candidate_leakage_demotion_and_hold(config):
    m = _machine(config)
    _to_state(m, "LCX", S.CANDIDATE)
    leaking = golden_research("LCX", leakage_passed=False, verdict=Verdict.REJECT)
    tr = m.advance(
        "LCX", T0 + 10, _ev(research=leaking, capacity_usd=5e6, significance_threshold=THRESHOLD)
    )
    assert tr is not None and (tr.from_state, tr.to_state) == (S.CANDIDATE, S.RESEARCH)
    assert tr.actor is Actor.SYSTEM and "leakage" in tr.reason
    assert not tr.gates["leakage_clean"].passed
    assert list(tr.gates) == list(PROMOTION_EDGES[S.CANDIDATE].gates)
    # a non-leakage failure at CANDIDATE holds, no counter
    m2 = _machine(config)
    _to_state(m2, "LCX", S.CANDIDATE)
    weak = golden_research("LCX", t_stat=1.0)
    for k in range(5):
        evidence = _ev(research=weak, capacity_usd=5e6, significance_threshold=THRESHOLD)
        assert m2.advance("LCX", T0 + 10 + k, evidence) is None
        assert m2.evaluations[-1].failed_gates == ["statistical_significance"]
        assert m2.evaluations[-1].gates["statistical_significance"] == GateResult(
            passed=False, value=1.0, threshold=THRESHOLD
        )
    assert m2.state("LCX") is S.CANDIDATE and m2.record("LCX").consecutive_failures == 0


def test_candidate_capacity_missing_fails_capacity_gate_only(config):
    m = _machine(config)
    _to_state(m, "LCX", S.CANDIDATE)
    evidence = _ev(research=golden_research("LCX"), significance_threshold=THRESHOLD)
    assert m.advance("LCX", T0 + 10, evidence) is None
    assert m.evaluations[-1].failed_gates == ["capacity"]


def test_legacy_candidate_capacity_missing_fails_capacity_gate_only(legacy):
    """The v1.4.0 form: evidence with no significance threshold."""
    m = _machine(legacy)
    _to_state(m, "LCX", S.CANDIDATE)
    assert m.advance("LCX", T0 + 10, _ev(research=golden_research("LCX"))) is None
    assert m.evaluations[-1].failed_gates == ["capacity"]


def test_candidate_without_a_significance_threshold_is_not_promoted(config, legacy):
    """Default policy: research evidence nobody recorded a multiple-testing
    threshold for holds at CANDIDATE (the gate fails, threshold null), and a
    threshold above the t holds too; the same evidence with a threshold the
    t clears is promoted.  The legacy policy promotes the bare evidence."""
    good = golden_research("LCX")  # t 4.0
    m = _machine(config)
    _to_state(m, "LCX", S.CANDIDATE)
    assert m.advance("LCX", T0 + 10, _ev(research=good, capacity_usd=5e6)) is None
    held = m.evaluations[-1]
    assert held.outcome == Outcome.HOLD and held.failed_gates == ["statistical_significance"]
    assert held.gates["statistical_significance"] == GateResult(
        passed=False, value=4.0, threshold=None
    )
    validate_typed(held.gates["statistical_significance"])
    above = _ev(research=good, capacity_usd=5e6, significance_threshold=4.5)
    assert m.advance("LCX", T0 + 11, above) is None
    assert m.evaluations[-1].gates["statistical_significance"] == GateResult(
        passed=False, value=4.0, threshold=4.5
    )
    assert m.state("LCX") is S.CANDIDATE and m.record("LCX").consecutive_failures == 0
    cleared = _ev(research=good, capacity_usd=5e6, significance_threshold=THRESHOLD)
    tr = m.advance("LCX", T0 + 12, cleared)
    assert tr is not None and tr.to_state is S.VALIDATING
    assert tr.gates["statistical_significance"] == GateResult(
        passed=True, value=4.0, threshold=THRESHOLD
    )
    validate_typed(tr)

    old = _machine(legacy)
    _to_state(old, "LCX", S.CANDIDATE)
    tr = old.advance("LCX", T0 + 10, _ev(research=good, capacity_usd=5e6))
    assert tr is not None and tr.to_state is S.VALIDATING
    assert tr.gates["statistical_significance"] == GateResult(passed=True, value=4.0, threshold=3.0)


@pytest.mark.parametrize("state", [S.VALIDATING, S.PAPER])
def test_persistent_failure_demotes_to_candidate(config, state):
    m = _machine(config)
    ts = _to_state(m, "LCX", state)
    bad = (
        _ev(validation=ValidationEvidence(0.0, 0.02, False, False))
        if state is S.VALIDATING
        else _ev(paper=PaperEvidence(1, 0.0, 0.02, -1.0, 2, 0.0))
    )
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
    bad = (
        _ev(validation=ValidationEvidence(0.0, 0.02, True, True))
        if state is S.VALIDATING
        else _ev(paper=PaperEvidence(1, 0.015, 0.02, 1.0, 0, 0.0))
    )
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
    """The wrapped machine reproduces LifecycleTracker state-for-state — and
    the CUSUM statistic bit-for-bit — on the golden adaptive ic_path, up to
    and including the (terminal) retirement."""
    with open(ROOT / "tests" / "golden" / "expected_adaptive.json") as fh:
        lc = json.load(fh)["lifecycle"]
    live = LifecycleConfig.from_config(lc["config"])
    assert live == config.live and live.breach_rule == "cusum"
    fraction = lc["new_fraction"]
    assert fraction == BLOCK_FRACTION
    tracker = LifecycleTracker("EQ01", live, policy="p")
    m = _machine(config, "EQ01")
    ts = _to_state(m, "EQ01", S.ACTIVE)
    retired_at = None
    for k, (ic, informative) in enumerate(zip(lc["ic_path"], lc["ic_informative"], strict=False)):
        ts += lc["ts_step_ns"]
        expected = tracker.update(ts, ic, informative, new_fraction=fraction)
        m.advance("EQ01", ts, _ev(live=_live(ic, informative, eval_index=k, new_fraction=fraction)))
        assert m.state("EQ01").name == expected == lc["expected_states"][k]
        rec = m.record("EQ01")
        assert (rec.breach_count, rec.recovery_count) == (
            tracker.breach_count,
            tracker.recovery_count,
        )
        assert rec.breach_count == 0, "the CUSUM rule does not count breaches"
        assert rec.cusum == tracker.cusum == lc["expected_cusum"][k]
        if expected == "RETIRED":
            retired_at = k
            break
    assert retired_at == 28 and m.state("EQ01") is S.RETIRED
    assert "persistent breach: CUSUM" in m.transitions[-1].reason


def test_legacy_live_edges_match_the_adaptive_tracker(legacy):
    """The same delegation under the consecutive rule, on a hand-written
    path: breach -> WATCH (1), neutral (reset), two breaches, three
    recoveries -> ACTIVE, then six consecutive breaches -> RETIRED."""
    path = [0.01, -0.01, 0.002, -0.01, -0.01, 0.005, 0.005, 0.005] + [-0.001] * 6
    states = ["ACTIVE"] + ["WATCH"] * 6 + ["ACTIVE"] + ["WATCH"] * 5 + ["RETIRED"]
    breaches = [0, 1, 0, 1, 2, 0, 0, 0, 1, 2, 3, 4, 5, 0]
    tracker = LifecycleTracker("EQ01", legacy.live, policy="p")
    m = _machine(legacy, "EQ01")
    ts = _to_state(m, "EQ01", S.ACTIVE)
    for k, ic in enumerate(path):
        ts += STEP
        expected = tracker.update(ts, ic, True, new_fraction=BLOCK_FRACTION)
        m.advance("EQ01", ts, _ev(live=_live(ic, eval_index=k)))
        assert m.state("EQ01").name == expected == states[k]
        rec = m.record("EQ01")
        assert (rec.breach_count, rec.recovery_count) == (
            tracker.breach_count,
            tracker.recovery_count,
        )
        assert rec.breach_count == breaches[k]
        assert rec.cusum == tracker.cusum == 0.0
    assert m.transitions[-1].reason == (
        "persistent breach: 6 consecutive evals below watch gate 0.0"
    )


def test_legacy_live_breach_watch_retire_and_wrapping(legacy):
    """The consecutive-breach rule (the default up to v1.4.0), by name."""
    m = _machine(legacy)
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
    assert tr.gates == {
        "rolling_ic": GateResult(passed=False, value=-0.01, threshold=legacy.live.watch_ic_gate)
    }
    assert tr.policy == legacy.policy and tr.actor is Actor.SYSTEM
    assert "watch gate" in tr.reason
    assert m.record("LCX").breach_count == 1, "the entering breach counts"
    n = legacy.live.retire_breach_evals
    for k in range(2, n):
        assert m.advance("LCX", ts + 5 + k, _ev(live=_live(-0.01))) is None
        assert m.record("LCX").breach_count == k
    tr = m.advance("LCX", ts + 5 + n, _ev(live=_live(-0.01)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.RETIRED)
    assert "persistent breach" in tr.reason
    validate_typed(tr)
    assert m.record("LCX").breach_count == 0
    assert m.record("LCX").cusum == 0.0, "the consecutive rule never moves the statistic"


def test_legacy_live_neutral_zone_and_reactivation(legacy):
    """Consecutive rule: the neutral zone resets both counters."""
    m = _machine(legacy)
    ts = _to_state(m, "LCX", S.WATCH)
    assert m.advance("LCX", ts + 1, _ev(live=_live(-0.01))) is None
    assert m.record("LCX").breach_count == 2
    assert m.advance("LCX", ts + 2, _ev(live=_live(0.002))) is None
    assert (m.record("LCX").breach_count, m.record("LCX").recovery_count) == (0, 0)
    n = legacy.live.reactivate_evals
    for k in range(1, n):
        assert m.advance("LCX", ts + 2 + k, _ev(live=_live(0.005))) is None
        assert m.record("LCX").recovery_count == k
    tr = m.advance("LCX", ts + 2 + n, _ev(live=_live(0.005)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.ACTIVE)
    assert tr.gates["rolling_ic"] == GateResult(
        passed=True, value=0.005, threshold=legacy.live.reactivate_ic_gate
    )


def test_live_cusum_breach_watch_retire_and_wrapping(config):
    """The default retirement rule.  With gate 0.0, k 0.0025, h 0.01 and
    new_fraction 0.125, a reading of -0.01 adds

        0.125 * (0.0 - (-0.01) - 0.0025) = 0.0009375

    so S after n such breaches is n * 0.0009375: 0.009375 after ten (under
    h) and 0.0103125 after eleven — the eleventh breach retires, where the
    consecutive rule retired at the sixth."""
    live = config.live
    m = _machine(config)
    ts = _to_state(m, "LCX", S.ACTIVE)
    # healthy: 0.125 * (0.0 - 0.01 - 0.0025) < 0, S clamps at 0
    assert m.advance("LCX", ts + 1, _ev(live=_live(0.01))) is None
    assert m.evaluations[-1].outcome == Outcome.HOLD and m.record("LCX").cusum == 0.0
    assert m.advance("LCX", ts + 2, _ev(live=_live(None))) is None
    assert m.evaluations[-1].outcome == Outcome.NO_EVIDENCE
    assert m.advance("LCX", ts + 3, _ev(live=_live(-0.5, informative=False))) is None
    assert m.evaluations[-1].outcome == Outcome.NO_EVIDENCE
    assert m.advance("LCX", ts + 4, Evidence.empty()) is None
    assert m.state("LCX") is S.ACTIVE and m.record("LCX").cusum == 0.0, "silence moves nothing"
    tr = m.advance("LCX", ts + 5, _ev(live=_live(-0.01)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.ACTIVE, S.WATCH)
    assert tr.gates == {
        "rolling_ic": GateResult(passed=False, value=-0.01, threshold=live.watch_ic_gate)
    }
    assert tr.policy == config.policy and tr.actor is Actor.SYSTEM
    assert tr.reason == "rolling_ic -0.010000 < watch gate 0.0"
    rec = m.record("LCX")
    assert rec.breach_count == 0, "the CUSUM rule keeps no breach count"
    assert rec.cusum == 0.0009375, "S survives the entry into WATCH"
    stat = rec.cusum
    for n in range(2, 11):
        assert m.advance("LCX", ts + 5 + n, _ev(live=_live(-0.01))) is None, n
        stat = _cusum_step(stat, -0.01, live, BLOCK_FRACTION)
        assert m.record("LCX").cusum == stat == pytest.approx(n * 0.0009375, abs=1e-15)
        assert m.record("LCX").breach_count == 0
        assert m.evaluations[-1].outcome == Outcome.HOLD
        assert m.evaluations[-1].gates["rolling_ic"] == GateResult(
            passed=False, value=-0.01, threshold=live.watch_ic_gate
        )
    assert m.state("LCX") is S.WATCH and stat < live.cusum_h, "ten breaches: 0.009375 < 0.01"
    tr = m.advance("LCX", ts + 5 + 11, _ev(live=_live(-0.01)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.RETIRED)
    assert tr.reason == (
        f"persistent breach: CUSUM {_cusum_step(stat, -0.01, live, BLOCK_FRACTION):.6f} "
        ">= 0.01 (slack 0.0025) below watch gate 0.0"
    )
    assert tr.reason.startswith("persistent breach: CUSUM 0.01031")
    assert tr.gates == {
        "rolling_ic": GateResult(passed=False, value=-0.01, threshold=live.watch_ic_gate)
    }
    validate_typed(tr)
    assert (m.record("LCX").breach_count, m.record("LCX").cusum) == (0, 0.0)


def test_live_cusum_ignores_breaches_inside_the_slack(config):
    """Readings just under the watch gate but inside the slack k add no
    evidence: 0.125 * (0.0 - (-0.001) - 0.0025) < 0.  Eight of them never
    retire the alpha (the consecutive rule retires at the sixth)."""
    m = _machine(config)
    ts = _to_state(m, "LCX", S.ACTIVE)
    tr = m.advance("LCX", ts + 1, _ev(live=_live(-0.001)))
    assert tr is not None and tr.to_state is S.WATCH
    for k in range(2, 9):
        assert m.advance("LCX", ts + k, _ev(live=_live(-0.001))) is None
        assert m.state("LCX") is S.WATCH
        assert (m.record("LCX").cusum, m.record("LCX").breach_count) == (0.0, 0)


def test_live_cusum_retires_only_on_a_breach_and_never_on_entry(config):
    """A reading of -0.09 puts S at 0.125 * (0.09 - 0.0025) = 0.0109375, over
    h = 0.01, at once: it only enters WATCH.  A reading ABOVE the gate
    (0.004: S - 0.125 * 0.0065 = 0.010125, still over h) does not retire.
    The next breach (-0.003: S + 0.125 * 0.0005 = 0.0101875) does."""
    live = config.live
    m = _machine(config)
    ts = _to_state(m, "LCX", S.ACTIVE)
    tr = m.advance("LCX", ts + 1, _ev(live=_live(-0.09)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.ACTIVE, S.WATCH)
    assert m.record("LCX").cusum == 0.0109375 >= live.cusum_h
    assert m.advance("LCX", ts + 2, _ev(live=_live(0.004))) is None
    assert m.state("LCX") is S.WATCH
    assert m.record("LCX").cusum == pytest.approx(0.010125, abs=1e-15)
    assert m.record("LCX").cusum >= live.cusum_h
    assert m.evaluations[-1].gates["rolling_ic"].passed
    tr = m.advance("LCX", ts + 3, _ev(live=_live(-0.003)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.RETIRED)
    assert tr.reason == (
        "persistent breach: CUSUM 0.010187 >= 0.01 (slack 0.0025) below watch gate 0.0"
    )
    assert tr.gates["rolling_ic"] == GateResult(passed=False, value=-0.003, threshold=0.0)
    assert m.record("LCX").cusum == 0.0
    # a disjoint window (new_fraction 1.0) weighs eight times a block:
    # entry on -0.0105: S = 0.125 * (0.0105 - 0.0025) = 0.001; then
    # 1.0 * (0.0135 - 0.0025) = 0.011 on top gives 0.012 >= 0.01 on the second
    # reading (a block-weighted -0.0135 adds 0.001375 only and holds)
    m2 = _machine(config)
    ts = _to_state(m2, "LCX", S.ACTIVE)
    tr = m2.advance("LCX", ts + 1, _ev(live=_live(-0.0105)))
    assert tr is not None and tr.to_state is S.WATCH
    assert m2.record("LCX").cusum == pytest.approx(0.001, abs=1e-15)
    tr = m2.advance("LCX", ts + 2, _ev(live=_live(-0.0135, new_fraction=1.0)))
    assert tr is not None and tr.to_state is S.RETIRED
    assert tr.reason == (
        "persistent breach: CUSUM 0.012000 >= 0.01 (slack 0.0025) below watch gate 0.0"
    )
    m3 = _machine(config)
    ts = _to_state(m3, "LCX", S.WATCH)
    assert m3.advance("LCX", ts + 1, _ev(live=_live(-0.0135))) is None
    assert m3.record("LCX").cusum == pytest.approx(0.0009375 + 0.001375, abs=1e-15)


def test_live_cusum_neutral_zone_drains_and_reactivation(config):
    """CUSUM rule: the neutral zone resets the recovery count and DRAINS S
    (it does not reset it); three recoveries re-activate and reset S.

    S: 0.0009375 (entry, -0.01) -> 0.001875 (-0.01)
       -> 0.001875 - 0.125 * (0.002 + 0.0025) = 0.0013125 (neutral 0.002)
       -> 0.0013125 - 0.125 * (0.005 + 0.0025) = 0.000375 (recovery 1)
       -> 0 (recovery 2, clamped) -> 0 (recovery 3: ACTIVE)."""
    live = config.live
    m = _machine(config)
    ts = _to_state(m, "LCX", S.WATCH)
    assert m.record("LCX").cusum == 0.0009375
    assert m.advance("LCX", ts + 1, _ev(live=_live(-0.01))) is None
    assert (m.record("LCX").breach_count, m.record("LCX").cusum) == (0, 0.001875)
    assert m.advance("LCX", ts + 2, _ev(live=_live(0.005))) is None
    assert m.record("LCX").recovery_count == 1
    assert m.record("LCX").cusum == pytest.approx(0.0009375, abs=1e-15)
    assert m.advance("LCX", ts + 3, _ev(live=_live(-0.01))) is None
    assert m.record("LCX").recovery_count == 0, "a breach resets the recovery count"
    assert m.record("LCX").cusum == pytest.approx(0.001875, abs=1e-15)
    assert m.advance("LCX", ts + 4, _ev(live=_live(0.005))) is None
    assert m.record("LCX").recovery_count == 1
    assert m.advance("LCX", ts + 5, _ev(live=_live(-0.01))) is None
    stat = m.record("LCX").cusum
    assert stat == pytest.approx(0.001875, abs=1e-15)
    assert m.advance("LCX", ts + 6, _ev(live=_live(0.002))) is None
    stat = _cusum_step(stat, 0.002, live, BLOCK_FRACTION)
    assert m.record("LCX").cusum == stat == pytest.approx(0.0013125, abs=1e-15)
    assert (m.record("LCX").breach_count, m.record("LCX").recovery_count) == (0, 0)
    n = live.reactivate_evals
    drained = [0.000375, 0.0]
    for k in range(1, n):
        assert m.advance("LCX", ts + 6 + k, _ev(live=_live(0.005))) is None
        assert m.record("LCX").recovery_count == k
        stat = _cusum_step(stat, 0.005, live, BLOCK_FRACTION)
        assert m.record("LCX").cusum == stat == pytest.approx(drained[k - 1], abs=1e-15)
    tr = m.advance("LCX", ts + 6 + n, _ev(live=_live(0.005)))
    assert tr is not None and (tr.from_state, tr.to_state) == (S.WATCH, S.ACTIVE)
    assert tr.gates["rolling_ic"] == GateResult(
        passed=True, value=0.005, threshold=live.reactivate_ic_gate
    )
    assert tr.reason == "re-activation: 3 consecutive evals >= reactivate gate 0.005"
    assert (m.record("LCX").recovery_count, m.record("LCX").cusum) == (0, 0.0)


def test_retired_is_terminal_for_system(config):
    m = _machine(config)
    ts = _to_state(m, "LCX", S.CANDIDATE)
    m.retire("LCX", ts + 1, "desk decision")
    for ev in (
        _ev(live=_live(0.5)),
        _ev(research=golden_research("LCX"), capacity_usd=1e9),
        Evidence.empty(),
    ):
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


@pytest.mark.parametrize("policy", ["default", "legacy"])
def test_registry_and_log_round_trip_and_bytes(config, tmp_path, policy):
    cfg = _policy(config, policy)
    log_path = tmp_path / "transitions.jsonl"
    m = _machine(cfg, "LC01", LifecycleTransitionLog(log_path))
    m.register("LC02", T0)
    ts = _to_state(m, "LC01", S.WATCH)
    m.advance("LC02", ts, _ev(research=golden_research("LC02", t_stat=1.0)))
    m.retire("LC02", ts + 1, "manual")
    reg_path = tmp_path / "registry.json"
    m.registry.save(reg_path)
    text = reg_path.read_text()
    assert text.endswith("\n") and text == m.registry.render()
    assert json.loads(text)["x-version"] == 2
    assert all("cusum" in rec for rec in json.loads(text)["alphas"].values())
    loaded = AlphaRegistry.load(reg_path)
    assert loaded.render() == text
    assert loaded.alpha_ids() == ["LC01", "LC02"]
    for aid in loaded.alpha_ids():
        assert loaded.get(aid) == m.registry.get(aid)
    logged = LifecycleTransitionLog(log_path).read_all()
    assert logged == m.transitions
    for line in log_path.read_text().splitlines():
        assert line == json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"))

    # a reloaded registry resumes the live sub-machine exactly
    m2 = AlphaLifecycle(cfg, loaded)
    if policy == "legacy":  # the consecutive rule resumes its breach count
        assert loaded.get("LC01").breach_count == 1
        assert m2.advance("LC01", ts + 5, _ev(live=_live(-0.01))) is None
        assert loaded.get("LC01").breach_count == 2
        assert loaded.get("LC01").cusum == 0.0
    else:  # the CUSUM rule resumes its statistic: 0.0009375 per -0.01 block
        assert (loaded.get("LC01").breach_count, loaded.get("LC01").cusum) == (0, 0.0009375)
        assert m2.advance("LC01", ts + 5, _ev(live=_live(-0.01))) is None
        assert (loaded.get("LC01").breach_count, loaded.get("LC01").cusum) == (0, 0.001875)
        # ... through a second save / load as well
        loaded.save(reg_path)
        again = AlphaRegistry.load(reg_path)
        assert again.get("LC01").cusum == 0.001875
        m3 = AlphaLifecycle(cfg, again)
        assert m3.advance("LC01", ts + 6, _ev(live=_live(-0.01))) is None
        assert again.get("LC01").cusum == pytest.approx(0.0028125, abs=1e-15)
        assert again.get("LC01").cusum == _cusum_step(0.001875, -0.01, cfg.live, BLOCK_FRACTION)


def test_registry_rejects_bad_documents(tmp_path):
    reg = AlphaRegistry("p")
    reg.add(AlphaRecord.new("A1", T0))
    doc = reg.to_dict()
    assert doc["x-version"] == 2 and doc["alphas"]["A1"]["cusum"] == 0.0
    for wrong in (1, 3):  # 1 = a registry written by v1.4.0 (no cusum)
        with pytest.raises(ValueError, match="x-version"):
            AlphaRegistry.from_dict({**doc, "x-version": wrong})
    for bad_cusum in (-0.001, float("nan"), float("inf"), True, "0.0", None):
        bad = json.loads(json.dumps(doc))
        bad["alphas"]["A1"]["cusum"] = bad_cusum
        with pytest.raises(ValueError, match="cusum"):
            AlphaRegistry.from_dict(bad)
    bad = json.loads(json.dumps(doc))
    del bad["alphas"]["A1"]["cusum"]
    with pytest.raises(KeyError, match="cusum"):
        AlphaRegistry.from_dict(bad)
    kept = json.loads(json.dumps(doc))
    kept["alphas"]["A1"]["cusum"] = 0.00625
    assert AlphaRegistry.from_dict(kept).get("A1").cusum == 0.00625
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


#: PROMOTE gate of the report (``promote_gates`` key) -> lifecycle gate.
_REPORT_GATE = {
    "leakage": "leakage_clean",
    "ic": "oos_ic",
    "significance": "statistical_significance",
    "fold_consistency": "fold_consistency",
    "folds": "fold_count",
    "hypothesis": "hypothesis_sign",
    "cost": "net_pnl_after_costs",
}


def test_bootstrap_failed_gates_agree_with_report_verdicts(bootstrap):
    """ITERATE alphas fail exactly the PROMOTE gates the report says they fail;
    REJECT alphas (all leakage-clean here) fail the ITERATE thresholds.

    Since v1.5.0 the significance gate compares the report's ``gate_tstat``
    (the pooled-slope HAC t its PROMOTE gate read) with the threshold the
    report was judged at — the ledger's Bonferroni |t|, never below the
    configured 3.0 floor — carried by the evidence."""
    ledger = load_ledger_entries(ROOT)
    for row in bootstrap.rows:
        rep = load_report(ROOT, row.alpha_id)
        ev = bootstrap.registry.get(row.alpha_id).last_evaluation
        assert ev is not None and ev.state is S.CANDIDATE
        expected_failed = []
        gate_ic, gate_t = rep["gate_ic"], rep["gate_tstat"]
        judged_at = significance_threshold_from_report(rep)
        assert judged_at == rep["gates"]["min_nw_tstat"] == rep["ledger_t_threshold"]
        assert judged_at == ExperimentLedger.bonferroni_t_threshold_at(rep["ledger_looks"])
        assert rep["ledger_looks"] == ledger[row.alpha_id]["gate_looks"]
        t_threshold = max(VALIDATE_GATES["min_nw_tstat"], judged_at)
        if not rep["leakage"]["passed"]:
            expected_failed.append("leakage_clean")
        if gate_ic < VALIDATE_GATES["min_oos_ic"]:
            expected_failed.append("oos_ic")
        if gate_t < t_threshold:
            expected_failed.append("statistical_significance")
        if rep["fold_sign_consistency"] < VALIDATE_GATES["min_fold_sign_consistency"]:
            expected_failed.append("fold_consistency")
        if rep["n_folds_run"] < VALIDATE_GATES["min_nondegenerate_folds"]:
            expected_failed.append("fold_count")
        if not rep["hypothesis_confirmed"]:
            expected_failed.append("hypothesis_sign")
        if not rep["net_pnl_1x_cost"] > 0.0:
            expected_failed.append("net_pnl_after_costs")
        # gates the report has no row for: capacity and stability, and the
        # two the lifecycle added in v1.5.0 (tested on their own below)
        not_in_report = ("capacity", "stability", "net_pnl_bootstrap_ci", "cross_alpha_correlation")
        report_gates = [g for g in ev.failed_gates if g not in not_in_report]
        assert report_gates == expected_failed, row.alpha_id
        # ... which is exactly the set of PROMOTE gates the report itself
        # records as failed
        assert set(rep["promote_gates"]) == set(_REPORT_GATE)
        assert set(report_gates) == {
            _REPORT_GATE[name] for name, passed in rep["promote_gates"].items() if not passed
        }, row.alpha_id
        sig = ev.gates["statistical_significance"]
        assert (sig.value, sig.threshold) == (gate_t, t_threshold), row.alpha_id
        assert row.verdict == rep["verdict"]
        if rep["verdict"] == "ITERATE":
            assert ev.gates["oos_ic"].value >= VALIDATE_GATES["iterate_min_ic"]
            assert ev.gates["statistical_significance"].value >= VALIDATE_GATES["iterate_min_tstat"]
        else:
            assert (
                ev.gates["oos_ic"].value < VALIDATE_GATES["iterate_min_ic"]
                or ev.gates["statistical_significance"].value < VALIDATE_GATES["iterate_min_tstat"]
            )
        assert (
            ev.gates["ledger_entry_exists"].value == float(ledger[row.alpha_id]["n"])
            if "ledger_entry_exists" in ev.gates
            else True
        )
    by_id = {r.alpha_id: r.failed_gates for r in bootstrap.rows}
    # Hand cross-checks against REPORT.md. Updated 2026-09-20: several gates
    # moved when fold_sign_consistency stopped scoring the beta-SIGNED signal
    # (an alpha backwards in every fold used to report 1.00 consistency and
    # pass this gate) and when the walk-forward stopped training inside its
    # own declared holdout. EQ07, FX01 and FX03 fail fold_consistency, which
    # they previously passed falsely.
    # Updated 2026-10-03 (v1.4.0 dataset: equity flow to the close; the FX
    # data and therefore the FX rows are unchanged). On the full session the
    # equity ICs are lower: EQ01 and EQ05 keep their ITERATE verdict but no
    # longer clear the PROMOTE significance gate, EQ05 also falls under the
    # 0.01 IC gate, and both now fail the crossed/uncrossed stability gate.
    # Updated 2026-10-04 (v1.5.0 default methods; same dataset). The gate t is
    # the pooled-slope HAC t against the ledger's Bonferroni threshold (4.37
    # at 3936 looks) instead of the bucket t against 3.0: FX01 (gate t 2.26)
    # no longer clears significance, EQ03 (5.16) still does. Under the
    # cost-aware position policy no alpha clears its round-trip cost, so every
    # alpha fails the cost gate and — the capacity now being the breakeven
    # capacity — the capacity gate with it: EQ03 fails those two alone.
    # Updated with the v1.5.0 gates: every alpha also fails
    # net_pnl_bootstrap_ci (no trade, or a negative lower bound); the
    # correlation gate passes vacuously for all 24 (nobody is at VALIDATING).
    assert by_id["EQ01"] == (
        "statistical_significance",
        "net_pnl_after_costs",
        "net_pnl_bootstrap_ci",
        "capacity",
        "stability",
    )
    assert by_id["EQ03"] == ("net_pnl_after_costs", "net_pnl_bootstrap_ci", "capacity")
    assert by_id["EQ05"] == (
        "oos_ic",
        "statistical_significance",
        "net_pnl_after_costs",
        "net_pnl_bootstrap_ci",
        "capacity",
        "stability",
    )
    assert by_id["FX01"] == (
        "statistical_significance",
        "fold_consistency",
        "net_pnl_after_costs",
        "net_pnl_bootstrap_ci",
        "capacity",
    )
    assert by_id["EQ07"] == (
        "oos_ic",
        "statistical_significance",
        "fold_consistency",
        "hypothesis_sign",
        "net_pnl_after_costs",
        "net_pnl_bootstrap_ci",
        "capacity",
        "stability",
    )
    assert set(by_id["FX03"]) >= {"statistical_significance", "hypothesis_sign", "fold_consistency"}


def test_pipeline_entry_selection_is_per_dataset():
    """The ledger keeps one promotion_pipeline entry per alpha AND dataset;
    the evidence is the entry of the dataset alpha_params.json names."""
    from iap.lifecycle.bootstrap import select_pipeline_entries

    old, new = "a" * 64, "b" * 64
    entries = [
        {"alpha_id": "EQ01", "kind": "promotion_pipeline", "key": "k-old", "dataset_version": old},
        {"alpha_id": "EQ01", "kind": "promotion_pipeline", "key": "k-new", "dataset_version": new},
        {"alpha_id": "EQ02", "kind": "promotion_pipeline", "key": "k-unstamped"},
        {"alpha_id": "EQ01", "kind": "adaptive_deployment", "key": "other-kind"},
    ]
    assert select_pipeline_entries(entries, new)["EQ01"]["key"] == "k-new"
    assert select_pipeline_entries(entries, old)["EQ01"]["key"] == "k-old"
    # an entry written before the dataset stamp existed still backs its alpha
    assert select_pipeline_entries(entries, new)["EQ02"]["key"] == "k-unstamped"
    # a dataset the ledger has never seen: nothing stamped matches
    assert set(select_pipeline_entries(entries, "c" * 64)) == {"EQ02"}
    with pytest.raises(ValueError, match="duplicate promotion_pipeline entry for EQ01"):
        select_pipeline_entries(entries, None)
    with pytest.raises(ValueError, match="duplicate"):
        select_pipeline_entries(entries + [dict(entries[1], key="again")], new)


def test_pipeline_entry_selection_prefers_the_default_method_bundle():
    """Since v1.5.0 the ledger also keeps one entry per method bundle: the
    evidence is the entry recorded under the default bundle; a legacy-methods
    entry (or one that names no bundle) of the same dataset is history and
    backs the alpha only when it has no default-bundle entry."""
    from iap.lifecycle.bootstrap import select_pipeline_entries

    data = "b" * 64

    def entry(alpha_id, key, methods=None):
        config = {} if methods is None else {"methods": methods}
        return {
            "alpha_id": alpha_id,
            "kind": "promotion_pipeline",
            "key": key,
            "dataset_version": data,
            "config": config,
        }

    entries = [
        entry("EQ01", "k-unnamed"),  # written before v1.5.0: the legacy bundle
        entry("EQ01", "k-v2", "v2"),
        entry("EQ02", "k-legacy", "legacy_v1"),
    ]
    picked = select_pipeline_entries(entries, data)
    assert picked["EQ01"]["key"] == "k-v2"
    assert picked["EQ02"]["key"] == "k-legacy"  # nothing under the default yet
    assert select_pipeline_entries(entries, data, methods="legacy_v1")["EQ01"]["key"] == "k-unnamed"
    with pytest.raises(ValueError, match="duplicate promotion_pipeline entry for EQ01"):
        select_pipeline_entries(entries + [entry("EQ01", "k-v2-again", "v2")], data)


def test_committed_ledger_backs_every_alpha_on_the_current_dataset():
    params = load_params_document(ROOT)
    ledger = load_ledger_entries(ROOT)
    assert sorted(ledger) == sorted(ALPHA_IDS)
    assert {e["dataset_version"] for e in ledger.values()} == {params["data_version"]}
    assert ledger == load_ledger_entries(ROOT, params["data_version"])
    # ... under the default method bundle, all judged at one look count
    assert {e["config"]["methods"] for e in ledger.values()} == {"v2"}
    assert len({e["gate_looks"] for e in ledger.values()}) == 1


def test_bootstrap_research_mapping(bootstrap):
    rep = load_report(ROOT, "EQ03")
    ledger = load_ledger_entries(ROOT)
    params = load_params_document(ROOT)
    result, missing = research_evidence("EQ03", rep, ledger["EQ03"], params)
    assert missing == [] and result is not None
    assert result.ic == rep["gate_ic"] == rep["oos_ic_uncrossed"]
    # the t the PROMOTE gate read: the pooled-slope HAC t on uncrossed rows
    assert result.t_stat == rep["gate_tstat"] == rep["nw_tstat_pooled_uncrossed"]
    assert rep["methods"]["significance"] == "pooled_slope"
    # a report written before v1.5.0 has no gate_tstat: what ITS gate read
    v140 = {k: v for k, v in rep.items() if k != "gate_tstat"}
    old, old_missing = research_evidence("EQ03", v140, ledger["EQ03"], params)
    assert old_missing == [] and old.t_stat == rep["nw_tstat_uncrossed"]
    assert dataclasses.replace(old, t_stat=result.t_stat) == result
    v140["nw_tstat_uncrossed"] = None
    old, _ = research_evidence("EQ03", v140, ledger["EQ03"], params)
    assert old.t_stat == rep["nw_tstat"]
    # the threshold the report was judged at travels beside the result
    judged_at = significance_threshold_from_report(rep)
    assert judged_at == rep["gates"]["min_nw_tstat"]
    assert judged_at == ExperimentLedger.bonferroni_t_threshold_at(ledger["EQ03"]["gate_looks"])
    assert judged_at > VALIDATE_GATES["min_nw_tstat"]
    sig = bootstrap.registry.get("EQ03").last_evaluation.gates["statistical_significance"]
    assert sig == GateResult(passed=True, value=rep["gate_tstat"], threshold=judged_at)
    assert significance_threshold_from_report({}) is None
    assert significance_threshold_from_report({"gates": {"min_nw_tstat": None}}) is None
    assert significance_threshold_from_report({"gates": {"min_nw_tstat": 0.0}}) is None
    assert significance_threshold_from_report({"gates": {"min_nw_tstat": 3.0}}) == 3.0
    assert result.rank_ic == rep["oos_rank_ic"]
    assert result.experiment_id == ledger["EQ03"]["key"][:16]
    assert result.n_experiments_in_ledger == ledger["EQ03"]["n"]
    assert result.created_ts == max(f["test_end"] for f in rep["folds"])
    scale = 1e4 / REFERENCE_NOTIONAL_USD
    assert result.net_return_bps == pytest.approx(rep["stress"]["cost"]["x1"]["total_pnl"] * scale)
    assert result.transaction_cost_bps == pytest.approx(
        rep["stress"]["cost"]["x1"]["total_costs"] * scale
    )
    assert result.verdict is Verdict.ITERATE and result.leakage_passed
    assert result.dataset_version == params["data_version"]
    assert capacity_from_report(rep) == pytest.approx(
        sum(rep["capacity_usd_by_instrument"].values())
    )
    rec = bootstrap.registry.get("EQ03")
    assert (rec.experiment_id, rec.data_version, rec.feature_version, rec.model_version) == (
        result.experiment_id,
        result.dataset_version,
        result.feature_version,
        result.model_version,
    )
    validate_typed(result)


def test_bootstrap_nan_metric_is_reported_not_crashed(tmp_path):
    rep = load_report(ROOT, "EQ03")
    rep["gate_ic"] = None
    rep["oos_rank_ic"] = float("nan")
    ledger = load_ledger_entries(ROOT)
    result, missing = research_evidence("EQ03", rep, ledger["EQ03"], load_params_document(ROOT))
    assert result is None and missing == ["gate_ic", "oos_rank_ic"]
    unledgered, _ = research_evidence(
        "EQ03", load_report(ROOT, "EQ03"), None, load_params_document(ROOT)
    )
    assert unledgered is not None and unledgered.n_experiments_in_ledger == 0
    assert unledgered.experiment_id == "EQ03-unledgered"
    # the gate t: gate_tstat when the report has the key (even when null) ...
    rep = load_report(ROOT, "EQ03")
    rep["gate_tstat"] = None
    result, missing = research_evidence("EQ03", rep, ledger["EQ03"], load_params_document(ROOT))
    assert result is None and missing == ["gate_tstat"]
    # ... and for a report written before v1.5.0 the v1.4.0 rule, unchanged
    del rep["gate_tstat"]
    rep["nw_tstat_uncrossed"] = None
    rep["nw_tstat"] = None
    result, missing = research_evidence("EQ03", rep, ledger["EQ03"], load_params_document(ROOT))
    assert result is None and missing == ["nw_tstat"]

    # end to end: a copy of the tree with one NaN report stays at RESEARCH
    root = tmp_path / "repo"
    for rel in ("configs/strategies", "research/alpha_reports"):
        (root / rel).mkdir(parents=True)
    for name in ("lifecycle.json", "strategies.json", "alpha_params.json"):
        (root / "configs" / "strategies" / name).write_bytes(
            (ROOT / "configs" / "strategies" / name).read_bytes()
        )
    (root / "research" / "experiments.json").write_bytes(
        (ROOT / "research" / "experiments.json").read_bytes()
    )
    for aid in ("EQ01", "EQ03"):
        doc = load_report(ROOT, aid)
        if aid == "EQ03":
            doc["gate_tstat"] = None
        (root / "research" / "alpha_reports" / f"{aid}.json").write_text(json.dumps(doc))
    out = run_bootstrap(root, write=True, alpha_ids=["EQ01", "EQ03"])
    states = {r.alpha_id: r.state for r in out.rows}
    assert states == {"EQ01": S.CANDIDATE, "EQ03": S.RESEARCH}
    eq03 = out.registry.get("EQ03")
    assert eq03.last_evaluation.failed_gates == ["ledger_entry_exists", "leakage_clean"]
    assert eq03.last_evaluation.gates["ledger_entry_exists"].value is None
    assert [r.missing_metrics for r in out.rows if r.alpha_id == "EQ03"] == [("gate_tstat",)]
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
            (ROOT / "configs" / "strategies" / name).read_bytes()
        )
    (root / "research" / "experiments.json").write_bytes(
        (ROOT / "research" / "experiments.json").read_bytes()
    )
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
    assert registry_path.read_bytes() != first_registry  # the retire is still there
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


_BLOCK_FOR_STATE = {
    S.RESEARCH: 1,
    S.CANDIDATE: 1,
    S.VALIDATING: 2,
    S.PAPER: 3,
    S.ACTIVE: 4,
    S.WATCH: 4,
    S.RETIRED: 4,
}


def _random_evidence(rng: SplitMix64, alpha_id: str, state: LifecycleState) -> Evidence:
    """Random evidence; half the time the block the current state reads,
    otherwise any block (including none), so every state is exercised."""
    pick = _BLOCK_FOR_STATE[state] if rng.below(2) == 0 else rng.below(5)
    if pick == 0:
        return Evidence.empty()
    if pick == 1:
        leak = rng.below(8) == 0
        ic = rng.uniform() * 0.04
        return _ev(
            research=golden_research(
                alpha_id,
                ic=ic,
                rank_ic=ic * (0.5 + rng.uniform()),
                t_stat=2.0 + rng.uniform() * 4.0,
                fold_consistency=round(0.5 + rng.uniform() * 0.5, 2),
                net_bps=rng.uniform() * 10.0 - 1.0,
                leakage_passed=not leak,
                verdict=Verdict.REJECT if leak else Verdict.PROMOTE,
            ),
            capacity_usd=5e5 + rng.uniform() * 4e6,
            # judged at a ledger threshold in [3.0, 4.0), or (1 in 8) at none
            significance_threshold=None if rng.below(8) == 0 else 3.0 + rng.uniform(),
        )
    if pick == 2:
        return _ev(
            validation=ValidationEvidence(
                holdout_ic=0.005 + rng.uniform() * 0.03,
                research_ic=0.02,
                replay_hash_match=rng.below(4) > 0,
                parity=rng.below(4) > 0,
            )
        )
    if pick == 3:
        return _ev(
            paper=PaperEvidence(
                n_sessions=3 + rng.below(6),
                realized_ic=0.005 + rng.uniform() * 0.03,
                research_ic=0.02,
                net_pnl=rng.uniform() * 200.0 - 20.0,
                n_kill_events=int(rng.below(4) == 0),
                tracking_error=rng.uniform(),
            )
        )
    ic = None if rng.below(5) == 0 else rng.uniform() * 0.04 - 0.02
    return _ev(
        live=_live(
            ic,
            informative=rng.below(6) > 0,
            eval_index=rng.below(1000),
            new_fraction=(1 + rng.below(8)) / 8.0,  # 0.125 .. 1.0
        )
    )


@pytest.mark.parametrize("policy", ["default", "legacy"])
def test_property_state_index_steps_and_retired_terminal(config, policy):
    cfg = _policy(config, policy)
    rng = SplitMix64(20260919)
    m = _machine(cfg, "P1")
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
            kind = (
                EdgeKind.LIVE
                if before in (S.ACTIVE, S.WATCH)
                else (EdgeKind.PROMOTION if int(after) > int(before) else EdgeKind.DEMOTION)
            )
            edge_for(before, after, kind)
            validate_typed(tr)
        else:
            assert after is before
        assert m.record(aid).consecutive_failures < config.max_consecutive_failures
        rec = m.record(aid)
        if policy == "legacy":
            assert rec.cusum == 0.0
            assert rec.breach_count < cfg.live.retire_breach_evals
        else:
            assert rec.breach_count == 0 and rec.cusum >= 0.0
        if after not in (S.ACTIVE, S.WATCH):
            assert (rec.breach_count, rec.recovery_count, rec.cusum) == (0, 0, 0.0)
    assert reached >= {S.CANDIDATE, S.VALIDATING, S.PAPER, S.ACTIVE, S.WATCH, S.RETIRED}
    assert m.transitions and all(isinstance(t, LifecycleTransition) for t in m.transitions)


@pytest.mark.parametrize("policy", ["default", "legacy"])
def test_property_transition_log_replays_to_registry(config, tmp_path, policy):
    rng = SplitMix64(7)
    log = LifecycleTransitionLog(tmp_path / "t.jsonl")
    m = _machine(_policy(config, policy), "P1", log)
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
    assert not any(
        math.isnan(g.value) for t in m.transitions for g in t.gates.values() if g.value is not None
    )


# --------------------------------------------------------------------------
# The cross-alpha correlation gate (AF03) and the net P&L bootstrap gate
# --------------------------------------------------------------------------


def _peers(*rows) -> CrossAlphaEvidence:
    return CrossAlphaEvidence(tuple(CrossAlphaPeer(a, s, rho) for a, s, rho in rows))


def _candidate(config) -> AlphaLifecycle:
    """A machine whose alpha LCX is at CANDIDATE."""
    m = _machine(config)
    m.advance("LCX", T0 + STEP, _ev(research=golden_research("LCX")))
    assert m.state("LCX") is S.CANDIDATE
    return m


def _full(**kw) -> Evidence:
    """Candidate evidence that passes every gate unless ``kw`` changes it."""
    base = dict(research=golden_research("LCX"), capacity_usd=5e6, significance_threshold=THRESHOLD)
    base.update(kw)
    return _ev(**base)


def test_gate_table_has_twenty_rows_and_the_candidate_edge_eleven(config, legacy):
    names = [s.name for s in GATE_SPECS]
    assert len(names) == 20 and len(ALLOWED_TRANSITIONS) == 17
    assert names[names.index("net_pnl_after_costs") + 1] == "net_pnl_bootstrap_ci"
    assert names[names.index("stability") + 1] == "cross_alpha_correlation"
    edge = PROMOTION_EDGES[S.CANDIDATE]
    assert len(edge.gates) == 11 and edge.gates[-1] == "cross_alpha_correlation"
    assert AlphaLifecycle(config, AlphaRegistry(config.policy)).edge_gates(edge) == edge.gates
    # the legacy policy evaluates the same edge without the bootstrap gate
    legacy_gates = AlphaLifecycle(legacy, AlphaRegistry(legacy.policy)).edge_gates(edge)
    assert legacy_gates == tuple(g for g in edge.gates if g != BOOTSTRAP_GATE)
    assert legacy.net_pnl_ci_gate == "absent" and config.net_pnl_ci_gate == "required"


@pytest.mark.parametrize(
    "peers, passed, value",
    [
        (None, False, None),  # nobody measured: fail closed
        ((), True, 0.0),  # no other alpha: vacuous pass
        ((("A", "ACTIVE", 0.82),), False, 0.82),
        ((("A", "ACTIVE", -0.9),), False, 0.9),  # |correlation|
        ((("A", "VALIDATING", 0.7),), True, 0.7),  # the tie: max is inclusive
        ((("A", "PAPER", 0.7), ("B", "WATCH", -0.7)), True, 0.7),
        ((("A", "RESEARCH", 1.0), ("B", "CANDIDATE", 1.0), ("C", "RETIRED", 1.0)), True, 0.0),
        ((("A", "ACTIVE", 0.25), ("B", "VALIDATING", -0.4)), True, 0.4),
        ((("A", "WATCH", 0.71), ("B", "RETIRED", 0.1)), False, 0.71),
    ],
)
def test_cross_alpha_correlation_gate(config, peers, passed, value):
    gate = build_gates(config)["cross_alpha_correlation"]
    block = None if peers is None else _peers(*peers)
    got = gate.evaluate("LCX", _full(cross_alpha=block))
    assert (got.passed, got.value, got.threshold) == (passed, value, 0.7)
    # ... and through the machine: pass -> VALIDATING, fail -> HOLD on this gate alone
    m = _candidate(config)
    tr = m.advance("LCX", T0 + 2 * STEP, _full(cross_alpha=block))
    if passed:
        assert tr is not None and tr.to_state is S.VALIDATING
        assert tr.gates["cross_alpha_correlation"].value == value
    else:
        assert tr is None and m.evaluations[-1].outcome == Outcome.HOLD
        assert m.evaluations[-1].failed_gates == ["cross_alpha_correlation"]
        assert m.record("LCX").consecutive_failures == 0


def test_cross_alpha_min_state_is_configurable(config):
    paper_only = dataclasses.replace(config, cross_alpha_min_state="PAPER")
    block = _peers(("A", "VALIDATING", 0.99), ("B", "PAPER", 0.3))
    got = build_gates(paper_only)["cross_alpha_correlation"].evaluate("X", _full(cross_alpha=block))
    assert (got.passed, got.value) == (True, 0.3)
    assert (
        not build_gates(config)["cross_alpha_correlation"]
        .evaluate("X", _full(cross_alpha=block))
        .passed
    )
    for bad in ("CANDIDATE", "WATCH", "RETIRED", "research"):
        with pytest.raises(ValueError, match="cross_alpha_min_state"):
            dataclasses.replace(config, cross_alpha_min_state=bad)


def test_cross_alpha_evidence_is_strict_and_names_the_binding_peer():
    block = _peers(("B", "ACTIVE", -0.6), ("C", "PAPER", 0.6), ("D", "RETIRED", 0.9))
    assert block.max_abs_correlation("VALIDATING") == 0.6
    # the tie between B and C: the smaller id is the binding peer
    assert block.binding_peer("VALIDATING").alpha_id == "B"
    assert block.binding_peer("ACTIVE").alpha_id == "B"
    assert _peers(("D", "RETIRED", 0.9)).binding_peer("VALIDATING") is None
    assert CrossAlphaEvidence.from_dict(block.to_dict()) == block
    assert block.to_dict()["peers"][0] == {"alpha_id": "B", "state": "ACTIVE", "correlation": -0.6}
    with pytest.raises(ValueError, match="sorted by alpha_id"):
        _peers(("B", "ACTIVE", 0.1), ("A", "ACTIVE", 0.1))
    with pytest.raises(ValueError, match="sorted by alpha_id"):
        _peers(("A", "ACTIVE", 0.1), ("A", "PAPER", 0.2))
    with pytest.raises(ValueError, match=r"\[-1, 1\]"):
        CrossAlphaPeer("A", "ACTIVE", 1.01)
    with pytest.raises(ValueError, match="non-finite"):
        CrossAlphaPeer("A", "ACTIVE", float("nan"))
    with pytest.raises(ValueError, match="unknown lifecycle state"):
        CrossAlphaPeer("A", "LIVE", 0.1)
    with pytest.raises(ValueError, match="non-empty"):
        CrossAlphaPeer("", "ACTIVE", 0.1)
    with pytest.raises(ValueError, match="exactly the key 'peers'"):
        CrossAlphaEvidence.from_dict({"peers": [], "extra": 1})
    with pytest.raises(ValueError, match="unknown keys"):
        CrossAlphaEvidence.from_dict(
            {"peers": [{"alpha_id": "A", "state": "ACTIVE", "correlation": 0.1, "n": 3}]}
        )
    with pytest.raises(ValueError, match="CrossAlphaEvidence or None"):
        _ev(cross_alpha={"peers": []})


@pytest.mark.parametrize(
    "block, passed, value, reason",
    [
        (None, False, None, "no bootstrap interval"),
        (golden_bootstrap(12.5, 90.0), True, 12.5, "lower bound 12.50 > 0.0"),
        (golden_bootstrap(-35.5, 60.25), False, -35.5, "lower bound -35.50 <= 0.0"),
        (golden_bootstrap(0.0, 45.0), False, 0.0, "lower bound 0.00 <= 0.0"),  # strict
        (golden_bootstrap(0.0, 0.0, n_trades=0), False, None, "no trade"),
        (golden_bootstrap(50.0, 90.0, n_trades=0), False, None, "no trade"),
        (golden_bootstrap(None, None, n_bars=5), False, None, "too few"),
        (golden_bootstrap(12.5, 90.0, level=0.9), False, None, "requires 0.95"),
        (golden_bootstrap(12.5, 90.0, level=0.99), False, None, "requires 0.95"),
    ],
)
def test_net_pnl_bootstrap_gate(config, block, passed, value, reason):
    gate = build_gates(config)[BOOTSTRAP_GATE]
    evidence = _full(pnl_bootstrap=block)
    got = gate.evaluate("LCX", evidence)
    assert (got.passed, got.value, got.threshold) == (passed, value, 0.0)
    assert reason in bootstrap_gate_reason(evidence, config)
    m = _candidate(config)
    tr = m.advance("LCX", T0 + 2 * STEP, evidence)
    if passed:
        assert tr is not None and tr.to_state is S.VALIDATING and len(tr.gates) == 11
    else:
        assert tr is None and m.evaluations[-1].failed_gates == [BOOTSTRAP_GATE]


def test_net_pnl_bootstrap_gate_is_absent_under_the_legacy_policy(legacy):
    """``net_pnl_ci_gate = "absent"``: evidence with no interval at all is
    promoted on ten gates and the gate is in no result."""
    m = _candidate(legacy)
    tr = m.advance("LCX", T0 + 2 * STEP, _full(pnl_bootstrap=None, significance_threshold=None))
    assert tr is not None and tr.to_state is S.VALIDATING
    assert BOOTSTRAP_GATE not in tr.gates and len(tr.gates) == 10
    assert tr.reason == "all 10 gates passed: CANDIDATE -> VALIDATING"
    assert BOOTSTRAP_GATE not in m.evaluations[-1].gates
    # a failing evaluation under the legacy policy does not list it either
    m2 = _candidate(legacy)
    assert m2.advance("LCX", T0 + 2 * STEP, _full(pnl_bootstrap=None, capacity_usd=1.0)) is None
    assert m2.evaluations[-1].failed_gates == ["capacity"]


def test_pnl_bootstrap_evidence_is_strict():
    block = golden_bootstrap()
    assert PnlBootstrapEvidence.from_dict(block.to_dict()) == block
    assert list(block.to_dict()) == [
        "ci_low",
        "ci_high",
        "level",
        "n_resamples",
        "seed",
        "mean_block",
        "n_bars",
        "n_trades",
    ]
    good = block.to_dict()
    for change, message in [
        ({"ci_low": None}, "both numbers or both null"),
        ({"ci_high": 1.0}, "ci_high < ci_low"),
        ({"level": 1.0}, r"\(0, 1\)"),
        ({"n_resamples": 0}, "< 1"),
        ({"seed": -1}, "< 0"),
        ({"mean_block": 0.5}, ">= 1"),
        ({"n_trades": 1.5}, "expected an integer"),
        ({"ci_low": float("inf")}, "non-finite"),
    ]:
        with pytest.raises(ValueError, match=message):
            PnlBootstrapEvidence.from_dict({**good, **change})
    with pytest.raises(ValueError, match="missing keys"):
        PnlBootstrapEvidence.from_dict({k: v for k, v in good.items() if k != "seed"})
    with pytest.raises(ValueError, match="PnlBootstrapEvidence or None"):
        _ev(pnl_bootstrap=good)


def test_bootstrap_builds_both_blocks_from_the_artefacts(bootstrap, config):
    """On the committed artefacts: every alpha's evidence carries the
    report's interval and trade count, nobody is at or beyond VALIDATING so
    the correlation gate passes vacuously for all 24, and the bootstrap gate
    fails for all 24 — 17 for making no trade, 7 on a negative lower bound."""
    correlations = load_signal_correlations(ROOT)
    assert correlations is not None and sorted(correlations) == sorted(ALPHA_IDS)
    no_trade, negative = [], []
    for row in bootstrap.rows:
        rep = load_report(ROOT, row.alpha_id)
        block = pnl_bootstrap_from_report(rep)
        assert block is not None and block.level == config.gates.net_pnl_ci_level
        assert (block.n_resamples, block.seed) == (1000, 20260829)
        assert block.n_trades == sum(f["trade_count_1x"] for f in rep["fold_diagnostics"])
        gates = bootstrap.registry.get(row.alpha_id).last_evaluation.gates
        corr = gates["cross_alpha_correlation"]
        assert (corr.passed, corr.value, corr.threshold) == (True, 0.0, 0.7), row.alpha_id
        boot = gates[BOOTSTRAP_GATE]
        assert not boot.passed and boot.threshold == 0.0
        if block.n_trades == 0:
            assert boot.value is None
            no_trade.append(row.alpha_id)
        else:
            assert boot.value == block.ci_low < 0.0
            negative.append(row.alpha_id)
    assert (len(no_trade), len(negative)) == (17, 7)
    assert negative == ["EQ06", "EQ11", "FX05", "FX08", "FX09", "FX10", "FX11"]
    # no verdict or state moved because of the two gates: every alpha
    # already failed net_pnl_after_costs
    for row in bootstrap.rows:
        assert row.state is S.CANDIDATE and "net_pnl_after_costs" in row.failed_gates


def test_cross_alpha_evidence_follows_registration_order(config):
    """Peers are the alphas registered before, in their current state; an
    unmeasured pair leaves the block out (fail closed) — except for the
    first alpha, whose empty block is a fact."""
    registry = AlphaRegistry(config.policy)
    assert cross_alpha_evidence("A1", registry, None) == NO_PEERS
    registry.add(AlphaRecord.new("A1", T0))
    registry.add(AlphaRecord.new("A0", T0))
    corr = {"A2": {"A0": -0.25, "A1": 0.5}}
    block = cross_alpha_evidence("A2", registry, corr)
    assert [(p.alpha_id, p.state, p.correlation) for p in block.peers] == [
        ("A0", S.RESEARCH, -0.25),
        ("A1", S.RESEARCH, 0.5),
    ]
    assert cross_alpha_evidence("A2", registry, None) is None
    assert cross_alpha_evidence("A2", registry, {"A2": {"A0": 0.1}}) is None
    # the alpha itself is never its own peer
    assert [
        p.alpha_id for p in cross_alpha_evidence("A1", registry, {"A1": {"A0": 0.3}}).peers
    ] == ["A0"]
