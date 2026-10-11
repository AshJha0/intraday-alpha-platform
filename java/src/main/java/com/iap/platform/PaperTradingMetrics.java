package com.iap.platform;

import com.iap.backtest.BacktestEngine;
import com.iap.core.MarketEvent;
import com.iap.execution.ExecConfig;
import com.iap.execution.Fill;
import com.iap.monitoring.Counter;
import com.iap.monitoring.Gauge;
import com.iap.monitoring.Histogram;
import com.iap.monitoring.MetricsRegistry;
import com.iap.orderbook.OrderBook;
import com.iap.risk.RiskLimits;

/**
 * Observability helpers of {@link PaperTrading} (PLATFORM_CONVENTIONS.md §12.6):
 * risk-limit gauges, the execution-metrics decorator and book-health counters.
 * Package-private: split out of PaperTrading (v1.13 E4) without behaviour change.
 */
final class PaperTradingMetrics {
    private PaperTradingMetrics() {
    }

    /** Export the live risk limits as gauges so alerts never hard-code them. */
    static void exportRiskLimits(MetricsRegistry reg, RiskLimits l) {
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_daily_loss")).set(l.maxDailyLoss());
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_strategy_daily_loss")).set(l.strategyMaxDailyLoss());
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_gross_notional")).set(l.maxGrossNotional());
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_net_notional")).set(l.maxNetNotional());
    }

    /**
     * Execution observability decorator (PLATFORM_CONVENTIONS.md §12.6):
     * delegates every callback to the pinned {@link PaperTrading.RiskWiring} unchanged and
     * additionally exports the platform's OWN execution counters —
     * {@code exec_orders_submitted_total}, {@code exec_fills_total} and the
     * {@code exec_slippage_bps} histogram (|fill − mark| in basis points,
     * integer-scaled ×100). These replace the venue simulator's counters,
     * which described a component the deployment never ran.
     */
    static final class ExecMetrics implements BacktestEngine.ExecutionListener {
        private final PaperTrading.RiskWiring inner;
        private final Counter submitted;
        private final Counter fills;
        private final Histogram slippage;
        private final BacktestEngine[] engineHolder;
        private final ExecConfig exec;

        ExecMetrics(PaperTrading.RiskWiring inner, MetricsRegistry reg,
                BacktestEngine[] engineHolder, ExecConfig exec) {
            this.inner = inner;
            this.submitted = reg.counter("exec_orders_submitted_total");
            this.fills = reg.counter("exec_fills_total");
            this.slippage = reg.histogram("exec_slippage_bps");
            this.engineHolder = engineHolder;
            this.exec = exec;
            reg.counter("exec_child_orders_rejected_total");
        }

        /** Seed the counters of a resumed session (§12.3). */
        void counters(long ordersSubmitted, long fillCount) {
            submitted.add(ordersSubmitted);
            fills.add(fillCount);
        }

        long submitted() {
            return submitted.get();
        }

        long fills() {
            return fills.get();
        }

        @Override
        public void onFill(Fill f) {
            inner.onFill(f);
            fills.inc();
            BacktestEngine eng = engineHolder[0];
            if (eng == null) {
                return;
            }
            BacktestEngine.Account a = eng.accounts().get(f.instrumentId());
            if (a == null || !a.markValid || a.mark <= 0.0) {
                return;
            }
            double price = (double) f.priceTicks()
                    * exec.instrument(f.instrumentId()).tickSize();
            double bps = Math.abs(price - a.mark) / a.mark * 10_000.0;
            slippage.record(Math.round(bps * 100.0));
        }

        @Override
        public void onOrderSubmitted(com.iap.execution.ChildOrder o) {
            inner.onOrderSubmitted(o);
            submitted.inc();
        }

        @Override
        public void onOrderTerminal(com.iap.execution.ChildOrder o) {
            inner.onOrderTerminal(o);
        }

        @Override
        public void onMarket(MarketEvent ev,
                com.iap.orderbook.ConsolidatedBook book) {
            inner.onMarket(ev, book);
        }
    }

    /**
     * Export book-health counters. Called on EVERY event (§12.6): the risk
     * config halts after ONE sequence gap, so a gap must be visible on the
     * next scrape rather than at the next 1,024-event sampling point.
     */
    static void sampleBookHealth(Counter gaps, Counter dups,
            Gauge stale, BacktestEngine engine, long instrumentId,
            long[] lastSeen) {
        long g = 0;
        long d = 0;
        boolean anyStale = false;
        for (OrderBook b : engine.simulator().instrumentBook(instrumentId)
                .venues().values()) {
            g += b.gapsDetected();
            d += b.duplicatesDropped();
            anyStale |= b.isStale();
        }
        if (g > lastSeen[0]) {
            gaps.add(g - lastSeen[0]);
            lastSeen[0] = g;
        }
        if (d > lastSeen[1]) {
            dups.add(d - lastSeen[1]);
            lastSeen[1] = d;
        }
        stale.set(anyStale ? 1.0 : 0.0);
    }
}
