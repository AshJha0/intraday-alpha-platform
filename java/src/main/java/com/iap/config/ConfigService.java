package com.iap.config;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.codec.Sha256;
import com.iap.execution.InstrumentSpec;
import com.iap.execution.VenueSpec;

/**
 * Typed configuration service for {@code configs/*.json} (spec §26:
 * configuration is versioned, validated and audited).
 *
 * <p>Every load computes the file's SHA-256 and appends a structured audit
 * record; {@link #reload} re-reads a file and, when the hash changed,
 * appends a {@code config_changed} record carrying both hashes — so every
 * configuration change is traceable ("audit logs for strategy/risk
 * configuration changes"). Validation is strict: a missing file, malformed
 * JSON, or a document without the expected shape raises
 * {@link IllegalArgumentException}/{@link IllegalStateException}; callers
 * that must never run open (the risk engine) treat that as fail-closed.
 */
public final class ConfigService {
    /** One audit record ({@code config_loaded} / {@code config_changed}). */
    public record Audit(String action, String file, String sha256,
            String previousSha256, long bytes) {
        /** Sorted-key JSON line for the audit JSONL. */
        public String toJsonLine() {
            StringBuilder sb = new StringBuilder(160);
            sb.append("{\"action\":\"").append(action)
                    .append("\",\"bytes\":").append(bytes)
                    .append(",\"file\":\"").append(file)
                    .append("\",\"previous_sha256\":");
            if (previousSha256 == null) {
                sb.append("null");
            } else {
                sb.append('"').append(previousSha256).append('"');
            }
            sb.append(",\"sha256\":\"").append(sha256).append("\"}");
            return sb.toString();
        }
    }

    private final Path configsDir;
    private final Map<String, Map<String, Object>> docs = new LinkedHashMap<>();
    private final Map<String, String> hashes = new LinkedHashMap<>();
    private final List<Audit> audit = new ArrayList<>();

    /** Load and validate the pinned platform config files from a dir. */
    public ConfigService(Path configsDir) {
        this.configsDir = configsDir;
        for (String name : new String[] {"risk.json", "instruments.json",
                "venues.json", "execution.json", "strategies.json",
                "generator.json"}) {
            load(name);
        }
        validate();
    }

    private Map<String, Object> load(String name) {
        Path path = configsDir.resolve(name);
        byte[] bytes;
        try {
            bytes = Files.readAllBytes(path);
        } catch (IOException e) {
            throw new IllegalStateException("cannot read config " + path, e);
        }
        Object parsed = Json.parse(new String(bytes, StandardCharsets.UTF_8));
        if (!(parsed instanceof Map)) {
            throw new IllegalArgumentException(name + ": top level must be an object");
        }
        Map<String, Object> doc = Json.object(parsed);
        String hash = Sha256.hex(bytes);
        String prev = hashes.get(name);
        if (prev == null) {
            audit.add(new Audit("config_loaded", name, hash, null, bytes.length));
        } else if (!prev.equals(hash)) {
            audit.add(new Audit("config_changed", name, hash, prev, bytes.length));
        }
        docs.put(name, doc);
        hashes.put(name, hash);
        return doc;
    }

    private void validate() {
        for (Map.Entry<String, Map<String, Object>> e : docs.entrySet()) {
            Object v = e.getValue().get("x-version");
            if (!(v instanceof Long) || (Long) v < 1) {
                throw new IllegalArgumentException(
                        e.getKey() + ": missing/invalid x-version");
            }
        }
        // shape checks for the typed accessors (fail early, not on use)
        instruments();
        venues();
        if (!(doc("execution.json").get("defaults") instanceof Map)) {
            throw new IllegalArgumentException("execution.json: missing defaults");
        }
        if (!(doc("risk.json").get("global") instanceof Map)) {
            throw new IllegalArgumentException("risk.json: missing global section");
        }
    }

    /**
     * Re-read one config file; returns true when its content changed
     * (a {@code config_changed} audit record is appended).
     */
    public boolean reload(String name) {
        if (!docs.containsKey(name)) {
            throw new IllegalArgumentException("unknown config file " + name);
        }
        String before = hashes.get(name);
        load(name);
        return !hashes.get(name).equals(before);
    }

    /** Parsed document of one config file. */
    public Map<String, Object> doc(String name) {
        Map<String, Object> d = docs.get(name);
        if (d == null) {
            throw new IllegalArgumentException("unknown config file " + name);
        }
        return d;
    }

    /** SHA-256 (hex) of one loaded config file. */
    public String sha256(String name) {
        String h = hashes.get(name);
        if (h == null) {
            throw new IllegalArgumentException("unknown config file " + name);
        }
        return h;
    }

    /** The audit trail so far (loads + changes, in order). */
    public List<Audit> auditTrail() {
        return List.copyOf(audit);
    }

    /** Audit trail as JSONL (one sorted-key object per line). */
    public String auditJsonl() {
        StringBuilder sb = new StringBuilder(256);
        for (Audit a : audit) {
            sb.append(a.toJsonLine()).append('\n');
        }
        return sb.toString();
    }

    // ------------------------------------------------------ typed accessors

    /** The risk.json document (RiskLimits parses it strictly). */
    public Map<String, Object> riskDoc() {
        return doc("risk.json");
    }

    /** Instrument reference data keyed by instrument_id. */
    public TreeMap<Long, InstrumentSpec> instruments() {
        Object arr = doc("instruments.json").get("instruments");
        if (!(arr instanceof List)) {
            throw new IllegalArgumentException("instruments.json: missing instruments[]");
        }
        TreeMap<Long, InstrumentSpec> out = new TreeMap<>();
        for (Object o : Json.array(arr)) {
            Map<String, Object> ins = Json.object(o);
            for (String key : new String[] {"instrument_id", "tick_size",
                    "lot_size", "adv"}) {
                if (!ins.containsKey(key)) {
                    throw new IllegalArgumentException(
                            "instruments.json: instrument missing " + key);
                }
            }
            long iid = Json.asLong(ins.get("instrument_id"));
            double tick = Json.asDouble(ins.get("tick_size"));
            if (iid <= 0 || tick <= 0.0) {
                throw new IllegalArgumentException(
                        "instruments.json: bad instrument_id/tick_size for " + iid);
            }
            out.put(iid, new InstrumentSpec(iid, tick,
                    Json.asDouble(ins.get("lot_size")),
                    Json.asDouble(ins.get("adv"))));
        }
        if (out.isEmpty()) {
            throw new IllegalArgumentException("instruments.json: empty universe");
        }
        return out;
    }

    /** Per-instrument tick sizes (risk engine reference data). */
    public TreeMap<Long, Double> tickSizes() {
        TreeMap<Long, Double> out = new TreeMap<>();
        for (Map.Entry<Long, InstrumentSpec> e : instruments().entrySet()) {
            out.put(e.getKey(), e.getValue().tickSize());
        }
        return out;
    }

    /** Venue execution profiles keyed by venue_id. */
    public TreeMap<Integer, VenueSpec> venues() {
        return VenueSpec.loadVenues(configsDir.resolve("venues.json"));
    }

    /** execution.json defaults.seed (the pinned platform seed). */
    public long executionSeed() {
        return Json.asLong(Json.object(doc("execution.json").get("defaults")).get("seed"));
    }

    /** execution.json defaults.max_child_qty. */
    public long maxChildQty() {
        return Json.asLong(Json.object(doc("execution.json").get("defaults"))
                .get("max_child_qty"));
    }

    /** execution.json cost_model.impact_coeff_bps_per_pct_adv. */
    public double impactCoeffBpsPerPctAdv() {
        return Json.asDouble(Json.object(doc("execution.json").get("cost_model"))
                .get("impact_coeff_bps_per_pct_adv"));
    }

    /**
     * Monitoring HTTP port: {@code execution.json} optional
     * {@code monitoring.port}, default 8080 (grafana contract).
     */
    public int monitoringPort() {
        Object mon = doc("execution.json").get("monitoring");
        if (mon instanceof Map) {
            Object p = Json.object(mon).get("port");
            if (p instanceof Long) {
                return (int) Json.asLong(p);
            }
        }
        return 8080;
    }
}
