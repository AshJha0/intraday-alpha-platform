"""Shadow mode (v1.11, AI3): run a candidate model beside the champion.

:class:`ShadowRunner` scores the same frames with both models.  The
decision it returns is **always the champion's** — computed from the
champion alone, before the candidate is scored, and returned as a fresh
array — so a shadow candidate (even one that raises) can never change what
trades.  Both scores and both would-be decisions are recorded per frame.

:meth:`ShadowRunner.compare` scores the record against realised outcomes:
Spearman IC, hit rate, calibration (classifiers) and the P&L delta of the
candidate's would-be decisions over the champion's on per-row backtest
P&L (``pnl[i]`` = the P&L row ``i`` would earn if allowed, e.g. the maker
backtest's per-quote markout).

:func:`promotion_decision` never promotes on shadow numbers alone: the
candidate must beat the champion **and** the alpha's evidence must pass
every gate of the lifecycle PROMOTION edge into ``to_state``
(:mod:`iap.lifecycle.gates`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from iap.mlops.monitoring import calibration, spearman_ic


def _scorer(model: Any):
    if hasattr(model, "score") and hasattr(model, "tau"):  # MakerFilter-like
        return model.score, float(model.tau)
    if hasattr(model, "predict_proba"):
        return (lambda X: model.predict_proba(X)[:, 1]), 0.5
    return (lambda X: np.asarray(model.predict(X), dtype=float)), 0.0


@dataclass
class ShadowRunner:
    champion: Any
    candidate: Any
    champion_id: str | None = None
    candidate_id: str | None = None
    kind: str = "classifier"
    champion_scores: list = field(default_factory=list)
    candidate_scores: list = field(default_factory=list)
    champion_allow: list = field(default_factory=list)
    candidate_allow: list = field(default_factory=list)
    candidate_errors: list = field(default_factory=list)

    def step(self, X) -> np.ndarray:
        """Score one frame; return the champion's decision (bool mask)."""
        X = np.asarray(X, dtype=float)
        score_c, tau_c = _scorer(self.champion)
        s_ch = np.asarray(score_c(X), dtype=float)
        decision = np.isfinite(s_ch) & (s_ch >= tau_c)
        out = decision.copy()
        try:
            score_k, tau_k = _scorer(self.candidate)
            s_ca = np.asarray(score_k(X.copy()), dtype=float)
        except Exception as exc:  # the shadow must never break the champion
            self.candidate_errors.append(repr(exc))
            s_ca = np.full(s_ch.shape, np.nan)
            tau_k = np.inf
        self.champion_scores.append(s_ch)
        self.candidate_scores.append(s_ca)
        self.champion_allow.append(decision)
        self.candidate_allow.append(np.isfinite(s_ca) & (s_ca >= tau_k))
        return out

    def run(self, frames) -> list[np.ndarray]:
        return [self.step(X) for X in frames]

    def _cat(self, name: str) -> np.ndarray:
        parts = getattr(self, name)
        return np.concatenate(parts) if parts else np.empty(0)

    def compare(self, y, pnl=None) -> ShadowComparison:
        y = np.asarray(y, dtype=float).ravel()
        sc, sk = self._cat("champion_scores"), self._cat("candidate_scores")
        ac = self._cat("champion_allow").astype(bool)
        ak = self._cat("candidate_allow").astype(bool)
        if y.size != sc.size:
            raise ValueError(f"y has {y.size} rows, shadow record has {sc.size}")

        def hit(allow: np.ndarray) -> float | None:
            m = allow & np.isfinite(y)
            return float((y[m] > 0).mean()) if m.any() else None

        def side(s, allow) -> dict:
            out = {"ic": spearman_ic(s, y), "hit_rate": hit(allow), "n_allowed": int(allow.sum())}
            if self.kind == "classifier":
                cal = calibration(s, y)
                out.update(brier=cal["brier"], ece=cal["ece"])
            return out

        champion, candidate = side(sc, ac), side(sk, ak)
        pnl_delta = None
        if pnl is not None:
            p = np.nan_to_num(np.asarray(pnl, dtype=float).ravel())
            champion["pnl"] = float(p[ac].sum())
            candidate["pnl"] = float(p[ak].sum())
            pnl_delta = candidate["pnl"] - champion["pnl"]
        return ShadowComparison(
            champion_id=self.champion_id,
            candidate_id=self.candidate_id,
            n=int(sc.size),
            champion=champion,
            candidate=candidate,
            pnl_delta=pnl_delta,
            agreement=float((ac == ak).mean()) if sc.size else None,
            candidate_errors=len(self.candidate_errors),
        )


@dataclass(frozen=True)
class ShadowComparison:
    champion_id: str | None
    candidate_id: str | None
    n: int
    champion: dict
    candidate: dict
    pnl_delta: float | None
    agreement: float | None
    candidate_errors: int

    def candidate_better(self) -> bool:
        """Candidate IC >= champion IC, no candidate errors, and (when P&L
        was supplied) a positive P&L delta."""
        ic_c, ic_k = self.champion.get("ic"), self.candidate.get("ic")
        if self.candidate_errors or ic_k is None:
            return False
        if ic_c is not None and ic_k < ic_c:
            return False
        return self.pnl_delta is None or self.pnl_delta > 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def promotion_decision(
    comparison: ShadowComparison,
    evidence,
    config=None,
    to_state: str = "ACTIVE",
    alpha_id: str = "",
) -> dict:
    """Promote only if the candidate beats the champion in shadow AND every
    gate of the lifecycle PROMOTION edge into ``to_state`` passes."""
    from iap.lifecycle.config import load_policy_config
    from iap.lifecycle.gates import build_gates
    from iap.lifecycle.machine import transition_table

    cfg = config or load_policy_config()
    edges = [
        r for r in transition_table() if r["kind"] == "PROMOTION" and r["to_state"] == to_state
    ]
    if not edges:
        raise ValueError(f"no PROMOTION edge into {to_state!r}")
    gates = build_gates(cfg)
    results = {}
    for g in edges[0]["gates"]:
        r = gates[g].evaluate(alpha_id, evidence)
        results[g] = {"passed": bool(r.passed), "value": r.value, "threshold": r.threshold}
    gates_ok = all(v["passed"] for v in results.values())
    better = comparison.candidate_better()
    return {
        "promote": bool(gates_ok and better),
        "candidate_better": better,
        "gates_passed": gates_ok,
        "edge": f"{edges[0]['from_state']}->{to_state}",
        "gates": results,
        "comparison": comparison.to_dict(),
    }
