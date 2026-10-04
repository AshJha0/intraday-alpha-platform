package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.risk.Decision;
import com.iap.risk.InstrumentRef;
import com.iap.risk.OrderRequest;
import com.iap.risk.RiskDecision;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskFill;
import com.iap.risk.RiskLimits;
import com.iap.risk.Rules;
import com.iap.risk.Scope;
import com.iap.risk.Severity;

/**
 * Per-rule allow/deny coverage of the hard risk engine, kill-switch
 * precedence (global &gt; strategy &gt; instrument &gt; venue) and the
 * fail-closed behavior on malformed configuration (spec §16).
 */
public class RiskRuleTest {
    private static final long TS = 1_800_000_000_000_000_000L;

    private static Map<String, Object> configDoc() {
        return Json.object(com.iap.config.Json.parseFile(
                java.nio.file.Paths.get("..", "configs", "risk", "risk.json")));
    }

    private static RiskEngine engine() {
        TreeMap<Long, Double> ticks = new TreeMap<>();
        ticks.put(1L, 0.01);
        ticks.put(2L, 0.01);
        RiskEngine eng = RiskEngine.fromConfigTicks(configDoc(), ticks);
        eng.onMarket(1, 2450, 2452, TS);
        eng.onMarket(2, 3119, 3121, TS);
        return eng;
    }

    private static OrderRequest limitBuy(long id, long qty, long px, long ts) {
        return new OrderRequest(id, 1, 0, qty, px, OrderRequest.LIMIT, 1,
                "S1", 0.5, ts);
    }

    private static void expect(RiskEngine eng, OrderRequest o, String rule) {
        RiskDecision d = eng.checkOrder(o);
        assertEquals("rule (" + d.reason() + ")", rule, d.ruleId());
        if (Rules.ALLOW.equals(rule)) {
            assertEquals(Decision.ALLOW, d.decision());
            assertEquals(Severity.INFO, d.severity());
        } else {
            assertEquals(Decision.REJECT, d.decision());
        }
    }

    @Test
    public void cleanOrderIsAllowed() {
        RiskEngine eng = engine();
        RiskDecision d = eng.checkOrder(limitBuy(1, 100, 2450, TS + 1));
        assertTrue(d.allowed());
        assertEquals(Rules.ALLOW, d.ruleId());
        assertEquals(Severity.INFO, d.severity());
        assertEquals(1, eng.metrics.counterValue("risk_allowed_total"));
        assertEquals(1, eng.metrics.counterValue("risk_decisions_total"));
    }

    @Test
    public void malformedOrdersAreRejectedWithReasons() {
        RiskEngine eng = engine();
        // qty <= 0
        expect(eng, new OrderRequest(1, 1, 0, 0, 2450, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 1), Rules.MALFORMED_ORDER);
        // bad side
        expect(eng, new OrderRequest(2, 1, 2, 10, 2450, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 1), Rules.MALFORMED_ORDER);
        // unknown order type
        expect(eng, new OrderRequest(3, 1, 0, 10, 2450, 9, 1, "S1", 0.5,
                TS + 1), Rules.MALFORMED_ORDER);
        // MARKET with a price
        expect(eng, new OrderRequest(4, 1, 0, 10, 2450, OrderRequest.MARKET, 1,
                "S1", 0.5, TS + 1), Rules.MALFORMED_ORDER);
        // LIMIT without a price
        expect(eng, new OrderRequest(5, 1, 0, 10, 0, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 1), Rules.MALFORMED_ORDER);
        // urgency out of range
        expect(eng, new OrderRequest(6, 1, 0, 10, 2450, OrderRequest.LIMIT, 1,
                "S1", 1.5, TS + 1), Rules.MALFORMED_ORDER);
    }

    @Test
    public void referenceDataAndMarketGates() {
        RiskEngine eng = engine();
        // unknown instrument
        expect(eng, new OrderRequest(1, 999, 0, 10, 2450, OrderRequest.LIMIT,
                1, "S1", 0.5, TS + 1), Rules.UNKNOWN_INSTRUMENT);
        // duplicate id (window 0 = whole session)
        expect(eng, limitBuy(2, 10, 2450, TS + 1), Rules.ALLOW);
        expect(eng, limitBuy(2, 10, 2451, TS + 2), Rules.DUPLICATE_ORDER_ID);
        // stale price: age beyond stale_feed_timeout_ns (5s)
        expect(eng, limitBuy(3, 10, 2450, TS + 6_000_000_000L),
                Rules.STALE_PRICE);
        // no reference price at all
        TreeMap<Long, Double> ticks = new TreeMap<>();
        ticks.put(1L, 0.01);
        RiskEngine fresh = RiskEngine.fromConfigTicks(configDoc(), ticks);
        RiskDecision d = fresh.checkOrder(limitBuy(1, 10, 2450, TS));
        assertEquals(Rules.STALE_PRICE, d.ruleId());
        assertTrue(d.reason().contains("no reference price"));
    }

    @Test
    public void sequenceGapGateClosesAndRecovers() {
        RiskEngine eng = engine();
        eng.onSequenceGap(1, TS + 1);
        expect(eng, limitBuy(1, 10, 2450, TS + 2), Rules.SEQUENCE_GAP);
        eng.onFeedRecovered(1, TS + 3);
        expect(eng, limitBuy(2, 10, 2450, TS + 4), Rules.ALLOW);
    }

    @Test
    public void fatFingerPriceBandAndThrottle() {
        RiskEngine eng = engine();
        expect(eng, limitBuy(1, 60_000, 2450, TS + 1), Rules.FAT_FINGER_QTY);
        expect(eng, limitBuy(2, 45_000, 2460, TS + 2),
                Rules.FAT_FINGER_NOTIONAL);
        expect(eng, limitBuy(3, 100, 3000, TS + 3), Rules.PRICE_BAND);
        // token bucket: burst 4 at one timestamp, refill 500/s
        RiskEngine thr = engine();
        for (long i = 1; i <= 4; i++) {
            expect(thr, new OrderRequest(i, 1, 0, 10, 0, OrderRequest.MARKET,
                    1, "S9", 0.5, TS + 10), Rules.ALLOW);
        }
        expect(thr, new OrderRequest(5, 1, 0, 10, 0, OrderRequest.MARKET, 1,
                "S9", 0.5, TS + 10), Rules.RATE_THROTTLE);
        // 2ms later one token (500/s) has refilled
        expect(thr, new OrderRequest(6, 1, 0, 10, 0, OrderRequest.MARKET, 1,
                "S9", 0.5, TS + 10 + 2_000_000L), Rules.ALLOW);
    }

    @Test
    public void selfMatchPreventionVariants() {
        // 10ms between orders so the token bucket (burst 4, 500/s refill)
        // never interferes with the self-match assertions
        long step = 10_000_000L;
        RiskEngine eng = engine();
        expect(eng, limitBuy(1, 100, 2450, TS + step), Rules.ALLOW); // bid rests
        // priced sell at/below own bid crosses
        expect(eng, new OrderRequest(2, 1, 1, 50, 2450, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 2 * step), Rules.SELF_MATCH);
        // priced sell above own bid is fine
        expect(eng, new OrderRequest(3, 1, 1, 50, 2455, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 3 * step), Rules.ALLOW);
        // unpriced marketable buy vs own resting ask (order 3) crosses
        expect(eng, new OrderRequest(4, 1, 0, 50, 0, OrderRequest.MARKET, 1,
                "S1", 0.5, TS + 4 * step), Rules.SELF_MATCH);
        // PEG orders are checked at their pegged touch (bid 2450 for a
        // buy) — below the own ask at 2455, so no cross
        expect(eng, new OrderRequest(5, 1, 0, 50, 0, OrderRequest.PEG, 1,
                "S1", 0.5, TS + 5 * step), Rules.ALLOW);
        // ... but a sell at/below the pegged bid would cross it
        expect(eng, new OrderRequest(7, 1, 1, 50, 2450, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 5 * step + 1), Rules.SELF_MATCH);
        // once the resting ask is done, the unpriced buy is fine (an
        // unpriced BUY only conflicts with opposite-side resting orders;
        // the own resting bid 1 does not block it)
        eng.onOrderDone(3);
        expect(eng, new OrderRequest(6, 1, 0, 50, 0, OrderRequest.IOC, 1,
                "S1", 0.5, TS + 6 * step), Rules.ALLOW);
    }

    @Test
    public void unpricedBuyIgnoresOwnRestingBids() {
        RiskEngine eng = engine();
        expect(eng, limitBuy(1, 100, 2450, TS + 1), Rules.ALLOW); // own bid
        expect(eng, new OrderRequest(2, 1, 0, 50, 0, OrderRequest.MARKET, 1,
                "S1", 0.5, TS + 2), Rules.ALLOW);
    }

    @Test
    public void positionAndNotionalProjections() {
        RiskEngine eng = engine();
        // fill 95k long; buying 40k more projects past 100k position cap
        eng.onFill(new RiskFill(TS + 1, "S1", 1, 0, 0, 95_000, 2452));
        expect(eng, limitBuy(1, 40_000, 2452, TS + 2), Rules.POSITION_LIMIT);
        // 3k more breaches the 2.4M instrument notional at mid 24.51
        expect(eng, limitBuy(2, 3_000, 2452, TS + 3),
                Rules.INSTRUMENT_NOTIONAL);
        // selling reduces exposure -> allowed
        expect(eng, new OrderRequest(3, 1, 1, 40_000, 2455, OrderRequest.LIMIT,
                1, "S1", 0.5, TS + 4), Rules.ALLOW);
    }

    @Test
    public void openOrdersCountInPositionProjection() {
        RiskEngine eng = engine();
        // two resting 40k buys, then a third projects 120k > 100k cap
        expect(eng, limitBuy(1, 40_000, 2450, TS + 1), Rules.ALLOW);
        expect(eng, limitBuy(2, 40_000, 2450, TS + 2), Rules.ALLOW);
        expect(eng, limitBuy(3, 40_000, 2450, TS + 3), Rules.POSITION_LIMIT);
        // a fill against order 1 moves qty from open to position; the
        // worst-case projection is unchanged and still rejects
        eng.onFill(new RiskFill(TS + 4, "S1", 1, 1, 0, 40_000, 2450));
        expect(eng, limitBuy(4, 40_000, 2450, TS + 5), Rules.POSITION_LIMIT);
    }

    @Test
    public void grossNotionalFailsClosedWithoutMarks() {
        TreeMap<Long, Double> ticks = new TreeMap<>();
        ticks.put(1L, 0.01);
        ticks.put(2L, 0.01);
        RiskEngine eng = RiskEngine.fromConfigTicks(configDoc(), ticks);
        eng.onMarket(1, 2450, 2452, TS);
        // instrument 2 position exists but never had a mark
        eng.onFill(new RiskFill(TS + 1, "S1", 2, 0, 0, 100, 3120));
        RiskDecision d = eng.checkOrder(limitBuy(1, 100, 2450, TS + 2));
        assertEquals(Rules.GROSS_NOTIONAL, d.ruleId());
        assertTrue(d.reason().contains("fail-closed"));
    }

    @Test
    public void lossLimitsLatchKillSwitches() {
        RiskEngine eng = engine();
        // round trip: buy 95k @ 2452, sell 95k @ 2398 -> -51,300 realized
        eng.onFill(new RiskFill(TS + 1, "S1", 1, 0, 0, 95_000, 2452));
        eng.onFill(new RiskFill(TS + 2, "S1", 1, 0, 1, 95_000, 2398));
        assertEquals(-51_300.0, eng.strategyPnl("S1"), 1e-6);
        expect(eng, limitBuy(1, 100, 2450, TS + 3), Rules.KILL_STRATEGY);
        assertFalse("global not yet killed", eng.killSwitchEngaged());
        // another strategy loses enough to breach the 250k daily loss:
        // the buy at 35.00 marked at 31.20 is -380k UNREALIZED and latches
        // the global switch at the fill, before the closing sell
        eng.onMarket(2, 3119, 3121, TS + 3);
        eng.onFill(new RiskFill(TS + 4, "S2", 2, 0, 0, 100_000, 3500));
        assertTrue("mark-to-market latch", eng.killSwitchEngaged());
        assertEquals(-380_000.0, eng.unrealizedPnl(), 1e-6);
        eng.onFill(new RiskFill(TS + 5, "S2", 2, 0, 1, 100_000, 3120));
        assertTrue(eng.killSwitchEngaged());
        assertEquals(-431_300.0, eng.globalDailyPnl(), 1e-6);
        assertEquals(1.0, eng.metrics.gaugeValue("risk_kill_switch_engaged"), 0.0);
        expect(eng, new OrderRequest(2, 2, 0, 10, 3120, OrderRequest.LIMIT, 1,
                "S3", 0.5, TS + 6), Rules.KILL_GLOBAL);
    }

    @Test
    public void killSwitchPrecedenceGlobalStrategyInstrumentVenue() {
        RiskEngine eng = engine();
        eng.engageKill(Scope.VENUE, "1", TS + 1, "venue kill");
        expect(eng, limitBuy(1, 10, 2450, TS + 2), Rules.KILL_VENUE);
        eng.engageKill(Scope.INSTRUMENT, "1", TS + 3, "instrument kill");
        expect(eng, limitBuy(2, 10, 2450, TS + 4), Rules.KILL_INSTRUMENT);
        eng.engageKill(Scope.STRATEGY, "S1", TS + 5, "strategy kill");
        expect(eng, limitBuy(3, 10, 2450, TS + 6), Rules.KILL_STRATEGY);
        eng.engageKill(Scope.GLOBAL, "", TS + 7, "global kill");
        expect(eng, limitBuy(4, 10, 2450, TS + 8), Rules.KILL_GLOBAL);
        // clearing in reverse order re-exposes the next switch down
        eng.clearKill(Scope.GLOBAL, "", TS + 9, "clear");
        expect(eng, limitBuy(5, 10, 2450, TS + 10), Rules.KILL_STRATEGY);
        eng.clearKill(Scope.STRATEGY, "S1", TS + 11, "clear");
        expect(eng, limitBuy(6, 10, 2450, TS + 12), Rules.KILL_INSTRUMENT);
        eng.clearKill(Scope.INSTRUMENT, "1", TS + 13, "clear");
        expect(eng, limitBuy(7, 10, 2450, TS + 14), Rules.KILL_VENUE);
        eng.clearKill(Scope.VENUE, "1", TS + 15, "clear");
        expect(eng, limitBuy(8, 10, 2450, TS + 16), Rules.ALLOW);
    }

    @Test
    public void venueDisconnectRejectsUntilReconnect() {
        RiskEngine eng = engine();
        eng.onVenueDisconnect(1, TS + 1);
        expect(eng, limitBuy(1, 10, 2450, TS + 2), Rules.VENUE_DISCONNECTED);
        // venue 0 = SOR-routed: every known venue is down, nowhere to route
        expect(eng, new OrderRequest(2, 1, 0, 10, 2450, OrderRequest.LIMIT, 0,
                "S1", 0.5, TS + 3), Rules.VENUE_DISCONNECTED);
        eng.onVenueReconnect(1, TS + 4);
        expect(eng, limitBuy(3, 10, 2450, TS + 5), Rules.ALLOW);
    }

    @Test
    public void failClosedOnMalformedConfig() {
        TreeMap<Long, Double> ticks = new TreeMap<>();
        ticks.put(1L, 0.01);
        // missing limit
        Map<String, Object> doc = configDoc();
        Json.object(doc.get("per_order")).remove("max_order_qty");
        RiskEngine eng = RiskEngine.fromConfigTicks(doc, ticks);
        eng.onMarket(1, 2450, 2452, TS);
        RiskDecision d = eng.checkOrder(limitBuy(1, 10, 2450, TS + 1));
        assertEquals(Rules.CONFIG_MISSING, d.ruleId());
        assertEquals(Decision.REJECT, d.decision());
        assertEquals(Severity.BREACH, d.severity());
        assertTrue(d.reason().contains("max_order_qty"));
        // negative limit
        Map<String, Object> doc2 = configDoc();
        Json.object(doc2.get("global")).put("max_daily_loss", -5.0);
        RiskEngine eng2 = RiskEngine.fromConfigTicks(doc2, ticks);
        assertEquals(Rules.CONFIG_MISSING,
                eng2.checkOrder(limitBuy(1, 10, 2450, TS)).ruleId());
        // wrong type
        Map<String, Object> doc3 = configDoc();
        Json.object(doc3.get("per_order")).put("max_order_qty", "many");
        assertEquals(Rules.CONFIG_MISSING, RiskEngine.fromConfigTicks(doc3, ticks)
                .checkOrder(limitBuy(1, 10, 2450, TS)).ruleId());
        // an entirely empty document
        assertEquals(Rules.CONFIG_MISSING,
                RiskEngine.fromConfigTicks(new HashMap<>(), ticks)
                        .checkOrder(limitBuy(1, 10, 2450, TS)).ruleId());
    }

    @Test
    public void strictLimitsParserAcceptsRepoConfig() {
        RiskLimits limits = RiskLimits.fromJson(configDoc());
        assertEquals(50_000, limits.maxOrderQty());
        assertEquals(0, limits.duplicateOrderWindowNs());
        assertFalse(limits.killSwitchEngaged());
        assertEquals(500.0, limits.maxOrderRatePerSec(), 0.0);
    }

    private static final long SEC = 1_000_000_000L;

    @Test
    public void auditReasonsPrintU64OrderIdsUnsigned() {
        // Audit parity with the Rust reference: order ids are u64 and are
        // printed as unsigned decimals. 2^63 is Long.MIN_VALUE in Java —
        // the reason text must still be byte-identical to Rust's
        // "would cross own open order {oid} at {price_ticks}".
        RiskEngine eng = engine();
        long bigId = Long.MIN_VALUE; // 2^63 = 9223372036854775808 as u64
        // Resting LIMIT sell with the huge id...
        expect(eng, new OrderRequest(bigId, 1, 1, 10, 2451,
                OrderRequest.LIMIT, 1, "S1", 0.5, TS + SEC), Rules.ALLOW);
        // ...then a buy that would cross it names it, unsigned.
        RiskDecision d = eng.checkOrder(new OrderRequest(7, 1, 0, 10, 2451,
                OrderRequest.LIMIT, 1, "S1", 0.5, TS + 2 * SEC));
        assertEquals(Rules.SELF_MATCH, d.ruleId());
        assertEquals("would cross own open order 9223372036854775808 at 2451",
                d.reason());
        // The audit line carries the exact same text.
        assertTrue(eng.auditJsonl().contains(
                "\"reason\":\"would cross own open order "
                        + "9223372036854775808 at 2451\""));
    }

    @Test
    public void selfMatchNamesFirstRestingOrderInUnsignedIdOrder() {
        // resting is iterated in UNSIGNED id order (Rust BTreeMap<u64>):
        // 5 < 2^63 as u64, so the SELF_MATCH reason must name order 5 even
        // though 2^63 sorts first under signed ordering.
        RiskEngine eng = engine();
        expect(eng, new OrderRequest(5, 1, 1, 10, 2451, OrderRequest.LIMIT,
                1, "S1", 0.5, TS + SEC), Rules.ALLOW);
        expect(eng, new OrderRequest(Long.MIN_VALUE, 1, 1, 10, 2451,
                OrderRequest.LIMIT, 1, "S1", 0.5, TS + 2 * SEC), Rules.ALLOW);
        RiskDecision d = eng.checkOrder(new OrderRequest(9, 1, 0, 10, 2451,
                OrderRequest.LIMIT, 1, "S1", 0.5, TS + 3 * SEC));
        assertEquals(Rules.SELF_MATCH, d.ruleId());
        assertEquals("would cross own open order 5 at 2451", d.reason());
    }

    @Test
    public void duplicateOrderIdReasonPrintsUnsigned() {
        RiskEngine eng = engine();
        long maxU64 = -1L; // 18446744073709551615 as u64
        expect(eng, new OrderRequest(maxU64, 1, 0, 10, 2450,
                OrderRequest.LIMIT, 1, "S1", 0.5, TS + SEC), Rules.ALLOW);
        RiskDecision d = eng.checkOrder(new OrderRequest(maxU64, 1, 0, 10,
                2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + 2 * SEC));
        assertEquals(Rules.DUPLICATE_ORDER_ID, d.ruleId());
        assertEquals("order_id 18446744073709551615 already used at ts "
                + (TS + SEC), d.reason());
    }

    @Test
    public void instrumentKillScopeIdBoundToU32() {
        // The Rust reference parses the INSTRUMENT scope id with
        // parse::<u32>(); an id outside that domain does not address an
        // instrument and throws instead of silently doing nothing.
        RiskEngine eng = engine();
        // u32::MAX engages (kill checks precede reference-data checks).
        eng.engageKill(Scope.INSTRUMENT, "4294967295", TS, "ops");
        RiskDecision d = eng.checkOrder(new OrderRequest(1, 4294967295L, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + SEC));
        assertEquals(Rules.KILL_INSTRUMENT, d.ruleId());
        // u32::MAX + 1 parses as a long but is out of u32 range, and a
        // negative id does not parse at all: both fail closed.
        for (String bad : new String[] {"4294967296", "-1", "", "1.0"}) {
            expectIae(() -> eng.engageKill(Scope.INSTRUMENT, bad, TS, "ops"),
                    "escalated to GLOBAL");
            eng.clearKill(Scope.GLOBAL, "", TS, "escalation reviewed");
        }
        RiskDecision d2 = eng.checkOrder(new OrderRequest(2, 4294967296L, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + 2 * SEC));
        assertEquals(Rules.UNKNOWN_INSTRUMENT, d2.ruleId()); // not killed
        // In-range clears still work.
        eng.clearKill(Scope.INSTRUMENT, "4294967295", TS + 4 * SEC, "clear");
        RiskDecision d4 = eng.checkOrder(new OrderRequest(4, 4294967295L, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + 5 * SEC));
        assertEquals(Rules.UNKNOWN_INSTRUMENT, d4.ruleId()); // kill lifted
    }

    /** Assert the runnable throws IllegalArgumentException mentioning {@code needle}. */
    private static void expectIae(Runnable r, String needle) {
        try {
            r.run();
            org.junit.Assert.fail("expected IllegalArgumentException: " + needle);
        } catch (IllegalArgumentException e) {
            assertTrue(String.valueOf(e.getMessage()),
                    String.valueOf(e.getMessage()).contains(needle));
        }
    }

    /**
     * Regression — FAIL-OPEN defect: an unvaluable OPEN ORDER used to be
     * skipped in the gross/net loop, so live working exposure vanished from
     * the aggregate and a correct GROSS_NOTIONAL reject became an ALLOW.
     * Five working MARKET children (9,000 @ mid 100.01 = 900,090 each,
     * 4,500,450 gross) then instrument 1's book goes one-sided (halt/open):
     * the two children on instrument 1 are 1,800,180 of real exposure.
     */
    @Test
    public void unvaluableOpenOrderRejectsInsteadOfVanishingFromGross() {
        long[][] plan = {{1, 1, 0}, {2, 1, 0}, {3, 2, 1}, {4, 2, 1}, {5, 3, 0}};
        RiskEngine[] built = new RiskEngine[2];
        long ts = 0;
        for (int b = 0; b < 2; b++) {
            TreeMap<Long, Double> ticks = new TreeMap<>();
            ticks.put(1L, 0.01);
            ticks.put(2L, 0.01);
            ticks.put(3L, 0.01);
            RiskEngine eng = RiskEngine.fromConfigTicks(configDoc(), ticks);
            for (long iid : new long[] {1, 2, 3}) {
                eng.onMarket(iid, 10_000, 10_002, TS); // mid 100.01
            }
            ts = TS + 100_000_000L;
            // buys on 1, sells on 2, buy on 3 keeps |net| under the net cap
            for (long[] p : plan) {
                RiskDecision c = eng.checkOrder(new OrderRequest(p[0], p[1],
                        (int) p[2], 9_000, 0, OrderRequest.MARKET, 1, "S1", 0.5, ts));
                assertEquals("child " + p[0] + " must rest (" + c.reason() + ")",
                        Rules.ALLOW, c.ruleId());
                ts += 100_000_000L;
            }
            built[b] = eng;
        }
        OrderRequest sixth = new OrderRequest(6, 3, 0, 9_000, 0,
                OrderRequest.MARKET, 1, "S1", 0.5, ts + 100_000_000L);
        RiskDecision healthy = built[0].checkOrder(sixth);
        assertEquals(Rules.GROSS_NOTIONAL, healthy.ruleId());
        assertEquals("projected gross notional 5400540.00 exceeds "
                + "max_gross_notional 5000000.00", healthy.reason());

        built[1].onMarket(1, 10_000, 0, ts); // one-sided: instrument 1 has no mid
        RiskDecision degraded = built[1].checkOrder(sixth);
        assertEquals(degraded.reason(), Rules.GROSS_NOTIONAL, degraded.ruleId());
        assertEquals(Severity.WARN, degraded.severity());
        assertEquals("open order 1 in instrument 1 has no mark price (fail-closed)",
                degraded.reason());
    }

    /**
     * Regression — SILENT NO-OP defect: an INSTRUMENT kill sent as a ticker
     * used to emit KILL_SWITCH_ENGAGED and halt nothing. It must now fail
     * loudly, emit MALFORMED_KILL (never a success record) and escalate to
     * the GLOBAL kill.
     */
    @Test
    public void unparseableKillScopeIdEscalatesAndNeverLooksSuccessful() {
        RiskEngine eng = engine();
        expectIae(() -> eng.engageKill(Scope.INSTRUMENT, "AAPL", TS, "ops halt"),
                "escalated to GLOBAL");
        // the very next order is stopped, not allowed
        assertEquals(Rules.KILL_GLOBAL,
                eng.checkOrder(limitBuy(1, 100, 2450, TS + SEC)).ruleId());
        assertEquals(1, eng.metrics.counterValue("risk_malformed_kills_total"));
        int malformed = 0;
        for (com.iap.risk.RiskEvent e : eng.audit()) {
            assertFalse("a phantom halt must never leave a success record",
                    Rules.KILL_SWITCH_ENGAGED.equals(e.ruleId()));
            if (Rules.MALFORMED_KILL.equals(e.ruleId())) {
                malformed++;
                assertEquals(Decision.KILL.code(), e.decision());
                assertEquals(Severity.BREACH.code(), e.severity());
                assertEquals("kill scope id \"AAPL\" is not a valid INSTRUMENT id: "
                        + "escalated to GLOBAL (fail-closed): ops halt", e.reason());
            }
        }
        assertEquals(1, malformed);

        // a VENUE id above u16 is the same defect
        RiskEngine venue = engine();
        expectIae(() -> venue.engageKill(Scope.VENUE, "65536", TS, "ops"),
                "escalated to GLOBAL");
        assertEquals(Rules.KILL_GLOBAL,
                venue.checkOrder(limitBuy(1, 100, 2450, TS + SEC)).ruleId());

        // clearing is the permissive direction: it clears NOTHING and reports
        RiskEngine held = engine();
        held.engageKill(Scope.INSTRUMENT, "1", TS, "halt");
        expectIae(() -> held.clearKill(Scope.INSTRUMENT, "AAPL", TS + SEC, "ops clear"),
                "nothing cleared");
        assertEquals("the real halt must still be in force", Rules.KILL_INSTRUMENT,
                held.checkOrder(limitBuy(2, 100, 2450, TS + 2 * SEC)).ruleId());
        for (com.iap.risk.RiskEvent e : held.audit()) {
            assertFalse("nothing was cleared, so nothing may claim it was",
                    Rules.KILL_SWITCH_CLEARED.equals(e.ruleId()));
        }
    }

    /**
     * Regression — FAIL-OPEN defect: an unmarked held lot used to contribute
     * zero unrealized P&amp;L instead of making the daily total
     * undeterminable, so a loss limit could fail to trip. It must behave
     * exactly like the missing-FX-rate branch: null, and orders reject
     * FX_RATE_MISSING.
     */
    @Test
    public void unmarkedHeldLotMakesDailyPnlUndeterminable() {
        RiskEngine eng = engine();
        // S1 long 1,000 and S2 short 1,000 of instrument 2: two HELD lots,
        // but a flat firm position, so check 19 cannot mask the P&L path.
        assertTrue(eng.onFill(new RiskFill(TS + SEC, "S1", 2, 0, 0, 1_000, 3_120)));
        assertTrue(eng.onFill(new RiskFill(TS + SEC, "S2", 2, 0, 1, 1_000, 3_120)));
        assertEquals(0L, eng.position(2));
        org.junit.Assert.assertNotNull(eng.globalDailyPnl());
        org.junit.Assert.assertNotNull(eng.strategyDailyPnl("S1"));

        eng.onMarket(2, 3_119, 0, TS + 2 * SEC); // instrument 2 goes one-sided
        org.junit.Assert.assertNull(eng.globalDailyPnl());
        org.junit.Assert.assertNull(eng.strategyDailyPnl("S1"));

        // fail-closed pre-trade, exactly like a missing conversion rate
        RiskDecision d = eng.checkOrder(limitBuy(9, 100, 2450, TS + 3 * SEC));
        assertEquals(d.reason(), Rules.FX_RATE_MISSING, d.ruleId());
        assertEquals("global daily pnl undeterminable: conversion rate missing",
                d.reason());
    }

    @Test
    public void riskEventJsonEscapesLikeSerdeJson() {
        // \b (0x08) and \f (0x0c) must serialize as the two-character
        // escapes serde_json emits, not as u00xx escapes; other control
        // characters keep the lowercase u00xx form.
        com.iap.risk.RiskEvent ev = new com.iap.risk.RiskEvent(7,
                Scope.GLOBAL, "", Rules.KILL_SWITCH_ENGAGED, 3, 3,
                "a\bb\fc\nd\re\tf\u0001g\"h\\i");
        assertEquals("{\"decision\":3,"
                + "\"reason\":\"a\\bb\\fc\\nd\\re\\tf\\u0001g\\\"h\\\\i\","
                + "\"rule_id\":\"KILL_SWITCH_ENGAGED\","
                + "\"scope\":\"GLOBAL\",\"scope_id\":\"\","
                + "\"severity\":3,\"timestamp\":7}", ev.toJsonLine());
    }

    // -------------------------------------------- fail-closed review fixes

    private static OrderRequest buyOn(long id, long iid, long px, int venue,
            long ts) {
        return new OrderRequest(id, iid, 0, 100, px, OrderRequest.LIMIT, venue,
                "S1", 0.5, ts);
    }

    /**
     * FAIL-OPEN regression: a mark stamped AFTER the order had a negative
     * age and was never stale (and every genuine update behind it is
     * dropped as a regression). Beyond the stale window ahead of the
     * engine's event clock it rejects with STALE_PRICE.
     */
    @Test
    public void futureStampedMarketDataFailsClosed() {
        RiskEngine eng = engine();
        long t = TS + 100_000_000L;
        eng.onMarket(1, 2450, 2452, TS + 3600 * SEC);
        RiskDecision d = eng.checkOrder(limitBuy(1, 100, 2450, t));
        assertEquals(Decision.REJECT, d.decision());
        assertEquals(Rules.STALE_PRICE, d.ruleId());
        assertEquals("reference price timestamp " + (TS + 3600 * SEC)
                + " is more than 5000000000ns ahead of the latest order event time "
                + t, d.reason());
        // the genuine update is behind the poisoned state: dropped, still closed
        eng.onMarket(1, 2450, 2452, t);
        assertEquals(1, eng.metrics.counterValue(
                "risk_market_regressions_dropped_total"));
        expect(eng, limitBuy(2, 100, 2450, t), Rules.STALE_PRICE);
        // boundary: exactly the window ahead is trusted, one ns more is not
        eng.onMarket(2, 3119, 3121, t + 5 * SEC);
        expect(eng, buyOn(3, 2, 3120, 1, t), Rules.ALLOW);
        eng.onMarket(2, 3119, 3121, t + 5 * SEC + 1);
        expect(eng, buyOn(4, 2, 3120, 1, t), Rules.STALE_PRICE);
        // the engine clock, not the order's own (regressed) timestamp,
        // decides: an order stamped 10s behind an order already seen is not
        // "future data"
        RiskEngine eng2 = engine();
        expect(eng2, limitBuy(5, 10, 2450, t), Rules.ALLOW);
        expect(eng2, limitBuy(6, 10, 2450, t - 10 * SEC), Rules.ALLOW);
    }

    @Test
    public void futureStampedConversionRateFailsClosed() {
        TreeMap<Long, InstrumentRef> refs = new TreeMap<>();
        refs.put(1L, InstrumentRef.equity(0.01));
        refs.put(102L, new InstrumentRef(1e-5, 1000.0, "USD"));
        refs.put(108L, new InstrumentRef(1e-5, 1000.0, "GBP"));
        RiskEngine eng = RiskEngine.fromConfig(configDoc(), refs);
        long t = TS + 100_000_000L;
        eng.onMarket(108, 85_315, 85_325, t);
        eng.onMarket(102, 127_335, 127_345, t + 3600 * SEC);
        RiskDecision d = eng.checkOrder(new OrderRequest(1, 108, 0, 100, 0,
                OrderRequest.MARKET, 1, "S1", 0.5, t));
        assertEquals(Rules.FX_RATE_MISSING, d.ruleId());
        assertEquals("conversion rate GBP -> USD timestamp " + (t + 3600 * SEC)
                + " is more than 5000000000ns ahead of the latest order event time "
                + t, d.reason());
        // Found by the differential fuzzer (as a fail-open in the Rust and
        // Python engines; this port was right): a pair stamped exactly
        // i64::MAX is the extreme future-stamped mark and rejects the same.
        eng = RiskEngine.fromConfig(configDoc(), refs);
        eng.onMarket(108, 85_315, 85_325, t);
        eng.onMarket(102, 127_335, 127_345, Long.MAX_VALUE);
        d = eng.checkOrder(new OrderRequest(2, 108, 0, 100, 0,
                OrderRequest.MARKET, 1, "S1", 0.5, t));
        assertEquals(Rules.FX_RATE_MISSING, d.ruleId());
        assertEquals("conversion rate GBP -> USD timestamp " + Long.MAX_VALUE
                + " is more than 5000000000ns ahead of the latest order event time "
                + t, d.reason());
    }

    /** The repo limits with one double limit replaced by NaN (0-based slot). */
    private static RiskLimits nanLimit(int slot) {
        RiskLimits l = RiskLimits.fromJson(configDoc());
        double nan = Double.NaN;
        return new RiskLimits(l.killSwitchEngaged(),
                slot == 0 ? nan : l.maxGrossNotional(),
                slot == 1 ? nan : l.maxNetNotional(),
                slot == 2 ? nan : l.maxDailyLoss(),
                l.maxOrderRatePerSec(),
                slot == 3 ? nan : l.orderRateBurst(),
                l.maxOrderQty(),
                slot == 4 ? nan : l.maxOrderNotional(),
                slot == 5 ? nan : l.priceBandBps(),
                l.staleBookReject(), l.duplicateOrderWindowNs(),
                l.maxPositionQty(),
                slot == 6 ? nan : l.maxInstrumentNotional(),
                slot == 7 ? nan : l.strategyMaxDailyLoss(),
                l.maxSequenceGapBeforeHalt(), l.staleFeedTimeoutNs(),
                l.reportingCcy(), l.fxConversion());
    }

    /**
     * {@code x > NaN} is false: a NaN limit used to ALLOW. Every double
     * limit comparison is {@code !(x <= limit)}, so NaN rejects; and
     * reference data must be finite as well as positive.
     */
    @Test
    public void nanLimitOrReferenceDataNeverPassesACheck() {
        String[] rules = {Rules.GROSS_NOTIONAL, Rules.NET_NOTIONAL,
            Rules.DAILY_LOSS, Rules.RATE_THROTTLE, Rules.FAT_FINGER_NOTIONAL,
            Rules.PRICE_BAND, Rules.INSTRUMENT_NOTIONAL, Rules.STRATEGY_LOSS};
        for (int slot = 0; slot < rules.length; slot++) {
            TreeMap<Long, Double> ticks = new TreeMap<>();
            ticks.put(1L, 0.01);
            RiskEngine eng = new RiskEngine(nanLimit(slot),
                    RiskEngine.equityRefs(ticks));
            eng.onMarket(1, 2450, 2452, TS);
            expect(eng, limitBuy(1, 100, 2450, TS + 1), rules[slot]);
        }
        for (double bad : new double[] {Double.NaN, Double.POSITIVE_INFINITY,
            0.0, -1.0}) {
            expectIae(() -> new InstrumentRef(bad, 1.0, "USD"),
                    "must be finite and > 0");
            expectIae(() -> new InstrumentRef(0.01, bad, "USD"),
                    "must be finite and > 0");
        }
    }

    @Test
    public void positionOverflowRejectsOrdersAndKillsOnFills() {
        long max = Long.MAX_VALUE;
        // checkOrder: the projection leaves i64 -> MALFORMED_ORDER reject
        RiskEngine eng = engine();
        assertTrue(eng.onFill(new RiskFill(TS + SEC, "S1", 1, 0, 0, max, 2451)));
        assertFalse(eng.killSwitchEngaged());
        RiskDecision d = eng.checkOrder(limitBuy(1, 100, 2450, TS + 1));
        assertEquals(Decision.REJECT, d.decision());
        assertEquals(Rules.MALFORMED_ORDER, d.ruleId());
        assertEquals(Severity.WARN, d.severity());
        assertEquals("projected position overflows i64 (fail-closed)", d.reason());
        // onFill: nothing applied, GLOBAL kill latched through the kill path
        String before = eng.snapshot();
        int n = eng.audit().size();
        assertFalse(eng.onFill(new RiskFill(TS + SEC, "S1", 1, 7, 0, 1, 2451)));
        assertTrue(eng.killSwitchEngaged());
        assertEquals(max, eng.position(1));
        assertEquals(n + 1, eng.audit().size());
        com.iap.risk.RiskEvent ev = eng.audit().get(n);
        assertEquals(Rules.KILL_SWITCH_ENGAGED, ev.ruleId());
        assertEquals(Scope.GLOBAL, ev.scope());
        assertEquals(Decision.KILL.code(), ev.decision());
        assertEquals("fill for order 7 overflows i64 position accounting "
                + "(fail-closed)", ev.reason());
        assertEquals(before.replace("\"kill_global\":false", "\"kill_global\":true"),
                eng.snapshot());
        expect(eng, new OrderRequest(2, 1, 1, 100, 2452, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 2), Rules.KILL_GLOBAL);
        // the short side: -i64::MAX is the floor of the symmetric domain
        RiskEngine eng2 = engine();
        assertTrue(eng2.onFill(new RiskFill(TS + SEC, "S1", 1, 0, 1, max, 2451)));
        expect(eng2, new OrderRequest(3, 1, 1, 100, 2452, OrderRequest.LIMIT, 1,
                "S1", 0.5, TS + 1), Rules.MALFORMED_ORDER);
        assertFalse(eng2.onFill(new RiskFill(TS + SEC, "S1", 1, 0, 1, 1, 2451)));
        assertTrue(eng2.killSwitchEngaged());
        assertEquals(-max, eng2.position(1));
        // another strategy's lot is fine, the AGGREGATE position overflows
        RiskEngine eng3 = engine();
        assertTrue(eng3.onFill(new RiskFill(TS + SEC, "S1", 1, 0, 0, max, 2451)));
        assertFalse(eng3.onFill(new RiskFill(TS + SEC, "S2", 1, 0, 0, 1, 2451)));
        assertTrue(eng3.killSwitchEngaged());
        // a mark whose bid + ask leaves i64 is no mark at all
        RiskEngine eng4 = engine();
        eng4.onMarket(1, max, max, TS + 1);
        RiskDecision d4 = eng4.checkOrder(limitBuy(4, 100, 2450, TS + 2));
        assertEquals(Rules.STALE_PRICE, d4.ruleId());
        assertEquals("no reference price for instrument 1", d4.reason());
        assertTrue(eng4.onFill(new RiskFill(TS + SEC, "S1", 1, 0, 0, 1, 1)));
    }

    @Test
    public void sorOrderRejectedWhileAnyVenueKillIsEngaged() {
        RiskEngine eng = engine();
        eng.engageKill(Scope.VENUE, "7", TS, "halt");
        eng.engageKill(Scope.VENUE, "3", TS, "halt");
        expect(eng, limitBuy(1, 100, 2450, TS + 1), Rules.ALLOW); // venue 1 untouched
        eng.onOrderDone(1);
        RiskDecision d = eng.checkOrder(buyOn(2, 1, 2450, 0, TS + 2));
        assertEquals(Rules.KILL_VENUE, d.ruleId());
        assertEquals(Severity.BREACH, d.severity());
        assertEquals("venue 0 (SOR) order rejected: venue 3 kill switch engaged",
                d.reason());
        com.iap.risk.RiskEvent ev = eng.audit().get(eng.audit().size() - 1);
        assertEquals(Scope.VENUE, ev.scope());
        assertEquals("0", ev.scopeId());
        eng.clearKill(Scope.VENUE, "3", TS, "clear");
        assertEquals("venue 0 (SOR) order rejected: venue 7 kill switch engaged",
                eng.checkOrder(buyOn(3, 1, 2450, 0, TS + 3)).reason());
        eng.clearKill(Scope.VENUE, "7", TS, "clear");
        expect(eng, buyOn(4, 1, 2450, 0, TS + 4), Rules.ALLOW);
        // disconnects: the router stays open while any known venue is up
        // and closes when EVERY known venue is down
        eng.onOrderDone(4);
        eng.onVenueDisconnect(3, TS);
        d = eng.checkOrder(buyOn(5, 1, 2450, 0, TS + 5));
        assertEquals(Rules.VENUE_DISCONNECTED, d.ruleId());
        assertEquals(Severity.WARN, d.severity());
        assertEquals("venue 0 (SOR) order rejected: every known venue is "
                + "disconnected", d.reason());
        assertEquals("0", eng.audit().get(eng.audit().size() - 1).scopeId());
        eng.onVenueReconnect(5, TS);
        expect(eng, buyOn(6, 1, 2450, 0, TS + 6), Rules.ALLOW);
        eng.onOrderDone(6);
        eng.onVenueDisconnect(5, TS);
        expect(eng, buyOn(7, 1, 2450, 0, TS + 7), Rules.VENUE_DISCONNECTED);
        eng.onVenueReconnect(3, TS);
        expect(eng, buyOn(8, 1, 2450, 0, TS + 8), Rules.ALLOW);
    }

    /**
     * Kill scope ids follow Rust {@code u16::from_str} / {@code u32::from_str}:
     * an optional single {@code +}, ASCII digits only — {@code Integer.parseInt}
     * accepted {@code -0} and non-ASCII digits the reference rejects.
     */
    @Test
    public void killScopeIdGrammarIsRustFromStr() {
        RiskEngine eng = engine();
        eng.engageKill(Scope.VENUE, "+1", TS, "plus form");
        expect(eng, limitBuy(1, 100, 2450, TS + 1), Rules.KILL_VENUE);
        eng.clearKill(Scope.VENUE, "01", TS, "leading zero");
        expect(eng, limitBuy(2, 100, 2450, TS + 2), Rules.ALLOW);
        for (Scope scope : new Scope[] {Scope.VENUE, Scope.INSTRUMENT}) {
            for (String bad : new String[] {"-0", "+", "", "++1", " 1",
                "١", "１", "4294967296"}) {
                expectIae(() -> eng.engageKill(scope, bad, TS, "ops"),
                        "escalated to GLOBAL");
                eng.clearKill(Scope.GLOBAL, "", TS, "escalation reviewed");
            }
        }
        expectIae(() -> eng.engageKill(Scope.VENUE, "65536", TS, "ops"),
                "escalated to GLOBAL");
    }

    /** Rust {@code {}} of an f64 in the urgency reason, not Java's. */
    @Test
    public void urgencyReasonUsesRustFloatDisplay() {
        assertEquals("2", OrderRequest.rustDisplay(2.0));
        assertEquals("-5", OrderRequest.rustDisplay(-5.0));
        assertEquals("0.5", OrderRequest.rustDisplay(0.5));
        assertEquals("1.5", OrderRequest.rustDisplay(1.5));
        assertEquals("-0", OrderRequest.rustDisplay(-0.0));
        assertEquals("0", OrderRequest.rustDisplay(0.0));
        assertEquals("0.0000001", OrderRequest.rustDisplay(1e-7));
        assertEquals("1000000000000000000000", OrderRequest.rustDisplay(1e21));
        assertEquals("123456789.125", OrderRequest.rustDisplay(123456789.125));
        assertEquals("1.0000000000000002", OrderRequest.rustDisplay(1.0000000000000002));
        assertEquals("0.00002", OrderRequest.rustDisplay(2e-5));
        assertEquals("100000000000000000000000", OrderRequest.rustDisplay(1e23));
        // Found by the differential fuzzer: the smallest subnormals. One
        // significant digit round-trips; Double.toString prints two
        // ("4.9E-324", "9.9E-324"), the reference prints "5e-324", "1e-323".
        String zeros = "0".repeat(323);
        assertEquals("0." + zeros + "5", OrderRequest.rustDisplay(Double.MIN_VALUE));
        assertEquals("-0." + zeros + "5", OrderRequest.rustDisplay(-Double.MIN_VALUE));
        assertEquals("0." + "0".repeat(322) + "1",
                OrderRequest.rustDisplay(2 * Double.MIN_VALUE));
        assertEquals("0." + "0".repeat(322) + "15",
                OrderRequest.rustDisplay(3 * Double.MIN_VALUE));
        assertEquals("NaN", OrderRequest.rustDisplay(Double.NaN));
        assertEquals("inf", OrderRequest.rustDisplay(Double.POSITIVE_INFINITY));
        assertEquals("-inf", OrderRequest.rustDisplay(Double.NEGATIVE_INFINITY));
        assertEquals("urgency must be in [0, 1]: 2", new OrderRequest(1, 1, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 2.0, TS).validationError());
        assertEquals("urgency must be in [0, 1]: inf", new OrderRequest(1, 1, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1",
                Double.POSITIVE_INFINITY, TS).validationError());
        assertEquals("urgency must be in [0, 1]: NaN", new OrderRequest(1, 1, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", Double.NaN,
                TS).validationError());
    }

    private static RiskEngine unmarked(RiskLimits limits) {
        TreeMap<Long, Double> ticks = new TreeMap<>();
        ticks.put(1L, 0.01);
        ticks.put(2L, 0.01);
        return new RiskEngine(limits, RiskEngine.equityRefs(ticks));
    }

    /**
     * i64 MIN / MAX timestamps on orders, market updates and fills: every
     * timestamp difference is checked, an overflow is a MALFORMED_ORDER
     * reject (never a wrap) and state-in paths apply cleanly.
     */
    @Test
    public void extremeTimestampsFailClosedWithoutOverflow() {
        String why = "timestamp arithmetic overflows i64 (fail-closed)";
        long min = Long.MIN_VALUE;
        long max = Long.MAX_VALUE;
        RiskLimits limits = RiskLimits.fromJson(configDoc());
        // orders: age vs a mark at TS
        RiskEngine eng = engine();
        RiskDecision d = eng.checkOrder(buyOn(1, 1, 2450, 1, min));
        assertEquals(Decision.REJECT, d.decision());
        assertEquals(Rules.MALFORMED_ORDER, d.ruleId());
        assertEquals(Severity.WARN, d.severity());
        assertEquals(why, d.reason());
        expect(eng, buyOn(2, 1, 2450, 1, max), Rules.STALE_PRICE);
        // market updates: stored as given, the checks reject
        eng = unmarked(limits);
        eng.onMarket(1, 2450, 2452, min);
        d = eng.checkOrder(buyOn(3, 1, 2450, 1, TS));
        assertEquals(Rules.MALFORMED_ORDER, d.ruleId());
        assertEquals(why, d.reason());
        expect(eng, buyOn(4, 1, 2450, 1, min), Rules.ALLOW); // age 0
        eng.onMarket(2, 3119, 3121, max);
        expect(eng, buyOn(5, 2, 3120, 1, TS), Rules.STALE_PRICE);
        // fills: the timestamp only stamps the audit
        assertTrue(eng.onFill(new RiskFill(min, "S1", 1, 0, 0, 1, 2451)));
        assertTrue(eng.onFill(new RiskFill(max, "S1", 1, 0, 0, 1, 2451)));
        assertEquals(2, eng.position(1));
        assertFalse(eng.killSwitchEngaged());
        // throttle elapsed: the bucket clock is at TS, the order at MIN + 10
        eng = unmarked(limits);
        eng.onMarket(1, 2450, 2452, TS);
        expect(eng, buyOn(6, 1, 2450, 1, TS), Rules.ALLOW);
        eng.onMarket(2, 3119, 3121, min + 10);
        d = eng.checkOrder(buyOn(7, 2, 3120, 1, min + 10));
        assertEquals(Rules.MALFORMED_ORDER, d.ruleId());
        assertEquals(why, d.reason());
        expect(eng, buyOn(10, 1, 2450, 1, TS + 1), Rules.ALLOW);
        // duplicate window: the id's age and the prune cutoff
        RiskLimits l = limits;
        eng = unmarked(new RiskLimits(l.killSwitchEngaged(),
                l.maxGrossNotional(), l.maxNetNotional(), l.maxDailyLoss(),
                l.maxOrderRatePerSec(), l.orderRateBurst(), l.maxOrderQty(),
                l.maxOrderNotional(), l.priceBandBps(), l.staleBookReject(),
                10L, l.maxPositionQty(), l.maxInstrumentNotional(),
                l.strategyMaxDailyLoss(), l.maxSequenceGapBeforeHalt(),
                l.staleFeedTimeoutNs(), l.reportingCcy(), l.fxConversion()));
        eng.onMarket(1, 2450, 2452, TS);
        expect(eng, buyOn(8, 1, 2450, 1, TS), Rules.ALLOW);
        assertEquals(why, eng.checkOrder(buyOn(8, 1, 2450, 1, min)).reason());
        assertEquals(why, eng.checkOrder(buyOn(9, 1, 2450, 1, min)).reason());
    }

    /**
     * Found by the differential fuzzer (tests/golden/risk_fuzz, the
     * {@code unicode} scripts): strategy ids are ordered by code point, as
     * the reference's {@code BTreeMap<String, _>} orders them, not by UTF-16
     * code unit. U+FFEE sorts BEFORE U+1F600 by code point and AFTER it by
     * code unit (the astral character is the surrogate pair D83D DE00), so
     * when one mark breaches both strategies the two latches — and the
     * snapshot's lots — come out in the reference's order.
     */
    @Test
    public void strategyIdsAreOrderedByCodePointLikeTheReference() {
        String bmp = String.valueOf((char) 0xFFEE);
        String astral = new String(Character.toChars(0x1F600));
        assertTrue("UTF-16 code units order the astral id first",
                astral.compareTo(bmp) < 0);
        RiskEngine eng = engine();
        assertTrue(eng.onFill(new RiskFill(TS, astral, 1, 0, 0, 100_000, 2451)));
        assertTrue(eng.onFill(new RiskFill(TS, bmp, 1, 0, 0, 100_000, 2451)));
        int before = eng.audit().size();
        eng.onMarket(1, 2350, 2352, TS + 1); // -1.00 x 100,000 for each strategy
        assertEquals("STRATEGY_LOSS", eng.audit().get(before).ruleId());
        assertEquals(bmp, eng.audit().get(before).scopeId());
        assertEquals("STRATEGY_LOSS", eng.audit().get(before + 1).ruleId());
        assertEquals(astral, eng.audit().get(before + 1).scopeId());
        String snap = eng.snapshot();
        assertTrue("lots are listed in code-point order",
                snap.indexOf("\"strategy_id\":\"" + bmp + "\"")
                        < snap.indexOf("\"strategy_id\":\"" + astral + "\""));
    }

    /**
     * Found by the differential fuzzer (tests/golden/risk_fuzz, the
     * {@code config} scripts): the CONFIG_MISSING reason is audit output, so
     * a fail-closed engine must name the same first offending key, in the
     * same words, as the reference parser.
     */
    @Test
    public void configErrorsNameTheFirstOffendingKeyLikeTheReference() {
        String[][] cases = {
            // {section, key, JSON value or null to remove the key, reason}
            {"global", "max_gross_notional", "0",
                "risk.json: global.max_gross_notional must be > 0, got 0"},
            {"global", "max_daily_loss", "-0.0",
                "risk.json: global.max_daily_loss must be > 0, got -0"},
            {"per_order", "price_band_bps", "-1e21",
                "risk.json: per_order.price_band_bps must be > 0, got "
                        + "-1000000000000000000000"},
            {"global", null, null,
                "risk.json: missing/non-bool global.kill_switch_engaged"},
            {"per_order", null, null,
                "risk.json: missing per_order.duplicate_order_window_ns"},
            {"currency", null, null,
                "risk.json: missing/empty currency.reporting_ccy"},
            {"currency", "conversion", "{\"EUR\":{\"instrument_id\":0,\"invert\":false}}",
                "risk.json: currency.conversion.EUR.instrument_id out of u32 range"},
            {"currency", "conversion", "{\"EUR\":101}",
                "risk.json: currency.conversion.EUR.instrument_id missing/invalid"},
            {"currency", "conversion",
                "{\"ZZZ\":{\"invert\":true},\"AAA\":{\"instrument_id\":101}}",
                "risk.json: currency.conversion.AAA.invert missing/non-bool"},
        };
        for (String[] c : cases) {
            Map<String, Object> doc = configDoc();
            if (c[1] == null) {
                doc.remove(c[0]);
            } else {
                Json.object(doc.get(c[0])).put(c[1], Json.parse(c[2]));
            }
            RiskEngine eng = RiskEngine.fromConfig(doc, new TreeMap<>());
            RiskDecision d = eng.checkOrder(limitBuy(1, 10, 2450, TS));
            assertEquals(Rules.CONFIG_MISSING, d.ruleId());
            assertEquals("fail-closed: invalid argument: " + c[3], d.reason());
        }
        // two offending keys: the reference's parse order decides, and the
        // sequence-gap threshold is read AFTER the per-strategy loss limit
        Map<String, Object> doc = configDoc();
        Json.object(doc.get("market_data")).put("max_sequence_gap_before_halt", -1L);
        Json.object(doc.get("per_strategy")).put("max_daily_loss", -5.5);
        assertEquals("fail-closed: invalid argument: risk.json: "
                + "per_strategy.max_daily_loss must be > 0, got -5.5",
                RiskEngine.fromConfig(doc, new TreeMap<>())
                        .checkOrder(limitBuy(1, 10, 2450, TS)).reason());
    }
}
