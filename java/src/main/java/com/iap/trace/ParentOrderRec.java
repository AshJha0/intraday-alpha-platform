package com.iap.trace;

import java.util.Map;
import java.util.TreeMap;

import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * {@code schemas/order/parent_order.schema.json} (x-version 1): a
 * strategy-level order handed to an execution algorithm.
 * {@code decisionTs <= arrivalTs <= endTs}; {@code limitPriceTicks} 0 =
 * unpriced; {@code params} are the algorithm's numeric parameters keyed by
 * name ({@code participation} for POV), serialised in code-point key order.
 */
public record ParentOrderRec(long parentOrderId, String strategyId, String alphaId,
        long instrumentId, int side, long qty, String algo, long decisionTs,
        long arrivalTs, long endTs, double urgency, long limitPriceTicks,
        Map<String, Double> params) {
    private static final String[] KEYS = {"parent_order_id", "strategy_id",
        "alpha_id", "instrument_id", "side", "qty", "algo", "decision_ts",
        "arrival_ts", "end_ts", "urgency", "limit_price_ticks", "params"};

    public ParentOrderRec {
        Trees.ident(strategyId, "ParentOrder.strategy_id");
        Trees.ident(alphaId, "ParentOrder.alpha_id");
        if (side != 0 && side != 1) {
            throw new IllegalArgumentException("ParentOrder.side must be 0 or 1");
        }
        if (qty < 1) {
            throw new IllegalArgumentException("ParentOrder.qty must be >= 1");
        }
        checkAlgo(algo, "ParentOrder.algo");
        if (decisionTs > arrivalTs) {
            throw new IllegalArgumentException("ParentOrder: decision_ts ("
                    + decisionTs + ") > arrival_ts (" + arrivalTs + ")");
        }
        if (arrivalTs > endTs) {
            throw new IllegalArgumentException("ParentOrder: arrival_ts ("
                    + arrivalTs + ") > end_ts (" + endTs + ")");
        }
        Trees.finite(urgency, "ParentOrder.urgency");
        if (urgency < 0.0 || urgency > 1.0) {
            throw new IllegalArgumentException("ParentOrder.urgency outside [0, 1]");
        }
        if (limitPriceTicks < 0) {
            throw new IllegalArgumentException("ParentOrder.limit_price_ticks < 0");
        }
        TreeMap<String, Double> copy = new TreeMap<>(CanonicalJson.KEY_ORDER);
        for (Map.Entry<String, Double> e : params.entrySet()) {
            if (!e.getKey().matches("[A-Za-z_][A-Za-z0-9_]*")) {
                throw new IllegalArgumentException(
                        "ParentOrder.params: bad key '" + e.getKey() + "'");
            }
            copy.put(e.getKey(), Trees.finite(e.getValue(),
                    "ParentOrder.params." + e.getKey()));
        }
        params = java.util.Collections.unmodifiableMap(copy);
    }

    static void checkAlgo(String algo, String where) {
        if (!("TWAP".equals(algo) || "VWAP".equals(algo) || "POV".equals(algo)
                || "IS".equals(algo))) {
            throw new IllegalArgumentException(where + ": unknown algo " + algo);
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("parent_order_id", Trees.u64Tree(parentOrderId));
        t.put("strategy_id", strategyId);
        t.put("alpha_id", alphaId);
        t.put("instrument_id", instrumentId);
        t.put("side", (long) side);
        t.put("qty", qty);
        t.put("algo", algo);
        t.put("decision_ts", decisionTs);
        t.put("arrival_ts", arrivalTs);
        t.put("end_ts", endTs);
        t.put("urgency", urgency);
        t.put("limit_price_ticks", limitPriceTicks);
        t.put("params", new TreeMap<String, Object>(params));
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static ParentOrderRec fromTree(Map<String, Object> t) {
        String p = "ParentOrder";
        Trees.checkKeys(t, KEYS, p);
        Map<String, Object> raw = Trees.obj(t, "params", p);
        TreeMap<String, Double> params = new TreeMap<>();
        for (String k : raw.keySet()) {
            params.put(k, Trees.num(raw, k, p + ".params"));
        }
        return new ParentOrderRec(Trees.u64(t, "parent_order_id", p),
                Trees.str(t, "strategy_id", p), Trees.str(t, "alpha_id", p),
                Trees.u32(t, "instrument_id", p),
                (int) Trees.ranged(t, "side", p, 0, 1),
                Trees.ranged(t, "qty", p, 1, Long.MAX_VALUE),
                Trees.str(t, "algo", p), Trees.i64(t, "decision_ts", p),
                Trees.i64(t, "arrival_ts", p), Trees.i64(t, "end_ts", p),
                Trees.num(t, "urgency", p),
                Trees.ranged(t, "limit_price_ticks", p, 0, Long.MAX_VALUE),
                params);
    }
}
