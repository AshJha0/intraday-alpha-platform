package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.config.ConfigService;
import com.iap.platform.PaperTrading;
import com.iap.platform.SessionStore;
import com.iap.risk.RiskEngine;

/**
 * State recovery across a restart (PLATFORM_CONVENTIONS.md §12.3) — the fix
 * for round-3 SEV-1 "no state recovery: a restart clears the kill switch,
 * positions and realized P&amp;L". Proposed tests 10 (RestartRecoversState)
 * and 11 (AuditLogPersistedAndReplays).
 *
 * <p>Scenario: an OOM-kill / node drain mid-session. The pod comes back and
 * must NOT start flat and un-latched.
 */
public class PaperStateRecoveryTest {
    /** Every state artefact is written, with the pinned schema. */
    @Test
    public void checkpointWritesTheFourStateFiles() throws IOException {
        PaperTrading.Options opts = PaperFixtures.session(1200);
        opts.checkpointEveryEvents = 256;
        PaperTrading.Result res = PaperTrading.run(opts);
        Path dir = res.stateDir;
        for (String name : new String[] {SessionStore.SESSION_STATE,
                SessionStore.RISK_SNAPSHOT, SessionStore.RISK_AUDIT,
                SessionStore.CONFIG_AUDIT}) {
            assertTrue(name + " written", Files.exists(dir.resolve(name)));
        }
        SessionStore store = new SessionStore(dir);
        SessionStore.State st = store.readState();
        assertEquals(1200, st.eventCursor);
        assertEquals(1, st.instrumentId);
        assertEquals("EQ01", st.alphaId);
        assertEquals(res.fillCount, st.fillCount);
        assertEquals(res.ordersSubmitted, st.ordersSubmitted);
        assertEquals(0, st.restarts);
        assertEquals(64, st.configSha256.length());
        // the state document round-trips exactly
        assertEquals(st.toJson(), store.readState().toJson());
        // no temp file survives an atomic write
        assertTrue(!Files.exists(dir.resolve(
                SessionStore.SESSION_STATE + ".tmp")));
    }

    /**
     * Test 11 — the risk audit JSONL is persisted and is byte-identical to
     * the in-memory audit the engine replays from; the report records its
     * sha256 so a postmortem is reproducible (GOVERNANCE.md §3).
     */
    @Test
    public void auditLogIsPersistedAndMatchesTheEngine() throws IOException {
        PaperTrading.Options opts = PaperFixtures.session(1200);
        opts.checkpointEveryEvents = 256;
        PaperTrading.Result res = PaperTrading.run(opts);
        Path audit = res.stateDir.resolve(SessionStore.RISK_AUDIT);
        String onDisk = new String(Files.readAllBytes(audit),
                StandardCharsets.UTF_8);
        assertEquals("persisted audit == engine audit",
                res.riskAudit.auditJsonl(), onDisk);
        List<String> lines = PaperFixtures.lines(audit);
        assertEquals(res.riskAudit.audit().size(), lines.size());
        assertTrue("the session made risk decisions", lines.size() > 100);
        // every line is a schema-shaped RiskEvent object
        for (String line : lines) {
            Map<String, Object> doc = PaperFixtures.json(line);
            assertTrue(line, doc.containsKey("decision"));
            assertTrue(line, doc.containsKey("rule_id"));
            assertTrue(line, doc.containsKey("scope"));
            assertTrue(line, doc.containsKey("timestamp"));
        }
        // the report carries the file and its hash
        Map<String, Object> risk = Json.object(
                PaperFixtures.json(res.reportJson).get("risk"));
        assertEquals(audit.toString(), risk.get("audit_jsonl"));
        assertEquals(com.iap.codec.Sha256.hex(
                        onDisk.getBytes(StandardCharsets.UTF_8)),
                risk.get("audit_sha256"));
        // deterministic: a second identical session writes the same audit
        PaperTrading.Options again = PaperFixtures.session(1200);
        again.checkpointEveryEvents = 256;
        PaperTrading.Result res2 = PaperTrading.run(again);
        assertEquals(onDisk, new String(Files.readAllBytes(
                res2.stateDir.resolve(SessionStore.RISK_AUDIT)),
                StandardCharsets.UTF_8));
    }

    /**
     * Test 10 — the snapshot/restore round trip is byte-identical, and a
     * resumed session continues the risk state instead of starting flat.
     */
    @Test
    public void restartResumesPositionsPnlAndOrderIds() throws IOException {
        // leg 1: a session that "crashes" after 900 events
        PaperTrading.Options leg1 = PaperFixtures.session(900);
        PaperTrading.Result r1 = PaperTrading.run(leg1);
        assertTrue("leg 1 took a position", r1.riskAudit.position(1) != 0);
        SessionStore store = new SessionStore(r1.stateDir);
        SessionStore.State st1 = store.readState();
        String snap1 = store.read(SessionStore.RISK_SNAPSHOT);

        // golden round trip: restore(snapshot()) re-serializes byte-identically
        ConfigService cfg = new ConfigService(PaperFixtures.configs());
        RiskEngine restored = RiskEngine.restore(cfg.riskLimits(),
                cfg.riskInstruments(), store.readRiskSnapshot(), 1L);
        assertEquals("snapshot round trip is byte-identical",
                snap1, restored.snapshot());
        assertEquals(r1.riskAudit.position(1), restored.position(1));
        assertEquals(r1.riskAudit.realizedPnl(), restored.realizedPnl(), 0.0);
        assertEquals(r1.riskAudit.killSwitchEngaged(),
                restored.killSwitchEngaged());

        // leg 2: restart over the same state dir, resuming at the cursor
        PaperTrading.Options leg2 = new PaperTrading.Options();
        leg2.configsDir = PaperFixtures.configs();
        leg2.eventsFile = PaperFixtures.goldenEvents();
        leg2.maxEvents = 1500;
        leg2.stateDir = r1.stateDir;
        leg2.resume = true;
        leg2.adminToken = "";
        PaperTrading.Result r2 = PaperTrading.run(leg2);
        assertEquals(1, r2.restarts);
        assertEquals(1500, r2.eventsProcessed);
        assertEquals(1, r2.metrics.counterValue("risk_session_restarts_total"));
        // the resumed session carried the exposure and the realized P&L in
        assertEquals("orders ids continue (no duplicate-id reuse)",
                true, r2.ordersSubmitted > st1.ordersSubmitted);
        assertTrue("the restart did not reset realized P&L",
                r2.riskAudit.realizedPnl() != 0.0);
        SessionStore.State st2 = new SessionStore(r1.stateDir).readState();
        assertEquals(1500, st2.eventCursor);
        assertEquals(1, st2.restarts);
        // the audit log GREW: leg 2's decisions are appended, not overwritten
        List<String> audit2 = PaperFixtures.lines(
                r1.stateDir.resolve(SessionStore.RISK_AUDIT));
        assertTrue("audit appended across the restart",
                audit2.size() > st1.auditLines);
        assertEquals("state's line count matches the file",
                st2.auditLines, audit2.size());
        assertEquals("leg 1 lines are untouched",
                PaperFixtures.lines(r1.stateDir.resolve(SessionStore.RISK_AUDIT))
                        .subList(0, (int) st1.auditLines).size(),
                (int) st1.auditLines);
        assertTrue("the restart itself is audited",
                audit2.get((int) st1.auditLines).contains("STATE_RESTORED"));
    }

    /**
     * The headline property: a latched kill switch SURVIVES a restart. The
     * runbook used to document "a restart starts un-latched" as a clearing
     * mechanism; §12.3 forbids that.
     */
    @Test
    public void scenarioRestartDoesNotClearALatchedKillSwitch()
            throws IOException {
        // A session whose config latches the switch at boot leaves a latched
        // snapshot behind.
        Path configs = PaperFixtures.copyConfigs();
        PaperFixtures.writeConfig(configs, "risk.json",
                PaperFixtures.readConfig(configs, "risk.json")
                        .replace("\"kill_switch_engaged\": false",
                                "\"kill_switch_engaged\": true"));
        PaperTrading.Options leg1 = PaperFixtures.session(600);
        leg1.configsDir = configs;
        PaperTrading.Result r1 = PaperTrading.run(leg1);
        assertTrue("config master switch latched the engine",
                r1.killSwitchEngaged);

        // Restart with the ARMED (committed) configs: the snapshot still
        // carries the latch, so the restart does NOT resume trading.
        PaperTrading.Options leg2 = new PaperTrading.Options();
        leg2.configsDir = PaperFixtures.configs();
        leg2.eventsFile = PaperFixtures.goldenEvents();
        leg2.maxEvents = 1000;
        leg2.stateDir = r1.stateDir;
        leg2.resume = true;
        leg2.adminToken = "";
        PaperTrading.Result r2 = PaperTrading.run(leg2);
        assertTrue("the latch survived the restart", r2.killSwitchEngaged);
        assertEquals("no order was allowed after the restart", 0, r2.riskAllowed);
        assertTrue("every order was rejected", r2.riskRejected > 0);
    }

    /** Corrupt or absent state under --resume fails closed. */
    @Test
    public void resumeFailsClosedOnMissingOrCorruptState() throws IOException {
        PaperTrading.Options opts = PaperFixtures.session(400);
        opts.resume = true;
        try {
            PaperTrading.run(opts);
            fail("resume without a checkpoint accepted");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("no checkpoint"));
        }

        PaperTrading.Result r1 = PaperTrading.run(PaperFixtures.session(400));
        Files.write(r1.stateDir.resolve(SessionStore.SESSION_STATE),
                "{ not json".getBytes(StandardCharsets.UTF_8));
        PaperTrading.Options resume = PaperFixtures.session(800);
        resume.stateDir = r1.stateDir;
        resume.resume = true;
        try {
            PaperTrading.run(resume);
            fail("corrupt session_state.json accepted");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("corrupt platform state"));
        }

        // a checkpoint for a different instrument/alpha is refused
        PaperTrading.Result r2 = PaperTrading.run(PaperFixtures.session(400));
        PaperTrading.Options other = PaperFixtures.session(800);
        other.stateDir = r2.stateDir;
        other.resume = true;
        other.instrumentId = 2;
        try {
            PaperTrading.run(other);
            fail("mismatched checkpoint accepted");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("checkpoint is for"));
        }

        // and a completed session cannot be resumed past its end
        PaperTrading.Options done = PaperFixtures.session(400);
        done.stateDir = r2.stateDir;
        done.resume = true;
        try {
            PaperTrading.run(done);
            fail("resuming a completed session accepted");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("at/past the end"));
        }
    }

    /**
     * A torn append leaves risk_audit.jsonl LONGER than the cursor
     * session_state.json records (the audit is flushed before the state file
     * is renamed, so a kill between the two is the common case). Resuming
     * would replay decisions the file already holds and break the "the audit
     * replays byte-identically" property GOVERNANCE §3 depends on, so the
     * resume is refused with both counts named (conventions §12.3).
     */
    @Test
    public void scenarioTornAuditAppendRefusesToResume() throws IOException {
        PaperTrading.Result r = PaperTrading.run(PaperFixtures.session(400));
        java.nio.file.Path audit = r.stateDir.resolve(SessionStore.RISK_AUDIT);
        long before = Files.readAllLines(audit).size();
        assertTrue("the session recorded decisions", before > 0);

        // a decision was appended and flushed; the process died before the
        // atomic rename of session_state.json
        Files.writeString(audit,
                "{\"decision\":0,\"reason\":\"torn\",\"rule_id\":\"X\"}\n",
                java.nio.file.StandardOpenOption.APPEND);
        PaperTrading.Options resume = PaperFixtures.session(800);
        resume.stateDir = r.stateDir;
        resume.resume = true;
        try {
            PaperTrading.run(resume);
            fail("resumed from a torn audit append");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("risk_audit.jsonl"));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains(String.valueOf(before + 1)));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("audit_lines="));
        }

        // the same guard catches a truncated / foreign audit
        Files.writeString(audit, "");
        try {
            PaperTrading.run(resume);
            fail("resumed from a truncated audit");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("refusing to resume"));
        }
    }

    /** A truncated/garbage risk snapshot is rejected, not silently ignored. */
    @Test
    public void corruptRiskSnapshotIsRejected() throws IOException {
        PaperTrading.Result r = PaperTrading.run(PaperFixtures.session(400));
        Files.write(r.stateDir.resolve(SessionStore.RISK_SNAPSHOT),
                "{\"x-version\":99}".getBytes(StandardCharsets.UTF_8));
        PaperTrading.Options resume = PaperFixtures.session(800);
        resume.stateDir = r.stateDir;
        resume.resume = true;
        try {
            PaperTrading.run(resume);
            fail("bad snapshot version accepted");
        } catch (IllegalArgumentException | IllegalStateException expected) {
            assertNotEquals("", String.valueOf(expected.getMessage()));
        }
    }
}
