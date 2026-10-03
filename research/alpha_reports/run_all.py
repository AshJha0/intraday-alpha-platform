#!/usr/bin/env python3
"""Full promotion pipeline for the 24 flagship alphas (spec §§11-13, 20).

Run from the repo root (or anywhere — paths are derived from this file):

    PYTHONPATH=python/src python3 research/alpha_reports/run_all.py

For every alpha EQ01..EQ12 / FX01..FX12 on the bundled 2-day synthetic
feature data (data/features/):

1. expanding walk-forward validation (4 folds, purged at the alpha's label
   horizon, 60 s embargo) — OOS IC / RankIC / the pooled-slope HAC t /
   hit rate / fold sign consistency;
2. automatic leakage tests (label-column guard, shift-by-one, frame
   truncation, and the recompute probe on the raw events);
3. decay curve across the 11 pinned horizons;
4. turnover + breakeven capacity;
5. cost x{0.5,1,2}, latency +{0,1,5} events, vol-regime split stress,
   per-fold diagnostics and the bootstrap interval of the pooled net P&L;
6. verdict per the pinned spec §20 gates (PROMOTE / ITERATE / REJECT), the
   PROMOTE t threshold derived from the multiple-testing ledger.

Method bundle: ``v2``, the defaults since v1.5.0 (``iap.validation.methods``).
``--methods legacy_v1`` computes the report under the rules up to v1.4.0
into a directory of the caller's choice (``--out-dir``; the committed
``research/alpha_reports`` and ``alpha_params.json`` are written by the
default bundle only) — it is how a v1.4.0 number is reproduced.

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

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

import numpy as np  # noqa: E402
from iap.alpha import ALPHA_IDS, build, fit_all, save_params  # noqa: E402
from iap.alpha.data import load_features, session_days, split_by_day  # noqa: E402
from iap.backtest import Backtester, CostModel  # noqa: E402
from iap.backtest.engine import ensemble_scores  # noqa: E402
from iap.experiment.tracker import data_version  # noqa: E402
from iap.validation import ExperimentLedger, validate_alpha  # noqa: E402
from iap.validation.leakage import RecomputeSources  # noqa: E402
from iap.validation.methods import METHODS, METHODS_V2, ResearchMethods, methods  # noqa: E402
from iap.validation.metrics import HORIZONS_NS  # noqa: E402
from iap.validation.validate import GATES  # noqa: E402

REPORTS_DIR = REPO / "research" / "alpha_reports"
LEDGER_PATH = REPO / "research" / "experiments.json"
#: Ledger ``kind`` of the entries this pipeline records.
LEDGER_KIND = "promotion_pipeline"
#: Pinned protocol of the pipeline (the spec defaults of ``iap.research``).
N_FOLDS = 4
EMBARGO_S = 60
#: Seed of the P&L bootstrap (``configs/execution/execution.json`` seed).
BOOTSTRAP_SEED = 20260829

#: Pinned research execution model (round-3): 1 s of event-time latency, a
#: 60 s decision age bound and session flattening.
RESEARCH_LATENCY_NS = 1_000_000_000
RESEARCH_MAX_DECISION_AGE_NS = 60_000_000_000
PARAMS_PATH = REPO / "configs" / "strategies" / "alpha_params.json"

#: Looks-at-the-data counted per alpha in one pipeline run. This is the
#: denominator of every multiple-testing correction in the research, so it
#: counts what the chain ACTUALLY evaluates:
#: ``iap.validation.validate.looks_per_validation`` itemises one validation
#: (83 at four folds under the default methods) and the day-2 backtest of
#: the serialized parameters is one more — 84, the same number
#: ``iap.research.LOOKS_PER_EXPERIMENT`` carries. Under ``legacy_v1`` the
#: chain evaluates less and debits 28, as it did up to v1.4.0.
LOOKS_PER_ALPHA = methods(METHODS_V2).looks(N_FOLDS)


def ledger_config(horizon: str, bundle: ResearchMethods) -> dict:
    """The ledger identity of one alpha's run: what was looked at. The
    method bundle is part of it since v1.5.0 — the same alpha on the same
    data under other rules is another look. A ``legacy_v1`` run keeps the
    three-key identity it had up to v1.4.0, so re-running it lands on the
    entry it reproduces instead of adding one."""
    config = {"n_folds": N_FOLDS, "embargo_s": EMBARGO_S, "horizon": horizon}
    if bundle.name != "legacy_v1":
        config["methods"] = bundle.name
    return config


#: pinned-horizon selection during alpha design scanned 9 horizons x 24
#: alphas once; recorded the first time the ledger is created
DESIGN_SCAN_COUNT = 216


def _load_meta() -> dict[int, dict]:
    cfg = json.loads((REPO / "configs" / "instruments" / "instruments.json").read_text())
    meta: dict[int, dict] = {}
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


def research_backtester(bundle: ResearchMethods, meta: dict[int, dict]) -> Backtester:
    """The pipeline's backtester under a method bundle. Round-3 pinned
    research execution model: latency in EVENT TIME, a bounded decision age
    (a decision made before a long quote gap no longer "fills" at whatever
    row comes next) and no overnight carry — under the position / fill / row
    rules and the cost model of the bundle."""
    cost_model = bundle.cost_model(
        CostModel.load(REPO / "configs" / "execution" / "execution.json")
    )
    return Backtester(
        cost_model,
        meta,
        bundle.backtest_config(
            latency_ns=RESEARCH_LATENCY_NS,
            max_decision_age_ns=RESEARCH_MAX_DECISION_AGE_NS,
            flatten_at_session_end=True,
        ),
    )


def max_participation() -> float:
    """``defaults.max_participation`` of ``configs/execution/execution.json``."""
    exec_cfg = json.loads((REPO / "configs" / "execution" / "execution.json").read_text())
    return float(exec_cfg["defaults"]["max_participation"])


def alpha_report(
    aid: str,
    frames: dict,
    backtester: Backtester,
    meta: dict[int, dict],
    bundle: ResearchMethods,
    *,
    recompute: RecomputeSources | None = None,
    ledger_t: float | None = None,
    gate_looks: int | None = None,
) -> dict:
    """One alpha's validation report under ``bundle`` — the document written
    to ``<ALPHA_ID>.json``. ``ledger_t`` / ``gate_looks`` are the PROMOTE t
    threshold and the look count it was derived at (ledger policy only);
    ``recompute`` supplies the raw events of the recompute probe."""
    probe = build(aid)
    return validate_alpha(
        lambda aid=aid: build(aid),
        frames,
        backtester,
        meta,
        max_participation(),
        n_folds=N_FOLDS,
        embargo_ns=EMBARGO_S * 1_000_000_000,
        ledger_t_threshold=ledger_t,
        ledger_looks=gate_looks,
        seed=BOOTSTRAP_SEED,
        recompute=(
            recompute.get("FX" if probe.asset_class == "FX" else "EQUITY")
            if bundle.recompute_probe and recompute is not None
            else None
        ),
        **bundle.validate_kwargs(),
    )


def _capital_usd(backtester: Backtester, meta: dict[int, dict], iids) -> float:
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
        v["crossed_frac"]
        for iid, v in inst.items()
        if int(iid) >= 100
        and isinstance(v.get("crossed_frac"), (int, float))
        and np.isfinite(v["crossed_frac"])
    )
    eq_gap, fx_gap = row_gap_ranges()
    if fx_crossed:
        fx_range = (
            f"{fx_crossed[0] * 100:.0f}-{fx_crossed[-1] * 100:.0f} % "
            f"of rows (mean {np.mean(fx_crossed) * 100:.0f} %)"
        )
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
        f"- **mean row gap** is the spacing of emissions. Equity rows are {eq_gap}",
        f'  apart, FX rows {fx_gap}: any "+1 event" latency claim means two',
        "  different things, which is why the latency stress is in event time.",
        "- **zero 1s / zero 1m** is the fraction of VALID labels that are",
        "  exactly 0. A 500 ms-5 s FX label is 89-99 % zeros: a REJECT at those",
        "  horizons is a sampling artefact of the emission cadence, not",
        "  evidence against the hypothesis.",
        "- **label max age** is the pinned freshness bound",
        "  `max(5 s, 2 x median quote gap)`: a label whose forward mid is",
        "  older than this is INVALID (API_FEATURES section 6). Equity rows",
        "  are sparse relative to the 5 s floor, so the bound removes a",
        "  material share of the equity labels at horizons of 10 s and more",
        "  (`valid 1m` above); 15 m labels anchored in the last 15 minutes of",
        "  a session do not exist at all.",
    ]
    return lines


def row_gap_ranges() -> tuple[str, str]:
    """``(equity, FX)`` mean-row-gap ranges as prose (``~3.1-3.3 s``), read
    from ``features_summary.json`` so the text cannot drift from the table."""
    path = REPO / "data" / "features" / "features_summary.json"
    if not path.is_file():
        return "an unknown gap", "an unknown gap"
    inst = json.loads(path.read_text()).get("instruments", {})

    def span(pick) -> str:
        gaps = sorted(
            v["mean_row_gap_ns"] / 1e9
            for iid, v in inst.items()
            if pick(int(iid)) and isinstance(v.get("mean_row_gap_ns"), (int, float))
        )
        if not gaps:
            return "an unknown gap"
        lo, hi = f"{gaps[0]:.1f}", f"{gaps[-1]:.1f}"
        return f"~{lo} s" if lo == hi else f"~{lo}-{hi} s"

    return span(lambda i: i < 100), span(lambda i: i >= 100)


#: PROMOTE gates in the order a report lists a failure (validate_alpha's
#: ``promote_gates`` keys).
GATE_ORDER = ("leakage", "ic", "significance", "fold_consistency", "folds", "hypothesis", "cost")


def failed_gates(report: dict) -> str:
    """The PROMOTE gates a report fails, comma-separated (``-`` for none)."""
    failed = [g for g in GATE_ORDER if not report["promote_gates"][g]]
    return ", ".join(failed) if failed else "-"


def _write_report(
    reports: dict[str, dict],
    ledger: ExperimentLedger,
    day2_bt: dict[str, dict],
    ensembles: dict[str, dict],
    runtime_s: float,
    bundle: ResearchMethods,
    gate_looks: int | None,
    looks_added: int,
    reports_dir: Path,
) -> None:
    lines = []
    a = lines.append
    gates = next(iter(reports.values()))["gates"]
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
    a(f"- PROMOTE: leakage pass AND gate IC >= {GATES['min_oos_ic']}")
    a(
        f"  AND gate t >= {gates['min_nw_tstat']:.3f} AND fold sign consistency >= "
        f"{GATES['min_fold_sign_consistency']} AND >= "
        f"{GATES['min_nondegenerate_folds']} non-degenerate folds AND hypothesis "
        "confirmed AND net P&L > 0 at 1x costs."
    )
    a(
        f"- ITERATE: leakage pass AND gate IC >= {GATES['iterate_min_ic']} AND "
        f"gate t >= {GATES['iterate_min_tstat']}."
    )
    a("- REJECT: otherwise (always, on leakage failure).")
    a("")
    a(f"## Methods (`{bundle.name}`, iap.validation.methods)")
    a("")
    if bundle.name == METHODS_V2:
        a("The defaults since v1.5.0; each has a named legacy rule")
        a("(`run_all.py --methods legacy_v1` reproduces the v1.4.0 report).")
    else:
        a("The LEGACY rules, the defaults up to v1.4.0. This report is a")
        a("reproduction, not the committed promotion report.")
    a("")
    rows_text = (
        "a valid label, or one invalid for BLACKOUT alone scored at its realised reopen return"
        if bundle.ic_rows == "blackout_reopen"
        else "valid labels only"
    )
    a(f"- **Rows every IC scores:** {rows_text}.")
    a("- **Gate IC:** the pooled IC of the standardized signal on those rows, uncrossed book only.")
    t_text = (
        "the HAC t of the pooled slope (the significance of the gate IC itself)"
        if bundle.significance == "pooled_slope"
        else "the Newey-West t of the mean of within-bucket ICs"
    )
    a(f"- **Gate t:** {t_text}; lag count `L` follows the horizon.")
    if bundle.tstat_threshold == "ledger":
        a(
            f"- **PROMOTE t threshold:** {gates['min_nw_tstat']:.3f} = the Bonferroni |t| at the "
            f"run's gate look count, {gate_looks:,} (the ledger before this run plus the "
            f"{looks_added:,} this run adds; never below {GATES['min_nw_tstat']})."
        )
    else:
        a(f"- **PROMOTE t threshold:** fixed {GATES['min_nw_tstat']}.")
    if bundle.position_policy == "cost_aware":
        a(
            "- **Backtest:** cost-aware positions (enter only when |expected return| exceeds "
            "the round-trip spread + fee, hold for the label horizon or until an opposite "
            "signal that clears the same bar, renew at half of it), fills capped at the "
            "displayed L1 size, only the rows the IC scores are traded, square-root impact. "
            "An alpha whose forecast never clears its costs makes NO trade: its net P&L is "
            "0, which does not pass `net P&L > 0`."
        )
    else:
        a(
            "- **Backtest:** `sign(expected return)` re-decided on every row, fills of any "
            "size at the touch, every row traded, linear impact."
        )
    cap_text = (
        "edge breakeven — the size at which the realised gross edge per round trip of the "
        "1x backtest equals its cost, capped at the participation line; 0 for an alpha that "
        "does not trade or loses before costs"
        if bundle.capacity == "breakeven"
        else "participation proxy (max participation x ADV x price)"
    )
    a(f"- **Capacity:** {cap_text}.")
    a("")
    a("## Which rows are trustworthy (read this before the tables)")
    a("")
    for line in data_quality_lines():
        a(line)
    a("")
    a("## Master table (walk-forward OOS, expanding purged+embargoed folds)")
    a("")
    a("Folds are quantiles of the ROW INDEX, not of the wall span: the equity")
    a("session is 6.5 h of each 24 h day, so equal wall segments leave folds")
    a("EMPTY or badly unequal while a report still claims 4 folds. A fold")
    a("with fewer than 32 usable pairs is DEGENERATE and counts as a failed")
    a("fold in `folds+`; PROMOTE needs >= 3 non-degenerate folds.")
    a("")
    a("`IC` is the pooled OOS IC on the standardized signal; `gate IC` is the")
    a("same IC restricted to rows whose consolidated book was NOT crossed")
    a("(a crossed book means a stale venue quote whose mid mechanically")
    a("reverts). **The gates read `gate IC` and `gate t`**; `t other` is the")
    a("t-statistic the gate does not read (within-bucket under the default")
    a("methods, pooled-slope under the legacy ones). `fails` lists the PROMOTE")
    a("gates the alpha does not pass.")
    a("")
    a(
        "| alpha | horizon | IC | gate IC | IC crs | crs% | gate t | t other | L | hit "
        "| folds+ | deg | hyp | leak | trades 1x | net P&L 1x | flips/h | verdict | fails |"
    )
    a(
        "|-------|---------|----|---------|--------|------|--------|---------|---|-----"
        "|--------|-----|-----|------|-----------|-----------|---------|---------|-------|"
    )
    other_t = (
        "nw_tstat_uncrossed"
        if bundle.significance == "pooled_slope"
        else ("nw_tstat_pooled_uncrossed")
    )
    for aid in ALPHA_IDS:
        r = reports[aid]
        a(
            f"| {aid} | {r['horizon']} | {_fmt(r['oos_ic'])} | "
            f"{_fmt(r.get('gate_ic'))} | "
            f"{_fmt(r.get('oos_ic_crossed'))} | "
            f"{_fmt(r.get('crossed_frac'), '.3f')} | "
            f"{_fmt(r.get('gate_tstat'), '.2f')} | "
            f"{_fmt(r.get(other_t), '.2f')} | "
            f"{r.get('nw_lags', '-')} | "
            f"{_fmt(r['oos_hit_rate'], '.3f')} | "
            f"{_fmt(r['fold_sign_consistency'], '.2f')} | "
            f"{r.get('n_degenerate_folds', '-')} | "
            f"{'yes' if r['hypothesis_confirmed'] else 'no'} | "
            f"{'pass' if r['leakage']['passed'] else 'FAIL'} | "
            f"{r['trade_count_1x_cost']} | "
            f"{_fmt(r['net_pnl_1x_cost'], '+.0f')} | "
            f"{_fmt(r['turnover_flips_per_hour'], '.0f')} | **{r['verdict']}** | "
            f"{failed_gates(r)} |"
        )
    n_promote = sum(1 for r in reports.values() if r["verdict"] == "PROMOTE")
    n_iterate = sum(1 for r in reports.values() if r["verdict"] == "ITERATE")
    n_reject = sum(1 for r in reports.values() if r["verdict"] == "REJECT")
    a("")
    a(
        f"**Verdicts: {n_promote} PROMOTE / {n_iterate} ITERATE / "
        f"{n_reject} REJECT** (of {len(reports)})."
    )
    n_untraded = sum(1 for r in reports.values() if r["trade_count_1x_cost"] == 0)
    n_losing = sum(
        1 for r in reports.values() if r["trade_count_1x_cost"] > 0 and r["net_pnl_1x_cost"] <= 0
    )
    n_winning = sum(1 for r in reports.values() if r["net_pnl_1x_cost"] > 0)
    a("")
    a(
        f"At 1x costs on the last fold: {n_untraded} alphas make no trade, {n_losing} trade "
        f"and lose, {n_winning} end above zero."
    )
    a("")
    a("## Headline ICs beside the gate IC")
    a("")
    a("The pooled IC lets the most volatile instrument dominate and reads a")
    a("difference in mean return between instruments as correlation. `vol-scaled`")
    a("puts every instrument on a unit scale before pooling; `instr mean` averages")
    a("the per-instrument ICs; both on the rows the gate reads. `valid-only` is the")
    a("gate IC with every invalid-label row dropped (the legacy row policy), and")
    a("`reopen rows` the number of BLACKOUT rows the default policy scores at")
    a("their reopen return. `scale ok` = the vol-scaled IC has the gate IC's sign.")
    a("None of these is a gate input (iap.validation.validate, module docs).")
    a("")
    a("| alpha | gate IC | vol-scaled | instr mean | valid-only | reopen rows | scale ok |")
    a("|-------|---------|------------|------------|------------|-------------|----------|")
    for aid in ALPHA_IDS:
        r = reports[aid]
        scale = r.get("ic_scale_consistent")
        unc = r.get("oos_ic_uncrossed") is not None
        a(
            f"| {aid} | {_fmt(r.get('gate_ic'))} | "
            f"{_fmt(r.get('oos_ic_vol_scaled_uncrossed' if unc else 'oos_ic_vol_scaled'))} | "
            f"{_fmt(r.get('oos_ic_instrument_mean_uncrossed' if unc else 'oos_ic_instrument_mean'))}"
            f" | {_fmt(r.get('gate_ic_valid_only'))} | "
            f"{r.get('n_blackout_rows_scored') if r.get('n_blackout_rows_scored') is not None else '-'}"
            f" | {'-' if scale is None else ('yes' if scale else 'NO')} |"
        )
    if bundle.fold_diagnostics:
        a("")
        a("## Every fold: cost survival, pooled net P&L, capacity")
        a("")
        a("`folds > 0` counts the walk-forward folds whose net P&L at 1x costs is")
        a("above zero; `pooled net` sums the 1x net P&L of all four test segments")
        a("and the interval is its 95 % stationary-bootstrap interval over 1-minute")
        a("bars (1 000 resamples, seeded). Report-only: the cost gate reads the last")
        a("fold, and no gate reads the interval (iap.validation.diagnostics).")
        a("`capacity` is the edge-breakeven capacity summed over the universe.")
        a("")
        a("| alpha | folds > 0 | pooled net 1x | CI low | CI high | capacity USD |")
        a("|-------|-----------|---------------|--------|---------|--------------|")
        for aid in ALPHA_IDS:
            r = reports[aid]
            boot = r["net_pnl_bootstrap"]
            a(
                f"| {aid} | {r['n_folds_survive_1x_cost']} of {r['n_folds_run']} | "
                f"{_fmt(r['net_pnl_1x_pooled'], '+.0f')} | "
                f"{_fmt(boot['ci_low'], '+.0f')} | {_fmt(boot['ci_high'], '+.0f')} | "
                f"{_fmt(sum(r['capacity_usd_by_instrument'].values()), '.0f')} |"
            )
    a("")
    a("## Decay curves (OOS IC by horizon, last fold)")
    a("")
    horizons = ["10ms", "50ms", "100ms", "500ms", "1s", "5s", "10s", "30s", "1m", "5m", "15m"]
    a("| alpha | " + " | ".join(horizons) + " |")
    a("|-------|" + "|".join(["------"] * len(horizons)) + "|")
    for aid in ALPHA_IDS:
        d = reports[aid]["decay_ic_by_horizon"]
        a(f"| {aid} | " + " | ".join(_fmt(d.get(h), "+.3f") for h in horizons) + " |")
    a("")
    a("## Cost and latency stress (last fold, net P&L)")
    a("")
    eq_gap, fx_gap = row_gap_ranges()
    a(f"Latency is stressed in EVENT TIME (a row is {eq_gap} on equities and")
    a(f'{fx_gap} on FX, so an "+N events" grid is not a latency budget). The')
    a("event grid is kept as the last three columns for continuity.")
    a("")
    a(
        "| alpha | x0.5 cost | x1 cost | x2 cost | 100ms P&L | 500ms | 1s "
        "| 5s | +0ev IC | +1ev IC | +5ev IC |"
    )
    a(
        "|-------|-----------|---------|---------|-----------|-------|----"
        "|----|---------|---------|---------|"
    )
    for aid in ALPHA_IDS:
        st = reports[aid]["stress"]
        lt = st.get("latency_time", {})
        a(
            f"| {aid} | {_fmt(st['cost']['x0.5']['total_pnl'], '+.0f')} | "
            f"{_fmt(st['cost']['x1']['total_pnl'], '+.0f')} | "
            f"{_fmt(st['cost']['x2']['total_pnl'], '+.0f')} | "
            + " | ".join(
                _fmt(lt.get(k, {}).get("total_pnl"), "+.0f") for k in ("100ms", "500ms", "1s", "5s")
            )
            + " | "
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
        a(f"| {aid} | {_fmt(rg['ic_high_vol'], '+.4f')} | {_fmt(rg['ic_low_vol'], '+.4f')} |")
    a("")
    a("## Day-2 out-of-sample backtest (day-1-fitted params, 1x costs)")
    a("")
    a(
        "These are the exact parameters serialized to "
        "`configs/strategies/alpha_params.json` (the port contract)."
    )
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
    a(
        "Sharpe scaling: 1-minute event-time bars, annualized by "
        "sqrt(252 * session_hours * 60) (6.5h equities, 21h FX); assumes "
        "independent bar P&L — a research yardstick, not a production claim."
    )
    a("")
    a("## Multiple testing")
    a("")
    looks_each = bundle.looks(N_FOLDS)
    if gate_looks is not None:
        a(
            f"This run was judged at **{gate_looks:,} looks** — the multiple-testing ledger "
            f"as it stood before the run plus the {looks_added:,} the run adds. Bonferroni "
            f"per-test p-threshold {0.05 / gate_looks:.2e}, |t| >= "
            f"{ledger.bonferroni_t_threshold_at(gate_looks):.3f}: that is the PROMOTE t "
            "threshold above."
        )
        a("")
        a("The count is this run's, fixed when it ran. The ledger keeps growing as the")
        a("later pipelines (runner experiments, the ML and adaptive reports) record")
        a("theirs: its current total is in `research/experiments.json`, and a run")
        a("recorded here is not re-judged when it grows (iap.validation.ledger,")
        a('"Gate look count"). A rerun of this script reproduces this report: it is')
        a("judged at the count recorded on its ledger entries.")
    else:
        a(ledger.note())
    a("")
    a("Every walk-forward evaluation, decay-curve horizon, stress variant and")
    a(
        "backtest in this run is counted in `research/experiments.json` "
        f"({looks_each} per alpha per run, itemised in "
        "`iap.validation.validate.looks_per_validation`), plus the one-time design "
        f"horizon scan ({DESIGN_SCAN_COUNT}). Experiments are "
        "DE-DUPLICATED by (alpha, kind, configuration, dataset): re-running this "
        "script does not inflate the denominator, so the selection-adjusted "
        "threshold is a property of the research design, not of how often the "
        "script ran."
    )
    a("")
    yardstick = (
        (2.0 * np.log(gate_looks)) ** 0.5
        if gate_looks is not None
        else ledger.expected_max_null_t()
    )
    a(
        f"Alphas whose |gate t| is below the selection yardstick {yardstick:.2f} — the "
        "expected largest |t| under the global null over that many looks — are "
        "consistent with pure selection, whatever their verdict:"
    )
    below = [
        aid
        for aid in ALPHA_IDS
        if (
            reports[aid].get("gate_tstat") is not None
            and abs(reports[aid]["gate_tstat"]) < yardstick
        )
    ]
    a("")
    a("- " + (", ".join(below) if below else "none"))
    a("")
    (reports_dir / "REPORT.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--methods",
        choices=sorted(METHODS),
        default=METHODS_V2,
        help="research method bundle (default v2; legacy_v1 = the rules up to v1.4.0)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where the reports go (required with --methods legacy_v1; "
        "default research/alpha_reports)",
    )
    ap.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="multiple-testing ledger (default research/experiments.json; with "
        "--out-dir, <out-dir>/experiments.json — a run outside the committed "
        "reports keeps its own ledger unless told otherwise)",
    )
    args = ap.parse_args(argv)
    bundle = methods(args.methods)
    committed = bundle.name == METHODS_V2 and args.out_dir is None
    if bundle.name != METHODS_V2 and args.out_dir is None:
        raise SystemExit(
            "--methods legacy_v1 needs --out-dir: research/alpha_reports and "
            "alpha_params.json are written by the default bundle only"
        )
    reports_dir = REPORTS_DIR if args.out_dir is None else args.out_dir
    if args.ledger is None:
        args.ledger = LEDGER_PATH if args.out_dir is None else reports_dir / "experiments.json"

    t_start = time.time()
    reports_dir.mkdir(parents=True, exist_ok=True)
    meta = _load_meta()
    frames = load_features(REPO / "data" / "features")
    backtester = research_backtester(bundle, meta)
    recompute = RecomputeSources(REPO / "data" / "normalized", REPO / "configs")

    # Dataset-scoped (iap.validation.ledger): the same configuration on a
    # regenerated dataset is a new look, recorded beside the old one.
    ledger = ExperimentLedger(args.ledger, dataset_version=data_version(REPO))
    if ledger.total_experiments == 0:
        ledger.record(
            "ALL",
            "design_horizon_scan",
            config={"horizons_scanned": 9, "alphas": 24},
            count=DESIGN_SCAN_COUNT,
        )

    # The whole pass is declared before the first alpha is evaluated, so all
    # 24 are judged at one look count whatever the loop order
    # (iap.validation.ledger, "Gate look count").
    looks_each = bundle.looks(N_FOLDS)
    identities = [
        (aid, LEDGER_KIND, ledger_config(build(aid).horizon, bundle), looks_each)
        for aid in ALPHA_IDS
    ]
    batch_total = ledger.batch_total(identities)
    # What one pass adds to the ledger the first time it runs (a rerun adds
    # nothing and is judged at the recorded count, so the report it renders
    # quotes the same two numbers).
    looks_added = looks_each * len(identities)
    gate_looks_seen: set[int] = set()

    reports: dict[str, dict] = {}
    for aid, kind, config, count in identities:
        t0 = time.time()
        gate_looks: int | None = None
        ledger_t: float | None = None
        if bundle.tstat_threshold == "ledger":
            gate_looks = ledger.gate_looks_for(aid, kind, config, batch_total)
            ledger_t = ledger.bonferroni_t_threshold_at(gate_looks)
            gate_looks_seen.add(gate_looks)
        rep = alpha_report(
            aid,
            frames,
            backtester,
            meta,
            bundle,
            recompute=recompute,
            ledger_t=ledger_t,
            gate_looks=gate_looks,
        )
        reports[aid] = rep
        ledger.record(
            aid,
            kind,
            # The config is the experiment's IDENTITY — what was looked at.
            # How many looks that costs is the recording's size and travels
            # in `count`, not here: carrying `looks` in the identity meant
            # that correcting the look accounting (21 -> 28) forked every
            # alpha into a second "configuration", double-counting the
            # multiple-testing denominator the ledger exists to keep honest.
            config=config,
            result={
                "oos_ic": rep["oos_ic"],
                "nw_tstat": rep["nw_tstat"],
                "gate_ic": rep["gate_ic"],
                "gate_tstat": rep["gate_tstat"],
                "verdict": rep["verdict"],
            },
            count=count,
            gate_looks=gate_looks,
        )
        (reports_dir / f"{aid}.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n")
        print(
            f"{aid}: gate_ic={rep['gate_ic']} gate_t={rep['gate_tstat']} "
            f"verdict={rep['verdict']} ({time.time() - t0:.1f}s)"
        )
    if len(gate_looks_seen) > 1:
        raise RuntimeError(
            f"the pass was judged at several look counts {sorted(gate_looks_seen)}: the "
            "ledger holds some of its alphas from an earlier run and not others — start "
            "from the ledger as committed"
        )
    run_gate_looks = next(iter(gate_looks_seen)) if gate_looks_seen else None

    # -- day-1 final fit -> serialized params + day-2 OOS backtest --------
    days = session_days(frames)
    if len(days) < 2:
        raise RuntimeError("bundled data must span >= 2 days")
    train, test = split_by_day(frames, days[1])
    models = fit_all(train)
    if committed:
        # The serialized parameters are the port contract: one file, written
        # by the default pipeline (the fit itself does not depend on the
        # method bundle, so a legacy reproduction would write the same file).
        save_params(models, PARAMS_PATH)

    day2_bt: dict[str, dict] = {}
    all_scores: dict[str, dict[str, dict]] = {"EQUITY": {}, "FX": {}}
    betas: dict[str, dict[str, float]] = {"EQUITY": {}, "FX": {}}
    for aid in ALPHA_IDS:
        model = models[aid]
        scores = model.score(test)
        res = backtester.for_horizon(model.horizon).run(
            scores=scores, frames=test, asset_class=model.asset_class
        )
        capital = _capital_usd(backtester, meta, scores)
        day2_bt[aid] = res.metrics(capital)
        all_scores[model.asset_class][aid] = scores
        betas[model.asset_class][aid] = float(model.params().get("beta", 0.0))

    # Ensembles: only alphas the pipeline did not REJECT (pinned, round-3).
    # An equal-weight basket containing REJECTed and hypothesis-contradicting
    # alphas is not a portfolio anyone would run.
    eligible = {aid: reports[aid]["verdict"] != "REJECT" for aid in ALPHA_IDS}
    ensembles: dict[str, dict] = {}
    for ac in ("EQUITY", "FX"):
        members = [a for a in sorted(all_scores[ac]) if eligible.get(a)]
        try:
            ens = ensemble_scores(all_scores[ac], betas[ac], eligible=eligible)
        except ValueError:
            continue
        # A basket has no horizon of its own; it is traded at the SHORTEST
        # horizon of its members (the blend is stale once its fastest
        # component is).
        horizon = min((models[a].horizon for a in members), key=lambda h: HORIZONS_NS[h])
        res = backtester.for_horizon(horizon).run(scores=ens, frames=test, asset_class=ac)
        capital = _capital_usd(backtester, meta, ens)
        ensembles[
            f"{ac} equal-weight, non-REJECT only ({len(members)} alphas: "
            f"{', '.join(members)}; traded at {horizon})"
        ] = res.metrics(capital)

    ledger.save()
    _write_report(
        reports,
        ledger,
        day2_bt,
        ensembles,
        time.time() - t_start,
        bundle,
        run_gate_looks,
        looks_added,
        reports_dir,
    )

    n_promote = sum(1 for r in reports.values() if r["verdict"] == "PROMOTE")
    n_iterate = sum(1 for r in reports.values() if r["verdict"] == "ITERATE")
    n_reject = sum(1 for r in reports.values() if r["verdict"] == "REJECT")
    print(
        f"\nDone in {time.time() - t_start:.0f}s: {n_promote} PROMOTE / "
        f"{n_iterate} ITERATE / {n_reject} REJECT -> {reports_dir / 'REPORT.md'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
