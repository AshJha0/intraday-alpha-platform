package com.iap.platform;

import java.nio.charset.StandardCharsets;

import com.iap.monitoring.Histogram;
import com.iap.monitoring.MetricsRegistry;
import com.iap.platform.PaperTrading.Options;
import com.iap.platform.PaperTrading.Result;

/**
 * JSON bodies of {@link PaperTrading}: {@code /status} and the session report.
 * Package-private: split out of PaperTrading (v1.13 E4) without behaviour change.
 */
final class PaperTradingReports {
    private PaperTradingReports() {
    }

    static String esc(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "\\r");
    }

    /**
     * The {@code /status} body (§12.5): sorted keys, live progress, and the
     * fields runbooks tell operators to read.
     */
    static String statusJson(Result res, Options opts) {
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
                + ",\"status\":\"" + res.state.label()
                + "\",\"trace_count\":" + res.traceCount
                + ",\"trace_digest\":\"" + res.traceDigest + "\"}";
    }

    static String pct(MetricsRegistry reg, String name) {
        Histogram h = reg.histogramRef(name);
        if (h == null || h.count() == 0) {
            return "{\"count\":0}";
        }
        long[] p = h.p50p99p999();
        return "{\"count\":" + h.count() + ",\"p50\":" + p[0] + ",\"p99\":"
                + p[1] + ",\"p999\":" + p[2] + "}";
    }

    /** Finite double as JSON number, NaN as JSON {@code null}. */
    static String numOrNull(double v) {
        return Double.isNaN(v) ? "null" : Double.toString(v);
    }

    static String reportJson(Result res, Options opts,
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
                .append("\",\"trace\":{\"count\":").append(res.traceCount)
                .append(",\"digest\":\"").append(res.traceDigest)
                .append("\",\"jsonl\":\"")
                .append(esc(store.file(SessionStore.DECISION_TRACES).toString()))
                .append("\"},\"x-version\":3}");
        return sb.toString();
    }
}
