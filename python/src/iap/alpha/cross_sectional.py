"""Cross-sectional alpha machinery + EQ11 (spec §11).

Cross-sectional alphas need every universe instrument at once.  They share a
causal event-time grid (``iap.alpha.data``): each instrument contributes its
latest at-or-before feature value to each grid point, the cross-sectional
computation runs per grid point, and the result is mapped back to each
instrument's native rows via a latest-grid-point-at-or-before lookup — no
step ever looks forward in time.
"""

from __future__ import annotations

from typing import Dict, Mapping

import numpy as np
import pandas as pd

from iap.alpha.base import EPS, EQ_CONSTITUENT_IDS, LinearAlpha
from iap.alpha.data import asof_to_grid, grid_to_rows, make_grid


class CrossSectionalLinearAlpha(LinearAlpha):
    """LinearAlpha whose raw signal is computed jointly on a shared grid.

    Subclasses implement :meth:`grid_signals`, receiving a matrix of the
    grid-sampled input feature (instruments x grid points) and returning the
    per-instrument signal matrix of the same shape.
    """

    cross_sectional = True
    GRID_STEP_NS: int = 5_000_000_000  # 5s default
    MAX_AGE_NS: int | None = None      # staleness cap for grid sampling
    INPUT_FEATURE: str = ""

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        raise RuntimeError(
            f"{self.alpha_id} is cross-sectional; use signals() over the universe"
        )

    def grid_signals(self, mat: np.ndarray) -> np.ndarray:
        """Cross-sectional transform per grid column (NaN-aware)."""
        raise NotImplementedError

    def signals(self, data: Mapping[int, pd.DataFrame]) -> Dict[int, pd.Series]:
        ids = self.universe(list(data))
        frames = {i: data[i] for i in ids if len(data[i])}
        if not frames:
            return {}
        grid = make_grid(frames, self.GRID_STEP_NS)
        rows = []
        for iid in sorted(frames):
            df = frames[iid]
            rows.append(
                asof_to_grid(
                    df["exchange_ts"].to_numpy(),
                    df[self.INPUT_FEATURE].to_numpy(dtype=float),
                    grid,
                    self.MAX_AGE_NS,
                )
            )
        mat = np.vstack(rows)
        sig = self.grid_signals(mat)
        out: Dict[int, pd.Series] = {}
        for k, iid in enumerate(sorted(frames)):
            df = frames[iid]
            out[iid] = pd.Series(
                grid_to_rows(grid, sig[k], df["exchange_ts"].to_numpy()),
                index=df.index,
            )
        return out


class EQ11CrossSectionalReversal(CrossSectionalLinearAlpha):
    """Cross-sectional momentum/reversal alpha.

    Economic rationale: over minutes, single-name moves relative to the
    cross-section are dominated by idiosyncratic liquidity shocks rather
    than common information, so the spread between recent winners and losers
    compresses: short the names that outran the cross-section over the last
    minute, buy the laggards (cross-sectional reversal — the short-horizon
    counterpart of cross-sectional momentum).  Signal: negative
    cross-sectionally demeaned and studentized 1-minute log return on a
    shared 5-second grid, fitted linear scaling.
    """

    alpha_id = "EQ11"
    name = "cross_sectional_reversal"
    asset_class = "EQUITY"
    horizon = "15m"
    features = ("ret_log_1m_v1",)
    INPUT_FEATURE = "ret_log_1m_v1"
    GRID_STEP_NS = 5_000_000_000
    MAX_AGE_NS = 60_000_000_000  # a name quiet for > 1m drops out of the cross-section
    MIN_NAMES = 4                # need a real cross-section

    def universe(self, instrument_ids):
        return sorted(i for i in instrument_ids if i in EQ_CONSTITUENT_IDS)

    def grid_signals(self, mat: np.ndarray) -> np.ndarray:
        fin = np.isfinite(mat)
        n = fin.sum(axis=0)
        denom = np.maximum(n, 1)
        vals = np.where(fin, mat, 0.0)
        mean = vals.sum(axis=0) / denom
        var = np.where(fin, (mat - mean[None, :]) ** 2, 0.0).sum(axis=0) / denom
        std = np.sqrt(var)
        sig = -(mat - mean[None, :]) / (std[None, :] + EPS)
        sig[:, n < self.MIN_NAMES] = np.nan
        sig[~fin] = np.nan
        return sig
