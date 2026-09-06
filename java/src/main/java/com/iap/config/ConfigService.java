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

    /** Environment variable naming the configuration directory. */
    public static final String CONFIG_DIR_ENV = "IAP_CONFIG_DIR";

    /**
     * The pinned config-directory resolution order
     * (PLATFORM_CONVENTIONS.md §12.2): an explicit {@code --configs} value,
     * else {@code $IAP_CONFIG_DIR}, else the caller's default. A blank
     * environment value is treated as unset; a value that is not a readable
     * directory is a configuration error (fail fast — never silently fall
     * back to the baked-in configs an operator meant to override).
     */
    public static Path resolveDir(Path explicit, Map<String, String> env,
            Path fallback) {
        if (explicit != null) {
            return explicit;
        }
        String v = env.get(CONFIG_DIR_ENV);
        if (v == null || v.isBlank()) {
            return fallback;
        }
        Path p = Path.of(v.trim());
        if (!Files.isDirectory(p)) {
            throw new IllegalArgumentException(CONFIG_DIR_ENV + "=" + v
                    + " is not a readable directory");
        }
        return p;
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
        executionLimits();
        sorOptions();
        com.iap.risk.RiskLimits.fromJson(riskDoc());
        adaptive();
    }

    /**
     * The {@code strategies.json} {@code adaptive} block, validated
     * (PLATFORM_CONVENTIONS.md §12.2). Every key must be present and
     * positive; a missing block or key is an {@link IllegalArgumentException}
     * naming file and key, never an NPE/ClassCastException at first use.
     */
    public Map<String, Object> adaptive() {
        Object block = doc("strategies.json").get("adaptive");
        if (!(block instanceof Map)) {
            throw new IllegalArgumentException(
                    "strategies.json: missing/non-object adaptive block");
        }
        Map<String, Object> a = Json.object(block);
        for (String key : new String[] {"block_ns", "ic_window_ns",
                "ic_bucket_ns", "min_ic_buckets"}) {
            Object v = a.get(key);
            if (!(v instanceof Long)) {
                throw new IllegalArgumentException(
                        "strategies.json: missing/non-integer adaptive." + key);
            }
            if ((Long) v <= 0) {
                throw new IllegalArgumentException(
                        "strategies.json: adaptive." + key + " must be > 0, got "
                                + v);
            }
        }
        if (!(a.get("lifecycle") instanceof Map)) {
            throw new IllegalArgumentException(
                    "strategies.json: missing/non-object adaptive.lifecycle");
        }
        return a;
    }

    /** Parsed, validated hard-risk limits (`configs/risk.json`). */
    public com.iap.risk.RiskLimits riskLimits() {
        return com.iap.risk.RiskLimits.fromJson(riskDoc());
    }

    /**
     * SHA-256 over the loaded config hashes: the hex digest of
     * {@code "<file>=<sha256>\n"} lines sorted by file name. One value that
     * identifies the whole configuration a session ran with (the session
     * report's {@code config_sha256}, PLATFORM_CONVENTIONS.md §12.2).
     */
    public String configSha256() {
        StringBuilder sb = new StringBuilder(512);
        for (String name : new TreeMap<>(hashes).keySet()) {
            sb.append(name).append('=').append(hashes.get(name)).append('\n');
        }
        return Sha256.hex(sb.toString().getBytes(StandardCharsets.UTF_8));
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

    /**
     * Instrument reference data keyed by instrument_id. {@code qtyUnit} is
     * the FX {@code lot_size} (1 qty unit = 1,000 base ccy) and 1 for
     * EQUITY/ETF (qty already in shares); {@code quoteCcy} is the FX
     * {@code quote_currency} or the equity {@code currency}. Missing or
     * invalid fields fail closed (throw).
     */
    public TreeMap<Long, InstrumentSpec> instruments() {
        Object arr = doc("instruments.json").get("instruments");
        if (!(arr instanceof List)) {
            throw new IllegalArgumentException("instruments.json: missing instruments[]");
        }
        TreeMap<Long, InstrumentSpec> out = new TreeMap<>();
        for (Object o : Json.array(arr)) {
            Map<String, Object> ins = Json.object(o);
            for (String key : new String[] {"instrument_id", "tick_size",
                    "lot_size", "adv", "asset_class"}) {
                if (!ins.containsKey(key)) {
                    throw new IllegalArgumentException(
                            "instruments.json: instrument missing " + key);
                }
            }
            long iid = Json.asLong(ins.get("instrument_id"));
            double tick = Json.asDouble(ins.get("tick_size"));
            if (iid <= 0 || !(tick > 0.0)) {
                throw new IllegalArgumentException(
                        "instruments.json: bad instrument_id/tick_size for " + iid);
            }
            String assetClass = String.valueOf(ins.get("asset_class"));
            double lot = Json.asDouble(ins.get("lot_size"));
            double adv = Json.asDouble(ins.get("adv"));
            // The money unit (PLATFORM_CONVENTIONS.md §12.1) is derived from
            // lot_size; a non-positive lot_size or adv is a reference-data
            // error, not a value to normalise away.
            if (!(lot > 0.0)) {
                throw new IllegalArgumentException(
                        "instruments.json: lot_size must be > 0 for instrument "
                                + iid + ", got " + lot);
            }
            if (!(adv > 0.0)) {
                throw new IllegalArgumentException(
                        "instruments.json: adv must be > 0 for instrument "
                                + iid + ", got " + adv);
            }
            double unit;
            Object ccy;
            switch (assetClass) {
                case "FX" -> {
                    unit = lot;
                    ccy = ins.get("quote_currency");
                }
                case "EQUITY", "ETF" -> {
                    unit = 1.0;
                    ccy = ins.get("currency");
                }
                default -> throw new IllegalArgumentException(
                        "instruments.json: unknown asset_class " + assetClass
                                + " for " + iid);
            }
            if (!(ccy instanceof String) || ((String) ccy).isEmpty()) {
                throw new IllegalArgumentException(
                        "instruments.json: missing currency for " + iid);
            }
            out.put(iid, new InstrumentSpec(iid, tick, unit, adv, (String) ccy));
        }
        if (out.isEmpty()) {
            throw new IllegalArgumentException("instruments.json: empty universe");
        }
        return out;
    }

    /** Risk-engine reference data (tick, qty unit, quote ccy) per instrument. */
    public TreeMap<Long, com.iap.risk.InstrumentRef> riskInstruments() {
        TreeMap<Long, com.iap.risk.InstrumentRef> out = new TreeMap<>();
        for (Map.Entry<Long, InstrumentSpec> e : instruments().entrySet()) {
            out.put(e.getKey(), e.getValue().riskRef());
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

    private Map<String, Object> executionDefaults() {
        return Json.object(doc("execution.json").get("defaults"));
    }

    private static double needNum(Map<String, Object> m, String key, String where) {
        Object v = m.get(key);
        if (!(v instanceof Long) && !(v instanceof Double)) {
            throw new IllegalArgumentException(
                    "execution.json: missing/non-numeric " + where + "." + key);
        }
        return Json.asDouble(v);
    }

    /**
     * execution.json defaults.{max_participation, min_slice_interval_ns,
     * latency_budget_ns} — the enforced execution controls (strict).
     */
    public com.iap.backtest.BacktestEngine.ExecutionLimits executionLimits() {
        Map<String, Object> d = executionDefaults();
        return new com.iap.backtest.BacktestEngine.ExecutionLimits(
                needNum(d, "max_participation", "defaults"),
                (long) needNum(d, "min_slice_interval_ns", "defaults"),
                (long) needNum(d, "latency_budget_ns", "defaults"));
    }

    /** execution.json sor.{prefer_rebate, max_venue_latency_ns} (strict). */
    public com.iap.sor.SorOptions sorOptions() {
        Object sor = doc("execution.json").get("sor");
        if (!(sor instanceof Map)) {
            throw new IllegalArgumentException("execution.json: missing sor block");
        }
        Map<String, Object> s = Json.object(sor);
        Object pr = s.get("prefer_rebate");
        if (!(pr instanceof Boolean)) {
            throw new IllegalArgumentException(
                    "execution.json: missing/non-bool sor.prefer_rebate");
        }
        return new com.iap.sor.SorOptions((Boolean) pr,
                (long) needNum(s, "max_venue_latency_ns", "sor"));
    }

    /** Risk-engine FX conversion table from configs/risk.json. */
    public TreeMap<String, com.iap.risk.RiskLimits.FxConversion> fxConversion() {
        return com.iap.risk.RiskLimits.fromJson(riskDoc()).fxConversion();
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
