"""Equity flagship alphas EQ01-EQ10 and EQ12 (spec §11).

EQ11 (cross-sectional momentum/reversal) lives in
``iap.alpha.cross_sectional`` because it needs the whole universe at once.

All alphas here are per-instrument :class:`~iap.alpha.base.LinearAlpha`
models over the bundled synthetic feature frames.  Every raw signal is
causal: it reads only same-row features (which the feature engine computed
from events at-or-before the row's exchange_ts) or backward-looking rolling
constructions over prior rows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from iap.alpha.base import (
    EPS,
    EQ_CONSTITUENT_IDS,
    ETF_ID,
    LinearAlpha,
    col,
)


class EQ01Microprice(LinearAlpha):
    """Microprice directional alpha.

    Economic rationale: the depth-weighted microprice
    ``(Pb*Qa + Pa*Qb)/(Qb+Qa)`` is a better estimate of the efficient price
    than the mid: when the bid queue dwarfs the ask queue the next trade is
    more likely to lift the offer and the mid migrates toward the microprice.
    A positive microprice-minus-mid deviation therefore predicts a positive
    short-horizon mid move.  Signal: ``micro_mid_dev_bps_v1`` with fitted
    linear scaling.
    """

    alpha_id = "EQ01"
    name = "microprice_directional"
    asset_class = "EQUITY"
    horizon = "1s"
    features = ("micro_mid_dev_bps_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "micro_mid_dev_bps_v1")


class EQ02OfiL1(LinearAlpha):
    """L1 order-flow imbalance alpha.

    Economic rationale: net additions at the best bid minus net additions at
    the best ask (Cont-Kukanov-Stoikov order-flow imbalance) measure
    instantaneous excess demand at the touch; sustained positive OFI pushes
    the price up roughly linearly in imbalance over short horizons.  Signal:
    depth-normalized 1-second L1 OFI, fitted linear scaling.
    """

    alpha_id = "EQ02"
    name = "ofi_l1"
    asset_class = "EQUITY"
    horizon = "5s"
    features = ("ofi_norm_l1_w1s_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "ofi_norm_l1_w1s_v1")


class EQ03OfiMultiLevel(LinearAlpha):
    """Multi-level OFI alpha.

    Economic rationale: order-flow imbalance deeper in the book carries
    slower but complementary information — quote revisions at levels 2-5
    anticipate future touch pressure before it reaches L1.  Combining L1 and
    L5 OFI over two windows (pinned weights 0.5/0.3/0.2) captures both the
    immediate and the building imbalance; the combination is then linearly
    scaled to the fitted expected return.
    """

    alpha_id = "EQ03"
    name = "ofi_multilevel"
    asset_class = "EQUITY"
    horizon = "5s"
    features = ("ofi_norm_l1_w1s_v1", "ofi_norm_l5_w1s_v1", "ofi_norm_l5_w5s_v1")
    WEIGHTS = (0.5, 0.3, 0.2)  # pinned combination weights

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        w1, w2, w3 = self.WEIGHTS
        return (
            w1 * col(df, "ofi_norm_l1_w1s_v1")
            + w2 * col(df, "ofi_norm_l5_w1s_v1")
            + w3 * col(df, "ofi_norm_l5_w5s_v1")
        )


class EQ04TradeFlow(LinearAlpha):
    """Trade-flow imbalance alpha.

    Economic rationale: aggressive (marketable) order flow is informed on
    average; an excess of buy-aggressor over sell-aggressor volume signals
    private information or urgent demand that continues to move the price
    after the prints.  Signal: 10-second trade imbalance
    ``(buys - sells)/(buys + sells)``, fitted linear scaling.
    """

    alpha_id = "EQ04"
    name = "trade_flow_imbalance"
    asset_class = "EQUITY"
    horizon = "5s"
    features = ("trade_imbalance_w10s_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "trade_imbalance_w10s_v1")


class EQ05QueueDynamics(LinearAlpha):
    """Queue depletion/replenishment alpha.

    Economic rationale: the touch queue that is being eaten (depleted) is
    about to fail — price ticks away from it — while the queue being
    replenished is defended.  Ask depletion plus bid replenishment is upward
    pressure; the normalized net rate over 1s is the signal, with fitted
    linear scaling.
    """

    alpha_id = "EQ05"
    name = "queue_dynamics"
    asset_class = "EQUITY"
    horizon = "1s"
    features = (
        "queue_depletion_rate_bid_w1s_v1",
        "queue_depletion_rate_ask_w1s_v1",
        "queue_replenish_rate_bid_w1s_v1",
        "queue_replenish_rate_ask_w1s_v1",
    )

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        dep_b = col(df, "queue_depletion_rate_bid_w1s_v1")
        dep_a = col(df, "queue_depletion_rate_ask_w1s_v1")
        rep_b = col(df, "queue_replenish_rate_bid_w1s_v1")
        rep_a = col(df, "queue_replenish_rate_ask_w1s_v1")
        up = dep_a + rep_b
        down = dep_b + rep_a
        return (up - down) / (up + down + EPS)


class EQ06Momentum(LinearAlpha):
    """Short-horizon momentum alpha.

    Economic rationale: order flow is autocorrelated (order splitting,
    herding), so a volatility-adjusted 10-second drift tends to continue
    over the next few seconds before liquidity providers fully adjust.
    Signal: ``ret_log_10s / rvol_1m`` (the vol-adjusted return feature),
    fitted linear scaling.
    """

    alpha_id = "EQ06"
    name = "short_horizon_momentum"
    asset_class = "EQUITY"
    horizon = "10s"
    features = ("ret_vol_adj_10s_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "ret_vol_adj_10s_v1")


class EQ07MeanReversion(LinearAlpha):
    """Short-horizon mean-reversion alpha.

    Economic rationale: very-short-horizon moves overshoot — a 1-second
    vol-adjusted jump reflects transient liquidity demand (temporary impact)
    that decays, pulling the mid partially back.  Signal: negative of the
    1-second vol-adjusted return, fitted linear scaling.
    """

    alpha_id = "EQ07"
    name = "short_horizon_reversion"
    asset_class = "EQUITY"
    horizon = "10s"
    features = ("ret_vol_adj_1s_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return -col(df, "ret_vol_adj_1s_v1")


class EQ08VwapDeviation(LinearAlpha):
    """VWAP/mid deviation alpha.

    Economic rationale: execution algos benchmark to VWAP; when the mid
    trades rich to the rolling volume-weighted average price, algo sell
    pressure (and buyer patience) pulls it back toward VWAP, and vice versa.
    Signal: negative deviation of mid from a trailing 5-minute
    activity-weighted average price, in bps.  The frames carry no per-trade
    prices, so VWAP is proxied by the traded-volume-weighted rolling mean of
    the mid (weight = ``traded_volume_w10s_v1``); stated here per the honest
    reporting rule.
    """

    alpha_id = "EQ08"
    name = "vwap_mid_deviation"
    asset_class = "EQUITY"
    horizon = "10s"
    features = ("mid_price_v1", "traded_volume_w10s_v1")
    WINDOW = "300s"  # pinned trailing window

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        mid = col(df, "mid_price_v1").to_numpy(dtype=float)
        w = col(df, "traded_volume_w10s_v1").to_numpy(dtype=float)
        w = np.where(np.isfinite(w), np.maximum(w, 0.0), 0.0)
        ok = np.isfinite(mid)
        pw = np.where(ok, mid * w, 0.0)
        ww = np.where(ok, w, 0.0)
        idx = pd.to_datetime(df["exchange_ts"].to_numpy(), unit="ns")
        num = pd.Series(pw, index=idx).rolling(self.WINDOW).sum().to_numpy()
        den = pd.Series(ww, index=idx).rolling(self.WINDOW).sum().to_numpy()
        vwap = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
        dev_bps = np.where(
            ok & np.isfinite(vwap) & (mid > 0), (mid - vwap) / mid * 1e4, np.nan
        )
        return pd.Series(-dev_bps, index=df.index)


class EQ09ResidualReversion(LinearAlpha):
    """Sector-relative / residual alpha.

    Economic rationale: moves not explained by the common factor are
    disproportionately liquidity noise and revert as arbitrageurs trade the
    spread back.  The synthetic universe has no sector taxonomy, so the
    "sector" factor is the single index ETF (stated honestly): the signal is
    the negative beta-residual 10s return ``-(ret_log_10s - beta *
    ref_ret_10s)`` provided by the feature factory, fitted linear scaling.
    The ETF itself is excluded (its residual is identically 0).
    """

    alpha_id = "EQ09"
    name = "index_residual_reversion"
    asset_class = "EQUITY"
    horizon = "10s"
    features = ("ret_resid_10s_v1",)

    def universe(self, instrument_ids):
        return sorted(i for i in instrument_ids if i in EQ_CONSTITUENT_IDS)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return -col(df, "ret_resid_10s_v1")


class EQ10IndexLeadLag(LinearAlpha):
    """Index-constituent lead-lag alpha.

    Economic rationale: the index instrument aggregates flow across all
    constituents and updates faster than the small names; a move in the
    index that a constituent has not yet matched predicts the constituent
    catching up (classic ETF->constituent lead-lag).  Signal: the reference
    (ETF) 1-second return ``ref_ret_1s_v1``, fitted linear scaling; the ETF
    itself is excluded from the universe.
    """

    alpha_id = "EQ10"
    name = "index_lead_lag"
    asset_class = "EQUITY"
    horizon = "1s"
    features = ("ref_ret_1s_v1",)

    def universe(self, instrument_ids):
        return sorted(
            i for i in instrument_ids if i in EQ_CONSTITUENT_IDS and i != ETF_ID
        )

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "ref_ret_1s_v1")


class EQ12LiquidityConditionedOfi(LinearAlpha):
    """Liquidity/regime-conditioned alpha.

    Economic rationale: the price impact of a unit of order-flow imbalance
    is inversely proportional to available liquidity — the same OFI moves a
    thin book further than a deep one.  Signal: 1-second normalized L1 OFI
    scaled by ``2/(1 + liq_regime_ratio)`` (ratio of current quoted depth to
    its 1-minute mean): weight 1 in normal liquidity, up to 2 when the book
    thins, down toward 2/3 when unusually deep.  Fitted linear scaling.
    """

    alpha_id = "EQ12"
    name = "liquidity_conditioned_ofi"
    asset_class = "EQUITY"
    horizon = "5s"
    features = ("ofi_norm_l1_w1s_v1", "liq_regime_ratio_v1")

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        ofi = col(df, "ofi_norm_l1_w1s_v1")
        ratio = col(df, "liq_regime_ratio_v1")
        weight = 2.0 / (1.0 + ratio.clip(lower=0.0))
        return ofi * weight
