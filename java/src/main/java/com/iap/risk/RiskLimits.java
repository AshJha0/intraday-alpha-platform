package com.iap.risk;

import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;

/**
 * Hard-risk limits ({@code configs/risk.json}, x-version 3 — the complete
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
        TreeMap<String, FxConversion> out = new TreeMap<>();
        for (Map.Entry<String, Object> e : Json.object(table).entrySet()) {
            if (!(e.getValue() instanceof Map)) {
                throw new IllegalArgumentException(
                        "risk.json: currency.conversion." + e.getKey()
                                + " must be an object");
            }
            Map<String, Object> spec = Json.object(e.getValue());
            Object iid = spec.get("instrument_id");
            if (!(iid instanceof Long) || (Long) iid <= 0
                    || (Long) iid > 0xFFFFFFFFL) {
                throw new IllegalArgumentException(
                        "risk.json: currency.conversion." + e.getKey()
                                + ".instrument_id missing/invalid");
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

    private static Map<String, Object> section(Map<String, Object> doc, String name) {
        Object s = doc.get(name);
        if (!(s instanceof Map)) {
            throw new IllegalArgumentException("risk.json: missing section " + name);
        }
        return Json.object(s);
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
                    "risk.json: " + section + "." + key + " must be > 0, got " + v);
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

    /** Strict parse of a {@code configs/risk.json} document. */
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
        Object gapRaw = section(doc, "market_data").get("max_sequence_gap_before_halt");
        if (!(gapRaw instanceof Long) || (Long) gapRaw < 0) {
            throw new IllegalArgumentException(
                    "risk.json: missing market_data.max_sequence_gap_before_halt");
        }
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
                (Long) gapRaw,
                needPosI64(doc, "market_data", "stale_feed_timeout_ns"),
                reportingCcy(doc),
                parseConversion(doc));
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
