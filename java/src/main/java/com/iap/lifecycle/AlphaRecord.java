package com.iap.lifecycle;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * One alpha's lifecycle row ({@code iap.lifecycle.registry.AlphaRecord}):
 * state, when it entered it, the last transition and gate evaluation, the
 * versions of the research it was registered from, and the counters the
 * machine needs to resume exactly (demotion failures, live breach /
 * recovery counts). Mutated only by {@link AlphaLifecycle}.
 */
public final class AlphaRecord {
    private static final String[] KEYS = {"alpha_id", "state", "state_index",
        "since_ts", "last_transition", "last_evaluation", "experiment_id",
        "data_version", "feature_version", "model_version",
        "consecutive_failures", "breach_count", "recovery_count"};

    private final String alphaId;
    LifecycleState state;
    long sinceTs;
    LifecycleTransition lastTransition;
    GateEvaluation lastEvaluation;
    private final String experimentId;
    private final String dataVersion;
    private final String featureVersion;
    private final String modelVersion;
    int consecutiveFailures;
    int breachCount;
    int recoveryCount;

    /** Full constructor (loading); see {@link #fresh} for a new registration. */
    public AlphaRecord(String alphaId, LifecycleState state, long sinceTs,
            LifecycleTransition lastTransition, GateEvaluation lastEvaluation,
            String experimentId, String dataVersion, String featureVersion,
            String modelVersion, int consecutiveFailures, int breachCount,
            int recoveryCount) {
        this.alphaId = Trees.ident(alphaId, "AlphaRecord.alpha_id");
        if (state == null) {
            throw new IllegalArgumentException("AlphaRecord.state is null");
        }
        this.state = state;
        this.sinceTs = sinceTs;
        this.lastTransition = lastTransition;
        this.lastEvaluation = lastEvaluation;
        this.experimentId = experimentId == null ? null
                : Trees.ident(experimentId, "AlphaRecord.experiment_id");
        this.dataVersion = dataVersion == null ? null
                : Trees.sha256(dataVersion, "AlphaRecord.data_version");
        this.featureVersion = featureVersion == null ? null
                : Trees.sha256(featureVersion, "AlphaRecord.feature_version");
        this.modelVersion = modelVersion == null ? null
                : Trees.sha256(modelVersion, "AlphaRecord.model_version");
        if (consecutiveFailures < 0 || breachCount < 0 || recoveryCount < 0) {
            throw new IllegalArgumentException(
                    "AlphaRecord " + alphaId + ": counters must be >= 0");
        }
        this.consecutiveFailures = consecutiveFailures;
        this.breachCount = breachCount;
        this.recoveryCount = recoveryCount;
    }

    /** A freshly registered alpha: RESEARCH, no history, zero counters. */
    public static AlphaRecord fresh(String alphaId, long sinceTs, String experimentId,
            String dataVersion, String featureVersion, String modelVersion) {
        return new AlphaRecord(alphaId, LifecycleState.RESEARCH, sinceTs, null, null,
                experimentId, dataVersion, featureVersion, modelVersion, 0, 0, 0);
    }

    public String alphaId() {
        return alphaId;
    }

    public LifecycleState state() {
        return state;
    }

    public long sinceTs() {
        return sinceTs;
    }

    public LifecycleTransition lastTransition() {
        return lastTransition;
    }

    public GateEvaluation lastEvaluation() {
        return lastEvaluation;
    }

    public String experimentId() {
        return experimentId;
    }

    public String dataVersion() {
        return dataVersion;
    }

    public String featureVersion() {
        return featureVersion;
    }

    public String modelVersion() {
        return modelVersion;
    }

    public int consecutiveFailures() {
        return consecutiveFailures;
    }

    public int breachCount() {
        return breachCount;
    }

    public int recoveryCount() {
        return recoveryCount;
    }

    /** JSON-ready tree (registry record layout). */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("alpha_id", alphaId);
        t.put("state", state.name());
        t.put("state_index", (long) state.index());
        t.put("since_ts", sinceTs);
        t.put("last_transition", lastTransition == null ? null : lastTransition.toTree());
        t.put("last_evaluation", lastEvaluation == null ? null : lastEvaluation.toTree());
        t.put("experiment_id", experimentId);
        t.put("data_version", dataVersion);
        t.put("feature_version", featureVersion);
        t.put("model_version", modelVersion);
        t.put("consecutive_failures", (long) consecutiveFailures);
        t.put("breach_count", (long) breachCount);
        t.put("recovery_count", (long) recoveryCount);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static AlphaRecord fromTree(Map<String, Object> t) {
        String p = "AlphaRecord";
        Trees.checkKeys(t, KEYS, p);
        String alphaId = Trees.str(t, "alpha_id", p);
        LifecycleState state = LifecycleState.parse(Trees.str(t, "state", p),
                p + ".state");
        if (Trees.i64(t, "state_index", p) != state.index()) {
            throw new IllegalArgumentException("AlphaRecord " + alphaId
                    + ": state_index disagrees with state");
        }
        Object lt = t.get("last_transition");
        Object le = t.get("last_evaluation");
        return new AlphaRecord(alphaId, state, Trees.i64(t, "since_ts", p),
                lt == null ? null
                        : LifecycleTransition.fromTree(Trees.obj(lt, p + ".last_transition")),
                le == null ? null
                        : GateEvaluation.fromTree(Trees.obj(le, p + ".last_evaluation")),
                Trees.optStr(t, "experiment_id", p), Trees.optStr(t, "data_version", p),
                Trees.optStr(t, "feature_version", p), Trees.optStr(t, "model_version", p),
                (int) Trees.ranged(t, "consecutive_failures", p, 0, Integer.MAX_VALUE),
                (int) Trees.ranged(t, "breach_count", p, 0, Integer.MAX_VALUE),
                (int) Trees.ranged(t, "recovery_count", p, 0, Integer.MAX_VALUE));
    }
}
