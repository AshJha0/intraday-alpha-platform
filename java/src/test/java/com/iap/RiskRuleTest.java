package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.risk.Decision;
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
                java.nio.file.Paths.get("..", "configs", "risk.json")));
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
        // venue 0 = SOR-routed, connectivity check skipped
        expect(eng, new OrderRequest(2, 1, 0, 10, 2450, OrderRequest.LIMIT, 0,
                "S1", 0.5, TS + 3), Rules.ALLOW);
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
        // parse::<u32>(): out-of-range or unparseable ids are a no-op.
        RiskEngine eng = engine();
        // u32::MAX engages (kill checks precede reference-data checks).
        eng.engageKill(Scope.INSTRUMENT, "4294967295", TS, "ops");
        RiskDecision d = eng.checkOrder(new OrderRequest(1, 4294967295L, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + SEC));
        assertEquals(Rules.KILL_INSTRUMENT, d.ruleId());
        // u32::MAX + 1 parses as a long but is out of u32 range: no-op.
        eng.engageKill(Scope.INSTRUMENT, "4294967296", TS, "ops");
        RiskDecision d2 = eng.checkOrder(new OrderRequest(2, 4294967296L, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + 2 * SEC));
        assertEquals(Rules.UNKNOWN_INSTRUMENT, d2.ruleId()); // not killed
        // Negative ids are a no-op too.
        eng.engageKill(Scope.INSTRUMENT, "-1", TS, "ops");
        RiskDecision d3 = eng.checkOrder(new OrderRequest(3, -1L, 0, 10,
                2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + 3 * SEC));
        assertEquals(Rules.UNKNOWN_INSTRUMENT, d3.ruleId()); // not killed
        // In-range clears still work.
        eng.clearKill(Scope.INSTRUMENT, "4294967295", TS + 4 * SEC, "clear");
        RiskDecision d4 = eng.checkOrder(new OrderRequest(4, 4294967295L, 0,
                10, 2450, OrderRequest.LIMIT, 1, "S1", 0.5, TS + 5 * SEC));
        assertEquals(Rules.UNKNOWN_INSTRUMENT, d4.ruleId()); // kill lifted
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
}
