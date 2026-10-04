package com.iap.risk;

import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;

/**
 * Hard-risk limits ({@code configs/risk/risk.json}, x-version 3 — the complete
 * pinned limit set plus the currency block; Rust reference
 * {@code rust/risk/src/limits.rs}).
 * Parsing is STRICT: any missing or invalid limit is an
 * {@link IllegalArgumentException}, and the engine built from a failed
 * parse is fail-closed (rejects every order with {@code CONFIG_MISSING}).
 */
public record RiskLimits(
        boolean killSwitchEngaged,
        double maxGrossNotional,
        double maxNetNotional,
        double maxDailyLoss,
        double maxOrderRatePerSec,
        double orderRateBurst,
        long maxOrderQty,
        double maxOrderNotional,
        double priceBandBps,
        boolean staleBookReject,
        long duplicateOrderWindowNs,
        long maxPositionQty,
        double maxInstrumentNotional,
        double strategyMaxDailyLoss,
        long maxSequenceGapBeforeHalt,
        long staleFeedTimeoutNs,
        String reportingCcy,
        TreeMap<String, FxConversion> fxConversion) {

    /**
     * How one quote currency converts into the reporting currency: the
     * last consolidated mid of {@code instrumentId} (an FX pair), inverted
     * when the pair is quoted REPORTING/CCY (e.g. USD/JPY for JPY).
     */
    public record FxConversion(long instrumentId, boolean invert) {
    }

    private static TreeMap<String, FxConversion> parseConversion(
            Map<String, Object> doc) {
        Object table = section(doc, "currency").get("conversion");
        if (!(table instanceof Map)) {
            throw new IllegalArgumentException(
                    "risk.json: missing currency.conversion object");
        }
        // Entries are validated in the reference's order (its JSON object
        // is a sorted map) and with its messages, so the first offending
        // entry — and therefore the CONFIG_MISSING reason — is the same: a
        // spec that is not an object has no instrument_id; a negative or
        // non-integer id is "missing/invalid"; 0 and anything above
        // u32::MAX is "out of u32 range".
        TreeMap<String, Object> sorted = new TreeMap<>(RiskEngine.CODE_POINT_ORDER);
        sorted.putAll(Json.object(table));
        TreeMap<String, FxConversion> out = new TreeMap<>(RiskEngine.CODE_POINT_ORDER);
        for (Map.Entry<String, Object> e : sorted.entrySet()) {
            Map<String, Object> spec = e.getValue() instanceof Map
                    ? Json.object(e.getValue()) : Map.of();
            Object iid = spec.get("instrument_id");
            if (!(iid instanceof Long) || (Long) iid < 0) {
                throw new IllegalArgumentException(
                        "risk.json: currency.conversion." + e.getKey()
                                + ".instrument_id missing/invalid");
            }
            if ((Long) iid == 0 || (Long) iid > 0xFFFFFFFFL) {
                throw new IllegalArgumentException(
                        "risk.json: currency.conversion." + e.getKey()
                                + ".instrument_id out of u32 range");
            }
            Object inv = spec.get("invert");
            if (!(inv instanceof Boolean)) {
                throw new IllegalArgumentException(
                        "risk.json: currency.conversion." + e.getKey()
                                + ".invert missing/non-bool");
            }
            out.put(e.getKey(), new FxConversion((Long) iid, (Boolean) inv));
        }
        return out;
    }

    /**
     * {@code doc[section]} with the reference's indexing semantics: a
     * missing or non-object section has no keys, so the error names the
     * first KEY that is then missing (never the section), exactly as
     * {@code serde_json}'s {@code doc[section][key]} yields Null.
     */
    private static Map<String, Object> section(Map<String, Object> doc, String name) {
        Object s = doc.get(name);
        return s instanceof Map ? Json.object(s) : Map.of();
    }

    private static double needF64(Map<String, Object> doc, String section, String key) {
        Object v = section(doc, section).get(key);
        if (!(v instanceof Long) && !(v instanceof Double)) {
            throw new IllegalArgumentException(
                    "risk.json: missing/non-numeric " + section + "." + key);
        }
        double d = Json.asDouble(v);
        if (!Double.isFinite(d)) {
            throw new IllegalArgumentException(
                    "risk.json: non-finite " + section + "." + key);
        }
        return d;
    }

    private static double needPosF64(Map<String, Object> doc, String section, String key) {
        double v = needF64(doc, section, key);
        if (v <= 0.0) {
            throw new IllegalArgumentException(
                    "risk.json: " + section + "." + key + " must be > 0, got "
                            + OrderRequest.rustDisplay(v));
        }
        return v;
    }

    private static long needPosI64(Map<String, Object> doc, String section, String key) {
        Object v = section(doc, section).get(key);
        if (!(v instanceof Long)) {
            throw new IllegalArgumentException(
                    "risk.json: missing/non-integer " + section + "." + key);
        }
        long l = (Long) v;
        if (l <= 0) {
            throw new IllegalArgumentException(
                    "risk.json: " + section + "." + key + " must be > 0, got " + l);
        }
        return l;
    }

    private static boolean needBool(Map<String, Object> doc, String section, String key) {
        Object v = section(doc, section).get(key);
        if (!(v instanceof Boolean)) {
            throw new IllegalArgumentException(
                    "risk.json: missing/non-bool " + section + "." + key);
        }
        return (Boolean) v;
    }

    /** Strict parse of a {@code configs/risk/risk.json} document. */
    public static RiskLimits fromJson(Map<String, Object> doc) {
        Object dupRaw = section(doc, "per_order").get("duplicate_order_window_ns");
        if (!(dupRaw instanceof Long)) {
            throw new IllegalArgumentException(
                    "risk.json: missing per_order.duplicate_order_window_ns");
        }
        long dup = (Long) dupRaw;
        if (dup < 0) {
            throw new IllegalArgumentException(
                    "risk.json: duplicate_order_window_ns must be >= 0");
        }
        // Arguments are evaluated left to right, i.e. in the reference's
        // field order: the FIRST offending key decides the error, and that
        // text is the CONFIG_MISSING reason every order is then audited with.
        return new RiskLimits(
                needBool(doc, "global", "kill_switch_engaged"),
                needPosF64(doc, "global", "max_gross_notional"),
                needPosF64(doc, "global", "max_net_notional"),
                needPosF64(doc, "global", "max_daily_loss"),
                needPosF64(doc, "global", "max_order_rate_per_sec"),
                needPosF64(doc, "global", "order_rate_burst"),
                needPosI64(doc, "per_order", "max_order_qty"),
                needPosF64(doc, "per_order", "max_order_notional"),
                needPosF64(doc, "per_order", "price_band_bps"),
                needBool(doc, "per_order", "stale_book_reject"),
                dup,
                needPosI64(doc, "per_instrument", "max_position_qty"),
                needPosF64(doc, "per_instrument", "max_instrument_notional"),
                needPosF64(doc, "per_strategy", "max_daily_loss"),
                sequenceGaps(doc),
                needPosI64(doc, "market_data", "stale_feed_timeout_ns"),
                reportingCcy(doc),
                parseConversion(doc));
    }

    private static long sequenceGaps(Map<String, Object> doc) {
        Object v = section(doc, "market_data").get("max_sequence_gap_before_halt");
        if (!(v instanceof Long) || (Long) v < 0) {
            throw new IllegalArgumentException(
                    "risk.json: missing market_data.max_sequence_gap_before_halt");
        }
        return (Long) v;
    }

    private static String reportingCcy(Map<String, Object> doc) {
        Object v = section(doc, "currency").get("reporting_ccy");
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw new IllegalArgumentException(
                    "risk.json: missing/empty currency.reporting_ccy");
        }
        return (String) v;
    }
}
