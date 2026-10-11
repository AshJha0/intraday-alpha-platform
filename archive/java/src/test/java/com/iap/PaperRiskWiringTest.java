package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.backtest.BacktestEngine;
import com.iap.config.ConfigService;
import com.iap.core.MarketEvent;
import com.iap.execution.ExecConfig;
import com.iap.execution.Fill;
import com.iap.execution.LatencyConfig;
import com.iap.monitoring.MetricsRegistry;
import com.iap.platform.PaperTrading;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskEvent;
import com.iap.risk.Rules;
import com.iap.sor.SorOptions;

/**
 * The production risk wiring ({@link PaperTrading.RiskWiring}) on the
 * golden EQ vector: the stale-price gate fires on a feed stall, the risk
 * engine's position never lags the account, terminal children release
 * their open-order slots and sequence gaps close the gap gate.
 */
public class PaperRiskWiringTest {
    private static final long SEC = 1_000_000_000L;

    private static ConfigService cfg() {
        return new ConfigService(Paths.get("..", "configs"));
    }

    private static ExecConfig exec(ConfigService cfg) {
        return new ExecConfig(LatencyConfig.DEFAULT, cfg.executionSeed(),
                cfg.impactCoeffBpsPerPctAdv(), cfg.instruments(), cfg.venues());
    }

    private static MarketEvent shifted(MarketEvent e, long dt) {
        return new MarketEvent(e.eventId, e.instrumentId, e.venueId,
                e.exchangeTs + dt, e.receiveTs + dt, e.sequence, e.eventType,
                e.side, e.priceTicks, e.qty, e.orderId, e.tradeId);
    }

    /**
     * Scenario: XV1 stalls for 6 s (heartbeats only), then resumes. Every
     * decision during the stall is checked against a frozen mark stamped
     * with the last top-of-book time: STALE_PRICE, zero fills in the gap,
     * trading resumes after the first post-gap quote.
     */
    @Test
    public void paperStaleFeedRejectsOrders() {
        ConfigService cfg = cfg();
        List<MarketEvent> golden = Golden.eq();
        int k = 400;
        List<MarketEvent> evs = new ArrayList<>(golden.subList(0, k));
        MarketEvent last = golden.get(k - 1);
        long gapStart = last.exchangeTs;
        long seq = last.sequence;
        for (int i = 0; i < 5; i++) {
            long ts = gapStart + 6 * SEC + i * 100_000_000L;
            evs.add(new MarketEvent(last.eventId + 1 + i, last.instrumentId,
                    last.venueId, ts, ts, ++seq, 9, 0, 0, 0, 0, 0));
        }
        long shift = 7 * SEC;
        long gapEnd = golden.get(k).exchangeTs + shift;
        for (int i = k; i < golden.size(); i++) {
            MarketEvent e = golden.get(i);
            evs.add(new MarketEvent(e.eventId + 5, e.instrumentId, e.venueId,
                    e.exchangeTs + shift, e.receiveTs + shift, e.sequence + 5,
                    e.eventType, e.side, e.priceTicks, e.qty, e.orderId, e.tradeId));
        }
        MetricsRegistry reg = new MetricsRegistry();
        RiskEngine risk = RiskEngine.fromConfig(cfg.riskDoc(), cfg.riskInstruments(), reg);
        BacktestEngine[] holder = new BacktestEngine[1];
        PaperTrading.RiskWiring wiring = new PaperTrading.RiskWiring(risk, "PAPER", 1,
                reg);
        // alternate the target every vector so a decision is forced at
        // every event, including the heartbeats inside the stall
        long[] flip = {0};
        BacktestEngine.Strategy strategy = vec -> (++flip[0] & 1) == 0 ? 100 : -100;
        BacktestEngine engine = new BacktestEngine(exec(cfg), strategy, wiring.hook(),
                wiring, BacktestEngine.USD_ONLY, 100, 1,
                BacktestEngine.ExecutionLimits.NONE, SorOptions.DEFAULT);
        holder[0] = engine;
        BacktestEngine.Summary s = engine.run(evs);
        assertTrue(s.fillCount > 0);
        long staleInStall = 0;
        for (RiskEvent e : risk.audit()) {
            boolean inStall = e.timestamp() >= gapStart + 6 * SEC
                    && e.timestamp() < gapEnd;
            if (inStall) {
                assertTrue("nothing is allowed against a 6 s old mark: " + e.ruleId(),
                        !e.ruleId().equals(Rules.ALLOW));
                if (e.ruleId().equals(Rules.STALE_PRICE)) {
                    staleInStall++;
                    assertTrue(e.reason(), e.reason().contains("age 6"));
                }
            }
        }
        assertTrue("stale gate fires during the stall", staleInStall >= 1);
        for (Fill f : engine.simulator().fills()) {
            assertTrue("no fill inside the stall",
                    f.ts() <= gapStart + 1_000_000L || f.ts() >= gapEnd);
        }
        boolean allowedAfter = risk.audit().stream().anyMatch(e ->
                e.ruleId().equals(Rules.ALLOW) && e.timestamp() >= gapEnd);
        assertTrue("trading resumes after the first post-gap quote", allowedAfter);
        assertEquals(reg.counterValue("risk_rejected_total"),
                risk.audit().stream().filter(e -> e.decision() == 2
                        && !e.ruleId().equals(Rules.MALFORMED_FILL)).count());
    }

    /**
     * The risk engine sees every fill before the next decision and every
     * terminal child: at hook time its position equals the account's, and
     * its open-order set never exceeds the simulator's live children.
     */
    @Test
    public void paperRiskPositionNotLagging() {
        ConfigService cfg = cfg();
        List<MarketEvent> evs = Golden.eq().subList(0, 1500);
        MetricsRegistry reg = new MetricsRegistry();
        RiskEngine risk = RiskEngine.fromConfig(cfg.riskDoc(), cfg.riskInstruments(), reg);
        BacktestEngine[] holder = new BacktestEngine[1];
        PaperTrading.RiskWiring wiring = new PaperTrading.RiskWiring(risk, "PAPER", 1,
                reg);
        BacktestEngine.RiskHook inner = wiring.hook();
        long[] checks = {0};
        BacktestEngine.RiskHook spy = (iid, pos, inflight, delta, ts) -> {
            BacktestEngine.Account a = holder[0].accounts().get(iid);
            long accountPos = a == null ? 0 : a.position;
            assertEquals("risk position at hook time", accountPos, risk.position(iid));
            assertEquals("engine's own view", accountPos, pos);
            long live = holder[0].simulator().pendingIds().size()
                    + holder[0].simulator().restingIds().size();
            assertTrue("open orders released on terminal reports",
                    risk.openOrderCount() <= live);
            checks[0]++;
            return inner.approve(iid, pos, inflight, delta, ts);
        };
        long[] flip = {0};
        BacktestEngine.Strategy strategy = vec -> (++flip[0] % 50) < 25 ? 100 : -100;
        BacktestEngine engine = new BacktestEngine(exec(cfg), strategy, spy, wiring,
                BacktestEngine.USD_ONLY, 100, 1, BacktestEngine.ExecutionLimits.NONE,
                SorOptions.DEFAULT);
        holder[0] = engine;
        BacktestEngine.Summary s = engine.run(evs);
        assertTrue(s.fillCount > 0);
        assertTrue(checks[0] > 10);
        assertEquals(s.accounts.get(1L).position, risk.position(1));
        assertEquals("every child terminal at the end", 0, risk.openOrderCount());
        // one child per allowed decision (open-order parity)
        Map<Long, com.iap.execution.ChildOrder> orders = engine.simulator().orders();
        long allowed = risk.audit().stream().filter(e -> e.ruleId().equals(Rules.ALLOW)).count();
        assertEquals(allowed, orders.size());
    }

    /** A sequence gap closes the gap gate until the SNAPSHOT recovery. */
    @Test
    public void paperSequenceGapClosesTheGate() {
        ConfigService cfg = cfg();
        List<MarketEvent> golden = Golden.eq();
        List<MarketEvent> evs = new ArrayList<>(golden.subList(0, 300));
        for (int i = 300; i < 600; i++) {
            MarketEvent e = golden.get(i);
            evs.add(new MarketEvent(e.eventId, e.instrumentId, e.venueId,
                    e.exchangeTs, e.receiveTs, e.sequence + 7, e.eventType, e.side,
                    e.priceTicks, e.qty, e.orderId, e.tradeId)); // jump: gap
        }
        MetricsRegistry reg = new MetricsRegistry();
        RiskEngine risk = RiskEngine.fromConfig(cfg.riskDoc(), cfg.riskInstruments(), reg);
        BacktestEngine[] holder = new BacktestEngine[1];
        PaperTrading.RiskWiring wiring = new PaperTrading.RiskWiring(risk, "PAPER", 1,
                reg);
        long[] flip = {0};
        BacktestEngine engine = new BacktestEngine(exec(cfg),
                vec -> (++flip[0] & 1) == 0 ? 100 : -100, wiring.hook(), wiring,
                BacktestEngine.USD_ONLY, 100, 1, BacktestEngine.ExecutionLimits.NONE,
                SorOptions.DEFAULT);
        holder[0] = engine;
        engine.run(evs);
        long gapRejects = risk.audit().stream()
                .filter(e -> e.ruleId().equals(Rules.SEQUENCE_GAP)).count();
        assertTrue("SEQUENCE_GAP fires after the jump", gapRejects > 0);
        long gapTs = golden.get(300).exchangeTs;
        for (Fill f : engine.simulator().fills()) {
            assertTrue("no fill on a stale venue", f.ts() < gapTs + 1_000_000L);
        }
        assertEquals(1, engine.simulator().venueBook(1, 1).gapsDetected());
    }
}
