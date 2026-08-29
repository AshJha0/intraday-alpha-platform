"""Model dataset construction from the feature parquet store (spec §14).

Loads ``data/features/features_<instrument_id>.parquet`` (features + event-time
labels produced by ``iap.features.__main__``), selects a pinned, curated
predictor set, and returns train-ready arrays.

Design decisions (pinned, documented in ML_REPORT.md):

- **Target**: 5s cost-adjusted forward return (``label_cost_5s``), restricted
  to rows where ``label_valid_5s`` — cost-adjusted returns keep the economics
  honest at the modelling stage already (conventions §7).
- **Predictors**: a curated ~48-column subset of the 205-feature registry,
  spanning every family, chosen for cadence-stability and to keep the full
  pipeline inside its runtime budget.  The full grid remains available in the
  parquet for deeper studies.
- **Missing values**: features carry NaN where invalid.  Imputation is
  train-fold-only: per-fold train means are computed and applied to both
  train and test (no test-set statistics leak into training).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[4]

#: Prediction target: 5s cost-adjusted forward return (spec §14 primary).
TARGET_COLUMN = "label_cost_5s"
TARGET_VALID_COLUMN = "label_valid_5s"
#: Companion mid-to-mid label used for economic evaluation.
TARGET_MID_COLUMN = "label_mid_5s"
TARGET_HORIZON_NS = 5_000_000_000

#: Pinned curated predictor set (every family represented).
FEATURE_SET: Tuple[str, ...] = (
    # price / returns
    "ret_log_1s_v1", "ret_log_5s_v1", "ret_log_10s_v1", "ret_log_30s_v1",
    "ret_accel_5s_v1", "ret_resid_5s_v1", "ret_vol_adj_5s_v1",
    "mid_change_ticks_1s_v1",
    # microstructure
    "micro_mid_dev_bps_v1", "spread_bps_v1",
    "imbalance_l1_v1", "imbalance_l3_v1", "imbalance_l5_v1",
    "imbalance_l1_avg_w1s_v1", "imbalance_l5_avg_w10s_v1",
    "depth_total_l1_v1", "depth_total_l5_v1",
    "queue_depletion_rate_bid_w1s_v1", "queue_depletion_rate_ask_w1s_v1",
    # order flow
    "ofi_norm_l5_w5s_v1", "ofi_norm_l5_w30s_v1",
    "signed_volume_w1s_v1", "signed_volume_w10s_v1",
    "trade_imbalance_w1s_v1", "trade_imbalance_w10s_v1",
    "trade_intensity_w1s_v1", "cancel_add_ratio_w1s_v1",
    # liquidity
    "quoted_depth_ratio_v1", "effective_spread_bps_w10s_v1",
    "participation_w10s_v1", "depth_slope_bid_v1", "depth_slope_ask_v1",
    # volatility
    "rvol_w10s_v1", "rvol_w1m_v1", "vol_ratio_w10s_w1m_v1",
    "range_bps_w10s_v1", "jump_flag_w1m_v1",
    # time of day
    "minute_of_day_v1", "session_frac_v1", "norm_spread_m5_v1",
    # cross asset
    "ref_ret_5s_v1", "beta_w5m_v1", "leadlag_corr_w1m_v1",
    # venue
    "venue_depth_hhi_v1", "venue_imbalance_divergence_v1",
    # regime
    "trend_score_w1m_v1", "meanrev_score_w1m_v1", "vol_regime_ratio_v1",
    # execution
    "half_spread_cost_bps_v1", "expected_impact_bps_v1",
    "fill_prob_bid_h1s_v1",
)

#: Extra columns the meta-labeling stage conditions on (spec §14 secondary).
META_CONTEXT_COLUMNS: Tuple[str, ...] = (
    "spread_bps_v1", "rvol_w1m_v1", "depth_total_l1_v1", "imbalance_l1_v1",
    "half_spread_cost_bps_v1", "expected_impact_bps_v1",
)


@dataclass
class Dataset:
    """Train-ready arrays, sorted by (exchange_ts, instrument_id)."""

    X: np.ndarray               # (n, n_features) float64, NaN preserved
    y: np.ndarray               # (n,) cost-adjusted 5s forward return
    y_mid: np.ndarray           # (n,) mid-to-mid 5s forward return
    ts: np.ndarray              # (n,) int64 exchange_ts
    instrument_id: np.ndarray   # (n,) int32
    feature_names: List[str]
    meta_context: np.ndarray    # (n, len(META_CONTEXT_COLUMNS))

    def __len__(self) -> int:
        return len(self.y)


def load_dataset(
    features_dir: Optional[Path] = None,
    instruments: Optional[Sequence[int]] = None,
    feature_set: Sequence[str] = FEATURE_SET,
) -> Dataset:
    """Load and concatenate per-instrument feature frames into a Dataset.

    Rows are kept only where the target label is valid and finite.  Frames
    are concatenated in sorted instrument order, then globally sorted by
    (exchange_ts, instrument_id) — a pinned deterministic ordering.
    """
    fdir = Path(features_dir) if features_dir is not None \
        else _REPO / "data" / "features"
    if not fdir.is_dir():
        raise ValueError(
            f"missing features dir {fdir}; regenerate via "
            "`PYTHONPATH=src python3 -m iap.features`"
        )
    files = sorted(fdir.glob("features_*.parquet"),
                   key=lambda p: int(p.stem.split("_")[1]))
    if instruments is not None:
        want = set(int(i) for i in instruments)
        files = [p for p in files if int(p.stem.split("_")[1]) in want]
    if not files:
        raise ValueError(f"no feature parquet files under {fdir}")

    cols = (["instrument_id", "exchange_ts", TARGET_COLUMN,
             TARGET_VALID_COLUMN, TARGET_MID_COLUMN]
            + list(feature_set) + list(META_CONTEXT_COLUMNS))
    # de-dup while preserving order (meta columns overlap the feature set)
    seen: set = set()
    cols = [c for c in cols if not (c in seen or seen.add(c))]

    frames = []
    for path in files:
        df = pd.read_parquet(path, columns=cols)
        df = df[df[TARGET_VALID_COLUMN].astype(bool)]
        df = df[np.isfinite(df[TARGET_COLUMN].to_numpy())]
        frames.append(df)
    data = pd.concat(frames, ignore_index=True)
    data.sort_values(["exchange_ts", "instrument_id"],
                     kind="mergesort", inplace=True)
    data.reset_index(drop=True, inplace=True)

    return Dataset(
        X=data[list(feature_set)].to_numpy(dtype=np.float64),
        y=data[TARGET_COLUMN].to_numpy(dtype=np.float64),
        y_mid=data[TARGET_MID_COLUMN].to_numpy(dtype=np.float64),
        ts=data["exchange_ts"].to_numpy(dtype=np.int64),
        instrument_id=data["instrument_id"].to_numpy(dtype=np.int32),
        feature_names=list(feature_set),
        meta_context=data[list(META_CONTEXT_COLUMNS)].to_numpy(
            dtype=np.float64),
    )


class TrainScaler:
    """Train-only impute (mean) + standardize transform (no test leakage)."""

    def __init__(self) -> None:
        self.mean_: Optional[np.ndarray] = None
        self.std_: Optional[np.ndarray] = None

    def fit(self, X_train: np.ndarray) -> "TrainScaler":
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(X_train, axis=0)
            std = np.nanstd(X_train, axis=0)
        mean = np.where(np.isfinite(mean), mean, 0.0)
        std = np.where(np.isfinite(std) & (std > 0.0), std, 1.0)
        self.mean_ = mean
        self.std_ = std
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        if self.mean_ is None or self.std_ is None:
            raise RuntimeError("TrainScaler.transform before fit")
        Xf = np.where(np.isfinite(X), X, self.mean_)
        return (Xf - self.mean_) / self.std_
