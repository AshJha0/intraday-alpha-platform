package com.iap.trace;

import java.util.Map;

import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * {@code schemas/trace/decision_trace.schema.json} (x-version 1): the
 * auditable chain for one decision ("why did we trade?").
 * {@code traceId = CanonicalJson.makeTraceId(sessionId, instrumentId,
 * eventTs, sequence)}; the four version hashes pin the data, feature
 * registry, model and configuration the decision was made under. One
 * canonical line ({@link #toLine}) is what {@link JsonlTraceSink} writes
 * and {@link TraceDigest} hashes.
 */
public record DecisionTrace(String traceId, String sessionId, long instrumentId,
        long eventTs, long sequence, String dataVersion, String featureVersion,
        String modelVersion, String configVersion, TraceStages stages) {
    private static final String[] KEYS = {"trace_id", "session_id",
        "instrument_id", "event_ts", "sequence", "data_version",
        "feature_version", "model_version", "config_version", "stages"};

    public DecisionTrace {
        if (traceId == null || traceId.length() != 32
                || !Trees.isSha256Hex(traceId + traceId)) {
            throw new IllegalArgumentException(
                    "DecisionTrace.trace_id must be 32 lowercase hex chars");
        }
        Trees.ident(sessionId, "DecisionTrace.session_id");
        if (instrumentId < 0 || instrumentId > 0xFFFFFFFFL) {
            throw new IllegalArgumentException("DecisionTrace.instrument_id outside u32");
        }
        Trees.sha256(dataVersion, "DecisionTrace.data_version");
        Trees.sha256(featureVersion, "DecisionTrace.feature_version");
        Trees.sha256(modelVersion, "DecisionTrace.model_version");
        Trees.sha256(configVersion, "DecisionTrace.config_version");
        if (stages == null) {
            throw new IllegalArgumentException("DecisionTrace.stages is null");
        }
    }

    /** Build with the pinned trace id derived from the four key fields. */
    public static DecisionTrace of(String sessionId, long instrumentId, long eventTs,
            long sequence, String dataVersion, String featureVersion,
            String modelVersion, String configVersion, TraceStages stages) {
        return new DecisionTrace(
                CanonicalJson.makeTraceId(sessionId, instrumentId, eventTs, sequence),
                sessionId, instrumentId, eventTs, sequence, dataVersion,
                featureVersion, modelVersion, configVersion, stages);
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("trace_id", traceId);
        t.put("session_id", sessionId);
        t.put("instrument_id", instrumentId);
        t.put("event_ts", eventTs);
        t.put("sequence", Trees.u64Tree(sequence));
        t.put("data_version", dataVersion);
        t.put("feature_version", featureVersion);
        t.put("model_version", modelVersion);
        t.put("config_version", configVersion);
        t.put("stages", stages.toTree());
        return t;
    }

    /** The canonical JSONL line of this trace (no newline). */
    public String toLine() {
        return CanonicalJson.serialize(toTree());
    }

    /** Strict inverse of {@link #toTree}. */
    public static DecisionTrace fromTree(Map<String, Object> t) {
        String p = "DecisionTrace";
        Trees.checkKeys(t, KEYS, p);
        return new DecisionTrace(Trees.str(t, "trace_id", p),
                Trees.str(t, "session_id", p), Trees.u32(t, "instrument_id", p),
                Trees.i64(t, "event_ts", p), Trees.u64(t, "sequence", p),
                Trees.str(t, "data_version", p), Trees.str(t, "feature_version", p),
                Trees.str(t, "model_version", p), Trees.str(t, "config_version", p),
                TraceStages.fromTree(Trees.obj(t, "stages", p)));
    }

    /** Parse one canonical JSONL line. */
    public static DecisionTrace fromLine(String line) {
        return fromTree(Trees.obj(com.iap.config.Json.parse(line, true),
                "DecisionTrace"));
    }
}
