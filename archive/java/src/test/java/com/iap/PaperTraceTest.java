package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.contracts.CanonicalJson;
import com.iap.platform.PaperTrading;
import com.iap.platform.SessionStore;
import com.iap.trace.DecisionTrace;
import com.iap.trace.ExecutionReportRec;
import com.iap.trace.Explain;
import com.iap.trace.RiskDecisionRec;
import com.iap.trace.TCAResultRec;
import com.iap.trace.TraceDigest;
import com.iap.trace.TraceStages;

/**
 * The paper-trading vertical emits one {@code DecisionTrace} per decision
 * cycle into {@code <state-dir>/decision_traces.jsonl}: deterministic
 * (identical runs ⇒ identical digests), structurally valid (every line
 * parses as the contract, no NaN), and the report / {@code /status} digest
 * equals {@code TraceDigest.ofJsonl(file)}; {@code --resume} continues the
 * digest instead of restarting it.
 */
public class PaperTraceTest {
    private static final String[] TOP_KEYS = {"trace_id", "session_id",
        "instrument_id", "event_ts", "sequence", "data_version", "feature_version",
        "model_version", "config_version", "stages"};
    private static final String[] STAGE_KEYS = {"signal", "portfolio", "risk",
        "parent_orders", "child_orders", "routing", "fills", "tca", "attribution"};

    private static Path traceFile(PaperTrading.Result res) {
        return res.stateDir.resolve(SessionStore.DECISION_TRACES);
    }

    @Test
    public void identicalRunsProduceIdenticalDigestsAndValidLines() throws IOException {
        PaperTrading.Options a = PaperFixtures.session(1200);
        a.checkpointEveryEvents = 256;
        PaperTrading.Result r1 = PaperTrading.run(a);
        PaperTrading.Options b = PaperFixtures.session(1200);
        b.checkpointEveryEvents = 256;
        PaperTrading.Result r2 = PaperTrading.run(b);

        assertTrue("the session made decisions", r1.traceCount > 10);
        assertEquals(r1.traceCount, r2.traceCount);
        assertEquals(r1.traceDigest, r2.traceDigest);
        assertNotEquals(TraceDigest.EMPTY, r1.traceDigest);
        List<String> lines1 = PaperFixtures.lines(traceFile(r1));
        List<String> lines2 = PaperFixtures.lines(traceFile(r2));
        assertEquals(lines1, lines2);
        assertEquals(r1.traceCount, lines1.size());

        // the report and /status carry the digest the file reproduces
        TraceDigest ofFile = TraceDigest.ofJsonl(traceFile(r1));
        assertEquals(r1.traceDigest, ofFile.hexDigest());
        assertEquals(r1.traceCount, ofFile.count());
        Map<String, Object> report = PaperFixtures.json(r1.reportJson);
        Map<String, Object> trace = Json.object(report.get("trace"));
        assertEquals(r1.traceDigest, trace.get("digest"));
        assertEquals(r1.traceCount, Json.asLong(trace.get("count")));
        assertEquals(traceFile(r1).toString(), trace.get("jsonl"));
        assertEquals(3L, Json.asLong(report.get("x-version")));
        Map<String, Object> status = PaperFixtures.json(
                PaperTrading.statusJson(r1, a));
        assertEquals(r1.traceDigest, status.get("trace_digest"));
        assertEquals(r1.traceCount, Json.asLong(status.get("trace_count")));
        assertEquals(r1.traceCount, r1.metrics.counterValue("trace_records_total"));

        // every line is a structurally valid, finite, canonical DecisionTrace
        int withFills = 0;
        int rejected = 0;
        int withTca = 0;
        int tcaSkipped = 0;
        String sessionId = null;
        for (String line : lines1) {
            assertFalse(line, line.contains("NaN") || line.contains("Infinity"));
            Map<String, Object> raw = Json.object(
                    com.iap.config.Json.parse(line, true));
            for (String k : TOP_KEYS) {
                assertTrue(k, raw.containsKey(k));
            }
            for (String k : STAGE_KEYS) {
                assertTrue(k, Json.object(raw.get("stages")).containsKey(k));
            }
            DecisionTrace t = DecisionTrace.fromLine(line);
            assertEquals("canonical line", line, t.toLine());
            assertEquals(CanonicalJson.makeTraceId(t.sessionId(), t.instrumentId(),
                    t.eventTs(), t.sequence()), t.traceId());
            assertEquals(1L, t.instrumentId());
            assertEquals(r1.configSha256, t.configVersion());
            if (sessionId == null) {
                sessionId = t.sessionId();
            }
            assertEquals(sessionId, t.sessionId());
            TraceStages st = t.stages();
            assertEquals(1, st.signal().size());
            assertEquals(1, st.risk().size());
            RiskDecisionRec rd = st.risk().get(0);
            if (rd.decision() == RiskDecisionRec.ALLOW) {
                assertEquals(1, st.parentOrders().size());
                assertEquals(1, st.childOrders().size());
                assertEquals(1, st.routing().size());
                assertEquals(rd.orderId(), st.parentOrders().get(0).parentOrderId());
                assertEquals(st.childOrders().get(0).childOrderId(),
                        st.routing().get(0).childOrderId());
                long filled = 0;
                for (ExecutionReportRec er : st.fills()) {
                    filled += er.filledQty();
                    assertEquals(st.childOrders().get(0).childOrderId(), er.orderId());
                }
                assertTrue(filled <= st.parentOrders().get(0).qty());
                if (filled > 0) {
                    withFills++;
                }
                if (!st.tca().isEmpty()) {
                    withTca++;
                    TCAResultRec tca = st.tca().get(0);
                    assertEquals(filled, tca.filledQty());
                    assertNotNull(st.attribution());
                    assertEquals(-tca.spreadCostBps(), st.attribution().spreadBps(), 0.0);
                } else {
                    // only a child the stream ended before it arrived at the
                    // venue has no TCA window (never a guessed benchmark)
                    tcaSkipped++;
                    assertEquals(0, filled);
                    assertEquals(1, st.fills().size());
                    assertEquals(ExecutionReportRec.CANCELED, st.fills().get(0).status());
                    assertTrue(st.parentOrders().get(0).arrivalTs() > r1.lastEventTs);
                }
            } else {
                rejected++;
                assertTrue(st.parentOrders().isEmpty());
                assertTrue(st.fills().isEmpty());
                assertTrue(rd.ruleIndex() >= 0);
            }
            // explain() renders every trace
            String text = Explain.render(t, Map.of(1, "XV1", 2, "XV2"));
            assertTrue(text, text.startsWith(rd.decision() == RiskDecisionRec.ALLOW
                    ? "Order " : "Trace "));
            assertTrue(text, text.contains("\nRisk:       "));
        }
        assertTrue("some decisions filled", withFills > 0);
        assertTrue("TCA ran at parent end", withTca > 0);
        assertEquals("every parent the timeline covered got a TCA record",
                tcaSkipped, r1.metrics.counterValue("trace_tca_skipped_total"));
        assertTrue("at most the last in-flight child lacks a window", tcaSkipped <= 1);
        assertEquals("REJECT traces == pre-trade rejects", rejected,
                r1.metrics.counterValue("exec_child_orders_rejected_total"));
    }

    @Test
    public void resumeContinuesTheDigestAndRefusesATornTraceFile() throws IOException {
        PaperTrading.Options leg1 = PaperFixtures.session(900);
        PaperTrading.Result r1 = PaperTrading.run(leg1);
        SessionStore store = new SessionStore(r1.stateDir);
        SessionStore.State st1 = store.readState();
        assertEquals(r1.traceCount, st1.traceLines);
        List<String> before = PaperFixtures.lines(traceFile(r1));
        assertEquals(st1.traceLines, before.size());

        PaperTrading.Options leg2 = new PaperTrading.Options();
        leg2.configsDir = PaperFixtures.configs();
        leg2.eventsFile = PaperFixtures.goldenEvents();
        leg2.maxEvents = 1500;
        leg2.stateDir = r1.stateDir;
        leg2.resume = true;
        leg2.adminToken = "";
        PaperTrading.Result r2 = PaperTrading.run(leg2);
        List<String> after = PaperFixtures.lines(traceFile(r1));
        assertTrue("leg 2 appended traces", after.size() > before.size());
        assertEquals("leg 1 lines are untouched", before,
                after.subList(0, before.size()));
        assertEquals(after.size(), r2.traceCount);
        assertEquals(r2.traceCount, new SessionStore(r1.stateDir).readState().traceLines);
        // the digest continued across the restart: it is the digest of the
        // whole file, not of leg 2 alone
        assertEquals(TraceDigest.ofJsonl(traceFile(r1)).hexDigest(), r2.traceDigest);
        assertEquals(r2.traceCount, r2.metrics.counterValue("trace_records_total"));
        // the session id is stable across the restart
        assertEquals(DecisionTrace.fromLine(before.get(0)).sessionId(),
                DecisionTrace.fromLine(after.get(after.size() - 1)).sessionId());

        // a torn / truncated trace file refuses to resume, naming the counts
        PaperTrading.Result r3 = PaperTrading.run(PaperFixtures.session(400));
        Path traces = traceFile(r3);
        long n = PaperFixtures.lines(traces).size();
        Files.writeString(traces, before.get(0) + "\n",
                java.nio.file.StandardOpenOption.APPEND);
        PaperTrading.Options resume = PaperFixtures.session(800);
        resume.stateDir = r3.stateDir;
        resume.resume = true;
        try {
            PaperTrading.run(resume);
            fail("resumed from a torn trace append");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains(SessionStore.DECISION_TRACES));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains(String.valueOf(n + 1)));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("trace_lines="));
        }
        Files.write(traces, new byte[0]);
        try {
            PaperTrading.run(resume);
            fail("resumed from a truncated trace file");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("refusing to resume"));
        }
    }

    @Test
    public void stateDocumentCarriesTheTraceCursor() throws IOException {
        PaperTrading.Result r = PaperTrading.run(PaperFixtures.session(400));
        String doc = new String(Files.readAllBytes(
                r.stateDir.resolve(SessionStore.SESSION_STATE)), StandardCharsets.UTF_8);
        assertTrue(doc, doc.contains("\"trace_lines\":" + r.traceCount));
        assertTrue(doc, doc.contains("\"x-version\":" + SessionStore.STATE_VERSION));
        assertEquals(2, SessionStore.STATE_VERSION);
    }
}
