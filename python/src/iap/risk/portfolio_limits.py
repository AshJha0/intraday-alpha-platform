"""Enforced portfolio limits and a latching kill switch (v1.18 REL-17).

A book-level layer ABOVE the order-level hard risk engine
(:mod:`iap.risk.engine`, the Rust-parity port, unchanged).  It reads a
declarative, versioned config (``configs/risk/portfolio_limits.json``,
``x-version`` 1, strict keys, ``limits_version`` recorded in every
decision) and answers one question for a proposed weight set or order:
does it breach any limit?  A breach is a REFUSAL (:class:`LimitRefused`),
never a warning, and the optimizer wrapper
(:func:`iap.portfolio.enforced.solve_enforced`) and :meth:`PortfolioGuard.
check_order` both go through it.

Weights are fractions of ``capital`` (signed notional / capital).

| limit (config key) | rule |
|---|---|
| ``gross_max`` | ``sum |w| <= gross_max`` |
| ``net_max`` | ``|sum w| <= net_max`` |
| ``name_max`` | ``|w_i| <= name_max`` for every name |
| ``sector_net_max`` | ``|sum_{i in s} w_i| <= sector_net_max`` per sector |
| ``sector_gross_max`` | ``sum_{i in s} |w_i| <= sector_gross_max`` per sector |
| ``beta_max`` | ``|sum beta_i w_i| <= beta_max`` (missing beta reads 1.0) |
| ``participation_max`` | ``|w_i - w_prev_i| * capital <= participation_max * adv_notional_i`` (liquidity-adjusted; a name with no ADV may not trade) |
| ``signal_share_max`` | each signal sleeve's gross <= ``signal_share_max`` x the book's gross |
| ``signal_corr_max`` | ex-ante correlation of any two sleeves (under ``Sigma``) <= ``signal_corr_max`` |
| ``daily_loss_limit`` | ``(equity_day_start - equity) / capital >= limit`` trips the kill switch |
| ``drawdown_stop`` | ``(peak_equity - equity) / capital >= stop`` trips the kill switch |

Any key set to ``null`` is inactive; an unknown key, a negative or
non-finite bound, or a wrong ``x-version`` fails the load.

**Kill switch.**  Tripped by a loss limit, by :meth:`PortfolioGuard.halt`
or by the monitoring layer; once tripped it LATCHES: every weight set and
every order is refused, a new trading day does not clear it, and only
:meth:`PortfolioGuard.reset` with a named operator and a reason clears it
(both recorded in ``history``).  A reset while a loss limit is still
breached is refused.

**Monitoring hook.**  :class:`HaltSink` is the minimal interface to the
REL-16 monitoring state: the guard calls ``report_halt(source, reason,
active)`` on every trip (``active=True``) and reset (``False``), so the
monitor can show HALT.  Any object with that method works; the default is
a no-op.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

__all__ = [
    "DEFAULT_LIMITS_PATH",
    "LIMIT_KEYS",
    "PORTFOLIO_LIMITS_VERSION",
    "Breach",
    "HaltSink",
    "LimitRefused",
    "PortfolioGuard",
    "PortfolioLimits",
    "check_signal_mix",
    "check_weights",
]

#: ``x-version`` of ``configs/risk/portfolio_limits.json`` this parser reads.
PORTFOLIO_LIMITS_VERSION = 1
DEFAULT_LIMITS_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "risk" / "portfolio_limits.json"
)
#: Every limit key, in the order checks run and breaches are listed.
LIMIT_KEYS = (
    "gross_max",
    "net_max",
    "name_max",
    "sector_net_max",
    "sector_gross_max",
    "beta_max",
    "participation_max",
    "signal_share_max",
    "signal_corr_max",
    "daily_loss_limit",
    "drawdown_stop",
)
_TOL = 1e-8


@dataclass(frozen=True)
class PortfolioLimits:
    """The declarative limit set (module docs); ``None`` = inactive."""

    limits_version: str
    gross_max: float | None = None
    net_max: float | None = None
    name_max: float | None = None
    sector_net_max: float | None = None
    sector_gross_max: float | None = None
    beta_max: float | None = None
    participation_max: float | None = None
    signal_share_max: float | None = None
    signal_corr_max: float | None = None
    daily_loss_limit: float | None = None
    drawdown_stop: float | None = None

    def __post_init__(self) -> None:
        if not self.limits_version:
            raise ValueError("portfolio limits: limits_version must be non-empty")
        for k in LIMIT_KEYS:
            v = getattr(self, k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"portfolio limits: {k} must be a number or null")
            if not math.isfinite(float(v)) or float(v) < 0:
                raise ValueError(f"portfolio limits: {k} must be finite and >= 0")
        if self.signal_corr_max is not None and self.signal_corr_max > 1:
            raise ValueError("portfolio limits: signal_corr_max must be <= 1")

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> PortfolioLimits:
        if doc.get("x-version") != PORTFOLIO_LIMITS_VERSION:
            raise ValueError(f"portfolio limits: x-version must be {PORTFOLIO_LIMITS_VERSION}")
        allowed = {"x-version", "limits_version", "description", "limits"}
        extra = set(doc) - allowed
        if extra:
            raise ValueError(f"portfolio limits: unknown top-level keys {sorted(extra)}")
        lim = doc.get("limits")
        if not isinstance(lim, Mapping):
            raise ValueError("portfolio limits: 'limits' must be an object")
        extra = set(lim) - set(LIMIT_KEYS)
        if extra:
            raise ValueError(f"portfolio limits: unknown limit keys {sorted(extra)}")
        return cls(str(doc.get("limits_version", "")), **{k: lim.get(k) for k in LIMIT_KEYS})

    @classmethod
    def load(cls, path: str | Path | None = None) -> PortfolioLimits:
        p = Path(path) if path is not None else DEFAULT_LIMITS_PATH
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "x-version": PORTFOLIO_LIMITS_VERSION,
            "limits_version": self.limits_version,
            "limits": {k: getattr(self, k) for k in LIMIT_KEYS},
        }


@dataclass(frozen=True)
class Breach:
    """One limit breach: which limit, where, the value and the bound."""

    limit: str
    scope: str
    value: float
    bound: float

    def __str__(self) -> str:
        return f"{self.limit}[{self.scope}] {self.value:.6g} > {self.bound:.6g}"


class LimitRefused(Exception):
    """A weight set or order refused by the enforced limits or the kill switch."""

    def __init__(self, breaches: Sequence[Breach], limits_version: str, halted: bool = False):
        self.breaches = tuple(breaches)
        self.limits_version = limits_version
        self.halted = halted
        what = "kill switch latched" if halted else "; ".join(str(b) for b in self.breaches)
        super().__init__(f"refused under {limits_version}: {what}")


class HaltSink(Protocol):
    """Minimal REL-16 monitoring hook (module docs)."""

    def report_halt(self, source: str, reason: str, active: bool) -> None: ...


class _NullSink:
    def report_halt(self, source: str, reason: str, active: bool) -> None:
        return None


def _over(
    breaches: list[Breach], limit: str, scope: str, value: float, bound: float | None
) -> None:
    if bound is not None and value > bound + _TOL:
        breaches.append(Breach(limit, scope, float(value), float(bound)))


def check_weights(
    limits: PortfolioLimits,
    w: np.ndarray,
    instrument_ids: Sequence[int],
    *,
    w_prev: np.ndarray | None = None,
    betas: Mapping[int, float] | None = None,
    sectors: Mapping[int, str] | None = None,
    adv_notional: Mapping[int, float] | None = None,
    capital: float | None = None,
) -> list[Breach]:
    """Every breach of the static limits by ``w`` (empty list = allowed).

    A limit whose input is missing is a breach, not a pass: ``beta_max``
    needs nothing (missing betas read 1.0), but ``participation_max`` needs
    ``adv_notional`` and ``capital``, and a sector limit with no ``sectors``
    puts every name in one ``sector_unknown`` bucket.
    """
    w = np.asarray(w, dtype=np.float64)
    ids = [int(i) for i in instrument_ids]
    if w.shape != (len(ids),) or not np.all(np.isfinite(w)):
        raise ValueError("w must be finite with one entry per instrument")
    out: list[Breach] = []
    _over(out, "gross_max", "book", float(np.abs(w).sum()), limits.gross_max)
    _over(out, "net_max", "book", abs(float(w.sum())), limits.net_max)
    if limits.name_max is not None:
        for iid, wi in zip(ids, w, strict=True):
            _over(out, "name_max", str(iid), abs(float(wi)), limits.name_max)
    if limits.sector_net_max is not None or limits.sector_gross_max is not None:
        sec = {iid: (sectors or {}).get(iid) or "sector_unknown" for iid in ids}
        for s in sorted(set(sec.values())):
            m = np.array([sec[i] == s for i in ids])
            _over(out, "sector_net_max", s, abs(float(w[m].sum())), limits.sector_net_max)
            _over(out, "sector_gross_max", s, float(np.abs(w[m]).sum()), limits.sector_gross_max)
    if limits.beta_max is not None:
        b = np.array([float((betas or {}).get(i, 1.0)) for i in ids])
        b = np.where(np.isfinite(b), b, 1.0)
        _over(out, "beta_max", "book", abs(float(b @ w)), limits.beta_max)
    if limits.participation_max is not None:
        prev = np.zeros_like(w) if w_prev is None else np.asarray(w_prev, dtype=np.float64)
        trade = np.abs(w - prev)
        for iid, t in zip(ids, trade, strict=True):
            if t <= _TOL:
                continue
            adv = None if adv_notional is None else adv_notional.get(iid)
            if capital is None or adv is None or not math.isfinite(adv) or adv <= 0:
                out.append(Breach("participation_max", f"{iid}:no_adv", float(t), 0.0))
                continue
            _over(out, "participation_max", str(iid), t * capital / adv, limits.participation_max)
    return out


def check_signal_mix(
    limits: PortfolioLimits,
    sleeves: Mapping[str, np.ndarray],
    sigma: np.ndarray | None = None,
) -> list[Breach]:
    """Concentration and correlation limits across signal sleeves (module docs)."""
    out: list[Breach] = []
    names = sorted(sleeves)
    W = {k: np.asarray(sleeves[k], dtype=np.float64) for k in names}
    total = sum(float(np.abs(v).sum()) for v in W.values())
    if limits.signal_share_max is not None and total > 0:
        for k in names:
            _over(
                out,
                "signal_share_max",
                k,
                float(np.abs(W[k]).sum()) / total,
                limits.signal_share_max,
            )
    if limits.signal_corr_max is not None:
        if sigma is None:
            raise ValueError("signal_corr_max needs sigma")
        S = np.asarray(sigma, dtype=np.float64)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                va, vb = float(W[a] @ S @ W[a]), float(W[b] @ S @ W[b])
                if va <= 0 or vb <= 0:
                    continue
                c = float(W[a] @ S @ W[b]) / math.sqrt(va * vb)
                _over(out, "signal_corr_max", f"{a}~{b}", c, limits.signal_corr_max)
    return out


@dataclass
class PortfolioGuard:
    """Limits + loss tracking + latching kill switch (module docs)."""

    limits: PortfolioLimits
    capital: float
    sink: HaltSink = field(default_factory=_NullSink)
    source: str = "portfolio_limits"
    halted: bool = False
    halt_reason: str | None = None
    day_start_equity: float | None = None
    peak_equity: float | None = None
    equity: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (math.isfinite(self.capital) and self.capital > 0):
            raise ValueError("capital must be finite and > 0")

    # -- kill switch -------------------------------------------------------
    def halt(self, reason: str) -> None:
        """Trip (or keep) the kill switch; idempotent while latched."""
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            self.history.append({"event": "halt", "reason": reason})
            self.sink.report_halt(self.source, reason, True)

    def reset(self, operator: str, reason: str) -> None:
        """Explicit operator reset; refused while a loss limit is still breached."""
        if not operator or not reason:
            raise ValueError("reset needs a named operator and a reason")
        if not self.halted:
            return
        live = self._loss_breaches()
        if live:
            raise LimitRefused(live, self.limits.limits_version, halted=True)
        self.halted = False
        self.history.append(
            {"event": "reset", "operator": operator, "reason": reason, "cleared": self.halt_reason}
        )
        self.halt_reason = None
        self.sink.report_halt(self.source, f"reset by {operator}: {reason}", False)

    # -- loss tracking -----------------------------------------------------
    def start_day(self, equity: float) -> None:
        """New trading day: the daily-loss base moves; the latch does NOT clear."""
        self.day_start_equity = float(equity)
        self.equity = float(equity)
        self.peak_equity = max(self.peak_equity or float(equity), float(equity))

    def mark(self, equity: float) -> list[Breach]:
        """Mark-to-market; trips the kill switch on a loss-limit breach."""
        if not math.isfinite(equity):
            self.halt("non-finite equity mark")
            return []
        if self.day_start_equity is None:
            self.start_day(equity)
        self.equity = float(equity)
        self.peak_equity = max(self.peak_equity or self.equity, self.equity)
        b = self._loss_breaches()
        if b:
            self.halt("; ".join(str(x) for x in b))
        return b

    def _loss_breaches(self) -> list[Breach]:
        out: list[Breach] = []
        if self.equity is None:
            return out
        if self.day_start_equity is not None:
            loss = (self.day_start_equity - self.equity) / self.capital
            _over(out, "daily_loss_limit", "book", loss, self.limits.daily_loss_limit)
        if self.peak_equity is not None:
            dd = (self.peak_equity - self.equity) / self.capital
            _over(out, "drawdown_stop", "book", dd, self.limits.drawdown_stop)
        # a limit of exactly 0 must not trip on a flat book
        return [b for b in out if b.value > 0]

    # -- refusal points ----------------------------------------------------
    def check_weights(self, w: np.ndarray, instrument_ids: Sequence[int], **kw: Any) -> None:
        """Raise :class:`LimitRefused` if halted or ``w`` breaches any limit."""
        if self.halted:
            raise LimitRefused((), self.limits.limits_version, halted=True)
        b = check_weights(self.limits, w, instrument_ids, capital=self.capital, **kw)
        if b:
            raise LimitRefused(b, self.limits.limits_version)

    def check_order(
        self,
        instrument_id: int,
        signed_notional: float,
        positions: Mapping[int, float],
        **kw: Any,
    ) -> None:
        """Pre-trade: refuse an order whose POST-TRADE book breaches a limit.

        ``positions`` are current signed notionals by instrument; the order
        adds ``signed_notional`` to one of them.  Participation is checked
        on the order itself (``w_prev`` = the pre-trade book).
        """
        if self.halted:
            raise LimitRefused((), self.limits.limits_version, halted=True)
        if not math.isfinite(signed_notional):
            raise ValueError("signed_notional must be finite")
        ids = sorted(set(positions) | {int(instrument_id)})
        prev = np.array([float(positions.get(i, 0.0)) for i in ids]) / self.capital
        post = prev.copy()
        post[ids.index(int(instrument_id))] += signed_notional / self.capital
        before = {
            (x.limit, x.scope): x.value
            for x in check_weights(self.limits, prev, ids, w_prev=prev, capital=self.capital, **kw)
        }
        after = check_weights(self.limits, post, ids, w_prev=prev, capital=self.capital, **kw)
        # an order that only reduces an existing breach is allowed (risk-reducing)
        b = [x for x in after if x.value > before.get((x.limit, x.scope), -math.inf) + _TOL]
        if b:
            raise LimitRefused(b, self.limits.limits_version)
