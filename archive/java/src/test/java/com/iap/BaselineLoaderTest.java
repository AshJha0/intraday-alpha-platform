package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Locale;
import java.util.Map;

import org.junit.Test;

import com.iap.adaptive.BaselineLoader;
import com.iap.adaptive.Psi;

/**
 * research/baselines/*.json loader (API_ADAPTIVE.md schema): round-trip of
 * the pinned bucket_edges/bucket_fractions form, derivation from a raw
 * values array, and directory loading keyed by alpha id.
 */
public class BaselineLoaderTest {
    private static final double[] EDGES =
            {-4, -3, -2, -1, 0, 1, 2, 3, 4};
    private static final double[] FRACTIONS =
            {0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1};

    private static String json(String alphaId, double[] edges,
            double[] fractions, long count) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"alpha_id\":\"").append(alphaId)
                .append("\",\"bucket_edges\":[");
        for (int i = 0; i < edges.length; i++) {
            sb.append(i == 0 ? "" : ",")
                    .append(String.format(Locale.ROOT, "%s", edges[i]));
        }
        sb.append("],\"bucket_fractions\":[");
        for (int i = 0; i < fractions.length; i++) {
            sb.append(i == 0 ? "" : ",")
                    .append(String.format(Locale.ROOT, "%s", fractions[i]));
        }
        sb.append("],\"count\":").append(count)
                .append(",\"feature_version\":\"").append(FEATURE_VERSION)
                .append("\",\"x-version\":")
                .append(BaselineLoader.SCHEMA_VERSION).append("}");
        return sb.toString();
    }

    /** Stand-in feature-registry hash for the synthetic baselines here. */
    private static final String FEATURE_VERSION =
            "585dd7b92b738f9da7df863dba1ac049ff10b75b60a8559baac1e49b6f3062ac";

    @Test
    public void supersededSchemaVersionIsRejected() throws IOException {
        Path dir = Files.createTempDirectory("iap-baselines-v1");
        Path file = dir.resolve("signal_eq01.json");
        Files.write(file,
                json("EQ01", EDGES, FRACTIONS, 10).replace(
                        "\"x-version\":" + BaselineLoader.SCHEMA_VERSION,
                        "\"x-version\":1")
                        .getBytes(StandardCharsets.UTF_8));
        try {
            BaselineLoader.load(file);
            org.junit.Assert.fail("a v1 baseline must be rejected");
        } catch (IllegalArgumentException e) {
            org.junit.Assert.assertTrue(e.getMessage(),
                    e.getMessage().contains("x-version"));
        }
    }

    @Test
    public void baselineFromFeatureRegistryMismatchIsRejected()
            throws IOException {
        Path dir = Files.createTempDirectory("iap-baselines-fv");
        Path file = dir.resolve("signal_eq01.json");
        Files.write(file, json("EQ01", EDGES, FRACTIONS, 10)
                .getBytes(StandardCharsets.UTF_8));
        BaselineLoader.Baseline b = BaselineLoader.load(file);
        org.junit.Assert.assertEquals(FEATURE_VERSION, b.featureVersion());
        // matching registry: accepted
        BaselineLoader.requireFeatureVersion(b, FEATURE_VERSION);
        // a different registry means the feature semantics may have moved
        try {
            BaselineLoader.requireFeatureVersion(b, "0".repeat(64));
            org.junit.Assert.fail("a foreign feature_version must be rejected");
        } catch (IllegalArgumentException e) {
            org.junit.Assert.assertTrue(e.getMessage(),
                    e.getMessage().contains("feature_version"));
        }
    }

    @Test
    public void roundTripOfThePinnedSchema() throws IOException {
        Path dir = Files.createTempDirectory("iap-baselines");
        Path file = dir.resolve("signal_eq01.json");
        Files.write(file, json("EQ01", EDGES, FRACTIONS, 77856)
                .getBytes(StandardCharsets.UTF_8));
        BaselineLoader.Baseline b = BaselineLoader.load(file);
        assertEquals("EQ01", b.alphaId());
        assertEquals(77856, b.count());
        assertArrayEquals(EDGES, b.edges(), 0.0);
        assertArrayEquals(FRACTIONS, b.fractions(), 0.0);
        // loaded baseline against itself is exactly zero drift
        assertEquals(0.0, Psi.psi(b.fractions(), b.fractions()), 0.0);
    }

    @Test
    public void rawValuesFormDerivesEdgesAndFractionsViaThePinnedFormulas()
            throws IOException {
        double[] values = new double[101];
        for (int i = 0; i <= 100; i++) {
            values[i] = i * 0.01; // quantile p is exactly p
        }
        StringBuilder sb = new StringBuilder(
                "{\"alpha_id\":\"EQ02\",\"values\":[");
        for (int i = 0; i < values.length; i++) {
            sb.append(i == 0 ? "" : ",")
                    .append(String.format(Locale.ROOT, "%s", values[i]));
        }
        sb.append("]}");
        Path dir = Files.createTempDirectory("iap-baselines");
        Path file = dir.resolve("signal_eq02.json");
        Files.write(file, sb.toString().getBytes(StandardCharsets.UTF_8));
        BaselineLoader.Baseline b = BaselineLoader.load(file);
        assertArrayEquals(Psi.edges(values), b.edges(), 0.0);
        assertArrayEquals(Psi.fractions(values, Psi.edges(values)),
                b.fractions(), 0.0);
        assertEquals(values.length, b.count());
    }

    @Test
    public void loadDirKeysByAlphaAndMissingDirIsEmpty() throws IOException {
        Path dir = Files.createTempDirectory("iap-baselines");
        Files.write(dir.resolve("signal_eq01.json"),
                json("EQ01", EDGES, FRACTIONS, 10)
                        .getBytes(StandardCharsets.UTF_8));
        Files.write(dir.resolve("signal_fx05.json"),
                json("FX05", EDGES, FRACTIONS, 20)
                        .getBytes(StandardCharsets.UTF_8));
        Files.write(dir.resolve("README.txt"),
                "not json".getBytes(StandardCharsets.UTF_8)); // ignored
        Map<String, BaselineLoader.Baseline> all = BaselineLoader.loadDir(dir);
        assertEquals(2, all.size());
        assertEquals(10, all.get("EQ01").count());
        assertEquals(20, all.get("FX05").count());
        assertTrue(BaselineLoader.loadDir(
                Paths.get("no", "such", "dir")).isEmpty());
    }

    @Test
    public void malformedBaselinesAreRejected() {
        try {
            BaselineLoader.parse(com.iap.config.Json.object(
                    com.iap.config.Json.parse(
                            "{\"bucket_edges\":[1],\"bucket_fractions\":[1]}")));
            fail("missing alpha_id");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("alpha_id"));
        }
        try {
            BaselineLoader.parse(com.iap.config.Json.object(
                    com.iap.config.Json.parse("{\"alpha_id\":\"X\","
                            + "\"bucket_edges\":[1,2,3],"
                            + "\"bucket_fractions\":[1]}")));
            fail("wrong edge count");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("9"));
        }
        try {
            BaselineLoader.parse(com.iap.config.Json.object(
                    com.iap.config.Json.parse("{\"alpha_id\":\"X\"}")));
            fail("no distribution at all");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("values"));
        }
    }
}
