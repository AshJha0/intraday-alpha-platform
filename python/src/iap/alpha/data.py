"""Feature-frame loading and event-time grid helpers for the alpha layer.

Frames are the parquet files written by ``python3 -m iap.features``
(``data/features/features_<instrument_id>.parquet``): one row per emission,
sorted by ``exchange_ts``, feature columns NaN where invalid, plus label
columns ``label_mid_<h> / label_cost_<h> / label_valid_<h>``.

Grid helpers give cross-sectional alphas a shared causal clock: an
event-time grid at fixed step; each instrument contributes its latest
at-or-before row value (no interpolation, no lookahead), and grid values are
mapped back to native rows the same way (latest grid point <= row ts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd

NS_DAY = 86_400_000_000_000

LABEL_PREFIXES = ("label_mid_", "label_cost_", "label_valid_")


def label_columns(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if c.startswith(LABEL_PREFIXES)]


def load_features(
    features_dir, instrument_ids: Sequence[int] | None = None
) -> Dict[int, pd.DataFrame]:
    """Load per-instrument feature frames, sorted by exchange_ts."""
    features_dir = Path(features_dir)
    paths = sorted(features_dir.glob("features_*.parquet"))
    if not paths:
        raise ValueError(f"no feature parquet files in {features_dir}")
    out: Dict[int, pd.DataFrame] = {}
    for p in paths:
        try:
            iid = int(p.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if instrument_ids is not None and iid not in instrument_ids:
            continue
        df = pd.read_parquet(p)
        if not df["exchange_ts"].is_monotonic_increasing:
            df = df.sort_values("exchange_ts", kind="mergesort")
        out[iid] = df.reset_index(drop=True)
    if not out:
        raise ValueError(f"no matching feature files in {features_dir}")
    return out


def session_days(frames: Mapping[int, pd.DataFrame]) -> List[int]:
    """Sorted distinct UTC day indices (exchange_ts // day) across frames."""
    days: set[int] = set()
    for df in frames.values():
        days.update(np.unique(df["exchange_ts"].to_numpy() // NS_DAY).tolist())
    return sorted(int(d) for d in days)


def split_by_day(
    frames: Mapping[int, pd.DataFrame], day: int
) -> Tuple[Dict[int, pd.DataFrame], Dict[int, pd.DataFrame]]:
    """(rows on days < day, rows on days >= day) — both re-indexed."""
    lo: Dict[int, pd.DataFrame] = {}
    hi: Dict[int, pd.DataFrame] = {}
    for iid, df in frames.items():
        d = df["exchange_ts"].to_numpy() // NS_DAY
        lo[iid] = df[d < day].reset_index(drop=True)
        hi[iid] = df[d >= day].reset_index(drop=True)
    return lo, hi


def ts_span(frames: Mapping[int, pd.DataFrame]) -> Tuple[int, int]:
    t0 = min(int(df["exchange_ts"].iloc[0]) for df in frames.values() if len(df))
    t1 = max(int(df["exchange_ts"].iloc[-1]) for df in frames.values() if len(df))
    return t0, t1


def make_grid(frames: Mapping[int, pd.DataFrame], step_ns: int) -> np.ndarray:
    """Shared event-time grid: t0 + k*step, covering the union span."""
    t0, t1 = ts_span(frames)
    n = int((t1 - t0) // step_ns) + 1
    return t0 + step_ns * np.arange(1, n + 1, dtype=np.int64)


def asof_to_grid(
    ts: np.ndarray, values: np.ndarray, grid: np.ndarray, max_age_ns: int | None = None
) -> np.ndarray:
    """Latest at-or-before lookup of (ts, values) onto grid (NaN when none
    or when the sample is older than ``max_age_ns``)."""
    idx = np.searchsorted(ts, grid, side="right") - 1
    out = np.full(len(grid), np.nan)
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    if max_age_ns is not None:
        age = np.full(len(grid), np.inf)
        age[ok] = grid[ok] - ts[idx[ok]]
        out[age > max_age_ns] = np.nan
    return out


def grid_to_rows(grid: np.ndarray, grid_values: np.ndarray, row_ts: np.ndarray) -> np.ndarray:
    """Map grid-level values back onto native rows (latest grid point <= t)."""
    idx = np.searchsorted(grid, row_ts, side="right") - 1
    out = np.full(len(row_ts), np.nan)
    ok = idx >= 0
    out[ok] = grid_values[idx[ok]]
    return out
