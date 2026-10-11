package com.iap.platform;

import java.util.List;
import java.util.Locale;

import com.iap.backtest.BacktestEngine;
import com.iap.platform.PaperTrading.SessionState;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskEvent;
import com.iap.trace.JsonlTraceSink;

/**
 * Session-state I/O helpers of {@link PaperTrading}: the session exit marker,
 * checkpoint commit and checkpoint P&amp;L booking (PLATFORM_CONVENTIONS.md §12.3).
 * Package-private: split out of PaperTrading (v1.13 E4) without behaviour change.
 */
final class PaperTradingSession {
    private PaperTradingSession() {
    }

    /**
     * Record how the session left the state directory
     * ({@link SessionStore#SESSION_EXIT}) for an exporter that outlives this
     * process (PLATFORM_CONVENTIONS.md §12.3). Called on the trading thread
     * AFTER the checkpoint it describes is committed, so it can never weaken
     * the checkpoint. Best effort: a failure to write the marker is reported
     * on stderr and never changes the session's outcome.
     */
    static void markSessionExit(SessionStore store, SessionState state,
            long eventCursor) {
        try {
            store.writeSessionExit(state.label().toUpperCase(Locale.ROOT),
                    state.code(), eventCursor,
                    System.currentTimeMillis() / 1000L);
        } catch (RuntimeException e) {
            System.err.println("session marker not written: " + e.getMessage());
        }
    }

    /**
     * Persist one checkpoint (§12.3). Called ONLY on the trading thread. The
     * audit and trace lines are appended first; the risk snapshot and
     * {@code session_state.json} are then committed as one unit by
     * {@link SessionStore#commitCheckpoint}: the state file is the single
     * commit point and records the snapshot's sha256, so a crash at any
     * instant leaves either the previous or the new (cursor, risk state)
     * pair — never a cursor beside a risk snapshot of another instant.
     *
     * @return the new audit-line cursor
     */
    static long checkpoint(SessionStore store, RiskEngine risk,
            SessionStore.State st, long auditBase, long auditCursor,
            double equityPeak, JsonlTraceSink traces) {
        List<RiskEvent> audit = risk.audit();
        StringBuilder sb = new StringBuilder(256);
        for (int i = (int) auditCursor; i < audit.size(); i++) {
            sb.append(audit.get(i).toJsonLine()).append('\n');
        }
        store.appendJsonl(SessionStore.RISK_AUDIT, sb.toString());
        st.auditLines = auditBase + audit.size();
        traces.flush();
        st.traceLines = traces.count();
        st.equityPeak = equityPeak;
        store.commitCheckpoint(st, risk.snapshot());
        return audit.size();
    }

    /**
     * Session-cumulative P&amp;L for a checkpoint taken mid-session: the
     * carried-in bases plus this leg's live account totals (the same sums
     * {@code BacktestEngine.finish} reports at the end).
     */
    static void bookPnl(SessionStore.State st, BacktestEngine engine,
            double basePnl, double baseGross) {
        double total = 0.0;
        double gross = 0.0;
        for (BacktestEngine.Account a : engine.accounts().values()) {
            total += a.pnlReporting;
            gross += a.grossPnl;
        }
        st.totalPnl = basePnl + total;
        st.grossPnl = baseGross + gross;
    }
}
