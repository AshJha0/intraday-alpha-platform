package com.iap.lifecycle;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;
import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * The set of {@link AlphaRecord}s keyed by alpha id, persisted to
 * {@code research/alpha_registry.json} ({@code x-version} 1) with a
 * byte-deterministic layout: Python's {@code json.dumps(sort_keys=True,
 * indent=2, ensure_ascii=True)} plus one trailing newline — identical state
 * ⇒ identical bytes, and a load-then-save of the Python-written file is
 * byte-identical. Loading is strict: {@code x-version}, key = record id,
 * {@code state_index} = state, gate order / failed gates consistent.
 */
public final class AlphaRegistry {
    /** {@code x-version} of {@code research/alpha_registry.json}. */
    public static final long REGISTRY_VERSION = 1;

    /** The pinned description line of the registry document. */
    public static final String DESCRIPTION =
            "Alpha promotion lifecycle registry (iap.lifecycle). One record per alpha: "
            + "current LifecycleState, the event time it was entered, the last transition "
            + "and gate evaluation, the research versions it was registered from and the "
            + "counters the machine resumes from. Deterministic and wall-clock free: an "
            + "identical rerun of `python -m iap.lifecycle bootstrap` reproduces the bytes.";

    private final String policy;
    private final String description;
    private final TreeMap<String, AlphaRecord> records =
            new TreeMap<>(CanonicalJson.KEY_ORDER);

    /** An empty registry for {@code policy}. */
    public AlphaRegistry(String policy) {
        this(policy, DESCRIPTION);
    }

    private AlphaRegistry(String policy, String description) {
        if (policy == null || policy.isEmpty()) {
            throw new IllegalArgumentException("AlphaRegistry: policy name must not be empty");
        }
        this.policy = policy;
        this.description = description;
    }

    /** The policy name every record was evaluated under. */
    public String policy() {
        return policy;
    }

    public boolean contains(String alphaId) {
        return records.containsKey(alphaId);
    }

    public int size() {
        return records.size();
    }

    /** Sorted alpha ids (the only iteration order the registry offers). */
    public List<String> alphaIds() {
        return new ArrayList<>(records.keySet());
    }

    /** The record of {@code alphaId}; unknown ids are an {@link IllegalArgumentException}. */
    public AlphaRecord get(String alphaId) {
        AlphaRecord r = records.get(alphaId);
        if (r == null) {
            throw new IllegalArgumentException("unknown alpha '" + alphaId + "'");
        }
        return r;
    }

    /** Register a record; a duplicate id is an {@link IllegalArgumentException}. */
    public AlphaRecord add(AlphaRecord record) {
        if (records.containsKey(record.alphaId())) {
            throw new IllegalArgumentException("alpha '" + record.alphaId()
                    + "' is already registered");
        }
        records.put(record.alphaId(), record);
        return record;
    }

    /** Records in sorted alpha-id order. */
    public List<AlphaRecord> records() {
        return new ArrayList<>(records.values());
    }

    /** JSON-ready tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("x-version", REGISTRY_VERSION);
        t.put("description", description);
        t.put("policy", policy);
        TreeMap<String, Object> alphas = new TreeMap<>(CanonicalJson.KEY_ORDER);
        for (Map.Entry<String, AlphaRecord> e : records.entrySet()) {
            alphas.put(e.getKey(), e.getValue().toTree());
        }
        t.put("alphas", alphas);
        return t;
    }

    /** The exact file text: sorted keys, 2-space indent, ASCII, trailing newline. */
    public String render() {
        return CanonicalJson.indented(toTree()) + "\n";
    }

    /** Write {@link #render} to {@code path} (creating parent directories). */
    public void save(Path path) {
        try {
            if (path.getParent() != null) {
                Files.createDirectories(path.getParent());
            }
            Files.write(path, render().getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new IllegalStateException("cannot write alpha registry " + path, e);
        }
    }

    /** Strict inverse of {@link #toTree}. */
    public static AlphaRegistry fromTree(Map<String, Object> doc) {
        String p = "alpha registry";
        Trees.checkKeys(doc, new String[] {"x-version", "description", "policy",
            "alphas"}, p);
        long version = Trees.i64(doc, "x-version", p);
        if (version != REGISTRY_VERSION) {
            throw new IllegalArgumentException(p + ": x-version " + version + " != "
                    + REGISTRY_VERSION);
        }
        AlphaRegistry registry = new AlphaRegistry(Trees.str(doc, "policy", p),
                Trees.str(doc, "description", p));
        Map<String, Object> alphas = Trees.obj(doc, "alphas", p);
        for (String alphaId : new TreeMap<String, Object>(alphas).keySet()) {
            AlphaRecord record = AlphaRecord.fromTree(
                    Trees.obj(alphas, alphaId, p + ".alphas"));
            if (!record.alphaId().equals(alphaId)) {
                throw new IllegalArgumentException(p + ": key '" + alphaId
                        + "' != record '" + record.alphaId() + "'");
            }
            registry.add(record);
        }
        return registry;
    }

    /** Load {@code research/alpha_registry.json} (strict). */
    public static AlphaRegistry load(Path path) {
        Object parsed;
        try {
            parsed = Json.parse(new String(Files.readAllBytes(path),
                    StandardCharsets.UTF_8), true);
        } catch (IOException e) {
            throw new IllegalStateException("cannot read alpha registry " + path, e);
        }
        return fromTree(Trees.obj(parsed, path.toString()));
    }
}
