"""One prepared replay shared across maker configurations (v1.14, review E-P1-2).

A study unit runs ``MakerBacktester.run_instrument`` once per alpha x exit x
variant (6-9 configurations) on the same session's events.  Each call scans
the whole multi-instrument stream twice before it starts (venue lookup and,
with a router, the venue set) and then skips every other instrument's event
inside the replay loop.

:class:`SharedReplay` does that per-stream work once per unit: it keeps the
instrument's events as one :class:`~iap.research.events.EventColumns`
(numpy, no Python object per row), resolves the venue the backtester would
have picked (the first event's venue) and the router's candidate venues,
and replays the same prepared stream for every configuration.  The
simulator itself is untouched (``iap.backtest.maker`` is not edited); the
event loop still runs once per configuration -- folding the configurations
into one pass is the later Rust/pyo3 simulator's job.

Behaviour is identical by construction: ``run_instrument`` ignores every
event of another instrument, so pre-filtering does not change the replay,
and the explicit ``venue_id`` is the one it would have found.
:func:`run_grid` returns results in the order the configurations are given;
``tests``/``test_research_replay.py`` pins equality with independent runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from iap.core.events import MarketEvent
from iap.research.events import EventColumns


@dataclass(frozen=True)
class GridCell:
    """One configuration of a grid: a ``MakerConfig`` plus its allow mask."""

    config: Any  # iap.backtest.maker.MakerConfig
    allow: np.ndarray | None = None


class SharedReplay:
    """One instrument's event stream, prepared once and replayed per config."""

    def __init__(self, events: EventColumns | Sequence[MarketEvent], instrument_id: int) -> None:
        cols = events if isinstance(events, EventColumns) else EventColumns.from_events(events)
        self.instrument_id = int(instrument_id)
        self.events = cols.for_instrument(self.instrument_id)
        self.venue_id = self.events.first_venue()
        self.route_venues = self.events.venues()

    def __len__(self) -> int:
        return len(self.events)

    def run(
        self,
        exec_config,
        config,
        scores: pd.DataFrame,
        *,
        calibration=None,
        allow: np.ndarray | None = None,
        router=None,
        fill_hazard=None,
    ):
        """``MakerBacktester(...).run_instrument`` on the shared stream."""
        from iap.backtest.maker import MakerBacktester

        if self.venue_id is None:
            raise ValueError(f"no event for instrument {self.instrument_id}")
        bt = MakerBacktester(exec_config, config, calibration, router, fill_hazard)
        return bt.run_instrument(
            self.events, scores, self.instrument_id, venue_id=self.venue_id, allow=allow
        )

    def run_grid(
        self,
        exec_config,
        cells: Mapping[str, GridCell] | Iterable[tuple[str, GridCell]],
        scores: pd.DataFrame,
        *,
        calibration=None,
        router=None,
        fill_hazard=None,
    ) -> dict[str, Any]:
        """``{key: MakerResult}`` for every configuration, in the given order."""
        items = cells.items() if isinstance(cells, Mapping) else cells
        return {
            key: self.run(
                exec_config,
                cell.config,
                scores,
                calibration=calibration,
                allow=cell.allow,
                router=router,
                fill_hazard=fill_hazard,
            )
            for key, cell in items
        }


__all__ = ["GridCell", "SharedReplay"]
