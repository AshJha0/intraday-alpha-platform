package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.core.MarketEvent;
import com.iap.features.FeatureEngine;
import com.iap.features.FeatureVector;
import com.iap.features.Features;

/**
 * Feature-engine golden parity (API_FEATURES.md section 5) + engine
 * behavior: cadence, warmup, validity rules. Golden comparisons are BY
 * FEATURE NAME: every checkpoint feature this port implements natively
 * (the pinned native 40 plus the alpha-input auxiliaries) must match
 * tests/golden/expected_features.json at abs 1e-9 / rel 1e-9, with exact
 * validity agreement.
 */
public class FeatureGoldenTest {
    private static final Map<Long, Double> TICKS = new TreeMap<>();

    static {
        TICKS.put(1L, 0.01);
        TICKS.put(101L, 1e-05);
    }

    private static final Map<String, List<FeatureVector>> CACHE = new HashMap<>();

    private static synchronized List<FeatureVector> rows(String vectorFile) {
        return CACHE.computeIfAbsent(vectorFile, f -> {
            FeatureEngine engine = new FeatureEngine(TICKS, 0);
            List<MarketEvent> events =
                    f.startsWith("events_eq") ? Golden.eq() : Golden.fx();
            return engine.run(events);
        });
    }

    private void checkCheckpoint(String market, String cpKey, String vectorFile) {
        Map<String, Object> golden = Golden.json("expected_features.json");
        Map<String, Object> cp = Json.object(
                Json.object(Json.object(golden.get(market)).get("checkpoints"))
                        .get(cpKey));
        List<FeatureVector> all = rows(vectorFile);
        int idx = Integer.parseInt(cpKey) - 1;
        assertTrue("checkpoint index in range", idx < all.size());
        FeatureVector vec = all.get(idx);
        assertEquals(Json.asLong(cp.get("timestamp")), vec.timestamp);
        int compared = 0;
        for (Map.Entry<String, Object> e : Json.object(cp.get("features")).entrySet()) {
            String name = e.getKey();
            int slot = Features.index(name);
            if (slot < 0) {
                continue; // not in the native sub-vector
            }
            compared++;
            Map<String, Object> entry = Json.object(e.getValue());
            boolean wantValid = (Boolean) entry.get("valid");
            assertEquals(market + " cp " + cpKey + " " + name + " validity",
                    wantValid, vec.valid[slot]);
            if (!wantValid) {
                assertTrue(name + " invalid slot must carry NaN",
                        Double.isNaN(vec.values[slot]));
                continue;
            }
            double want = Json.asDouble(entry.get("value"));
            double got = vec.values[slot];
            assertTrue(market + " cp " + cpKey + " " + name + ": got " + got
                            + " want " + want,
                    Math.abs(got - want) <= 1e-9 + 1e-9 * Math.abs(want));
        }
        // 16 native names + vol_regime_ratio_v1 (aux) appear per checkpoint;
        // never silently compare fewer.
        assertTrue(market + " cp " + cpKey + " compared " + compared,
                compared >= 17);
    }

    @Test
    public void nativeNamesResolveAndCount() {
        // All 40 pinned native names (API_FEATURES.md section 3) must resolve.
        java.util.List<String> native40 = new java.util.ArrayList<>();
        for (int k : new int[] {1, 3, 5, 10}) {
            for (String w : new String[] {"1s", "5s", "30s"}) {
                native40.add("ofi_l" + k + "_w" + w + "_v1");
            }
        }
        for (int k : new int[] {1, 3, 5, 10}) {
            native40.add("imbalance_l" + k + "_v1");
        }
        native40.add("mid_price_v1");
        native40.add("microprice_v1");
        native40.add("micro_mid_dev_bps_v1");
        native40.add("spread_ticks_v1");
        native40.add("spread_bps_v1");
        for (int k : new int[] {1, 5, 10}) {
            native40.add("depth_bid_l" + k + "_v1");
            native40.add("depth_ask_l" + k + "_v1");
        }
        for (String w : new String[] {"1s", "10s", "1m"}) {
            native40.add("signed_volume_w" + w + "_v1");
            native40.add("trade_imbalance_w" + w + "_v1");
        }
        for (String w : new String[] {"10s", "1m", "5m"}) {
            native40.add("rvol_w" + w + "_v1");
        }
        native40.add("ret_simple_1s_v1");
        native40.add("ret_log_1s_v1");
        native40.add("ret_log_10s_v1");
        native40.add("ret_log_1m_v1");
        assertEquals(40, native40.size());
        Set<Integer> slots = new HashSet<>();
        for (String n : native40) {
            int s = Features.index(n);
            assertTrue(n + " must be implemented natively", s >= 0);
            slots.add(s);
        }
        assertEquals(40, slots.size());
        assertEquals(45, Features.COUNT);
        assertEquals(40, Features.NATIVE_COUNT);
        assertEquals(-1, Features.index("no_such_feature_v1"));
        // Round-trip: index(name(slot)) == slot.
        for (int i = 0; i < Features.COUNT; i++) {
            assertEquals(i, Features.index(Features.name(i)));
        }
    }

    @Test
    public void registryPinsAgree() {
        Map<String, Object> golden = Golden.json("expected_features.json");
        assertEquals(205, Json.asLong(golden.get("registered_count")));
        assertEquals(64, ((String) golden.get("registry_hash")).length());
        Map<String, Object> tol = Json.object(golden.get("tolerance"));
        assertEquals(1e-9, (Double) tol.get("abs"), 0.0);
        assertEquals(1e-9, (Double) tol.get("rel"), 0.0);
    }

    @Test
    public void eqCheckpoint500() {
        checkCheckpoint("eq", "500", "events_eq_mbo.jsonl");
    }

    @Test
    public void eqCheckpoint1000() {
        checkCheckpoint("eq", "1000", "events_eq_mbo.jsonl");
    }

    @Test
    public void eqCheckpoint1500() {
        checkCheckpoint("eq", "1500", "events_eq_mbo.jsonl");
    }

    @Test
    public void eqCheckpoint2000() {
        checkCheckpoint("eq", "2000", "events_eq_mbo.jsonl");
    }

    @Test
    public void fxCheckpoint400() {
        checkCheckpoint("fx", "400", "events_fx_quote.jsonl");
    }

    @Test
    public void fxCheckpoint800() {
        checkCheckpoint("fx", "800", "events_fx_quote.jsonl");
    }

    @Test
    public void nanNeverLeaksIntoValidSlot() {
        for (String file : new String[] {"events_eq_mbo.jsonl", "events_fx_quote.jsonl"}) {
            for (FeatureVector vec : rows(file)) {
                for (int i = 0; i < Features.COUNT; i++) {
                    if (vec.valid[i]) {
                        assertTrue(Features.name(i) + " at ts " + vec.timestamp,
                                Double.isFinite(vec.values[i]));
                    } else {
                        assertTrue(Features.name(i) + " at ts " + vec.timestamp,
                                Double.isNaN(vec.values[i]));
                    }
                }
            }
        }
    }

    @Test
    public void cadenceZeroEmitsEveryEvent() {
        List<MarketEvent> events = Golden.eq();
        FeatureEngine engine = new FeatureEngine(TICKS, 0);
        FeatureVector vec = new FeatureVector();
        for (MarketEvent ev : events) {
            assertTrue(engine.apply(ev, vec));
        }
        assertEquals(events.size(), engine.vectorsEmitted());
        assertEquals(events.size(), engine.eventsProcessed());
    }

    @Test
    public void cadenceThrottlesEmissions() {
        List<MarketEvent> events = Golden.eq();
        FeatureEngine engine = new FeatureEngine(TICKS, Features.W_1S);
        FeatureVector vec = new FeatureVector();
        java.util.List<Long> emitTs = new java.util.ArrayList<>();
        for (MarketEvent ev : events) {
            if (engine.apply(ev, vec)) {
                emitTs.add(vec.timestamp);
            }
        }
        assertTrue(emitTs.size() > 1);
        assertTrue(emitTs.size() < events.size());
        for (int i = 1; i < emitTs.size(); i++) {
            assertTrue(emitTs.get(i) - emitTs.get(i - 1) >= Features.W_1S);
        }
        try {
            new FeatureEngine(TICKS, -1);
            throw new AssertionError("negative cadence must throw");
        } catch (IllegalArgumentException expected) {
            // pinned behavior
        }
    }

    @Test
    public void warmupGatesWindowedFeatures() {
        // Two-sided book from the first event, but windowed features stay
        // invalid until t - first_event_ts >= w.
        Map<Long, Double> ticks = new TreeMap<>();
        ticks.put(7L, 0.01);
        FeatureEngine engine = new FeatureEngine(ticks, 0);
        FeatureVector vec = new FeatureVector();
        long t0 = 1_000_000_000_000L;
        assertTrue(engine.apply(add(1, t0, 0, 100, 10, 1), vec));
        assertTrue(engine.apply(add(2, t0 + 1000, 1, 101, 10, 2), vec));
        // book_ok features valid immediately; windowed ones not yet warm.
        assertTrue(vec.valid[Features.MID_PRICE]);
        assertTrue(!vec.valid[Features.OFI_L1_W1S]);
        assertTrue(!vec.valid[Features.SIGNED_VOLUME_W1S]);
        assertTrue(!vec.valid[Features.RVOL_W10S]);
        // 1s later: 1s windows warm (sums valid with empty windows).
        assertTrue(engine.apply(add(3, t0 + Features.W_1S + 1000, 0, 99, 5, 3), vec));
        assertTrue(vec.valid[Features.OFI_L1_W1S]);
        assertTrue(vec.valid[Features.SIGNED_VOLUME_W1S]);
        assertEquals(0.0, vec.values[Features.SIGNED_VOLUME_W1S], 0.0);
        // trade imbalance needs traded volume even when warm.
        assertTrue(!vec.valid[Features.TRADE_IMBALANCE_W1S]);
        assertTrue(!vec.valid[Features.RVOL_W10S]); // 10s not yet warm
    }

    @Test
    public void oneSidedBookInvalidatesBookFeatures() {
        Map<Long, Double> ticks = new TreeMap<>();
        ticks.put(7L, 0.01);
        FeatureEngine engine = new FeatureEngine(ticks, 0);
        FeatureVector vec = new FeatureVector();
        long t0 = 1_000_000_000_000L;
        assertTrue(engine.apply(add(1, t0, 0, 100, 10, 1), vec)); // bid only
        assertTrue(!vec.valid[Features.MID_PRICE]);
        assertTrue(!vec.valid[Features.SPREAD_TICKS]);
        assertTrue(!vec.valid[Features.IMBALANCE_L1]);
        assertTrue(!vec.valid[Features.DEPTH_BID_L1]);
        assertTrue(!vec.valid[Features.RET_LOG_1S]);
        assertTrue(Double.isNaN(vec.values[Features.MID_PRICE]));
    }

    @Test
    public void unknownInstrumentThrows() {
        Map<Long, Double> ticks = new TreeMap<>();
        ticks.put(7L, 0.01);
        FeatureEngine engine = new FeatureEngine(ticks, 0);
        FeatureVector vec = new FeatureVector();
        try {
            engine.apply(add(1, 1000, 0, 100, 10, 1, 8), vec);
            throw new AssertionError("unknown instrument must throw");
        } catch (IllegalArgumentException expected) {
            // pinned behavior
        }
    }

    @Test
    public void snapshotContinuesIdentically() {
        // Deep-copy snapshot: feed half the golden vector, snapshot, feed
        // the rest to both engines — identical emissions.
        List<MarketEvent> events = Golden.eq();
        FeatureEngine a = new FeatureEngine(TICKS, 0);
        FeatureVector va = new FeatureVector();
        int half = events.size() / 2;
        for (int i = 0; i < half; i++) {
            a.apply(events.get(i), va);
        }
        FeatureEngine b = a.snapshot();
        FeatureVector vb = new FeatureVector();
        for (int i = half; i < events.size(); i++) {
            boolean ea = a.apply(events.get(i), va);
            boolean eb = b.apply(events.get(i), vb);
            assertEquals(ea, eb);
            if (ea) {
                assertEquals(va.timestamp, vb.timestamp);
                for (int s = 0; s < Features.COUNT; s++) {
                    assertEquals(va.valid[s], vb.valid[s]);
                    if (va.valid[s]) {
                        assertEquals(va.values[s], vb.values[s], 0.0);
                    }
                }
            }
        }
    }

    private static MarketEvent add(long seq, long ts, int side, long px,
            long qty, long oid) {
        return add(seq, ts, side, px, qty, oid, 7);
    }

    private static MarketEvent add(long seq, long ts, int side, long px,
            long qty, long oid, long iid) {
        return new MarketEvent(seq, iid, 1, ts, ts, seq, 1, side, px, qty, oid, 0);
    }

    // ---------------------------------------------------------------------
    // Anomaly-vector golden (API_FEATURES.md section 2 ingestion rules)
    // ---------------------------------------------------------------------

    private void checkAnomalySide(String side, double tick) {
        Map<String, Object> golden =
                Golden.json("expected_features_anomalies.json");
        Map<String, Object> doc = Json.object(golden.get(side));
        String vector = (String) doc.get("vector");
        long iid = Json.asLong(doc.get("instrument_id"));
        List<MarketEvent> events = Golden.events(vector);
        assertEquals("vector length", Json.asLong(doc.get("n_events")),
                events.size());
        Map<Long, Double> ticks = new TreeMap<>();
        ticks.put(iid, tick);
        FeatureEngine engine = new FeatureEngine(ticks, 0);
        Map<String, Object> cps = Json.object(doc.get("checkpoints"));
        FeatureVector vec = new FeatureVector();
        int compared = 0;
        for (int i = 0; i < events.size(); i++) {
            boolean emitted = engine.apply(events.get(i), vec);
            String key = Integer.toString(i + 1);
            Object raw = cps.get(key);
            if (raw == null) {
                continue;
            }
            Map<String, Object> cp = Json.object(raw);
            String at = side + "@" + key;
            assertTrue(at + ": cadence 0 must emit", emitted);
            assertEquals(at + ": timestamp", Json.asLong(cp.get("timestamp")),
                    vec.timestamp);
            assertEquals(at + ": events_processed",
                    Json.asLong(cp.get("events_processed")),
                    engine.eventsProcessed());
            assertEquals(at + ": events_dropped (only APPLIED events count)",
                    Json.asLong(cp.get("events_dropped")),
                    engine.eventsDropped());
            assertEquals(at + ": ts_regressions_dropped",
                    Json.asLong(cp.get("ts_regressions_dropped")),
                    engine.tsRegressionsDropped());
            assertEquals(at + ": oversized_qty_dropped",
                    Json.asLong(cp.get("oversized_qty_dropped")),
                    engine.oversizedQtyDropped());
            assertEquals(at + ": oversized_depth_skipped",
                    Json.asLong(cp.get("oversized_depth_skipped")),
                    engine.oversizedDepthSkipped());
            assertEquals(at + ": stale recoveries",
                    Json.asLong(cp.get("recoveries")), engine.recoveries(iid));
            assertEquals(at + ": warmup anchor", Json.asLong(cp.get("warm_ts")),
                    engine.warmTs(iid));
            assertEquals(at + ": book_ok", cp.get("book_ok"),
                    Boolean.valueOf(engine.bookOk(iid)));
            for (Map.Entry<String, Object> e
                    : Json.object(cp.get("features")).entrySet()) {
                int slot = Features.index(e.getKey());
                if (slot < 0) {
                    continue;
                }
                Map<String, Object> entry = Json.object(e.getValue());
                boolean wantValid = (Boolean) entry.get("valid");
                assertEquals(at + " " + e.getKey() + " validity", wantValid,
                        vec.valid[slot]);
                if (!wantValid) {
                    continue;
                }
                compared++;
                double want = Json.asDouble(entry.get("value"));
                double got = vec.values[slot];
                assertTrue(at + " " + e.getKey() + ": got " + got + " want "
                        + want,
                        Math.abs(got - want) <= 1e-9 + 1e-9 * Math.abs(want));
            }
        }
        assertTrue(side + ": too few valid features compared", compared > 50);
    }

    @Test
    public void anomalyGoldenEqCheckpoints() {
        checkAnomalySide("eq", 0.01);
    }

    @Test
    public void anomalyGoldenFxCheckpoints() {
        checkAnomalySide("fx", 1e-05);
    }

    @Test
    public void anomalyGoldenExercisesTheDropPaths() {
        Map<String, Object> golden =
                Golden.json("expected_features_anomalies.json");
        for (String side : new String[] {"eq", "fx"}) {
            Map<String, Object> cps = Json.object(
                    Json.object(golden.get(side)).get("checkpoints"));
            long best = -1;
            Map<String, Object> last = null;
            for (Map.Entry<String, Object> e : cps.entrySet()) {
                long k = Long.parseLong(e.getKey());
                if (k > best) {
                    best = k;
                    last = Json.object(e.getValue());
                }
            }
            assertTrue(side, last != null);
            assertTrue(side, Json.asLong(last.get("events_dropped")) > 0);
            assertTrue(side,
                    Json.asLong(last.get("ts_regressions_dropped")) > 0);
            assertTrue(side, Json.asLong(last.get("recoveries")) > 0);
        }
    }

    // ---------------------------------------------------------------------
    // Ingestion scenarios (mirror of python/tests/test_feature_ingestion.py)
    // ---------------------------------------------------------------------

    private static final long NS = 1_000_000_000L;
    private static final long T0 = 1_787_578_200L * NS;

    /** Single-instrument feed with explicit per-venue sequence numbers. */
    private static final class Feed {
        final FeatureEngine engine;
        final long iid;
        final Map<Integer, Long> seq = new TreeMap<>();
        long id;
        FeatureVector vec = new FeatureVector();

        Feed(long iid, double tick) {
            Map<Long, Double> ticks = new TreeMap<>();
            ticks.put(iid, tick);
            this.engine = new FeatureEngine(ticks, 0);
            this.iid = iid;
        }

        boolean send(int et, long ts, int venue, long sequence, int side,
                long price, long qty, long orderId, long tradeId) {
            long sq = sequence == 0
                    ? seq.getOrDefault(venue, 0L) + 1 : sequence;
            seq.put(venue, Math.max(seq.getOrDefault(venue, 0L), sq));
            MarketEvent ev = new MarketEvent(++id, iid, venue, ts, ts + 150_000,
                    sq, et, side, price, qty, orderId, tradeId);
            return engine.apply(ev, vec);
        }

        void add(long ts, int side, long price, long qty, long oid) {
            send(com.iap.core.EventType.ADD, ts, 1, 0, side, price, qty, oid, 0);
        }

        long seqOf(int venue) {
            return seq.getOrDefault(venue, 0L);
        }

        boolean valid(String name) {
            return vec.valid[Features.index(name)];
        }

        double value(String name) {
            return vec.values[Features.index(name)];
        }

        void assertNoValidNan() {
            for (int i = 0; i < vec.values.length; i++) {
                if (vec.valid[i]) {
                    assertTrue("slot " + i, !Double.isNaN(vec.values[i])
                            && !Double.isInfinite(vec.values[i]));
                }
            }
        }
    }

    @Test
    public void scenarioGatewayReplayAfterReconnect() {
        Feed f = new Feed(1L, 0.01);
        long t = T0;
        f.add(t, com.iap.core.Side.BID, 1000, 100, 1);
        f.add(t, com.iap.core.Side.ASK, 1002, 100, 2);
        t += 2 * NS;
        f.send(com.iap.core.EventType.TRADE, t, 1, 0, com.iap.core.Side.BID,
                1001, 50, 0, 1);
        assertEquals(50.0, f.value("signed_volume_w1s_v1"), 0.0);

        // the gateway replays the same message (duplicate sequence)
        f.send(com.iap.core.EventType.TRADE, t, 1, f.seqOf(1),
                com.iap.core.Side.BID, 1001, 50, 0, 1);
        assertEquals("a duplicate trade must not double-count signed volume",
                50.0, f.value("signed_volume_w1s_v1"), 0.0);
        assertEquals(1L, f.engine.eventsDropped());

        // side > 1 on an ADD: dropped by the book, no depth change
        double depthBefore = f.value("depth_bid_l1_v1");
        f.send(com.iap.core.EventType.ADD, t, 1, 0, 2, 1000, 70, 900, 0);
        assertEquals(2L, f.engine.eventsDropped());
        assertEquals(depthBefore, f.value("depth_bid_l1_v1"), 0.0);

        // gap -> stale venue; a CANCEL arriving while stale is dropped
        f.send(com.iap.core.EventType.ADD, t, 1, f.seqOf(1) + 10,
                com.iap.core.Side.BID, 999, 10, 901, 0);
        f.send(com.iap.core.EventType.CANCEL, t, 1, 0, com.iap.core.Side.BID,
                1000, 0, 1, 0);
        assertTrue(f.engine.eventsDropped() >= 3);
        assertTrue("stale venue leaves the merged view",
                !f.valid("mid_price_v1"));
    }

    @Test
    public void scenarioVenueDisconnectThenSnapshotRecovery() {
        Feed f = new Feed(1L, 0.01);
        long t = T0;
        f.add(t, com.iap.core.Side.BID, 1000, 100, 1);
        f.add(t, com.iap.core.Side.ASK, 1002, 100, 2);
        long bid = 1000;
        long oid = 10;
        for (int k = 0; k < 200; k++) {
            t += 2 * NS;
            long step = (k % 2 == 0) ? 1 : -1;
            f.add(t, com.iap.core.Side.BID, bid + step, 100, oid);
            f.send(com.iap.core.EventType.CANCEL, t, 1, 0,
                    com.iap.core.Side.BID, bid, 0, k == 0 ? 1 : oid - 1, 0);
            bid += step;
            oid++;
        }
        assertTrue(f.valid("rvol_w1m_v1"));
        assertTrue(f.value("rvol_w1m_v1") > 0.0);
        assertTrue(f.valid("ret_vol_adj_10s_v1"));

        // gap then 120 s of silence
        t += NS;
        f.send(com.iap.core.EventType.ADD, t, 1, f.seqOf(1) + 50,
                com.iap.core.Side.BID, 1000, 10, oid + 50, 0);
        assertTrue(!f.valid("mid_price_v1"));

        // recovery: a complete SNAPSHOT burst 1 % higher
        t += 120 * NS;
        long sq = f.seqOf(1) + 1;
        f.send(com.iap.core.EventType.SNAPSHOT, t, 1, sq,
                com.iap.core.Side.BID, 1010, 100, 0, 3);
        f.send(com.iap.core.EventType.SNAPSHOT, t, 1, sq + 1,
                com.iap.core.Side.ASK, 1012, 100, 0, 2);
        f.send(com.iap.core.EventType.SNAPSHOT, t, 1, sq + 2,
                com.iap.core.Side.BID, 1009, 90, 0, 1);
        f.send(com.iap.core.EventType.SNAPSHOT, t, 1, sq + 3,
                com.iap.core.Side.ASK, 1013, 90, 0, 0);

        assertEquals(1L, f.engine.recoveries(1L));
        assertEquals(t, f.engine.warmTs(1L));
        assertTrue(f.engine.bookOk(1L));
        for (String name : new String[] {"rvol_w10s_v1", "rvol_w1m_v1",
            "rvol_w5m_v1", "ret_log_10s_v1", "ret_log_1m_v1",
            "ret_vol_adj_10s_v1", "vol_regime_ratio_v1", "ofi_l1_w1s_v1",
            "signed_volume_w1m_v1"}) {
            assertTrue(name + " valid right after a stale recovery",
                    !f.valid(name));
        }
        assertTrue(f.valid("mid_price_v1"));
        f.assertNoValidNan();
    }

    @Test
    public void scenarioZeroRvolMakesRatiosInvalidNotHuge() {
        Feed f = new Feed(1L, 0.01);
        long t = T0;
        f.add(t, com.iap.core.Side.BID, 1000, 100, 1);
        f.add(t, com.iap.core.Side.ASK, 1002, 100, 2);
        for (long k = 0; k < 70; k++) {
            t += NS;
            f.add(t, com.iap.core.Side.BID, 990, 5, 500 + k);
        }
        assertTrue(f.valid("rvol_w1m_v1"));
        assertEquals(0.0, f.value("rvol_w1m_v1"), 0.0);
        assertTrue(!f.valid("ret_vol_adj_10s_v1"));
        assertTrue(!f.valid("vol_regime_ratio_v1"));
    }

    @Test
    public void scenarioOneSidedFlickerKeepsRvolSamples() {
        Feed f = new Feed(1L, 0.01);
        long t = T0;
        f.add(t, com.iap.core.Side.BID, 1000, 100, 1);
        f.add(t, com.iap.core.Side.ASK, 1002, 100, 2);
        t += 11 * NS;
        f.send(com.iap.core.EventType.CANCEL, t, 1, 0, com.iap.core.Side.BID,
                1000, 0, 1, 0);
        assertTrue(!f.valid("mid_price_v1"));
        t += 1000;
        f.add(t, com.iap.core.Side.BID, 999, 100, 3);
        assertTrue(f.valid("rvol_w10s_v1"));
        assertTrue("the flicker mid change must enter the vol window",
                f.value("rvol_w10s_v1") > 0.0);
    }

    @Test
    public void scenarioZeroPriceQuoteIsDroppedNotApplied() {
        for (long price : new long[] {0L, -5L}) {
            Feed f = new Feed(101L, 1e-05);
            long t = T0;
            f.send(com.iap.core.EventType.QUOTE, t, 10, 0,
                    com.iap.core.Side.BID, 110000, 1000, 0, 0);
            f.send(com.iap.core.EventType.QUOTE, t, 10, 0,
                    com.iap.core.Side.ASK, 110002, 1000, 0, 0);
            assertTrue(f.valid("mid_price_v1"));
            f.send(com.iap.core.EventType.QUOTE, t + NS, 10, 0,
                    com.iap.core.Side.BID, price, 1000, 0, 0);
            assertEquals("price " + price, 1L, f.engine.eventsDropped());
            assertTrue("previous quote prevails", f.valid("mid_price_v1"));
            f.assertNoValidNan();
        }
    }

    @Test
    public void scenarioCrossVenueTimestampRegression() {
        Feed f = new Feed(1L, 0.01);
        long t = T0;
        f.send(com.iap.core.EventType.ADD, t, 1, 0, com.iap.core.Side.BID,
                1000, 100, 1, 0);
        f.send(com.iap.core.EventType.ADD, t, 1, 0, com.iap.core.Side.ASK,
                1002, 100, 2, 0);
        double midBefore = f.value("mid_price_v1");

        // venue 2's gateway clock runs 5 ms behind venue 1's
        boolean emitted = f.send(com.iap.core.EventType.ADD, t - 5_000_000, 2,
                0, com.iap.core.Side.BID, 1001, 100, 3, 0);
        assertTrue("a ts regression emits no vector", !emitted);
        assertEquals(1L, f.engine.tsRegressionsDropped());
        assertEquals(1L, f.engine.eventsDropped());

        f.send(com.iap.core.EventType.ADD, t + NS, 2, 0,
                com.iap.core.Side.BID, 1001, 100, 4, 0);
        assertTrue(f.value("mid_price_v1") > midBefore);
    }

    @Test
    public void scenarioOversizedQuantitiesNeverOverflowWindowSums() {
        Feed f = new Feed(1L, 0.01);
        long t = T0;
        f.add(t, com.iap.core.Side.BID, 1000, 100, 1);
        f.add(t, com.iap.core.Side.ASK, 1002, 100, 2);
        f.send(com.iap.core.EventType.TRADE, t + NS, 1, 0,
                com.iap.core.Side.BID, 1001, Long.MAX_VALUE, 0, 1);
        assertEquals(1L, f.engine.oversizedQtyDropped());
        f.send(com.iap.core.EventType.ADD, t + 2 * NS, 1, 0,
                com.iap.core.Side.BID, 998, Long.MAX_VALUE, 7, 0);
        assertEquals(2L, f.engine.oversizedQtyDropped());
        assertTrue(f.engine.oversizedDepthSkipped() >= 1);
        assertTrue("an oversized merged depth is unusable",
                !f.engine.bookOk(1L));
        f.assertNoValidNan();
    }
}
