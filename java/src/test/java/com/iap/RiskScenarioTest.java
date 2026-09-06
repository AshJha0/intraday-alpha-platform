package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.risk.Decision;
import com.iap.risk.InstrumentRef;
import com.iap.risk.OrderRequest;
import com.iap.risk.RiskDecision;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskEvent;
import com.iap.risk.RiskFill;
import com.iap.risk.RiskLimits;
import com.iap.risk.Rules;
import com.iap.risk.Scope;
import com.iap.risk.Severity;

/**
 * Real-life risk scenarios (round 3), each pinned from the contract
 * (PLATFORM_CONVENTIONS.md §11) and mirrored by the Rust reference tests
 * in {@code rust/risk/tests/rules.rs}: mark-to-market latching, FX
 * notionals in the reporting currency, in-flight MARKET/PEG orders in the
 * projections, gross including resting orders, throttle timestamp
 * regression, kill re-arm precedence, session roll, snapshot/restore and
 * the bootstrap gate.
 */
public class RiskScenarioTest {
    private static final long T0 = 1_800_000_000_000_000_000L;
    private static final long NS = 1_000_000_000L;

    private static Map<String, Object> configDoc() {
        return Json.object(com.iap.config.Json.parseFile(
                java.nio.file.Paths.get("..", "configs", "risk.json")));
    }

    private static TreeMap<Long, InstrumentRef> refs() {
        TreeMap<Long, InstrumentRef> m = new TreeMap<>();
        m.put(1L, InstrumentRef.equity(0.01));
        m.put(2L, InstrumentRef.equity(0.01));
        m.put(102L, new InstrumentRef(1e-5, 1000.0, "USD"));
        m.put(103L, new InstrumentRef(0.001, 1000.0, "JPY"));
        m.put(108L, new InstrumentRef(1e-5, 1000.0, "GBP"));
        return m;
    }

    private static RiskEngine engine() {
        RiskEngine eng = RiskEngine.fromConfig(configDoc(), refs());
        eng.onMarket(1, 2450, 2452, T0); // mid 24.51
        eng.onMarket(2, 3119, 3121, T0); // mid 31.20
        eng.onMarket(103, 147_515, 147_525, T0); // mid 147.520 JPY
        return eng;
    }

    private static OrderRequest typed(long id, long iid, int side, long qty,
            long px, int type, String sid, long ts) {
        return new OrderRequest(id, iid, side, qty, px, type, 1, sid, 0.5, ts);
    }

    private static OrderRequest limit(long id, int side, long qty, long px, long ts) {
        return typed(id, 1, side, qty, px, OrderRequest.LIMIT, "S1", ts);
    }

    private static RiskFill fill(String sid, long iid, int side, long qty,
            long px, long ts) {
        return new RiskFill(ts, sid, iid, 0, side, qty, px);
    }

    private static List<String> kills(RiskEngine eng) {
        List<String> out = new ArrayList<>();
        for (RiskEvent e : eng.audit()) {
            if (e.decision() == Decision.KILL.code()) {
                out.add(e.ruleId());
            }
        }
        return out;
    }

    /** Scenario: a news gap marks a 95k long down 14% with no fill. */
    @Test
    public void riskMtmLossLatchesWithoutFill() {
        RiskEngine eng = engine();
        eng.onFill(fill("S1", 1, 0, 95_000, 2452, T0 + NS));
        assertTrue(kills(eng).isEmpty());
        eng.onMarket(1, 2099, 2101, T0 + 2 * NS); // mid 21.00 -> -334,400
        assertEquals(List.of(Rules.STRATEGY_LOSS, Rules.DAILY_LOSS), kills(eng));
        assertEquals(-334_400.0, eng.strategyDailyPnl("S1"), 1e-6);
        assertEquals(-334_400.0, eng.unrealizedPnl(), 1e-6);
        assertEquals(0.0, eng.realizedPnl(), 0.0);
        assertEquals(eng.unrealizedPnl(),
                eng.metrics.gaugeValue("risk_unrealized_pnl"), 0.0);
        assertEquals(Rules.KILL_GLOBAL,
                eng.checkOrder(limit(1, 0, 100, 2100, T0 + 3 * NS)).ruleId());
    }

    /** Older market updates never overwrite a newer mark. */
    @Test
    public void marketUpdateRegressionIsDropped() {
        RiskEngine eng = engine();
        eng.onMarket(1, 2000, 2002, T0 - NS);
        assertEquals(1, eng.metrics.counterValue(
                "risk_market_regressions_dropped_total"));
        RiskDecision d = eng.checkOrder(typed(1, 1, 0, 45_000, 0,
                OrderRequest.MARKET, "S1", T0 + 1));
        assertEquals(Rules.FAT_FINGER_NOTIONAL, d.ruleId());
    }

    /** Scenario: fat-fingered USD/JPY ticket priced in USD via lot size. */
    @Test
    public void riskFxNotionalUsesLotSizeAndCcy() {
        RiskEngine eng = engine();
        long t = T0 + 100_000_000L;
        RiskDecision d = eng.checkOrder(typed(1, 103, 1, 1001, 0,
                OrderRequest.MARKET, "S1", t));
        assertEquals(d.reason(), Rules.FAT_FINGER_NOTIONAL, d.ruleId());
        assertTrue(d.reason(), d.reason().startsWith("notional 1001000.00 USD"));
        assertTrue(eng.checkOrder(typed(2, 103, 1, 999, 0, OrderRequest.MARKET,
                "S1", t)).allowed());
        assertEquals(Rules.FAT_FINGER_NOTIONAL, eng.checkOrder(typed(3, 103, 1,
                50_000, 0, OrderRequest.MARKET, "S1", t)).ruleId());
        // missing conversion pair (GBP/USD never marked)
        eng.onMarket(108, 85_315, 85_325, t);
        assertEquals(Rules.FX_RATE_MISSING, eng.checkOrder(typed(4, 108, 0, 100,
                0, OrderRequest.MARKET, "S1", t)).ruleId());
        eng.onMarket(102, 127_335, 127_345, t);
        assertTrue(eng.checkOrder(typed(5, 108, 0, 100, 0, OrderRequest.MARKET,
                "S1", t + 10_000_000L)).allowed());
        // a stale conversion rate is as bad as a missing one
        eng.onMarket(108, 85_315, 85_325, t + 6 * NS);
        d = eng.checkOrder(typed(6, 108, 0, 100, 0, OrderRequest.MARKET, "S1",
                t + 6 * NS));
        assertEquals(Rules.FX_RATE_MISSING, d.ruleId());
        assertTrue(d.reason(), d.reason().contains("age"));
    }

    /** A JPY loss is converted before the loss limit is evaluated. */
    @Test
    public void fxPnlIsConvertedBeforeLossLimits() {
        RiskEngine eng = engine();
        eng.onMarket(103, 149_995, 150_005, T0);
        eng.onFill(new RiskFill(T0 + NS, "S1", 103, 0, 0, 10, 150_000));
        eng.onFill(new RiskFill(T0 + NS, "S1", 103, 0, 1, 10, 149_985));
        assertEquals(-1.0, eng.strategyPnl("S1"), 1e-9); // -150 JPY at 150
        eng.onFill(new RiskFill(T0 + 2 * NS, "S2", 103, 0, 0, 5_000, 150_000));
        eng.onFill(new RiskFill(T0 + 2 * NS, "S2", 103, 0, 1, 5_000, 148_500));
        assertTrue(eng.audit().stream().anyMatch(e ->
                e.ruleId().equals(Rules.STRATEGY_LOSS) && e.scopeId().equals("S2")));
    }

    /** Scenario (Knight-style): a burst of MARKET buys during a latency spike. */
    @Test
    public void riskInflightMarketOrdersCountInProjection() {
        RiskEngine eng = engine();
        eng.onMarket(1, 1999, 2001, T0); // mid 20.00
        eng.onFill(fill("S1", 1, 0, 90_000, 2000, T0 + 1));
        long t = T0 + 100_000_000L;
        assertTrue(eng.checkOrder(typed(1, 1, 0, 5_000, 0, OrderRequest.MARKET,
                "S1", t)).allowed());
        assertTrue(eng.checkOrder(typed(2, 1, 0, 5_000, 0, OrderRequest.MARKET,
                "S1", t + 10_000_000L)).allowed());
        RiskDecision d = eng.checkOrder(typed(3, 1, 0, 5_000, 0,
                OrderRequest.MARKET, "S1", t + 20_000_000L));
        assertEquals(d.reason(), Rules.POSITION_LIMIT, d.ruleId());
        assertTrue(d.reason().contains("105000"));
        assertEquals(Rules.POSITION_LIMIT, eng.checkOrder(typed(4, 1, 0, 5_000,
                0, OrderRequest.IOC, "S1", t + 30_000_000L)).ruleId());
        assertEquals(2, eng.openOrderCount());
        eng.onFill(new RiskFill(t + 40_000_000L, "S1", 1, 1, 0, 5_000, 2000));
        assertEquals(1, eng.openOrderCount());
        eng.onOrderDone(2);
        assertEquals(0, eng.openOrderCount());
        assertTrue(eng.checkOrder(typed(5, 1, 0, 5_000, 0, OrderRequest.MARKET,
                "S1", t + 50_000_000L)).allowed());
    }

    /** PEG orders are tracked at their pegged touch. */
    @Test
    public void riskPegOrdersTrackedForSelfMatchAndProjection() {
        RiskEngine eng = engine();
        eng.onMarket(1, 1999, 2001, T0);
        long t = T0 + 100_000_000L;
        assertTrue(eng.checkOrder(typed(1, 1, 0, 100, 0, OrderRequest.PEG,
                "S1", t)).allowed());
        RiskDecision d = eng.checkOrder(typed(2, 1, 1, 50, 0, OrderRequest.MARKET,
                "S1", t + 10_000_000L));
        assertEquals(Rules.SELF_MATCH, d.ruleId());
        assertTrue(d.reason(), d.reason().contains("at 1999"));
        assertTrue(eng.checkOrder(typed(3, 1, 1, 50, 2005, OrderRequest.LIMIT,
                "S1", t + 20_000_000L)).allowed());
        eng.onFill(fill("S1", 1, 0, 99_900, 2000, t + 25_000_000L));
        assertEquals(Rules.POSITION_LIMIT, eng.checkOrder(typed(4, 1, 0, 100,
                1999, OrderRequest.LIMIT, "S1", t + 30_000_000L)).ruleId());
        eng.onOrderDone(1);
        assertTrue(eng.checkOrder(typed(5, 1, 0, 100, 1999, OrderRequest.LIMIT,
                "S1", t + 40_000_000L)).allowed());
    }

    /** Gross includes every resting order across instruments. */
    @Test
    public void riskGrossIncludesRestingOrders() {
        TreeMap<Long, InstrumentRef> refs = new TreeMap<>();
        for (long iid = 1; iid <= 41; iid++) {
            refs.put(iid, InstrumentRef.equity(0.01));
        }
        RiskEngine eng = RiskEngine.fromConfig(configDoc(), refs);
        for (long iid = 1; iid <= 41; iid++) {
            eng.onMarket(iid, 2999, 3001, T0);
        }
        long t = T0 + 100_000_000L;
        for (int i = 0; i < 40; i++) {
            int side = i % 2 == 0 ? 0 : 1;
            assertTrue("resting order " + i, eng.checkOrder(typed(i + 1, i + 1,
                    side, 4_000, 3000, OrderRequest.LIMIT, "S" + (i % 4),
                    t + i * 10_000_000L)).allowed());
        }
        assertEquals(40, eng.openOrderCount());
        RiskDecision d = eng.checkOrder(typed(41, 41, 0, 8_000, 3000,
                OrderRequest.LIMIT, "S0", t + 400_000_000L));
        assertEquals(d.reason(), Rules.GROSS_NOTIONAL, d.ruleId());
        eng.onOrderDone(1);
        assertTrue(eng.checkOrder(typed(42, 41, 0, 3_000, 3000,
                OrderRequest.LIMIT, "S0", t + 410_000_000L)).allowed());
    }

    /** Scenario: two threads share a strategy id; one clock runs behind. */
    @Test
    public void riskThrottleRegressionThenForward() {
        RiskEngine eng = engine();
        long base = T0 + 100_000_000L;
        for (int i = 0; i < 4; i++) {
            assertTrue(eng.checkOrder(typed(10 + i, 1, 0, 10, 0,
                    OrderRequest.MARKET, "S1", base + i)).allowed());
        }
        assertEquals(Rules.RATE_THROTTLE, eng.checkOrder(typed(20, 1, 0, 10, 0,
                OrderRequest.MARKET, "S1", base - 10 * NS)).ruleId());
        assertEquals(Rules.RATE_THROTTLE, eng.checkOrder(typed(21, 1, 0, 10, 0,
                OrderRequest.MARKET, "S1", base + 3)).ruleId());
        assertTrue(eng.checkOrder(typed(22, 1, 0, 10, 0, OrderRequest.MARKET,
                "S1", base + 2_000_003L)).allowed());
    }

    /** Scenario: 11:40 latch, root cause fixed, CRO approves a resumed session. */
    @Test
    public void riskClearKillAfterLossLatchResumesWithOverride() {
        RiskEngine eng = engine();
        eng.onFill(fill("S1", 1, 0, 95_000, 2452, T0 + NS));
        eng.onFill(fill("S1", 1, 1, 94_900, 2398, T0 + NS)); // -51,246, 100 left
        assertEquals(Rules.KILL_STRATEGY,
                eng.checkOrder(limit(1, 0, 100, 2450, T0 + NS)).ruleId());
        eng.clearKill(Scope.STRATEGY, "S1", T0 + 2 * NS, "ops clear");
        RiskDecision d = eng.checkOrder(limit(2, 0, 100, 2450, T0 + 2 * NS));
        assertEquals(d.reason(), Rules.STRATEGY_LOSS, d.ruleId());
        assertEquals(Decision.REJECT, d.decision());
        // the next mark of a held instrument re-latches (no fill needed)
        eng.onMarket(1, 2450, 2452, T0 + 3 * NS);
        assertEquals(Rules.KILL_STRATEGY,
                eng.checkOrder(limit(3, 0, 100, 2450, T0 + 3 * NS)).ruleId());
        long latches = eng.audit().stream().filter(e ->
                e.ruleId().equals(Rules.STRATEGY_LOSS)
                        && e.decision() == Decision.KILL.code()).count();
        assertEquals(2, latches);
        // an override below the loss is legal but ineffective
        eng.overrideLossLimit(Scope.STRATEGY, "S1", 51_000.0, T0 + 4 * NS, "cro");
        eng.clearKill(Scope.STRATEGY, "S1", T0 + 4 * NS, "ops clear #2");
        assertEquals(Rules.STRATEGY_LOSS,
                eng.checkOrder(limit(4, 0, 100, 2450, T0 + 4 * NS)).ruleId());
        // override above the loss + clear -> ALLOW
        eng.overrideLossLimit(Scope.STRATEGY, "S1", 75_000.0, T0 + 5 * NS, "cro");
        eng.clearKill(Scope.STRATEGY, "S1", T0 + 5 * NS, "ops clear INC-1");
        eng.onMarket(1, 2450, 2452, T0 + 5 * NS);
        assertTrue(eng.checkOrder(limit(5, 0, 100, 2450, T0 + 5 * NS)).allowed());
        RiskEvent ov = eng.audit().stream()
                .filter(e -> e.ruleId().equals(Rules.LOSS_LIMIT_OVERRIDE))
                .findFirst().orElseThrow();
        assertEquals("daily loss limit 50000.00 -> 51000.00 approved by cro",
                ov.reason());
        assertEquals(Severity.WARN.code(), ov.severity());
        try {
            eng.overrideLossLimit(Scope.STRATEGY, "S1", -1.0, T0, "x");
            fail("negative override must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            eng.overrideLossLimit(Scope.INSTRUMENT, "1", 10.0, T0, "x");
            fail("instrument scope has no loss limit");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    /** Scenario: the FX book rolls at 22:00 Sunday. */
    @Test
    public void scenarioSessionRollRebasesDailyPnlAndKeepsLatches() {
        RiskEngine eng = engine();
        eng.onFill(fill("S1", 1, 0, 95_000, 2452, T0 + 1));
        eng.onFill(fill("S2", 2, 0, 1_000, 3120, T0 + 1));
        eng.onFill(fill("S2", 2, 1, 1_000, 3110, T0 + 1));
        eng.overrideLossLimit(Scope.GLOBAL, "", 400_000.0, T0, "cro");
        eng.engageKill(Scope.STRATEGY, "S2", T0, "manual");
        assertEquals(-1_050.0, eng.globalDailyPnl(), 1e-6);
        eng.rollSession(T0 + NS, "roll");
        assertEquals(0.0, eng.globalDailyPnl(), 0.0);
        assertEquals(95_000, eng.position(1));
        assertEquals(Rules.KILL_STRATEGY, eng.checkOrder(typed(1, 1, 0, 100, 2450,
                OrderRequest.LIMIT, "S2", T0 + NS)).ruleId());
        eng.onMarket(1, 2099, 2101, T0 + 2 * NS);
        assertTrue("override gone after the roll", eng.killSwitchEngaged());
        assertTrue(eng.audit().stream().anyMatch(e ->
                e.ruleId().equals(Rules.SESSION_ROLLED)));
    }

    private static List<String> script(RiskEngine eng, int from, int n) {
        List<String> lines = new ArrayList<>();
        for (int k = from; k < from + n; k++) {
            long t = T0 + 100_000_000L + k * 20_000_000L;
            int side = k % 3 == 0 ? 1 : 0;
            long px = k % 5 == 0 ? 0 : 2450 + (k % 4);
            long iid = 1 + (k % 2);
            long price = k % 2 == 1 ? 3120 : px;
            int type = px == 0 ? OrderRequest.MARKET : OrderRequest.LIMIT;
            OrderRequest o = typed(1000 + k, iid, side, 100 + k, price, type,
                    "S" + (k % 3), t);
            RiskDecision d = eng.checkOrder(o);
            lines.add(o.orderId() + ":" + d.ruleId());
            if (k % 4 == 0) {
                eng.onFill(new RiskFill(t, o.strategyId(), iid, o.orderId(), side,
                        50, iid == 1 ? 2451 : 3120));
            }
            if (k % 7 == 0) {
                eng.onOrderDone(o.orderId());
            }
            if (k % 9 == 0) {
                eng.onMarket(1, 2449 + (k % 3), 2452, t);
            }
        }
        return lines;
    }

    /** Scenario: mid-session restart from a snapshot; bootstrap gate. */
    @Test
    public void riskSnapshotRestoreRoundtrip() {
        RiskEngine unbroken = engine();
        assertEquals(30, script(unbroken, 0, 30).size());
        String snapText = unbroken.snapshot();
        Map<String, Object> snap = Json.object(Json.parse(snapText));
        assertEquals(1L, Json.asLong(snap.get("x-version")));
        RiskLimits limits = RiskLimits.fromJson(configDoc());
        RiskEngine restored = RiskEngine.restore(limits, refs(), snap, T0 + 5 * NS);
        assertEquals(1, restored.audit().size());
        assertEquals(Rules.STATE_RESTORED, restored.audit().get(0).ruleId());
        List<String> a = script(unbroken, 30, 20);
        List<String> b = script(restored, 30, 20);
        assertEquals(a, b);
        List<String> full = unbroken.auditJsonl().lines().toList();
        List<String> tail = restored.auditJsonl().lines().skip(1).toList();
        assertEquals(full.subList(full.size() - tail.size(), full.size()), tail);
        assertEquals(Json.parse(unbroken.snapshot()), Json.parse(restored.snapshot()));
        // malformed snapshots are refused as a whole
        Map<String, Object> bad = Json.object(Json.parse(snapText));
        bad.put("x-version", 99L);
        try {
            RiskEngine.restore(limits, refs(), bad, T0);
            fail("bad version must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        Map<String, Object> bad2 = Json.object(Json.parse(snapText));
        Json.object(Json.array(bad2.get("lots")).get(0)).put("avg_price", "nan");
        try {
            RiskEngine.restore(limits, refs(), bad2, T0);
            fail("bad lot must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        // bootstrap gate: fail-closed until positions arrive
        RiskEngine fresh = engine();
        fresh.requireBootstrap();
        RiskDecision d = fresh.checkOrder(limit(1, 0, 100, 2450, T0 + 1));
        assertEquals(Rules.NOT_BOOTSTRAPPED, d.ruleId());
        assertEquals(Severity.BREACH, d.severity());
        int rejected = fresh.bootstrapPositions(List.of(
                fill("S1", 1, 0, 95_000, 2452, T0),
                fill("S1", 999, 0, 1, 1, T0)), T0 + NS);
        assertEquals(1, rejected);
        assertEquals(95_000, fresh.position(1));
        assertEquals(Rules.POSITION_LIMIT,
                fresh.checkOrder(limit(2, 0, 40_000, 2452, T0 + NS)).ruleId());
        assertTrue(fresh.audit().stream().anyMatch(e ->
                e.ruleId().equals(Rules.BOOTSTRAP_COMPLETE)));
    }

    /** Malformed fills are audited and dropped, never applied or thrown. */
    @Test
    public void malformedFillsAreAuditedAndDropped() {
        RiskEngine eng = engine();
        assertFalse(eng.onFill(new RiskFill(T0, "S1", 1, 0, 0, 0, 2452)));
        assertFalse(eng.onFill(new RiskFill(T0, "S1", 1, 0, 2, 100, 2452)));
        assertFalse(eng.onFill(new RiskFill(T0, "S1", 1, 0, 0, 100, 0)));
        assertFalse(eng.onFill(new RiskFill(T0, "S1", 999, 0, 0, 100, 2452)));
        assertEquals(0, eng.position(1));
        assertEquals(4, eng.metrics.counterValue("risk_malformed_fills_total"));
        assertTrue(eng.onFill(new RiskFill(T0, "S1", 1, 0, 0, 100, 2452)));
        assertEquals(100, eng.position(1));
        assertNull("unpriceable currency makes P&L undeterminable",
                engineWithoutJpyRate().globalDailyPnl());
    }

    private static RiskEngine engineWithoutJpyRate() {
        RiskEngine eng = RiskEngine.fromConfig(configDoc(), refs());
        eng.onFill(new RiskFill(T0, "S1", 103, 0, 0, 10, 150_000));
        return eng;
    }

    /** Duplicate-id window > 0: ids expire and the seen set is pruned. */
    @Test
    public void duplicateWindowExpiresAndPrunes() {
        Map<String, Object> doc = configDoc();
        Json.object(doc.get("per_order")).put("duplicate_order_window_ns", NS);
        RiskEngine eng = RiskEngine.fromConfig(doc, refs());
        eng.onMarket(1, 2450, 2452, T0);
        assertTrue(eng.checkOrder(limit(7, 0, 100, 2450, T0)).allowed());
        assertEquals(Rules.DUPLICATE_ORDER_ID,
                eng.checkOrder(limit(7, 0, 100, 2450, T0 + NS)).ruleId());
        assertTrue(eng.checkOrder(limit(7, 0, 100, 2450, T0 + NS + 1)).allowed());
        Map<String, Object> snap = Json.object(Json.parse(eng.snapshot()));
        assertEquals(1, Json.array(snap.get("seen_orders")).size());
    }

    /** Self-match is a firm-level (any strategy, any venue) control. */
    @Test
    public void selfMatchIsFirmWideAcrossStrategiesAndVenues() {
        RiskEngine eng = engine();
        assertTrue(eng.checkOrder(limit(1, 0, 100, 2450, T0 + 1)).allowed());
        RiskDecision d = eng.checkOrder(new OrderRequest(2, 1, 1, 50, 2450,
                OrderRequest.LIMIT, 2, "S2", 0.5, T0 + 10_000_001L));
        assertEquals(Rules.SELF_MATCH, d.ruleId());
    }
}
