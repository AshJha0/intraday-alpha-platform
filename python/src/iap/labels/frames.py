"""Which rows of a feature frame a research statistic scores, and against what.

A frame carries, per horizon ``h``, ``label_mid_<h>`` / ``label_valid_<h>``
and — since v1.5.0 — ``label_reason_<h>`` / ``label_reopen_<h>``
(:mod:`iap.labels.labels`).  A label is INVALID when anything inside
``(t, t + h]`` was not tradable (``LabelReason.BLACKOUT``), and up to v1.4.0
every IC dropped those rows.  That is a selection on the outcome: whether a
halt, an auction or a stale-venue gap falls inside the horizon is not known
at ``t``, the rows dropped are the ones before such a gap, and what is left
is a sample of the quiet windows.  On the bundled equities a 15-minute
window almost always contains a non-tradable refresh, so the valid-only
population of a 15-minute alpha is a small and unrepresentative part of its
decisions.

**Row policy (pinned; the default changed in v1.5.0).**

* ``"blackout_reopen"`` — the default.  A row whose label is invalid for
  ``BLACKOUT`` ALONE is scored at the realised reopen return
  (``label_reopen_<h>``: anchor mid to the first tradable mid at or after
  ``t + h``); every other row as before.  Rows invalid for any other reason
  — the stream ended, no anchor, a non-tradable anchor, a stale forward mid
  — have no price to score against and stay out.
* ``"valid_only"`` — the legacy rule: valid labels only.

A frame without the ``label_reopen_<h>`` column (a feature store written
before v1.5.0, a hand-built test frame) has no reopen return to score, so
both policies give the valid-label series on it; :func:`scored_labels`
reports how many rows were rescued so a caller can say which it got.

The research backtester's default row block uses the same rows
(:func:`scored_rows`): it trades exactly the rows the IC is measured on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from iap.labels.labels import LabelReason

__all__ = [
    "DEFAULT_IC_ROWS",
    "IC_ROWS",
    "LEGACY_IC_ROWS",
    "scored_labels",
    "scored_rows",
]

#: Row policies (module docs): the default, then the legacy rule.
IC_ROWS = ("blackout_reopen", "valid_only")
DEFAULT_IC_ROWS = "blackout_reopen"
LEGACY_IC_ROWS = "valid_only"


def _check(ic_rows: str) -> None:
    if ic_rows not in IC_ROWS:
        raise ValueError(f"unknown ic_rows {ic_rows!r}; known: {IC_ROWS}")


def _rescued(frame: pd.DataFrame, horizon: str, valid: np.ndarray) -> np.ndarray | None:
    """Reopen return where the row is invalid for BLACKOUT alone (NaN
    elsewhere), or ``None`` when the frame has no reopen column."""
    column = f"label_reopen_{horizon}"
    if column not in frame.columns:
        return None
    reopen = frame[column].to_numpy(dtype=float)
    take = ~valid & np.isfinite(reopen)
    reason_column = f"label_reason_{horizon}"
    if reason_column in frame.columns:
        take &= frame[reason_column].to_numpy(dtype=np.int64) == LabelReason.BLACKOUT
    return np.where(take, reopen, np.nan)


def scored_labels(
    frame: pd.DataFrame, horizon: str, ic_rows: str = DEFAULT_IC_ROWS
) -> tuple[np.ndarray, int]:
    """``(labels, n_rescued)``: the mid label series a statistic is scored
    against under ``ic_rows`` (NaN on rows that are not scored) and the
    number of BLACKOUT rows scored at their reopen return."""
    _check(ic_rows)
    labels = frame[f"label_mid_{horizon}"].to_numpy(dtype=float).copy()
    valid = frame[f"label_valid_{horizon}"].to_numpy(dtype=bool)
    labels[~valid] = np.nan
    if ic_rows == LEGACY_IC_ROWS:
        return labels, 0
    rescued = _rescued(frame, horizon, valid)
    if rescued is None:
        return labels, 0
    take = np.isfinite(rescued)
    labels[take] = rescued[take]
    return labels, int(take.sum())


def scored_rows(frame: pd.DataFrame, horizon: str, ic_rows: str = DEFAULT_IC_ROWS) -> np.ndarray:
    """Boolean mask of the rows :func:`scored_labels` scores."""
    labels, _ = scored_labels(frame, horizon, ic_rows)
    return np.isfinite(labels)
