"""Auction targets (labels) per NOII snapshot.

* ``cross_vs_mid_bps`` - the window's cross print vs the mid at t: what a
  position opened at t and closed *in* the cross earns before costs;
* ``into_cross_drift_bps`` - the last mid strictly before the scheduled
  cross vs the mid at t: what a position closed with a taker trade just
  before the cross earns before costs.

The closing cross (``cross_type="C"``) is the primary target and the
opening cross (``"O"``) the second; the same code serves both.

Mids come from ``mids`` (columns ``symbol, ts, mid`` and optionally
``half_spread``, all in dollars and epoch ns - e.g. exported from a
normalized dataset's book).  Without it the NOII *current reference price*
is the proxy: Nasdaq publishes it inside the inside quotes, but it is not
the mid, so ``mid_source`` records which one a label used.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from iap.auction.features import GROUP_KEYS, SCHEDULED_CROSS_TOD_NS

TARGET_COLUMNS = ("cross_vs_mid_bps", "into_cross_drift_bps")


def _cross_prices(crosses: pd.DataFrame) -> pd.DataFrame:
    """One print per (date, symbol, cross type): the largest by shares."""
    c = crosses[crosses["price"].astype(float) > 0]
    c = c.sort_values([*GROUP_KEYS, "shares", "ts"], ascending=[True, True, True, False, True])
    c = c.groupby(GROUP_KEYS, as_index=False).last()
    return c[[*GROUP_KEYS, "price"]].rename(columns={"price": "cross_price"})


def _asof(left_ts: np.ndarray, symbols: np.ndarray, mids: pd.DataFrame, strict: bool):
    left = pd.DataFrame({"_k": np.arange(len(left_ts)), "ts": left_ts, "symbol": symbols})
    left = left.sort_values("ts", kind="mergesort")
    right = mids.sort_values("ts", kind="mergesort")
    cols = ["ts", "symbol", "mid"] + (["half_spread"] if "half_spread" in right else [])
    m = pd.merge_asof(
        left,
        right[cols],
        on="ts",
        by="symbol",
        direction="backward",
        allow_exact_matches=not strict,
    )
    return m.sort_values("_k")


def auction_targets(
    features: pd.DataFrame,
    crosses: pd.DataFrame,
    mids: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """``features`` with mid_t, half_spread_t, cross_price and the targets."""
    out = features.reset_index(drop=True).copy()
    ts = out["ts"].to_numpy(np.int64)
    sched = out["cross_type"].map(SCHEDULED_CROSS_TOD_NS).to_numpy(np.int64)
    cross_epoch = ts - out["tod_ns"].to_numpy(np.int64) + sched
    sym = out["symbol"].to_numpy(object)
    if mids is not None and len(mids):
        mids = mids.assign(ts=mids["ts"].astype(np.int64), mid=mids["mid"].astype(float))
        now = _asof(ts, sym, mids, strict=False)
        pre = _asof(cross_epoch, sym, mids, strict=True)
        out["mid_t"] = now["mid"].to_numpy(float)
        out["half_spread_t"] = (
            now["half_spread"].to_numpy(float) if "half_spread" in now else np.nan
        )
        out["mid_pre_cross"] = pre["mid"].to_numpy(float)
        out["mid_source"] = "mids"
    else:
        out["mid_t"] = out["ref"].to_numpy(float)
        out["half_spread_t"] = np.nan
        out["mid_pre_cross"] = out.groupby(GROUP_KEYS)["ref"].transform("last").to_numpy(float)
        out["mid_source"] = "noii_reference"
    out = out.merge(_cross_prices(crosses), on=GROUP_KEYS, how="left")
    mid_t = out["mid_t"].to_numpy(float)
    ok = np.isfinite(mid_t) & (mid_t > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["cross_vs_mid_bps"] = np.where(
            ok, 1e4 * (out["cross_price"].to_numpy(float) / mid_t - 1.0), np.nan
        )
        out["into_cross_drift_bps"] = np.where(
            ok, 1e4 * (out["mid_pre_cross"].to_numpy(float) / mid_t - 1.0), np.nan
        )
    return out
