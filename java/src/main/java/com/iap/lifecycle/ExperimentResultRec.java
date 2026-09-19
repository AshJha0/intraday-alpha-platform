package com.iap.lifecycle;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;

import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * {@code schemas/research/experiment_result.schema.json} (x-version 1) —
 * what an experiment produced, the {@code research} evidence block of the
 * lifecycle gates. All metrics are finite; {@code verdict} is PROMOTE /
 * ITERATE / REJECT and always REJECT when {@code leakagePassed} is false;
 * {@code createdTs} is event / ledger time (0 allowed), never wall clock.
 */
public record ExperimentResultRec(String experimentId, String alphaId,
        String datasetVersion, String featureVersion, String modelVersion,
        double ic, double rankIc, double tStat, long nwLags, double hitRate,
        double turnover, double grossReturnBps, double transactionCostBps,
        double netReturnBps, double maxDrawdownBps, double sharpe,
        double foldConsistency, long nFolds, boolean leakagePassed,
        Map<String, Object> leakageDetail, Boolean hypothesisSignConfirmed,
        String verdict, long nExperimentsInLedger, String gitCommit,
        long createdTs) {
    private static final String[] KEYS = {"experiment_id", "alpha_id",
        "dataset_version", "feature_version", "model_version", "ic", "rank_ic",
        "t_stat", "nw_lags", "hit_rate", "turnover", "gross_return_bps",
        "transaction_cost_bps", "net_return_bps", "max_drawdown_bps", "sharpe",
        "fold_consistency", "n_folds", "leakage_passed", "leakage_detail",
        "hypothesis_sign_confirmed", "verdict", "n_experiments_in_ledger",
        "git_commit", "created_ts"};

    public ExperimentResultRec {
        Trees.ident(experimentId, "ExperimentResult.experiment_id");
        Trees.ident(alphaId, "ExperimentResult.alpha_id");
        Trees.sha256(datasetVersion, "ExperimentResult.dataset_version");
        Trees.sha256(featureVersion, "ExperimentResult.feature_version");
        if (modelVersion != null) {
            Trees.sha256(modelVersion, "ExperimentResult.model_version");
        }
        for (double v : new double[] {ic, rankIc, tStat, hitRate, turnover,
                grossReturnBps, transactionCostBps, netReturnBps, maxDrawdownBps,
                sharpe, foldConsistency}) {
            Trees.finite(v, "ExperimentResult");
        }
        if (hitRate < 0.0 || hitRate > 1.0 || foldConsistency < 0.0
                || foldConsistency > 1.0) {
            throw new IllegalArgumentException(
                    "ExperimentResult: hit_rate/fold_consistency outside [0, 1]");
        }
        if (turnover < 0.0 || transactionCostBps < 0.0 || maxDrawdownBps < 0.0) {
            throw new IllegalArgumentException(
                    "ExperimentResult: turnover/cost/drawdown must be >= 0");
        }
        if (nwLags < 0 || nwLags > 0xFFFFFFFFL || nFolds < 0 || nFolds > 0xFFFFFFFFL) {
            throw new IllegalArgumentException("ExperimentResult: nw_lags/n_folds outside u32");
        }
        if (!("PROMOTE".equals(verdict) || "ITERATE".equals(verdict)
                || "REJECT".equals(verdict))) {
            throw new IllegalArgumentException("ExperimentResult.verdict: unknown " + verdict);
        }
        if (!leakagePassed && !"REJECT".equals(verdict)) {
            throw new IllegalArgumentException(
                    "ExperimentResult: leakage failure forces REJECT");
        }
        if (Math.abs((grossReturnBps - transactionCostBps) - netReturnBps) > 1e-9) {
            throw new IllegalArgumentException("ExperimentResult: net != gross - cost");
        }
        if (gitCommit == null || gitCommit.isEmpty()) {
            throw new IllegalArgumentException("ExperimentResult.git_commit is empty");
        }
        if (createdTs < 0) {
            throw new IllegalArgumentException("ExperimentResult.created_ts < 0");
        }
        leakageDetail = Collections.unmodifiableMap(new LinkedHashMap<>(leakageDetail));
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("experiment_id", experimentId);
        t.put("alpha_id", alphaId);
        t.put("dataset_version", datasetVersion);
        t.put("feature_version", featureVersion);
        t.put("model_version", modelVersion);
        t.put("ic", ic);
        t.put("rank_ic", rankIc);
        t.put("t_stat", tStat);
        t.put("nw_lags", nwLags);
        t.put("hit_rate", hitRate);
        t.put("turnover", turnover);
        t.put("gross_return_bps", grossReturnBps);
        t.put("transaction_cost_bps", transactionCostBps);
        t.put("net_return_bps", netReturnBps);
        t.put("max_drawdown_bps", maxDrawdownBps);
        t.put("sharpe", sharpe);
        t.put("fold_consistency", foldConsistency);
        t.put("n_folds", nFolds);
        t.put("leakage_passed", leakagePassed);
        TreeMap<String, Object> detail = new TreeMap<>(CanonicalJson.KEY_ORDER);
        detail.putAll(leakageDetail);
        t.put("leakage_detail", detail);
        t.put("hypothesis_sign_confirmed", hypothesisSignConfirmed);
        t.put("verdict", verdict);
        t.put("n_experiments_in_ledger", Trees.u64Tree(nExperimentsInLedger));
        t.put("git_commit", gitCommit);
        t.put("created_ts", createdTs);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static ExperimentResultRec fromTree(Map<String, Object> t) {
        String p = "ExperimentResult";
        Trees.checkKeys(t, KEYS, p);
        return new ExperimentResultRec(Trees.str(t, "experiment_id", p),
                Trees.str(t, "alpha_id", p), Trees.str(t, "dataset_version", p),
                Trees.str(t, "feature_version", p), Trees.optStr(t, "model_version", p),
                Trees.num(t, "ic", p), Trees.num(t, "rank_ic", p),
                Trees.num(t, "t_stat", p), Trees.u32(t, "nw_lags", p),
                Trees.num(t, "hit_rate", p), Trees.num(t, "turnover", p),
                Trees.num(t, "gross_return_bps", p),
                Trees.num(t, "transaction_cost_bps", p),
                Trees.num(t, "net_return_bps", p), Trees.num(t, "max_drawdown_bps", p),
                Trees.num(t, "sharpe", p), Trees.num(t, "fold_consistency", p),
                Trees.u32(t, "n_folds", p), Trees.bool(t, "leakage_passed", p),
                Trees.obj(t, "leakage_detail", p),
                Trees.optBool(t, "hypothesis_sign_confirmed", p),
                Trees.str(t, "verdict", p), Trees.u64(t, "n_experiments_in_ledger", p),
                Trees.str(t, "git_commit", p),
                Trees.ranged(t, "created_ts", p, 0, Long.MAX_VALUE));
    }
}
