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

    /**
     * Copy exactly the six pinned files (nested layout, PLATFORM_CONVENTIONS
     * §0) into a fresh temp dir — no alpha_params.json, so the test also
     * proves ConfigService needs nothing else.
     */
    private static Path copyPinnedConfigs(String prefix) throws IOException {
        Path dir = Files.createTempDirectory(prefix);
        for (String name : ConfigService.PINNED_FILES) {
            Path dst = ConfigService.resolve(dir, name);
            Files.createDirectories(dst.getParent());
            Files.copy(ConfigService.resolve(CONFIGS, name), dst);
        }
        return dir;
    }

    /** The pinned names are the domain-folder layout of the repo tree. */
    @Test
    public void pinnedFilesUseTheDomainLayout() {
        assertEquals(List.of("risk/risk.json", "instruments/instruments.json",
                "venues/venues.json", "execution/execution.json",
                "strategies/strategies.json", "marketdata/generator.json",
                "strategies/lifecycle.json"),
                List.of(ConfigService.PINNED_FILES));
        assertEquals("strategies/alpha_params.json", ConfigService.ALPHA_PARAMS);
        for (String name : ConfigService.PINNED_FILES) {
            assertTrue(name, Files.isRegularFile(
                    ConfigService.resolve(CONFIGS, name)));
        }
        assertTrue(Files.isRegularFile(
                ConfigService.resolve(CONFIGS, ConfigService.ALPHA_PARAMS)));
    }

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
        assertEquals(7, audit.size());
        for (ConfigService.Audit a : audit) {
            assertEquals("config_loaded", a.action());
            assertEquals(64, a.sha256().length());
            assertEquals(null, a.previousSha256());
            assertTrue(a.bytes() > 0);
        }
        // the audit names every file by its relative path in the tree
        assertEquals(cfg.sha256(ConfigService.RISK), audit.stream()
                .filter(a -> a.file().equals("risk/risk.json"))
                .findFirst().orElseThrow().sha256());
        // JSONL renders one parseable sorted-key object per line
        String jsonl = cfg.auditJsonl();
        assertEquals(7, jsonl.lines().count());
        jsonl.lines().forEach(line -> {
            Map<String, Object> obj = Json.object(Json.parse(line));
            assertEquals(List.of("action", "bytes", "file", "previous_sha256",
                    "sha256"), List.copyOf(obj.keySet()));
        });
    }

    @Test
    public void reloadDetectsChangesAndAuditsBothHashes() throws IOException {
        // copy the configs into a scratch dir so nothing outside java/ moves
        Path dir = copyPinnedConfigs("iap-configs");
        ConfigService cfg = new ConfigService(dir);
        String before = cfg.sha256(ConfigService.RISK);
        assertFalse("no change yet", cfg.reload(ConfigService.RISK));
        assertEquals(7, cfg.auditTrail().size());
        // mutate the file -> reload reports the change and audits it
        Path risk = ConfigService.resolve(dir, ConfigService.RISK);
        String text = new String(Files.readAllBytes(risk),
                StandardCharsets.UTF_8)
                .replace("\"max_daily_loss\": 250000.0",
                        "\"max_daily_loss\": 300000.0");
        Files.write(risk, text.getBytes(StandardCharsets.UTF_8));
        assertTrue(cfg.reload(ConfigService.RISK));
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
        Path dir = copyPinnedConfigs("iap-bad-configs");
        Path generator = ConfigService.resolve(dir, ConfigService.GENERATOR);
        // missing file
        Files.delete(generator);
        try {
            new ConfigService(dir);
            fail("missing file must fail");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage().contains("generator.json"));
        }
        // malformed JSON
        Files.write(generator, "{not json".getBytes(StandardCharsets.UTF_8));
        try {
            new ConfigService(dir);
            fail("bad json must fail");
        } catch (IllegalArgumentException expected) {
            assertTrue(true);
        }
        // structurally wrong document (no x-version)
        Files.write(generator, "{\"seed\": 1}".getBytes(StandardCharsets.UTF_8));
        try {
            new ConfigService(dir);
            fail("missing x-version must fail");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("x-version"));
        }
        // unknown file access is rejected
        Files.copy(ConfigService.resolve(CONFIGS, ConfigService.GENERATOR),
                generator, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        ConfigService ok = new ConfigService(dir);
        try {
            ok.doc("nope.json");
            fail("unknown config file");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("unknown config"));
        }
    }
}
