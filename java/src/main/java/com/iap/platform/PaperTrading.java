package com.iap.platform;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

import com.iap.adaptive.BaselineLoader;
import com.iap.adaptive.DriftMonitor;
import com.iap.adaptive.LifecycleGauge;
import com.iap.adaptive.RollingIc;
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
import com.iap.monitoring.Counter;
import com.iap.monitoring.GcMetrics;
import com.iap.monitoring.Gauge;
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
import com.iap.risk.RiskEvent;
import com.iap.risk.RiskFill;
import com.iap.risk.RiskLimits;

/**
 * Paper-trading mode (spec §20 step 11 / §31): streams the normalized
 * golden vectors through the SAME component chain as the backtester —
 * book → features → alphas → portfolio sizing → hard risk → execution
 * simulator — in as-fast-as-possible or real-time-scaled mode, exposing
 * {@code /metrics}, {@code /health}, {@code /ready}, {@code /status} and the
 * kill-switch admin API (PLATFORM_CONVENTIONS.md §12.5) and writing a
 * session report JSON (P&amp;L, fills, risk events, latency percentiles).
 *
 * <p>The trading path is deterministic (event time only); wall-clock is
 * used ONLY for observability (latency histograms, realtime pacing, probe
 * freshness) and never influences a decision, so two runs over the same
 * events produce identical fills, P&amp;L and risk decisions.
 *
 * <p><b>Durable state (PLATFORM_CONVENTIONS.md §12.3).</b> Risk snapshot,
 * platform accounting, the risk audit JSONL and the config audit are
 * written to {@code --state-dir} at event-driven checkpoints (every
 * {@link #CHECKPOINT_EVERY_EVENTS} events), at session end, and from a JVM
 * shutdown hook. {@code --resume} restores them, so a restart mid-session
 * keeps positions, realized P&amp;L and any latched kill switch: the daily
 * loss limit is a daily limit, not a per-restart limit.
 *
 * <p>Portfolio sizing: on every completed 1-minute bar (once
 * {@code >= 22} bars exist) the production optimizer solves the pinned
 * single-asset problem (EWMA variance, box + participation + vol target)
 * and the resulting |weight| scales the strategy's max position. Runnable
 * via {@code java/paper.sh}.
 */
public final class PaperTrading {
    private static final long BAR_NS = EwmaCovariance.BAR_NS;

    /** Pinned checkpoint interval, in processed events (§12.3). */
    public static final long CHECKPOINT_EVERY_EVENTS = 1024;

    /**
     * Liveness budget (§12.5): a running loop that has not advanced
     * {@code events_processed} for this long, with events still pending, is
     * wedged and {@code /health} answers 503.
     */
    public static final long LIVENESS_STALL_NS = 30_000_000_000L;

    /** Session lifecycle for {@code platform_session_state} / {@code /status}. */
    public enum SessionState {
        /** Configs loaded, events decoding, no event processed yet. */
        STARTING(0),
        /** The trading loop is processing events. */
        RUNNING(1),
        /** The last event was processed and the report was written. */
        FINISHED(2),
        /** The session aborted; the process exits non-zero. */
        FAILED(3);

        private final int code;

        SessionState(int code) {
            this.code = code;
        }

        /** The pinned numeric code exported as the gauge value. */
        public int code() {
            return code;
        }

        /** Lower-case name used in the {@code /status} JSON. */
        public String label() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

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
        /**
         * research/baselines directory (API_ADAPTIVE.md schema). Missing
         * directory or no baseline for the alpha = drift monitoring stays
         * disarmed (no {@code alpha_live_vs_backtest_drift} series).
         */
        public Path baselinesDir = Paths.get("..", "research", "baselines");
        /**
         * Durable state directory (§12.3). {@code null} = derive from the
         * report path ({@code <report dir>/state}); state persistence is
         * always on so a crash is recoverable.
         */
        public Path stateDir;
        /** Resume from the checkpoint in {@link #stateDir} (fails closed). */
        public boolean resume;
        /** Events between checkpoints (0 = only at session end). */
        public long checkpointEveryEvents = CHECKPOINT_EVERY_EVENTS;
        /**
         * Admin API token. {@code null} = read {@code $IAP_ADMIN_TOKEN} /
         * {@code $IAP_ADMIN_TOKEN_FILE}; empty = admin API disabled.
         */
        public String adminToken;
        /**
         * Lifecycle hook called on the trading thread once the monitoring
         * server is bound and the session is about to process its first
         * event, with the live {@link Result} the endpoints read. Embedders
         * (and the endpoint tests) use it to learn the bound port and to
         * observe progress while the session runs.
         */
        public java.util.function.Consumer<Result> onServerStarted;
    }

    /** Session result (deterministic fields + the report JSON). */
    public static final class Result {
        /**
         * Volatile: written by the trading thread on EVERY event and read by
         * the HTTP {@code /status} supplier from a server thread, so the
         * endpoint reports live progress rather than a value that only
         * appears when the session ends.
         */
        public volatile long eventsProcessed;
        public long fillCount;
        public long ordersSubmitted;
        public long riskDecisions;
        public long riskAllowed;
        public long riskRejected;
        public long riskEvents;
        public boolean killSwitchEngaged;
        /** Pre-trade rejects by STALE_PRICE / SEQUENCE_GAP. */
        public long riskRejectedStale;
        /** The session's risk engine (audit log, positions) for inspection. */
        public RiskEngine riskAudit;
        /** Execution-control counters of the engine. */
        public BacktestEngine.Counters counters;
        /** Total P&amp;L in the reporting currency (USD). */
        public double totalPnl;
        public double grossPnl;
        public double feesNet;
        public double impact;
        public double spreadCost;
        public int httpPort = -1;
        public String reportJson;
        public MetricsRegistry metrics;
        /** Final live-vs-backtest PSI (NaN = never computed / no baseline). */
        public double driftPsi = Double.NaN;
        /** Final rolling realized IC (NaN = too few realized pairs). */
        public double rollingIc = Double.NaN;
        /** Final lifecycle state per the pinned thresholds. */
        public LifecycleGauge.State lifecycle = LifecycleGauge.State.ACTIVE;

        // ---- live/observability state (§12.5) ----
        /** Current session lifecycle (drives probes and the state gauge). */
        public volatile SessionState state = SessionState.STARTING;
        /** Event time (receive_ts, ns) of the last processed event. */
        public volatile long lastEventTs;
        /** {@code System.nanoTime()} when the last event was processed. */
        public volatile long lastEventWallNs;
        /** Wall-clock unix seconds when the last event was processed. */
        public volatile long lastEventWallUnix;
        /** True while a kill switch is latched (halted, not unhealthy). */
        public volatile boolean halted;
        /** Number of events the session must still process. */
        public volatile long eventsPending;
        /** Reason a probe fails, or "" — surfaced in /health, /ready. */
        public volatile String failureReason = "";
        /** Number of {@code --resume} restarts (0 for a fresh session). */
        public long restarts;
        /** SHA-256 identifying the whole configuration (§12.2). */
        public String configSha256 = "";
        /** Durable state directory actually used. */
        public Path stateDir;
        /** The admin service (drained by the trading thread). */
        public AdminService admin;
    }

    private PaperTrading() {
    }

    /**
     * Validate the run options before anything is opened, decoded or bound
     * (PLATFORM_CONVENTIONS.md §12.2 — fail fast, name the flag).
     */
    public static void validate(Options o) {
        if (o.configsDir == null) {
            throw new IllegalArgumentException("--configs is required");
        }
        if (o.eventsFile == null) {
            throw new IllegalArgumentException("--events is required");
        }
        if (o.alphaId == null || o.alphaId.isBlank()) {
            throw new IllegalArgumentException("--alpha must be a non-empty id");
        }
        if (o.instrumentId <= 0) {
            throw new IllegalArgumentException(
                    "--instrument must be > 0, got " + o.instrumentId);
        }
        if (o.maxEvents <= 0) {
            throw new IllegalArgumentException(
                    "--max-events must be > 0, got " + o.maxEvents);
        }
        if (!(Double.isFinite(o.speed) && o.speed > 0.0)) {
            throw new IllegalArgumentException(
                    "--speed must be finite and > 0, got " + o.speed);
        }
        if (o.port < -1 || o.port > 65535) {
            throw new IllegalArgumentException(
                    "--port must be -1 (off), 0 (ephemeral) or 1..65535, got "
                            + o.port);
        }
        if (o.maxPos <= 0) {
            throw new IllegalArgumentException(
                    "--max-pos must be > 0, got " + o.maxPos);
        }
        if (!(Double.isFinite(o.confMin) && o.confMin >= 0.0)) {
            throw new IllegalArgumentException(
                    "--conf-min must be finite and >= 0, got " + o.confMin);
        }
        if (o.checkpointEveryEvents < 0) {
            throw new IllegalArgumentException(
                    "--checkpoint-every must be >= 0, got "
                            + o.checkpointEveryEvents);
        }
        if (!Files.isReadable(o.eventsFile)) {
            throw new IllegalArgumentException(
                    "--events file is not readable: " + o.eventsFile);
        }
    }

    /** Where durable state lives for these options (§12.3). */
    public static Path stateDirOf(Options o) {
        if (o.stateDir != null) {
            return o.stateDir;
        }
        if (o.reportPath != null) {
            Path parent = o.reportPath.toAbsolutePath().getParent();
            return parent.resolve("state");
        }
        return Paths.get("out", "state");
    }

    /** Run one paper-trading session. */
    public static Result run(Options opts) throws IOException {
        validate(opts);
        // Resolve the admin token FIRST: a named-but-unreadable
        // $IAP_ADMIN_TOKEN_FILE is a configuration error, and §12.2 says
        // configuration fails before anything is opened or decoded.
        String adminToken = opts.adminToken != null ? opts.adminToken
                : AdminService.tokenFromEnv(System.getenv());
        MetricsRegistry reg = new MetricsRegistry();
        GcMetrics gc = new GcMetrics(reg);
        Result res = new Result();
        ConfigService cfg = new ConfigService(opts.configsDir);
        RiskLimits limits = cfg.riskLimits();
        res.configSha256 = cfg.configSha256();
        ExecConfig exec = new ExecConfig(LatencyConfig.DEFAULT,
                cfg.executionSeed(), cfg.impactCoeffBpsPerPctAdv(),
                cfg.instruments(), cfg.venues());
        LinearZParams alphaParams = Alphas.loadParams(
                opts.configsDir.resolve("strategies").resolve("alpha_params.json"))
                .get(opts.alphaId);
        if (alphaParams == null) {
            throw new IllegalArgumentException("unknown alpha id " + opts.alphaId);
        }

        // ---------------------------------------------------- durable state
        SessionStore store = new SessionStore(stateDirOf(opts));
        res.stateDir = store.dir();
        store.writeAtomic(SessionStore.CONFIG_AUDIT, cfg.auditJsonl());
        SessionStore.State st;
        if (opts.resume) {
            if (!store.hasCheckpoint()) {
                throw new IllegalStateException("--resume: no checkpoint in "
                        + store.dir() + " (" + SessionStore.SESSION_STATE
                        + " / " + SessionStore.RISK_SNAPSHOT + ")");
            }
            st = store.readState();
            if (st.instrumentId != opts.instrumentId
                    || !st.alphaId.equals(opts.alphaId)) {
                throw new IllegalStateException("--resume: checkpoint is for "
                        + "instrument " + st.instrumentId + " alpha "
                        + st.alphaId + ", not instrument " + opts.instrumentId
                        + " alpha " + opts.alphaId);
            }
            st.restarts++;
        } else {
            st = new SessionStore.State();
            st.instrumentId = opts.instrumentId;
            st.alphaId = opts.alphaId;
            st.configSha256 = res.configSha256;
            store.truncate(SessionStore.RISK_AUDIT);
        }
        res.restarts = st.restarts;
        // Values carried in from a resumed checkpoint. The in-loop checkpoint
        // overwrites st.* with session-cumulative values, so the session
        // totals must be built from these bases, never from st.* at the end.
        final long baseOrders = st.ordersSubmitted;
        final long baseFills = st.fillCount;
        final double basePnl = st.totalPnl;
        final double baseGross = st.grossPnl;

        // decode (JSONL -> MarketEvent), timing each line
        List<MarketEvent> events = new ArrayList<>();
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
        if (events.isEmpty()) {
            throw new IllegalArgumentException("no events for instrument "
                    + opts.instrumentId + " in " + opts.eventsFile);
        }
        int startIndex = (int) Math.min(st.eventCursor, events.size());
        if (opts.resume && startIndex >= events.size()) {
            throw new IllegalStateException("--resume: checkpoint cursor "
                    + st.eventCursor + " is at/past the end of the "
                    + events.size() + "-event stream");
        }

        // The audit file and the checkpoint that names it must agree before a
        // resume is allowed (§12.3). risk_audit.jsonl is appended and flushed
        // BEFORE session_state.json is atomically renamed, so a process killed
        // between the two leaves an audit LONGER than the recorded cursor;
        // resuming from it would replay decisions the file already records and
        // silently corrupt the "replays byte-identically" property the
        // postmortem depends on. A SHORTER file means a truncated or foreign
        // audit. Either way: fail closed, naming the file and both counts.
        if (opts.resume) {
            long onDisk = store.readLines(SessionStore.RISK_AUDIT).size();
            if (onDisk != st.auditLines) {
                throw new IllegalStateException("--resume: "
                        + store.file(SessionStore.RISK_AUDIT) + " has " + onDisk
                        + " lines but " + SessionStore.SESSION_STATE
                        + " records audit_lines=" + st.auditLines
                        + " (torn append, truncated file, or an audit from a"
                        + " different session): refusing to resume");
            }
        }

        // ------------------------------------------------------ risk engine
        RiskEngine risk;
        if (opts.resume) {
            long restoreTs = events.get(startIndex).exchangeTs;
            risk = RiskEngine.restore(limits, cfg.riskInstruments(),
                    store.readRiskSnapshot(), restoreTs, reg);
            reg.counter("risk_session_restarts_total").add(st.restarts);
        } else {
            risk = RiskEngine.fromConfig(cfg.riskDoc(), cfg.riskInstruments(),
                    reg);
        }
        exportRiskLimits(reg, limits);

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
        BacktestEngine[] engineHolder = new BacktestEngine[1];

        // Adaptability monitoring (spec §20 step 13 / API_ADAPTIVE.md):
        // live-vs-backtest PSI drift, rolling realized IC, and the pinned
        // IC-gated lifecycle state machine evaluated once per event-time
        // adaptive block (configs/strategies.json adaptive.*). Only rows
        // with confidence > 0 feed the monitors — the same population the
        // research baseline was captured from. Observational only — never
        // feeds back into a trading decision, so determinism is untouched
        // (PLATFORM_CONVENTIONS.md §12.6: the alert text says so too).
        DriftMonitor drift = new DriftMonitor(
                BaselineLoader.loadDir(opts.baselinesDir));
        Path strategiesJson = opts.configsDir.resolve("strategies.json");
        Map<String, Object> adaptiveCfg = cfg.adaptive();
        RollingIc rollingIc = new RollingIc(
                RollingIc.parseHorizonNs(alphaParams.horizon()),
                com.iap.config.Json.asLong(adaptiveCfg.get("ic_window_ns")),
                com.iap.config.Json.asLong(adaptiveCfg.get("ic_bucket_ns")),
                (int) com.iap.config.Json.asLong(
                        adaptiveCfg.get("min_ic_buckets")));
        LifecycleGauge lifecycle =
                LifecycleGauge.fromStrategiesConfig(strategiesJson);
        long blockNs =
                com.iap.config.Json.asLong(adaptiveCfg.get("block_ns"));
        long[] lifecycleBlock = {Long.MIN_VALUE};
        long[] lastSignalTs = {Long.MIN_VALUE};
        String driftGauge = MetricsRegistry.labeled(
                "alpha_live_vs_backtest_drift", "alpha", opts.alphaId);
        String icGauge = MetricsRegistry.labeled("alpha_rolling_ic", "alpha",
                opts.alphaId);
        String lifecycleGauge = MetricsRegistry.labeled("alpha_lifecycle_state",
                "alpha", opts.alphaId);
        reg.gauge(lifecycleGauge).set(lifecycle.state().code());

        BacktestEngine.Strategy strategy = vec -> {
            reg.counter("alpha_signals_total").inc();
            AlphaSignal sig = Alphas.scoreRow(alphaParams, vec);
            lastSignalTs[0] = vec.timestamp;
            if (sig.confidence() > 0.0) {
                double psi = drift.onSignal(opts.alphaId,
                        sig.expectedReturn());
                if (!Double.isNaN(psi)) {
                    reg.gauge(driftGauge).set(psi);
                }
                if (vec.valid[Features.MID_PRICE]) {
                    rollingIc.onObservation(vec.timestamp,
                            sig.expectedReturn(),
                            vec.values[Features.MID_PRICE]);
                }
                double ic = rollingIc.ic(vec.timestamp);
                if (!Double.isNaN(ic)) {
                    reg.gauge(icGauge).set(ic);
                }
            }
            // One lifecycle evaluation per event-time adaptive block.
            // Note (documented divergence from the Python reference walk-forward):
            // if the feed is silent across MULTIPLE block boundaries, this live
            // loop evaluates once at the newest boundary, whereas
            // iap.backtest.adaptive evaluates every boundary. On such gaps a
            // live retirement can therefore lag the reference by up to the
            // number of skipped boundaries; acceptable for a monitoring gauge.
            long block = Math.floorDiv(vec.timestamp, blockNs);
            if (lifecycleBlock[0] == Long.MIN_VALUE) {
                lifecycleBlock[0] = block;
            } else if (block != lifecycleBlock[0]) {
                lifecycleBlock[0] = block;
                // evaluate at the block boundary the event just crossed
                reg.gauge(lifecycleGauge).set(lifecycle
                        .update(rollingIc.ic(block * blockNs)).code());
            }
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

        // ---- hard risk wiring (PLATFORM_CONVENTIONS.md §11.4, pinned) ----
        RiskWiring wiring = new RiskWiring(risk, "PAPER", opts.venueId, reg);
        wiring.riskOrderSeq = st.riskOrderSeq;
        double unit = exec.instrument(opts.instrumentId).qtyUnit();
        ExecMetrics execMetrics = new ExecMetrics(wiring, reg, engineHolder,
                exec);
        BacktestEngine.RiskHook hook = wiring.hook();

        BacktestEngine.FxConverter fx = marketFxConverter(cfg, engineHolder);
        BacktestEngine engine = new BacktestEngine(exec, strategy, hook,
                execMetrics, fx, opts.maxPos, opts.venueId, cfg.executionLimits(),
                cfg.sorOptions());
        engineHolder[0] = engine;

        // ------------------------------------------- probes + admin + server
        res.eventsProcessed = st.eventCursor;
        res.eventsPending = events.size() - startIndex;
        res.lastEventWallNs = System.nanoTime();
        Object checkpointLock = new Object();
        AdminService admin = new AdminService(risk, store, reg, adminToken,
                () -> res.lastEventTs,
                () -> res.state == SessionState.RUNNING);
        res.admin = admin;
        long staleFeedNs = limits.staleFeedTimeoutNs();
        MetricsServer server = null;
        if (opts.port >= 0) {
            server = new MetricsServer(reg, opts.port, () -> statusJson(res, opts),
                    () -> health(res), () -> ready(res, opts, staleFeedNs),
                    admin.enabled() ? admin : null);
            server.start();
            res.httpPort = server.port();
        }
        if (opts.onServerStarted != null) {
            opts.onServerStarted.accept(res);
        }

        // ------------------------------------------------ pre-resolved series
        Counter cEvents = reg.counter("md_events_total");
        Counter cGaps = reg.counter("md_sequence_gaps_total");
        Counter cDups = reg.counter("md_duplicates_total");
        Gauge gEventTime = reg.gauge("md_last_event_unixtime");
        Gauge gWallTime = reg.gauge("md_last_event_wallclock_unixtime");
        Gauge gGap = reg.gauge("md_event_time_gap_seconds");
        Gauge gGross = reg.gauge("portfolio_gross_notional");
        Gauge gNet = reg.gauge("portfolio_net_notional");
        Gauge gDrawdown = reg.gauge("portfolio_drawdown");
        Gauge gStale = reg.gauge(MetricsRegistry.labeled("book_stale",
                "instrument", Long.toString(opts.instrumentId)));
        Gauge gSessionState = reg.gauge("platform_session_state");
        Histogram hBook = reg.histogram("book_update_latency_ns");
        cEvents.add(st.eventCursor);
        reg.gauge(MetricsRegistry.labeled("platform_mode", "mode", "asap"))
                .set(opts.realtime ? 0.0 : 1.0);
        reg.gauge(MetricsRegistry.labeled("platform_mode", "mode", "realtime"))
                .set(opts.realtime ? 1.0 : 0.0);
        execMetrics.counters(st.ordersSubmitted, st.fillCount);
        gSessionState.set(SessionState.STARTING.code());

        // Mutable holders so the shutdown hook checkpoints the same live
        // values the trading loop advances (§12.3).
        long[] progress = {st.eventCursor};   // processed events
        // Audit bookkeeping: auditBase is the number of lines a PREVIOUS leg
        // already wrote to the file; auditCursor is the index into THIS
        // process's engine audit (a resumed engine starts with an empty one).
        final long auditBase = st.auditLines;
        long[] auditCursor = {0};
        double[] equityPeak = {st.equityPeak};
        long prevReceiveTs = events.get(startIndex).receiveTs;
        long[] bookHealth = {0, 0}; // last exported {gaps, duplicates}
        Thread hook0 = null;
        try {
            final SessionStore.State stf = st;
            hook0 = new Thread(() -> {
                synchronized (checkpointLock) {
                    if (res.state != SessionState.RUNNING) {
                        return;
                    }
                    try {
                        stf.eventCursor = progress[0];
                        stf.riskOrderSeq = wiring.riskOrderSeq;
                        stf.fillCount = execMetrics.fills();
                        stf.ordersSubmitted = execMetrics.submitted();
                        auditCursor[0] = checkpoint(store, risk, stf,
                                auditBase, auditCursor[0], equityPeak[0]);
                    } catch (RuntimeException ignored) {
                        // a shutdown hook must never throw
                    }
                }
            }, "iap-checkpoint");
            Runtime.getRuntime().addShutdownHook(hook0);
            res.state = SessionState.RUNNING;
            gSessionState.set(SessionState.RUNNING.code());
            for (int i = startIndex; i < events.size(); i++) {
                MarketEvent ev = events.get(i);
                if (opts.realtime) {
                    long waitNs = (long) ((ev.receiveTs - prevReceiveTs)
                            / opts.speed);
                    sleepNs(waitNs);
                }
                long gapNs = ev.receiveTs - prevReceiveTs;
                prevReceiveTs = ev.receiveTs;
                // Admin commands (kill / clear / override / roll) are applied
                // on THIS thread at an event boundary, so the resulting
                // RiskEvent carries the current event time (§12.5).
                admin.drain();
                long t0 = System.nanoTime();
                engine.onEvent(ev);
                hBook.record(System.nanoTime() - t0);
                cEvents.inc();
                progress[0]++;
                gEventTime.set(Math.floorDiv(ev.receiveTs, 1_000_000_000L));
                gGap.set(gapNs / 1e9);
                long nowNs = System.nanoTime();
                long nowUnix = System.currentTimeMillis() / 1000L;
                gWallTime.set(nowUnix);
                res.lastEventTs = ev.receiveTs;
                res.lastEventWallNs = nowNs;
                res.lastEventWallUnix = nowUnix;
                res.eventsProcessed = progress[0];
                res.eventsPending = events.size() - (i + 1);
                res.halted = risk.killSwitchEngaged();
                // exposure / drawdown gauges (same money unit as risk, §12.1)
                BacktestEngine.Account a =
                        engine.accounts().get(opts.instrumentId);
                if (a != null && a.markValid) {
                    double notional = a.position * unit * a.mark;
                    gGross.set(Math.abs(notional));
                    gNet.set(notional);
                    double equity = a.equity(unit);
                    equityPeak[0] = Math.max(equityPeak[0], equity);
                    gDrawdown.set(equityPeak[0] - equity);
                }
                // Sequence gaps / duplicates are exported on EVERY event
                // (§12.6): max_sequence_gap_before_halt = 1, so a single gap
                // must be visible on the very next scrape.
                sampleBookHealth(cGaps, cDups, gStale, engine, opts.instrumentId,
                        bookHealth);
                if (opts.checkpointEveryEvents > 0
                        && progress[0] % opts.checkpointEveryEvents == 0) {
                    gc.sample();
                    synchronized (checkpointLock) {
                        st.eventCursor = progress[0];
                        st.riskOrderSeq = wiring.riskOrderSeq;
                        st.fillCount = execMetrics.fills();
                        st.ordersSubmitted = execMetrics.submitted();
                        auditCursor[0] = checkpoint(store, risk, st,
                                auditBase, auditCursor[0], equityPeak[0]);
                    }
                }
            }
            BacktestEngine.Summary summary;
            synchronized (checkpointLock) {
                summary = engine.finish();
                sampleBookHealth(cGaps, cDups, gStale, engine, opts.instrumentId,
                        bookHealth);
                gc.sample();
            }
            res.eventsProcessed = progress[0];
            res.fillCount = baseFills + summary.fillCount;
            BacktestEngine.Account a = summary.accounts.get(opts.instrumentId);
            res.ordersSubmitted = baseOrders
                    + (a == null ? 0 : a.ordersSubmitted);
            res.totalPnl = basePnl + summary.totalPnl;
            res.grossPnl = baseGross + summary.grossPnl;
            res.feesNet = summary.feesNet;
            res.impact = summary.impact;
            res.spreadCost = summary.spreadCost;
            res.riskDecisions = reg.counterValue("risk_decisions_total");
            res.riskAllowed = reg.counterValue("risk_allowed_total");
            res.riskRejected = reg.counterValue("risk_rejected_total");
            res.riskEvents = reg.counterValue("risk_events_total");
            res.riskRejectedStale = risk.audit().stream()
                    .filter(e -> e.ruleId().equals(com.iap.risk.Rules.STALE_PRICE)
                            || e.ruleId().equals(com.iap.risk.Rules.SEQUENCE_GAP))
                    .count();
            res.killSwitchEngaged = risk.killSwitchEngaged();
            res.halted = res.killSwitchEngaged;
            res.riskAudit = risk;
            res.counters = summary.counters;
            res.driftPsi = drift.psi(opts.alphaId);
            res.rollingIc = lastSignalTs[0] == Long.MIN_VALUE ? Double.NaN
                    : rollingIc.ic(lastSignalTs[0]);
            // session end closes the last partial adaptive block: one
            // final lifecycle evaluation so the report reflects it
            res.lifecycle = lifecycle.update(res.rollingIc);
            reg.gauge(lifecycleGauge).set(res.lifecycle.code());
            if (!Double.isNaN(res.rollingIc)) {
                reg.gauge(icGauge).set(res.rollingIc);
            }
            res.metrics = reg;
            // Final checkpoint FIRST: the report records the audit file's
            // sha256, so every audit line must already be on disk. The state
            // on disk then describes a COMPLETED session (cursor at the end),
            // so a resume of it is refused.
            synchronized (checkpointLock) {
                st.eventCursor = progress[0];
                st.riskOrderSeq = wiring.riskOrderSeq;
                st.totalPnl = res.totalPnl;
                st.grossPnl = res.grossPnl;
                st.fillCount = res.fillCount;
                st.ordersSubmitted = res.ordersSubmitted;
                st.configSha256 = res.configSha256;
                checkpoint(store, risk, st, auditBase, auditCursor[0],
                        equityPeak[0]);
                res.state = SessionState.FINISHED;
                gSessionState.set(SessionState.FINISHED.code());
            }
            res.reportJson = reportJson(res, opts, reg, store);
            if (opts.reportPath != null) {
                Files.createDirectories(
                        opts.reportPath.toAbsolutePath().getParent());
                Files.write(opts.reportPath,
                        res.reportJson.getBytes(StandardCharsets.UTF_8));
            }
        } catch (RuntimeException | IOException e) {
            res.state = SessionState.FAILED;
            res.failureReason = String.valueOf(e.getMessage());
            gSessionState.set(SessionState.FAILED.code());
            throw e;
        } finally {
            admin.shutdown();
            if (hook0 != null) {
                try {
                    Runtime.getRuntime().removeShutdownHook(hook0);
                } catch (IllegalStateException ignored) {
                    // already shutting down
                }
            }
            if (server != null) {
                server.stop();
            }
        }
        return res;
    }

    /**
     * Persist one checkpoint (§12.3), in commit order: audit lines, then the
     * risk snapshot, then {@code session_state.json} last — the state file is
     * the commit point, so a crash between writes leaves a consistent (older)
     * checkpoint rather than a cursor pointing past unsaved risk state.
     *
     * @return the new audit-line cursor
     */
    private static long checkpoint(SessionStore store, RiskEngine risk,
            SessionStore.State st, long auditBase, long auditCursor,
            double equityPeak) {
        List<RiskEvent> audit = risk.audit();
        StringBuilder sb = new StringBuilder(256);
        for (int i = (int) auditCursor; i < audit.size(); i++) {
            sb.append(audit.get(i).toJsonLine()).append('\n');
        }
        store.appendJsonl(SessionStore.RISK_AUDIT, sb.toString());
        st.auditLines = auditBase + audit.size();
        st.equityPeak = equityPeak;
        store.writeAtomic(SessionStore.RISK_SNAPSHOT, risk.snapshot());
        store.writeAtomic(SessionStore.SESSION_STATE, st.toJson());
        return audit.size();
    }

    /** Export the live risk limits as gauges so alerts never hard-code them. */
    private static void exportRiskLimits(MetricsRegistry reg, RiskLimits l) {
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_daily_loss")).set(l.maxDailyLoss());
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_strategy_daily_loss")).set(l.strategyMaxDailyLoss());
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_gross_notional")).set(l.maxGrossNotional());
        reg.gauge(MetricsRegistry.labeled("risk_limit", "limit",
                "max_net_notional")).set(l.maxNetNotional());
    }

    // ------------------------------------------------------------- probes --

    /**
     * Liveness (§12.5): 503 only when the process cannot make progress —
     * a failed session, or a running loop whose {@code events_processed} has
     * not advanced for {@link #LIVENESS_STALL_NS} while events are pending.
     * A latched kill switch is NOT unhealthy (halted ≠ dead).
     */
    public static MetricsServer.HttpResult health(Result res) {
        SessionState state = res.state;
        if (state == SessionState.FAILED) {
            return new MetricsServer.HttpResult(503,
                    "{\"reason\":\"" + esc(res.failureReason)
                            + "\",\"status\":\"failed\"}");
        }
        if (state == SessionState.RUNNING && res.eventsPending > 0
                && System.nanoTime() - res.lastEventWallNs > LIVENESS_STALL_NS) {
            return new MetricsServer.HttpResult(503,
                    "{\"reason\":\"trading loop has not advanced in "
                            + (LIVENESS_STALL_NS / 1_000_000_000L)
                            + "s\",\"status\":\"stalled\"}");
        }
        return new MetricsServer.HttpResult(200,
                "{\"session\":\"" + state.label() + "\",\"status\":\"ok\""
                        + ",\"trading\":\"" + (res.halted ? "halted" : "active")
                        + "\"}");
    }

    /**
     * Readiness (§12.5): 503 before the first event, after the session has
     * finished, and — in realtime mode — when no event has been processed
     * within {@code stale_feed_timeout_ns} of wall clock. A halted (killed)
     * session is still ready: it is deliberately not trading, not broken.
     */
    public static MetricsServer.HttpResult ready(Result res, Options opts,
            long staleFeedNs) {
        SessionState state = res.state;
        if (state != SessionState.RUNNING) {
            return new MetricsServer.HttpResult(503,
                    "{\"reason\":\"session " + state.label()
                            + "\",\"status\":\"not_ready\"}");
        }
        if (opts.realtime) {
            long ageNs = System.nanoTime() - res.lastEventWallNs;
            if (res.eventsProcessed == 0 || ageNs > staleFeedNs) {
                return new MetricsServer.HttpResult(503,
                        "{\"feed_age_ns\":" + ageNs
                                + ",\"reason\":\"stale_feed\""
                                + ",\"status\":\"not_ready\"}");
            }
        }
        return new MetricsServer.HttpResult(200,
                "{\"status\":\"ready\",\"trading\":\""
                        + (res.halted ? "halted" : "active") + "\"}");
    }

    /**
     * The production risk wiring (PLATFORM_CONVENTIONS.md §11.4, pinned) —
     * the ONLY path between the backtest engine and the hard risk engine:
     * <ul>
     *   <li>{@code onFill}: every fill (with the child's order id) reaches
     *       {@code risk.onFill} BEFORE the next decision;</li>
     *   <li>{@code onOrderTerminal}: every FILLED/CANCELLED child reaches
     *       {@code risk.onOrderDone} (open-order tracking);</li>
     *   <li>{@code onMarket}: per-venue stale transitions become
     *       {@code onSequenceGap} / {@code onFeedRecovered}; the reference
     *       price is the best bid/ask over NON-STALE venue books, stamped
     *       with the oldest market-data time (last applied non-HEARTBEAT
     *       event) among the venues at the consolidated touch — a frozen
     *       venue that still forms the touch ages the mark (STALE_PRICE
     *       after stale_feed_timeout_ns), a heartbeat never refreshes it,
     *       and no fresh two-sided venue leaves the previous mark to
     *       age;</li>
     *   <li>{@link #hook()}: the pre-trade check runs with a fresh
     *       strategy-side order id (consumed even when rejected — the
     *       duplicate-id rule is session-wide), a MARKET order on the session
     *       venue, stamped with the decision event time; {@code
     *       onOrderSubmitted} maps the simulator's child id to that risk id,
     *       so fills and terminal reports reach the risk engine under the id
     *       it tracks as open.</li>
     * </ul>
     */
    public static final class RiskWiring implements BacktestEngine.ExecutionListener {
        private final RiskEngine risk;
        private final String strategyId;
        private final int venueId;
        private final MetricsRegistry reg;
        private final TreeMap<Integer, Boolean> venueStale = new TreeMap<>();
        /** venue -> exchange_ts of its last applied market-data event. */
        private final TreeMap<Integer, Long> lastDataTs = new TreeMap<>();
        /** simulator child id -> risk order id. */
        private final TreeMap<Long, Long> riskIdOf = new TreeMap<>();
        /**
         * Strategy-side order-id sequence. Package-private so a resumed
         * session continues it (§12.3): ids are never reused across a
         * restart, so the risk engine's duplicate-id rule stays sound.
         */
        long riskOrderSeq;
        private long pendingRiskId;

        public RiskWiring(RiskEngine risk, String strategyId, int venueId,
                MetricsRegistry reg) {
            this.risk = risk;
            this.strategyId = strategyId;
            this.venueId = venueId;
            this.reg = reg;
        }

        @Override
        public void onFill(Fill f) {
            Long rid = riskIdOf.get(f.orderId());
            risk.onFill(new RiskFill(f.ts(), strategyId, f.instrumentId(),
                    rid == null ? 0 : rid, f.side(), f.qty(), f.priceTicks()));
        }

        @Override
        public void onOrderSubmitted(com.iap.execution.ChildOrder o) {
            riskIdOf.put(o.orderId, pendingRiskId);
        }

        @Override
        public void onOrderTerminal(com.iap.execution.ChildOrder o) {
            Long rid = riskIdOf.remove(o.orderId);
            if (rid != null) {
                risk.onOrderDone(rid);
            }
        }

        @Override
        public void onMarket(MarketEvent ev, com.iap.orderbook.ConsolidatedBook book) {
            if (ev.eventType != com.iap.core.EventType.HEARTBEAT) {
                lastDataTs.put(ev.venueId, ev.exchangeTs);
            }
            long bestBid = Long.MIN_VALUE;
            long bestAsk = Long.MAX_VALUE;
            for (var e : book.venues().entrySet()) {
                OrderBook vb = e.getValue();
                boolean stale = vb.isStale();
                Boolean prev = venueStale.put(e.getKey(), stale);
                boolean was = prev != null && prev;
                if (stale && !was) {
                    risk.onSequenceGap(ev.instrumentId, ev.exchangeTs);
                } else if (!stale && was) {
                    risk.onFeedRecovered(ev.instrumentId, ev.exchangeTs);
                }
                if (stale) {
                    continue;
                }
                long[] bb = vb.bestBid();
                long[] ba = vb.bestAsk();
                if (bb != null) {
                    bestBid = Math.max(bestBid, bb[0]);
                }
                if (ba != null) {
                    bestAsk = Math.min(bestAsk, ba[0]);
                }
            }
            if (bestBid == Long.MIN_VALUE || bestAsk == Long.MAX_VALUE) {
                return; // no fresh two-sided venue: the previous mark ages
            }
            long markTs = Long.MAX_VALUE;
            for (var e : book.venues().entrySet()) {
                OrderBook vb = e.getValue();
                if (vb.isStale()) {
                    continue;
                }
                long[] bb = vb.bestBid();
                long[] ba = vb.bestAsk();
                boolean atTouch = (bb != null && bb[0] == bestBid)
                        || (ba != null && ba[0] == bestAsk);
                Long dataTs = lastDataTs.get(e.getKey());
                if (atTouch && dataTs != null) {
                    markTs = Math.min(markTs, dataTs);
                }
            }
            if (markTs == Long.MAX_VALUE) {
                return;
            }
            risk.onMarket(ev.instrumentId, bestBid, bestAsk, markTs);
        }

        /** The pre-trade hook for {@link BacktestEngine}. */
        public BacktestEngine.RiskHook hook() {
            return (iid, pos, inflight, delta, ts) -> {
                if (delta == 0) {
                    return 0;
                }
                long t0 = System.nanoTime();
                long orderId = ++riskOrderSeq;
                pendingRiskId = orderId;
                OrderRequest req = new OrderRequest(orderId, iid,
                        delta > 0 ? 0 : 1, Math.abs(delta), 0, OrderRequest.MARKET,
                        venueId, strategyId, 0.5, ts);
                RiskDecision d = risk.checkOrder(req);
                reg.histogram("order_path_latency_ns")
                        .record(System.nanoTime() - t0);
                if (!d.allowed()) {
                    reg.counter("exec_child_orders_rejected_total").inc();
                }
                return d.allowed() ? delta : 0;
            };
        }
    }

    /**
     * Execution observability decorator (PLATFORM_CONVENTIONS.md §12.6):
     * delegates every callback to the pinned {@link RiskWiring} unchanged and
     * additionally exports the platform's OWN execution counters —
     * {@code exec_orders_submitted_total}, {@code exec_fills_total} and the
     * {@code exec_slippage_bps} histogram (|fill − mark| in basis points,
     * integer-scaled ×100). These replace the venue simulator's counters,
     * which described a component the deployment never ran.
     */
    static final class ExecMetrics implements BacktestEngine.ExecutionListener {
        private final RiskWiring inner;
        private final Counter submitted;
        private final Counter fills;
        private final Histogram slippage;
        private final BacktestEngine[] engineHolder;
        private final ExecConfig exec;

        ExecMetrics(RiskWiring inner, MetricsRegistry reg,
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
     * FX converter for the backtest accounts: the rate of a quote currency
     * is the consolidated mid of the conversion pair named in
     * configs/risk.json {@code currency.conversion}, read from the
     * engine's own books (same source and inversion rule as the risk
     * engine); a missing pair or mark fails closed.
     */
    private static BacktestEngine.FxConverter marketFxConverter(
            ConfigService cfg, BacktestEngine[] engineHolder) {
        TreeMap<String, com.iap.risk.RiskLimits.FxConversion> table =
                cfg.fxConversion();
        TreeMap<Long, com.iap.execution.InstrumentSpec> instruments =
                cfg.instruments();
        return (ccy, ts) -> {
            if ("USD".equals(ccy)) {
                return 1.0;
            }
            com.iap.risk.RiskLimits.FxConversion conv = table.get(ccy);
            com.iap.execution.InstrumentSpec pair =
                    conv == null ? null : instruments.get(conv.instrumentId());
            if (pair == null) {
                throw new IllegalStateException("no conversion pair for " + ccy);
            }
            var book = engineHolder[0].simulator().instrumentBook(conv.instrumentId());
            long[] bb = book.bestBid();
            long[] ba = book.bestAsk();
            if (bb == null || ba == null) {
                throw new IllegalStateException("no conversion rate for " + ccy);
            }
            double mid = (double) (bb[0] + ba[0]) * pair.tickSize() / 2.0;
            return conv.invert() ? 1.0 / mid : mid;
        };
    }

    /**
     * Single-asset portfolio solve: |weight| in [0, 1] from EWMA variance.
     * An infeasible problem (API_PORTFOLIO_TCA.md §1.3) holds the previous
     * weight scale: the optimizer returns w_prev with {@code feasible ==
     * false} and the caller must not act on it.
     */
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
        if (!sol.feasible()) {
            return Math.min(Math.abs(positionWeight), 1.0); // hold
        }
        return Math.min(Math.abs(sol.weights()[0]), 1.0);
    }

    /**
     * Export book-health counters. Called on EVERY event (§12.6): the risk
     * config halts after ONE sequence gap, so a gap must be visible on the
     * next scrape rather than at the next 1,024-event sampling point.
     */
    private static void sampleBookHealth(Counter gaps, Counter dups,
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

    private static String esc(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "\\r");
    }

    /**
     * The {@code /status} body (§12.5): sorted keys, live progress, and the
     * fields runbooks tell operators to read.
     */
    public static String statusJson(Result res, Options opts) {
        return "{\"alpha_id\":\"" + esc(opts.alphaId)
                + "\",\"component\":\"paper_trading\""
                + ",\"config_sha256\":\"" + esc(res.configSha256)
                + "\",\"events_pending\":" + res.eventsPending
                + ",\"events_processed\":" + res.eventsProcessed
                + ",\"instrument_id\":" + opts.instrumentId
                + ",\"kill_switch_engaged\":" + res.halted
                + ",\"last_event_ts\":" + res.lastEventTs
                + ",\"last_event_wallclock\":" + res.lastEventWallUnix
                + ",\"lifecycle\":\"" + res.lifecycle
                + "\",\"mode\":\"" + (opts.realtime ? "realtime" : "asap")
                + "\",\"restarts\":" + res.restarts
                + ",\"status\":\"" + res.state.label() + "\"}";
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

    /** Finite double as JSON number, NaN as JSON {@code null}. */
    private static String numOrNull(double v) {
        return Double.isNaN(v) ? "null" : Double.toString(v);
    }

    private static String reportJson(Result res, Options opts,
            MetricsRegistry reg, SessionStore store) {
        StringBuilder sb = new StringBuilder(768);
        sb.append("{\"adaptive\":{\"drift_psi\":")
                .append(numOrNull(res.driftPsi))
                .append(",\"lifecycle\":\"").append(res.lifecycle)
                .append("\",\"lifecycle_code\":").append(res.lifecycle.code())
                .append(",\"rolling_ic\":").append(numOrNull(res.rollingIc))
                .append("},\"alpha_id\":\"").append(esc(opts.alphaId))
                .append("\",\"config_sha256\":\"").append(esc(res.configSha256))
                .append("\",\"events_processed\":").append(res.eventsProcessed)
                .append(",\"execution\":{\"fills\":")
                .append(reg.counterValue("exec_fills_total"))
                .append(",\"latency_budget_blocked\":")
                .append(res.counters.latencyBudgetBlocked)
                .append(",\"orders_submitted\":")
                .append(reg.counterValue("exec_orders_submitted_total"))
                .append(",\"participation_blocked\":").append(res.counters.participationBlocked)
                .append(",\"participation_capped\":").append(res.counters.participationCapped)
                .append(",\"rejected_pre_trade\":")
                .append(reg.counterValue("exec_child_orders_rejected_total"))
                .append(",\"slice_interval_blocked\":").append(res.counters.sliceIntervalBlocked)
                .append(",\"sor_no_route\":").append(res.counters.sorNoRoute)
                .append("},\"fills\":").append(res.fillCount)
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
                .append("},\"restarts\":").append(res.restarts)
                .append(",\"risk\":{\"allowed\":").append(res.riskAllowed)
                .append(",\"audit_jsonl\":\"")
                .append(esc(store.file(SessionStore.RISK_AUDIT).toString()))
                .append("\",\"audit_sha256\":\"")
                .append(com.iap.codec.Sha256.hex(store.read(
                        SessionStore.RISK_AUDIT).getBytes(StandardCharsets.UTF_8)))
                .append("\",\"decisions\":").append(res.riskDecisions)
                .append(",\"events\":").append(res.riskEvents)
                .append(",\"rejected\":").append(res.riskRejected)
                .append(",\"rejected_stale\":").append(res.riskRejectedStale)
                .append("},\"state_dir\":\"")
                .append(esc(store.dir().toString()))
                .append("\",\"status\":\"").append(res.state.label())
                .append("\",\"x-version\":2}");
        return sb.toString();
    }

    /** CLI: see java/paper.sh for the flags. */
    public static void main(String[] args) throws IOException {
        Options opts = new Options();
        Path explicitConfigs = null;
        opts.reportPath = Paths.get("out", "paper_session_report.json");
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--configs" -> explicitConfigs = Paths.get(need(args, ++i, a));
                case "--events" -> opts.eventsFile = Paths.get(need(args, ++i, a));
                case "--instrument" ->
                        opts.instrumentId = parseLong(need(args, ++i, a), a);
                case "--alpha" -> opts.alphaId = need(args, ++i, a);
                case "--mode" -> opts.realtime = switch (need(args, ++i, a)) {
                    case "realtime" -> true;
                    case "asap" -> false;
                    default -> throw new IllegalArgumentException(
                            "--mode must be asap|realtime");
                };
                case "--speed" -> opts.speed = parseDouble(need(args, ++i, a), a);
                case "--max-events" ->
                        opts.maxEvents = parseLong(need(args, ++i, a), a);
                case "--port" -> {
                    String v = need(args, ++i, a);
                    opts.port = "config".equals(v) ? -2 : (int) parseLong(v, a);
                }
                case "--report" -> opts.reportPath = Paths.get(need(args, ++i, a));
                case "--baselines" ->
                        opts.baselinesDir = Paths.get(need(args, ++i, a));
                case "--state-dir" -> opts.stateDir = Paths.get(need(args, ++i, a));
                case "--resume" -> opts.resume = true;
                case "--checkpoint-every" -> opts.checkpointEveryEvents =
                        parseLong(need(args, ++i, a), a);
                default -> throw new IllegalArgumentException(
                        "unknown argument " + a);
            }
        }
        // Config directory: --configs, else $IAP_CONFIG_DIR, else the default
        // (PLATFORM_CONVENTIONS.md §12.2).
        opts.configsDir = ConfigService.resolveDir(explicitConfigs,
                System.getenv(), opts.configsDir);
        if (opts.stateDir == null) {
            String env = System.getenv("IAP_STATE_DIR");
            if (env != null && !env.isBlank()) {
                opts.stateDir = Paths.get(env.trim());
            }
        }
        if (opts.port == -2) { // sentinel: from config
            opts.port = new ConfigService(opts.configsDir).monitoringPort();
        }
        validate(opts);
        Result res = run(opts);
        System.out.println(String.format(Locale.ROOT,
                "paper session: events=%d orders=%d fills=%d pnl=%.6f "
                        + "risk[allowed=%d rejected=%d] status=%s "
                        + "port=%d state=%s report=%s",
                res.eventsProcessed, res.ordersSubmitted, res.fillCount,
                res.totalPnl, res.riskAllowed, res.riskRejected,
                res.state.label().toUpperCase(Locale.ROOT), res.httpPort,
                res.stateDir, opts.reportPath));
    }

    private static String need(String[] args, int i, String flag) {
        if (i >= args.length) {
            throw new IllegalArgumentException(flag + " needs a value");
        }
        return args[i];
    }

    private static long parseLong(String v, String flag) {
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    flag + " must be an integer, got " + v);
        }
    }

    private static double parseDouble(String v, String flag) {
        try {
            return Double.parseDouble(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    flag + " must be a number, got " + v);
        }
    }
}
