"""Scripted lifecycle scenarios behind ``tests/golden/expected_lifecycle.json``.

Three synthetic alphas walk the machine with hand-written evidence whose
numbers are short decimals (a port replicates them without float drift):

* **LC01** — the full happy path RESEARCH -> CANDIDATE -> VALIDATING ->
  PAPER -> ACTIVE, a null and an uninformative live reading (no movement),
  ACTIVE -> WATCH on a breach, a neutral-zone reset, three recoveries ->
  ACTIVE, then six consecutive breaches -> RETIRED, a SYSTEM ``advance`` on
  the retired alpha (TERMINAL, no movement) and the HUMAN reset to RESEARCH;
* **LC02** — CANDIDATE, then a re-run showing leakage: demoted to RESEARCH;
  the leaking result and an empty evidence document both hold at RESEARCH;
* **LC03** — reaches VALIDATING, fails parity once (counter 1), an absent
  validation block is silence (counter unchanged), passes -> PAPER, fails the
  paper gates ``max_consecutive_failures`` (3) times -> CANDIDATE, is retired
  by a HUMAN and then ignores a SYSTEM ``advance``.

Each golden step records the action, the input evidence document and the
expected state / outcome / gate results / counters / transition after it.
Event times are ``T0 + k * STEP_NS`` (15-minute adaptive blocks).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from iap.contracts.types import Actor, ExperimentResult, LifecycleState, Verdict
from iap.lifecycle.config import PolicyConfig
from iap.lifecycle.evidence import Evidence, LiveEvidence, PaperEvidence, ValidationEvidence
from iap.lifecycle.machine import AlphaLifecycle, transition_table
from iap.lifecycle.registry import AlphaRegistry, Outcome

__all__ = [
    "GOLDEN_X_VERSION",
    "STEP_NS",
    "T0",
    "ScriptStep",
    "golden_document",
    "render_golden",
    "run_scripts",
    "scripts",
]

GOLDEN_X_VERSION = 1
T0 = 1_700_000_000_000_000_000
STEP_NS = 900_000_000_000

_DATASET = "0123456789abcdef" * 4
_FEATURES = "fedcba9876543210" * 4
_GIT = "golden"

#: Hand-pinned end states: the generator refuses to write a golden whose
#: machine run disagrees with the scenario's design.
EXPECTED_FINAL_STATES: Mapping[str, LifecycleState] = {
    "LC01": LifecycleState.RESEARCH,
    "LC02": LifecycleState.RESEARCH,
    "LC03": LifecycleState.RETIRED,
}


@dataclass(frozen=True)
class ScriptStep:
    """One scripted action: ``advance`` with evidence, or a HUMAN
    ``retire`` / ``reset`` with a reason."""

    action: str
    evidence: Optional[Evidence]
    reason: Optional[str]
    note: str

    def __post_init__(self) -> None:
        if self.action not in ("advance", "retire", "reset"):
            raise ValueError(f"unknown script action {self.action!r}")
        if (self.action == "advance") != (self.evidence is not None):
            raise ValueError("advance steps carry evidence; manual steps do not")
        if (self.action != "advance") != (self.reason is not None):
            raise ValueError("manual steps carry a reason; advance steps do not")


def research(alpha_id: str, *, ic: float = 0.02, rank_ic: float = 0.03,
             t_stat: float = 4.0, fold_consistency: float = 1.0, n_folds: int = 4,
             leakage_passed: bool = True, hypothesis: Optional[bool] = True,
             net_bps: float = 5.0, cost_bps: float = 7.0, n_ledger: int = 10,
             verdict: Verdict = Verdict.PROMOTE) -> ExperimentResult:
    """A research result with simple decimal metrics."""
    return ExperimentResult(
        experiment_id=f"{alpha_id.lower()}-research", alpha_id=alpha_id,
        dataset_version=_DATASET, feature_version=_FEATURES, model_version=None,
        ic=ic, rank_ic=rank_ic, t_stat=t_stat, nw_lags=2, hit_rate=0.55,
        turnover=40.0, gross_return_bps=net_bps + cost_bps,
        transaction_cost_bps=cost_bps, net_return_bps=net_bps,
        max_drawdown_bps=20.0, sharpe=1.5, fold_consistency=fold_consistency,
        n_folds=n_folds, leakage_passed=leakage_passed,
        leakage_detail={"shift_ok": leakage_passed, "label_guard_ok": True},
        hypothesis_sign_confirmed=hypothesis, verdict=verdict,
        n_experiments_in_ledger=n_ledger, git_commit=_GIT, created_ts=T0)


def _ev(research_result: Optional[ExperimentResult] = None, capacity: Optional[float] = None,
        validation: Optional[ValidationEvidence] = None,
        paper: Optional[PaperEvidence] = None,
        live: Optional[LiveEvidence] = None) -> Evidence:
    return Evidence(research=research_result, capacity_usd=capacity,
                    validation=validation, paper=paper, live=live)


def _live(rolling_ic: Optional[float], eval_index: int, informative: bool = True,
          n_buckets: int = 8) -> LiveEvidence:
    return LiveEvidence(rolling_ic=rolling_ic, n_buckets=n_buckets,
                        eval_index=eval_index, informative=informative)


def _adv(evidence: Evidence, note: str) -> ScriptStep:
    return ScriptStep("advance", evidence, None, note)


def _lc01() -> Tuple[ScriptStep, ...]:
    good = research("LC01")
    validation_ok = ValidationEvidence(holdout_ic=0.018, research_ic=0.02,
                                       replay_hash_match=True, parity=True)
    paper_ok = PaperEvidence(n_sessions=5, realized_ic=0.015, research_ic=0.02,
                             net_pnl=100.0, n_kill_events=0, tracking_error=0.001)
    steps: List[ScriptStep] = [
        _adv(_ev(good), "research result present and clean -> CANDIDATE"),
        _adv(_ev(good, capacity=5_000_000.0), "all nine promotion gates pass -> VALIDATING"),
        _adv(_ev(validation=validation_ok), "held-out replay tracks research, hash + parity -> PAPER"),
        _adv(_ev(paper=paper_ok), "five clean paper sessions -> ACTIVE"),
        _adv(_ev(live=_live(0.02, 1)), "healthy rolling IC: hold ACTIVE"),
        _adv(_ev(live=_live(None, 2, n_buckets=2)), "null rolling IC (too few buckets): no evidence"),
        _adv(_ev(live=_live(-0.01, 3, informative=False)), "uninformative re-read of a breach: no evidence"),
        _adv(_ev(live=_live(-0.01, 4)), "breach -> WATCH (entering breach counts: 1)"),
        _adv(_ev(live=_live(-0.01, 5)), "second consecutive breach (2)"),
        _adv(_ev(live=_live(0.002, 6)), "neutral zone [0.0, 0.005): both counters reset"),
        _adv(_ev(live=_live(0.01, 7)), "recovery 1"),
        _adv(_ev(live=_live(0.01, 8)), "recovery 2"),
        _adv(_ev(live=_live(0.01, 9)), "recovery 3 -> ACTIVE (re-activation)"),
        _adv(_ev(live=_live(-0.02, 10)), "relapse -> WATCH (breach 1)"),
    ]
    for k, idx in enumerate(range(11, 16), start=2):
        note = f"consecutive breach {k}" + (" -> RETIRED" if k == 6 else "")
        steps.append(_adv(_ev(live=_live(-0.02, idx)), note))
    steps.append(_adv(_ev(live=_live(0.05, 16)),
                      "SYSTEM advance on a RETIRED alpha: terminal, no movement"))
    steps.append(ScriptStep("reset", None, "re-research after regime change",
                            "HUMAN reset RETIRED -> RESEARCH"))
    return tuple(steps)


def _lc02() -> Tuple[ScriptStep, ...]:
    clean = research("LC02", ic=0.015, rank_ic=0.02, t_stat=3.5, verdict=Verdict.PROMOTE)
    leaking = research("LC02", ic=0.15, rank_ic=0.2, t_stat=30.0, leakage_passed=False,
                       verdict=Verdict.REJECT)
    return (
        _adv(_ev(clean), "clean research -> CANDIDATE"),
        _adv(_ev(leaking, capacity=5_000_000.0),
             "re-run shows leakage: leakage_clean fails -> demoted to RESEARCH"),
        _adv(_ev(leaking), "leaking result at RESEARCH: ledger passes, leakage fails, hold"),
        _adv(Evidence.empty(), "no evidence at RESEARCH: presence gate fails (value null), hold"),
    )


def _lc03() -> Tuple[ScriptStep, ...]:
    good = research("LC03", ic=0.03, rank_ic=0.04, t_stat=5.0, net_bps=8.0, cost_bps=4.0)
    validation_bad = ValidationEvidence(holdout_ic=0.028, research_ic=0.03,
                                        replay_hash_match=True, parity=False)
    validation_ok = ValidationEvidence(holdout_ic=0.028, research_ic=0.03,
                                       replay_hash_match=True, parity=True)
    paper_bad = PaperEvidence(n_sessions=2, realized_ic=0.01, research_ic=0.03,
                              net_pnl=-50.0, n_kill_events=1, tracking_error=0.002)
    return (
        _adv(_ev(good), "research present and clean -> CANDIDATE"),
        _adv(_ev(good, capacity=2_000_000.0), "promotion gates pass -> VALIDATING"),
        _adv(_ev(validation=validation_bad), "parity fails: consecutive_failures 1"),
        _adv(_ev(good), "validation block absent: silence, counter unchanged"),
        _adv(_ev(validation=validation_ok), "validation passes -> PAPER, counter reset"),
        _adv(_ev(paper=paper_bad), "paper gates fail: consecutive_failures 1"),
        _adv(_ev(paper=paper_bad), "paper gates fail: consecutive_failures 2"),
        _adv(_ev(paper=paper_bad), "third failure = max_consecutive_failures -> CANDIDATE"),
        ScriptStep("retire", None, "manual retirement: hypothesis withdrawn by the desk",
                   "HUMAN retire CANDIDATE -> RETIRED"),
        _adv(_ev(good, capacity=2_000_000.0), "SYSTEM advance on RETIRED: terminal"),
    )


def scripts() -> Dict[str, Tuple[ScriptStep, ...]]:
    """The three scenario scripts, keyed by alpha id (sorted)."""
    return {"LC01": _lc01(), "LC02": _lc02(), "LC03": _lc03()}


def _run_script(alpha_id: str, steps: Tuple[ScriptStep, ...],
                config: PolicyConfig) -> List[Dict[str, Any]]:
    registry = AlphaRegistry(config.policy)
    machine = AlphaLifecycle(config, registry)
    machine.register(alpha_id, T0)
    out: List[Dict[str, Any]] = []
    for k, step in enumerate(steps):
        event_ts = T0 + (k + 1) * STEP_NS
        row: Dict[str, Any] = {
            "step": k, "note": step.note, "action": step.action, "event_ts": event_ts,
            "evidence": None if step.evidence is None else step.evidence.to_dict(),
            "reason": step.reason,
        }
        if step.action == "advance":
            transition = machine.advance(alpha_id, event_ts, step.evidence)
            evaluation = machine.evaluations[-1]
            outcome = evaluation.outcome
            gates = {name: g.to_dict() for name, g in evaluation.gates.items()}
        elif step.action == "retire":
            transition = machine.retire(alpha_id, event_ts, step.reason, actor=Actor.HUMAN)
            outcome, gates = Outcome.TRANSITION, {}
        else:
            transition = machine.reset_to_research(alpha_id, event_ts, step.reason,
                                                   actor=Actor.HUMAN)
            outcome, gates = Outcome.TRANSITION, {}
        rec = registry.get(alpha_id)
        row["expected"] = {
            "state": rec.state.name,
            "state_index": int(rec.state),
            "outcome": outcome,
            "gates": gates,
            "consecutive_failures": rec.consecutive_failures,
            "breach_count": rec.breach_count,
            "recovery_count": rec.recovery_count,
            "transition": None if transition is None else transition.to_dict(),
        }
        out.append(row)
    final = registry.get(alpha_id).state
    if final is not EXPECTED_FINAL_STATES[alpha_id]:
        raise RuntimeError(f"{alpha_id}: script ended in {final.name}, designed to end in "
                           f"{EXPECTED_FINAL_STATES[alpha_id].name}")
    return out


def run_scripts(config: PolicyConfig) -> Dict[str, List[Dict[str, Any]]]:
    """Run every script through a fresh machine; ``{alpha_id: steps}``."""
    return {aid: _run_script(aid, steps, config) for aid, steps in sorted(scripts().items())}


def golden_document(config: PolicyConfig) -> Dict[str, Any]:
    """The golden document (JSON-ready, key order pinned)."""
    return {
        "x-version": GOLDEN_X_VERSION,
        "description": (
            "Alpha promotion lifecycle golden (iap.lifecycle). `config` is the merged "
            "policy (configs/strategies/lifecycle.json + strategies.json adaptive."
            "lifecycle); `transition_table` the allowed edges with their gates in "
            "evaluation order; `states` the integer ids; each scenario lists per step "
            "the action, the input evidence, and the expected state / outcome / gate "
            "results / counters / transition after it. Generated by "
            "python/tools/make_golden_lifecycle.py; every port reproduces every step "
            "exactly (states, booleans and decimals are exact; no tolerance)."),
        "t0": T0,
        "step_ns": STEP_NS,
        "config": config.to_dict(),
        "states": {s.name: int(s) for s in LifecycleState},
        "transition_table": transition_table(),
        "scenarios": run_scripts(config),
    }


def render_golden(doc: Mapping[str, Any]) -> str:
    """The exact bytes of the golden file (2-space indent, ASCII, insertion
    key order, trailing newline)."""
    return json.dumps(doc, indent=2, ensure_ascii=True, sort_keys=False,
                      allow_nan=False) + "\n"
