"""Full ML pipeline on the bundled data -> ML_REPORT.md (spec §14).

Runs, in order:

1. dataset load (5s cost-adjusted target, curated predictor set);
2. gated walk-forward model comparison (linear baselines always; trees +
   MLP only behind the positive-linear-OOS-IC gate), every fit tracked as a
   run under research/models/;
3. meta-labeling (trade/no-trade, probability-calibrated — isotonic when the
   calibration segment is thick enough, Platt/sigmoid otherwise) on the best
   primary model's pooled OOS predictions, with economic gate-on/gate-off
   evaluation;
4. writes ML_REPORT.md + calibration_curve.json next to this script.

Usage: ``python3 research/ml_reports/run_ml.py`` (from the repo root or
anywhere — paths are resolved relative to this file).  Budget: < 5 minutes.
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO / "python" / "src"))

from iap.experiment.tracker import ExperimentTracker  # noqa: E402
from iap.models.dataset import (  # noqa: E402
    FEATURE_SET,
    TARGET_COLUMN,
    load_dataset,
)
from iap.models.metalabel import (  # noqa: E402
    MIN_ISOTONIC_POSITIVES,
    run_meta_labeling,
)
from iap.models.pipeline import run_model_comparison  # noqa: E402
from iap.models.zoo import TREE_FALLBACK_ACTIVE, model_names  # noqa: E402


def _fmt(x: float, nd: int = 4) -> str:
    return f"{x:.{nd}f}"


def main() -> None:
    warnings.filterwarnings("ignore")
    t0 = time.time()
    tracker = ExperimentTracker()

    print("loading dataset ...")
    ds = load_dataset()
    print(f"  {len(ds)} rows, {len(ds.feature_names)} features")

    print("running gated model comparison ...")
    results = run_model_comparison(ds, tracker=tracker)
    gate = results["gate"]

    trained = [n for n in results["models"]]
    best = max(trained, key=lambda n: results["models"][n]["mean_ic"])
    best_pred = results["models"][best]["_pooled_pred"]

    print(f"running meta-labeling on primary model {best!r} ...")
    meta = run_meta_labeling(ds, best_pred, tracker=tracker,
                             primary_name=best)

    runtime = time.time() - t0
    n_experiments = tracker.experiment_count

    # ---------------- calibration plot data (JSON)
    with open(_HERE / "calibration_curve.json", "w") as f:
        json.dump({
            "primary_model": best,
            "curve": meta["calibration_curve"],
            "auc_test": meta["auc_test"],
            "brier_test": meta["brier_test"],
            "base_rate_test": meta["base_rate_test"],
        }, f, indent=2, sort_keys=True)
        f.write("\n")

    # ---------------- report
    L = []
    L.append("# ML Report — Gated Model Comparison + Meta-Labeling")
    L.append("")
    L.append(f"Dataset: {len(ds)} valid rows across 19 instruments "
             f"(2 synthetic days), target `{TARGET_COLUMN}` (5s "
             "cost-adjusted forward return), "
             f"{len(FEATURE_SET)} curated predictors (all 10 registry "
             "families represented). Walk-forward: 4 expanding folds whose "
             "boundaries are quantiles of the ROW INDEX (not of the wall "
             "span — this data occupies ~2.6 h of each 24 h day, so equal "
             "wall segments produced wildly unequal folds), 60s embargo, 5s "
             "label-horizon purge at every train boundary.")
    L.append("")
    L.append(f"Model fits recorded in the MODEL ledger "
             f"(`research/models/ledger.json`) so far: **{n_experiments}** "
             "— every tracked fit, across all rounds, each with a manifest "
             "under `research/models/run_NNNN_*/`. This is a different "
             "counter from the alpha multiple-testing ledger "
             "(`research/experiments.json`), which is de-duplicated by "
             "(alpha, kind, config) and carries the Bonferroni / expected-"
             "max-|t| yardstick quoted in `research/alpha_reports/"
             "REPORT.md`; neither number is a substitute for the other. "
             "Multiple-testing note: with this many looks at one dataset, "
             "isolated significance is meaningless — decisions below rest "
             "on signs and stability, not on any single t-stat.")
    L.append("")

    L.append("## Fold composition")
    L.append("")
    L.append("| model | folds | degenerate | fold | n_train | n_test | "
             "train classes | test classes |")
    L.append("|-------|-------|------------|------|---------|--------|"
             "---------------|--------------|")
    for name in sorted(results["models"]):
        m = results["models"][name]
        for f in m["per_fold"]:
            L.append(
                f"| {name} | {m['n_folds']} | {m['n_degenerate_folds']} | "
                f"{f['fold']} | {f['n_train']} | {f['n_test']} | "
                f"{','.join(f['train_asset_classes'])} | "
                f"{','.join(f['test_asset_classes'])} |")
        if m["asset_mix_warning"]:
            L.append(f"| {name} | **asset-mix warning**: folds "
                     f"{m['asset_mix_warning']} test on asset classes absent "
                     "from their train set | | | | | | |")
    L.append("")

    L.append("## The gate")
    L.append("")
    L.append(f"- Rule: {gate['rule']}.")
    L.append(f"- Best linear baseline: `{gate['best_linear_model']}` with "
             f"pooled OOS IC vs the MID-TO-MID label "
             f"{_fmt(gate.get('best_linear_pooled_ic_vs_mid'))} → gate "
             f"**{'PASSED' if gate['passed'] else 'FAILED'}** "
             f"(mean fold IC on the cost-adjusted target: "
             f"{_fmt(gate['best_linear_mean_oos_ic'])} — that target embeds "
             "the observable half-spread, so gating on it passed trivially "
             "and it is no longer the gate).")
    if gate["skipped_models"]:
        L.append(f"- Skipped models: {gate['skipped_models']}.")
    tree_note = ("sklearn HistGradientBoosting FALLBACK"
                 if TREE_FALLBACK_ACTIVE else "xgboost + lightgbm (native)")
    L.append(f"- Tree libraries: {tree_note}.")
    L.append("")

    L.append("## Baselines vs trees vs MLP (out-of-sample)")
    L.append("")
    L.append("| model | tier | mean IC | mean RankIC | IC t-stat | "
             "IC vs mid label | net bps/signal (conservative) | "
             "net bps/signal (label-exact) | trades |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    tier_of = {n: t for t in (0, 1, 2) for n in model_names(t)}
    for name in trained:
        m = results["models"][name]
        e, ex = m["economics"], m["economics_label_exact"]
        L.append(
            f"| {name} | {tier_of.get(name, '?')} | {_fmt(m['mean_ic'])} | "
            f"{_fmt(m['mean_rank_ic'])} | {_fmt(m['ic_tstat'], 2)} | "
            f"{_fmt(m['pooled_ic_vs_mid'])} | "
            f"{_fmt(e['mean_net_bps_per_signal'], 3)} | "
            f"{_fmt(ex['mean_net_bps_per_signal'], 3)} | {e['n_trades']} |")
    L.append("")
    L.append(f"Winner by mean OOS IC on the pinned target: **{best}**.")
    L.append("")

    L.append("### Honest read of these numbers")
    L.append("")
    # data-driven artifact diagnostics (spread column of the meta context)
    import numpy as np
    spread = ds.meta_context[:, 0]
    fin = np.isfinite(spread)
    crossed_frac = float((spread[fin] < 0.0).mean()) if fin.any() else 0.0
    is_eq = ds.instrument_id < 100  # equity ids < 100, FX pairs >= 101
    def _cf(mask):
        m = fin & mask
        return float((spread[m] < 0.0).mean()) if m.any() else 0.0
    crossed_eq, crossed_fx = _cf(is_eq), _cf(~is_eq)
    ok = fin & np.isfinite(ds.y)
    corr_spread_target = (
        float(np.corrcoef(spread[ok], ds.y[ok])[0, 1])
        if ok.sum() > 2 and spread[ok].std() > 0 and ds.y[ok].std() > 0
        else float("nan"))
    best_m = results["models"][best]
    art_bps = (best_m["economics_label_exact"]["mean_net_bps_per_signal"]
               - best_m["economics"]["mean_net_bps_per_signal"])
    L.append(
        "- The 5s cost-adjusted target embeds the round-trip spread, so "
        "part of every model's IC comes from predicting the observable "
        "spread component rather than direction. On this dataset the "
        "consolidated book at decision rows is crossed (negative spread) "
        f"{crossed_frac:.2%} of the time — {crossed_eq:.2%} on equities "
        "(the shared-efficient-price generator keeps venues coherent; an "
        "earlier generator left equities crossed ~95% of the time and "
        f"dominated this report) and {crossed_fx:.2%} on FX, where "
        "aggregated LP quotes go stale between venue updates (a real "
        "phenomenon of FX aggregation, amplified here by the synthetic "
        "update cadence) — and "
        f"corr(spread, target) = {_fmt(corr_spread_target, 3)}. This is a "
        "property of the target construction, not leakage: the automatic "
        "shift-by-one leakage test (see test suite) destroys directional "
        "IC as required.")
    L.append(
        "- The honest directional measure is **IC vs the mid-to-mid "
        "label** (table above): read that column, not the headline IC, "
        "for any claim about exploitable 5s directional signal on this "
        "bundled 2-day synthetic dataset.")
    L.append(
        "- Conservative economics floor realized round-trip costs at zero "
        "— you are never paid to cross a (rare) crossed synthetic book. "
        "The gap between label-exact and conservative bps/signal for the "
        f"winning model is {_fmt(art_bps, 3)} bps/signal of residual "
        "book-artifact; only the conservative column should inform any "
        "decision.")
    L.append(
        "- Model ordering on the pinned target reflects how well each fits "
        "the spread component plus whatever direction exists; with weak "
        "true signal underneath, the ordering says little about production "
        "alpha.")
    L.append("")

    # Which calibrator actually ran (isotonic vs the Platt/sigmoid fallback)
    # is a property of THIS run, not a constant: it depends on how many
    # positives the calibration segment carried.  Render it, never assert it.
    cal_method = meta.get("calibration_method") or "unknown"
    cal_pos = meta.get("calibration_positives", 0)
    cal_long = {
        "isotonic": "isotonic regression (non-parametric, monotone step map)",
        "sigmoid": "Platt scaling (a 2-parameter sigmoid)",
    }.get(cal_method, cal_method)
    cal_why = (
        f"{cal_pos} positive samples in the calibration segment "
        f">= the pinned isotonic minimum of {MIN_ISOTONIC_POSITIVES}"
        if cal_method == "isotonic" else
        f"only {cal_pos} positive samples in the calibration segment, below "
        f"the pinned isotonic minimum of {MIN_ISOTONIC_POSITIVES}, so the "
        "isotonic path was NOT taken")
    degenerate = bool(meta.get("gate_degenerate"))

    L.append("## Meta-labeling (trade/no-trade gate)")
    L.append("")
    L.append(
        f"Primary: `{best}` pooled OOS predictions; meta features: alpha "
        "strength/sign, spread, 1m vol, L1 depth, direction-aligned queue "
        "imbalance, half-spread cost, expected impact. Chronological "
        "50/25/25 train/calibration/test split with 60s embargo; probability "
        f"calibration on the calibration segment by **{cal_long}** "
        f"(`calibration_method: {cal_method}` — {cal_why}). Economic "
        "meta-label: realized net P&L > 0 under the conservative cost model.")
    L.append("")
    L.append(f"- Meta samples (primary would trade): "
             f"{meta['n_meta_samples']}; test base rate of profitable "
             f"signals: {_fmt(meta['base_rate_test'], 3)}.")
    L.append(f"- Test AUC {_fmt(meta['auc_test'], 3)}, Brier "
             f"{_fmt(meta['brier_test'], 4)} (calibration curve data: "
             "`calibration_curve.json`).")
    L.append(f"- `calibration_method`: **{cal_method}** "
             f"({cal_pos} calibration positives); `gate_degenerate`: "
             f"**{str(degenerate).lower()}**.")
    L.append("")
    L.append("| evaluation (test segment) | trades | total net bps | "
             "mean net bps/trade | hit rate |")
    L.append("|---|---|---|---|---|")
    for label, key in (("gate OFF", "economics_gate_off"),
                       (f"gate ON @ tau={meta['tau']}",
                        "economics_gate_tau"),
                       (f"gate ON @ best tau="
                        f"{_fmt(meta['best_tau_from_calibration'], 3)} "
                        "(chosen on calibration)",
                        "economics_gate_best_tau")):
        e = meta[key]
        L.append(f"| {label} | {e['n_trades']} | "
                 f"{_fmt(e['total_net_bps'], 1)} | "
                 f"{_fmt(e['mean_net_bps_per_trade'], 4)} | "
                 f"{_fmt(e['hit_rate'], 3)} |")
    L.append("")
    off = meta["economics_gate_off"]
    on = meta["economics_gate_best_tau"]
    if on["n_trades"] > 0 and off["n_trades"] > 0:
        L.append(
            f"Meta-gate effect at the calibration-chosen threshold: "
            f"{on['n_trades']}/{off['n_trades']} signals kept, mean net "
            f"per trade {_fmt(off['mean_net_bps_per_trade'], 4)} → "
            f"{_fmt(on['mean_net_bps_per_trade'], 4)} bps, total "
            f"{_fmt(off['total_net_bps'], 1)} → "
            f"{_fmt(on['total_net_bps'], 1)} bps.")
    else:
        L.append(
            "Meta-gate effect: the calibrated gate declined **every** test "
            f"signal at both thresholds ({off['n_trades']} signals, 0 "
            "trades). That is flagged `gate_degenerate: true` and is NOT "
            "reported here as an economic decision: a gate that never fires "
            "produces no evidence either way about whether abstaining pays. "
            "The gate-off row shows what the ungated primary would have "
            "done.")
    L.append("")

    L.append("## Conclusions")
    L.append("")
    # Conclusion 1 is DERIVED from the gate record above, never asserted:
    # a hard-coded round-2 sentence here once claimed the advanced models
    # ran while the gate section of the same file said they were skipped.
    ran_advanced = sorted(set(trained) - set(model_names(0)))
    if gate["passed"]:
        L.append(
            "1. The gate mechanism works and is exercised for real: the best "
            f"linear baseline (`{gate['best_linear_model']}`) reached a "
            "POSITIVE pooled OOS IC vs the mid-to-mid label "
            f"({_fmt(gate['best_linear_pooled_ic_vs_mid'])}), so the gate "
            "**PASSED** and the advanced models "
            f"({', '.join(ran_advanced) or 'none configured'}) ran on the "
            "same folds. Their numbers are in the table above; the gate did "
            "not have to block anything on this dataset.")
    else:
        L.append(
            "1. The gate mechanism works and is exercised for real: the best "
            f"linear baseline (`{gate['best_linear_model']}`) reached a "
            "NEGATIVE pooled OOS IC vs the mid-to-mid label "
            f"({_fmt(gate['best_linear_pooled_ic_vs_mid'])}), so the gate "
            "**FAILED** and "
            f"{', '.join(gate['skipped_models']) or 'no advanced model'} "
            "were **never fitted** on this dataset. Nothing above tier 0 was "
            "trained: any reading of tree or MLP behaviour here would be a "
            "reading of models that do not exist. Gating on the "
            "cost-adjusted target "
            "instead (mean fold IC "
            f"{_fmt(gate['best_linear_mean_oos_ic'])}) would have passed "
            "trivially, because that target embeds the observable "
            "half-spread — which is why it is no longer the gate.")
    L.append(
        "2. No 5s directional alpha exists in this bundled synthetic "
        "sample. Apparent IC is spread-component prediction; conservative "
        "economics are ~flat. Nothing here should be promoted.")
    if degenerate:
        L.append(
            "3. The meta-labeling machinery ran end to end, but its gate is "
            f"**degenerate on this dataset** (`gate_degenerate: true`): "
            f"calibrated by {cal_long} on {cal_pos} positives, every test "
            "probability fell below both thresholds, so the gate took zero "
            "trades. A gate that never fires demonstrates neither skill nor "
            "the value of abstaining — it only shows the calibrated "
            f"probabilities sit under tau at a "
            f"{_fmt(meta['base_rate_test'], 3)} base rate. The trade-off "
            "between P&L capture and trade count must be re-judged on data "
            "with real signal.")
    else:
        L.append(
            "3. The meta-labeling machinery (calibration + economic "
            "evaluation) behaves sensibly: probabilities are calibrated by "
            f"{cal_long} on {cal_pos} calibration positives (Brier "
            f"{_fmt(meta['brier_test'], 4)}), and the gate trades P&L "
            "capture against trade count exactly as designed. Its economic "
            "value must be re-judged on data with real signal.")
    L.append(
        f"4. Runtime {runtime:.0f}s; every fit is a tracked run under "
        "`research/models/` with manifest (git commit, data/feature/model "
        "versions, windows, hardware), metrics and pickled model.")
    L.append("")

    report_path = _HERE / "ML_REPORT.md"
    report_path.write_text("\n".join(L))
    print(f"wrote {report_path} ({runtime:.0f}s, "
          f"{n_experiments} experiments in ledger)")


if __name__ == "__main__":
    main()
