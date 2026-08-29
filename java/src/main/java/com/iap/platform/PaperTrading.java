package com.iap.platform;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.TreeMap;

import com.iap.alpha.AlphaSignal;
import com.iap.alpha.Alphas;
import com.iap.alpha.LinearZParams;
import com.iap.api.MetricsServer;
import com.iap.backtest.BacktestEngine;
import com.iap.codec.JsonlCodec;
import com.iap.config.ConfigService;
import com.iap.core.MarketEvent;
import com.iap.execution.ExecConfig;
import com.iap.execution.Fill;
import com.iap.execution.LatencyConfig;
import com.iap.features.Features;
import com.iap.monitoring.GcMetrics;
import com.iap.monitoring.Histogram;
import com.iap.monitoring.MetricsRegistry;
import com.iap.orderbook.OrderBook;
import com.iap.portfolio.Constraints;
import com.iap.portfolio.EwmaCovariance;
import com.iap.portfolio.PgdResult;
import com.iap.portfolio.PortfolioOptimizer;
import com.iap.portfolio.SolverParams;
import com.iap.risk.OrderRequest;
import com.iap.risk.RiskDecision;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskFill;

/**
 * Paper-trading mode (spec §20 step 11 / §31): streams the normalized
 * golden vectors through the SAME component chain as the backtester —
 * book → features → alphas → portfolio sizing → hard risk → execution
 * simulator — in as-fast-as-possible or real-time-scaled mode, exposing
 * {@code /metrics} (grafana metric-name contract) and writing a session
 * report JSON (P&amp;L, fills, risk events, latency percentiles).
 *
 * <p>The trading path is deterministic (event time only); wall-clock is
 * used ONLY for observability (latency histograms, realtime pacing) and
 * never influences a decision, so two runs over the same events produce
 * identical fills, P&amp;L and risk decisions.
 *
 * <p>Portfolio sizing: on every completed 1-minute bar (once
 * {@code >= 22} bars exist) the production optimizer solves the pinned
 * single-asset problem (EWMA variance, box + participation + vol target)
 * and the resulting |weight| scales the strategy's max position. Runnable
 * via {@code java/paper.sh}.
 */
public final class PaperTrading {
    private static final long BAR_NS = EwmaCovariance.BAR_NS;

    /** Run options (defaults = the pinned golden EQ session). */
    public static final class Options {
        /** configs/ directory. */
        public Path configsDir = Paths.get("..", "configs");
        /** Normalized JSONL event file to stream. */
        public Path eventsFile = Paths.get("..", "tests", "golden",
                "events_eq_mbo.jsonl");
        /** Instrument to trade. */
        public long instrumentId = 1;
        /** Alpha to score (configs/strategies/alpha_params.json id). */
        public String alphaId = "EQ01";
        /** Real-time-scaled (sleep between events) vs as-fast-as-possible. */
        public boolean realtime;
        /** Realtime speed multiplier (10 = 10x faster than real time). */
        public double speed = 1.0;
        /** Cap on events processed (whole file when larger). */
        public long maxEvents = Long.MAX_VALUE;
        /** HTTP port: -1 = no server, 0 = ephemeral, else fixed. */
        public int port = -1;
        /** Session report path (null = do not write). */
        public Path reportPath;
        /** Hard cap on the strategy position (units). */
        public long maxPos = 1000;
        /** Minimum alpha confidence to hold a position. */
        public double confMin = 0.2;
        /** Venue for child orders (0 = SOR). */
        public int venueId = 1;
    }

    /** Session result (deterministic fields + the report JSON). */
    public static final class Result {
        /**
         * Volatile: read by the HTTP {@code /status} supplier from the
         * server thread while the trading thread writes it — volatile
         * guarantees a torn-free, current value without locking.
         */
        public volatile long eventsProcessed;
        public long fillCount;
        public long ordersSubmitted;
        public long riskDecisions;
        public long riskAllowed;
        public long riskRejected;
        public long riskEvents;
        public boolean killSwitchEngaged;
        public double totalPnl;
        public double grossPnl;
        public double feesNet;
        public double impact;
        public double spreadCost;
        public int httpPort = -1;
        public String reportJson;
        public MetricsRegistry metrics;
    }

    private PaperTrading() {
    }

    /** Run one paper-trading session. */
    public static Result run(Options opts) throws IOException {
        MetricsRegistry reg = new MetricsRegistry();
        GcMetrics gc = new GcMetrics(reg);
        ConfigService cfg = new ConfigService(opts.configsDir);
        ExecConfig exec = new ExecConfig(LatencyConfig.DEFAULT,
                cfg.executionSeed(), cfg.impactCoeffBpsPerPctAdv(),
                cfg.instruments(), cfg.venues());
        RiskEngine risk = RiskEngine.fromConfig(cfg.riskDoc(), cfg.tickSizes(),
                reg);
        LinearZParams alphaParams = Alphas.loadParams(
                opts.configsDir.resolve("strategies").resolve("alpha_params.json"))
                .get(opts.alphaId);
        if (alphaParams == null) {
            throw new IllegalArgumentException("unknown alpha id " + opts.alphaId);
        }

        // decode (JSONL -> MarketEvent), timing each line
        List<MarketEvent> events = new ArrayList<>();
        synchronized (reg) {
            for (String line : Files.readAllLines(opts.eventsFile,
                    StandardCharsets.UTF_8)) {
                if (line.isEmpty()) {
                    continue;
                }
                long t0 = System.nanoTime();
                MarketEvent ev = JsonlCodec.decodeLine(line);
                reg.histogram("decode_latency_ns").record(System.nanoTime() - t0);
                if (ev.instrumentId == opts.instrumentId
                        && events.size() < opts.maxEvents) {
                    events.add(ev);
                }
            }
        }
        if (events.isEmpty()) {
            throw new IllegalArgumentException("no events for instrument "
                    + opts.instrumentId + " in " + opts.eventsFile);
        }

        // portfolio sizing state (1-minute bars -> EWMA variance -> weight)
        final class Sizing {
            final List<Double> barReturns = new ArrayList<>();
            long currentBar = Long.MIN_VALUE;
            double lastMid = Double.NaN;
            double barMid = Double.NaN;
            double weightScale = 1.0;
            double positionWeight; // current position / maxPos
        }
        Sizing sizing = new Sizing();
        long[] orderIdSeq = {0};
        BacktestEngine[] engineHolder = new BacktestEngine[1];

        BacktestEngine.Strategy strategy = vec -> {
            reg.counter("alpha_signals_total").inc();
            AlphaSignal sig = Alphas.scoreRow(alphaParams, vec);
            // 1-minute bar roll: last-observation mid per pinned bucket
            if (vec.valid[Features.MID_PRICE]) {
                double mid = vec.values[Features.MID_PRICE];
                long bar = Math.floorDiv(vec.timestamp, BAR_NS) * BAR_NS;
                if (bar != sizing.currentBar) {
                    if (!Double.isNaN(sizing.barMid)) {
                        if (!Double.isNaN(sizing.lastMid)) {
                            sizing.barReturns.add(
                                    Math.log(sizing.barMid / sizing.lastMid));
                        }
                        sizing.lastMid = sizing.barMid;
                        if (sizing.barReturns.size() >= 22) {
                            sizing.weightScale = solveWeight(sizing.barReturns,
                                    sig.expectedReturn(), sizing.positionWeight);
                            reg.counter("portfolio_solves_total").inc();
                        }
                    }
                    sizing.currentBar = bar;
                }
                sizing.barMid = mid;
            }
            if (sig.confidence() < opts.confMin || sig.expectedReturn() == 0.0) {
                return 0;
            }
            long target = Math.round(Math.signum(sig.expectedReturn())
                    * sizing.weightScale * opts.maxPos);
            sizing.positionWeight = (double) target / opts.maxPos;
            return target;
        };

        BacktestEngine.RiskHook hook = (iid, pos, inflight, delta, ts) -> {
            if (delta == 0) {
                return 0;
            }
            long t0 = System.nanoTime();
            // consolidated best bid/ask at decision time -> reference price
            var book = engineHolder[0].simulator().instrumentBook(iid);
            long[] bb = book.bestBid();
            long[] ba = book.bestAsk();
            if (bb != null && ba != null) {
                risk.onMarket(iid, bb[0], ba[0], ts);
            }
            OrderRequest req = new OrderRequest(++orderIdSeq[0], iid,
                    delta > 0 ? 0 : 1, Math.abs(delta), 0, OrderRequest.MARKET,
                    opts.venueId, "PAPER", 0.5, ts);
            RiskDecision d = risk.checkOrder(req);
            synchronized (reg) {
                reg.histogram("order_path_latency_ns")
                        .record(System.nanoTime() - t0);
            }
            return d.allowed() ? delta : 0;
        };

        BacktestEngine engine = new BacktestEngine(exec, strategy, hook,
                opts.maxPos, opts.venueId);
        engineHolder[0] = engine;

        MetricsServer server = null;
        Result res = new Result();
        if (opts.port >= 0) {
            server = new MetricsServer(reg, opts.port, () -> statusJson(res));
            server.start();
            res.httpPort = server.port();
        }

        double lot = exec.instrument(opts.instrumentId).lotSize();
        double equityPeak = 0.0;
        int fillCursor = 0;
        long prevReceiveTs = events.get(0).receiveTs;
        try {
            for (MarketEvent ev : events) {
                if (opts.realtime) {
                    long waitNs = (long) ((ev.receiveTs - prevReceiveTs)
                            / Math.max(opts.speed, 1e-9));
                    prevReceiveTs = ev.receiveTs;
                    sleepNs(waitNs);
                }
                synchronized (reg) {
                    long t0 = System.nanoTime();
                    engine.onEvent(ev);
                    reg.histogram("book_update_latency_ns")
                            .record(System.nanoTime() - t0);
                    reg.counter("md_events_total").inc();
                    reg.gauge("md_last_event_unixtime")
                            .set(Math.floorDiv(ev.receiveTs, 1_000_000_000L));
                    // book fills -> risk position/PnL accounting
                    List<Fill> fills = engine.simulator().fills();
                    while (fillCursor < fills.size()) {
                        Fill f = fills.get(fillCursor++);
                        risk.onFill(new RiskFill(f.ts(), "PAPER",
                                f.instrumentId(), 0, f.side(), f.qty(),
                                f.priceTicks()));
                    }
                    // exposure / drawdown gauges
                    BacktestEngine.Account a =
                            engine.accounts().get(opts.instrumentId);
                    if (a != null && a.markValid) {
                        double notional = a.position * lot * a.mark;
                        reg.gauge("portfolio_gross_notional")
                                .set(Math.abs(notional));
                        reg.gauge("portfolio_net_notional").set(notional);
                        double equity = a.equity(lot);
                        equityPeak = Math.max(equityPeak, equity);
                        reg.gauge("portfolio_drawdown").set(equityPeak - equity);
                    }
                    if ((engine.eventsProcessed() & 1023) == 0) {
                        sampleBookHealth(reg, engine, opts.instrumentId);
                        gc.sample();
                    }
                }
            }
            BacktestEngine.Summary summary;
            synchronized (reg) {
                summary = engine.finish();
                List<Fill> fills = engine.simulator().fills();
                while (fillCursor < fills.size()) {
                    Fill f = fills.get(fillCursor++);
                    risk.onFill(new RiskFill(f.ts(), "PAPER", f.instrumentId(),
                            0, f.side(), f.qty(), f.priceTicks()));
                }
                sampleBookHealth(reg, engine, opts.instrumentId);
                gc.sample();
            }
            res.eventsProcessed = summary.eventsProcessed;
            res.fillCount = summary.fillCount;
            BacktestEngine.Account a = summary.accounts.get(opts.instrumentId);
            res.ordersSubmitted = a == null ? 0 : a.ordersSubmitted;
            res.totalPnl = summary.totalPnl;
            res.grossPnl = summary.grossPnl;
            res.feesNet = summary.feesNet;
            res.impact = summary.impact;
            res.spreadCost = summary.spreadCost;
            res.riskDecisions = reg.counterValue("risk_decisions_total");
            res.riskAllowed = reg.counterValue("risk_allowed_total");
            res.riskRejected = reg.counterValue("risk_rejected_total");
            res.riskEvents = reg.counterValue("risk_events_total");
            res.killSwitchEngaged = risk.killSwitchEngaged();
            res.metrics = reg;
            res.reportJson = reportJson(res, opts, reg);
            if (opts.reportPath != null) {
                Files.createDirectories(
                        opts.reportPath.toAbsolutePath().getParent());
                Files.write(opts.reportPath,
                        res.reportJson.getBytes(StandardCharsets.UTF_8));
            }
        } finally {
            if (server != null) {
                server.stop();
            }
        }
        return res;
    }

    /** Single-asset portfolio solve: |weight| in [0, 1] from EWMA variance. */
    private static double solveWeight(List<Double> barReturns,
            double expectedReturn, double positionWeight) {
        double[][] rets = new double[barReturns.size()][1];
        for (int i = 0; i < barReturns.size(); i++) {
            rets[i][0] = barReturns.get(i);
        }
        double[][] sigma = EwmaCovariance.estimate(rets);
        Constraints cons = new Constraints(new double[] {-1.0},
                new double[] {1.0});
        cons.participation = new double[] {0.5};
        cons.volTarget = 5e-4; // per-bar vol target (pinned sizing choice)
        PgdResult sol = PortfolioOptimizer.solve(
                new double[] {expectedReturn}, sigma,
                new double[] {positionWeight}, 6.0, new double[] {1e-4}, cons,
                new SolverParams(null, 0.01, 100, 4, 1e-7));
        return Math.min(Math.abs(sol.weights()[0]), 1.0);
    }

    private static void sampleBookHealth(MetricsRegistry reg,
            BacktestEngine engine, long instrumentId) {
        long gaps = 0;
        long dups = 0;
        for (OrderBook b : engine.simulator().instrumentBook(instrumentId)
                .venues().values()) {
            gaps += b.gapsDetected();
            dups += b.duplicatesDropped();
        }
        long seenGaps = reg.counterValue("md_sequence_gaps_total");
        long seenDups = reg.counterValue("md_duplicates_total");
        reg.counter("md_sequence_gaps_total").add(Math.max(gaps - seenGaps, 0));
        reg.counter("md_duplicates_total").add(Math.max(dups - seenDups, 0));
    }

    private static void sleepNs(long ns) {
        if (ns <= 0) {
            return;
        }
        try {
            Thread.sleep(ns / 1_000_000L, (int) (ns % 1_000_000L));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static String statusJson(Result res) {
        return "{\"component\":\"paper_trading\",\"events_processed\":"
                + res.eventsProcessed + ",\"status\":\"ok\"}";
    }

    private static String pct(MetricsRegistry reg, String name) {
        Histogram h = reg.histogramRef(name);
        if (h == null || h.count() == 0) {
            return "{\"count\":0}";
        }
        long[] p = h.p50p99p999();
        return "{\"count\":" + h.count() + ",\"p50\":" + p[0] + ",\"p99\":"
                + p[1] + ",\"p999\":" + p[2] + "}";
    }

    private static String reportJson(Result res, Options opts,
            MetricsRegistry reg) {
        StringBuilder sb = new StringBuilder(512);
        sb.append("{\"alpha_id\":\"").append(opts.alphaId)
                .append("\",\"events_processed\":").append(res.eventsProcessed)
                .append(",\"fills\":").append(res.fillCount)
                .append(",\"instrument_id\":").append(opts.instrumentId)
                .append(",\"kill_switch_engaged\":").append(res.killSwitchEngaged)
                .append(",\"latency_ns\":{\"book_update\":")
                .append(pct(reg, "book_update_latency_ns"))
                .append(",\"decode\":").append(pct(reg, "decode_latency_ns"))
                .append(",\"order_path\":").append(pct(reg, "order_path_latency_ns"))
                .append("},\"mode\":\"").append(opts.realtime ? "realtime" : "asap")
                .append("\",\"orders_submitted\":").append(res.ordersSubmitted)
                .append(",\"pnl\":{\"fees_net\":").append(res.feesNet)
                .append(",\"gross\":").append(res.grossPnl)
                .append(",\"impact\":").append(res.impact)
                .append(",\"spread_cost\":").append(res.spreadCost)
                .append(",\"total\":").append(res.totalPnl)
                .append("},\"risk\":{\"allowed\":").append(res.riskAllowed)
                .append(",\"decisions\":").append(res.riskDecisions)
                .append(",\"events\":").append(res.riskEvents)
                .append(",\"rejected\":").append(res.riskRejected)
                .append("},\"x-version\":1}");
        return sb.toString();
    }

    /** CLI: see java/paper.sh for the flags. */
    public static void main(String[] args) throws IOException {
        Options opts = new Options();
        opts.reportPath = Paths.get("out", "paper_session_report.json");
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--configs" -> opts.configsDir = Paths.get(args[++i]);
                case "--events" -> opts.eventsFile = Paths.get(args[++i]);
                case "--instrument" ->
                        opts.instrumentId = Long.parseLong(args[++i]);
                case "--alpha" -> opts.alphaId = args[++i];
                case "--mode" -> opts.realtime = switch (args[++i]) {
                    case "realtime" -> true;
                    case "asap" -> false;
                    default -> throw new IllegalArgumentException(
                            "mode must be asap|realtime");
                };
                case "--speed" -> opts.speed = Double.parseDouble(args[++i]);
                case "--max-events" ->
                        opts.maxEvents = Long.parseLong(args[++i]);
                case "--port" -> {
                    String v = args[++i];
                    opts.port = "config".equals(v) ? -2 : Integer.parseInt(v);
                }
                case "--report" -> opts.reportPath = Paths.get(args[++i]);
                default -> throw new IllegalArgumentException(
                        "unknown argument " + a);
            }
        }
        if (opts.port == -2) { // sentinel: from config
            opts.port = new ConfigService(opts.configsDir).monitoringPort();
        }
        Result res = run(opts);
        System.out.println(String.format(Locale.ROOT,
                "paper session: events=%d orders=%d fills=%d pnl=%.6f "
                        + "risk[allowed=%d rejected=%d] port=%d report=%s",
                res.eventsProcessed, res.ordersSubmitted, res.fillCount,
                res.totalPnl, res.riskAllowed, res.riskRejected, res.httpPort,
                opts.reportPath));
    }
}
