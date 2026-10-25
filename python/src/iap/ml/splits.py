"""Chronological splits with purging and embargo (ML protocol section 2).

A :class:`ChronologicalSplit` is defined on a session calendar by the last
training session and the label horizon ``h`` (sessions).  A row at session
``d`` with a label over ``[d, d + h]``:

- is a **training** row only if ``d + h <= train_end`` (purge: no label window
  crosses the boundary);
- is a **validation** row only if ``d >= train_end + 1 + embargo`` with
  ``embargo = h_max``, the longest label horizon of the family (embargo), and
  ``d <= validation_end`` when one is given.

No row is in both; rows between the windows are in neither.  Everything is an
index into the sorted calendar, so the same split applies to features, labels
and session series alike.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = ["ChronologicalSplit", "SplitError"]


class SplitError(ValueError):
    """A split that would leak: overlapping windows, missing embargo, unknown sessions."""


@dataclass(frozen=True, slots=True)
class ChronologicalSplit:
    sessions: tuple[str, ...]
    train_end: str
    horizon: int
    embargo: int
    validation_end: str | None = None

    def __post_init__(self) -> None:
        cal = list(self.sessions)
        if cal != sorted(cal) or len(set(cal)) != len(cal):
            raise SplitError("sessions must be strictly increasing")
        if self.train_end not in cal:
            raise SplitError("train_end is not a session of the calendar")
        if self.horizon < 0 or self.embargo < self.horizon:
            raise SplitError(
                "embargo must be >= the label horizon (the longest horizon of the family)"
            )
        if self.validation_end is not None and (
            self.validation_end not in cal or self.validation_end <= self.train_end
        ):
            raise SplitError("validation_end must be a later session than train_end")

    # ------------------------------------------------------------------ indices
    def _index(self) -> dict[str, int]:
        return {s: i for i, s in enumerate(self.sessions)}

    @property
    def train_end_index(self) -> int:
        return self._index()[self.train_end]

    @property
    def validation_start_index(self) -> int:
        return self.train_end_index + 1 + self.embargo

    def train_mask(self, session: Sequence[str] | pd.Series) -> np.ndarray:
        idx = self._index()
        s = np.asarray([idx.get(x, -1) for x in session])
        if (s < 0).any():
            raise SplitError("rows carry sessions outside the calendar")
        return (s + self.horizon) <= self.train_end_index

    def validation_mask(self, session: Sequence[str] | pd.Series) -> np.ndarray:
        idx = self._index()
        s = np.asarray([idx.get(x, -1) for x in session])
        if (s < 0).any():
            raise SplitError("rows carry sessions outside the calendar")
        m = s >= self.validation_start_index
        if self.validation_end is not None:
            m &= s <= idx[self.validation_end]
        return m

    def train_sessions(self) -> list[str]:
        return [s for i, s in enumerate(self.sessions) if i + self.horizon <= self.train_end_index]

    def validation_sessions(self) -> list[str]:
        lo = self.validation_start_index
        hi = self._index()[self.validation_end] if self.validation_end else len(self.sessions) - 1
        return list(self.sessions[lo : hi + 1])

    def check_no_overlap(self, session: Sequence[str] | pd.Series) -> None:
        """Raise if any training label window reaches a validation session (the leakage the split exists to prevent)."""
        t = self.train_mask(session)
        v = self.validation_mask(session)
        if (t & v).any():
            raise SplitError("a row is in both windows")
        idx = self._index()
        s = np.asarray([idx[x] for x in session])
        if t.any() and v.any() and (s[t] + self.horizon).max() >= s[v].min():
            raise SplitError("a training label window reaches into the validation window")

    def to_dict(self) -> dict[str, object]:
        return {
            "train_end": self.train_end,
            "horizon": self.horizon,
            "embargo": self.embargo,
            "validation_start": self.sessions[self.validation_start_index]
            if self.validation_start_index < len(self.sessions)
            else None,
            "validation_end": self.validation_end,
            "n_train_sessions": len(self.train_sessions()),
            "n_validation_sessions": len(self.validation_sessions()),
        }
