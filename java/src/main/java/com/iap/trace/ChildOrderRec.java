package com.iap.trace;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code schemas/order/child_order.schema.json} (x-version 1): one slice of
 * a parent order addressed to a venue (0 = SOR decides). {@code orderType}
 * is MARKET=1 LIMIT=2 IOC=3 FOK=4 PEG=5 MID=6; {@code priceTicks} 0 for
 * MARKET; {@code expireTs} 0 = good till the parent's {@code endTs}.
 */
public record ChildOrderRec(long childOrderId, long parentOrderId,
        long instrumentId, int venueId, int side, long qty, long priceTicks,
        int orderType, long submitTs, long expireTs, long sliceIndex) {
    private static final String[] KEYS = {"child_order_id", "parent_order_id",
        "instrument_id", "venue_id", "side", "qty", "price_ticks", "order_type",
        "submit_ts", "expire_ts", "slice_index"};

    /** Wire code of a MARKET order. */
    public static final int MARKET = 1;

    public ChildOrderRec {
        if (venueId < 0 || venueId > 0xFFFF) {
            throw new IllegalArgumentException("ChildOrder.venue_id outside u16");
        }
        if (side != 0 && side != 1) {
            throw new IllegalArgumentException("ChildOrder.side must be 0 or 1");
        }
        if (qty < 1) {
            throw new IllegalArgumentException("ChildOrder.qty must be >= 1");
        }
        if (priceTicks < 0) {
            throw new IllegalArgumentException("ChildOrder.price_ticks < 0");
        }
        if (orderType < 1 || orderType > 6) {
            throw new IllegalArgumentException("ChildOrder.order_type must be 1..6");
        }
        if (sliceIndex < 0 || sliceIndex > 0xFFFFFFFFL) {
            throw new IllegalArgumentException("ChildOrder.slice_index outside u32");
        }
        if (expireTs != 0 && expireTs < submitTs) {
            throw new IllegalArgumentException("ChildOrder: expire_ts < submit_ts");
        }
        if (orderType == MARKET && priceTicks != 0) {
            throw new IllegalArgumentException("ChildOrder: MARKET orders are unpriced");
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("child_order_id", Trees.u64Tree(childOrderId));
        t.put("parent_order_id", Trees.u64Tree(parentOrderId));
        t.put("instrument_id", instrumentId);
        t.put("venue_id", (long) venueId);
        t.put("side", (long) side);
        t.put("qty", qty);
        t.put("price_ticks", priceTicks);
        t.put("order_type", (long) orderType);
        t.put("submit_ts", submitTs);
        t.put("expire_ts", expireTs);
        t.put("slice_index", sliceIndex);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static ChildOrderRec fromTree(Map<String, Object> t) {
        String p = "ChildOrder";
        Trees.checkKeys(t, KEYS, p);
        return new ChildOrderRec(Trees.u64(t, "child_order_id", p),
                Trees.u64(t, "parent_order_id", p),
                Trees.u32(t, "instrument_id", p), Trees.u16(t, "venue_id", p),
                (int) Trees.ranged(t, "side", p, 0, 1),
                Trees.ranged(t, "qty", p, 1, Long.MAX_VALUE),
                Trees.ranged(t, "price_ticks", p, 0, Long.MAX_VALUE),
                (int) Trees.ranged(t, "order_type", p, 1, 6),
                Trees.i64(t, "submit_ts", p), Trees.i64(t, "expire_ts", p),
                Trees.u32(t, "slice_index", p));
    }
}
