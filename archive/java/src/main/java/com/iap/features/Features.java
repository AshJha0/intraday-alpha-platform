package com.iap.features;

/**
 * Pinned feature-slot table for the native Java engine (API_FEATURES.md
 * section 3). The first {@link #NATIVE_COUNT} entries are the 40 pinned
 * native features; the remaining {@link #AUX_COUNT} are the auxiliary
 * alpha-input features the production alphas consume (API_ALPHA.md section
 * 4), computed natively with formulas pinned by the Python reference
 * ({@code ofi_norm} in iap/features/orderflow.py, {@code ret_vol_adj} in
 * price.py, {@code vol_regime_ratio} in regime.py). Slot order mirrors
 * rust/features/src/names.rs — golden comparisons are by feature NAME
 * (API_FEATURES.md section 1), so the order is an implementation pin only.
 */
public final class Features {
    /** Pinned windows (mirrors iap.features.spec.WINDOW_NS). */
    public static final long NS_PER_SEC = 1_000_000_000L;
    public static final long W_1S = 1 * NS_PER_SEC;
    public static final long W_5S = 5 * NS_PER_SEC;
    public static final long W_10S = 10 * NS_PER_SEC;
    public static final long W_30S = 30 * NS_PER_SEC;
    public static final long W_1M = 60 * NS_PER_SEC;
    public static final long W_5M = 300 * NS_PER_SEC;

    /** Pinned EPS used in every guarded division (spec EPS = 1e-12). */
    public static final double EPS = 1e-12;

    // Slot indices, pinned order (rust names.rs).
    public static final int OFI_L1_W1S = 0;
    public static final int OFI_L1_W5S = 1;
    public static final int OFI_L1_W30S = 2;
    public static final int OFI_L3_W1S = 3;
    public static final int OFI_L3_W5S = 4;
    public static final int OFI_L3_W30S = 5;
    public static final int OFI_L5_W1S = 6;
    public static final int OFI_L5_W5S = 7;
    public static final int OFI_L5_W30S = 8;
    public static final int OFI_L10_W1S = 9;
    public static final int OFI_L10_W5S = 10;
    public static final int OFI_L10_W30S = 11;
    public static final int IMBALANCE_L1 = 12;
    public static final int IMBALANCE_L3 = 13;
    public static final int IMBALANCE_L5 = 14;
    public static final int IMBALANCE_L10 = 15;
    public static final int MID_PRICE = 16;
    public static final int MICROPRICE = 17;
    public static final int MICRO_MID_DEV_BPS = 18;
    public static final int SPREAD_TICKS = 19;
    public static final int SPREAD_BPS = 20;
    public static final int DEPTH_BID_L1 = 21;
    public static final int DEPTH_ASK_L1 = 22;
    public static final int DEPTH_BID_L5 = 23;
    public static final int DEPTH_ASK_L5 = 24;
    public static final int DEPTH_BID_L10 = 25;
    public static final int DEPTH_ASK_L10 = 26;
    public static final int SIGNED_VOLUME_W1S = 27;
    public static final int SIGNED_VOLUME_W10S = 28;
    public static final int SIGNED_VOLUME_W1M = 29;
    public static final int TRADE_IMBALANCE_W1S = 30;
    public static final int TRADE_IMBALANCE_W10S = 31;
    public static final int TRADE_IMBALANCE_W1M = 32;
    public static final int RVOL_W10S = 33;
    public static final int RVOL_W1M = 34;
    public static final int RVOL_W5M = 35;
    public static final int RET_SIMPLE_1S = 36;
    public static final int RET_LOG_1S = 37;
    public static final int RET_LOG_10S = 38;
    public static final int RET_LOG_1M = 39;
    public static final int OFI_NORM_L1_W1S = 40;
    public static final int OFI_NORM_L5_W1S = 41;
    public static final int OFI_NORM_L5_W5S = 42;
    public static final int RET_VOL_ADJ_10S = 43;
    public static final int VOL_REGIME_RATIO = 44;

    /** Number of pinned native features (API_FEATURES.md section 3). */
    public static final int NATIVE_COUNT = 40;
    /** Auxiliary alpha-input features computed natively. */
    public static final int AUX_COUNT = 5;
    /** Total slots this engine computes. */
    public static final int COUNT = NATIVE_COUNT + AUX_COUNT;

    private static final String[] NAMES = {
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
        "imbalance_l1_v1",
        "imbalance_l3_v1",
        "imbalance_l5_v1",
        "imbalance_l10_v1",
        "mid_price_v1",
        "microprice_v1",
        "micro_mid_dev_bps_v1",
        "spread_ticks_v1",
        "spread_bps_v1",
        "depth_bid_l1_v1",
        "depth_ask_l1_v1",
        "depth_bid_l5_v1",
        "depth_ask_l5_v1",
        "depth_bid_l10_v1",
        "depth_ask_l10_v1",
        "signed_volume_w1s_v1",
        "signed_volume_w10s_v1",
        "signed_volume_w1m_v1",
        "trade_imbalance_w1s_v1",
        "trade_imbalance_w10s_v1",
        "trade_imbalance_w1m_v1",
        "rvol_w10s_v1",
        "rvol_w1m_v1",
        "rvol_w5m_v1",
        "ret_simple_1s_v1",
        "ret_log_1s_v1",
        "ret_log_10s_v1",
        "ret_log_1m_v1",
        "ofi_norm_l1_w1s_v1",
        "ofi_norm_l5_w1s_v1",
        "ofi_norm_l5_w5s_v1",
        "ret_vol_adj_10s_v1",
        "vol_regime_ratio_v1",
    };

    private Features() {
    }

    /** Registry name of a feature slot (e.g. {@code "ofi_l5_w5s_v1"}). */
    public static String name(int slot) {
        if (slot < 0 || slot >= COUNT) {
            throw new IllegalArgumentException("feature slot out of range: " + slot);
        }
        return NAMES[slot];
    }

    /** Slot for a registry name, or -1 when not implemented natively. */
    public static int index(String name) {
        for (int i = 0; i < COUNT; i++) {
            if (NAMES[i].equals(name)) {
                return i;
            }
        }
        return -1;
    }
}
