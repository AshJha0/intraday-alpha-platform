package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Map;

import org.junit.Test;

import com.iap.config.ConfigService;
import com.iap.platform.PaperTrading;

/**
 * Startup configuration contract (PLATFORM_CONVENTIONS.md §12.2). Round-3
 * findings SEV-1 "the ENGAGE path does nothing — IAP_CONFIG_DIR is unread"
 * and SEV-3 "startup validation gaps"; proposed tests 17 (ConfigFailFast) and
 * 19 (InstrumentSpecValidation).
 */
public class PlatformConfigTest {
    /** SEV-1 — the pinned resolution order --configs > $IAP_CONFIG_DIR > default. */
    @Test
    public void configDirResolutionHonoursIapConfigDir() throws IOException {
        Path copy = PaperFixtures.copyConfigs();
        Path fallback = Paths.get("..", "configs");
        // explicit flag wins
        assertEquals(copy, ConfigService.resolveDir(copy,
                Map.of("IAP_CONFIG_DIR", "/nope"), fallback));
        // else the environment
        assertEquals(copy, ConfigService.resolveDir(null,
                Map.of("IAP_CONFIG_DIR", copy.toString()), fallback));
        // blank is unset
        assertEquals(fallback, ConfigService.resolveDir(null,
                Map.of("IAP_CONFIG_DIR", "  "), fallback));
        assertEquals(fallback, ConfigService.resolveDir(null, Map.of(),
                fallback));
        // a named but missing directory fails fast: an operator's override
        // must never silently fall back to the image's baked-in configs
        try {
            ConfigService.resolveDir(null,
                    Map.of("IAP_CONFIG_DIR", "/no/such/configs"), fallback);
            fail("missing IAP_CONFIG_DIR accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("IAP_CONFIG_DIR"));
        }
    }

    /** Test 17 — CLI arguments are validated before any bind or decode. */
    @Test
    public void badCliArgumentsAreRejectedBeforeBinding() {
        expectBad(o -> o.speed = 0.0, "--speed");
        expectBad(o -> o.speed = -1.0, "--speed");
        expectBad(o -> o.speed = Double.NaN, "--speed");
        expectBad(o -> o.port = 70000, "--port");
        expectBad(o -> o.port = -2, "--port");
        expectBad(o -> o.maxEvents = 0, "--max-events");
        expectBad(o -> o.instrumentId = 0, "--instrument");
        expectBad(o -> o.alphaId = "  ", "--alpha");
        expectBad(o -> o.maxPos = 0, "--max-pos");
        expectBad(o -> o.eventsFile = Paths.get("no", "such", "events.jsonl"),
                "--events");
    }

    private static void expectBad(java.util.function.Consumer<PaperTrading.Options> mutate,
            String flag) {
        PaperTrading.Options o = new PaperTrading.Options();
        o.configsDir = PaperFixtures.configs();
        o.eventsFile = PaperFixtures.goldenEvents();
        mutate.accept(o);
        try {
            PaperTrading.validate(o);
            fail("accepted a bad " + flag);
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains(flag));
        }
    }

    /** Test 17 — a strategies.json without `adaptive` names file and key. */
    @Test
    public void missingAdaptiveBlockIsAnIaeNotAnNpe() throws IOException {
        Path dir = PaperFixtures.copyConfigs();
        String doc = PaperFixtures.readConfig(dir, "strategies.json");
        int at = doc.indexOf("\"adaptive\"");
        assertTrue("fixture has an adaptive block", at > 0);
        // drop the whole adaptive block by renaming it
        PaperFixtures.writeConfig(dir, "strategies.json",
                doc.replace("\"adaptive\"", "\"adaptive_disabled\""));
        try {
            new ConfigService(dir);
            fail("missing adaptive block accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("strategies.json"));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("adaptive"));
        }
    }

    /** ... and a missing/zero key inside it, naming the key. */
    @Test
    public void badAdaptiveKeyNamesTheKey() throws IOException {
        Path dir = PaperFixtures.copyConfigs();
        String doc = PaperFixtures.readConfig(dir, "strategies.json");
        assertTrue(doc.contains("\"ic_window_ns\""));
        PaperFixtures.writeConfig(dir, "strategies.json",
                doc.replace("\"ic_window_ns\"", "\"ic_window_nanos\""));
        try {
            new ConfigService(dir);
            fail("missing adaptive.ic_window_ns accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("adaptive.ic_window_ns"));
        }
    }

    /** Test 19 — instruments.json lot_size <= 0 / adv <= 0 fail at load. */
    @Test
    public void instrumentReferenceDataIsValidated() throws IOException {
        Path dir = PaperFixtures.copyConfigs();
        String doc = PaperFixtures.readConfig(dir, "instruments.json");
        assertTrue(doc.contains("\"lot_size\""));
        PaperFixtures.writeConfig(dir, "instruments.json",
                doc.replaceFirst("\"lot_size\"\\s*:\\s*[0-9.]+",
                        "\"lot_size\": 0"));
        try {
            new ConfigService(dir);
            fail("lot_size 0 accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("instruments.json"));
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("lot_size"));
        }

        Path dir2 = PaperFixtures.copyConfigs();
        String doc2 = PaperFixtures.readConfig(dir2, "instruments.json");
        PaperFixtures.writeConfig(dir2, "instruments.json",
                doc2.replaceFirst("\"adv\"\\s*:\\s*[0-9.eE+-]+", "\"adv\": -1"));
        try {
            new ConfigService(dir2);
            fail("negative adv accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("adv"));
        }
    }

    /** The config sha256 identifies the whole configuration and is stable. */
    @Test
    public void configSha256IsStableAndChangesWithContent() throws IOException {
        ConfigService a = new ConfigService(PaperFixtures.configs());
        ConfigService b = new ConfigService(PaperFixtures.configs());
        assertEquals(a.configSha256(), b.configSha256());
        assertEquals(64, a.configSha256().length());
        Path dir = PaperFixtures.copyConfigs();
        ConfigService c = new ConfigService(dir);
        assertEquals(a.configSha256(), c.configSha256());
        PaperFixtures.writeConfig(dir, "risk.json",
                PaperFixtures.readConfig(dir, "risk.json")
                        .replace("\"max_daily_loss\": 250000",
                                "\"max_daily_loss\": 249999"));
        assertTrue(new ConfigService(dir).configSha256()
                .equals(a.configSha256()) == false);
    }

    /** The config audit is written to the state dir at startup (§12.2). */
    @Test
    public void configAuditIsPersistedAtStartup() throws IOException {
        PaperTrading.Options opts = PaperFixtures.session(300);
        PaperTrading.Result res = PaperTrading.run(opts);
        Path audit = res.stateDir.resolve(
                com.iap.platform.SessionStore.CONFIG_AUDIT);
        assertTrue("config_audit.jsonl written", Files.exists(audit));
        var lines = PaperFixtures.lines(audit);
        assertEquals("one config_loaded line per pinned file", 6, lines.size());
        for (String line : lines) {
            Map<String, Object> doc = PaperFixtures.json(line);
            assertEquals("config_loaded", doc.get("action"));
            assertEquals(64, String.valueOf(doc.get("sha256")).length());
        }
    }
}
