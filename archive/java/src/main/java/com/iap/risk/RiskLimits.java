package com.iap.risk;

import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

import com.iap.config.Json;

/**
 * Hard-risk limits ({@code configs/risk/risk.json}, x-version 4 — the complete
 * pinned limit set plus the currency block; Rust reference
 * {@code rust/risk/src/limits.rs}).
 * Parsing is STRICT: any missing or invalid limit is an
 * {@link IllegalArgumentException}, and the engine built from a failed
 * parse is fail-closed (rejects every order with {@code CONFIG_MISSING}).
 *
 * <p>x-version 4 adds OPTIONAL keys (absent or null = that control is off,
 * {@code null} here; a present key is parsed strictly):
 * {@code market_data.max_mid_jump_ratio}, the SEC 15c3-5 {@code pre_trade}
 * set (checks 23-29 and {@code max_tracked_keys}) and the {@code overrides}
 * bounds.
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
        TreeMap<String, FxConversion> fxConversion,
        Double maxMidJumpRatio,
        Set<Long> restrictedInstruments,
        Long maxOpenOrders,
        Double maxOpenNotional,
        Long maxStrategyPositionQty,
        Double maxParticipationBps,
        Long participationFloorQty,
        Double maxCancelsPerSec,
        Double cancelBurst,
        Double maxVenueMsgsPerSec,
        Double venueMsgBurst,
        Long maxTrackedKeys,
        Double maxLossOverrideMultiple,
        Long minApprovers) {

    /** The x-version 3 limit set: every x-version 4 control off. */
    public RiskLimits(boolean killSwitchEngaged, double maxGrossNotional,
            double maxNetNotional, double maxDailyLoss, double maxOrderRatePerSec,
            double orderRateBurst, long maxOrderQty, double maxOrderNotional,
            double priceBandBps, boolean staleBookReject, long duplicateOrderWindowNs,
            long maxPositionQty, double maxInstrumentNotional,
            double strategyMaxDailyLoss, long maxSequenceGapBeforeHalt,
            long staleFeedTimeoutNs, String reportingCcy,
            TreeMap<String, FxConversion> fxConversion) {
        this(killSwitchEngaged, maxGrossNotional, maxNetNotional, maxDailyLoss,
                maxOrderRatePerSec, orderRateBurst, maxOrderQty, maxOrderNotional,
                priceBandBps, staleBookReject, duplicateOrderWindowNs, maxPositionQty,
                maxInstrumentNotional, strategyMaxDailyLoss, maxSequenceGapBeforeHalt,
                staleFeedTimeoutNs, reportingCcy, fxConversion, null, Set.of(), null,
                null, null, null, null, null, null, null, null, null, null, null);
    }

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

    /** An optional number key: null when absent or JSON null. */
    private static Double optPosF64(Map<String, Object> doc, String section, String key) {
        return section(doc, section).get(key) == null ? null
                : needPosF64(doc, section, key);
    }

    /** An optional integer key: null when absent or JSON null. */
    private static Long optPosI64(Map<String, Object> doc, String section, String key) {
        return section(doc, section).get(key) == null ? null
                : needPosI64(doc, section, key);
    }

    /** {@code pre_trade.restricted_instruments}: absent / null = empty. */
    private static Set<Long> parseRestricted(Map<String, Object> doc) {
        Object v = section(doc, "pre_trade").get("restricted_instruments");
        if (v == null) {
            return Set.of();
        }
        if (!(v instanceof List)) {
            throw new IllegalArgumentException(
                    "risk.json: pre_trade.restricted_instruments must be an array");
        }
        TreeSet<Long> out = new TreeSet<>();
        for (Object item : Json.array(v)) {
            if (!(item instanceof Long) || (Long) item < 0 || (Long) item > 0xFFFFFFFFL) {
                throw new IllegalArgumentException("risk.json: pre_trade.restricted_instruments"
                        + " entries must be u32 instrument ids");
            }
            out.add((Long) item);
        }
        return Collections.unmodifiableSet(out);
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
        RiskLimits base = new RiskLimits(
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
        // x-version 4 keys, parsed in the reference's order
        Double jump = optPosF64(doc, "market_data", "max_mid_jump_ratio");
        if (jump != null && jump <= 1.0) {
            throw new IllegalArgumentException(
                    "risk.json: market_data.max_mid_jump_ratio must be > 1, got "
                            + OrderRequest.rustDisplay(jump));
        }
        Set<Long> restricted = parseRestricted(doc);
        Long maxOpenOrders = optPosI64(doc, "pre_trade", "max_open_orders");
        Double maxOpenNotional = optPosF64(doc, "pre_trade", "max_open_notional");
        Long maxStrategyPos = optPosI64(doc, "pre_trade", "max_strategy_position_qty");
        Double partBps = optPosF64(doc, "pre_trade", "max_participation_bps");
        Long partFloor = partBps == null ? null
                : needPosI64(doc, "pre_trade", "participation_floor_qty");
        Double cancelRate = optPosF64(doc, "pre_trade", "max_cancels_per_sec");
        Double cancelBurst = cancelRate == null ? null
                : needPosF64(doc, "pre_trade", "cancel_burst");
        Double venueRate = optPosF64(doc, "pre_trade", "max_venue_msgs_per_sec");
        Double venueBurst = venueRate == null ? null
                : needPosF64(doc, "pre_trade", "venue_msg_burst");
        Long maxKeys = optPosI64(doc, "pre_trade", "max_tracked_keys");
        Double overrideMult = optPosF64(doc, "overrides", "max_loss_override_multiple");
        Long minApprovers = optPosI64(doc, "overrides", "min_approvers");
        return new RiskLimits(base.killSwitchEngaged(), base.maxGrossNotional(),
                base.maxNetNotional(), base.maxDailyLoss(), base.maxOrderRatePerSec(),
                base.orderRateBurst(), base.maxOrderQty(), base.maxOrderNotional(),
                base.priceBandBps(), base.staleBookReject(), base.duplicateOrderWindowNs(),
                base.maxPositionQty(), base.maxInstrumentNotional(),
                base.strategyMaxDailyLoss(), base.maxSequenceGapBeforeHalt(),
                base.staleFeedTimeoutNs(), base.reportingCcy(), base.fxConversion(),
                jump, restricted, maxOpenOrders, maxOpenNotional, maxStrategyPos,
                partBps, partFloor, cancelRate, cancelBurst, venueRate, venueBurst,
                maxKeys, overrideMult, minApprovers);
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
