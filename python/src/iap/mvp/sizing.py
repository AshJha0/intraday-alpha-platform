"""Sizing inputs of the MVP engine: the 1-minute bar roll feeding the EWMA
variance and the signed in-flight quantity.  Split out of
:class:`~iap.mvp.engine.MvpEngine` as a mixin (v1.13 E4); no behaviour change.
"""

from __future__ import annotations

import math

from iap.contracts.types import (
    Side,
)
from iap.features.engine import FeatureVector

BAR_NS = 60_000_000_000


class SizingMixin:
    """Mixin of :class:`~iap.mvp.engine.MvpEngine` (module docstring); it
    reads and writes the engine's own attributes."""

    def _roll_bar(self, vec: FeatureVector) -> None:
        """Java ``PaperTrading`` sizing bar roll: last mid per 1-minute bucket."""
        if not vec.validity[self._mid_index]:
            return
        mid = vec.values[self._mid_index]
        bar = (vec.timestamp // BAR_NS) * BAR_NS
        if bar != self._current_bar:
            if self._bar_mid is not None:
                if self._last_bar_mid is not None:
                    self.bar_returns.append(math.log(self._bar_mid / self._last_bar_mid))
                self._last_bar_mid = self._bar_mid
            self._current_bar = bar
        self._bar_mid = mid

    def _inflight_signed(self) -> int:
        live = self._live
        if live is None:
            return 0
        signed = 0
        for cid, child in live.children.items():
            q = self.sim.open_qty(cid)
            signed += q if child.side is Side.BID else -q
        return signed
