package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.Test;

import com.iap.platform.AdminService;
import com.iap.platform.PaperTrading;
import com.iap.platform.SessionStore;

/**
 * The runtime kill-switch admin API (PLATFORM_CONVENTIONS.md §12.5) — the
 * fix for round-3 SEV-1 "the documented ENGAGE path does nothing in either
 * deployment". Scenario: a runaway strategy mid-session; ops POSTs
 * {@code /admin/kill} with the approval reference and the very next order is
 * rejected, with the halt visible on {@code /metrics} and in both audit logs.
 */
public class AdminApiTest {
    private static final String TOKEN = "s3cret-approval-token";

    /** ScenarioRunawayStrategyManualKill: engage, verify, clear. */
    @Test
    public void scenarioRunawayStrategyManualKillHaltsAndAudits()
            throws Exception {
        PaperTrading.Options opts = PaperFixtures.session(900);
        opts.realtime = true;
        opts.speed = 700.0;
        opts.port = 0;
        opts.adminToken = TOKEN;
        AtomicInteger port = new AtomicInteger(-1);
        opts.onServerStarted = r -> port.set(r.httpPort);
        PaperTrading.Result[] out = new PaperTrading.Result[1];
        Throwable[] err = new Throwable[1];
        Thread session = new Thread(() -> {
            try {
                out[0] = PaperTrading.run(opts);
            } catch (Throwable t) {
                err[0] = t;
            }
        }, "paper-admin");
        session.setDaemon(true);
        session.start();
        long allowedAtKill = -1;
        try {
            long deadline = System.nanoTime() + 25_000_000_000L;
            while (port.get() < 0 && System.nanoTime() < deadline) {
                Thread.sleep(5);
            }
            assertTrue("server bound", port.get() > 0);
            int p = port.get();

            // no token -> 401; wrong token -> 403; wrong method -> 405
            assertEquals(401, PaperFixtures.request(p, "/admin/kill", "POST",
                    null, Map.of("scope", "global", "reason", "x"))[0]);
            assertEquals(403, PaperFixtures.request(p, "/admin/kill", "POST",
                    "wrong", Map.of("scope", "global", "reason", "x"))[0]);
            assertEquals(405, PaperFixtures.request(p, "/admin/kill", "GET",
                    TOKEN, null)[0]);
            // missing reason / bad scope -> 400, nothing changes
            assertEquals(400, PaperFixtures.request(p, "/admin/kill", "POST",
                    TOKEN, Map.of("scope", "global"))[0]);
            assertEquals(400, PaperFixtures.request(p, "/admin/kill", "POST",
                    TOKEN, Map.of("scope", "planet", "reason", "x"))[0]);
            assertEquals("0", gauge(p, "risk_kill_switch_engaged"));

            // wait until the session is actually trading, then ENGAGE
            long deadline2 = System.nanoTime() + 20_000_000_000L;
            while (System.nanoTime() < deadline2) {
                if (Long.parseLong(counter(p, "risk_allowed_total")) > 0) {
                    break;
                }
                Thread.sleep(5);
            }
            assertTrue("session traded before the kill",
                    Long.parseLong(counter(p, "risk_allowed_total")) > 0);
            Object[] kill = PaperFixtures.request(p, "/admin/kill", "POST",
                    TOKEN, Map.of("scope", "global",
                            "reason", "INC-2026-09-06 runaway EQ01, approved by risk"));
            assertEquals(String.valueOf(kill[1]), 200, kill[0]);
            assertEquals(Boolean.TRUE, PaperFixtures.json(kill[1]).get("ok"));
            // The command was applied ON the trading thread, so no order can
            // have been allowed after it: this reading is the final one.
            allowedAtKill = Long.parseLong(counter(p, "risk_allowed_total"));

            // verify per the runbook: the gauge is 1 and /status agrees
            assertEquals("1", gauge(p, "risk_kill_switch_engaged"));
            assertEquals(Boolean.TRUE, PaperFixtures.json(
                    PaperFixtures.get(p, "/status")[1]).get("kill_switch_engaged"));
            // halted is not unhealthy (§12.5)
            assertEquals(200, PaperFixtures.get(p, "/health")[0]);
            Thread.sleep(150);
            assertEquals("no order is allowed after the latch",
                    allowedAtKill, Long.parseLong(counter(p, "risk_allowed_total")));
        } finally {
            session.join(60_000);
        }
        if (err[0] != null) {
            throw new AssertionError(err[0]);
        }
        PaperTrading.Result res = out[0];
        assertTrue("kill switch latched for the rest of the session",
                res.killSwitchEngaged);
        assertEquals(allowedAtKill, res.riskAllowed);
        assertTrue("rejects climb after the latch", res.riskRejected > 0);

        // both audit trails record the halt with the approval reference
        Path stateDir = res.stateDir;
        List<String> riskAudit = PaperFixtures.lines(
                stateDir.resolve(SessionStore.RISK_AUDIT));
        assertTrue("risk audit persisted", riskAudit.size() > 0);
        String latch = riskAudit.stream()
                .filter(l -> l.contains("KILL_SWITCH_ENGAGED"))
                .findFirst().orElse(null);
        assertNotNull("KILL_SWITCH_ENGAGED in the risk audit JSONL", latch);
        assertTrue(latch, latch.contains("INC-2026-09-06 runaway EQ01"));
        List<String> adminAudit = PaperFixtures.lines(
                stateDir.resolve(SessionStore.ADMIN_AUDIT));
        // 401 + 403 + two 400s + the accepted 200 (the 405 is rejected by
        // the router before it reaches the service, per §12.5)
        assertTrue("every admin request that reached the service is audited: "
                + adminAudit.size(), adminAudit.size() >= 5);
        assertTrue("the refusals are audited with their status",
                String.join("\n", adminAudit).contains("\"code\":401"));
        assertFalse("the token itself is never written",
                String.join("\n", adminAudit).contains(TOKEN));
        String applied = adminAudit.stream()
                .filter(l -> l.contains("\"code\":200")).findFirst().orElse(null);
        assertNotNull("the accepted call is audited", applied);
        assertTrue(applied, applied.contains("\"action\":\"kill\""));
        assertTrue(applied, applied.contains("\"actor_token_sha256\":\""
                + com.iap.codec.Sha256.hex(TOKEN.getBytes(StandardCharsets.UTF_8))));
    }

    /** Without a token the admin routes do not exist at all. */
    @Test
    public void adminApiIsDisabledWithoutAToken() throws Exception {
        AdminService svc = new AdminService(
                RiskEngines.armed(), new SessionStore(
                        Files.createTempDirectory("iap-admin")),
                new com.iap.monitoring.MetricsRegistry(), "",
                () -> 1L, () -> true);
        assertFalse(svc.enabled());
        assertEquals(404, svc.handle("kill", "anything",
                Map.of("scope", "global", "reason", "x")).code());
    }

    /** The token may come from a Secret file; a named-but-missing file fails. */
    @Test
    public void tokenIsReadFromEnvOrSecretFile() throws Exception {
        Path f = Files.createTempFile("iap-token", ".txt");
        Files.write(f, "file-token\nignored\n".getBytes(StandardCharsets.UTF_8));
        assertEquals("file-token", AdminService.tokenFromEnv(
                Map.of("IAP_ADMIN_TOKEN_FILE", f.toString())));
        assertEquals("env-token", AdminService.tokenFromEnv(
                Map.of("IAP_ADMIN_TOKEN", "env-token",
                        "IAP_ADMIN_TOKEN_FILE", f.toString())));
        assertEquals(null, AdminService.tokenFromEnv(Map.of()));
        try {
            AdminService.tokenFromEnv(
                    Map.of("IAP_ADMIN_TOKEN_FILE", "/no/such/secret"));
            fail("a named but unreadable secret file must fail fast");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("IAP_ADMIN_TOKEN_FILE"));
        }
    }

    /** Commands only apply on the trading thread; none applies while idle. */
    @Test
    public void commandsAreRefusedWhenNoSessionIsDraining() throws Exception {
        AdminService svc = new AdminService(RiskEngines.armed(),
                new SessionStore(Files.createTempDirectory("iap-admin")),
                new com.iap.monitoring.MetricsRegistry(), TOKEN,
                () -> 1L, () -> false);
        assertTrue(svc.enabled());
        assertEquals(503, svc.handle("kill", TOKEN,
                Map.of("scope", "global", "reason", "approval REF")).code());
    }

    /** override/roll validate their arguments before touching the engine. */
    @Test
    public void overrideAndRollValidateArguments() throws Exception {
        AdminService svc = new AdminService(RiskEngines.armed(),
                new SessionStore(Files.createTempDirectory("iap-admin")),
                new com.iap.monitoring.MetricsRegistry(), TOKEN,
                () -> 1L, () -> true);
        assertEquals(400, svc.handle("override", TOKEN,
                Map.of("scope", "global", "reason", "r")).code());
        assertEquals(400, svc.handle("override", TOKEN,
                Map.of("scope", "global", "reason", "r", "limit", "-5")).code());
        assertEquals(400, svc.handle("kill", TOKEN,
                Map.of("scope", "instrument", "reason", "r", "id", "abc")).code());
        assertEquals(400, svc.handle("kill", TOKEN,
                Map.of("scope", "strategy", "reason", "r")).code());
    }

    private static String gauge(int port, String name) throws Exception {
        return series(port, name);
    }

    private static String counter(int port, String name) throws Exception {
        return series(port, name);
    }

    private static String series(int port, String name) throws Exception {
        String text = String.valueOf(PaperFixtures.get(port, "/metrics")[1]);
        for (String line : text.split("\n")) {
            if (line.startsWith(name + " ")) {
                return line.substring(name.length() + 1);
            }
        }
        return "0";
    }
}
