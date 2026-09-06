package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.file.Path;
import java.util.Map;

import org.junit.Test;

import com.iap.config.ConfigService;
import com.iap.platform.PaperTrading;

/**
 * The one pinned money unit (PLATFORM_CONVENTIONS.md §12.1) — the fix for
 * round-3 SEV-1 "risk-engine P&amp;L/notional and the platform's own
 * accounting differ by lot_size (100x); the daily-loss kill switch cannot
 * fire". Proposed tests 6 (DailyLossLatchInPaper), 7
 * (RiskAndPortfolioUnitsAgree) and 5 (KillSwitchFromConfigHaltsPaper).
 */
public class PaperUnitsTest {
    /**
     * Test 7 — the risk engine and the portfolio gauges value the same
     * position identically: {@code |pos| * qty_unit * mark}. A 100x
     * disagreement would show here first.
     */
    @Test
    public void riskAndPortfolioNotionalsAgree() throws IOException {
        PaperTrading.Result res = PaperTrading.run(PaperFixtures.session(1500));
        ConfigService cfg = new ConfigService(PaperFixtures.configs());
        double qtyUnit = cfg.instruments().get(1L).qtyUnit();
        long pos = res.riskAudit.position(1);
        assertTrue("the session ended with exposure", pos != 0);
        Double gross = res.metrics.gaugeValue("portfolio_gross_notional");
        Double net = res.metrics.gaugeValue("portfolio_net_notional");
        assertTrue("gauges exported", gross != null && net != null);
        assertEquals("net notional is signed by the position",
                Math.copySign(gross, pos), net, 1e-9);
        // the mark implied by the gauge is the instrument's real mid, so the
        // gauge is |pos| * qty_unit * mark with the SAME qty_unit the risk
        // engine uses (no second lot factor anywhere)
        double impliedMark = gross / (Math.abs(pos) * qtyUnit);
        assertTrue("implied mark is a real price: " + impliedMark,
                impliedMark > 1.0 && impliedMark < 10_000.0);
    }

    /**
     * Test 6 / §12.1 — the two P&amp;L paths agree exactly: the risk engine's
     * daily P&amp;L equals the backtester's {@code grossPnl - spreadCost}, and
     * the session report's {@code pnl.total} is that same number net of fees
     * and impact. One unit, end to end.
     */
    @Test
    public void riskAndBacktestPnlAgreeInOneUnit() throws IOException {
        PaperTrading.Result res = PaperTrading.run(PaperFixtures.session(1500));
        Double daily = res.riskAudit.globalDailyPnl();
        assertTrue("daily P&L determinable", daily != null);
        assertEquals("risk daily P&L == grossPnl - spreadCost",
                res.grossPnl - res.spreadCost, daily, 1e-9);
        assertEquals("realized + unrealized == daily",
                res.riskAudit.realizedPnl() + res.riskAudit.unrealizedPnl(),
                daily, 1e-9);
        assertEquals("report total == daily - fees - impact",
                daily - res.feesNet - res.impact, res.totalPnl, 1e-9);
        Map<String, Object> pnl = Json.object(
                PaperFixtures.json(res.reportJson).get("pnl"));
        assertEquals(res.totalPnl, Json.asDouble(pnl.get("total")), 0.0);
        // and the limits the alerts divide by are exported live, in that unit
        assertEquals(250000.0,
                res.metrics.gaugeValue("risk_limit{limit=\"max_daily_loss\"}"),
                0.0);
        assertEquals(50000.0, res.metrics.gaugeValue(
                "risk_limit{limit=\"max_strategy_daily_loss\"}"), 0.0);
    }

    /**
     * Test 6 — a session whose daily loss limit is small enough to breach
     * LATCHES the global kill switch, stops allowing orders and keeps
     * rejecting. Before the unit pin this could not happen at all.
     */
    @Test
    public void scenarioDailyLossLimitLatchesTheKillSwitch() throws IOException {
        Path configs = PaperFixtures.copyConfigs();
        String risk = PaperFixtures.readConfig(configs, "risk.json");
        assertTrue(risk.contains("\"max_daily_loss\": 250000"));
        // 20 USD: the golden EQ session loses more than that in its own unit
        PaperFixtures.writeConfig(configs, "risk.json",
                risk.replace("\"max_daily_loss\": 250000",
                        "\"max_daily_loss\": 20"));
        PaperTrading.Options opts = PaperFixtures.session(1500);
        opts.configsDir = configs;
        PaperTrading.Result res = PaperTrading.run(opts);
        assertTrue("the session latched the global kill switch",
                res.killSwitchEngaged);
        assertEquals(1.0,
                res.metrics.gaugeValue("risk_kill_switch_engaged"), 0.0);
        assertTrue("orders were rejected after the latch", res.riskRejected > 0);
        assertTrue("it traded before the latch", res.riskAllowed > 0);
        assertEquals(Boolean.TRUE,
                PaperFixtures.json(res.reportJson).get("kill_switch_engaged"));
        // the latch is in the audit log
        assertTrue(res.riskAudit.auditJsonl().contains("KILL_GLOBAL")
                || res.riskAudit.auditJsonl().contains("DAILY_LOSS"));
    }

    /**
     * Test 5 — the deploy-time hard stop: {@code kill_switch_engaged: true}
     * in risk.json boots the platform halted; zero orders are allowed and the
     * gauge reads 1 from the first scrape.
     */
    @Test
    public void scenarioConfigMasterKillSwitchHaltsTheSession()
            throws IOException {
        Path configs = PaperFixtures.copyConfigs();
        PaperFixtures.writeConfig(configs, "risk.json",
                PaperFixtures.readConfig(configs, "risk.json")
                        .replace("\"kill_switch_engaged\": false",
                                "\"kill_switch_engaged\": true"));
        PaperTrading.Options opts = PaperFixtures.session(800);
        opts.configsDir = configs;
        PaperTrading.Result res = PaperTrading.run(opts);
        assertEquals("no order allowed", 0, res.riskAllowed);
        assertEquals(1.0,
                res.metrics.gaugeValue("risk_kill_switch_engaged"), 0.0);
        assertTrue(res.killSwitchEngaged);
        assertEquals(0, res.fillCount);
        assertEquals(0, res.metrics.counterValue("exec_fills_total"));
        assertEquals(Boolean.TRUE,
                PaperFixtures.json(res.reportJson).get("kill_switch_engaged"));
    }
}
