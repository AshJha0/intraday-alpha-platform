"""Sizing inputs of the MVP engine: the 1-minute bar roll feeding the EWMA
variance and the signed in-flight quantity.  Split out of
:class:`~iap.mvp.engine.MvpEngine` as a mixin (v1.13 E4); no behaviour change.

**v1.17 WS-D item D3 (opt-in): conformal sizing.**  With
``portfolio.conformal`` present in mvp.json (:class:`iap.mvp.config.ConformalSpec`)
the mixin keeps an online conformal calibrator
(:class:`iap.models.conformal.AdaptiveConformal`; ``method="split"`` is the
same window with ``gamma=0``) over the residual *next sizing-bar log return
minus the last expected return of the previous bar* -- a SCALE PROXY for
the horizon residual, which the engine does not observe online -- and
scales every portfolio target by :func:`iap.models.conformal.confidence_from_width`
of the current interval width against ``ref_width`` through
:func:`iap.models.conformal.conformal_size` (whole lots, never above the
unscaled target or ``max_position_qty``).  Until ``warmup`` residuals are
in, the target passes unscaled.  With the key absent (every pinned and
golden config) nothing here runs and the engine's sizing is byte-identical
to v1.16; ``iap.models.conformal`` is imported only when the block is on.
"""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING

from iap.contracts.types import (
    Side,
)
from iap.features.engine import FeatureVector

if TYPE_CHECKING:
    from iap.mvp.adapters import SimulatorAdapter
    from iap.mvp.config import MvpConfig
    from iap.mvp.orders import _LiveParent

BAR_NS = 60_000_000_000


class SizingMixin:
    """Mixin of :class:`~iap.mvp.engine.MvpEngine` (module docstring); it
    reads and writes the engine's own attributes."""

    if TYPE_CHECKING:
        # The engine attributes this mixin touches, declared for the type
        # checker only (F3); MvpEngine.__init__ assigns them.
        cfg: MvpConfig
        sim: SimulatorAdapter
        _mid_index: int
        _current_bar: int | None
        _bar_mid: float | None
        _last_bar_mid: float | None
        bar_returns: list[float]
        _live: _LiveParent | None

    def _roll_bar(self, vec: FeatureVector) -> None:
        """Java ``PaperTrading`` sizing bar roll: last mid per 1-minute bucket."""
        if not vec.validity[self._mid_index]:
            return
        mid = vec.values[self._mid_index]
        bar = (vec.timestamp // BAR_NS) * BAR_NS
        if bar != self._current_bar:
            if self._bar_mid is not None:
                if self._last_bar_mid is not None:
                    r = math.log(self._bar_mid / self._last_bar_mid)
                    self.bar_returns.append(r)
                    self._conformal_update(r)
                self._last_bar_mid = self._bar_mid
            self._current_bar = bar
        self._bar_mid = mid

    # ------------------------------------------------- v1.17 D3 conformal

    def _init_conformal(self) -> None:
        """Set up the opt-in conformal calibrator from ``portfolio.conformal``
        (``None`` = off; nothing is imported or allocated)."""
        spec = self.cfg.portfolio.conformal
        self._conformal = None
        self._conformal_pending: float | None = None
        self.conformal_sizing: dict[str, float | int | bool | None] = {
            "enabled": spec is not None,
            "updates": 0,
            "decisions_seen": 0,
            "decisions_scaled": 0,
            "qty_removed": 0,
            "min_multiplier": None,
            "ref_width": None if spec is None else spec.ref_width,
        }
        if spec is None:
            return
        from iap.models.conformal import AdaptiveConformal

        self._conformal = AdaptiveConformal(
            alpha=spec.alpha,
            gamma=spec.gamma if spec.method == "adaptive" else 0.0,
            window=spec.window,
        )

    def _conformal_observe(self, expected_return: float) -> None:
        """Remember the latest forecast of the current bar (paired with the
        next bar's return in :meth:`_conformal_update`)."""
        if self._conformal is not None and math.isfinite(expected_return):
            self._conformal_pending = float(expected_return)

    def _conformal_update(self, bar_return: float) -> None:
        if self._conformal is None or self._conformal_pending is None:
            return
        self._conformal.update(bar_return, self._conformal_pending)
        self._conformal_pending = None
        self.conformal_sizing["updates"] = int(self.conformal_sizing["updates"] or 0) + 1

    def _conformal_scale(self, target_qty: int, expected_return: float) -> int:
        """The portfolio target scaled by the conformal confidence (identity
        when the block is off or still warming up)."""
        if self._conformal is None:
            return target_qty
        spec = self.cfg.portfolio.conformal
        assert spec is not None
        self._conformal_observe(expected_return)
        self.conformal_sizing["decisions_seen"] = (
            int(self.conformal_sizing["decisions_seen"] or 0) + 1
        )
        scores = self._conformal._scores
        if len(scores) < spec.warmup or target_qty == 0:
            return target_qty
        from iap.models.conformal import confidence_from_width, conformal_size

        ref = spec.ref_width
        if ref is None:
            ref = 2.0 * statistics.median(scores)
            self.conformal_sizing["ref_width"] = ref
        if not ref > 0.0:
            return target_qty
        width = 2.0 * self._conformal.half_width()
        conf = confidence_from_width(width, ref)
        sized = conformal_size(target_qty, conf, self.cfg.portfolio.max_position_qty)
        if sized != target_qty:
            self.conformal_sizing["decisions_scaled"] = (
                int(self.conformal_sizing["decisions_scaled"] or 0) + 1
            )
            self.conformal_sizing["qty_removed"] = int(
                self.conformal_sizing["qty_removed"] or 0
            ) + abs(target_qty - sized)
        prev = self.conformal_sizing["min_multiplier"]
        self.conformal_sizing["min_multiplier"] = conf if prev is None else min(float(prev), conf)
        return sized

    def _inflight_signed(self) -> int:
        live = self._live
        if live is None:
            return 0
        signed = 0
        for cid, child in live.children.items():
            q = self.sim.open_qty(cid)
            signed += q if child.side is Side.BID else -q
        return signed
