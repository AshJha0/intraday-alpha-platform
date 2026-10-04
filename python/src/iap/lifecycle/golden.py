"""Scripted lifecycle scenarios behind ``tests/golden/expected_lifecycle.json``.

Synthetic alphas walk the machine with hand-written evidence whose numbers
are short decimals (a port replicates them without float drift).  Under the
DEFAULT policy (``tstat_threshold = "ledger"``, the CUSUM retirement rule —
``configs/strategies/lifecycle.json`` + ``strategies.json``):

* **LC01** — the full happy path RESEARCH -> CANDIDATE -> VALIDATING ->
  PAPER -> ACTIVE, a null and an uninformative live reading (no movement),
  ACTIVE -> WATCH on a breach, a reading in the neutral zone, three
  recoveries -> ACTIVE, a relapse and then breaches until the CUSUM
  statistic reaches its threshold -> RETIRED, a SYSTEM ``advance`` on the
  retired alpha (TERMINAL, no movement) and the HUMAN reset to RESEARCH;
* **LC02** — CANDIDATE, then a re-run showing leakage: demoted to RESEARCH;
  the leaking result and an empty evidence document both hold at RESEARCH;
* **LC03** — the significance threshold: at CANDIDATE a t below the
  evidence's ledger threshold fails ``statistical_significance``, evidence
  WITHOUT a threshold fails it with ``threshold = null``, and a threshold
  below the configured floor is replaced by the floor; then VALIDATING,
  a parity failure (counter 1), an absent validation block (silence),
  -> PAPER, three failed paper evaluations -> CANDIDATE, a HUMAN retirement
  and a SYSTEM ``advance`` that it ignores;
* **LC04** — what the CUSUM rule changes: eight consecutive readings just
  under the watch gate but inside the slack never retire the alpha (the
  consecutive rule would at the sixth), a reading above the gate while
  ``S`` is over the threshold does not retire it either, and two deep
  breaches in disjoint windows (``new_fraction`` 1.0) do;
* **LC05** — the cross-alpha correlation gate, at CANDIDATE with every other
  gate passing: evidence WITHOUT a ``cross_alpha`` block fails it with
  ``value = null`` (fail closed), a peer at ACTIVE correlated 0.82 fails it,
  so does one correlated -0.9 (the absolute value is taken) while peers at
  CANDIDATE and RETIRED are ignored whatever their correlation, and peers
  that are all ineligible pass with value 0.0; after a HUMAN retire / reset
  two peers at exactly +0.7 and -0.7 pass (``max`` is inclusive: the tie);
  then an EMPTY peer list passes vacuously; then eligible peers at 0.25 and
  -0.4 pass with value 0.4.

* **LC06** — the net P&L bootstrap gate, at CANDIDATE with every other
  gate passing: evidence WITHOUT a ``pnl_bootstrap`` block fails it with
  ``value = null``; an interval whose lower bound is negative fails it at
  that value; an alpha that made no trade fails it with ``value = null``
  (the degenerate [0, 0] interval is not evidence); so do an interval
  without bounds and one taken at another level than the policy's; a lower
  bound of exactly 0.0 fails (strict); a positive lower bound passes.

Every other script that walks CANDIDATE -> VALIDATING carries an empty
``cross_alpha`` block (no other alpha: the vacuous pass) and a passing
``pnl_bootstrap`` block, so each of them still tests the one rule it was
written for.

Under the LEGACY policy (``tstat_threshold = "fixed"``,
``breach_rule = "consecutive"`` — the rules up to v1.4.0, which every port
keeps selectable):

* **LG01** — the v1.4.0 LC01 script: research evidence with no
  significance threshold passes the fixed 3.0 gate, and six consecutive
  breaches retire the alpha.  The correlation gate is a row of the table
  under either policy, so the script carries the empty ``cross_alpha``
  block too; ``net_pnl_bootstrap_ci`` is ABSENT under the legacy policy
  (``net_pnl_ci_gate = "absent"``), so the script carries no
  ``pnl_bootstrap`` block and is promoted on ten gates.

Each golden step records the action, the input evidence document and the
expected state / outcome / gate results / counters / CUSUM statistic /
transition after it.  Event times are ``T0 + k * STEP_NS`` (15-minute
adaptive blocks).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from iap.adaptive.lifecycle import LifecycleConfig
from iap.contracts.types import Actor, ExperimentResult, LifecycleState, Verdict
from iap.lifecycle.config import PolicyConfig
from iap.lifecycle.evidence import (
    CrossAlphaEvidence,
    CrossAlphaPeer,
    Evidence,
    LiveEvidence,
    PaperEvidence,
    PnlBootstrapEvidence,
    ValidationEvidence,
)
from iap.lifecycle.machine import AlphaLifecycle, transition_table
from iap.lifecycle.registry import AlphaRegistry, Outcome

__all__ = [
    "GOLDEN_X_VERSION",
    "STEP_NS",
    "T0",
    "ScriptStep",
    "golden_document",
    "legacy_config",
    "legacy_scripts",
    "render_golden",
    "run_scripts",
    "scripts",
]

#: 2 since v1.5.0: the evidence carries ``significance_threshold`` and
#: ``live.new_fraction``, the config ``tstat_threshold`` and the retirement
#: rule, every expected row ``cusum``; a ``legacy`` section pins the rules
#: up to v1.4.0.  3: the evidence carries ``cross_alpha``, the config
#: ``cross_alpha_min_state`` and ``gates.max_cross_alpha_correlation``, the
#: CANDIDATE -> VALIDATING edge the ``cross_alpha_correlation`` gate, and
#: scenario LC05 pins it; likewise ``pnl_bootstrap``, ``net_pnl_ci_gate``,
#: ``gates.net_pnl_ci_level`` / ``min_net_pnl_ci_low``, the
#: ``net_pnl_bootstrap_ci`` gate and LC06.
GOLDEN_X_VERSION = 3
#: Share of a 2 h rolling-IC window that one 15-minute block renews — the
#: ``new_fraction`` of a live reading in the scripts.
BLOCK_FRACTION = 0.125
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
    "LC04": LifecycleState.RETIRED,
    "LC05": LifecycleState.VALIDATING,
    "LC06": LifecycleState.VALIDATING,
    "LG01": LifecycleState.RESEARCH,
}

#: "There is no other alpha": the vacuous pass of the correlation gate.
NO_PEERS = CrossAlphaEvidence(())


def bootstrap(
    ci_low: float | None = 12.5,
    ci_high: float | None = 90.0,
    *,
    level: float = 0.95,
    n_trades: int = 40,
    n_bars: int = 626,
) -> PnlBootstrapEvidence:
    """A P&L bootstrap block; the defaults are an interval above zero."""
    return PnlBootstrapEvidence(
        ci_low=ci_low,
        ci_high=ci_high,
        level=level,
        n_resamples=1000,
        seed=20260829,
        mean_block=9.0,
        n_bars=n_bars,
        n_trades=n_trades,
    )


#: An interval whose lower bound is above zero: the gate passes.
GOOD_BOOTSTRAP = bootstrap()
#: "absent" in a script call: the evidence carries no ``pnl_bootstrap`` block.
NO_BOOTSTRAP = None
_DEFAULT = object()


@dataclass(frozen=True)
class ScriptStep:
    """One scripted action: ``advance`` with evidence, or a HUMAN
    ``retire`` / ``reset`` with a reason."""

    action: str
    evidence: Evidence | None
    reason: str | None
    note: str

    def __post_init__(self) -> None:
        if self.action not in ("advance", "retire", "reset"):
            raise ValueError(f"unknown script action {self.action!r}")
        if (self.action == "advance") != (self.evidence is not None):
            raise ValueError("advance steps carry evidence; manual steps do not")
        if (self.action != "advance") != (self.reason is not None):
            raise ValueError("manual steps carry a reason; advance steps do not")


def research(
    alpha_id: str,
    *,
    ic: float = 0.02,
    rank_ic: float = 0.03,
    t_stat: float = 4.0,
    fold_consistency: float = 1.0,
    n_folds: int = 4,
    leakage_passed: bool = True,
    hypothesis: bool | None = True,
    net_bps: float = 5.0,
    cost_bps: float = 7.0,
    n_ledger: int = 10,
    verdict: Verdict = Verdict.PROMOTE,
) -> ExperimentResult:
    """A research result with simple decimal metrics."""
    return ExperimentResult(
        experiment_id=f"{alpha_id.lower()}-research",
        alpha_id=alpha_id,
        dataset_version=_DATASET,
        feature_version=_FEATURES,
        model_version=None,
        ic=ic,
        rank_ic=rank_ic,
        t_stat=t_stat,
        nw_lags=2,
        hit_rate=0.55,
        turnover=40.0,
        gross_return_bps=net_bps + cost_bps,
        transaction_cost_bps=cost_bps,
        net_return_bps=net_bps,
        max_drawdown_bps=20.0,
        sharpe=1.5,
        fold_consistency=fold_consistency,
        n_folds=n_folds,
        leakage_passed=leakage_passed,
        leakage_detail={"shift_ok": leakage_passed, "label_guard_ok": True},
        hypothesis_sign_confirmed=hypothesis,
        verdict=verdict,
        n_experiments_in_ledger=n_ledger,
        git_commit=_GIT,
        created_ts=T0,
    )


def _ev(
    research_result: ExperimentResult | None = None,
    capacity: float | None = None,
    validation: ValidationEvidence | None = None,
    paper: PaperEvidence | None = None,
    live: LiveEvidence | None = None,
    threshold: float | None = None,
    cross_alpha: CrossAlphaEvidence | None = None,
    pnl_bootstrap: Any = _DEFAULT,
) -> Evidence:
    """Evidence with the named blocks.  Candidate evidence (a research
    result WITH a capacity) carries :data:`GOOD_BOOTSTRAP` unless the caller
    says otherwise; everything else carries no bootstrap block."""
    if pnl_bootstrap is _DEFAULT:
        candidate = research_result is not None and capacity is not None
        pnl_bootstrap = GOOD_BOOTSTRAP if candidate else None
    return Evidence(
        research=research_result,
        capacity_usd=capacity,
        validation=validation,
        paper=paper,
        live=live,
        significance_threshold=threshold,
        cross_alpha=cross_alpha,
        pnl_bootstrap=pnl_bootstrap,
    )


def _peers(*rows: tuple[str, str, float]) -> CrossAlphaEvidence:
    """A cross-alpha block from ``(alpha_id, state name, correlation)`` rows."""
    return CrossAlphaEvidence(tuple(CrossAlphaPeer(a, s, rho) for a, s, rho in rows))


def _live(
    rolling_ic: float | None,
    eval_index: int,
    informative: bool = True,
    n_buckets: int = 8,
    new_fraction: float = BLOCK_FRACTION,
) -> LiveEvidence:
    return LiveEvidence(
        rolling_ic=rolling_ic,
        n_buckets=n_buckets,
        eval_index=eval_index,
        informative=informative,
        new_fraction=new_fraction,
    )


def _adv(evidence: Evidence, note: str) -> ScriptStep:
    return ScriptStep("advance", evidence, None, note)


def _lc01() -> tuple[ScriptStep, ...]:
    good = research("LC01")
    validation_ok = ValidationEvidence(
        holdout_ic=0.018, research_ic=0.02, replay_hash_match=True, parity=True
    )
    paper_ok = PaperEvidence(
        n_sessions=5,
        realized_ic=0.015,
        research_ic=0.02,
        net_pnl=100.0,
        n_kill_events=0,
        tracking_error=0.001,
    )
    steps: list[ScriptStep] = [
        _adv(_ev(good), "research result present and clean -> CANDIDATE"),
        _adv(
            _ev(good, capacity=5_000_000.0, threshold=3.5, cross_alpha=NO_PEERS),
            "all eleven promotion gates pass (t 4.0 >= ledger threshold 3.5) -> VALIDATING",
        ),
        _adv(
            _ev(validation=validation_ok), "held-out replay tracks research, hash + parity -> PAPER"
        ),
        _adv(_ev(paper=paper_ok), "five clean paper sessions -> ACTIVE"),
        _adv(_ev(live=_live(0.02, 1)), "healthy rolling IC: hold ACTIVE, CUSUM stays 0"),
        _adv(
            _ev(live=_live(None, 2, n_buckets=2)), "null rolling IC (too few buckets): no evidence"
        ),
        _adv(
            _ev(live=_live(-0.01, 3, informative=False)),
            "uninformative re-read of a breach: no evidence",
        ),
        _adv(_ev(live=_live(-0.01, 4)), "breach -> WATCH; CUSUM starts accumulating"),
        _adv(_ev(live=_live(-0.01, 5)), "second breach: CUSUM grows, still under its threshold"),
        _adv(_ev(live=_live(0.002, 6)), "neutral zone [0.0, 0.005): CUSUM drains, no recovery"),
        _adv(_ev(live=_live(0.01, 7)), "recovery 1"),
        _adv(_ev(live=_live(0.01, 8)), "recovery 2"),
        _adv(_ev(live=_live(0.01, 9)), "recovery 3 -> ACTIVE (re-activation); CUSUM reset"),
        _adv(_ev(live=_live(-0.02, 10)), "relapse -> WATCH"),
    ]
    for k, idx in enumerate(range(11, 15), start=2):
        note = f"breach {k} since the relapse" + (
            ": CUSUM reaches its threshold on a breach -> RETIRED" if k == 5 else ""
        )
        steps.append(_adv(_ev(live=_live(-0.02, idx)), note))
    steps.append(
        _adv(_ev(live=_live(0.05, 15)), "SYSTEM advance on a RETIRED alpha: terminal, no movement")
    )
    steps.append(
        ScriptStep(
            "reset", None, "re-research after regime change", "HUMAN reset RETIRED -> RESEARCH"
        )
    )
    return tuple(steps)


def _lc02() -> tuple[ScriptStep, ...]:
    clean = research("LC02", ic=0.015, rank_ic=0.02, t_stat=3.5, verdict=Verdict.PROMOTE)
    leaking = research(
        "LC02", ic=0.15, rank_ic=0.2, t_stat=30.0, leakage_passed=False, verdict=Verdict.REJECT
    )
    return (
        _adv(_ev(clean), "clean research -> CANDIDATE"),
        _adv(
            _ev(leaking, capacity=5_000_000.0, threshold=3.25, cross_alpha=NO_PEERS),
            "re-run shows leakage: leakage_clean fails -> demoted to RESEARCH",
        ),
        _adv(_ev(leaking), "leaking result at RESEARCH: ledger passes, leakage fails, hold"),
        _adv(Evidence.empty(), "no evidence at RESEARCH: presence gate fails (value null), hold"),
    )


def _lc03() -> tuple[ScriptStep, ...]:
    good = research("LC03", ic=0.03, rank_ic=0.04, t_stat=5.0, net_bps=8.0, cost_bps=4.0)
    validation_bad = ValidationEvidence(
        holdout_ic=0.028, research_ic=0.03, replay_hash_match=True, parity=False
    )
    validation_ok = ValidationEvidence(
        holdout_ic=0.028, research_ic=0.03, replay_hash_match=True, parity=True
    )
    paper_bad = PaperEvidence(
        n_sessions=2,
        realized_ic=0.01,
        research_ic=0.03,
        net_pnl=-50.0,
        n_kill_events=1,
        tracking_error=0.002,
    )
    return (
        _adv(_ev(good), "research present and clean -> CANDIDATE"),
        _adv(
            _ev(good, capacity=2_000_000.0, threshold=5.5, cross_alpha=NO_PEERS),
            "t 5.0 below the evidence's ledger threshold 5.5: significance fails, hold",
        ),
        _adv(
            _ev(good, capacity=2_000_000.0, cross_alpha=NO_PEERS),
            "no significance threshold in the evidence: the gate fails, threshold null",
        ),
        _adv(
            _ev(good, capacity=2_000_000.0, threshold=2.0, cross_alpha=NO_PEERS),
            "a threshold below the floor: max(3.0, 2.0) = 3.0 applies -> VALIDATING",
        ),
        _adv(_ev(validation=validation_bad), "parity fails: consecutive_failures 1"),
        _adv(_ev(good), "validation block absent: silence, counter unchanged"),
        _adv(_ev(validation=validation_ok), "validation passes -> PAPER, counter reset"),
        _adv(_ev(paper=paper_bad), "paper gates fail: consecutive_failures 1"),
        _adv(_ev(paper=paper_bad), "paper gates fail: consecutive_failures 2"),
        _adv(_ev(paper=paper_bad), "third failure = max_consecutive_failures -> CANDIDATE"),
        ScriptStep(
            "retire",
            None,
            "manual retirement: hypothesis withdrawn by the desk",
            "HUMAN retire CANDIDATE -> RETIRED",
        ),
        _adv(_ev(good, capacity=2_000_000.0, threshold=4.5), "SYSTEM advance on RETIRED: terminal"),
    )


def _to_active(
    alpha_id: str, threshold: float | None, pnl_bootstrap: Any = _DEFAULT
) -> list[ScriptStep]:
    """The four promotion steps RESEARCH -> ACTIVE with clean evidence."""
    good = research(alpha_id)
    validation_ok = ValidationEvidence(
        holdout_ic=0.018, research_ic=0.02, replay_hash_match=True, parity=True
    )
    paper_ok = PaperEvidence(
        n_sessions=5,
        realized_ic=0.015,
        research_ic=0.02,
        net_pnl=100.0,
        n_kill_events=0,
        tracking_error=0.001,
    )
    return [
        _adv(_ev(good), "research result present and clean -> CANDIDATE"),
        _adv(
            _ev(
                good,
                capacity=5_000_000.0,
                threshold=threshold,
                cross_alpha=NO_PEERS,
                pnl_bootstrap=pnl_bootstrap,
            ),
            "promotion gates -> VALIDATING",
        ),
        _adv(_ev(validation=validation_ok), "validation -> PAPER"),
        _adv(_ev(paper=paper_ok), "paper -> ACTIVE"),
    ]


def _lc04() -> tuple[ScriptStep, ...]:
    steps = _to_active("LC04", 3.5)
    steps.append(_adv(_ev(live=_live(-0.001, 1)), "a breach inside the slack -> WATCH, CUSUM 0"))
    for idx in range(2, 9):
        steps.append(
            _adv(
                _ev(live=_live(-0.001, idx)),
                f"breach {idx} inside the slack: CUSUM stays 0, no retirement"
                + (" (the consecutive rule retires here)" if idx == 6 else ""),
            )
        )
    steps += [
        _adv(
            _ev(live=_live(-0.0135, 9, new_fraction=1.0)),
            "a deep breach in a disjoint window: CUSUM 0.011 >= 0.01 on a breach -> RETIRED",
        ),
        ScriptStep("reset", None, "second look after the retirement", "HUMAN reset -> RESEARCH"),
    ]
    steps += _to_active("LC04", 3.5)
    steps += [
        _adv(_ev(live=_live(-0.06, 10)), "deep breach -> WATCH, CUSUM 0.0071875"),
        _adv(
            _ev(live=_live(-0.06, 11)),
            "second deep breach: CUSUM 0.014375 >= 0.01 on a breach -> RETIRED",
        ),
        ScriptStep("reset", None, "third look", "HUMAN reset -> RESEARCH"),
    ]
    steps += _to_active("LC04", 3.5)
    steps += [
        _adv(
            _ev(live=_live(-0.09, 12)),
            "a breach deep enough to put CUSUM over its threshold at once -> WATCH only: "
            "the entering reading never retires",
        ),
        _adv(
            _ev(live=_live(0.004, 13)),
            "a reading ABOVE the gate with CUSUM still over its threshold: no retirement",
        ),
        _adv(
            _ev(live=_live(-0.003, 14)),
            "the next breach, with CUSUM over its threshold -> RETIRED",
        ),
    ]
    return tuple(steps)


def _lc05() -> tuple[ScriptStep, ...]:
    """The cross-alpha correlation gate (module docs)."""
    good = research("LC05")

    def candidate(cross_alpha: CrossAlphaEvidence | None) -> Evidence:
        return _ev(good, capacity=5_000_000.0, threshold=3.5, cross_alpha=cross_alpha)

    def again(reason: str) -> list[ScriptStep]:
        return [
            ScriptStep("retire", None, reason, "HUMAN retire VALIDATING -> RETIRED"),
            ScriptStep("reset", None, reason, "HUMAN reset RETIRED -> RESEARCH"),
            _adv(_ev(good), "research result present and clean -> CANDIDATE"),
        ]

    steps: list[ScriptStep] = [
        _adv(_ev(good), "research result present and clean -> CANDIDATE"),
        _adv(
            candidate(None),
            "no cross_alpha block: the correlation gate fails closed (value null), hold",
        ),
        _adv(
            candidate(_peers(("LC01", "ACTIVE", 0.82))),
            "correlated 0.82 with an ACTIVE alpha: above 0.7, the gate fails, hold",
        ),
        _adv(
            candidate(
                _peers(
                    ("LC01", "ACTIVE", -0.9),
                    ("LC02", "CANDIDATE", 0.95),
                    ("LC03", "RETIRED", 0.99),
                )
            ),
            "the mirror image of an ACTIVE alpha (-0.9): |correlation| is gated; "
            "the CANDIDATE and RETIRED peers are not counted, hold",
        ),
        _adv(
            candidate(_peers(("LC02", "CANDIDATE", 0.95), ("LC03", "RETIRED", 0.99))),
            "no peer at or beyond VALIDATING: value 0.0, vacuous pass -> VALIDATING",
        ),
    ]
    steps += again("second candidate evaluation of the correlation gate")
    steps.append(
        _adv(
            candidate(_peers(("LC01", "PAPER", 0.7), ("LC04", "WATCH", -0.7))),
            "two eligible peers at exactly the threshold (0.7, -0.7): max is inclusive, "
            "the tie passes -> VALIDATING",
        )
    )
    steps += again("third candidate evaluation of the correlation gate")
    steps.append(
        _adv(candidate(NO_PEERS), "an empty peer list: value 0.0, vacuous pass -> VALIDATING")
    )
    steps += again("fourth candidate evaluation of the correlation gate")
    steps.append(
        _adv(
            candidate(_peers(("LC01", "ACTIVE", 0.25), ("LC04", "VALIDATING", -0.4))),
            "additive: the largest |correlation| with an eligible peer is 0.4 -> VALIDATING",
        )
    )
    return tuple(steps)


def _lc06() -> tuple[ScriptStep, ...]:
    """The net P&L bootstrap gate (module docs)."""
    good = research("LC06")

    def candidate(boot: PnlBootstrapEvidence | None) -> Evidence:
        return _ev(
            good, capacity=5_000_000.0, threshold=3.5, cross_alpha=NO_PEERS, pnl_bootstrap=boot
        )

    return (
        _adv(_ev(good), "research result present and clean -> CANDIDATE"),
        _adv(candidate(None), "no pnl_bootstrap block: the gate fails closed (value null), hold"),
        _adv(
            candidate(bootstrap(-35.5, 60.25)),
            "the interval spans zero (lower bound -35.5): the gate fails at that value, hold",
        ),
        _adv(
            candidate(bootstrap(0.0, 0.0, n_trades=0)),
            "no trade in any fold: the degenerate [0, 0] interval is not evidence "
            "(value null), hold",
        ),
        _adv(
            candidate(bootstrap(None, None, n_bars=5)),
            "five bars: no interval exists (bounds null), value null, hold",
        ),
        _adv(
            candidate(bootstrap(12.5, 90.0, level=0.9)),
            "an interval at level 0.9 under a policy that requires 0.95: value null, hold",
        ),
        _adv(
            candidate(bootstrap(0.0, 45.0)),
            "a lower bound of exactly 0.0: the comparison is strict, the gate fails, hold",
        ),
        _adv(
            candidate(bootstrap(12.5, 90.0)),
            "lower bound 12.5 > 0.0 at level 0.95 with 40 trades: all gates pass -> VALIDATING",
        ),
    )


def _lg01() -> tuple[ScriptStep, ...]:
    """The v1.4.0 LC01 script (run under :func:`legacy_config`)."""
    steps: list[ScriptStep] = _to_active("LG01", None, NO_BOOTSTRAP)
    steps += [
        _adv(_ev(live=_live(0.02, 1)), "healthy rolling IC: hold ACTIVE"),
        _adv(
            _ev(live=_live(None, 2, n_buckets=2)), "null rolling IC (too few buckets): no evidence"
        ),
        _adv(
            _ev(live=_live(-0.01, 3, informative=False)),
            "uninformative re-read of a breach: no evidence",
        ),
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
    steps.append(
        _adv(_ev(live=_live(0.05, 16)), "SYSTEM advance on a RETIRED alpha: terminal, no movement")
    )
    steps.append(
        ScriptStep(
            "reset", None, "re-research after regime change", "HUMAN reset RETIRED -> RESEARCH"
        )
    )
    return tuple(steps)


def scripts() -> dict[str, tuple[ScriptStep, ...]]:
    """The scenario scripts of the default policy, keyed by alpha id (sorted)."""
    return {
        "LC01": _lc01(),
        "LC02": _lc02(),
        "LC03": _lc03(),
        "LC04": _lc04(),
        "LC05": _lc05(),
        "LC06": _lc06(),
    }


def legacy_scripts() -> dict[str, tuple[ScriptStep, ...]]:
    """The scenario scripts run under :func:`legacy_config`."""
    return {"LG01": _lg01()}


def legacy_config(config: PolicyConfig) -> PolicyConfig:
    """``config`` under the rules up to v1.4.0: the fixed significance
    threshold, the consecutive-breach retirement rule and no net P&L
    bootstrap gate (same thresholds)."""
    live = config.live
    return replace(
        config,
        tstat_threshold="fixed",
        net_pnl_ci_gate="absent",
        live=LifecycleConfig.legacy(
            live.watch_ic_gate,
            live.reactivate_ic_gate,
            live.retire_breach_evals,
            live.reactivate_evals,
        ),
    )


def _run_script(
    alpha_id: str, steps: tuple[ScriptStep, ...], config: PolicyConfig
) -> list[dict[str, Any]]:
    registry = AlphaRegistry(config.policy)
    machine = AlphaLifecycle(config, registry)
    machine.register(alpha_id, T0)
    out: list[dict[str, Any]] = []
    for k, step in enumerate(steps):
        event_ts = T0 + (k + 1) * STEP_NS
        row: dict[str, Any] = {
            "step": k,
            "note": step.note,
            "action": step.action,
            "event_ts": event_ts,
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
            transition = machine.reset_to_research(
                alpha_id, event_ts, step.reason, actor=Actor.HUMAN
            )
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
            "cusum": rec.cusum,
            "transition": None if transition is None else transition.to_dict(),
        }
        out.append(row)
    final = registry.get(alpha_id).state
    if final is not EXPECTED_FINAL_STATES[alpha_id]:
        raise RuntimeError(
            f"{alpha_id}: script ended in {final.name}, designed to end in "
            f"{EXPECTED_FINAL_STATES[alpha_id].name}"
        )
    return out


def run_scripts(
    config: PolicyConfig, which: Mapping[str, tuple[ScriptStep, ...]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Run every script (default: :func:`scripts`) through a fresh machine
    under ``config``; ``{alpha_id: steps}``."""
    todo = scripts() if which is None else which
    return {aid: _run_script(aid, steps, config) for aid, steps in sorted(todo.items())}


def golden_document(config: PolicyConfig) -> dict[str, Any]:
    """The golden document (JSON-ready, key order pinned)."""
    return {
        "x-version": GOLDEN_X_VERSION,
        "description": (
            "Alpha promotion lifecycle golden (iap.lifecycle). `config` is the merged "
            "policy (configs/strategies/lifecycle.json + strategies.json adaptive."
            "lifecycle); `transition_table` the allowed edges with their gates in "
            "evaluation order; `states` the integer ids; each scenario lists per step "
            "the action, the input evidence, and the expected state / outcome / gate "
            "results / counters / CUSUM statistic / transition after it. `legacy` holds "
            "the same machine under the rules up to v1.4.0 (tstat_threshold fixed, "
            "breach_rule consecutive, net_pnl_ci_gate absent) with its own config and "
            "scenarios. Generated by "
            "python/tools/make_golden_lifecycle.py; every port reproduces every step "
            "exactly (states, booleans and decimals are exact; no tolerance)."
        ),
        "t0": T0,
        "step_ns": STEP_NS,
        "config": config.to_dict(),
        "states": {s.name: int(s) for s in LifecycleState},
        "transition_table": transition_table(),
        "scenarios": run_scripts(config),
        "legacy": {
            "config": legacy_config(config).to_dict(),
            "scenarios": run_scripts(legacy_config(config), legacy_scripts()),
        },
    }


def render_golden(doc: Mapping[str, Any]) -> str:
    """The exact bytes of the golden file (2-space indent, ASCII, insertion
    key order, trailing newline)."""
    return json.dumps(doc, indent=2, ensure_ascii=True, sort_keys=False, allow_nan=False) + "\n"
