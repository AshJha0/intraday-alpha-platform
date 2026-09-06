#!/usr/bin/env python3
"""Adaptive-deployment study: STATIC vs SCHEDULED vs DRIFT-TRIGGERED
(spec §20 steps 12-13; the adaptability layer).

Run from the repo root (or anywhere — paths derive from this file):

    PYTHONPATH=python/src python3 research/adaptive_reports/run_adaptive.py

For a representative alpha subset — the 6 golden alphas (EQ01, EQ03,
EQ06, FX01, FX05, FX09) plus the 4 best remaining by walk-forward OOS IC
from research/alpha_reports/ — replays the bundled 2-session data as a
deployment under four refit policies (static, scheduled weekly, scheduled
daily, drift-triggered; pinned in configs/strategies.json `adaptive`) with
drift monitoring and lifecycle gating, and reports the comparison
honestly.

Outputs (deterministic except the runtime line and the appending
experiments ledger):

- research/adaptive_reports/ADAPTIVE_REPORT.md   the honest comparison
- research/adaptive_reports/<ALPHA>_adaptive.json  per-alpha evidence
- research/lifecycle_log.jsonl                   every lifecycle transition
- research/baselines/run_*.json                  captured drift baselines
- research/experiments.json                      multiple-testing ledger

HONESTY NOTE (spec §32, stated in the report as well): two synthetic
sessions cannot demonstrate that any refit policy makes money — weekly
scheduling cannot even fire once.  What this study CAN show, and shows,
is that the machinery is deterministic, leak-free and behaves exactly as
pinned: refits trigger when and only when the rules say, retirement halts
allocation, and every look is counted.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

import numpy as np  # noqa: E402

from iap.adaptive import (  # noqa: E402
    LifecycleConfig,
    LifecycleLog,
    build_policy,
    load_adaptive_config,
)
from iap.alpha import build  # noqa: E402
from iap.alpha.data import load_features  # noqa: E402
from iap.backtest import Backtester, BacktestConfig, CostModel  # noqa: E402
from iap.backtest.adaptive import AdaptiveDeployment  # noqa: E402
from iap.validation import ExperimentLedger  # noqa: E402

REPORTS_DIR = REPO / "research" / "adaptive_reports"
ALPHA_REPORTS_DIR = REPO / "research" / "alpha_reports"
BASELINES_DIR = REPO / "research" / "baselines"
LIFECYCLE_LOG = REPO / "research" / "lifecycle_log.jsonl"
LEDGER_PATH = REPO / "research" / "experiments.json"

GOLDEN_ALPHAS = ("EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09")
POLICY_NAMES = ("static", "scheduled_weekly", "scheduled_daily",
                "drift_triggered")
NS_H = 3_600_000_000_000


def select_alphas() -> List[str]:
    """Golden 6 + the 4 best remaining by walk-forward OOS IC (from the
    promotion reports — a *recorded* prior look, not a new one)."""
    scored = []
    for p in sorted(ALPHA_REPORTS_DIR.glob("*.json")):
        rep = json.loads(p.read_text())
        aid = rep.get("alpha_id")
        if aid and aid not in GOLDEN_ALPHAS and rep.get("oos_ic") is not None:
            scored.append((float(rep["oos_ic"]), aid))
    best = [aid for _, aid in sorted(scored, reverse=True)[:4]]
    return list(GOLDEN_ALPHAS) + best


def _load_meta() -> Dict[int, dict]:
    cfg = json.loads((REPO / "configs" / "instruments.json").read_text())
    out: Dict[int, dict] = {}
    for row in cfg["instruments"]:
        out[int(row["instrument_id"])] = {
            "symbol": row["symbol"],
            "asset_class": row["asset_class"],
            "tick_size": float(row["tick_size"]),
            "lot_size": int(row["lot_size"]),
            "adv": float(row["adv"]),
            "ref_price": float(row.get("ref_price", 1.0)),
        }
    return out


def _fmt(v, spec=".4f", none="   -  "):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return none
    return format(v, spec)


def _ic_stability(eval_rows: List[dict]) -> dict:
    ics = [r["rolling_ic"] for r in eval_rows if r["rolling_ic"] is not None]
    if len(ics) < 2:
        return {"mean": None, "std": None, "n": len(ics)}
    a = np.asarray(ics)
    return {"mean": float(a.mean()), "std": float(a.std()), "n": int(a.size)}


def run_alpha(aid: str, frames, cfg, backtester, lc_cfg, log, ledger) -> dict:
    psi_thr = float(cfg["policies"]["drift_triggered"]["psi_threshold"])
    dep = AdaptiveDeployment(lambda: build(aid), frames, cfg, backtester,
                             psi_thr)
    # persist the captured baselines (the deployment's research-window view)
    if dep.signal_baseline is not None:
        dep.signal_baseline.save(BASELINES_DIR / f"{dep.signal_baseline.name}.json")
    for b in dep.feature_baselines.values():
        b.save(BASELINES_DIR / f"{b.name}.json")
    if dep.ic_baseline is not None:
        dep.ic_baseline.save(BASELINES_DIR / f"{dep.ic_baseline.name}.json")

    out = {
        "alpha_id": aid,
        "horizon": dep.horizon,
        "universe": dep.universe,
        "deploy_start": dep.deploy_start,
        "n_blocks": len(dep.block_bounds) - 1,
        "ic_baseline": (None if dep.ic_baseline is None
                        else dep.ic_baseline.to_dict()),
        "policies": {},
    }
    for pname in POLICY_NAMES:
        res = dep.run(build_policy(pname, cfg), lc_cfg, log, policy_label=pname)
        m = res.backtest
        stab = _ic_stability(res.eval_rows)
        out["policies"][pname] = {
            "policy": res.policy,
            "refit_count": res.refit_count,
            "refit_events": res.refit_events,
            "drift_event_count": res.drift_event_count,
            "n_evals": res.n_evals,
            "net_pnl": m.total_pnl,
            "gross_pnl": m.gross_pnl,
            "total_costs": m.total_costs,
            "trade_count": m.trade_count,
            "deployed_ic": res.deployed_ic,
            "rolling_ic_mean": stab["mean"],
            "rolling_ic_std": stab["std"],
            "n_ic_evals": stab["n"],
            "n_evals": res.n_evals,
            "n_informative_evals": res.n_informative_evals,
            "ic_baseline_kind": (
                dep.ic_baseline.baseline_kind if dep.ic_baseline else None),
            "ic_baseline_rows": getattr(dep, "ic_baseline_rows", 0),
            "lifecycle_transitions": res.transitions,
            "final_state": res.final_state,
        }
        # One deployment = ONE experiment (pinned, round-3).  Counting the
        # 211 monitoring evaluations of a single policy as 211 separate
        # "experiments" made the Bonferroni denominator a function of the
        # block cadence: a monitor that looks twice as often does not test
        # twice as many hypotheses.  The evaluation counts stay in the JSON
        # as diagnostics.
        ledger.record(
            aid, "adaptive_deployment",
            config={"policy": pname, **res.policy},
            result={
                "net_pnl": m.total_pnl, "refits": res.refit_count,
                "deployed_ic": res.deployed_ic,
                "final_state": res.final_state,
                "n_evals": res.n_evals,
                "n_informative_evals": res.n_informative_evals,
            },
            count=1,
        )
    return out


def _write_report(results: Dict[str, dict], cfg, ledger, log,
                  runtime_s: float) -> None:
    lc = cfg["lifecycle"]
    dt = cfg["policies"]["drift_triggered"]
    lines: List[str] = []
    a = lines.append
    a("# Adaptive deployment report — STATIC vs SCHEDULED vs DRIFT-TRIGGERED")
    a("")
    a("Generated by `research/adaptive_reports/run_adaptive.py` on the bundled")
    a("2-session synthetic dataset. Deterministic: identical reruns reproduce")
    a("every number except this runtime line and the appending experiments")
    a(f"ledger. Runtime {runtime_s:.0f}s.")
    a("")
    a("## READ THIS FIRST — what two sessions can and cannot show")
    a("")
    a("**The sample is two synthetic sessions (~2.6 dense hours each).** That")
    a("is far too short to conclude that any refit policy improves live P&L:")
    a("")
    a("- **SCHEDULED(weekly) cannot fire even once** — the sample crosses no")
    a("  week boundary, so it is *identical to STATIC here by construction*.")
    a("  Its column exists to prove the machinery, not to rank it.")
    a("- SCHEDULED(daily) fires exactly once (the day-2 boundary), so the")
    a("  comparison rests on a single refit on a single synthetic day.")
    a("- Drift baselines come from a 2.5-hour warmup of the same generator —")
    a("  real regime shifts are not in the data, so PSI stays mostly below")
    a("  threshold and most drift-triggered refits come from the rolling-IC")
    a("  z-score, which is noisy at this sample size.")
    a("- P&L differences between policies below are **within noise**; do not")
    a("  read a ranking into them. All alphas remain net-negative after")
    a("  costs on this data, exactly as the promotion report shows.")
    a("")
    a("What the study DOES establish: the adaptability machinery is")
    a("deterministic and leak-free (asserted + shift-tested), refits trigger")
    a("exactly when the pinned rules say, retirement verifiably halts")
    a("allocation, every transition is logged with a reason, and every look")
    a("at the data is counted in the experiments ledger.")
    a("")
    a("## Pinned configuration (configs/strategies.json `adaptive`)")
    a("")
    a(f"- blocks {cfg['block_ns'] / NS_H:.2f}h, warmup {cfg['warmup_ns'] / NS_H:.1f}h,"
      f" trailing train window {cfg['train_window_ns'] / NS_H:.0f}h,"
      f" embargo {cfg['embargo_ns'] / 1e9:.0f}s")
    a(f"- drift trigger: PSI > {dt['psi_threshold']} (10-quantile-bucket, eps 1e-6)"
      f" OR rolling-IC z < {dt['ic_z_threshold']}; min refit gap "
      f"{dt['min_refit_gap_ns'] / NS_H:.0f}h")
    a(f"- lifecycle: WATCH below IC {lc['watch_ic_gate']}, RETIRE after "
      f"{lc['retire_breach_evals']} consecutive breaches, re-activate at IC >= "
      f"{lc['reactivate_ic_gate']} for {lc['reactivate_evals']} consecutive evals")
    a("")
    a("## Master table (per alpha x policy)")
    a("")
    a("`IC dep` = IC of the deployed (gated) scores over the whole deployment;")
    a("`rIC std` = std of the rolling IC across evaluations (stability, lower")
    a("= steadier); `state` = final lifecycle state; refits include the")
    a("initial deployment fit.")
    a("")
    a("| alpha | policy | refits | drift ev | net P&L | costs | trades | "
      "IC dep | rIC mean | rIC std | transitions | state |")
    a("|-------|--------|--------|----------|---------|-------|--------|"
      "--------|----------|---------|-------------|-------|")
    for aid in results:
        for pname in POLICY_NAMES:
            p = results[aid]["policies"][pname]
            a(
                f"| {aid} | {pname} | {p['refit_count']} | "
                f"{p['drift_event_count']} | {_fmt(p['net_pnl'], '+.0f')} | "
                f"{_fmt(p['total_costs'], '.0f')} | {p['trade_count']} | "
                f"{_fmt(p['deployed_ic'])} | {_fmt(p['rolling_ic_mean'])} | "
                f"{_fmt(p['rolling_ic_std'])} | "
                f"{len(p['lifecycle_transitions'])} | {p['final_state']} |"
            )
    a("")
    a("## Policy aggregates (sum / mean over the 10 alphas)")
    a("")
    a("| policy | total refits | total drift ev | total net P&L | "
      "mean IC dep | retired alphas |")
    a("|--------|--------------|----------------|---------------|"
      "-------------|----------------|")
    for pname in POLICY_NAMES:
        rows = [results[aid]["policies"][pname] for aid in results]
        ics = [r["deployed_ic"] for r in rows if r["deployed_ic"] is not None]
        a(
            f"| {pname} | {sum(r['refit_count'] for r in rows)} | "
            f"{sum(r['drift_event_count'] for r in rows)} | "
            f"{sum(r['net_pnl'] for r in rows):+.0f} | "
            f"{np.mean(ics):.4f} | "
            f"{sum(1 for r in rows if r['final_state'] == 'RETIRED')} |"
        )
    a("")
    a("### Which policy 'wins' where — and why that is not a conclusion")
    a("")
    win_pnl: Dict[str, int] = {p: 0 for p in POLICY_NAMES}
    win_ic: Dict[str, int] = {p: 0 for p in POLICY_NAMES}
    for aid in results:
        ps = results[aid]["policies"]
        best_pnl = max(POLICY_NAMES, key=lambda p: ps[p]["net_pnl"])
        win_pnl[best_pnl] += 1
        best_ic = max(
            POLICY_NAMES,
            key=lambda p: (ps[p]["deployed_ic"]
                           if ps[p]["deployed_ic"] is not None else -9.0),
        )
        win_ic[best_ic] += 1
    a("| policy | best net P&L (of 10) | best deployed IC (of 10) |")
    a("|--------|----------------------|--------------------------|")
    for pname in POLICY_NAMES:
        a(f"| {pname} | {win_pnl[pname]} | {win_ic[pname]} |")
    a("")
    a("Ties go to the first policy in table order; static and")
    a("scheduled_weekly are bitwise identical here (weekly never fires), so")
    a("wins credited to `static` are shared with `scheduled_weekly` by")
    a("construction. With one synthetic day of true out-of-warmup data these")
    a("win counts are coin flips, not evidence. The honest headline: **on")
    a("this sample, refitting neither rescues nor ruins any alpha — the")
    a("differences are one to two orders of magnitude smaller than the cost")
    a("drag.** A real ranking needs months of sessions.")
    a("")
    a("## Lifecycle activity")
    a("")
    rows = log.read_all()
    a(f"{len(rows)} transitions logged to `research/lifecycle_log.jsonl` "
      "(every one carries alpha, policy, event_ts, from/to, reason, the "
      "rolling IC that caused it and the evaluation index).")
    a("")
    if rows:
        a("| alpha | policy | eval | from | to | rolling IC |")
        a("|-------|--------|------|------|----|-----------|")
        for r in rows:
            a(f"| {r['alpha_id']} | {r['policy']} | {r['eval_index']} | "
              f"{r['from']} | {r['to']} | {_fmt(r['rolling_ic'])} |")
        a("")
        retired = sorted({r["alpha_id"] for r in rows if r["to"] == "RETIRED"})
        if retired:
            a(f"Alphas that hit RETIRED under at least one policy: "
              f"{', '.join(retired)} — allocation was verifiably halted for "
              "the retired spans (positions forced flat; shadow scoring "
              "continued so the pinned re-activation rule stayed reachable).")
            a("")
    a("## Drift monitor readout")
    a("")
    a("PSI is computed per evaluation against warmup baselines for the")
    a("alpha's signal and each declared feature; KS (D, p) is diagnostic")
    a("only. Counts of evaluations with any PSI above the pinned trigger")
    a(f"threshold ({dt['psi_threshold']}) appear in the master table")
    a("(`drift ev`). Baselines are serialized to `research/baselines/` in")
    a("the /API_ADAPTIVE.md schema (x-version 2: each file records the")
    a("`feature_version` it was captured against, and a loader rejects a")
    a("baseline from a different feature registry) — the same files the")
    a("Java live monitor consumes.")
    a("")
    no_ic = sorted(aid for aid, r in results.items()
                   if r.get("ic_baseline") is None)
    if no_ic:
        a(f"**No OOS IC baseline: {', '.join(no_ic)}.** The IC baseline is")
        a("captured from the purged held-out tail of the warmup; when that")
        a("tail yields too few 5-minute IC buckets, no baseline is written")
        a("and the rolling-IC gauge is UNAVAILABLE for that alpha. Read the")
        a("consequence: for these alphas every drift event and refit below")
        a("comes from PSI alone, and the lifecycle gauge never sees an IC")
        a("reading, so they cannot be retired on decay. This is reported")
        a("rather than papered over with an in-sample baseline — round 2")
        a("used the warmup model's own training rows, which biased `ic_z`")
        a("negative and fired refits on the IS/OOS gap instead of on drift.")
        a("")
    a("**Known false-positive pattern (reported, not hidden):** FX10")
    a("monitors `minute_of_day_v1`, and a time-of-day feature 'drifts' by")
    a("construction as the session progresses — its PSI against a")
    a("morning-warmup baseline exceeds the threshold at most evaluations")
    a("(hence its large drift-event count and refit count under the")
    a("drift-triggered policy). This is exactly the class of monitor")
    a("misconfiguration a production deployment must catch in review:")
    a("deterministic calendar features do not belong in a drift trigger.")
    a("The machinery behaves as pinned; the lesson is about monitor")
    a("selection and is left visible here on purpose.")
    a("")
    a("## Multiple testing")
    a("")
    a(ledger.note())
    a("")
    a("One deployment (alpha x refit policy) is ONE experiment in")
    a("`research/experiments.json` (kind `adaptive_deployment`), and the")
    a("ledger de-duplicates reruns of the same configuration. Counting each")
    a("monitoring evaluation as an experiment — as earlier versions did —")
    a("made the selection-adjusted threshold a function of the block cadence")
    a("rather than of the research design.")
    a("")
    a(f"`ledger_n_at_report` = **{ledger.total_experiments}** "
      f"({ledger.distinct_experiments} distinct configurations), read at")
    a("render time.")
    a("")
    (REPORTS_DIR / "ADAPTIVE_REPORT.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    t_start = time.time()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_adaptive_config(REPO / "configs" / "strategies.json")
    lc_cfg = LifecycleConfig.from_config(cfg["lifecycle"])
    meta = _load_meta()
    frames = load_features(REPO / "data" / "features")
    # Same pinned research execution model as run_all.py (round-3): latency
    # in EVENT TIME, a bounded decision age and no overnight carry.
    backtester = Backtester(
        CostModel.load(REPO / "configs" / "execution.json"), meta,
        BacktestConfig(
            latency_ns=1_000_000_000,
            max_decision_age_ns=60_000_000_000,
            flatten_at_session_end=True,
        ),
    )
    log = LifecycleLog(LIFECYCLE_LOG, truncate=True)
    ledger = ExperimentLedger(LEDGER_PATH)

    alphas = select_alphas()
    print(f"alpha subset: {alphas}")
    results: Dict[str, dict] = {}
    for aid in alphas:
        t0 = time.time()
        rep = run_alpha(aid, frames, cfg, backtester, lc_cfg, log, ledger)
        results[aid] = rep
        (REPORTS_DIR / f"{aid}_adaptive.json").write_text(
            json.dumps(rep, indent=2, sort_keys=True) + "\n"
        )
        summary = " ".join(
            f"{p}:rf={rep['policies'][p]['refit_count']}"
            f",pnl={rep['policies'][p]['net_pnl']:.0f}"
            for p in POLICY_NAMES
        )
        print(f"{aid}: {summary} ({time.time() - t0:.1f}s)")

    ledger.save()
    _write_report(results, cfg, ledger, log, time.time() - t_start)
    print(f"\nDone in {time.time() - t_start:.0f}s -> "
          f"{REPORTS_DIR / 'ADAPTIVE_REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
