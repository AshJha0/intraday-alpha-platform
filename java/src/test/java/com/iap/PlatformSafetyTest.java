package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import org.junit.Test;

import com.iap.api.MetricsServer;
import com.iap.backtest.BacktestEngine;
import com.iap.codec.Sha256;
import com.iap.config.ConfigService;
import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.execution.ExecConfig;
import com.iap.execution.LatencyConfig;
import com.iap.monitoring.MetricsRegistry;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.platform.AdminService;
import com.iap.platform.PaperTrading;
import com.iap.platform.SessionStore;
import com.iap.risk.RiskEngine;
import com.iap.risk.Rules;
import com.iap.risk.Scope;

/**
 * Safety properties of the paper-trading platform: the venue kill reaches
 * SOR-routed orders, a resumed session is consistent with its risk state, a
 * checkpoint has one commit point, a stop request is honoured by the trading
 * thread, an admin kill latches at once and is never dropped, the admin API
 * bounds abuse and attributes operators, and the gap gate reopens only when
 * no venue is stale.
 */
public class PlatformSafetyTest {
    private static final String TOKEN = "s3cret-approval-token";

    private static ConfigService cfg() {
        return new ConfigService(PaperFixtures.configs());
    }

    private static ExecConfig exec(ConfigService cfg) {
        return new ExecConfig(LatencyConfig.DEFAULT, cfg.executionSeed(),
                cfg.impactCoeffBpsPerPctAdv(), cfg.instruments(), cfg.venues());
    }

    private static String sha(String s) {
        return Sha256.hex(s.getBytes(StandardCharsets.UTF_8));
    }

    private static Map<String, Object> parse(String json) {
        return com.iap.config.Json.object(com.iap.config.Json.parse(json));
    }

    private static AdminService service(RiskEngine risk, Path dir,
            Map<String, String> operators, boolean running) {
        return new AdminService(risk, new SessionStore(dir),
                new MetricsRegistry(), operators, () -> 1L, () -> running);
    }

    private static final Map<String, String> GLOBAL_KILL =
            Map.of("scope", "global", "reason", "INC-1 approved");

    // ------------------------------------------------ 1. venue kill + SOR

    /**
     * Under SOR (session venue 0) the pre-trade check must name the venue
     * the child is routed to: with every venue killed, nothing is allowed.
     */
    @Test
    public void venueKillIsEnforcedOnSorRoutedOrders() {
        ConfigService cfg = cfg();
        ExecConfig exec = exec(cfg);
        MetricsRegistry reg = new MetricsRegistry();
        RiskEngine risk = RiskEngine.fromConfig(cfg.riskDoc(),
                cfg.riskInstruments(), reg);
        for (int vid : cfg.venues().keySet()) {
            risk.engageKill(Scope.VENUE, Integer.toString(vid), 1L, "venue down");
        }
        BacktestEngine[] holder = new BacktestEngine[1];
        PaperTrading.RiskWiring wiring = new PaperTrading.RiskWiring(risk,
                "PAPER", 0, reg).withRouting(exec, cfg.sorOptions(), holder);
        long[] flip = {0};
        BacktestEngine engine = new BacktestEngine(exec,
                vec -> (++flip[0] % 50) < 25 ? 100 : -100, wiring.hook(), wiring,
                BacktestEngine.USD_ONLY, 100, 0,
                BacktestEngine.ExecutionLimits.NONE, cfg.sorOptions());
        holder[0] = engine;
        engine.run(Golden.eq().subList(0, 1500));
        long venueRejects = risk.audit().stream()
                .filter(e -> e.ruleId().equals(Rules.KILL_VENUE)).count();
        long allowed = risk.audit().stream()
                .filter(e -> e.ruleId().equals(Rules.ALLOW)).count();
        assertTrue("SOR-routed orders hit the venue kill", venueRejects > 0);
        assertEquals("nothing is allowed to a killed venue", 0, allowed);
        assertTrue("no child was sent", engine.simulator().orders().isEmpty());
    }

    /** A whole SOR session: risk and the engine always agree on the venue. */
    @Test
    public void sorSessionRiskVenueMatchesTheRoutedChild() throws IOException {
        PaperTrading.Options opts = PaperFixtures.session(1200);
        opts.venueId = 0;
        PaperTrading.Result res = PaperTrading.run(opts);
        assertEquals(PaperTrading.SessionState.FINISHED, res.state);
        assertTrue("the SOR session traded", res.ordersSubmitted > 0);
        assertEquals(0, res.metrics.counterValue(
                "risk_routed_venue_mismatch_total"));
    }

    // ------------------------------------------------------- 2. resume

    /** Open orders of a restored snapshot are released; positions seed the account. */
    @Test
    public void resumeReleasesOrphanedOrdersAndSeedsTheAccount() {
        ConfigService cfg = cfg();
        String empty = RiskEngine.fromConfig(cfg.riskDoc(),
                cfg.riskInstruments()).snapshot();
        assertTrue(empty, empty.contains("\"open\":{}"));
        assertTrue(empty, empty.contains("\"positions\":{}"));
        assertTrue(empty, empty.contains("\"market\":{}"));
        String text = empty
                .replace("\"open\":{}", "\"open\":{\"7\":{\"instrument_id\":1,"
                        + "\"price_ticks\":0,\"qty\":10,\"side\":0}}")
                .replace("\"positions\":{}", "\"positions\":{\"1\":300}")
                .replace("\"market\":{}", "\"market\":{\"1\":{\"ask_ticks\":10002,"
                        + "\"bid_ticks\":10000,\"gaps\":0,\"gated\":false,\"ts\":5}}");
        Map<String, Object> snap = parse(text);
        RiskEngine restored = RiskEngine.restore(cfg.riskLimits(),
                cfg.riskInstruments(), snap, 6L);
        assertEquals(1, restored.openOrderCount());
        assertEquals(1, PaperTrading.releaseOrphanedOpenOrders(restored, snap));
        assertEquals("orphaned open orders are released", 0,
                restored.openOrderCount());
        assertEquals(300, restored.position(1));

        ExecConfig exec = exec(cfg);
        BacktestEngine engine = new BacktestEngine(exec, vec -> 0,
                BacktestEngine.PASSTHROUGH_RISK, 100, 1);
        PaperTrading.seedAccounts(engine, snap, exec, 1, 12.5);
        BacktestEngine.Account a = engine.accounts().get(1L);
        assertEquals("the account starts at the risk position, not flat",
                300, a.position);
        assertTrue(a.markValid);
        double tick = exec.instrument(1).tickSize();
        assertEquals(10001 * tick, a.mark, 1e-9);
        assertEquals("equity continues from the carried-in P&L", 12.5,
                a.equity(exec.instrument(1).qtyUnit()), 1e-6);
        assertEquals("this leg's P&L increments start at zero", 0.0,
                a.pnlReporting, 0.0);
    }

    /** The state file is the single commit point and names the snapshot by hash. */
    @Test
    public void checkpointHasOneCommitPointVerifiedOnResume() throws IOException {
        SessionStore store = new SessionStore(
                Files.createTempDirectory("iap-commit"));
        String snapA = "{\"k\":\"A\",\"x-version\":1}";
        String snapB = "{\"k\":\"B\",\"x-version\":1}";
        SessionStore.State st = new SessionStore.State();
        st.eventCursor = 10;
        store.commitCheckpoint(st, snapA);
        assertEquals(sha(snapA), st.riskSnapshotSha256);
        assertEquals(sha(snapA), store.readState().riskSnapshotSha256);
        assertEquals("A", store.readCommittedRiskSnapshot(store.readState())
                .get("k"));
        assertFalse(Files.exists(store.file(SessionStore.RISK_SNAPSHOT_NEXT)));

        // crash BEFORE the commit: the new snapshot is staged, the state is old
        store.writeAtomic(SessionStore.RISK_SNAPSHOT_NEXT, snapB);
        assertEquals("the previous checkpoint is still the committed one", "A",
                store.readCommittedRiskSnapshot(store.readState()).get("k"));

        // crash AFTER the commit, before the snapshot rename: roll forward
        st.eventCursor = 20;
        st.riskSnapshotSha256 = sha(snapB);
        store.writeAtomic(SessionStore.SESSION_STATE, st.toJson());
        SessionStore.State committed = store.readState();
        assertEquals(20, committed.eventCursor);
        assertEquals("B", store.readCommittedRiskSnapshot(committed).get("k"));
        assertEquals(snapB, store.read(SessionStore.RISK_SNAPSHOT));
        assertFalse(Files.exists(store.file(SessionStore.RISK_SNAPSHOT_NEXT)));

        // a snapshot that is not the committed one is refused
        store.writeAtomic(SessionStore.RISK_SNAPSHOT, snapA);
        try {
            store.readCommittedRiskSnapshot(store.readState());
            fail("a risk snapshot the state does not commit to was accepted");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("risk_snapshot_sha256"));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("refusing to resume"));
        }
    }

    /** A session's own checkpoint commits to the snapshot it wrote. */
    @Test
    public void sessionCheckpointCommitsToItsRiskSnapshot() throws IOException {
        PaperTrading.Result res = PaperTrading.run(PaperFixtures.session(600));
        SessionStore store = new SessionStore(res.stateDir);
        SessionStore.State st = store.readState();
        assertEquals(sha(store.read(SessionStore.RISK_SNAPSHOT)),
                st.riskSnapshotSha256);
        assertEquals(res.totalPnl, st.totalPnl, 0.0);
        assertEquals(res.grossPnl, st.grossPnl, 0.0);
    }

    // --------------------------------------------- 3. stop on the loop thread

    /**
     * A stop request is honoured by the trading thread at an event boundary:
     * it checkpoints (cursor, P&amp;L, risk snapshot — one commit) and ends
     * the session as STOPPED; the checkpoint resumes to the end.
     */
    @Test
    public void stopRequestCheckpointsOnTheTradingThreadAndResumes()
            throws Exception {
        PaperTrading.Options opts = PaperFixtures.session(2000);
        opts.realtime = true;
        opts.speed = 700.0;
        opts.checkpointEveryEvents = 0; // only the stop path may checkpoint
        AtomicReference<PaperTrading.Result> live = new AtomicReference<>();
        opts.onServerStarted = live::set;
        PaperTrading.Result[] out = new PaperTrading.Result[1];
        Throwable[] err = new Throwable[1];
        Thread session = new Thread(() -> {
            try {
                out[0] = PaperTrading.run(opts);
            } catch (Throwable t) {
                err[0] = t;
            }
        }, "paper-stop");
        session.setDaemon(true);
        session.start();
        long deadline = System.nanoTime() + 60_000_000_000L;
        while (System.nanoTime() < deadline && session.isAlive()) {
            PaperTrading.Result r = live.get();
            if (r != null && r.eventsProcessed >= 900) {
                r.requestStop();
                break;
            }
            Thread.sleep(1);
        }
        session.join(60_000);
        if (err[0] != null) {
            throw new AssertionError(err[0]);
        }
        PaperTrading.Result res = out[0];
        assertEquals(PaperTrading.SessionState.STOPPED, res.state);
        assertTrue("stopped mid-stream: " + res.eventsProcessed,
                res.eventsProcessed >= 900 && res.eventsProcessed < 2000);
        assertEquals(503, PaperTrading.ready(res, opts, 1L).code());
        assertEquals(200, PaperTrading.health(res).code());

        SessionStore store = new SessionStore(res.stateDir);
        SessionStore.State st = store.readState();
        assertEquals("the cursor is the stop point", res.eventsProcessed,
                st.eventCursor);
        assertEquals("P&L is persisted by a mid-session checkpoint",
                res.totalPnl, st.totalPnl, 0.0);
        assertEquals(res.grossPnl, st.grossPnl, 0.0);
        assertTrue("the session had traded before the stop", st.fillCount > 0);
        assertNotEquals(0.0, st.totalPnl, 0.0);
        assertEquals(sha(store.read(SessionStore.RISK_SNAPSHOT)),
                st.riskSnapshotSha256);
        assertEquals("audit cursor matches the file", st.auditLines,
                PaperFixtures.lines(store.file(SessionStore.RISK_AUDIT)).size());

        PaperTrading.Options leg2 = PaperFixtures.session(2000);
        leg2.stateDir = res.stateDir;
        leg2.resume = true;
        PaperTrading.Result r2 = PaperTrading.run(leg2);
        assertEquals(PaperTrading.SessionState.FINISHED, r2.state);
        assertEquals(1, r2.restarts);
        assertEquals(2000, r2.eventsProcessed);
        assertTrue(r2.fillCount >= st.fillCount);
        String restoredLine = PaperFixtures.lines(
                store.file(SessionStore.RISK_AUDIT)).get((int) st.auditLines);
        assertTrue(restoredLine, restoredLine.contains("STATE_RESTORED"));
    }

    // ------------------------------------------------------- 4. kill latch

    /**
     * A kill accepted while the trading thread is not draining (quiet feed)
     * latches immediately, answers 202 instead of being dropped, blocks the
     * next order and is recorded by the risk engine as soon as the order
     * path runs.
     */
    @Test
    public void killLatchesImmediatelyAndIsNeverDropped() throws IOException {
        Path dir = Files.createTempDirectory("iap-admin");
        RiskEngine risk = RiskEngines.armed();
        AdminService svc = service(risk, dir,
                AdminService.singleOperator(TOKEN), true)
                .withApplyTimeoutMs(50);
        MetricsServer.HttpResult r = svc.handle("kill", TOKEN, GLOBAL_KILL,
                "10.0.0.9");
        assertEquals(r.json(), 202, r.code());
        assertEquals(Boolean.TRUE, PaperFixtures.json(r.json()).get("ok"));
        assertTrue("the kill is latched", svc.killPending());
        assertFalse("not yet in the risk engine", risk.killSwitchEngaged());

        // a non-kill command that times out is withdrawn and never applied
        assertEquals(503, svc.handle("override", TOKEN, Map.of("scope", "global",
                "reason", "raise", "limit", "5"), "10.0.0.9").code());

        // the order path: drains first, so the kill is applied before the check
        PaperTrading.RiskWiring wiring = new PaperTrading.RiskWiring(risk,
                "PAPER", 1, new MetricsRegistry()).withAdmin(svc);
        assertEquals("no order passes a pending kill", 0,
                wiring.hook().approve(1, 0, 0, 100, 2L));
        assertTrue("the risk engine holds the kill", risk.killSwitchEngaged());
        assertFalse(svc.killPending());
        assertEquals("the withdrawn override was not applied", 0, svc.drain());

        List<String> audit = PaperFixtures.lines(
                dir.resolve(SessionStore.ADMIN_AUDIT));
        String joined = String.join("\n", audit);
        assertTrue(joined, joined.contains("\"code\":202"));
        String late = audit.stream().filter(l -> l.contains("\"code\":200"))
                .findFirst().orElse("");
        assertTrue(joined, late.contains("applied after the request returned 202"));
        assertTrue("the audit says resting orders were not cancelled: " + late,
                late.contains("NOT cancelled"));
        assertTrue(late, late.contains("\"operator\":\"admin\""));
        assertTrue(late, late.contains("\"remote\":\"10.0.0.9\""));
        assertTrue(risk.auditJsonl().contains("KILL_SWITCH_ENGAGED"));
    }

    /** With a draining trading thread a kill answers 200 and cancels working orders. */
    @Test
    public void appliedKillRunsTheCancelPathAndReportsIt() throws Exception {
        Path dir = Files.createTempDirectory("iap-admin");
        RiskEngine risk = RiskEngines.armed();
        AdminService svc = service(risk, dir,
                AdminService.singleOperator(TOKEN), true);
        List<String> cancelled = new ArrayList<>();
        svc.onKill((scope, id, ts) -> {
            cancelled.add(scope.name());
            return "cancel requested for 3 working child orders";
        });
        AtomicBoolean stop = new AtomicBoolean();
        Thread trading = new Thread(() -> {
            while (!stop.get()) {
                svc.drain();
                try {
                    Thread.sleep(1);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }, "trading");
        trading.setDaemon(true);
        trading.start();
        try {
            MetricsServer.HttpResult r = svc.handle("kill", TOKEN, GLOBAL_KILL);
            assertEquals(r.json(), 200, r.code());
            assertTrue(r.json(), r.json().contains(
                    "cancel requested for 3 working child orders"));
        } finally {
            stop.set(true);
            trading.join(5_000);
        }
        assertEquals(List.of("GLOBAL"), cancelled);
        assertTrue(risk.killSwitchEngaged());
        assertFalse(svc.killPending());
        // the engine-side cancel path tolerates a session that has no engine
        assertTrue(PaperTrading.cancelWorkingChildren(null, 1L)
                .contains("NOT cancelled"));
    }

    // ---------------------------------------------------- 5. admin hardening

    /** Loopback by default; {@code IAP_BIND_ADDR} overrides. */
    @Test
    public void listenerBindsLoopbackUnlessConfigured() throws IOException {
        assertEquals("127.0.0.1", MetricsServer.bindAddress(Map.of()));
        assertEquals("127.0.0.1", MetricsServer.bindAddress(
                Map.of(MetricsServer.BIND_ADDR_ENV, "  ")));
        assertEquals("0.0.0.0", MetricsServer.bindAddress(
                Map.of(MetricsServer.BIND_ADDR_ENV, " 0.0.0.0 ")));
        MetricsServer server = new MetricsServer(new MetricsRegistry(), 0,
                () -> "{}", () -> new MetricsServer.HttpResult(200, "{}"),
                () -> new MetricsServer.HttpResult(200, "{}"), null, "127.0.0.1");
        try {
            assertEquals("127.0.0.1", server.boundAddress());
        } finally {
            server.stop();
        }
    }

    /** Failed auth: 429 after the limit, one summary line, valid tokens unaffected. */
    @Test
    public void failedAuthIsRateLimitedWithoutAnAuditFlood() throws IOException {
        Path dir = Files.createTempDirectory("iap-admin");
        long[] clock = {1_000L};
        AdminService svc = service(RiskEngines.armed(), dir,
                AdminService.singleOperator(TOKEN), false)
                .withClock(() -> clock[0]);
        for (int i = 0; i < AdminService.MAX_AUTH_FAILURES; i++) {
            assertEquals(403, svc.handle("kill", "wrong-" + i, GLOBAL_KILL,
                    "10.0.0.66").code());
        }
        for (int i = 0; i < 25; i++) {
            assertEquals(429, svc.handle("kill", i % 2 == 0 ? "wrong" : null,
                    GLOBAL_KILL, "10.0.0.66").code());
        }
        Path file = dir.resolve(SessionStore.ADMIN_AUDIT);
        assertEquals("one line per failure only up to the limit",
                AdminService.MAX_AUTH_FAILURES, PaperFixtures.lines(file).size());
        // a valid token is never locked out by a flood of bad ones
        assertEquals(503, svc.handle("kill", TOKEN, GLOBAL_KILL, "10.0.0.7")
                .code());

        clock[0] += AdminService.WINDOW_NS;
        assertEquals("a new window answers normally again", 403,
                svc.handle("kill", "wrong", GLOBAL_KILL, "10.0.0.66").code());
        List<String> lines = PaperFixtures.lines(file);
        long summaries = lines.stream()
                .filter(l -> l.contains("\"action\":\"audit_summary\"")).count();
        assertEquals("exactly one summary line for the window", 1, summaries);
        String summary = lines.stream()
                .filter(l -> l.contains("audit_summary")).findFirst().orElse("");
        assertTrue(summary, summary.contains("25 rejected admin requests"));
        assertFalse("a presented token is never written",
                String.join("\n", lines).contains("wrong-3"));
        assertTrue(lines.get(0), lines.get(0).contains("\"remote\":\"10.0.0.66\""));
    }

    /** Rejected requests cannot grow the audit without bound. */
    @Test
    public void rejectedRequestsAreAuditedUpToACapPerWindow() throws IOException {
        Path dir = Files.createTempDirectory("iap-admin");
        long[] clock = {1_000L};
        AdminService svc = service(RiskEngines.armed(), dir,
                AdminService.singleOperator(TOKEN), true)
                .withClock(() -> clock[0]);
        String huge = "x".repeat(6000);
        for (int i = 0; i < AdminService.MAX_REJECT_AUDIT_LINES + 20; i++) {
            assertEquals(400, svc.handle("kill", TOKEN,
                    Map.of("scope", "global", "reason", huge), "10.0.0.7").code());
        }
        Path file = dir.resolve(SessionStore.ADMIN_AUDIT);
        List<String> lines = PaperFixtures.lines(file);
        assertEquals(AdminService.MAX_REJECT_AUDIT_LINES, lines.size());
        for (String l : lines) {
            assertTrue("an oversized reason is clipped in the audit: " + l.length(),
                    l.length() < 1200);
        }
        svc.shutdown();
        List<String> after = PaperFixtures.lines(file);
        assertEquals(AdminService.MAX_REJECT_AUDIT_LINES + 1, after.size());
        assertTrue(after.get(after.size() - 1),
                after.get(after.size() - 1).contains("20 rejected admin requests"));
    }

    /** Several operators, each with an own token; the audit names the actor. */
    @Test
    public void operatorTokensFileAttributesEachOperator() throws IOException {
        Path f = Files.createTempFile("iap-operators", ".txt");
        Files.write(f, ("# operator_id:sha256hex\nalice:" + sha("token-alice")
                + "\n\nbob:" + sha("token-bob").toUpperCase(java.util.Locale.ROOT)
                + "\n").getBytes(StandardCharsets.UTF_8));
        Map<String, String> ops = AdminService.operatorsFromEnv(Map.of(
                AdminService.TOKENS_FILE_ENV, f.toString(),
                "IAP_ADMIN_TOKEN", TOKEN));
        assertEquals(new TreeMap<>(Map.of("alice", sha("token-alice"),
                "bob", sha("token-bob"),
                AdminService.DEFAULT_OPERATOR, sha(TOKEN))), new TreeMap<>(ops));
        assertTrue(AdminService.operatorsFromEnv(Map.of()).isEmpty());

        Path dir = Files.createTempDirectory("iap-admin");
        AdminService svc = service(RiskEngines.armed(), dir, ops, false);
        assertTrue(svc.enabled());
        assertEquals(503, svc.handle("kill", "token-alice", GLOBAL_KILL,
                "10.1.2.3").code());
        assertEquals(503, svc.handle("kill", "token-bob", GLOBAL_KILL,
                "10.1.2.4").code());
        assertEquals(503, svc.handle("kill", TOKEN, GLOBAL_KILL).code());
        assertEquals(403, svc.handle("kill", "token-mallory", GLOBAL_KILL,
                "10.1.2.5").code());
        List<String> lines = PaperFixtures.lines(
                dir.resolve(SessionStore.ADMIN_AUDIT));
        assertEquals(4, lines.size());
        assertTrue(lines.get(0), lines.get(0).contains("\"operator\":\"alice\"")
                && lines.get(0).contains("\"remote\":\"10.1.2.3\"")
                && lines.get(0).contains(sha("token-alice")));
        assertTrue(lines.get(1), lines.get(1).contains("\"operator\":\"bob\""));
        assertTrue(lines.get(2), lines.get(2).contains("\"operator\":\"admin\""));
        assertTrue(lines.get(3), lines.get(3).contains("\"operator\":\"\"")
                && lines.get(3).contains("\"code\":403"));

        Files.write(f, "alice-without-a-hash\n".getBytes(StandardCharsets.UTF_8));
        try {
            AdminService.operatorsFromEnv(
                    Map.of(AdminService.TOKENS_FILE_ENV, f.toString()));
            fail("a malformed operator line must fail fast");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(), expected.getMessage()
                    .contains(AdminService.TOKENS_FILE_ENV));
        }
        try {
            AdminService.operatorsFromEnv(
                    Map.of(AdminService.TOKENS_FILE_ENV, "/no/such/operators"));
            fail("a named but unreadable operators file must fail fast");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(), expected.getMessage()
                    .contains(AdminService.TOKENS_FILE_ENV));
        }
    }

    // ---------------------------------------------------------- 6. gap gate

    private static MarketEvent ev(int venue, long seq, int type, int side,
            long price, long qty, long orderId, long ts) {
        return new MarketEvent(venue * 1000L + seq, 1, venue, ts, ts, seq, type,
                side, price, qty, orderId, 0);
    }

    /** One venue's recovery must not reopen the gate while another is stale. */
    @Test
    public void gapGateReopensOnlyWhenNoVenueIsStale() {
        ConfigService cfg = cfg();
        RiskEngine risk = RiskEngine.fromConfig(cfg.riskDoc(),
                cfg.riskInstruments());
        PaperTrading.RiskWiring wiring = new PaperTrading.RiskWiring(risk,
                "PAPER", 1, new MetricsRegistry());
        ConsolidatedBook book = new ConsolidatedBook(1);
        List<MarketEvent> script = List.of(
                ev(1, 1, EventType.HEARTBEAT, 0, 0, 0, 0, 10),
                ev(2, 1, EventType.HEARTBEAT, 0, 0, 0, 0, 11),
                ev(1, 5, EventType.HEARTBEAT, 0, 0, 0, 0, 12),   // gap on 1
                ev(2, 5, EventType.HEARTBEAT, 0, 0, 0, 0, 13));  // gap on 2
        for (MarketEvent e : script) {
            book.apply(e);
            wiring.onMarket(e, book);
        }
        assertTrue(book.venueBook(1).isStale());
        assertTrue(book.venueBook(2).isStale());
        assertTrue(risk.snapshot(), risk.snapshot().contains("\"gated\":true"));

        // venue 1 recovers with a complete one-record SNAPSHOT burst
        MarketEvent rec1 = ev(1, 6, EventType.SNAPSHOT, 0, 10000, 7, 11, 14);
        book.apply(rec1);
        wiring.onMarket(rec1, book);
        assertFalse(book.venueBook(1).isStale());
        assertTrue(book.venueBook(2).isStale());
        assertTrue("venue 2 is still stale: the gate stays closed",
                risk.snapshot().contains("\"gated\":true"));

        MarketEvent rec2 = ev(2, 6, EventType.SNAPSHOT, 0, 10000, 7, 12, 15);
        book.apply(rec2);
        wiring.onMarket(rec2, book);
        assertFalse(book.venueBook(2).isStale());
        assertTrue("no venue is stale: the gate reopens",
                risk.snapshot().contains("\"gated\":false"));
        assertFalse(risk.snapshot().contains("\"gated\":true"));
    }

    // -------------------------------------------------------- 7. sizing hash

    /** Renaming the sizing-problem keys leaves the traces deterministic. */
    @Test
    public void tracesStayDeterministicWithTheRenamedSizingProblem()
            throws IOException {
        PaperTrading.Result a = PaperTrading.run(PaperFixtures.session(600));
        PaperTrading.Result b = PaperTrading.run(PaperFixtures.session(600));
        assertEquals("identical runs, identical traces", a.traceDigest,
                b.traceDigest);
    }
}
