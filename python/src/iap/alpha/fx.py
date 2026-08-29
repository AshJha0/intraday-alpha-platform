"""FX flagship alphas FX01-FX04 and FX07-FX12 (spec §12).

FX05/FX06 (currency-exposure machinery) live in ``iap.alpha.fx_exposure``.

The synthetic FX data is QUOTE+TRADE across three LPs (LP1/LP2/PRI) with no
futures market and no macro-event feed; where an alpha's premise needs data
the generator cannot supply, the machinery is implemented in full against
the best available proxy and the substitution is stated in the rationale
(honest-reporting rule, conventions §7).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from iap.alpha.base import FX_REF_ID, LinearAlpha, col

NS_S = 1_000_000_000


class FX01QuoteImbalance(LinearAlpha):
    """Quote/microprice imbalance alpha.

    Economic rationale: on a quote-driven market the size-weighted
    microprice across LPs leads the mid — an LP showing more size on the bid
    than the ask is skewing its quote because it expects (or is positioned
    for) an up-move; the mid migrates toward the microprice.  Signal:
    ``micro_mid_dev_bps_v1`` on the consolidated FX book, fitted linear
    scaling.
    """

    alpha_id = "FX01"
    name = "fx_quote_imbalance"
    asset_class = "FX"
    horizon = "500ms"
    features = ("micro_mid_dev_bps_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "micro_mid_dev_bps_v1")


class FX02TradeFlow(LinearAlpha):
    """FX trade-flow imbalance alpha.

    Economic rationale: FX dealer flow is informed at short horizons —
    aggressor imbalance against LP quotes reveals directional demand that
    LPs subsequently hedge, moving the price further in the flow direction.
    Signal: 1-minute trade imbalance ``(buys - sells)/(buys + sells)``,
    fitted linear scaling.
    """

    alpha_id = "FX02"
    name = "fx_trade_flow"
    asset_class = "FX"
    horizon = "1m"
    features = ("trade_imbalance_w1m_v1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "trade_imbalance_w1m_v1")


class FX03MultiVenueOfi(LinearAlpha):
    """Multi-venue order-flow imbalance alpha.

    Economic rationale: OFI on the consolidated multi-LP book aggregates
    quote revisions across venues; when several LPs simultaneously add bid
    size / pull ask size the excess demand is broader than any single
    venue's noise and pushes the consolidated mid.  Signal: pinned 0.6/0.4
    blend of depth-normalized L1 and L5 OFI over 30s on the merged book,
    fitted linear scaling.
    """

    alpha_id = "FX03"
    name = "fx_multivenue_ofi"
    asset_class = "FX"
    horizon = "1m"
    features = ("ofi_norm_l1_w30s_v1", "ofi_norm_l5_w30s_v1")
    WEIGHTS = (0.6, 0.4)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        w1, w2 = self.WEIGHTS
        return w1 * col(df, "ofi_norm_l1_w30s_v1") + w2 * col(df, "ofi_norm_l5_w30s_v1")


class FX04CrossVenueLeadLag(LinearAlpha):
    """Cross-venue lead-lag alpha.

    Economic rationale: when one LP's quote disagrees with the others, the
    venue that updated most recently usually carries the news and the rest
    converge to it; large cross-venue imbalance divergence therefore marks
    moments when the consolidated imbalance is about to be validated by the
    lagging venues.  Data limitation (stated honestly): the feature frames
    carry no per-venue mid series, so the leader's direction is proxied by
    the consolidated L1 imbalance amplified by the (unsigned) cross-venue
    imbalance divergence: ``imbalance_l1 * venue_imbalance_divergence``.
    Fitted linear scaling.
    """

    alpha_id = "FX04"
    name = "fx_cross_venue_leadlag"
    asset_class = "FX"
    horizon = "30s"
    features = ("imbalance_l1_v1", "venue_imbalance_divergence_v1")

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "imbalance_l1_v1") * col(df, "venue_imbalance_divergence_v1")


class FX07FuturesSpotLeadLag(LinearAlpha):
    """Futures-to-spot lead-lag alpha.

    Economic rationale: listed FX futures aggregate speculative flow and
    typically lead spot by tens of milliseconds to seconds; spot pairs catch
    up.  Data limitation (stated honestly): the synthetic universe has no
    futures market, so the leader is proxied by the deepest, fastest pair —
    EUR/USD, which is also the platform's pinned cross-asset reference — and
    the machinery is exactly the production lead-lag machinery: the signal
    is the reference instrument's trailing 30-second return
    ``ref_ret_30s_v1`` applied to every other pair, fitted linear scaling.
    EUR/USD itself is excluded (its reference is itself).
    """

    alpha_id = "FX07"
    name = "fx_futures_spot_leadlag"
    asset_class = "FX"
    horizon = "1m"
    features = ("ref_ret_30s_v1",)

    def universe(self, instrument_ids):
        return [i for i in super().universe(instrument_ids) if i != FX_REF_ID]

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "ref_ret_30s_v1")


class FX08LiquidityConditionedMomentum(LinearAlpha):
    """Liquidity/regime-conditioned momentum alpha.

    Economic rationale: momentum in FX continues when the move happened
    through a *liquid* market — a drift absorbed by full-size quotes
    reflects genuine repricing, while the same drift through an empty book
    is likely a liquidity hole that snaps back.  Signal: 1-minute
    vol-adjusted return gated to zero unless current quoted depth exceeds
    its 1-minute mean (``liq_regime_flag`` = 1).  Fitted linear scaling.
    """

    alpha_id = "FX08"
    name = "fx_liquidity_conditioned_momentum"
    asset_class = "FX"
    horizon = "1m"
    features = ("ret_vol_adj_1m_v1", "liq_regime_flag_v1")

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return col(df, "ret_vol_adj_1m_v1") * col(df, "liq_regime_flag_v1")


class FX09VolRegimeReversion(LinearAlpha):
    """Volatility-regime alpha.

    Economic rationale: when short-horizon volatility runs above its
    longer-horizon baseline the market is in an overreaction regime — LPs
    widen and thin their quotes, marginal flow moves the price too far, and
    prices partially revert once vol normalizes.  The reversion edge scales
    with how elevated the regime is.  Signal: negative 10-second
    vol-adjusted return multiplied by the vol-regime ratio
    (``rvol_1m / rvol_5m``): ``-ret_vol_adj_10s * vol_regime_ratio``.
    Fitted linear scaling.
    """

    alpha_id = "FX09"
    name = "fx_vol_regime_reversion"
    asset_class = "FX"
    horizon = "1m"
    features = ("ret_vol_adj_10s_v1", "vol_regime_ratio_v1")

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return -col(df, "ret_vol_adj_10s_v1") * col(df, "vol_regime_ratio_v1")


class FX10SessionTransition(LinearAlpha):
    """Session-transition effects alpha.

    Economic rationale: around session hand-offs (Asia->London, London->NY,
    session open/close) hedging and benchmark flows are one-sided and
    liquidity is rebuilding, so drifts started in the transition hour tend
    to extend.  Data limitation (stated honestly): the synthetic FX session
    is one continuous 00:00-21:00 UTC session with no true regional
    structure, so the transition windows are pinned proxy hours at
    conventional times — 00-01h (open), 07-08h (London), 13-14h (NY),
    20-21h (close).  Signal: 1-minute vol-adjusted return gated to the
    transition windows, fitted linear scaling.
    """

    alpha_id = "FX10"
    name = "fx_session_transition"
    asset_class = "FX"
    horizon = "1m"
    features = ("ret_vol_adj_1m_v1", "minute_of_day_v1")
    TRANSITION_HOURS = (0, 7, 13, 20)  # pinned proxy transition hours (UTC)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        minute = col(df, "minute_of_day_v1")
        hour = np.floor(minute / 60.0)
        gate = hour.isin(self.TRANSITION_HOURS).astype(float)
        gate[~np.isfinite(minute)] = np.nan
        return col(df, "ret_vol_adj_1m_v1") * gate


class FX11MacroSurpriseResponse(LinearAlpha):
    """Macro-event surprise response alpha.

    Economic rationale: macro releases whose print deviates from consensus
    cause an initial jump plus a continued drift in the surprise direction
    as slower capital reprices.  Data limitation (stated honestly): the
    synthetic generator injects NO macro calendar or consensus data, so the
    full event-study machinery here runs against generator-injected proxy
    events — volatility-acceleration bursts (rising edges of
    ``vol_ratio_w10s_w1m > 2.2``, i.e. essentially all of the last minute's
    mid moves happened in the last 10 seconds — the signature the
    generator's vol-regime switches and clustered flow inject).  The
    "surprise" is the signed vol-adjusted 1-minute return at the detected
    event; the signal decays exponentially (tau = 5m) from the event and
    expires after 15m.  With a real macro feed only the event source
    changes; results on this proxy say nothing about true macro alpha and
    are reported as such.
    """

    alpha_id = "FX11"
    name = "fx_macro_surprise_proxy"
    asset_class = "FX"
    horizon = "5m"
    features = ("vol_ratio_w10s_w1m_v1", "ret_vol_adj_1m_v1")
    EVENT_THRESHOLD = 2.2    # vol-acceleration ratio marking a proxy event
    TAU_NS = 300 * NS_S      # decay time constant (5m)
    EXPIRE_NS = 900 * NS_S   # signal expires 15m after the event

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        ts = df["exchange_ts"].to_numpy()
        flag = col(df, "vol_ratio_w10s_w1m_v1").to_numpy(dtype=float)
        surp = col(df, "ret_vol_adj_1m_v1").to_numpy(dtype=float)
        flag01 = np.where(np.isfinite(flag), flag, 0.0) > self.EVENT_THRESHOLD
        prev = np.concatenate(([False], flag01[:-1]))
        onset = flag01 & ~prev & np.isfinite(surp)  # rising edges w/ surprise
        ev_idx = np.flatnonzero(onset)
        out = np.zeros(len(ts))
        if ev_idx.size:
            ev_ts = ts[ev_idx]
            ev_surp = surp[ev_idx]
            last = np.searchsorted(ev_ts, ts, side="right") - 1
            has = last >= 0
            dt = np.zeros(len(ts))
            dt[has] = ts[has] - ev_ts[last[has]]
            decayed = np.zeros(len(ts))
            decayed[has] = ev_surp[last[has]] * np.exp(-dt[has] / self.TAU_NS)
            decayed[has & (dt > self.EXPIRE_NS)] = 0.0
            out = decayed
        out[~np.isfinite(flag)] = np.nan  # jump detector not warm yet
        return pd.Series(out, index=df.index)


class FX12VenueToxicity(LinearAlpha):
    """Venue-specific liquidity/toxicity alpha.

    Economic rationale: a consolidated imbalance is only actionable when it
    reflects broad, stable LP interest; when a single venue dominates the
    update flow (high update-concentration HHI) the "imbalance" is one LP
    flickering quotes — toxic liquidity that adversely selects takers.  The
    signal follows the L1 imbalance weighted down by venue update
    concentration: ``imbalance_l1 * (1 - venue_update_hhi_w10s)``, so a
    single-venue market contributes nothing.  Fitted linear scaling.
    """

    alpha_id = "FX12"
    name = "fx_venue_toxicity"
    asset_class = "FX"
    horizon = "1m"
    features = ("imbalance_l1_v1", "venue_update_hhi_w10s_v1")

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        hhi = col(df, "venue_update_hhi_w10s_v1")
        return col(df, "imbalance_l1_v1") * (1.0 - hhi).clip(lower=0.0)
