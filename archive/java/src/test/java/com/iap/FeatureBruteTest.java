package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.features.FeatureEngine;
import com.iap.features.FeatureVector;
import com.iap.features.Features;
import com.iap.orderbook.ConsolidatedBook;

/**
 * Incremental-vs-brute-force recompute tests (8 representative features on
 * the golden EQ vector). The brute-force side deliberately shares NO
 * rolling machinery with the engine: it rebuilds the merged depth from
 * {@link ConsolidatedBook#depth} (a different merge path), computes OFI
 * deltas with map unions, keeps plain append-only logs, and evaluates
 * every window by scanning the log per emission ((t - w, t] half-open).
 * Any drift in the engine's ring buffers, eviction order or at-or-before
 * lookups shows up as a mismatch here.
 */
public class FeatureBruteTest {
    private static final class Row {
        long t;
        boolean ok;
        long db5;
        long da5;
        long db10;
        long mid2;
        int nOfi;
        int nTr;
        int nDlm;
        int nLogmid;
    }

    private static final class Logs {
        final List<long[]> ofiL1 = new ArrayList<>();     // {ts, val}
        final List<long[]> ofiL3 = new ArrayList<>();
        final List<long[]> tradeSigned = new ArrayList<>();
        final List<long[]> tradeBuy = new ArrayList<>();
        final List<long[]> tradeSell = new ArrayList<>();
        final List<Long> dlmSqTs = new ArrayList<>();
        final List<Double> dlmSq = new ArrayList<>();
        final List<Long> logmidTs = new ArrayList<>();    // at mid changes
        final List<Double> logmid = new ArrayList<>();
        final List<Row> rows = new ArrayList<>();
        long firstTs;
    }

    private static List<FeatureVector> engineRows;
    private static Logs logs;

    private static synchronized void build() {
        if (logs != null) {
            return;
        }
        List<MarketEvent> events = Golden.eq();
        Map<Long, Double> ticks = new TreeMap<>();
        ticks.put(1L, 0.01);
        engineRows = new FeatureEngine(ticks, 0).run(events);
        logs = new Logs();
        buildBrute(events, logs);
    }

    private static long mapDelta(Map<Long, Long> prev, Map<Long, Long> curr) {
        long d = 0;
        for (Map.Entry<Long, Long> e : curr.entrySet()) {
            Long p = prev.get(e.getKey());
            d += e.getValue() - (p == null ? 0 : p);
        }
        for (Map.Entry<Long, Long> e : prev.entrySet()) {
            if (!curr.containsKey(e.getKey())) {
                d -= e.getValue();
            }
        }
        return d;
    }

    private static Map<Long, Long> topk(long[][] depth, int k) {
        Map<Long, Long> m = new HashMap<>();
        for (int i = 0; i < depth.length && i < k; i++) {
            m.put(depth[i][0], depth[i][1]);
        }
        return m;
    }

    private static void buildBrute(List<MarketEvent> events, Logs lg) {
        ConsolidatedBook cons = new ConsolidatedBook(1);
        long[][] prevBid = new long[0][];
        long[][] prevAsk = new long[0][];
        boolean prevOk = false;
        long prevMid2 = 0;
        boolean haveHist = false;
        double lastLogmid = 0.0;
        lg.firstTs = events.get(0).exchangeTs;
        for (MarketEvent ev : events) {
            cons.apply(ev);
            long t = ev.exchangeTs;
            int et = ev.eventType;
            if (et == EventType.TRADE) {
                long buy = ev.side == 0 ? ev.qty : 0;
                lg.tradeSigned.add(new long[] {t, buy - (ev.qty - buy)});
                lg.tradeBuy.add(new long[] {t, buy});
                lg.tradeSell.add(new long[] {t, ev.qty - buy});
            }
            boolean touch = et == EventType.ADD || et == EventType.MODIFY
                    || et == EventType.CANCEL || et == EventType.EXECUTE
                    || et == EventType.QUOTE
                    || (et == EventType.SNAPSHOT && ev.tradeId == 0);
            Row row = new Row();
            row.t = t;
            if (touch) {
                long[][] bid = cons.depth(Side.BID, 10);
                long[][] ask = cons.depth(Side.ASK, 10);
                if (prevBid.length != 0 || prevAsk.length != 0
                        || bid.length != 0 || ask.length != 0) {
                    lg.ofiL1.add(new long[] {t,
                        mapDelta(topk(prevBid, 1), topk(bid, 1))
                                - mapDelta(topk(prevAsk, 1), topk(ask, 1))});
                    lg.ofiL3.add(new long[] {t,
                        mapDelta(topk(prevBid, 3), topk(bid, 3))
                                - mapDelta(topk(prevAsk, 3), topk(ask, 3))});
                }
                boolean ok = bid.length != 0 && ask.length != 0;
                if (ok) {
                    long mid2 = bid[0][0] + ask[0][0];
                    // Pinned: compare against the last RECORDED mid sample,
                    // so a one-sided flicker that moves the mid still
                    // contributes a vol sample (API_FEATURES.md section 2).
                    if (!haveHist || mid2 != prevMid2) {
                        double lm = Math.log((double) mid2);
                        if (haveHist) {
                            double dlm = lm - lastLogmid;
                            lg.dlmSqTs.add(t);
                            lg.dlmSq.add(dlm * dlm);
                        }
                        lg.logmidTs.add(t);
                        lg.logmid.add(lm);
                        lastLogmid = lm;
                        haveHist = true;
                        prevMid2 = mid2;
                    }
                }
                prevOk = ok;
                prevBid = bid;
                prevAsk = ask;
            }
            // Post-event instantaneous state (whether touched or not).
            row.nOfi = lg.ofiL1.size();
            row.nTr = lg.tradeSigned.size();
            row.nDlm = lg.dlmSq.size();
            row.nLogmid = lg.logmid.size();
            row.ok = prevOk;
            if (prevOk) {
                for (int i = 0; i < prevBid.length; i++) {
                    if (i < 5) {
                        row.db5 += prevBid[i][1];
                    }
                    row.db10 += prevBid[i][1];
                }
                for (int i = 0; i < prevAsk.length && i < 5; i++) {
                    row.da5 += prevAsk[i][1];
                }
                row.mid2 = prevMid2;
            }
            lg.rows.add(row);
        }
    }

    private static long windowSumL(List<long[]> log, int n, long t, long w) {
        long s = 0;
        for (int i = 0; i < n && i < log.size(); i++) {
            if (log.get(i)[0] > t - w && log.get(i)[0] <= t) {
                s += log.get(i)[1];
            }
        }
        return s;
    }

    private static double windowSumD(List<Long> ts, List<Double> log, int n,
            long t, long w) {
        double s = 0;
        for (int i = 0; i < n && i < log.size(); i++) {
            if (ts.get(i) > t - w && ts.get(i) <= t) {
                s += log.get(i);
            }
        }
        return s;
    }

    private static Double atOrBefore(List<Long> ts, List<Double> log, int n,
            long t) {
        Double out = null;
        for (int i = 0; i < n && i < log.size(); i++) {
            if (ts.get(i) <= t) {
                out = log.get(i);
            }
        }
        return out;
    }

    private static boolean warm(long t, long w) {
        return t - logs.firstTs >= w;
    }

    @Test
    public void ofiL1W30s() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            long t = vec.timestamp;
            boolean w = warm(t, Features.W_30S);
            assertEquals("row " + i, w, vec.valid[Features.OFI_L1_W30S]);
            if (!w) {
                continue;
            }
            long brute = windowSumL(logs.ofiL1, logs.rows.get(i).nOfi, t,
                    Features.W_30S);
            assertEquals("row " + i, (double) brute,
                    vec.values[Features.OFI_L1_W30S], 0.0); // integer: exact
        }
    }

    @Test
    public void ofiL3W5s() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            long t = vec.timestamp;
            boolean w = warm(t, Features.W_5S);
            assertEquals("row " + i, w, vec.valid[Features.OFI_L3_W5S]);
            if (!w) {
                continue;
            }
            long brute = windowSumL(logs.ofiL3, logs.rows.get(i).nOfi, t,
                    Features.W_5S);
            assertEquals("row " + i, (double) brute,
                    vec.values[Features.OFI_L3_W5S], 0.0);
        }
    }

    @Test
    public void signedVolumeW10s() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            long t = vec.timestamp;
            boolean w = warm(t, Features.W_10S);
            assertEquals("row " + i, w, vec.valid[Features.SIGNED_VOLUME_W10S]);
            if (!w) {
                continue;
            }
            long brute = windowSumL(logs.tradeSigned, logs.rows.get(i).nTr, t,
                    Features.W_10S);
            assertEquals("row " + i, (double) brute,
                    vec.values[Features.SIGNED_VOLUME_W10S], 0.0);
        }
    }

    @Test
    public void tradeImbalanceW1m() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            long t = vec.timestamp;
            long buys = windowSumL(logs.tradeBuy, logs.rows.get(i).nTr, t,
                    Features.W_1M);
            long sells = windowSumL(logs.tradeSell, logs.rows.get(i).nTr, t,
                    Features.W_1M);
            boolean valid = warm(t, Features.W_1M) && (buys + sells) > 0;
            assertEquals("row " + i, valid, vec.valid[Features.TRADE_IMBALANCE_W1M]);
            if (!valid) {
                continue;
            }
            double brute = (double) (buys - sells) / (double) (buys + sells);
            assertEquals("row " + i, brute,
                    vec.values[Features.TRADE_IMBALANCE_W1M], 1e-12);
        }
    }

    @Test
    public void rvolW1m() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            long t = vec.timestamp;
            boolean w = warm(t, Features.W_1M);
            assertEquals("row " + i, w, vec.valid[Features.RVOL_W1M]);
            if (!w) {
                continue;
            }
            double s = windowSumD(logs.dlmSqTs, logs.dlmSq, logs.rows.get(i).nDlm, t,
                    Features.W_1M);
            double brute = Math.sqrt(Math.max(s, 0.0) / 60.0);
            assertEquals("row " + i, brute, vec.values[Features.RVOL_W1M],
                    1e-9 + 1e-9 * Math.abs(brute));
        }
    }

    @Test
    public void retLog10s() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            long t = vec.timestamp;
            Row row = logs.rows.get(i);
            Double past = row.ok
                    ? atOrBefore(logs.logmidTs, logs.logmid, row.nLogmid, t - Features.W_10S)
                    : null;
            boolean has = past != null;
            assertEquals("row " + i, has, vec.valid[Features.RET_LOG_10S]);
            if (!has) {
                continue;
            }
            double brute = Math.log((double) row.mid2) - past;
            assertEquals("row " + i, brute, vec.values[Features.RET_LOG_10S],
                    1e-9 + 1e-9 * Math.abs(brute));
        }
    }

    @Test
    public void imbalanceL5() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            Row row = logs.rows.get(i);
            boolean valid = row.ok && (row.db5 + row.da5) > 0;
            assertEquals("row " + i, valid, vec.valid[Features.IMBALANCE_L5]);
            if (!valid) {
                continue;
            }
            double brute = (double) (row.db5 - row.da5)
                    / (double) (row.db5 + row.da5);
            assertEquals("row " + i, brute, vec.values[Features.IMBALANCE_L5],
                    1e-12);
        }
    }

    @Test
    public void depthBidL10() {
        build();
        for (int i = 0; i < engineRows.size(); i++) {
            FeatureVector vec = engineRows.get(i);
            Row row = logs.rows.get(i);
            assertEquals("row " + i, row.ok, vec.valid[Features.DEPTH_BID_L10]);
            if (!row.ok) {
                continue;
            }
            assertEquals("row " + i, (double) row.db10,
                    vec.values[Features.DEPTH_BID_L10], 0.0);
        }
    }

    @Test
    public void bruteCoversWarmRows() {
        build();
        assertEquals(engineRows.size(), logs.rows.size());
        assertTrue("golden vector must exercise the windows",
                logs.ofiL1.size() > 500 && logs.tradeSigned.size() > 50
                        && logs.dlmSq.size() > 100);
    }
}
