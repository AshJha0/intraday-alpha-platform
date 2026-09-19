package com.iap.platform;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.backtest.BacktestEngine;
import com.iap.codec.Sha256;
import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.SessionStatus;
import com.iap.execution.ChildOrder;
import com.iap.execution.ExecConfig;
import com.iap.execution.ExecutionSimulator;
import com.iap.execution.Fill;
import com.iap.execution.OrderState;
import com.iap.execution.VenueSpec;
import com.iap.features.Features;
import com.iap.monitoring.Counter;
import com.iap.monitoring.MetricsRegistry;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;
import com.iap.risk.Decision;
import com.iap.risk.RiskDecision;
import com.iap.risk.Rules;
import com.iap.sor.SorOptions;
import com.iap.tca.MarketTimeline;
import com.iap.tca.Tca;
import com.iap.tca.TcaParentOrder;
import com.iap.tca.TcaService;
import com.iap.trace.AlphaSignalRec;
import com.iap.trace.Attribution;
import com.iap.trace.ChildOrderRec;
import com.iap.trace.DecisionTrace;
import com.iap.trace.ExecutionReportRec;
import com.iap.trace.JsonlTraceSink;
import com.iap.trace.ParentOrderRec;
import com.iap.trace.PortfolioTargetRec;
import com.iap.trace.RiskDecisionRec;
import com.iap.trace.TCAResultRec;
import com.iap.trace.TraceStages;
import com.iap.trace.VenueDecisionRec;

/**
 * Decision-trace recorder of the paper-trading vertical: builds one
 * {@link DecisionTrace} per decision cycle and hands it to a
 * {@link JsonlTraceSink} ({@code <state-dir>/decision_traces.jsonl}).
 *
 * <p>A decision cycle is one pre-trade risk decision of the platform
 * (PLATFORM_CONVENTIONS.md §11.4): the alpha signal and the sizing target
 * that produced the order delta, the hard-risk decision, and — when the
 * order was allowed — the single MARKET child the engine sends (recorded as
 * a one-child {@code IS} parent order), the routing view of every
 * configured venue, every fill / terminal report, the TCA record at the
 * parent's end and the pinned P&amp;L attribution. A REJECT / KILL trace is
 * emitted at once (signal, target, risk; no orders); an allowed trace is
 * emitted when its child reaches a terminal state and the market timeline
 * has caught up with it, so the stream order is a pure function of the
 * event stream (same events + configs + seed ⇒ same digest).
 *
 * <p>Pinned mappings (documented in the port notes): {@code session_id} =
 * {@code paper-<alpha>-<instrument>-<data_version[:16]>}; {@code data_version}
 * = sha256 of the event file bytes; {@code feature_version} = sha256 of the
 * Java engine's pinned slot table; {@code model_version} = sha256 of
 * {@code alpha_params.json}; {@code config_version} = the ConfigService
 * digest; {@code sequence} = the triggering market event's sequence. TCA bps
 * figures are in bps of {@code qty × decision mid} (the TCA reference
 * convention); an interval benchmark with no market prints falls back to
 * the arrival mid; a parent whose window the timeline cannot cover (no
 * prevailing state) carries no TCA stage and is counted
 * ({@code trace_tca_skipped_total}), never a guessed number.
 */
public final class PaperTraces implements BacktestEngine.ExecutionListener {
    /** State file name of the trace stream. */
    public static final String DECISION_TRACES = "decision_traces.jsonl";

    /** Strategy id every paper decision is booked under. */
    public static final String STRATEGY_ID = "PAPER";

    /** Pinned check order of the hard risk engine (rule_id → rule_index). */
    private static final String[] RULE_ORDER = {Rules.CONFIG_MISSING,
        Rules.KILL_GLOBAL, Rules.KILL_STRATEGY, Rules.KILL_INSTRUMENT,
        Rules.KILL_VENUE, Rules.MALFORMED_ORDER, Rules.UNKNOWN_INSTRUMENT,
        Rules.DUPLICATE_ORDER_ID, Rules.VENUE_DISCONNECTED, Rules.SEQUENCE_GAP,
        Rules.STALE_PRICE, Rules.FAT_FINGER_QTY, Rules.FX_RATE_MISSING,
        Rules.FAT_FINGER_NOTIONAL, Rules.PRICE_BAND, Rules.RATE_THROTTLE,
        Rules.SELF_MATCH, Rules.POSITION_LIMIT, Rules.INSTRUMENT_NOTIONAL,
        Rules.GROSS_NOTIONAL, Rules.NET_NOTIONAL, Rules.DAILY_LOSS,
        Rules.STRATEGY_LOSS};

    /** The registry hash of the Java engine's pinned feature slot table. */
    public static String engineFeatureVersion() {
        StringBuilder sb = new StringBuilder(2048);
        for (int i = 0; i < Features.COUNT; i++) {
            sb.append(Features.name(i)).append('\n');
        }
        return Sha256.hex(sb.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    /** {@code rule_index} of a risk rule id (−1 for ALLOW). */
    public static int ruleIndex(String ruleId) {
        for (int i = 0; i < RULE_ORDER.length; i++) {
            if (RULE_ORDER[i].equals(ruleId)) {
                return i;
            }
        }
        if (Rules.NOT_BOOTSTRAPPED.equals(ruleId)) {
            return 0; // shares check 0 with CONFIG_MISSING (pinned)
        }
        throw new IllegalArgumentException("risk rule without a pinned index: " + ruleId);
    }

    /** One decision cycle under construction. */
    private static final class Open {
        long eventTs;
        long sequence;
        AlphaSignalRec signal;
        PortfolioTargetRec portfolio;
        RiskDecisionRec risk;
        long riskOrderId;
        int side;
        long qty;
        long volumeAtDecision;
        double expectedReturn;
        ChildOrderRec child;
        VenueDecisionRec routing;
        long arrivalTs;
        long endTs;
        /** End of the TCA window ({@code endTs}, or the last market state at session end). */
        long tcaEndTs;
        final List<ExecutionReportRec> reports = new ArrayList<>();
        final List<Fill> fills = new ArrayList<>();

        /** Execution ids: {@code risk order id × 100 + report ordinal} (unique across resumes). */
        long nextExecutionId() {
            return riskOrderId * 100 + reports.size() + 1;
        }
    }

    private final JsonlTraceSink sink;
    private final BacktestEngine.ExecutionListener inner;
    private final ExecConfig exec;
    private final SorOptions sor;
    private final long instrumentId;
    private final String alphaId;
    private final String sessionId;
    private final String dataVersion;
    private final String featureVersion;
    private final String modelVersion;
    private final String configVersion;
    private final long horizonNs;
    private final int pinnedVenueId;
    private final double tickSize;
    private final double qtyUnit;
    private final BacktestEngine[] engineHolder;
    private final Counter records;
    private final Counter tcaSkipped;
    private final MarketTimeline timeline = new MarketTimeline();
    private final TreeMap<Long, Open> openByChild = new TreeMap<>();
    private final ArrayDeque<Open> completed = new ArrayDeque<>();
    private long currentEventTs = Long.MIN_VALUE;
    private long currentSequence;
    private AlphaSignalRec pendingSignal;
    private PortfolioTargetRec pendingPortfolio;
    private double pendingExpectedReturn;
    private Open pendingDecision;

    /**
     * @param inner the listener every callback is delegated to first (the
     *     risk wiring / metrics decorator) — traces observe, never decide
     */
    public PaperTraces(JsonlTraceSink sink, BacktestEngine.ExecutionListener inner,
            ExecConfig exec, SorOptions sor, long instrumentId, String alphaId,
            String dataVersion, String modelVersion, String configVersion,
            long horizonNs, int pinnedVenueId, BacktestEngine[] engineHolder,
            MetricsRegistry reg) {
        this.sink = sink;
        this.inner = inner;
        this.exec = exec;
        this.sor = sor;
        this.instrumentId = instrumentId;
        this.alphaId = alphaId;
        this.dataVersion = dataVersion;
        this.featureVersion = engineFeatureVersion();
        this.modelVersion = modelVersion;
        this.configVersion = configVersion;
        this.horizonNs = horizonNs;
        this.pinnedVenueId = pinnedVenueId;
        this.tickSize = exec.instrument(instrumentId).tickSize();
        this.qtyUnit = exec.instrument(instrumentId).qtyUnit();
        this.engineHolder = engineHolder;
        this.sessionId = "paper-" + alphaId + "-" + instrumentId + "-"
                + dataVersion.substring(0, 16);
        this.records = reg.counter("trace_records_total");
        this.tcaSkipped = reg.counter("trace_tca_skipped_total");
        this.records.add(sink.count()); // a resumed session continues the count
    }

    /** The session id every trace of this session carries. */
    public String sessionId() {
        return sessionId;
    }

    /** The sink (digest, count, flush). */
    public JsonlTraceSink sink() {
        return sink;
    }

    /** Called by the trading loop before each event reaches the engine. */
    public void beginEvent(MarketEvent ev) {
        currentEventTs = ev.exchangeTs;
        currentSequence = ev.sequence;
    }

    /**
     * The strategy produced a signal and a target for this event
     * ({@code portfolio} may be null before the first solve).
     */
    public void onSignal(long timestamp, double expectedReturn, double confidence,
            long targetQty, long prevQty, PortfolioTargetRec portfolio) {
        int direction = expectedReturn > 0.0 ? 1 : expectedReturn < 0.0 ? -1 : 0;
        pendingSignal = new AlphaSignalRec(timestamp, instrumentId, expectedReturn,
                confidence, horizonNs, direction, alphaId);
        pendingExpectedReturn = expectedReturn;
        pendingPortfolio = portfolio;
    }

    /** A pre-trade risk decision was taken for a delta of this event. */
    public void onRiskDecision(long riskOrderId, int side, long qty, long ts,
            RiskDecision d) {
        Open o = new Open();
        o.eventTs = ts;
        o.sequence = currentSequence;
        o.signal = pendingSignal;
        o.portfolio = pendingPortfolio;
        o.expectedReturn = pendingExpectedReturn;
        o.riskOrderId = riskOrderId;
        o.side = side;
        o.qty = qty;
        int decision = d.decision().code();
        o.risk = new RiskDecisionRec(riskOrderId, STRATEGY_ID, instrumentId, ts,
                decision, d.decision() == Decision.ALLOW ? "" : d.ruleId(),
                d.decision() == Decision.ALLOW ? -1 : ruleIndex(d.ruleId()), d.reason());
        if (d.allowed()) {
            pendingDecision = o;
        } else {
            emit(o);
        }
    }

    // ---- ExecutionListener (delegating) -----------------------------------

    @Override
    public void onOrderSubmitted(ChildOrder c) {
        inner.onOrderSubmitted(c);
        Open o = pendingDecision;
        pendingDecision = null;
        if (o == null || c.instrumentId != instrumentId) {
            return;
        }
        BacktestEngine eng = engineHolder[0];
        BacktestEngine.Account a = eng.accounts().get(instrumentId);
        o.volumeAtDecision = a == null ? 0 : a.sessionVolume;
        o.arrivalTs = c.arrivalTs;
        o.child = new ChildOrderRec(c.orderId, o.riskOrderId, instrumentId, c.venueId,
                c.side, c.qty, 0, ChildOrderRec.MARKET, c.decisionTs, c.expireTs, 0);
        o.routing = routing(c, eng.simulator().instrumentBook(instrumentId));
        openByChild.put(c.orderId, o);
    }

    @Override
    public void onFill(Fill f) {
        inner.onFill(f);
        Open o = openByChild.get(f.orderId());
        if (o == null) {
            return;
        }
        o.fills.add(f);
        long remaining = o.qty;
        for (Fill x : o.fills) {
            remaining -= x.qty();
        }
        o.reports.add(new ExecutionReportRec(f.orderId(), o.nextExecutionId(),
                remaining == 0 ? ExecutionReportRec.FILLED : ExecutionReportRec.PARTIAL,
                f.qty(), f.priceTicks(), f.venueId(), f.ts(), f.ts(), f.fee()));
    }

    @Override
    public void onOrderTerminal(ChildOrder c) {
        inner.onOrderTerminal(c);
        Open o = openByChild.remove(c.orderId);
        if (o == null) {
            return;
        }
        long ts = Math.max(currentEventTs, o.arrivalTs);
        if (c.state == OrderState.CANCELLED && c.remaining > 0) {
            o.reports.add(new ExecutionReportRec(c.orderId, o.nextExecutionId(),
                    ExecutionReportRec.CANCELED, 0, 0, c.venueId, ts, ts, 0.0));
        }
        o.endTs = ts;
        completed.addLast(o);
    }

    @Override
    public void onMarket(MarketEvent ev, ConsolidatedBook book) {
        inner.onMarket(ev, book);
        if (ev.instrumentId != instrumentId) {
            return;
        }
        // the TCA timeline, built by the pinned TcaService rule
        if (ev.eventType == EventType.TRADE) {
            timeline.addTrade(ev.exchangeTs, (double) ev.priceTicks * tickSize, ev.qty);
        }
        if (ev.eventType == EventType.STATUS && ev.qty == SessionStatus.HALT) {
            timeline.addHalt(ev.exchangeTs);
        }
        long[] bb = book.bestBid();
        long[] ba = book.bestAsk();
        if (bb != null && ba != null) {
            timeline.appendStatePinned(ev.exchangeTs, (double) bb[0] * tickSize,
                    (double) ba[0] * tickSize, bb[1], ba[1]);
        }
        drainCompleted();
    }

    /**
     * Session end: every child is terminal. A parent whose window the
     * timeline never reached (the stream ended first) is analysed over
     * {@code [arrival, last market state]} when that covers all its fills,
     * else emitted without a TCA stage (counted).
     */
    public void finish() {
        for (Open o : completed) {
            o.tcaEndTs = timeline.size() == 0 ? Long.MIN_VALUE
                    : Math.min(o.endTs, timeline.lastTs());
            emit(o);
        }
        completed.clear();
        if (!openByChild.isEmpty()) {
            throw new IllegalStateException("decision traces still open at session end: "
                    + openByChild.keySet());
        }
    }

    /**
     * Emit completed parents in completion order once the market timeline
     * has reached their end (so the TCA end mid is a real state, never a
     * fabricated one); a parent that is not ready holds the ones behind it.
     */
    private void drainCompleted() {
        while (!completed.isEmpty()) {
            Open o = completed.peekFirst();
            if (timeline.size() == 0 || o.endTs > timeline.lastTs()) {
                return;
            }
            completed.removeFirst();
            o.tcaEndTs = o.endTs;
            emit(o);
        }
    }

    // ---- record construction --------------------------------------------

    /** One venue as the router saw it, before ranks are assigned. */
    private record Seen(int venueId, boolean eligible, long price, long qty,
            VenueSpec spec) {
    }

    private VenueDecisionRec routing(ChildOrder c, ConsolidatedBook book) {
        List<Seen> seen = new ArrayList<>();
        List<Seen> ranked = new ArrayList<>();
        for (Map.Entry<Integer, VenueSpec> e : exec.venues.entrySet()) {
            int vid = e.getKey();
            VenueSpec spec = e.getValue();
            OrderBook vb = book.venues().get(vid);
            long[] contra = vb == null ? null : (c.side == 0 ? vb.bestAsk() : vb.bestBid());
            boolean eligible = ExecutionSimulator.venueOpen(vb)
                    && spec.latencyMeanNs() <= sor.maxVenueLatencyNs() && contra != null;
            Seen v = new Seen(vid, eligible, contra == null ? 0 : contra[0],
                    contra == null ? 0 : contra[1], spec);
            seen.add(v);
            if (eligible) {
                ranked.add(v);
            }
        }
        // rank eligible venues by the aggressive-routing preference: best
        // displayed contra price, then taker fee, commission, venue id
        ranked.sort((a, b) -> {
            int cmp = c.side == 0 ? Long.compare(a.price(), b.price())
                    : Long.compare(b.price(), a.price());
            if (cmp == 0) {
                cmp = Double.compare(a.spec().takerFeePerShare(),
                        b.spec().takerFeePerShare());
            }
            if (cmp == 0) {
                cmp = Double.compare(a.spec().commissionPerMillion(),
                        b.spec().commissionPerMillion());
            }
            return cmp != 0 ? cmp : Integer.compare(a.venueId(), b.venueId());
        });
        List<VenueDecisionRec.VenueScore> out = new ArrayList<>(seen.size());
        boolean routedEligible = false;
        for (Seen v : seen) {
            int rank = ranked.indexOf(v) + 1;
            out.add(new VenueDecisionRec.VenueScore(v.venueId(), v.eligible(), v.price(),
                    v.qty(), v.spec().takerFeePerShare(), v.spec().makerRebatePerShare(),
                    v.spec().commissionPerMillion(), v.spec().latencyMeanNs(), rank));
            if (v.venueId() == c.venueId) {
                routedEligible = v.eligible();
            }
        }
        String reason;
        int venueId;
        if (pinnedVenueId != 0) {
            venueId = routedEligible ? c.venueId : 0;
            reason = routedEligible
                    ? "pinned venue " + pinnedVenueId + " (--venue), eligible"
                    : "pinned venue " + pinnedVenueId + " (--venue) not eligible at"
                            + " decision time: the child was sent as pinned and the"
                            + " simulator's venue gate decides";
        } else {
            venueId = c.venueId;
            reason = "SOR aggressive: best displayed contra price, then taker fee,"
                    + " commission, venue id";
        }
        return new VenueDecisionRec(c.orderId, venueId, reason, out);
    }

    private TCAResultRec tca(Open o) {
        TcaParentOrder parent = TcaService.parentFromFills(o.riskOrderId, instrumentId,
                o.side, o.qty, o.eventTs, o.arrivalTs, o.tcaEndTs, o.fills, tickSize,
                timeline);
        Tca.OrderTca r = TcaService.analyze(parent, timeline);
        double denom = (double) o.qty * r.decisionMid();
        double fees = 0.0;
        for (Fill f : o.fills) {
            fees += f.fee();
        }
        double arrivalMid = r.arrivalMid();
        Double vwap = timeline.intervalVwap(o.arrivalTs, o.tcaEndTs);
        Double twap = timeline.intervalTwap(o.arrivalTs, o.tcaEndTs);
        TreeMap<String, Double> byVenue = new TreeMap<>();
        for (Fill f : o.fills) {
            double contrib = 1e4 * parent.sign() * (double) f.qty()
                    * ((double) f.priceTicks() * tickSize - arrivalMid) / denom;
            byVenue.merge(Integer.toString(f.venueId()), contrib, Double::sum);
        }
        long filled = parent.qtyFilled();
        BacktestEngine.Account a = engineHolder[0].accounts().get(instrumentId);
        long volume = (a == null ? 0 : a.sessionVolume) - o.volumeAtDecision;
        double participation = volume > 0 ? Math.min(1.0, (double) filled / (double) volume)
                : (filled > 0 ? 1.0 : 0.0);
        long latency = o.arrivalTs - o.eventTs;
        Tca.Perold p = r.perold();
        return new TCAResultRec(o.riskOrderId, instrumentId, o.side, o.qty, filled,
                (double) filled / (double) o.qty,
                Math.round(arrivalMid / tickSize),
                filled > 0 ? parent.fillVwap() / tickSize : 0.0,
                (vwap == null ? arrivalMid : vwap) / tickSize,
                (twap == null ? arrivalMid : twap) / tickSize,
                p.totalIsBps(), p.delayBps(), p.tradingBps(), p.opportunityBps(),
                1e4 * r.spreadCost() / denom, 1e4 * r.impactCost() / denom,
                1e4 * fees / (denom * qtyUnit), 1e4 * r.timingCost() / denom,
                r.arrivalSlippageBps() == null ? 0.0 : r.arrivalSlippageBps(),
                participation, o.fills.size(), byVenue, "IS",
                new TCAResultRec.LatencyStats(latency, latency, latency, latency, latency));
    }

    private void emit(Open o) {
        List<AlphaSignalRec> signal = o.signal == null ? List.of() : List.of(o.signal);
        List<ParentOrderRec> parents = List.of();
        List<ChildOrderRec> children = List.of();
        List<VenueDecisionRec> routing = List.of();
        List<TCAResultRec> tca = List.of();
        Attribution attribution = null;
        if (o.child != null) {
            parents = List.of(new ParentOrderRec(o.riskOrderId, STRATEGY_ID, alphaId,
                    instrumentId, o.side, o.qty, "IS", o.eventTs, o.arrivalTs, o.endTs,
                    1.0, 0, Map.of()));
            children = List.of(o.child);
            routing = List.of(o.routing);
            TCAResultRec t = null;
            try {
                t = tca(o);
            } catch (IllegalArgumentException | IllegalStateException e) {
                tcaSkipped.inc();
            }
            if (t != null) {
                tca = List.of(t);
                attribution = Attribution.of(o.side, o.expectedReturn, t);
            }
        }
        TraceStages stages = new TraceStages(signal, o.portfolio, List.of(o.risk),
                parents, children, routing, o.reports, tca, attribution);
        sink.emit(DecisionTrace.of(sessionId, instrumentId, o.eventTs, o.sequence,
                dataVersion, featureVersion, modelVersion, configVersion, stages));
        records.inc();
    }
}
