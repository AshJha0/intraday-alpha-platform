package com.iap.trace;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code schemas/execution/execution_report.schema.json} (x-version 1):
 * one venue execution report. {@code status} is NEW=1 PARTIAL=2 FILLED=3
 * CANCELED=4 REJECTED=5 EXPIRED=6; {@code filledQty} / {@code fillPriceTicks}
 * describe THIS report's fill (0 for non-fill statuses); {@code fees}
 * signed, negative = rebate.
 */
public record ExecutionReportRec(long orderId, long executionId, int status,
        long filledQty, long fillPriceTicks, int venueId, long exchangeTs,
        long receiveTs, double fees) {
    private static final String[] KEYS = {"order_id", "execution_id", "status",
        "filled_qty", "fill_price_ticks", "venue_id", "exchange_ts", "receive_ts",
        "fees"};

    /** Wire codes. */
    public static final int NEW = 1;
    public static final int PARTIAL = 2;
    public static final int FILLED = 3;
    public static final int CANCELED = 4;
    public static final int REJECTED = 5;
    public static final int EXPIRED = 6;

    public ExecutionReportRec {
        if (status < NEW || status > EXPIRED) {
            throw new IllegalArgumentException("ExecutionReport.status must be 1..6");
        }
        if (filledQty < 0 || fillPriceTicks < 0) {
            throw new IllegalArgumentException(
                    "ExecutionReport: filled_qty/fill_price_ticks must be >= 0");
        }
        if (venueId < 0 || venueId > 0xFFFF) {
            throw new IllegalArgumentException("ExecutionReport.venue_id outside u16");
        }
        if (receiveTs < exchangeTs) {
            throw new IllegalArgumentException("ExecutionReport: receive_ts < exchange_ts");
        }
        Trees.finite(fees, "ExecutionReport.fees");
        boolean isFill = status == PARTIAL || status == FILLED;
        if (isFill && filledQty <= 0) {
            throw new IllegalArgumentException(
                    "ExecutionReport: fill status needs filled_qty > 0");
        }
        if (!isFill && filledQty != 0) {
            throw new IllegalArgumentException(
                    "ExecutionReport: non-fill status carries filled_qty 0");
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("order_id", Trees.u64Tree(orderId));
        t.put("execution_id", Trees.u64Tree(executionId));
        t.put("status", (long) status);
        t.put("filled_qty", filledQty);
        t.put("fill_price_ticks", fillPriceTicks);
        t.put("venue_id", (long) venueId);
        t.put("exchange_ts", exchangeTs);
        t.put("receive_ts", receiveTs);
        t.put("fees", fees);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static ExecutionReportRec fromTree(Map<String, Object> t) {
        String p = "ExecutionReport";
        Trees.checkKeys(t, KEYS, p);
        return new ExecutionReportRec(Trees.u64(t, "order_id", p),
                Trees.u64(t, "execution_id", p),
                (int) Trees.ranged(t, "status", p, 1, 6),
                Trees.ranged(t, "filled_qty", p, 0, Long.MAX_VALUE),
                Trees.ranged(t, "fill_price_ticks", p, 0, Long.MAX_VALUE),
                Trees.u16(t, "venue_id", p), Trees.i64(t, "exchange_ts", p),
                Trees.i64(t, "receive_ts", p), Trees.num(t, "fees", p));
    }
}
