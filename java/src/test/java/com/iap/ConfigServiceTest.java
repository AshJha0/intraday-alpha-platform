package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.config.ConfigService;
import com.iap.execution.InstrumentSpec;

/**
 * Typed config service: loads and validates the pinned configs/, hashes
 * every file (audit trail with sha256), detects changes on reload, and
 * rejects malformed documents.
 */
public class ConfigServiceTest {
    private static final Path CONFIGS = Paths.get("..", "configs");

    @Test
    public void loadsAndTypesTheRepoConfigs() {
        ConfigService cfg = new ConfigService(CONFIGS);
        Map<Long, InstrumentSpec> instruments = cfg.instruments();
        assertTrue("universe present", instruments.size() >= 10);
        InstrumentSpec eq1 = instruments.get(1L);
        assertEquals(0.01, eq1.tickSize(), 0.0);
        assertEquals(38_000_000.0, eq1.adv(), 0.0);
        assertEquals(0.01, cfg.tickSizes().get(1L), 0.0);
        assertTrue(cfg.venues().containsKey(1));
        assertEquals(20260829L, cfg.executionSeed());
        assertEquals(1000L, cfg.maxChildQty());
        assertEquals(2.0, cfg.impactCoeffBpsPerPctAdv(), 0.0);
        assertEquals(8080, cfg.monitoringPort()); // default (no monitoring key)
        assertTrue(Json.object(cfg.riskDoc().get("global"))
                .containsKey("max_daily_loss"));
    }

    @Test
    public void auditTrailCarriesSha256PerFile() {
        ConfigService cfg = new ConfigService(CONFIGS);
        List<ConfigService.Audit> audit = cfg.auditTrail();
        assertEquals(6, audit.size());
        for (ConfigService.Audit a : audit) {
            assertEquals("config_loaded", a.action());
            assertEquals(64, a.sha256().length());
            assertEquals(null, a.previousSha256());
            assertTrue(a.bytes() > 0);
        }
        assertEquals(cfg.sha256("risk.json"), audit.stream()
                .filter(a -> a.file().equals("risk.json"))
                .findFirst().orElseThrow().sha256());
        // JSONL renders one parseable sorted-key object per line
        String jsonl = cfg.auditJsonl();
        assertEquals(6, jsonl.lines().count());
        jsonl.lines().forEach(line -> {
            Map<String, Object> obj = Json.object(Json.parse(line));
            assertEquals(List.of("action", "bytes", "file", "previous_sha256",
                    "sha256"), List.copyOf(obj.keySet()));
        });
    }

    @Test
    public void reloadDetectsChangesAndAuditsBothHashes() throws IOException {
        // copy the configs into a scratch dir so nothing outside java/ moves
        Path dir = Files.createTempDirectory("iap-configs");
        for (String name : new String[] {"risk.json", "instruments.json",
                "venues.json", "execution.json", "strategies.json",
                "generator.json"}) {
            Files.copy(CONFIGS.resolve(name), dir.resolve(name));
        }
        ConfigService cfg = new ConfigService(dir);
        String before = cfg.sha256("risk.json");
        assertFalse("no change yet", cfg.reload("risk.json"));
        assertEquals(6, cfg.auditTrail().size());
        // mutate the file -> reload reports the change and audits it
        String text = new String(Files.readAllBytes(dir.resolve("risk.json")),
                StandardCharsets.UTF_8)
                .replace("\"max_daily_loss\": 250000.0",
                        "\"max_daily_loss\": 300000.0");
        Files.write(dir.resolve("risk.json"),
                text.getBytes(StandardCharsets.UTF_8));
        assertTrue(cfg.reload("risk.json"));
        List<ConfigService.Audit> audit = cfg.auditTrail();
        ConfigService.Audit change = audit.get(audit.size() - 1);
        assertEquals("config_changed", change.action());
        assertEquals(before, change.previousSha256());
        assertFalse(before.equals(change.sha256()));
        assertEquals(300000.0, Json.asDouble(Json.object(
                cfg.riskDoc().get("global")).get("max_daily_loss")), 0.0);
    }

    @Test
    public void malformedConfigsAreRejected() throws IOException {
        Path dir = Files.createTempDirectory("iap-bad-configs");
        for (String name : new String[] {"risk.json", "instruments.json",
                "venues.json", "execution.json", "strategies.json",
                "generator.json"}) {
            Files.copy(CONFIGS.resolve(name), dir.resolve(name));
        }
        // missing file
        Files.delete(dir.resolve("generator.json"));
        try {
            new ConfigService(dir);
            fail("missing file must fail");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage().contains("generator.json"));
        }
        // malformed JSON
        Files.write(dir.resolve("generator.json"),
                "{not json".getBytes(StandardCharsets.UTF_8));
        try {
            new ConfigService(dir);
            fail("bad json must fail");
        } catch (IllegalArgumentException expected) {
            assertTrue(true);
        }
        // structurally wrong document (no x-version)
        Files.write(dir.resolve("generator.json"),
                "{\"seed\": 1}".getBytes(StandardCharsets.UTF_8));
        try {
            new ConfigService(dir);
            fail("missing x-version must fail");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("x-version"));
        }
        // unknown file access is rejected
        Files.copy(CONFIGS.resolve("generator.json"),
                dir.resolve("generator.json"),
                java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        ConfigService ok = new ConfigService(dir);
        try {
            ok.doc("nope.json");
            fail("unknown config file");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("unknown config"));
        }
    }
}
