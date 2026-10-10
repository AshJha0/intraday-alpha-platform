"""Causal auction features per NOII snapshot.

Every feature at a snapshot uses that snapshot and earlier snapshots of the
same (date, symbol, cross type) window only:

* ``imbalance_ratio`` - signed imbalance / (paired + imbalance), in [-1, 1];
  buy imbalance positive, ``N`` / ``O`` (no / insufficient) zero;
* ``ref_drift_bps`` - current reference price vs the window's first one;
* ``far_near_bps`` - far minus near indicative price over the reference
  (NaN while either is not published, e.g. far/near are zero before the
  last minutes of the closing window);
* ``near_vs_ref_bps`` - near indicative price vs the reference;
* ``time_to_cross_s`` - seconds to the scheduled cross (09:30 open, 16:00
  close, America/New_York); the scheduled time, not the print, so it is
  known at t.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

NS_S = 1_000_000_000
#: Scheduled cross times, ns since midnight America/New_York (pinned).
SCHEDULED_CROSS_TOD_NS = {"O": (9 * 3600 + 30 * 60) * NS_S, "C": 16 * 3600 * NS_S}
DIRECTION_SIGN = {"B": 1.0, "S": -1.0}
FEATURE_COLUMNS = (
    "imbalance_ratio",
    "ref_drift_bps",
    "far_near_bps",
    "near_vs_ref_bps",
    "time_to_cross_s",
)
GROUP_KEYS = ["date", "symbol", "cross_type"]


def auction_features(noii: pd.DataFrame, cross_type: str | None = "C") -> pd.DataFrame:
    """Feature frame of the NOII snapshots (one row per snapshot, sorted).

    ``cross_type`` selects ``"C"`` (closing, default), ``"O"`` (opening) or
    ``None`` for every scheduled cross type present.
    """
    df = noii
    if cross_type is not None:
        df = df[df["cross_type"] == cross_type]
    df = df[df["cross_type"].isin(list(SCHEDULED_CROSS_TOD_NS))]
    df = df.sort_values([*GROUP_KEYS, "ts"], kind="mergesort").reset_index(drop=True)
    out = df[[*GROUP_KEYS, "ts", "tod_ns"]].copy()
    sign = df["imbalance_direction"].map(DIRECTION_SIGN).fillna(0.0).to_numpy(float)
    imb = df["imbalance_shares"].to_numpy(float)
    paired = df["paired_shares"].to_numpy(float)
    denom = paired + imb
    with np.errstate(divide="ignore", invalid="ignore"):
        out["imbalance_ratio"] = np.where(denom > 0, sign * imb / denom, 0.0)
    ref = df["ref"].to_numpy(float)
    ref_ok = ref > 0
    ref_first = (
        df.assign(_r=np.where(ref_ok, ref, np.nan)).groupby(GROUP_KEYS)["_r"].transform("first")
    )
    far = df["far"].to_numpy(float)
    near = df["near"].to_numpy(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["ref_drift_bps"] = np.where(ref_ok, 1e4 * (ref / ref_first.to_numpy() - 1.0), np.nan)
        out["far_near_bps"] = np.where(
            ref_ok & (far > 0) & (near > 0), 1e4 * (far - near) / ref, np.nan
        )
        out["near_vs_ref_bps"] = np.where(ref_ok & (near > 0), 1e4 * (near - ref) / ref, np.nan)
    sched = df["cross_type"].map(SCHEDULED_CROSS_TOD_NS).to_numpy(np.int64)
    out["time_to_cross_s"] = (sched - df["tod_ns"].to_numpy(np.int64)) / NS_S
    out["ref"] = np.where(ref_ok, ref, np.nan)
    return out
