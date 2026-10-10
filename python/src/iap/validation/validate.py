"""Per-alpha validation orchestrator (spec §13 + §20 promotion gates).

``validate_alpha`` runs the full evidence chain for one alpha on one data
set and returns a JSON-serializable report:

1. expanding walk-forward (purged + embargoed) — per-fold OOS IC/RankIC/
   hit rate, the pooled IC and its HAC t-statistic;
2. leakage tests (label-column guard, shift-by-one, frame truncation and —
   when the raw events are at hand — the recompute probe);
3. decay curve across the 11 pinned horizons (fit at the pinned horizon,
   scored OOS, correlated against every horizon's label);
4. turnover and capacity;
5. cost / latency / regime stress (via the research backtester), per-fold
   diagnostics and a bootstrap interval for the pooled net P&L;
6. verdict per the pinned §20 gates (see GATES below).

Pinned promotion gates (spec §20 steps 4-7 distilled; thresholds pinned
here and echoed in every report):

- PROMOTE requires ALL of:
    leakage passed; gate IC >= 0.010; gate t >= the significance threshold;
    fold sign consistency >= 0.70; at least 3 NON-DEGENERATE folds;
    hypothesis_confirmed (fitted beta agrees with the stated rationale);
    cost survival (net backtest P&L > 0 at 1.0x costs on the last fold).
- ITERATE: leakage passed, gate IC >= 0.005 and gate t >= 1.5 (evidence
  of signal, fails at least one PROMOTE gate).
- REJECT: everything else, and ALWAYS when leakage fails.

Round-3 honesty rules baked into the report (all pinned):

- **Degenerate folds count as failures.**  A fold with fewer than
  ``MIN_TEST_PAIRS`` usable score/label pairs is reported
  (``degenerate: true``) and counted as a *failed* fold in
  ``fold_sign_consistency`` instead of silently vanishing from the mean.
  ``n_nondegenerate_folds`` is reported and gates PROMOTE.
- **Every per-fold and every stress statistic is scored on z** (round-4).
  ``beta_k`` is refit free-signed per fold, so ``ic(er, y)`` equals
  ``sign(beta_k) * ic(z, y)``: an alpha that is backwards in every fold used
  to report ``fold_sign_consistency = 1.00`` and a positive high-vol regime
  IC beside a NEGATIVE gate IC.  Fold IC / RankIC / hit rate, the regime
  split and the decay curve all read ``z`` now; ``ic_er`` is kept per fold as
  a clearly-named diagnostic.
- **The pooled IC is computed on z, not on expected_return.**  Folds fit
  different betas, so concatenating ``expected_return`` weights each fold by
  |beta_k| and a sign flip between folds can cancel the IC.  The gate uses
  the pooled standardized signal (``expected_return / beta_k`` per fold,
  falling back to the raw er when beta is 0); the er-pooled IC is kept as
  ``oos_ic_pooled_er`` for continuity.
- **Crossed-book conditioning.**  A consolidated FX book is CROSSED
  (``spread_ticks_v1 < 0``) whenever one LP's quote is stale; the mid then
  reverts mechanically when that LP refreshes, and a vol-scaled momentum
  signal is paid for measuring exactly that artefact.  Every report splits
  the OOS IC into ``oos_ic_uncrossed`` / ``oos_ic_crossed`` and records
  ``crossed_frac``.  **The PROMOTE gate uses the UNCROSSED IC.**
- **Newey-West lag count** follows the horizon (``metrics.nw_lags``) and is
  reported as ``nw_lags``.

**Method defaults (v1.5.0).**  The corrected methods that v1.3.0 added as
opt-in are the defaults; each legacy rule stays selectable by name and
``iap.validation.methods`` bundles them (``"v2"`` / ``"legacy_v1"``).

- **``significance``** — which t-statistic the gate reads.
  ``"pooled_slope"`` (default): the HAC t of the POOLED slope
  (:func:`iap.validation.metrics.pooled_slope_hac_tstat`), uncrossed rows
  when that is finite.  The gate IC is a pooled correlation, and this is the
  significance of exactly that number.  ``"within_bucket"`` (legacy): the
  Newey-West t of the mean of WITHIN-bucket ICs, which demeans score and
  label inside each 5-minute bucket and therefore discards whatever signal
  lives between buckets.  Both are always reported (``nw_tstat*`` and
  ``nw_tstat_pooled*``); ``gate_tstat`` is the one the gate read and
  ``methods.significance`` names it.
- **``tstat_threshold``** — the PROMOTE threshold of that t.  ``"ledger"``
  (default): ``max(3.0, ledger_t_threshold)``, where the caller passes the
  multiple-testing ledger's Bonferroni |t| at the run's gate look count
  (``iap.validation.ledger``, "Gate look count") and the count itself as
  ``ledger_looks``.  It can only tighten the gate, and it has no silent
  fall-back: the default policy without a threshold is an error.
  ``"fixed"`` (legacy): 3.0 whatever was tried.  The ITERATE threshold
  (1.5) is not ledger-derived: ITERATE means "evidence worth another look",
  not a multiple-testing claim.
- **``ic_rows``** — which rows every IC of the chain scores
  (:mod:`iap.labels.frames`).  ``"blackout_reopen"`` (default): a row whose
  label is invalid for BLACKOUT alone is scored at its realised reopen
  return instead of being dropped — dropping it is a selection on the
  outcome.  ``"valid_only"`` (legacy): valid labels only.  The pooled IC,
  the fold ICs, both t-statistics, rank IC, hit rate, the decay curve, the
  regime split, the row-latency IC and the leakage shift test all read the
  same rows; the IC under the OTHER policy is reported beside the gate IC
  (``oos_ic_valid_only`` / ``gate_ic_valid_only``,
  ``oos_ic_blackout_reopen`` / ``gate_ic_blackout_reopen``) with the number
  of rescued rows (``n_blackout_rows_scored``), so the size of the
  selection is on the page.
- **Which IC the gate reads** — the POOLED IC of the standardized signal on
  uncrossed rows (``gate_ic``), as before; what changed is the row policy
  above and the t that goes with it.  The scale-free ICs
  (``oos_ic_vol_scaled[_uncrossed]``, ``oos_ic_instrument_mean[_uncrossed]``,
  ``oos_ic_by_instrument``) are HEADLINE REPORT fields beside it, not gate
  inputs: the gate t is the significance of the pooled slope, and the
  lifecycle's validation and paper gates compare the research IC with a
  realised pooled IC (``holdout_ic_tracks_research``,
  ``paper_ic_tracking``), so a gate on a per-instrument-scaled IC would be
  tested by one statistic and tracked against a third.  A report whose
  vol-scaled IC disagrees in sign with the gate IC on the same rows says so
  (``ic_scale_consistent``).
- **``stress_version``** — the row-latency stress grid
  (:mod:`iap.validation.stress`): 2 (default) carries the whole base
  config, 1 (legacy) is the historic four-field rebuild.
- **``capacity``** — ``"breakeven"`` (default): per instrument, the size at
  which the edge per round trip equals its cost
  (:func:`iap.validation.metrics.capacity_breakeven` under the cost model
  in force), capped at the participation proxy; the edge is the REALISED
  gross return per round trip of the 1x backtest on the last fold, so an
  alpha that does not trade, or whose trades lose before costs, has
  capacity 0.  ``"participation"`` (legacy): ``max_participation x ADV x
  price``, the same for an alpha with a 5 bp edge and one with none.
- **``fold_diagnostics``** — ``True`` (default): cost survival, decay and
  regime for EVERY fold, the pooled net P&L and its stationary-bootstrap
  interval are fields of the report (:mod:`iap.validation.diagnostics`;
  report-only — no gate reads them, see that module for why).  ``False``
  (legacy) leaves them out.
- **``recompute``** — the raw-event source of the recompute leakage probe
  (:class:`iap.validation.leakage.RecomputeSource`); part of the standard
  run whenever it is given.  Without one the probe does not run and the
  report says so (``leakage.recompute_ok`` is ``null``).
- The research backtester's own defaults (cost-aware positions, fills
  capped at displayed size, invalid-label rows blocked, square-root impact)
  live in :mod:`iap.backtest`; ``validate_alpha`` sets the label horizon on
  the backtester it is handed and reports the rules in force under
  ``methods``.

**Research-validity options (v1.9, R1-R6; all opt-in, the ``"v3"``
bundle of** :mod:`iap.validation.methods` **turns them on).**  The v2
defaults are unchanged, so every published number and golden still holds.

- **``gate_ic_source``** (R3) - ``"pooled"`` (default) gates on the pooled
  IC as above; ``"instrument_mean"`` gates on the EQUAL-WEIGHT mean of the
  per-instrument ICs on the same rows (on real data QQQ is about half of
  the rows, so the pooled IC is largely QQQ's).  The pooled IC stays in the
  report as ``oos_ic`` / ``gate_ic_pooled``; ``gate_ic_source`` names the
  one the gate read.  The gate t is unchanged (the pooled-slope HAC t).
- **``split_mode``** (R2) - the fold layout of
  :class:`iap.validation.splits.WalkForwardSplitter`: ``"row_mass"``
  (default), ``"day_aligned"`` or ``"leave_one_day_out"``.
- **``validity_diagnostics``** (R1/R2/R6) - adds a ``validity`` block: the
  session days scored and their event tags, the gate statistics with the
  FOMC and holiday-thin days excluded (``ex_event``), the pooled-slope HAC
  t with no lag product across a day boundary, the day-clustered t and
  the day-block bootstrap t of the pooled gate-row IC.  Four more looks
  (:data:`VALIDITY_LOOKS`).  Report-only: no gate reads them.
- **``book_scope``** (R4) - ``"consolidated"`` (default: the synthetic
  multi-venue books, where a crossed book is a stale other-venue quote) or
  ``"single_venue"`` (ingested ITCH/LOBSTER: one Nasdaq book).  A single
  venue's own book cannot be crossed by another venue's stale quote, so the
  crossed/uncrossed split is switched off (``crossed_frac`` and
  ``oos_ic_crossed`` are ``null``, every row counts as uncrossed) and the
  report says ``price_reference: "nasdaq_bbo"`` - the label mid is the
  Nasdaq best bid/offer, not the NBBO.
- **``dataset_version``** (R6) - the content hash(es) of the data the
  report was computed on, echoed as ``dataset_versions`` so a report can
  never be read against another dataset.

**Overfitting statistics (v1.12, R7; opt-in, the ``"v4"`` bundle turns
them on).**  Report-only: no gate reads them, so no verdict moves.

- **``cpcv``** - adds a ``cpcv`` block: the model is refit on every one of
  the ``C(N, k)`` combinatorial purged splits
  (:mod:`iap.validation.cpcv`, ``cpcv_groups`` = N, ``cpcv_test_groups`` =
  k; day-aligned groups when there are >= N session days), the
  out-of-sample standardized signal is reassembled into
  ``phi = C(N-1, k-1)`` backtest paths and the pooled IC of each path is
  reported with its spread.  :data:`CPCV_LOOKS` more looks.
- **``deflated_sharpe``** - adds a ``deflated_sharpe`` block on the
  per-session-day net P&L of the 1x walk-forward backtests (needs
  ``fold_diagnostics``): Sharpe, PSR, minimum track record length and the
  deflated Sharpe ratio at ``deflated_sharpe_trials`` independent trials
  (:mod:`iap.validation.deflated`, "Which N": the pipelines pass the
  ledger's distinct configurations), plus ``dsr_at_looks`` at
  ``ledger_looks``.  :data:`DSR_LOOKS` more look.

**Looks.**  :func:`looks_per_validation` itemises what one call evaluates;
the ledger debits exactly that.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from iap.backtest.engine import Backtester, BacktestResult
from iap.labels.frames import DEFAULT_IC_ROWS, IC_ROWS, LEGACY_IC_ROWS, scored_labels
from iap.validation.cpcv import (
    DEFAULT_CPCV_GROUPS,
    DEFAULT_CPCV_TEST_GROUPS,
    CombinatorialPurgedSplitter,
)
from iap.validation.deflated import daily_pnl_from_bars, deflated_sharpe_block
from iap.validation.diagnostics import (
    BOOTSTRAP_RESAMPLES,
    bar_series,
    fold_row,
    stationary_bootstrap_ci,
)
from iap.validation.leakage import LeakageTester, RecomputeSource
from iap.validation.metrics import (
    HORIZON_ORDER,
    HORIZONS_NS,
    bucket_ics_with_counts,
    bucket_size_summary,
    capacity_breakeven,
    capacity_proxy_usd,
    day_block_bootstrap_tstat,
    day_cluster_tstat,
    decay_curve,
    hit_rate,
    ic,
    instrument_ics,
    newey_west_tstat,
    nw_lags,
    pooled_slope_hac_tstat,
    rank_ic,
    signal_turnover_detail,
)
from iap.validation.sessions import (
    NS_DAY,
    day_index_to_iso,
    day_tags,
    event_day_mask,
    session_days,
)
from iap.validation.splits import (
    DEFAULT_SPLIT_MODE,
    MIN_NONDEGENERATE_FOLDS,
    MIN_TEST_PAIRS,
    SPLIT_MODES,
    WalkForwardSplitter,
)
from iap.validation.stress import (
    COST_MULTIPLIERS,
    DEFAULT_STRESS_VERSION,
    LATENCY_SHIFTS,
    LATENCY_TIMES_NS,
    cost_stress_results,
    latency_stress,
    latency_stress_time,
    regime_split,
)

GATES = {
    "min_oos_ic": 0.010,
    "min_nw_tstat": 3.0,
    "min_fold_sign_consistency": 0.70,
    "min_nondegenerate_folds": MIN_NONDEGENERATE_FOLDS,
    "min_test_pairs": MIN_TEST_PAIRS,
    "iterate_min_ic": 0.005,
    "iterate_min_tstat": 1.5,
}

#: PROMOTE t-stat threshold policies (module docs): default, then legacy.
TSTAT_THRESHOLD_POLICIES = ("ledger", "fixed")
DEFAULT_TSTAT_THRESHOLD = "ledger"
LEGACY_TSTAT_THRESHOLD = "fixed"

#: Which t-statistic the gate reads (module docs): default, then legacy.
SIGNIFICANCE_STATISTICS = ("pooled_slope", "within_bucket")
DEFAULT_SIGNIFICANCE = "pooled_slope"
LEGACY_SIGNIFICANCE = "within_bucket"

#: Capacity definitions (module docs): default, then legacy.
CAPACITY_DEFINITIONS = ("breakeven", "participation")
DEFAULT_CAPACITY = "breakeven"
LEGACY_CAPACITY = "participation"

#: Which IC the gate reads (module docs, R3): default first.
GATE_IC_SOURCES = ("pooled", "instrument_mean")
DEFAULT_GATE_IC_SOURCE = "pooled"

#: Book scopes (module docs, R4): default first.
BOOK_SCOPES = ("consolidated", "single_venue")
DEFAULT_BOOK_SCOPE = "consolidated"

#: Looks the ``cpcv`` block adds: one pooled IC per backtest path at the
#: default N = 6, k = 2 (5 paths).
CPCV_LOOKS = 5
#: Looks the ``deflated_sharpe`` block adds: one Sharpe (PSR / DSR / MinTRL
#: are transforms of it).
DSR_LOOKS = 1

#: Looks the ``validity`` block adds: ex-event gate IC, day-separated HAC t,
#: day-clustered t, day-block bootstrap t.
VALIDITY_LOOKS = 4

_KEY_1X = f"x{1.0:g}"


def looks_per_validation(
    n_folds: int,
    fold_diagnostics: bool = True,
    validity_diagnostics: bool = False,
    cpcv: bool = False,
    deflated_sharpe: bool = False,
) -> int:
    """Looks at the data ONE ``validate_alpha`` call makes — the ledger is
    the denominator of every multiple-testing correction, so it counts what
    the chain actually evaluates:

    =====  ==========================================================
    looks  statistic
    =====  ==========================================================
    1      pooled walk-forward OOS IC with the t the gate reads
    1      the other t-statistic (within-bucket and pooled-slope are
           both computed and reported; one of them is the gate's)
    2      crossed / uncrossed conditional IC
    4      instrument-mean and vol-scaled IC, on all rows and on the
           uncrossed rows the gate reads
    2      the IC under the other row policy (valid-only against
           reopen-scored), on all rows and on uncrossed rows
    3      latency stress, ROW grid (``stress.LATENCY_SHIFTS``)
    4      latency stress, TIME grid (``stress.LATENCY_TIMES_NS``)
    1      leakage shift-by-one IC
    1      bootstrap interval of the pooled net P&L
    11 x F decay curve, one IC per pinned horizon, in each of F folds
    3 x F  cost stress, one backtest per multiplier, in each fold
    2 x F  regime split (high / low vol), in each fold
    =====  ==========================================================

    = ``19 + 16 * n_folds`` (83 at the pinned four folds).  With
    ``fold_diagnostics=False`` — the legacy chain — decay, cost stress and
    regime exist for the last fold only and the statistics added since
    v1.3.0 (the second t, the scale-free ICs, the other-row-policy IC, the
    bootstrap) are not debited, which is the 27 the chain counted up to
    v1.4.0 (28 with the caller's one backtest).

    ``validity_diagnostics=True`` (v1.9, opt-in) adds
    :data:`VALIDITY_LOOKS`; ``cpcv=True`` (v1.12) :data:`CPCV_LOOKS`;
    ``deflated_sharpe=True`` (v1.12) :data:`DSR_LOOKS`.
    """
    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    if cpcv or deflated_sharpe:
        return (
            looks_per_validation(n_folds, fold_diagnostics, validity_diagnostics)
            + (CPCV_LOOKS if cpcv else 0)
            + (DSR_LOOKS if deflated_sharpe else 0)
        )
    if validity_diagnostics:
        return looks_per_validation(n_folds, fold_diagnostics) + VALIDITY_LOOKS
    if not fold_diagnostics:
        per_fold = len(HORIZON_ORDER) + len(COST_MULTIPLIERS) + 2
        return 1 + per_fold + len(LATENCY_SHIFTS) + len(LATENCY_TIMES_NS) + 2 + 1
    per_fold = len(HORIZON_ORDER) + len(COST_MULTIPLIERS) + 2
    fixed = 1 + 1 + 2 + 4 + 2 + len(LATENCY_SHIFTS) + len(LATENCY_TIMES_NS) + 1 + 1
    return fixed + per_fold * int(n_folds)


def effective_gates(
    tstat_threshold: str = DEFAULT_TSTAT_THRESHOLD, ledger_t_threshold: float | None = None
) -> dict:
    """The gate thresholds under a t-stat policy.

    ``"ledger"`` (default) returns a copy whose ``min_nw_tstat`` is
    ``max(GATES["min_nw_tstat"], ledger_t_threshold)`` — the ledger-derived
    Bonferroni |t|, never looser than the fixed gate — and requires a finite
    positive ``ledger_t_threshold``.  ``"fixed"`` (legacy) returns
    :data:`GATES` itself.
    """
    if tstat_threshold not in TSTAT_THRESHOLD_POLICIES:
        raise ValueError(
            f"unknown tstat_threshold {tstat_threshold!r}; known: {TSTAT_THRESHOLD_POLICIES}"
        )
    if tstat_threshold == "fixed":
        return GATES
    if (
        ledger_t_threshold is None
        or not np.isfinite(ledger_t_threshold)
        or ledger_t_threshold <= 0.0
    ):
        raise ValueError(
            "tstat_threshold='ledger' (the default) needs a finite positive "
            "ledger_t_threshold — ExperimentLedger.bonferroni_t_threshold_at(the run's "
            "gate look count); pass tstat_threshold='fixed' to name the legacy 3.0 gate"
        )
    gates = dict(GATES)
    gates["min_nw_tstat"] = max(float(GATES["min_nw_tstat"]), float(ledger_t_threshold))
    return gates


def _pooled_arrays(scores, frames, horizon, ic_rows: str = DEFAULT_IC_ROWS):
    """(ts, expected_return, label, crossed) pooled over the universe.

    ``label`` is the series ``ic_rows`` scores (:mod:`iap.labels.frames`);
    ``crossed`` marks rows whose consolidated book was crossed
    (``spread_ticks_v1 < 0``, i.e. at least one venue quote was stale).
    """
    ts, xs, ys, cs = [], [], [], []
    for iid, sc in scores.items():
        df = frames[iid]
        er = sc["expected_return"].to_numpy(dtype=float).copy()
        er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
        lab, _ = scored_labels(df, horizon, ic_rows)
        if "spread_ticks_v1" in df.columns:
            sp = df["spread_ticks_v1"].to_numpy(dtype=float)
            crossed = np.isfinite(sp) & (sp < 0.0)
        else:
            crossed = np.zeros(len(df), dtype=bool)
        ts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        xs.append(er)
        ys.append(lab)
        cs.append(crossed)
    if not xs:
        return (np.empty(0, np.int64), np.empty(0), np.empty(0), np.empty(0, bool))
    return (np.concatenate(ts), np.concatenate(xs), np.concatenate(ys), np.concatenate(cs))


def _pooled_instrument_ids(scores, frames) -> np.ndarray:
    """Instrument id of every row of :func:`_pooled_arrays` (same order)."""
    ids = [np.full(len(frames[iid]), int(iid), dtype=np.int64) for iid in scores]
    return np.concatenate(ids) if ids else np.empty(0, np.int64)


def _pooled_row_policies(scores, frames, horizon):
    """``(valid-only labels, reopen-scored labels, reopen available)`` in the
    row order of :func:`_pooled_arrays` — the label series under BOTH row
    policies, so the IC under the policy the gate does not use can be
    reported beside it.  ``reopen available`` is False when any frame lacks
    ``label_reopen_<h>`` (a feature store written before v1.5.0): the two
    series are then the same."""
    valid, reopen = [], []
    available = True
    for iid in scores:
        df = frames[iid]
        valid.append(scored_labels(df, horizon, LEGACY_IC_ROWS)[0])
        reopen.append(scored_labels(df, horizon, DEFAULT_IC_ROWS)[0])
        available = available and f"label_reopen_{horizon}" in df.columns
    if not valid:
        return np.empty(0), np.empty(0), False
    return np.concatenate(valid), np.concatenate(reopen), available


def _fnum(v: float) -> float | None:
    return float(v) if np.isfinite(v) else None


def _capacity(
    definition: str,
    backtester: Backtester,
    result_1x: BacktestResult,
    test: Mapping[int, pd.DataFrame],
    universe,
    capacity_meta: Mapping[int, dict],
    max_participation: float,
) -> tuple[dict[str, float], dict[str, float], dict[str, dict]]:
    """``(capacity, participation proxy, breakeven detail)`` per instrument.

    The proxy is the historic ``max_participation x ADV x ref_price`` figure
    (:func:`capacity_proxy_usd`), unchanged.  The breakeven detail carries,
    per instrument, the realised gross edge per round trip of the 1x
    backtest (``gross P&L / (traded notional / 2)``, both in the reporting
    currency, the notional at the median executable mid and the conversion
    pair's reference rate), the breakeven size in units and in the
    reporting currency, and that size capped at the participation line
    converted the same way.  ``capacity`` is the capped breakeven figure
    under ``"breakeven"`` and the proxy under ``"participation"``."""
    proxy = {
        str(iid): capacity_proxy_usd(
            float(capacity_meta[iid]["adv"]),
            float(capacity_meta[iid]["ref_price"]),
            max_participation,
            float(capacity_meta[iid].get("lot_value_multiplier", 1.0)),
        )
        for iid in universe
    }
    detail: dict[str, dict] = {}
    cfg = backtester.config
    for iid in universe:
        meta = backtester.meta[iid]
        asset_class = str(meta["asset_class"])
        unit = float(meta["lot_size"]) if asset_class == "FX" else 1.0
        adv = float(capacity_meta[iid]["adv"])
        rate = backtester.reference_rate(backtester.quote_currency(iid))
        # ADV is in base units (shares, or base currency for FX), so ADV x
        # price is quote-currency notional; ``rate`` takes it to USD.
        cap_line = float(max_participation * adv * float(capacity_meta[iid]["ref_price"]) * rate)
        row = {
            "edge_return": 0.0,
            "round_trips": 0.0,
            "breakeven_units": 0.0,
            "breakeven_usd": 0.0,
            "participation_cap_usd": cap_line,
            "capacity_usd": 0.0,
        }
        res = result_1x.per_instrument.get(iid)
        frame = test.get(iid)
        if res is not None and frame is not None and res.traded_qty > 0:
            mid = frame["mid_price_v1"].to_numpy(dtype=float)
            hs = frame["spread_ticks_v1"].to_numpy(dtype=float) * float(meta["tick_size"]) / 2.0
            ok = np.isfinite(mid) & (mid > 0.0) & np.isfinite(hs) & (hs >= 0.0)
            if ok.any():
                mid_med = float(np.median(mid[ok]))
                hs_med = float(np.median(hs[ok]))
                notional = 0.5 * float(res.traded_qty) * unit * mid_med * rate
                edge = float(res.gross_pnl) / notional if notional > 0.0 else 0.0
                be = capacity_breakeven(
                    backtester.cost_model,
                    edge,
                    mid_med,
                    hs_med,
                    asset_class,
                    adv,
                    int(meta["lot_size"]),
                )
                be_usd = float(be["notional"]) * rate
                row.update(
                    edge_return=edge,
                    round_trips=float(res.traded_qty) / (2.0 * float(cfg.max_pos_qty)),
                    breakeven_units=float(be["units"]),
                    breakeven_usd=be_usd,
                    capacity_usd=float(min(be_usd, cap_line)),
                )
        detail[str(iid)] = {k: _fnum(v) for k, v in row.items()}
    if definition == "participation":
        return proxy, proxy, detail
    capacity = {k: float(v["capacity_usd"] or 0.0) for k, v in detail.items()}
    return capacity, proxy, detail


def validate_alpha(
    model_factory,
    frames: Mapping[int, pd.DataFrame],
    backtester: Backtester,
    capacity_meta: Mapping[int, dict],
    max_participation: float,
    n_folds: int = 4,
    embargo_ns: int = 60_000_000_000,
    tstat_threshold: str = DEFAULT_TSTAT_THRESHOLD,
    ledger_t_threshold: float | None = None,
    stress_version: int = DEFAULT_STRESS_VERSION,
    significance: str = DEFAULT_SIGNIFICANCE,
    capacity: str = DEFAULT_CAPACITY,
    fold_diagnostics: bool = True,
    seed: int = 0,
    n_boot: int = BOOTSTRAP_RESAMPLES,
    recompute: RecomputeSource | None = None,
    ledger_looks: int | None = None,
    ic_rows: str = DEFAULT_IC_ROWS,
    split_mode: str = DEFAULT_SPLIT_MODE,
    gate_ic_source: str = DEFAULT_GATE_IC_SOURCE,
    validity_diagnostics: bool = False,
    book_scope: str = DEFAULT_BOOK_SCOPE,
    dataset_version: str | Mapping[str, str] | None = None,
    cpcv: bool = False,
    cpcv_groups: int = DEFAULT_CPCV_GROUPS,
    cpcv_test_groups: int = DEFAULT_CPCV_TEST_GROUPS,
    deflated_sharpe: bool = False,
    deflated_sharpe_trials: int | None = None,
) -> dict:
    """Full validation of one alpha.  ``model_factory()`` returns a fresh
    unfitted model (a fresh instance per fold — no state bleeds across).

    The keyword arguments from ``tstat_threshold`` on are the method choices
    of the module docs; every default is the v1.5.0 rule and every legacy
    rule is named.  ``backtester`` is used with the alpha's label horizon set
    (:meth:`Backtester.for_horizon`).  ``seed`` seeds the P&L bootstrap
    (pass ``ExperimentSpec.seed``); ``ledger_looks`` is recorded beside the
    threshold it produced."""
    gates = effective_gates(tstat_threshold, ledger_t_threshold)
    if significance not in SIGNIFICANCE_STATISTICS:
        raise ValueError(f"unknown significance {significance!r}; known: {SIGNIFICANCE_STATISTICS}")
    if capacity not in CAPACITY_DEFINITIONS:
        raise ValueError(f"unknown capacity {capacity!r}; known: {CAPACITY_DEFINITIONS}")
    if ic_rows not in IC_ROWS:
        raise ValueError(f"unknown ic_rows {ic_rows!r}; known: {IC_ROWS}")
    if split_mode not in SPLIT_MODES:
        raise ValueError(f"unknown split_mode {split_mode!r}; known: {SPLIT_MODES}")
    if gate_ic_source not in GATE_IC_SOURCES:
        raise ValueError(f"unknown gate_ic_source {gate_ic_source!r}; known: {GATE_IC_SOURCES}")
    if book_scope not in BOOK_SCOPES:
        raise ValueError(f"unknown book_scope {book_scope!r}; known: {BOOK_SCOPES}")
    if deflated_sharpe and not fold_diagnostics:
        raise ValueError("deflated_sharpe needs fold_diagnostics (the 1x backtest P&L)")
    if deflated_sharpe and deflated_sharpe_trials is None:
        raise ValueError(
            "deflated_sharpe needs deflated_sharpe_trials - the DSR number of independent "
            "trials (iap.validation.deflated.effective_trials(ledger))"
        )
    single_venue = book_scope == "single_venue"
    probe = model_factory()
    horizon = probe.horizon
    horizon_ns = HORIZONS_NS[horizon]
    universe = probe.universe(list(frames))
    uframes = {i: frames[i] for i in universe}
    splitter = WalkForwardSplitter(n_folds=n_folds, embargo_ns=embargo_ns, mode=split_mode)
    asset_class = "FX" if probe.asset_class == "FX" else "EQUITY"
    bt = backtester.for_horizon(horizon)

    fold_rows: list[dict] = []
    diag_rows: list[dict] = []
    results_1x: list[BacktestResult] = []
    pooled_ts: list[np.ndarray] = []
    pooled_x: list[np.ndarray] = []  # standardized signal (z), gate input
    pooled_er: list[np.ndarray] = []  # expected_return (diagnostic)
    pooled_y: list[np.ndarray] = []
    pooled_c: list[np.ndarray] = []
    pooled_i: list[np.ndarray] = []
    pooled_valid: list[np.ndarray] = []
    pooled_reopen: list[np.ndarray] = []
    reopen_available = True
    last_model = None
    last_test = None
    last_scores = None
    last_cost: dict[str, dict] | None = None
    last_result_1x: BacktestResult | None = None
    for fold, train, test in splitter.split_frames(uframes, horizon_ns):
        model = model_factory()
        model.fit(train)
        scores = model.score(test)
        ts, er, y, crossed = _pooled_arrays(scores, test, horizon, ic_rows)
        iids = _pooled_instrument_ids(scores, test)
        if single_venue:
            # One exchange's own book: a negative spread there is a data
            # fault, not a stale other-venue quote (module docs, R4).
            crossed = np.zeros_like(crossed)
        # Pool the standardized signal: folds fit different betas, so pooling
        # expected_return weights each fold by |beta_k| (pinned, round-3).
        beta = float(model.params().get("beta", 0.0) or 0.0)
        z = er / beta if beta != 0.0 else er
        n_pairs = int(np.sum(np.isfinite(er) & np.isfinite(y)))
        degenerate = n_pairs < MIN_TEST_PAIRS
        # Fold statistics are computed on z, NEVER on expected_return.
        # beta_k is refit free-signed per fold, so ic(er, y) is
        # sign(beta_k) * ic(z, y): an alpha that is backwards in every fold
        # (EQ09: beta < 0 in all 4) scored ic(er, y) > 0 four times over and
        # published fold_sign_consistency = 1.00 beside a NEGATIVE gate IC.
        # The gate reads z (see the pooled block below), so the folds must too.
        fold_rows.append(
            {
                "fold": fold.index,
                "n_train": int(model.params().get("n_train", 0)),
                "n_test_rows": int(len(y)),
                "n_test_pairs": n_pairs,
                "degenerate": degenerate,
                "test_start": int(fold.test_start),
                "test_end": int(fold.test_end),
                "ic": _fnum(ic(z, y)),
                "ic_er": _fnum(ic(er, y)),  # diagnostic: sign-flipped by beta
                "rank_ic": _fnum(rank_ic(z, y)),
                "hit_rate": _fnum(hit_rate(z, y)),
                "beta_fit": model.params().get("beta_fit"),
            }
        )
        pooled_ts.append(ts)
        pooled_x.append(z)
        pooled_er.append(er)
        pooled_y.append(y)
        pooled_c.append(crossed)
        pooled_i.append(iids)
        y_valid, y_reopen, available = _pooled_row_policies(scores, test, horizon)
        pooled_valid.append(y_valid)
        pooled_reopen.append(y_reopen)
        reopen_available = reopen_available and available
        if fold_diagnostics:
            row, cost, results = fold_row(
                fold.index, scores, test, horizon, beta, bt, asset_class, ic_rows=ic_rows
            )
            diag_rows.append(row)
            results_1x.append(results[_KEY_1X])
            last_cost, last_result_1x = cost, results[_KEY_1X]
        last_model, last_test, last_scores = model, test, scores

    ts = np.concatenate(pooled_ts)
    x = np.concatenate(pooled_x)
    er_pooled = np.concatenate(pooled_er)
    y = np.concatenate(pooled_y)
    crossed = np.concatenate(pooled_c)
    oos_ic = ic(x, y)
    oos_ic_er = ic(er_pooled, y)
    oos_rank_ic = rank_ic(x, y)
    oos_hit = hit_rate(x, y)
    lags = nw_lags(horizon_ns)
    # Bucket ICs are weighted by their pair count: fixed time buckets range
    # from ~81 to ~2 592 pairs here, and equal weighting let one thin bucket
    # swing EQ03's headline t between 4.89 and 11.46 (metrics module docs).
    bics, bcounts = bucket_ics_with_counts(ts, x, y)
    nw_t = newey_west_tstat(bics, lags=lags, weights=bcounts)
    # The HAC t of the POOLED slope keeps between-bucket signal, which the
    # within-bucket Pearson above removes; it is the gate's t by default.
    nw_t_pooled = pooled_slope_hac_tstat(ts, x, y, lags=lags)
    # Scale-free ICs (the pooled IC lets the most volatile instrument
    # dominate; metrics.instrument_ics).  Headline report fields.
    instrument_ids = np.concatenate(pooled_i)
    by_instrument = instrument_ics(instrument_ids, x, y)
    # The same IC under BOTH row policies (the gate reads ``ic_rows``).
    y_valid_only = np.concatenate(pooled_valid)
    y_reopen = np.concatenate(pooled_reopen)
    n_blackout_scored = int(
        np.sum(np.isfinite(x) & ~np.isfinite(y_valid_only) & np.isfinite(y_reopen))
    )

    # Crossed-book conditioning (pinned): a crossed consolidated book means a
    # stale venue quote; its mid reverts when that venue refreshes.
    pairs_ok = np.isfinite(x) & np.isfinite(y)
    n_pairs_all = int(pairs_ok.sum())
    crossed_frac = float(np.mean(crossed[pairs_ok])) if n_pairs_all else float("nan")
    if single_venue:
        crossed_frac = float("nan")
    unc = ~crossed
    oos_ic_uncrossed = ic(np.where(unc, x, np.nan), np.where(unc, y, np.nan))
    oos_ic_crossed = ic(np.where(crossed, x, np.nan), np.where(crossed, y, np.nan))
    bics_unc, bcounts_unc = bucket_ics_with_counts(ts[unc], x[unc], y[unc])
    nw_t_uncrossed = newey_west_tstat(bics_unc, lags=lags, weights=bcounts_unc)
    nw_t_pooled_uncrossed = pooled_slope_hac_tstat(ts[unc], x[unc], y[unc], lags=lags)
    by_instrument_unc = instrument_ics(instrument_ids[unc], x[unc], y[unc])

    def _ic_pair(labels: np.ndarray) -> tuple[float | None, float | None]:
        """(all rows, uncrossed rows) pooled IC against ``labels``."""
        return _fnum(ic(x, labels)), _fnum(ic(x[unc], labels[unc]))

    ic_valid_only, ic_valid_only_unc = _ic_pair(y_valid_only)
    if reopen_available:
        ic_reopen, ic_reopen_unc = _ic_pair(y_reopen)
    else:
        ic_reopen, ic_reopen_unc = None, None

    # Degenerate folds count as FAILED folds, never as missing data.
    n_folds_run = len(fold_rows)
    n_nondegenerate = sum(1 for r in fold_rows if not r["degenerate"])
    positive = sum(
        1 for r in fold_rows if (not r["degenerate"]) and r["ic"] is not None and r["ic"] > 0
    )
    sign_consistency = float(positive / n_folds_run) if n_folds_run else float("nan")

    # leakage + decay + turnover on the last (largest-train) fold
    leak = LeakageTester(ic_rows=ic_rows).run(last_model, last_test, recompute=recompute).to_dict()
    scores_last = last_scores
    beta_last = float(last_model.params().get("beta", 0.0) or 0.0)
    decay: dict[str, float | None] = {}
    turnover_vals = []
    turnover_active_hours = 0.0
    turnover_span_hours = 0.0
    for iid, sc in scores_last.items():
        er_last = sc["expected_return"].to_numpy(dtype=float).copy()
        # Same confidence mask _pooled_arrays applies everywhere else: the
        # decay IC used to see rows whose signal was NaN as an exact 0.0
        # (62.4 % of FX02's last fold, 66.8 % of FX05's), which is not a
        # prediction of "no move" — it is the absence of a prediction, and a
        # column of zeros shrinks the IC toward 0 rather than dropping out.
        er_last[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
        z_last = er_last / beta_last if beta_last != 0.0 else er_last
        d = decay_curve(z_last, last_test[iid], ic_rows=ic_rows)
        for h, v in d.items():
            decay.setdefault(h, [])
            if np.isfinite(v):
                decay[h].append(v)
        tdet = signal_turnover_detail(
            last_test[iid]["exchange_ts"].to_numpy(),
            sc["expected_return"].to_numpy(),
            sc["confidence"].to_numpy(),
        )
        if np.isfinite(tdet["flips_per_hour"]):
            turnover_vals.append(tdet["flips_per_hour"])
            turnover_active_hours += tdet["active_hours"]
            turnover_span_hours += tdet["span_hours"]
    decay_out = {h: (_fnum(float(np.mean(v))) if v else None) for h, v in decay.items()}
    turnover = float(np.mean(turnover_vals)) if turnover_vals else float("nan")

    # stress (fit on all-but-last-segment model, applied to its test set)
    if last_cost is None or last_result_1x is None:
        last_cost, last_results = cost_stress_results(bt, last_test, scores_last, asset_class)
        last_result_1x = last_results[_KEY_1X]
    stress = {
        "cost": last_cost,
        "latency": latency_stress(
            bt,
            last_test,
            scores_last,
            asset_class,
            horizon,
            beta=beta_last,
            version=stress_version,
            ic_rows=ic_rows,
        ),
        "latency_time": latency_stress_time(bt, last_test, scores_last, asset_class, horizon),
        "regime": {
            k: _fnum(v)
            for k, v in regime_split(
                scores_last, last_test, horizon, beta=beta_last, ic_rows=ic_rows
            ).items()
        },
    }
    net_pnl_1x = stress["cost"]["x1"]["total_pnl"]

    capacity_by, capacity_proxy_by, capacity_detail = _capacity(
        capacity, bt, last_result_1x, last_test, universe, capacity_meta, max_participation
    )

    hypothesis_confirmed = bool(last_model.params().get("hypothesis_confirmed", False))
    # The PROMOTE gate reads the UNCROSSED IC: a crossed consolidated book is
    # a stale-quote artefact, not a tradable state (pinned, round-3).
    gate_ic_pooled = oos_ic_uncrossed if np.isfinite(oos_ic_uncrossed) else oos_ic
    if gate_ic_source == "instrument_mean":
        # Equal weight per instrument on the rows the pooled gate IC reads
        # (R3): the pooled IC is dominated by the busiest instrument.
        gate_ic = (
            by_instrument_unc["instrument_mean"]
            if np.isfinite(oos_ic_uncrossed)
            else by_instrument["instrument_mean"]
        )
    else:
        gate_ic = gate_ic_pooled
    if significance == "pooled_slope":
        gate_t = nw_t_pooled_uncrossed if np.isfinite(nw_t_pooled_uncrossed) else nw_t_pooled
    else:
        gate_t = nw_t_uncrossed if np.isfinite(nw_t_uncrossed) else nw_t
    promote_gates = {
        "leakage": bool(leak["passed"]),
        "ic": bool(np.isfinite(gate_ic) and gate_ic >= gates["min_oos_ic"]),
        "significance": bool(np.isfinite(gate_t) and gate_t >= gates["min_nw_tstat"]),
        "fold_consistency": bool(
            np.isfinite(sign_consistency) and sign_consistency >= gates["min_fold_sign_consistency"]
        ),
        "folds": bool(n_nondegenerate >= gates["min_nondegenerate_folds"]),
        "hypothesis": bool(hypothesis_confirmed),
        "cost": bool(net_pnl_1x > 0.0),
    }
    promote = all(promote_gates.values())
    iterate = (
        not promote
        and leak["passed"]
        and np.isfinite(gate_ic)
        and gate_ic >= gates["iterate_min_ic"]
        and np.isfinite(gate_t)
        and gate_t >= gates["iterate_min_tstat"]
    )
    verdict = "PROMOTE" if promote else ("ITERATE" if iterate else "REJECT")

    # Scale-free IC on the rows the gate reads (uncrossed when that IC is).
    vol_scaled_gate = (
        by_instrument_unc["vol_scaled"]
        if np.isfinite(oos_ic_uncrossed)
        else by_instrument["vol_scaled"]
    )
    scale_consistent = (
        bool(np.sign(vol_scaled_gate) == np.sign(gate_ic))
        if np.isfinite(vol_scaled_gate) and np.isfinite(gate_ic)
        else None
    )

    extras: dict[str, object] = {}
    if validity_diagnostics:
        gate_rows = unc if np.isfinite(oos_ic_uncrossed) else np.ones_like(unc)
        extras["validity"] = _validity_block(
            ts[gate_rows],
            x[gate_rows],
            y[gate_rows],
            instrument_ids[gate_rows],
            lags=lags,
            gate_ic_source=gate_ic_source,
            seed=seed,
            n_boot=n_boot,
        )
    if dataset_version is not None:
        extras["dataset_versions"] = (
            {"dataset": str(dataset_version)}
            if isinstance(dataset_version, str)
            else {str(k): str(v) for k, v in sorted(dataset_version.items())}
        )
    method_extras: dict[str, object] = {}
    if split_mode != DEFAULT_SPLIT_MODE:
        method_extras["split_mode"] = split_mode
    if gate_ic_source != DEFAULT_GATE_IC_SOURCE:
        method_extras["gate_ic_source"] = gate_ic_source
    if validity_diagnostics:
        method_extras["validity_diagnostics"] = True
    if single_venue:
        method_extras["book_scope"] = book_scope
    if cpcv:
        method_extras["cpcv"] = True  # N and k are in the cpcv block
        extras["cpcv"] = _cpcv_block(
            model_factory,
            uframes,
            horizon,
            horizon_ns,
            ic_rows,
            n_groups=cpcv_groups,
            k_test=cpcv_test_groups,
            embargo_ns=embargo_ns,
        )
    if deflated_sharpe:
        method_extras["deflated_sharpe"] = True
        extras["deflated_sharpe"] = deflated_sharpe_block(
            _daily_pnl(results_1x),
            int(deflated_sharpe_trials),
            n_looks=None if ledger_looks is None else int(ledger_looks),
        )
    if tstat_threshold != "fixed":
        extras["tstat_threshold_policy"] = tstat_threshold
        extras["ledger_t_threshold"] = float(ledger_t_threshold)
        extras["ledger_looks"] = None if ledger_looks is None else int(ledger_looks)
    if fold_diagnostics:
        series = bar_series(results_1x)
        extras["fold_diagnostics"] = diag_rows
        extras["n_folds_survive_1x_cost"] = sum(1 for r in diag_rows if r["survives_1x_cost"])
        extras["net_pnl_1x_pooled"] = float(sum(series))
        extras["net_pnl_bootstrap"] = stationary_bootstrap_ci(series, seed, n_boot=n_boot)

    return {
        **extras,
        "methods": {
            "ic_rows": ic_rows,
            "significance": significance,
            "tstat_threshold": tstat_threshold,
            "stress_version": int(stress_version),
            "capacity": capacity,
            "fold_diagnostics": bool(fold_diagnostics),
            "recompute_probe": recompute is not None,
            "position_policy": bt.config.position_policy,
            "cap_fills_at_l1": bool(bt.config.cap_fills_at_l1),
            "block_rows": bt.config.block_rows_label(),
            "impact_model": bt.cost_model.impact_model,
            **method_extras,
        },
        "alpha_id": probe.alpha_id,
        "name": probe.name,
        "asset_class": probe.asset_class,
        "horizon": horizon,
        "universe": universe,
        "gates": gates,
        "folds": fold_rows,
        "n_folds_run": n_folds_run,
        "n_nondegenerate_folds": n_nondegenerate,
        "n_degenerate_folds": n_folds_run - n_nondegenerate,
        "oos_ic": _fnum(oos_ic),
        "oos_ic_pooled_er": _fnum(oos_ic_er),
        "oos_ic_instrument_mean": _fnum(by_instrument["instrument_mean"]),
        "oos_ic_vol_scaled": _fnum(by_instrument["vol_scaled"]),
        "oos_ic_by_instrument": by_instrument["by_instrument"],
        "oos_ic_instrument_mean_uncrossed": _fnum(by_instrument_unc["instrument_mean"]),
        "oos_ic_vol_scaled_uncrossed": _fnum(by_instrument_unc["vol_scaled"]),
        "ic_scale_consistent": scale_consistent,
        "oos_ic_valid_only": ic_valid_only,
        "gate_ic_valid_only": ic_valid_only_unc if ic_valid_only_unc is not None else ic_valid_only,
        "oos_ic_blackout_reopen": ic_reopen,
        "gate_ic_blackout_reopen": ic_reopen_unc if ic_reopen_unc is not None else ic_reopen,
        "label_reopen_available": bool(reopen_available),
        "n_blackout_rows_scored": n_blackout_scored if reopen_available else None,
        "oos_ic_uncrossed": _fnum(oos_ic_uncrossed),
        "oos_ic_crossed": None if single_venue else _fnum(oos_ic_crossed),
        "crossed_frac": _fnum(crossed_frac),
        "gate_ic": _fnum(gate_ic),
        "gate_ic_source": gate_ic_source,
        "gate_ic_pooled": _fnum(gate_ic_pooled),
        "book_scope": book_scope,
        "price_reference": "nasdaq_bbo" if single_venue else "consolidated_mid",
        "gate_tstat": _fnum(gate_t),
        "oos_rank_ic": _fnum(oos_rank_ic),
        "oos_hit_rate": _fnum(oos_hit),
        "nw_tstat": _fnum(nw_t),
        "nw_tstat_uncrossed": _fnum(nw_t_uncrossed),
        "nw_tstat_pooled": _fnum(nw_t_pooled),
        "nw_tstat_pooled_uncrossed": _fnum(nw_t_pooled_uncrossed),
        "nw_lags": int(lags),
        "n_ic_buckets": int(bics.size),
        "n_ic_buckets_uncrossed": int(bics_unc.size),
        "ic_bucket_pairs": bucket_size_summary(bcounts),
        "ic_bucket_pairs_uncrossed": bucket_size_summary(bcounts_unc),
        "fold_sign_consistency": _fnum(sign_consistency),
        "hypothesis_confirmed": hypothesis_confirmed,
        "leakage": leak,
        "decay_ic_by_horizon": decay_out,
        "turnover_flips_per_hour": _fnum(turnover),
        # The denominator is reported so the cost statistic can be audited:
        # flips/h over ACTIVE hours, not over the wall span that includes the
        # hours the market was shut (see metrics.signal_turnover_detail).
        "turnover_active_hours": _fnum(turnover_active_hours),
        "turnover_span_hours": _fnum(turnover_span_hours),
        "capacity_usd_by_instrument": capacity_by,
        "capacity_proxy_usd_by_instrument": capacity_proxy_by,
        "capacity_breakeven_by_instrument": capacity_detail,
        "stress": stress,
        "net_pnl_1x_cost": _fnum(net_pnl_1x),
        "trade_count_1x_cost": int(stress["cost"]["x1"]["trade_count"]),
        # Each PROMOTE gate on its own, so a report can say WHICH gates an
        # alpha fails (the verdict is PROMOTE iff all are true).
        "promote_gates": promote_gates,
        "verdict": verdict,
    }


def _validity_block(
    ts: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    instrument_ids: np.ndarray,
    *,
    lags: int,
    gate_ic_source: str,
    seed: int,
    n_boot: int,
) -> dict:
    """The ``validity`` report block (module docs, R1/R2/R6) on the gate's
    rows: per-day ICs, the event-day split, the day-separated HAC t, the
    day-clustered t and the day-block bootstrap t."""

    def gate_stat(m: np.ndarray) -> float:
        if gate_ic_source == "instrument_mean":
            return instrument_ics(instrument_ids[m], x[m], y[m])["instrument_mean"]
        return ic(x[m], y[m])

    pairs = np.isfinite(x) & np.isfinite(y)
    days = session_days(ts)
    day_rows = []
    for d in np.unique(days[pairs]):
        m = pairs & (days == d)
        iso = day_index_to_iso(int(d))
        day_rows.append(
            {
                "day": iso,
                "tags": sorted(day_tags(iso)),
                "n_pairs": int(m.sum()),
                "ic": _fnum(ic(x[m], y[m])),
            }
        )
    keep = ~event_day_mask(ts)
    boot = day_block_bootstrap_tstat(ts, x, y, seed=seed, n_boot=min(int(n_boot), 2000))
    return {
        "n_days": len(day_rows),
        "days": day_rows,
        "ex_event": {
            "excluded_tags": ["fomc", "holiday_thin"],
            "n_days_excluded": sum(1 for r in day_rows if r["tags"]),
            "n_pairs": int(np.sum(pairs & keep)),
            "gate_ic": _fnum(gate_stat(keep)),
            "ic_pooled": _fnum(ic(x[keep], y[keep])),
            "tstat_pooled_slope": _fnum(
                pooled_slope_hac_tstat(ts[keep], x[keep], y[keep], lags=lags, day_ns=NS_DAY)
            ),
        },
        "tstat_pooled_slope_day_separated": _fnum(
            pooled_slope_hac_tstat(ts, x, y, lags=lags, day_ns=NS_DAY)
        ),
        "tstat_day_cluster": _fnum(day_cluster_tstat(ts, x, y)),
        "day_block_bootstrap": {k: _fnum(v) for k, v in boot.items()},
    }


def _daily_pnl(results: list[BacktestResult]) -> list[float]:
    """Net 1x P&L per UTC session day, pooled over instruments and folds."""
    ts: list[int] = []
    pnl: list[float] = []
    for result in results:
        for r in result.per_instrument.values():
            ts.extend(int(t) for t in r.bar_ts)
            pnl.extend(float(p) for p in r.bar_pnl)
    return daily_pnl_from_bars(ts, pnl)


def _cpcv_block(
    model_factory,
    uframes: Mapping[int, pd.DataFrame],
    horizon: str,
    horizon_ns: int,
    ic_rows: str,
    *,
    n_groups: int,
    k_test: int,
    embargo_ns: int,
) -> dict:
    """The ``cpcv`` report block (module docs): per-split and per-path
    pooled IC of the standardized signal on all scored rows."""
    splitter = CombinatorialPurgedSplitter(n_groups, k_test, embargo_ns)
    per_split: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    splits = []
    split_rows = []
    for split, train, test in splitter.split_frames(uframes, horizon_ns):
        model = model_factory()
        model.fit(train)
        scores = model.score(test)
        ts, er, y, _ = _pooled_arrays(scores, test, horizon, ic_rows)
        beta = float(model.params().get("beta", 0.0) or 0.0)
        z = er / beta if beta != 0.0 else er
        per_split[split.index] = (ts, z, y)
        splits.append(split)
        split_rows.append(
            {
                "split": split.index,
                "test_groups": list(split.test_groups),
                "n_train": int(model.params().get("n_train", 0)),
                "n_test_pairs": int(np.sum(np.isfinite(z) & np.isfinite(y))),
                "ic": _fnum(ic(z, y)),
            }
        )
    path_ics: list[float | None] = []
    for assignment in splitter.path_assignment():
        zs, ys = [], []
        for g, i in sorted(assignment.items()):
            ts, z, y = per_split[i]
            m = splits[i].group_mask(ts, g)
            zs.append(z[m])
            ys.append(y[m])
        path_ics.append(_fnum(ic(np.concatenate(zs), np.concatenate(ys))))
    finite = np.array([v for v in path_ics if v is not None], dtype=float)
    return {
        "grouping": splitter.grouping,
        "n_groups": splitter.n_groups,
        "k_test": splitter.k_test,
        "n_splits": splitter.n_splits,
        "n_paths": splitter.n_paths,
        "splits": split_rows,
        "path_ics": path_ics,
        "path_ic_mean": _fnum(float(finite.mean())) if finite.size else None,
        "path_ic_std": _fnum(float(finite.std(ddof=1))) if finite.size > 1 else None,
        "path_ic_min": _fnum(float(finite.min())) if finite.size else None,
        "path_ic_max": _fnum(float(finite.max())) if finite.size else None,
        "path_frac_positive": _fnum(float(np.mean(finite > 0))) if finite.size else None,
    }
