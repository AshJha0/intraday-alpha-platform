//! Pinned feature-name table for the native engine.
//!
//! The first [`NATIVE_COUNT`] entries are the 40 pinned native features of
//! `/API_FEATURES.md` §3; the remaining entries are auxiliary features the
//! engine also computes natively because the production alphas (API_ALPHA.md
//! §4) consume them — their formulas are pinned by the Python reference
//! (`ofi_norm` in `iap/features/orderflow.py`, `ret_vol_adj` in `price.py`,
//! `vol_regime_ratio` in `regime.py`).
//!
//! Golden comparisons are by feature *name* (API_FEATURES.md §1): this crate
//! exposes a documented sub-vector of the 205-feature registry, indexed by
//! the registry names below.

/// Number of pinned native features (API_FEATURES.md §3).
pub const NATIVE_COUNT: usize = 40;

/// Total features this engine computes (native 40 + 5 auxiliary).
pub const FEATURE_COUNT: usize = 45;

/// Registry names in this engine's pinned slot order.
pub const FEATURE_NAMES: [&str; FEATURE_COUNT] = [
    // --- order-flow imbalance (12) ------------------------------------
    "ofi_l1_w1s_v1",
    "ofi_l1_w5s_v1",
    "ofi_l1_w30s_v1",
    "ofi_l3_w1s_v1",
    "ofi_l3_w5s_v1",
    "ofi_l3_w30s_v1",
    "ofi_l5_w1s_v1",
    "ofi_l5_w5s_v1",
    "ofi_l5_w30s_v1",
    "ofi_l10_w1s_v1",
    "ofi_l10_w5s_v1",
    "ofi_l10_w30s_v1",
    // --- book imbalance (4) -------------------------------------------
    "imbalance_l1_v1",
    "imbalance_l3_v1",
    "imbalance_l5_v1",
    "imbalance_l10_v1",
    // --- microprice / mid / spread (5) --------------------------------
    "mid_price_v1",
    "microprice_v1",
    "micro_mid_dev_bps_v1",
    "spread_ticks_v1",
    "spread_bps_v1",
    // --- depth (6) ----------------------------------------------------
    "depth_bid_l1_v1",
    "depth_ask_l1_v1",
    "depth_bid_l5_v1",
    "depth_ask_l5_v1",
    "depth_bid_l10_v1",
    "depth_ask_l10_v1",
    // --- signed trade volume (3) --------------------------------------
    "signed_volume_w1s_v1",
    "signed_volume_w10s_v1",
    "signed_volume_w1m_v1",
    // --- trade imbalance (3) ------------------------------------------
    "trade_imbalance_w1s_v1",
    "trade_imbalance_w10s_v1",
    "trade_imbalance_w1m_v1",
    // --- realized volatility (3) --------------------------------------
    "rvol_w10s_v1",
    "rvol_w1m_v1",
    "rvol_w5m_v1",
    // --- returns (4) --------------------------------------------------
    "ret_simple_1s_v1",
    "ret_log_1s_v1",
    "ret_log_10s_v1",
    "ret_log_1m_v1",
    // --- auxiliary (5, alpha inputs; not part of the native 40) -------
    "ofi_norm_l1_w1s_v1",
    "ofi_norm_l5_w1s_v1",
    "ofi_norm_l5_w5s_v1",
    "ret_vol_adj_10s_v1",
    "vol_regime_ratio_v1",
];

/// Slot index for a registry feature name (`None` when not computed here).
pub fn feature_index(name: &str) -> Option<usize> {
    FEATURE_NAMES.iter().position(|&n| n == name)
}
