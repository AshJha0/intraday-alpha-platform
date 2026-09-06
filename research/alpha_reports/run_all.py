#!/usr/bin/env python3
"""Full promotion pipeline for the 24 flagship alphas (spec §§11-13, 20).

Run from the repo root (or anywhere — paths are derived from this file):

    PYTHONPATH=python/src python3 research/alpha_reports/run_all.py

For every alpha EQ01..EQ12 / FX01..FX12 on the bundled 2-day synthetic
feature data (data/features/):

1. expanding walk-forward validation (4 folds, purged at the alpha's label
   horizon, 60 s embargo) — OOS IC / RankIC / Newey-West-lite t-stat /
   hit rate / fold sign consistency;
2. automatic leakage tests (label-column guard + shift-by-one);
3. decay curve across the 11 pinned horizons;
4. turnover + capacity proxies;
5. cost x{0.5,1,2}, latency +{0,1,5} events, vol-regime split stress;
6. verdict per the pinned spec §20 gates (PROMOTE / ITERATE / REJECT).

Outputs (all deterministic — rerunning on the same data reproduces them,
except the experiments ledger which appends by design):

- research/alpha_reports/REPORT.md          the honest master table
- research/alpha_reports/<ALPHA_ID>.json    full per-alpha evidence
- research/experiments.json                 multiple-testing ledger
- configs/strategies/alpha_params.json      day-1-fitted production params
  (the contract input for the Java/C++/Rust ports, see /API_ALPHA.md)

Honest-reporting note (conventions §7, spec §32): the data is synthetic;
several stated microstructure hypotheses do NOT hold on it (the generator
mean-reverts harder than real markets) and several alphas therefore fail
their gates.  That is the expected, truthful outcome — the report shows it.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

import numpy as np  # noqa: E402

from iap.alpha import ALPHA_IDS, build, fit_all, save_params  # noqa: E402
from iap.alpha.data import load_features, session_days, split_by_day  # noqa: E402
from iap.backtest import Backtester, BacktestConfig, CostModel  # noqa: E402
from iap.backtest.engine import ensemble_scores  # noqa: E402
from iap.validation import ExperimentLedger, validate_alpha  # noqa: E402
from iap.validation.validate import GATES  # noqa: E402

REPORTS_DIR = REPO / "research" / "alpha_reports"
LEDGER_PATH = REPO / "research" / "experiments.json"

#: Pinned research execution model (round-3): 1 s of event-time latency, a
#: 60 s decision age bound and session flattening.
RESEARCH_LATENCY_NS = 1_000_000_000
RESEARCH_MAX_DECISION_AGE_NS = 60_000_000_000
PARAMS_PATH = REPO / "configs" / "strategies" / "alpha_params.json"

#: looks-at-the-data counted per alpha in one pipeline run: 1 walk-forward
#: eval + 11 decay horizons + 3 cost + 3 latency + 2 regime + 1 backtest
LOOKS_PER_ALPHA = 21
#: pinned-horizon selection during alpha design scanned 9 horizons x 24
#: alphas once; recorded the first time the ledger is created
DESIGN_SCAN_COUNT = 216


def _load_meta() -> Dict[int, dict]:
    cfg = json.loads((REPO / "configs" / "instruments.json").read_text())
    meta: Dict[int, dict] = {}
    for row in cfg["instruments"]:
        meta[int(row["instrument_id"])] = {
            "symbol": row["symbol"],
            "asset_class": row["asset_class"],
            "tick_size": float(row["tick_size"]),
            "lot_size": int(row["lot_size"]),
            "adv": float(row["adv"]),
            "ref_price": float(row.get("ref_price", 1.0)),
            # currency of prices / P&L (pinned: converted to USD per row by
            # the backtester, never summed across quote currencies)
            "base_currency": row.get("base_currency"),
            "quote_currency": row.get("quote_currency", row.get("currency", "USD")),
        }
    return meta


def _capital_usd(backtester: Backtester, meta: Dict[int, dict], iids) -> float:
    """max_pos x ref_price x unit per instrument, converted to USD with the
    conversion pair's ref_price (a JPY-quoted capital line is not USD)."""
    total = 0.0
    for i in iids:
        unit = meta[i]["lot_size"] if meta[i]["asset_class"] == "FX" else 1
        native = backtester.config.max_pos_qty * meta[i]["ref_price"] * unit
        total += native * backtester.reference_rate(backtester.quote_currency(i))
    return total


def _fmt(v, spec=".4f", none="   -  "):
    if v is None:
        return none
    if isinstance(v, float) and not np.isfinite(v):
        return none
    return format(v, spec)


def data_quality_lines() -> list:
    """Report the data-quality facts a researcher must know FIRST.

    Read from ``data/features/features_summary.json`` (written by the
    feature pipeline), so the report can never drift from the data.
    """
    path = REPO / "data" / "features" / "features_summary.json"
    if not path.is_file():
        return ["_(features_summary.json not found — regenerate the dataset)_"]
    summary = json.loads(path.read_text())
    inst = summary.get("instruments", {})
    lines = [
        "| instrument | rows | crossed book | mean row gap | label max age "
        "| valid 1m | zero 1s | zero 1m |",
        "|------------|------|--------------|--------------|---------------"
        "|----------|---------|---------|",
    ]
    for iid in sorted(inst, key=int):
        v = inst[iid]
        vf = v.get("label_valid_frac_by_horizon", {})
        zf = v.get("label_zero_frac_by_horizon", {})
        gap = v.get("mean_row_gap_ns")
        lines.append(
            f"| {v.get('symbol', iid)} | {v.get('rows', 0)} | "
            f"{_fmt(v.get('crossed_frac'), '.3f')} | "
            f"{(gap / 1e9):.1f}s | "
            f"{(v.get('label_max_age_ns', 0) / 1e9):.0f}s | "
            f"{_fmt(vf.get('1m'), '.3f')} | {_fmt(zf.get('1s'), '.3f')} | "
            f"{_fmt(zf.get('1m'), '.3f')} |"
        )
    # The FX crossed-book range is DERIVED from the same table rows above:
    # a hard-coded range here once contradicted the table two lines below it.
    fx_crossed = sorted(
        v["crossed_frac"] for iid, v in inst.items()
        if int(iid) >= 100 and isinstance(v.get("crossed_frac"), (int, float))
        and np.isfinite(v["crossed_frac"]))
    if fx_crossed:
        fx_range = (f"{fx_crossed[0] * 100:.0f}-{fx_crossed[-1] * 100:.0f} % "
                    f"of rows (mean {np.mean(fx_crossed) * 100:.0f} %)")
    else:
        fx_range = "not measurable from features_summary.json"
    lines += [
        "",
        "- **crossed book** is the fraction of emissions whose CONSOLIDATED",
        "  book is crossed (`spread_ticks_v1 < 0`) because one venue's quote",
        f"  is stale. On the FX pairs this is {fx_range}, and the mid on",
        "  those rows reverts mechanically when the stale LP refreshes — a",
        "  vol-scaled momentum signal is paid for measuring that artefact.",
        "  Every IC in this report is therefore split crossed/uncrossed.",
        "- **mean row gap** is the spacing of emissions. Equity rows are ~3.3 s",
        "  apart, FX rows ~15-22 s: any \"+1 event\" latency claim means two",
        "  different things, which is why the latency stress is in event time.",
        "- **zero 1s / zero 1m** is the fraction of VALID labels that are",
        "  exactly 0. A 500 ms-5 s FX label is 89-99 % zeros: a REJECT at those",
        "  horizons is a sampling artefact of the emission cadence, not",
        "  evidence against the hypothesis.",
        "- **label max age** is the pinned freshness bound",
        "  `max(5 s, 2 x median quote gap)`: a label whose forward mid is",
        "  older than this is INVALID (API_FEATURES section 6), which is why",
        "  15 m equity labels near the session close no longer exist.",
    ]
    return lines


def _write_report(reports: Dict[str, dict], ledger: ExperimentLedger,
                  day2_bt: Dict[str, dict], ensembles: Dict[str, dict],
                  runtime_s: float) -> None:
    lines = []
    a = lines.append
    a("# Flagship alpha promotion report (spec §§11-13, §20)")
    a("")
    a("Generated by `research/alpha_reports/run_all.py` on the bundled 2-day")
    a("seeded synthetic dataset (19 instruments, data/features/). Every metric")
    a("is deterministic — an identical rerun reproduces every number in every")
    a("table; only this runtime line and the appending experiments ledger")
    a(f"(hence the multiple-testing counts) move. Runtime {runtime_s:.0f}s.")
    a("")
    a("**Honesty note (spec §32):** the data is synthetic and strongly")
    a("mean-reverting; several textbook microstructure hypotheses do not hold on")
    a("it. Alphas failing their gates below are reported truthfully — a REJECT")
    a("on synthetic data is a statement about this dataset, not the idea.")
    a("`hyp` = fitted slope agrees with the stated economic rationale's sign;")
    a("alphas with `hyp=no` can never be PROMOTE regardless of |IC|.")
    a("")
    a("**Currency (round 3, PLATFORM_CONVENTIONS.md §11.6):** every P&L, cost")
    a("and capital figure below is in USD. FX instruments are quoted in their")
    a("quote currency (JPY for USD/JPY, CAD for USD/CAD, CHF for USD/CHF, GBP")
    a("for EUR/GBP) and each row's P&L is converted at the prevailing mid of")
    a("the conversion pair from the same frame set; earlier reports summed")
    a("quote-currency P&L as if USD (JPY-dominated FX rows) — those numbers")
    a("were wrong in scale, not in sign, and are superseded here.")
    a("")
    a("## Pinned promotion gates (spec §20)")
    a("")
    a(f"- PROMOTE: leakage pass AND UNCROSSED OOS IC >= {GATES['min_oos_ic']}")
    a(f"  AND its NW t >= {GATES['min_nw_tstat']} AND fold sign consistency >= "
      f"{GATES['min_fold_sign_consistency']} AND >= "
      f"{GATES['min_nondegenerate_folds']} non-degenerate folds AND hypothesis "
      "confirmed AND net P&L > 0 at 1x costs.")
    a(f"- ITERATE: leakage pass AND OOS IC >= {GATES['iterate_min_ic']} AND "
      f"NW t >= {GATES['iterate_min_tstat']}.")
    a("- REJECT: otherwise (always, on leakage failure).")
    a("")
    a("## Which rows are trustworthy (read this before the tables)")
    a("")
    for line in data_quality_lines():
        a(line)
    a("")
    a("## Master table (walk-forward OOS, expanding purged+embargoed folds)")
    a("")
    a("Folds are quantiles of the ROW INDEX, not of the wall span: this data")
    a("occupies ~2.6 h of each 24 h day, so equal wall segments used to leave")
    a("two of four folds EMPTY while the report still claimed 4 folds. A fold")
    a("with fewer than 32 usable pairs is DEGENERATE and counts as a failed")
    a("fold in `folds+`; PROMOTE needs >= 3 non-degenerate folds.")
    a("")
    a("`IC` is the pooled OOS IC on the standardized signal; `IC unc` is the")
    a("same IC restricted to rows whose consolidated book was NOT crossed")
    a("(a crossed book means a stale venue quote whose mid mechanically")
    a("reverts). **The gates read `IC unc` and its NW t.**")
    a("")
    a("| alpha | horizon | IC | IC unc | IC crs | crs% | NW t unc | L | hit "
      "| folds+ | deg | hyp | leak | net P&L 1x | flips/h | verdict |")
    a("|-------|---------|----|--------|--------|------|----------|---|-----"
      "|--------|-----|-----|------|-----------|---------|---------|")
    for aid in ALPHA_IDS:
        r = reports[aid]
        a(
            f"| {aid} | {r['horizon']} | {_fmt(r['oos_ic'])} | "
            f"{_fmt(r.get('oos_ic_uncrossed'))} | "
            f"{_fmt(r.get('oos_ic_crossed'))} | "
            f"{_fmt(r.get('crossed_frac'), '.3f')} | "
            f"{_fmt(r.get('nw_tstat_uncrossed'), '.2f')} | "
            f"{r.get('nw_lags', '-')} | "
            f"{_fmt(r['oos_hit_rate'], '.3f')} | "
            f"{_fmt(r['fold_sign_consistency'], '.2f')} | "
            f"{r.get('n_degenerate_folds', '-')} | "
            f"{'yes' if r['hypothesis_confirmed'] else 'no'} | "
            f"{'pass' if r['leakage']['passed'] else 'FAIL'} | "
            f"{_fmt(r['net_pnl_1x_cost'], '+.0f')} | "
            f"{_fmt(r['turnover_flips_per_hour'], '.0f')} | **{r['verdict']}** |"
        )
    n_promote = sum(1 for r in reports.values() if r["verdict"] == "PROMOTE")
    n_iterate = sum(1 for r in reports.values() if r["verdict"] == "ITERATE")
    n_reject = sum(1 for r in reports.values() if r["verdict"] == "REJECT")
    a("")
    a(f"**Verdicts: {n_promote} PROMOTE / {n_iterate} ITERATE / "
      f"{n_reject} REJECT** (of {len(reports)}).")
    a("")
    a("## Decay curves (OOS IC by horizon, last fold)")
    a("")
    horizons = ["10ms", "50ms", "100ms", "500ms", "1s", "5s", "10s", "30s",
                "1m", "5m", "15m"]
    a("| alpha | " + " | ".join(horizons) + " |")
    a("|-------|" + "|".join(["------"] * len(horizons)) + "|")
    for aid in ALPHA_IDS:
        d = reports[aid]["decay_ic_by_horizon"]
        a(f"| {aid} | " + " | ".join(_fmt(d.get(h), "+.3f") for h in horizons)
          + " |")
    a("")
    a("## Cost and latency stress (last fold, net P&L)")
    a("")
    a("Latency is stressed in EVENT TIME (a row is ~3.3 s on equities and")
    a("~15 s on FX, so an \"+N events\" grid is not a latency budget). The")
    a("event grid is kept as the last three columns for continuity.")
    a("")
    a("| alpha | x0.5 cost | x1 cost | x2 cost | 100ms P&L | 500ms | 1s "
      "| 5s | +0ev IC | +1ev IC | +5ev IC |")
    a("|-------|-----------|---------|---------|-----------|-------|----"
      "|----|---------|---------|---------|")
    for aid in ALPHA_IDS:
        st = reports[aid]["stress"]
        lt = st.get("latency_time", {})
        a(
            f"| {aid} | {_fmt(st['cost']['x0.5']['total_pnl'], '+.0f')} | "
            f"{_fmt(st['cost']['x1']['total_pnl'], '+.0f')} | "
            f"{_fmt(st['cost']['x2']['total_pnl'], '+.0f')} | "
            + " | ".join(
                _fmt(lt.get(k, {}).get("total_pnl"), '+.0f')
                for k in ("100ms", "500ms", "1s", "5s")) + " | "
            f"{_fmt(st['latency']['+0ev']['ic'], '+.4f')} | "
            f"{_fmt(st['latency']['+1ev']['ic'], '+.4f')} | "
            f"{_fmt(st['latency']['+5ev']['ic'], '+.4f')} |"
        )
    a("")
    a("## Regime split (OOS IC, last fold)")
    a("")
    a("| alpha | high-vol IC | low-vol IC |")
    a("|-------|-------------|------------|")
    for aid in ALPHA_IDS:
        rg = reports[aid]["stress"]["regime"]
        a(f"| {aid} | {_fmt(rg['ic_high_vol'], '+.4f')} | "
          f"{_fmt(rg['ic_low_vol'], '+.4f')} |")
    a("")
    a("## Day-2 out-of-sample backtest (day-1-fitted params, 1x costs)")
    a("")
    a("These are the exact parameters serialized to "
      "`configs/strategies/alpha_params.json` (the port contract).")
    a("")
    a("| alpha | net P&L | gross P&L | costs | trades | ann. Sharpe | maxDD |")
    a("|-------|---------|-----------|-------|--------|-------------|-------|")
    for aid in ALPHA_IDS:
        m = day2_bt[aid]
        a(
            f"| {aid} | {_fmt(m['total_pnl'], '+.0f')} | "
            f"{_fmt(m['gross_pnl'], '+.0f')} | {_fmt(m['total_costs'], '.0f')} |"
            f" {m['trade_count']} | {_fmt(m['sharpe_ann'], '+.2f')} | "
            f"{_fmt(m['max_drawdown'], '.0f')} |"
        )
    a("")
    a("| ensemble | net P&L | gross P&L | costs | trades | ann. Sharpe |")
    a("|----------|---------|-----------|-------|--------|-------------|")
    for name, m in sorted(ensembles.items()):
        a(
            f"| {name} | {_fmt(m['total_pnl'], '+.0f')} | "
            f"{_fmt(m['gross_pnl'], '+.0f')} | {_fmt(m['total_costs'], '.0f')} |"
            f" {m['trade_count']} | {_fmt(m['sharpe_ann'], '+.2f')} |"
        )
    a("")
    a("Sharpe scaling: 1-minute event-time bars, annualized by "
      "sqrt(252 * session_hours * 60) (6.5h equities, 21h FX); assumes "
      "independent bar P&L — a research yardstick, not a production claim.")
    a("")
    a("## Multiple testing")
    a("")
    a(ledger.note())
    a("")
    a(f"`ledger_n_at_report` = **{ledger.total_experiments}** "
      f"({ledger.distinct_experiments} distinct configurations). This number is")
    a("read from `research/experiments.json` at render time, so the report and")
    a("the ledger can never disagree — the stale \"1 224 experiments\" note")
    a("earlier versions carried is gone.")
    a("")
    a("Every walk-forward evaluation, decay-curve horizon, stress variant and")
    a("backtest in this run is counted in `research/experiments.json` "
      f"({LOOKS_PER_ALPHA} looks per alpha per run), plus the one-time design "
      f"horizon scan ({DESIGN_SCAN_COUNT} looks). Experiments are "
      "DE-DUPLICATED by (alpha, kind, configuration): re-running this script "
      "does not inflate the denominator, so the selection-adjusted threshold "
      "is a property of the research design, not of how often the script ran.")
    a("")
    a("Alphas whose |NW t| (uncrossed) is below the selection yardstick "
      f"{ledger.expected_max_null_t():.2f} are consistent with pure selection "
      "over this many looks, whatever their verdict:")
    below = [
        aid for aid in ALPHA_IDS
        if (reports[aid].get("nw_tstat_uncrossed") is not None
            and abs(reports[aid]["nw_tstat_uncrossed"])
            < ledger.expected_max_null_t())
    ]
    a("")
    a("- " + (", ".join(below) if below else "none"))
    a("")
    (REPORTS_DIR / "REPORT.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    t_start = time.time()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    meta = _load_meta()
    frames = load_features(REPO / "data" / "features")
    exec_cfg = json.loads((REPO / "configs" / "execution.json").read_text())
    max_participation = float(exec_cfg["defaults"]["max_participation"])
    cost_model = CostModel.load(REPO / "configs" / "execution.json")
    # Round-3 pinned research execution model: latency in EVENT TIME, a
    # bounded decision age (a 4-hour-old decision no longer "fills" at the
    # 20:00 close print) and no overnight carry.
    backtester = Backtester(cost_model, meta, BacktestConfig(
        latency_ns=RESEARCH_LATENCY_NS,
        max_decision_age_ns=RESEARCH_MAX_DECISION_AGE_NS,
        flatten_at_session_end=True,
    ))

    ledger = ExperimentLedger(LEDGER_PATH)
    if ledger.total_experiments == 0:
        ledger.record(
            "ALL", "design_horizon_scan",
            config={"horizons_scanned": 9, "alphas": 24},
            count=DESIGN_SCAN_COUNT,
        )

    reports: Dict[str, dict] = {}
    for aid in ALPHA_IDS:
        t0 = time.time()
        rep = validate_alpha(
            lambda aid=aid: build(aid), frames, backtester, meta,
            max_participation,
        )
        reports[aid] = rep
        ledger.record(
            aid, "promotion_pipeline",
            config={
                "n_folds": 4, "embargo_s": 60, "horizon": rep["horizon"],
                "looks": LOOKS_PER_ALPHA,
            },
            result={
                "oos_ic": rep["oos_ic"], "nw_tstat": rep["nw_tstat"],
                "verdict": rep["verdict"],
            },
            count=LOOKS_PER_ALPHA,
        )
        (REPORTS_DIR / f"{aid}.json").write_text(
            json.dumps(rep, indent=2, sort_keys=True) + "\n"
        )
        print(f"{aid}: ic={rep['oos_ic']} t={rep['nw_tstat']} "
              f"verdict={rep['verdict']} ({time.time() - t0:.1f}s)")

    # -- day-1 final fit -> serialized params + day-2 OOS backtest --------
    days = session_days(frames)
    if len(days) < 2:
        raise RuntimeError("bundled data must span >= 2 days")
    train, test = split_by_day(frames, days[1])
    models = fit_all(train)
    save_params(models, PARAMS_PATH)

    day2_bt: Dict[str, dict] = {}
    all_scores: Dict[str, Dict[str, dict]] = {"EQUITY": {}, "FX": {}}
    betas: Dict[str, Dict[str, float]] = {"EQUITY": {}, "FX": {}}
    for aid in ALPHA_IDS:
        model = models[aid]
        scores = model.score(test)
        res = backtester.run(scores=scores, frames=test, asset_class=model.asset_class)
        capital = _capital_usd(backtester, meta, scores)
        day2_bt[aid] = res.metrics(capital)
        all_scores[model.asset_class][aid] = scores
        betas[model.asset_class][aid] = float(model.params().get("beta", 0.0))

    # Ensembles: only alphas the pipeline did not REJECT (pinned, round-3).
    # An equal-weight basket containing REJECTed and hypothesis-contradicting
    # alphas is not a portfolio anyone would run.
    eligible = {aid: reports[aid]["verdict"] != "REJECT" for aid in ALPHA_IDS}
    ensembles: Dict[str, dict] = {}
    for ac in ("EQUITY", "FX"):
        members = [a for a in sorted(all_scores[ac]) if eligible.get(a)]
        try:
            ens = ensemble_scores(all_scores[ac], betas[ac], eligible=eligible)
        except ValueError:
            continue
        res = backtester.run(scores=ens, frames=test, asset_class=ac)
        capital = _capital_usd(backtester, meta, ens)
        ensembles[
            f"{ac} equal-weight, non-REJECT only ({len(members)} alphas: "
            f"{', '.join(members)})"
        ] = res.metrics(capital)

    ledger.save()
    _write_report(reports, ledger, day2_bt, ensembles, time.time() - t_start)

    n_promote = sum(1 for r in reports.values() if r["verdict"] == "PROMOTE")
    n_iterate = sum(1 for r in reports.values() if r["verdict"] == "ITERATE")
    n_reject = sum(1 for r in reports.values() if r["verdict"] == "REJECT")
    print(
        f"\nDone in {time.time() - t_start:.0f}s: {n_promote} PROMOTE / "
        f"{n_iterate} ITERATE / {n_reject} REJECT -> {REPORTS_DIR / 'REPORT.md'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
