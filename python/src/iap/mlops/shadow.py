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

v1.17 (review R-4): a shadow promotion is a LOOK.  ``compare(..., ts=)``
adds a paired day-block bootstrap of the candidate-minus-champion IC
(:func:`iap.validation.metrics.paired_day_block_bootstrap`, on the rank
scores the Spearman IC reads); when it is available the candidate must
also beat the champion at the bootstrap's 5th percentile (``p05 > 0``), and
``promotion_decision(..., ledger=)`` records the decision in the
experiments ledger (kind ``"shadow_promotion"``) so it enters the
multiple-testing denominator like every other look.
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

    def compare(self, y, pnl=None, ts=None, seed: int = 0, n_boot: int = 2000) -> ShadowComparison:
        """``ts`` (one event-time stamp per recorded row) turns on the
        paired day-block bootstrap of the IC delta (module docs, v1.17)."""
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
        ic_delta_bootstrap = None
        if ts is not None:
            from iap.validation.metrics import paired_day_block_bootstrap

            ts_arr = np.asarray(ts, dtype=np.int64).ravel()
            if ts_arr.size != sc.size:
                raise ValueError(f"ts has {ts_arr.size} rows, shadow record has {sc.size}")
            # Spearman = Pearson on ranks: rank scores and labels once on the
            # rows where both scores and the label are finite, so the
            # bootstrap re-correlates the quantity the IC reads (exactly the
            # Spearman IC of each side when neither side has gaps).
            m = np.isfinite(sc) & np.isfinite(sk) & np.isfinite(y)
            ic_delta_bootstrap = paired_day_block_bootstrap(
                ts_arr[m], _ranked(sc[m]), _ranked(sk[m]), _ranked(y[m]), seed=seed, n_boot=n_boot
            )
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
            ic_delta_bootstrap=ic_delta_bootstrap,
        )


def _ranked(s: np.ndarray) -> np.ndarray:
    """Ranks of the finite entries (NaN stays NaN), as the Spearman IC ranks."""
    out = np.full(s.shape, np.nan)
    m = np.isfinite(s)
    if m.any():
        out[m] = np.argsort(np.argsort(s[m], kind="stable"), kind="stable").astype(float)
    return out


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
    #: v1.17: paired day-block bootstrap of IC(candidate) - IC(champion)
    #: (``compare(ts=)``); ``None`` when no timestamps were given.
    ic_delta_bootstrap: dict | None = None

    def candidate_better(self) -> bool:
        """Candidate IC >= champion IC, no candidate errors, (when P&L was
        supplied) a positive P&L delta and (when the paired day-block
        bootstrap ran) an IC delta whose 5th percentile is above 0."""
        ic_c, ic_k = self.champion.get("ic"), self.candidate.get("ic")
        if self.candidate_errors or ic_k is None:
            return False
        if ic_c is not None and ic_k < ic_c:
            return False
        if self.pnl_delta is not None and not self.pnl_delta > 0.0:
            return False
        b = self.ic_delta_bootstrap
        if b is not None:
            p05 = b.get("p05")
            return p05 is not None and np.isfinite(p05) and float(p05) > 0.0
        return True

    def to_dict(self) -> dict:
        return asdict(self)


def promotion_decision(
    comparison: ShadowComparison,
    evidence,
    config=None,
    to_state: str = "ACTIVE",
    alpha_id: str = "",
    ledger=None,
) -> dict:
    """Promote only if the candidate beats the champion in shadow AND every
    gate of the lifecycle PROMOTION edge into ``to_state`` passes.

    ``ledger`` (an :class:`iap.validation.ledger.ExperimentLedger`, v1.17
    R-4) records the decision as one look of kind ``"shadow_promotion"``
    under ``alpha_id`` (the champion / candidate ids and the edge are its
    identity); the caller saves the ledger.  ``ledger_n`` in the result is
    the running total after the record."""
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
    out = {
        "promote": bool(gates_ok and better),
        "candidate_better": better,
        "gates_passed": gates_ok,
        "edge": f"{edges[0]['from_state']}->{to_state}",
        "gates": results,
        "comparison": comparison.to_dict(),
    }
    if ledger is not None:
        out["ledger_n"] = ledger.record(
            alpha_id or "shadow",
            "shadow_promotion",
            config={
                "champion_id": comparison.champion_id,
                "candidate_id": comparison.candidate_id,
                "edge": out["edge"],
                "rule": "candidate_better (IC, P&L delta, paired day-block bootstrap p05 > 0)"
                " and every PROMOTION gate",
            },
            result={
                "promote": out["promote"],
                "candidate_better": better,
                "gates_passed": gates_ok,
                "ic_champion": comparison.champion.get("ic"),
                "ic_candidate": comparison.candidate.get("ic"),
                "pnl_delta": comparison.pnl_delta,
                "ic_delta_bootstrap": comparison.ic_delta_bootstrap,
            },
        )
    return out
