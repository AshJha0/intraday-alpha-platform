"""The research workflow as one explicit graph.

hypothesis -> prior_art -> prereg_draft -> human_approval -> in_sample_run
-> triage -> narrative -> done, with exits ``invalid`` (bad hypothesis),
``duplicate_stop`` (prior art already ran the same test), ``rejected`` (the
human said no) and ``refused`` (the run named a holdout dataset).

Everything outside the graph comes in through :class:`ResearchDeps`:
prior-art search, the in-sample runner, the holdout predicate, and the
narrative writer and its verifier.  Tests and the demo pass stubs; no node
calls a model, the network or the file system.  The pre-registration draft
reuses the shape of :mod:`iap.governance.prereg_gate` (``prereg_id`` is
``digest({"alpha", "horizon"})[:16]`` and ``analysis_identity`` carries
data versions, family, metrics and thresholds) but is never signed or
registered: it is a dict in the state, and approving it here is not a
pre-registration on the blackboard.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from iap.governance.blackboard import digest
from iap.governance.untrusted import sanitise

from .core import Graph, Interrupt, Router

#: the horizons a hypothesis may name: the plain horizons the ledger already uses
#: (so a repeat of an old test can be submitted and stopped) and the bar horizons
HORIZONS = frozenset({"500ms", "1s", "5s", "10s", "30s", "1m", "5m", "15m", "30m", "60m", "1d"})
DEFAULT_THRESHOLDS = {"min_abs_t": 2.0, "min_sessions": 10}
VERDICTS = ("pass", "fail", "wrong_sign", "insufficient_data")


@dataclass(frozen=True)
class ResearchState:
    """The research flow's state.  Every field holds JSON values."""

    hypothesis: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    prior_hits: list = field(default_factory=list)
    duplicate_of: str | None = None
    draft: dict | None = None
    decision: dict | None = None
    run: dict | None = None
    refused: list = field(default_factory=list)
    verdict: dict | None = None
    narrative: str | None = None
    narrative_source: str | None = None
    outcome: str | None = None

    def check(self) -> None:
        for name in ("hypothesis",):
            if not isinstance(getattr(self, name), dict):
                raise TypeError(f"{name} must be an object")
        for name in ("errors", "prior_hits", "refused"):
            if not isinstance(getattr(self, name), list):
                raise TypeError(f"{name} must be a list")
        for name in ("draft", "decision", "run", "verdict"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), dict):
                raise TypeError(f"{name} must be an object or null")
        if self.decision is not None and not isinstance(self.decision.get("approved"), bool):
            raise ValueError("decision.approved must be true or false")
        if self.narrative_source not in (None, "model", "fallback"):
            raise ValueError(f"narrative_source {self.narrative_source!r}")
        if self.verdict is not None and self.verdict.get("verdict") not in VERDICTS:
            raise ValueError(f"verdict {self.verdict.get('verdict')!r}")


@dataclass(frozen=True)
class ResearchDeps:
    """The injected callables.

    ``search_prior(hypothesis) -> list[hit]``: each hit is a dict with at
    least ``id``; ``same_test: true`` marks a hit that already ran this test.
    ``run_in_sample(draft) -> dict`` returns ``t_stat``, ``mean_bps`` and
    ``n_sessions``.  ``is_holdout(name) -> bool``.  ``write(facts) -> str``
    is the (model) narrative writer and ``verify(text, facts) -> bool`` its
    checker."""

    search_prior: Callable[[Mapping[str, Any]], list]
    run_in_sample: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    is_holdout: Callable[[str], bool]
    write: Callable[[Mapping[str, Any]], str]
    verify: Callable[[str, Mapping[str, Any]], bool]
    thresholds: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))


def validate_hypothesis(h: Mapping[str, Any]) -> list[str]:
    """The problems with a hypothesis record (empty when it is valid)."""
    errors = []
    if not isinstance(h.get("alpha_id"), str) or not h.get("alpha_id"):
        errors.append("alpha_id must be a non-empty string")
    if h.get("horizon") not in HORIZONS:
        errors.append(f"horizon must be one of {sorted(HORIZONS)}")
    universe = h.get("universe")
    if not isinstance(universe, dict) or not universe.get("name"):
        errors.append("universe must be an object with a name")
    elif not isinstance(universe.get("datasets"), list) or not universe["datasets"]:
        errors.append("universe.datasets must be a non-empty list")
    if h.get("expected_sign") not in (1, -1):
        errors.append("expected_sign must be 1 or -1")
    mechanism = h.get("mechanism")
    if not isinstance(mechanism, str) or len(mechanism.strip()) < 20:
        errors.append("mechanism must be at least 20 characters of text")
    return errors


def triage(run: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
    """The verdict, computed by code from the run's numbers and the draft's
    thresholds.  Order: too few sessions, then wrong sign, then |t|."""
    th = draft["analysis_identity"]["thresholds"]
    t = float(run["t_stat"])
    n = int(run["n_sessions"])
    sign = int(draft["expected_sign"])
    if n < int(th["min_sessions"]):
        verdict = "insufficient_data"
    elif t * sign < 0:
        verdict = "wrong_sign"
    elif abs(t) >= float(th["min_abs_t"]):
        verdict = "pass"
    else:
        verdict = "fail"
    return {
        "verdict": verdict,
        "t_stat": t,
        "n_sessions": n,
        "expected_sign": sign,
        "min_abs_t": float(th["min_abs_t"]),
        "min_sessions": int(th["min_sessions"]),
    }


def fallback_narrative(facts: Mapping[str, Any]) -> str:
    """The code-written narrative used when the model text does not verify."""
    return (
        f"{facts['alpha_id']} at horizon {facts['horizon']}: in-sample t = "
        f"{facts['t_stat']:.2f} over {facts['n_sessions']} sessions (expected sign "
        f"{facts['expected_sign']:+d}, threshold |t| >= {facts['min_abs_t']:.2f}, "
        f"at least {facts['min_sessions']} sessions); verdict {facts['verdict']}. "
        "In-sample only; not a confirmatory result."
    )


def build_research_graph(deps: ResearchDeps) -> Graph:
    def hypothesis(s: ResearchState) -> dict:
        return {"errors": validate_hypothesis(s.hypothesis)}

    def invalid(s: ResearchState) -> dict:
        return {"outcome": "invalid"}

    def prior_art(s: ResearchState) -> dict:
        hits = [dict(h) for h in deps.search_prior(s.hypothesis)]
        same = sorted(str(h.get("id")) for h in hits if h.get("same_test") is True)
        return {"prior_hits": hits, "duplicate_of": same[0] if same else None}

    def duplicate_stop(s: ResearchState) -> dict:
        return {"outcome": "duplicate"}

    def prereg_draft(s: ResearchState) -> dict:
        h = s.hypothesis
        draft = {
            "kind": "prereg_draft",
            "signed": False,
            "prereg_id": digest({"alpha": h["alpha_id"], "horizon": h["horizon"]})[:16],
            "alpha": h["alpha_id"],
            "horizon": h["horizon"],
            "expected_sign": h["expected_sign"],
            "mechanism": sanitise(h["mechanism"]),
            "universe": h["universe"]["name"],
            "analysis_identity": {
                "data_versions": list(h["universe"]["datasets"]),
                "family": h["alpha_id"],
                "metrics": ["t_stat", "mean_bps", "n_sessions"],
                "thresholds": dict(deps.thresholds),
            },
            "prior_art": [h_["id"] for h_ in s.prior_hits],
        }
        return {"draft": draft}

    def human_approval(s: ResearchState) -> dict | Interrupt:
        if s.decision is None:
            return Interrupt(
                "human approval of the pre-registration draft",
                {"draft_sha256": digest(s.draft), "prereg_id": s.draft["prereg_id"]},
            )
        return {}

    def rejected(s: ResearchState) -> dict:
        return {"outcome": "rejected"}

    def in_sample_run(s: ResearchState) -> dict:
        datasets = s.draft["analysis_identity"]["data_versions"]
        holdout = sorted(d for d in datasets if deps.is_holdout(d))
        if holdout:
            return {"refused": holdout}
        out = dict(deps.run_in_sample(s.draft))
        return {
            "run": {
                "t_stat": float(out["t_stat"]),
                "mean_bps": float(out["mean_bps"]),
                "n_sessions": int(out["n_sessions"]),
            }
        }

    def refused(s: ResearchState) -> dict:
        return {"outcome": "refused_holdout"}

    def triage_node(s: ResearchState) -> dict:
        return {"verdict": triage(s.run, s.draft)}

    def narrative(s: ResearchState) -> dict:
        facts = {"alpha_id": s.draft["alpha"], "horizon": s.draft["horizon"], **s.verdict}
        try:
            text = sanitise(str(deps.write(facts)))
            ok = bool(text) and deps.verify(text, facts) is True
        except Exception:  # a failing writer or verifier falls back, never stops the run
            ok = False
        if ok:
            return {"narrative": text, "narrative_source": "model"}
        return {"narrative": fallback_narrative(facts), "narrative_source": "fallback"}

    def done(s: ResearchState) -> dict:
        return {"outcome": "done"}

    return Graph(
        name="research_flow",
        schema=ResearchState,
        nodes={
            "hypothesis": hypothesis,
            "invalid": invalid,
            "prior_art": prior_art,
            "duplicate_stop": duplicate_stop,
            "prereg_draft": prereg_draft,
            "human_approval": human_approval,
            "rejected": rejected,
            "in_sample_run": in_sample_run,
            "refused": refused,
            "triage": triage_node,
            "narrative": narrative,
            "done": done,
        },
        entry="hypothesis",
        edges={"prereg_draft": "human_approval", "triage": "narrative", "narrative": "done"},
        routers={
            "hypothesis": Router(
                lambda s: "invalid" if s.errors else "prior_art",
                frozenset({"invalid", "prior_art"}),
            ),
            "prior_art": Router(
                lambda s: "duplicate_stop" if s.duplicate_of else "prereg_draft",
                frozenset({"duplicate_stop", "prereg_draft"}),
            ),
            "human_approval": Router(
                lambda s: "in_sample_run" if s.decision["approved"] else "rejected",
                frozenset({"in_sample_run", "rejected"}),
            ),
            "in_sample_run": Router(
                lambda s: "refused" if s.refused else "triage",
                frozenset({"refused", "triage"}),
            ),
        },
        terminals={"invalid", "duplicate_stop", "rejected", "refused", "done"},
        decision_field="decision",
    )


# -- built-in stubs (the demo and the tests) -----------------------------------
DEMO_HYPOTHESIS = {
    "alpha_id": "demo_order_imbalance",
    "horizon": "5m",
    "universe": {"name": "demo_liquid_10", "datasets": ["demo_bars_2025q1"]},
    "expected_sign": 1,
    "mechanism": "Persistent order-flow imbalance precedes short-horizon drift in liquid names.",
}


def stub_deps(**overrides: Any) -> ResearchDeps:
    """Deterministic stand-ins: no prior art, a fixed in-sample result, no
    holdout, a writer that states the facts and a verifier that checks the
    verdict and t value appear in the text."""

    def write(facts: Mapping[str, Any]) -> str:
        return f"Stub writer: verdict {facts['verdict']} with t = {facts['t_stat']:.2f}."

    def verify(text: str, facts: Mapping[str, Any]) -> bool:
        return facts["verdict"] in text and f"{facts['t_stat']:.2f}" in text

    base: dict[str, Any] = {
        "search_prior": lambda h: [],
        "run_in_sample": lambda d: {"t_stat": 2.4, "mean_bps": 0.8, "n_sessions": 20},
        "is_holdout": lambda name: name.endswith("_holdout"),
        "write": write,
        "verify": verify,
    }
    base.update(overrides)
    return ResearchDeps(**base)
