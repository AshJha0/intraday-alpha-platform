package com.iap.tca;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.function.Function;

/**
 * Markout analysis — what the mid did after each fill (API_PORTFOLIO_TCA.md
 * §2.9). Port of the Python reference {@code iap.tca.markout};
 * {@code tests/golden/expected_markout.json} pins both (1e-9, nulls exact).
 *
 * <p>With side sign {@code s} (+1 buy / -1 sell), fill price {@code p},
 * fill time {@code t_f}, horizon {@code h}, {@code m_f} the fill's reference
 * mid (§2.4: the state prevailing at {@code t_f} for a TAKER fill, strictly
 * before it for a MAKER fill) and {@code m_h} the mid at or before
 * {@code t_f + h} in event time:
 * <pre>
 *   markout(h)               = s * (m_h - p)      positive = the fill looks good
 *   effective_half_spread    = s * (p - m_f)      negative = half-spread earned
 *   realised_half_spread(h)  = s * (p - m_h)      = -markout(h)
 *   price_impact(h)          = s * (m_h - m_f)
 *   effective_half_spread    = realised_half_spread(h) + price_impact(h)   (exact)
 *   adverse_selection(h)     = -price_impact(h) over MAKER fills
 * </pre>
 * Bps are per fill {@code 1e4 * x / p}; currency is {@code x * qty *
 * qty_unit}.
 *
 * <p>A markout is undefined — {@code null}, never zero — when no state
 * exists at or before {@code t_f + h}, when the timeline ends before
 * {@code t_f + h}, or when a gate ({@link MarketTimeline#addHalt}: a halt,
 * an auction, a no-quote gap) started inside
 * {@code (min(t_f, ts(m_h)), t_f + h]}. The reference mid is undefined when
 * no state prevails or the prevailing state predates a gate.
 *
 * <p>Cells report, per horizon over the fills defined at it, {@code n}, the
 * equal-weight mean of each measure in bps, its standard error (sample
 * standard deviation, ddof 1, over sqrt(n)) and the currency sum; with
 * fewer than {@code minFills} defined fills every statistic is null.
 */
public final class Markout {
    /** Default horizons (event-time ns), in report order. */
    public static final Map<String, Long> DEFAULT_HORIZONS_NS;

    static {
        Map<String, Long> m = new LinkedHashMap<>();
        m.put("100ms", 100_000_000L);
        m.put("1s", 1_000_000_000L);
        m.put("5s", 5_000_000_000L);
        m.put("30s", 30_000_000_000L);
        m.put("60s", 60_000_000_000L);
        m.put("5min", 300_000_000_000L);
        DEFAULT_HORIZONS_NS = java.util.Collections.unmodifiableMap(m);
    }

    private static final String[] MEASURES = {
        "markout", "effective_half_spread", "realised_half_spread", "price_impact"};

    private Markout() {
    }

    /** One fill as the markout analysis sees it. */
    public record Fill(long ts, double price, long qty, int side, String liquidity,
            int venueId, String algo, double qtyUnit) {
        public Fill {
            if (!"MAKER".equals(liquidity) && !"TAKER".equals(liquidity)) {
                throw new IllegalArgumentException(
                        "liquidity must be TAKER or MAKER, got " + liquidity);
            }
            if (side != 0 && side != 1) {
                throw new IllegalArgumentException("side must be 0 (buy) or 1 (sell)");
            }
            if (qty <= 0 || !(price > 0.0)) {
                throw new IllegalArgumentException("fill qty and price must be > 0");
            }
        }
    }

    /**
     * One LIMIT child that came to rest. {@code firstFillTs} /
     * {@code lastFillTs} are null for an order that never filled.
     */
    public record PassiveOrder(long qty, long filledQty, long restTs,
            Long firstFillTs, Long lastFillTs, long entryAheadQty) {
        public PassiveOrder {
            if (qty <= 0 || filledQty < 0 || filledQty > qty) {
                throw new IllegalArgumentException(
                        "passive order needs qty > 0 and 0 <= filled_qty <= qty");
            }
            if ((filledQty > 0) != (firstFillTs != null)) {
                throw new IllegalArgumentException(
                        "first_fill_ts must be set exactly when the order filled");
            }
        }
    }

    /** {@code m_f}, or null when undefined. */
    public static Double referenceMid(MarketTimeline tl, long ts, String liquidity) {
        long refTs = "TAKER".equals(liquidity) ? ts : ts - 1;
        int i = tl.prevailing(refTs);
        if (i < 0 || tl.haltIn(tl.ts(i), refTs)) {
            return null;
        }
        return tl.mid(i);
    }

    /** {@code m_h}: the mid at or before {@code fillTs + horizonNs}, or null. */
    public static Double markoutMid(MarketTimeline tl, long fillTs, long horizonNs) {
        if (horizonNs < 0) {
            throw new IllegalArgumentException("horizon_ns must be >= 0");
        }
        long t = fillTs + horizonNs;
        int i = tl.prevailing(t);
        if (i < 0 || tl.lastTs() < t) {
            return null;
        }
        if (tl.haltIn(Math.min(fillTs, tl.ts(i)), t)) {
            return null;
        }
        return tl.mid(i);
    }

    /**
     * The four measures of one fill at one horizon per unit of price, in
     * the order markout, effective_half_spread, realised_half_spread,
     * price_impact; null when undefined.
     */
    public static double[] fillMeasures(Fill fill, MarketTimeline tl, long horizonNs) {
        Double mh = markoutMid(tl, fill.ts(), horizonNs);
        Double mf = referenceMid(tl, fill.ts(), fill.liquidity());
        if (mh == null || mf == null) {
            return null;
        }
        double s = fill.side() == 0 ? 1.0 : -1.0;
        return new double[] {
            s * (mh - fill.price()),
            s * (fill.price() - mf),
            s * (fill.price() - mh),
            s * (mh - mf)};
    }

    /** {mean, standard error} of at least two values. */
    private static double[] meanSe(List<Double> values) {
        int n = values.size();
        double total = 0.0;
        for (double v : values) {
            total += v;
        }
        double mean = total / n;
        double ss = 0.0;
        for (double v : values) {
            ss += (v - mean) * (v - mean);
        }
        return new double[] {mean, Math.sqrt(ss / (n - 1) / n)};
    }

    private static Map<String, Object> cell(List<Fill> fills, MarketTimeline tl,
            Map<String, Long> horizons, int minFills) {
        long qty = 0;
        for (Fill f : fills) {
            qty += f.qty();
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("n_fills", (long) fills.size());
        out.put("qty", qty);
        Map<String, Object> hz = new LinkedHashMap<>();
        for (Map.Entry<String, Long> e : horizons.entrySet()) {
            List<List<Double>> bps = new ArrayList<>();
            double[] ccy = new double[MEASURES.length];
            for (int m = 0; m < MEASURES.length; m++) {
                bps.add(new ArrayList<>());
            }
            for (Fill f : fills) {
                double[] x = fillMeasures(f, tl, e.getValue());
                if (x == null) {
                    continue;
                }
                for (int m = 0; m < MEASURES.length; m++) {
                    bps.get(m).add(1e4 * x[m] / f.price());
                    ccy[m] += x[m] * (double) f.qty() * f.qtyUnit();
                }
            }
            int n = bps.get(0).size();
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("n", (long) n);
            for (int m = 0; m < MEASURES.length; m++) {
                if (n < minFills) {
                    row.put(MEASURES[m] + "_bps", null);
                    row.put(MEASURES[m] + "_se_bps", null);
                    row.put(MEASURES[m] + "_ccy", null);
                } else {
                    double[] ms = meanSe(bps.get(m));
                    row.put(MEASURES[m] + "_bps", ms[0]);
                    row.put(MEASURES[m] + "_se_bps", ms[1]);
                    row.put(MEASURES[m] + "_ccy", ccy[m]);
                }
            }
            hz.put(e.getKey(), row);
        }
        out.put("horizons", hz);
        return out;
    }

    private static <K extends Comparable<K>> Map<String, Object> split(
            List<Fill> fills, Function<Fill, K> key, MarketTimeline tl,
            Map<String, Long> horizons, int minFills) {
        TreeMap<K, List<Fill>> groups = new TreeMap<>();
        for (Fill f : fills) {
            groups.computeIfAbsent(key.apply(f), k -> new ArrayList<>()).add(f);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        for (Map.Entry<K, List<Fill>> e : groups.entrySet()) {
            out.put(String.valueOf(e.getKey()), cell(e.getValue(), tl, horizons, minFills));
        }
        return out;
    }

    /**
     * Markout table of one instrument's fills: cells {@code all},
     * {@code by_liquidity}, {@code by_venue}, {@code by_algo},
     * {@code by_side}, {@code by_time_bucket}
     * ({@code floorDiv(t_f - sessionStartTs, bucketNs)}) and
     * {@code adverse_selection} (the MAKER cell's {@code -price_impact}).
     * The returned tree has the keys of the Python reference; values are
     * {@code Long}, {@code Double}, {@code null} or nested maps.
     */
    public static Map<String, Object> report(List<Fill> fills, MarketTimeline tl,
            Map<String, Long> horizons, int minFills, long bucketNs,
            long sessionStartTs) {
        if (minFills < 2) {
            throw new IllegalArgumentException(
                    "min_fills must be >= 2 (a standard error needs two fills)");
        }
        if (bucketNs <= 0) {
            throw new IllegalArgumentException("bucket_ns must be > 0");
        }
        Map<String, Object> byLiquidity =
                split(fills, Fill::liquidity, tl, horizons, minFills);
        Map<String, Object> adverse = new LinkedHashMap<>();
        Object maker = byLiquidity.get("MAKER");
        for (String name : horizons.keySet()) {
            Map<String, Object> a = new LinkedHashMap<>();
            Map<?, ?> row = null;
            if (maker != null) {
                Map<?, ?> hz = (Map<?, ?>) ((Map<?, ?>) maker).get("horizons");
                row = (Map<?, ?>) hz.get(name);
            }
            if (row == null || row.get("price_impact_bps") == null) {
                a.put("n", row == null ? Long.valueOf(0) : row.get("n"));
                a.put("bps", null);
                a.put("se_bps", null);
                a.put("ccy", null);
            } else {
                a.put("n", row.get("n"));
                a.put("bps", -((Double) row.get("price_impact_bps")));
                a.put("se_bps", row.get("price_impact_se_bps"));
                a.put("ccy", -((Double) row.get("price_impact_ccy")));
            }
            adverse.put(name, a);
        }
        Map<String, Object> hzOut = new LinkedHashMap<>();
        for (Map.Entry<String, Long> e : horizons.entrySet()) {
            hzOut.put(e.getKey(), e.getValue());
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("horizons_ns", hzOut);
        out.put("min_fills", (long) minFills);
        out.put("bucket_ns", bucketNs);
        out.put("session_start_ts", sessionStartTs);
        out.put("all", cell(fills, tl, horizons, minFills));
        out.put("by_liquidity", byLiquidity);
        out.put("by_venue", split(fills, Fill::venueId, tl, horizons, minFills));
        out.put("by_algo", split(fills, Fill::algo, tl, horizons, minFills));
        out.put("by_side", split(fills, f -> f.side() == 0 ? "BUY" : "SELL",
                tl, horizons, minFills));
        out.put("by_time_bucket", split(fills,
                f -> Math.floorDiv(f.ts() - sessionStartTs, bucketNs),
                tl, horizons, minFills));
        out.put("adverse_selection", adverse);
        return out;
    }

    private static Map<String, Object> stat(List<Double> values, int minN) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("n", (long) values.size());
        if (values.size() < minN) {
            out.put("mean", null);
            out.put("se", null);
        } else {
            double[] ms = meanSe(values);
            out.put("mean", ms[0]);
            out.put("se", ms[1]);
        }
        return out;
    }

    private static Map<String, Object> passiveCell(List<PassiveOrder> orders,
            int minOrders) {
        int n = orders.size();
        long posted = 0;
        long filled = 0;
        long anyFill = 0;
        long full = 0;
        List<Double> first = new ArrayList<>();
        List<Double> complete = new ArrayList<>();
        for (PassiveOrder o : orders) {
            posted += o.qty();
            filled += o.filledQty();
            if (o.filledQty() > 0) {
                anyFill++;
                first.add((double) (o.firstFillTs() - o.restTs()));
            }
            if (o.filledQty() == o.qty()) {
                full++;
                complete.add((double) (o.lastFillTs() - o.restTs()));
            }
        }
        boolean enough = n >= minOrders;
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("n_orders", (long) n);
        out.put("posted_qty", posted);
        out.put("filled_qty", filled);
        out.put("fill_rate_qty", enough ? Double.valueOf((double) filled / (double) posted) : null);
        out.put("fill_rate_orders", enough ? Double.valueOf((double) anyFill / n) : null);
        out.put("full_fill_rate", enough ? Double.valueOf((double) full / n) : null);
        out.put("time_to_first_fill_ns", stat(first, minOrders));
        out.put("time_to_full_fill_ns", stat(complete, minOrders));
        return out;
    }

    /** Label of a queue-position bucket: "0", "1-500", ..., "&gt;2000". */
    public static String queueBucket(long entryAheadQty, long[] edges) {
        long lo = 0;
        for (long e : edges) {
            if (entryAheadQty <= e) {
                return e == lo ? Long.toString(e) : lo + "-" + e;
            }
            lo = e + 1;
        }
        return ">" + edges[edges.length - 1];
    }

    /**
     * Fill rate and time to fill of passive orders, overall and per
     * queue-position bucket at entry ({@code edges}: inclusive upper edges,
     * starting at 0, strictly ascending; empty buckets are omitted).
     */
    public static Map<String, Object> passiveOrderStats(List<PassiveOrder> orders,
            int minOrders, long[] edges) {
        if (minOrders < 2) {
            throw new IllegalArgumentException("min_orders must be >= 2");
        }
        if (edges.length == 0 || edges[0] != 0) {
            throw new IllegalArgumentException(
                    "queue_edges must start at 0 and be strictly ascending");
        }
        for (int i = 1; i < edges.length; i++) {
            if (edges[i] <= edges[i - 1]) {
                throw new IllegalArgumentException(
                        "queue_edges must start at 0 and be strictly ascending");
            }
        }
        List<String> labels = new ArrayList<>();
        for (long e : edges) {
            labels.add(queueBucket(e, edges));
        }
        labels.add(">" + edges[edges.length - 1]);
        Map<String, List<PassiveOrder>> groups = new LinkedHashMap<>();
        for (PassiveOrder o : orders) {
            groups.computeIfAbsent(queueBucket(o.entryAheadQty(), edges),
                    k -> new ArrayList<>()).add(o);
        }
        Map<String, Object> byQueue = new LinkedHashMap<>();
        for (String label : labels) {
            if (groups.containsKey(label)) {
                byQueue.put(label, passiveCell(groups.get(label), minOrders));
            }
        }
        List<Object> edgeList = new ArrayList<>();
        for (long e : edges) {
            edgeList.add(e);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("min_orders", (long) minOrders);
        out.put("queue_edges", edgeList);
        out.put("all", passiveCell(orders, minOrders));
        out.put("by_queue_ahead_at_entry", byQueue);
        return out;
    }
}
