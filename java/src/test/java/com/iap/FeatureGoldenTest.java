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
}
