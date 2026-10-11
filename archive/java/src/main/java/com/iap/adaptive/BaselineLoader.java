package com.iap.adaptive;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;

/**
 * Loads the research-layer signal baselines ({@code research/baselines/*.json},
 * schema pinned in API_ADAPTIVE.md) that the live {@link DriftMonitor}
 * compares against. A baseline pins, per alpha:
 *
 * <ul>
 *   <li>{@code alpha_id} — the alpha the baseline belongs to;</li>
 *   <li>{@code bucket_edges} — the 9 baseline quantiles 0.1..0.9 (the PSI
 *       bucket edges, {@link Psi#edges});</li>
 *   <li>{@code bucket_fractions} — the 10 baseline per-bucket fractions
 *       (sum 1 up to rounding);</li>
 *   <li>{@code count} — baseline sample size (provenance);</li>
 *   <li>{@code feature_version} — the feature-registry hash the baseline was
 *       captured against. A baseline captured under a different registry
 *       describes a feature whose semantics may have changed under the same
 *       name, so PSI against it is meaningless; callers that know their
 *       engine's registry hash must compare it (see
 *       {@link #requireFeatureVersion}).</li>
 * </ul>
 *
 * <p>The normative v2 schema ({@code iap.adaptive.drift.DriftBaseline})
 * uses {@code edges} / {@code expected_frac} / {@code n} and echoes the
 * pinned constants {@code psi_eps} / {@code n_buckets}, which are verified
 * when present. For cross-schema tolerance the loader also accepts the key
 * aliases {@code bucket_edges}/{@code quantiles} and {@code fractions}/
 * {@code bucket_fractions}/{@code base_fractions}, and — when no edges are
 * present — a raw {@code values} array from which edges and fractions are
 * derived via the pinned {@link Psi} formulas. Unknown keys are ignored.
 */
public final class BaselineLoader {
    /**
     * Pinned baseline schema version. v2 (round 3) adds the
     * {@code feature_version} provenance field; see MIGRATIONS.md.
     */
    public static final long SCHEMA_VERSION = 2;

    /** One loaded distribution baseline (immutable). */
    public static final class Baseline {
        private final String alphaId;
        private final String name;
        private final double[] edges;
        private final double[] fractions;
        private final long count;
        private final String featureVersion;

        Baseline(String alphaId, String name, double[] edges,
                double[] fractions, long count) {
            this(alphaId, name, edges, fractions, count, "");
        }

        Baseline(String alphaId, String name, double[] edges,
                double[] fractions, long count, String featureVersion) {
            this.alphaId = alphaId;
            this.name = name;
            this.edges = edges;
            this.fractions = fractions;
            this.count = count;
            this.featureVersion = featureVersion;
        }

        public String alphaId() {
            return alphaId;
        }

        /** Baseline name (file stem; {@code ""} when the file omitted it). */
        public String name() {
            return name;
        }

        /** The 9 pinned bucket edges (baseline quantiles 0.1..0.9). */
        public double[] edges() {
            return edges.clone();
        }

        /** The 10 baseline per-bucket fractions. */
        public double[] fractions() {
            return fractions.clone();
        }

        /** Baseline sample size ({@code 0} when the file omitted it). */
        public long count() {
            return count;
        }

        /**
         * Feature-registry hash this baseline was captured against
         * (schema v2 provenance; {@code ""} when the file omitted it).
         *
         * @return the registry hash, or {@code ""}
         */
        public String featureVersion() {
            return featureVersion;
        }
    }

    private BaselineLoader() {
    }

    private static double[] doubles(Object v, String what, int want) {
        List<Object> arr = Json.array(v);
        if (arr.size() != want) {
            throw new IllegalArgumentException("baseline " + what
                    + " must have " + want + " entries, got " + arr.size());
        }
        double[] out = new double[arr.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = Json.asDouble(arr.get(i));
        }
        return out;
    }

    private static Object firstOf(Map<String, Object> doc, String... keys) {
        for (String k : keys) {
            Object v = doc.get(k);
            if (v != null) {
                return v;
            }
        }
        return null;
    }

    /**
     * Whether a parsed baseline document is a distribution baseline the
     * drift monitor can consume ({@code kind} absent, {@code "signal"} or
     * {@code "feature"}) — {@code "ic"} baselines carry no distribution.
     */
    public static boolean isDistribution(Map<String, Object> doc) {
        Object kind = doc.get("kind");
        return kind == null || "signal".equals(kind) || "feature".equals(kind);
    }

    /** Parse one distribution-baseline document (class doc schema). */
    public static Baseline parse(Map<String, Object> doc) {
        String alphaId = (String) doc.get("alpha_id");
        if (alphaId == null) {
            throw new IllegalArgumentException("baseline missing alpha_id");
        }
        Object xv = doc.get("x-version");
        if (xv != null && Json.asLong(xv) != SCHEMA_VERSION) {
            throw new IllegalArgumentException(
                    "unsupported baseline x-version " + xv
                    + " (pinned " + SCHEMA_VERSION + "); see MIGRATIONS.md");
        }
        String featureVersion = doc.get("feature_version") instanceof String fv
                ? fv : "";
        long count = 0;
        Object n = firstOf(doc, "n", "count");
        if (n != null) {
            count = Json.asLong(n);
        }
        Object eps = doc.get("psi_eps");
        if (eps != null && Json.asDouble(eps) != Psi.EPS) {
            throw new IllegalArgumentException(
                    "baseline psi_eps mismatch (pinned 1e-6)");
        }
        Object buckets = doc.get("n_buckets");
        if (buckets != null && Json.asLong(buckets) != Psi.BUCKETS) {
            throw new IllegalArgumentException(
                    "baseline n_buckets mismatch (pinned 10)");
        }
        String name = doc.get("name") instanceof String s ? s : "";
        Object edgesRaw = firstOf(doc, "edges", "bucket_edges", "quantiles");
        Object fracRaw = firstOf(doc, "expected_frac", "fractions",
                "bucket_fractions", "base_fractions");
        if (edgesRaw != null && fracRaw != null) {
            double[] edges = doubles(edgesRaw, "edges", Psi.BUCKETS - 1);
            for (int i = 1; i < edges.length; i++) {
                if (edges[i] < edges[i - 1]) {
                    throw new IllegalArgumentException(
                            "baseline edges must be non-decreasing");
                }
            }
            return new Baseline(alphaId, name, edges,
                    doubles(fracRaw, "fractions", Psi.BUCKETS), count,
                    featureVersion);
        }
        Object valuesRaw = doc.get("values");
        if (valuesRaw == null) {
            throw new IllegalArgumentException("baseline for " + alphaId
                    + " has neither edges+expected_frac nor values");
        }
        List<Object> arr = Json.array(valuesRaw);
        double[] values = new double[arr.size()];
        for (int i = 0; i < values.length; i++) {
            values[i] = Json.asDouble(arr.get(i));
        }
        double[] edges = Psi.edges(values);
        return new Baseline(alphaId, name, edges,
                Psi.fractions(values, edges),
                count == 0 ? values.length : count,
                featureVersion);
    }

    /** Load one baseline file. */
    public static Baseline load(Path file) {
        return parse(Json.object(Json.parseFile(file)));
    }

    /**
     * Reject a baseline captured against a different feature registry
     * (pinned, API_ADAPTIVE section 4). A baseline whose
     * {@code feature_version} differs from the engine's registry hash
     * describes a feature whose semantics may have changed under the same
     * name, so PSI or IC measured against it is meaningless. Fails closed:
     * a baseline that carries no {@code feature_version} at all is also
     * rejected, because it cannot be shown to match.
     *
     * @param b the loaded baseline
     * @param engineRegistryHash the running feature engine's registry hash
     * @throws IllegalArgumentException on any mismatch
     */
    public static void requireFeatureVersion(Baseline b,
            String engineRegistryHash) {
        if (engineRegistryHash == null || engineRegistryHash.isEmpty()) {
            throw new IllegalArgumentException(
                    "engine registry hash must be supplied to verify baseline "
                    + b.name());
        }
        if (!engineRegistryHash.equals(b.featureVersion())) {
            throw new IllegalArgumentException("baseline " + b.name()
                    + ": feature_version '" + b.featureVersion()
                    + "' does not match the engine's registry hash '"
                    + engineRegistryHash
                    + "' - captured against a different feature registry");
        }
    }

    /**
     * Load the SIGNAL-distribution baselines of a directory, keyed by
     * alpha id — the live drift monitor compares live signal values, so
     * {@code "kind": "feature"} and {@code "kind": "ic"} files are
     * skipped. Deterministic regardless of directory order: files are
     * visited sorted by filename, and when several signal baselines exist
     * for one alpha the pinned live-parity name
     * {@code signal_<alpha_id lowercase>} wins, else the lexicographically
     * first name. A missing directory yields an empty map — drift
     * monitoring simply stays disarmed until a baseline ships.
     */
    public static Map<String, Baseline> loadDir(Path dir) throws IOException {
        TreeMap<String, Baseline> out = new TreeMap<>();
        if (dir == null || !Files.isDirectory(dir)) {
            return out;
        }
        List<Path> files = new java.util.ArrayList<>();
        try (DirectoryStream<Path> stream =
                Files.newDirectoryStream(dir, "*.json")) {
            for (Path f : stream) {
                files.add(f);
            }
        }
        files.sort(java.util.Comparator.comparing(p ->
                p.getFileName().toString()));
        for (Path f : files) {
            Map<String, Object> doc = Json.object(Json.parseFile(f));
            if (!isDistribution(doc) || "feature".equals(doc.get("kind"))) {
                continue;
            }
            Baseline b = parse(doc);
            Baseline prev = out.get(b.alphaId());
            if (prev == null) {
                out.put(b.alphaId(), b);
                continue;
            }
            String pinned = "signal_"
                    + b.alphaId().toLowerCase(java.util.Locale.ROOT);
            if (!prev.name().equals(pinned) && b.name().equals(pinned)) {
                out.put(b.alphaId(), b);
            }
        }
        return out;
    }
}
