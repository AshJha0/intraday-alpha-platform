"""Result queries of the MVP engine: P&L identity, research labels and the
realized IC.  Split out of :class:`~iap.mvp.engine.MvpEngine` as a mixin
(v1.13 E4); no behaviour change.
"""

from __future__ import annotations

from iap.labels.labels import HORIZONS_NS, LabelResult, compute_labels, max_sample_age
from iap.mvp.metrics import IcResult, pearson
from iap.risk.events import Decision as RiskDecisionCode

_PNL_IDENTITY_TOL = 1e-9


class EngineMetricsMixin:
    """Mixin of :class:`~iap.mvp.engine.MvpEngine` (module docstring); it
    reads and writes the engine's own attributes."""

    # -------------------------------------------------------------- results

    def risk_daily_pnl(self) -> float:
        """The risk engine's daily P&L (realized + unrealized, reporting ccy)."""
        pnl = self.risk_engine.global_daily_pnl()
        if pnl is None:
            raise RuntimeError("risk daily P&L undeterminable (missing conversion rate)")
        return pnl

    def assert_pnl_identity(self) -> None:
        """§12.1: risk daily P&L == gross - spread_cost; the risk position ==
        the account position."""
        acct = self.account
        if self.risk_engine.position(self.iid) != acct.position:
            raise RuntimeError(
                f"risk position {self.risk_engine.position(self.iid)} != "
                f"account position {acct.position}"
            )
        lhs = self.risk_daily_pnl()
        rhs = acct.gross_pnl - acct.spread_cost
        if abs(lhs - rhs) > _PNL_IDENTITY_TOL * max(1.0, abs(lhs), abs(rhs)):
            raise RuntimeError(f"P&L identity broken: risk daily {lhs!r} != gross - spread {rhs!r}")

    def labels(self, horizon: str) -> LabelResult:
        """The research forward labels (:func:`iap.labels.compute_labels`) at
        every decision timestamp for one pinned ``horizon``, computed over
        the stream observed so far (cached per observed extent)."""
        if horizon not in HORIZONS_NS:
            raise ValueError(f"unknown horizon {horizon!r}")
        last_ts = self.last_ts if self.last_ts is not None else 0
        key = (len(self.decision_ts), len(self.mid_series), last_ts)
        cached = self._labels.get(horizon)
        if cached is not None and cached[0] == key:
            return cached[1]
        result = compute_labels(
            self.decision_ts,
            self.mid_series,
            last_ts,
            [horizon],
            max_age_ns=max_sample_age(self.mid_series),
        )[horizon]
        self._labels[horizon] = (key, result)
        return result

    def realized_pairs(self, alpha_id: str, horizon: str) -> list[tuple[int, float, float, float]]:
        """``(decision index, expected_return, label_mid, label_cost)`` of the
        alpha's confidence > 0 decisions whose label at ``horizon`` is valid,
        in decision order."""
        lab = self.labels(horizon)
        out: list[tuple[int, float, float, float]] = []
        for index, er in self.ic_samples[alpha_id]:
            if lab.valid[index]:
                out.append((index, er, lab.mid[index], lab.cost[index]))
        return out

    def realized_ic(self, alpha_id: str, horizon: str | None = None) -> IcResult:
        """Realized IC of ``alpha_id`` at ``horizon`` (default: the MVP
        holding horizon) — see the module docstring for the definition."""
        h = self.horizon if horizon is None else horizon
        pairs = self.realized_pairs(alpha_id, h)
        xs = [er for _, er, _, _ in pairs]
        ys = [m for _, _, m, _ in pairs]
        cs = [c for _, _, _, c in pairs]
        # Shift-by-one: the signal of the PREVIOUS confidence > 0 decision
        # against this decision's label (conventions §7).
        signals = [er for _, er in self.ic_samples[alpha_id]]
        position = {index: k for k, (index, _) in enumerate(self.ic_samples[alpha_id])}
        xs_shift: list[float] = []
        ys_shift: list[float] = []
        for index, _, m, _ in pairs:
            k = position[index]
            if k > 0:
                xs_shift.append(signals[k - 1])
                ys_shift.append(m)
        return IcResult(
            horizon=h,
            ic=pearson(xs, ys),
            ic_cost=pearson(xs, cs),
            ic_shifted=pearson(xs_shift, ys_shift),
            n=len(pairs),
            n_signals=len(self.ic_samples[alpha_id]),
        )

    def n_kill_events(self) -> int:
        return sum(1 for e in self.risk_engine.audit() if e.decision == RiskDecisionCode.KILL)

    def risk_decisions_by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.risk_engine.audit():
            out[e.rule_id] = out.get(e.rule_id, 0) + 1
        return dict(sorted(out.items()))
