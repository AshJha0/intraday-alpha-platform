# Predictability of Order-Flow Imbalance in Liquid Equity Markets: Significant, Stable, and Still Not Worth Trading

> Dated record. The figures below are those of the dataset in force when the paper was written; the 2026-10-03 update restates them on the v1.4.0 dataset and re-checks each conclusion, and the 2026-10-04 update at the end restates them under the v1.5.0 default research methods (same dataset) and re-checks each conclusion again.

*Intraday Alpha Platform research series, paper 1 of 6 (spec §28). Generated 2026-08-29 from the repository's committed research artifacts.*

---

## Abstract

We study the short-horizon predictive power of order-flow imbalance (OFI) on
the platform's bundled two-day synthetic equity dataset, using the flagship
alphas EQ02 (L1 OFI) and EQ03 (multi-level OFI) with the liquidity-conditioned
variant EQ12 as a robustness check. Under walk-forward validation with purging
and embargo, L1 OFI achieves a pooled out-of-sample information coefficient
(IC) of 0.0274 (Newey-West t = 5.89) at the pinned 5-second horizon, and the
multi-level variant posts 0.0256 (t = 7.24), with perfect fold sign
consistency (both non-degenerate test folds positive) and a passed automated
leakage test — t-statistics that, unusually for this series, clear even the
ledger-wide selection-adjusted thresholds. The signal exhibits the classic
microstructure decay profile: near-zero IC below 100 ms, a peak around
10 seconds (IC ≈ 0.045-0.047), and slow decay out to 15 minutes. The honest
headline, however, is economic, not statistical: at realistic costs the
strategy loses money at every cost multiplier tested, including at *half*
the modeled costs. On the held-out second day, EQ02 grosses +2,305 currency
units but pays 219,916 in costs across 11,805 trades — costs exceed gross
alpha by roughly two orders of magnitude at 246 signal flips per hour. OFI
on this dataset is a textbook example of a statistically real, economically
unharvestable signal, and both alphas are gated ITERATE rather than PROMOTE
by the platform's promotion rules. All data is synthetic; this is a
methodology demonstration, not a market claim.

---

## 1. Introduction

Order-flow imbalance — the net of additions and deletions at the best quotes
— is among the best-documented short-horizon predictors in the equity
microstructure literature (Cont, Kukanov and Stoikov's OFI being the
canonical formulation). The platform implements OFI as a first-class feature
family and dedicates three flagship alphas to it:

- **EQ02** (`ofi_l1`): depth-normalized 1-second L1 OFI
  (`python/src/iap/alpha/equity.py`, class `EQ02OfiL1`).
- **EQ03** (`ofi_multilevel`): pinned 0.5/0.3/0.2 combination of L1-window-1s,
  L5-window-1s and L5-window-5s normalized OFI (class `EQ03OfiMultiLevel`).
- **EQ12** (`liquidity_conditioned_ofi`): the same L1 signal conditioned on
  liquidity state (class `EQ12LiquidityConditionedOfi`).

This paper reports what the platform's validation framework (spec §13)
actually found, including the part that a less honest report would bury: the
cost-adjusted result.

## 2. Data

**All data in this study is synthetic.** The dataset is produced by the
seeded generator in `python/src/iap/marketdata/generator.py`: two trading
days (2026-08-24, 2026-08-25) of full market-by-order (MBO) streams for 11
equity instruments (SYN.EQ.001-010 plus the SYN.ETF.IDX index ETF,
instrument ids 1-11) across two venues (XV1, XV2). The generator maintains a
real internal order book per stream (executions consume FIFO heads, crossing
adds are marketable), drives the mid with two-state regime-switching
volatility, and clusters order flow with a self-exciting (Hawkes-style)
intensity. Identical seed implies bit-identical data.

Volumes and quality control (`data/normalized/qc_report.json`):

| quantity | value |
|---|---|
| equity events, day 1 / day 2 | 105,640 / 105,294 |
| total normalized events (eq + FX) | 310,159 rows (`events.parquet`) |
| streams monitored | 46 |
| sequence gaps detected | 322 (461 missing events) |
| duplicates dropped | 450 |
| out-of-order arrivals | 182 |
| invalid events | 173 |

The generator drives all venues of an instrument from one shared efficient
price (this is the redesigned generator; an earlier version with per-venue
price noise left the consolidated equity book crossed ~95% of the time —
see the ML report's artifact discussion). Feature frames
(`data/features/features_<id>.parquet`) are event-time emissions from the
deterministic feature engine; instrument 1 has 14,105 rows over the two
days with a median inter-emission gap of ~0.9 s (computed directly from the
committed parquet's `exchange_ts` column).

### Why synthetic data is disclosed prominently

The generator's mid is strongly mean-reverting by construction
(`research/alpha_reports/REPORT.md`, honesty note). Several textbook
hypotheses fail on it, and any result here is a statement about the pipeline
and this dataset — not about real markets. The value of this paper is that
every number is reproducible to the digit from a pinned seed.

## 3. Methodology

### 3.1 Feature construction

The OFI contribution at each book refresh is the pinned multi-level
generalization documented in `python/src/iap/features/orderflow.py`: for
level count k and side s, with prev/curr {price → size} maps of the best k
levels,

```
delta_s(k) = sum_p [ curr_k.get(p,0) - prev_k.get(p,0) ]
e(k)       = delta_bid(k) - delta_ask(k)
```

`ofi_lk_w` is the exact integer sum of e(k) over the half-open event-time
window (t-w, t], and `ofi_norm_lk_w` divides by the 10-second mean two-sided
depth. All raw signals are causal: same-row features computed from events
at-or-before the row's `exchange_ts`.

### 3.2 Validation protocol (spec §13, §20)

- **Walk-forward:** 4 expanding folds with purging at label horizon and a
  60 s embargo; only out-of-sample test pairs are scored (EQ02: 11,675 +
  66,202 test pairs in the two non-degenerate folds, per
  `research/alpha_reports/EQ02.json`).
- **Leakage:** an automated shift-by-one test must degrade IC (EQ02:
  unshifted 0.0273 vs shifted 0.0215 in the last fold — degraded, passed).
- **Gates** (pinned in the JSON artifacts): PROMOTE requires OOS IC ≥ 0.01,
  NW t ≥ 3.0, fold sign consistency ≥ 0.7, confirmed economic hypothesis
  sign, *and* positive net P&L at 1x costs; ITERATE requires IC ≥ 0.005 and
  t ≥ 1.5.
- **Costs:** the research cost model (`python/src/iap/backtest/costs.py`,
  `configs/execution/execution.json`) charges half-spread + $0.003/share taker fee +
  linear impact (2.0 bps per pct-of-ADV), with a stress grid at {0.5, 1, 2}x.
- **Execution lag:** the backtester never executes on the decision row
  (`latency_rows = 1` default in `python/src/iap/backtest/engine.py`).

### 3.3 Multiple testing

The experiments ledger records 1,224 experiments at report time
(`research/alpha_reports/REPORT.md`). The Bonferroni per-test threshold is
|t| ≥ 4.10, and the expected maximum |t| under the global null across this
many looks is ~3.77. EQ02's t = 5.89 and EQ03's t = 7.24 clear both bars —
rare in this series — and are further supported by sign stability across
folds, the confirmed hypothesis sign, and the coherent decay profile:
exactly the multi-criteria stance of spec §13. Statistical reality is not
in serious doubt here; economics are (§4.3).

## 4. Results

### 4.1 Headline walk-forward statistics

From the master table in `research/alpha_reports/REPORT.md` and the per-alpha
JSONs:

| alpha | signal | horizon | OOS IC | Rank IC | NW t | hit rate | folds+ | leakage | net P&L (1x) | flips/h | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| EQ02 | L1 OFI | 5s | 0.0274 | 0.0548 | 5.89 | 0.542 | 2/2 | pass | -182,644 | 246 | ITERATE |
| EQ03 | multi-level OFI | 5s | 0.0256 | 0.0434 | 7.24 | 0.528 | 2/2 | pass | -199,913 | 277 | ITERATE |
| EQ12 | liq.-conditioned OFI | 5s | 0.0262 | 0.0547 | 5.49 | 0.543 | 2/2 | pass | -183,375 | 245 | ITERATE |

(`folds+` counts the non-degenerate test folds — the first two expanding
folds have zero test pairs on this dataset.) The three OFI variants are
statistically indistinguishable in IC (0.026-0.027); the multi-level
construction earns the strongest t-statistic (7.24), consistent with
deeper-book quote revisions carrying complementary, slightly slower and
more stable information, while liquidity conditioning (EQ12) adds nothing
material on this dataset.

### 4.2 Decay profile

OOS IC by horizon, last fold (`research/alpha_reports/REPORT.md`, decay
table):

| alpha | 10ms | 50ms | 100ms | 500ms | 1s | 5s | 10s | 30s | 1m | 5m | 15m |
|---|---|---|---|---|---|---|---|---|---|---|---|
| EQ02 | +0.002 | +0.002 | +0.009 | +0.028 | +0.034 | +0.043 | +0.047 | +0.034 | +0.020 | +0.006 | +0.005 |
| EQ03 | -0.000 | -0.001 | +0.005 | +0.025 | +0.031 | +0.042 | +0.045 | +0.033 | +0.019 | +0.008 | +0.006 |

Two observations. First, there is essentially no predictability below
100 ms: OFI on this dataset is not a latency race. Second, the IC *peaks at
10 seconds* — just beyond the pinned 5 s trading horizon — and decays
gently afterwards, still positive at 15 minutes. The information is real
but slower than the trading cadence, which matters for the cost analysis
below: a slow signal traded fast pays turnover costs it does not need to
pay.

### 4.3 The cost-adjusted collapse

Cost stress (last fold, net P&L, from the REPORT.md stress table):

| alpha | x0.5 costs | x1 costs | x2 costs | trades |
|---|---|---|---|---|
| EQ02 | -90,382 | -182,644 | -367,167 | 9,973 |
| EQ03 | -99,029 | -199,913 | -401,681 | 10,914 |
| EQ12 | -90,745 | -183,375 | -368,634 | 9,905 |

**The signal is cost-negative at every multiplier, including half costs.**
The gross/net decomposition on the held-out day 2 (day-1-fitted parameters,
REPORT.md day-2 backtest) makes the failure mode explicit:

| alpha | gross P&L | costs | net P&L | trades | ann. Sharpe |
|---|---|---|---|---|---|
| EQ02 | +2,305 | 219,916 | -217,611 | 11,805 | -882.9 |
| EQ03 | +2,280 | 240,947 | -238,667 | 12,953 | -884.8 |
| EQ12 | +2,225 | 218,467 | -216,242 | 11,614 | -847.2 |

Gross alpha is positive out-of-sample — the prediction works — but costs are
roughly two orders of magnitude larger than gross. The driver is turnover:
246-277 signal flips per hour against an IC whose peak sits at 10 s.
Contrast EQ08 (VWAP/mid deviation, same dataset, 11 flips/h): it loses only
-7,543 at 1x costs — the least of any equity alpha — purely because it
trades ~25x less, though it fails the statistical gates outright (t -0.14,
hypothesis unconfirmed, REJECT). On this dataset no alpha is net-positive
at 1x costs; the *size* of the loss is almost entirely a turnover/cost
problem.

### 4.4 Latency and regime sensitivity

From `research/alpha_reports/EQ02.json` (`stress` block):

| shift | EQ02 IC | EQ03 IC |
|---|---|---|
| +0 events | 0.0273 | 0.0267 |
| +1 event | 0.0215 | 0.0214 |
| +5 events | 0.0068 | 0.0085 |

One event of extra execution lag costs ~21% of EQ02's last-fold IC, and
five events cost ~75% — consistent with the ~0.9 s median emission gap
against a decay curve that peaks near 10 s: the signal survives being one
emission late, but not five. OFI here is fast by equity-alpha standards
(compare the slow alphas EQ08/EQ09, whose IC is flat out to +5 events),
without being a sub-event latency race.

Regime split (REPORT.md): EQ02 IC is +0.0256 in high-volatility states and
+0.0291 in low-volatility states; EQ03 +0.0235 / +0.0300. The OFI edge on
this dataset is present in both regimes, slightly stronger in low-vol —
regime conditioning offers no rescue from the cost problem.

### 4.5 Capacity

Per-instrument capacity estimates (`research/alpha_reports/EQ02.json`,
`capacity_usd_by_instrument`) run $34-47M per single-stock instrument and
$1.31B for the index ETF — not a binding constraint at research scale; costs
bind long before capacity does.

## 5. Limitations

1. **Synthetic data.** The generator's strongly mean-reverting mid inflates
   reversion-family alphas and dampens flow-momentum ones; real-market OFI
   magnitudes and decay speeds will differ. A REJECT/ITERATE here is a
   statement about this dataset, not the idea (REPORT.md honesty note;
   spec §32).
2. **Two days, 11 instruments.** Fold counts are small; two of four folds
   for EQ02/EQ03 have zero test pairs (`EQ02.json`, `folds`), so pooled OOS
   statistics rest on the back half of the sample.
3. **Multiple testing.** With 1,224 recorded experiments, the Bonferroni
   per-test threshold is |t| ≥ 4.10 and the expected max |t| under the
   global null ~3.77. EQ02 (5.89) and EQ03 (7.24) clear both — but on two
   days of strongly overlapping 5 s labels, t-statistics overstate
   effective sample size; the fold-consistency and decay-shape evidence is
   the more defensible support.
4. **Linear cost model.** Costs are half-spread + fee + linear impact
   (`iap.backtest.costs`); queue-position and adverse-selection effects are
   modeled only in the execution simulator (see paper 5), not in this
   research backtest. Real net P&L for a passive implementation could be
   better than modeled here — this is the standard argument for
   maker-style harvesting of slow signals, and it is untested in this study.
5. **Ann. Sharpe yardstick.** The day-2 Sharpe values are annualized from
   1-minute event-time bars assuming independence (REPORT.md scaling note);
   they are a research yardstick, not a production claim.

## 6. Conclusions

On the platform's synthetic two-day equity dataset, order-flow imbalance is
a *real* predictor by every statistical criterion the validation framework
applies: positive OOS IC at every horizon from 500 ms to 15 m, perfect
sign consistency across the non-degenerate folds, passed leakage tests,
selection-adjusted-significant t-statistics, and a decay profile that peaks
near 10 s. Multi-level OFI earns the most stable statistics of the family,
echoing the qualitative literature result. And none of it survives costs:
at 246-277 flips per hour the strategy pays roughly two orders of magnitude
more in spread, fees and impact than its gross alpha earns, at 1x and even
0.5x modeled costs. The platform's verdict — ITERATE, not PROMOTE — is the
correct institutional answer: the signal earns further research (slower
trade scheduling, maker-style execution, ensembling with the slow
low-turnover alphas), not capital.

The broader lesson this paper exists to document (spec §32): *statistical
significance is the cheapest of the promotion gates.* The expensive gate is
net-of-cost economics, and OFI at 5-second turnover fails it decisively on
this dataset.

## Artifact provenance

| claim | artifact |
|---|---|
| master table, decay, cost/latency stress, regime split, day-2 backtest | `research/alpha_reports/REPORT.md` |
| EQ02/EQ03/EQ12 per-alpha metrics, folds, leakage, capacity | `research/alpha_reports/EQ02.json`, `EQ03.json`, `EQ12.json` |
| alpha definitions and rationales | `python/src/iap/alpha/equity.py` |
| OFI feature formulas | `python/src/iap/features/orderflow.py` |
| cost model | `python/src/iap/backtest/costs.py`, `configs/execution/execution.json` |
| stress axes | `python/src/iap/validation/stress.py` |
| data volumes and QC | `data/normalized/qc_report.json` |
| generator design | `python/src/iap/marketdata/generator.py` |


## Erratum / Update — 2026-09-06 (round-3 research fixes)

The research layer was re-audited and several defects that affect the
numbers above were fixed; this section records the revised figures. The
body is left as the dated record of what was computed at the time.

1. **"Four expanding walk-forward folds" were two.** Fold boundaries were
   equal segments of the WALL SPAN, and the equity frames occupy 13:30-16:05
   UTC of each day plus a lone 20:00 close print — so two of the four folds
   contained ZERO test rows and were silently dropped, while the report still
   claimed four folds and a fold sign consistency of 1.00. Boundaries are now
   quantiles of the ROW INDEX; a fold with fewer than 32 usable pairs is
   reported, counts as a FAILED fold, and PROMOTE requires at least three
   non-degenerate folds. On the current data every alpha runs **four
   non-degenerate folds** (`n_degenerate_folds = 0`).
2. **Crossed-book conditioning.** Every IC is now reported split by whether
   the consolidated book was crossed (a stale venue quote). On the equity
   book this changes little — the crossed fraction is ~1 % — but it is now
   visible: EQ02 IC 0.0309 pooled /
   0.0312 uncrossed, EQ03
   0.0299 / 0.0298, EQ12
   0.0298 / 0.0315. The
   promotion gates now read the UNCROSSED IC and its Newey-West t.
3. **Newey-West lag count** scales with the horizon
   (`L = ceil(h / bucket) + 1`) instead of a fixed L = 2, and is reported as
   `nw_lags`. The 5 s OFI alphas keep L = 2; the 15 m alphas move to L = 4.
   Re-derived t-statistics (uncrossed): EQ02 **8.47**,
   EQ03 **10.61**, EQ12
   **8.07**.
4. **Labels no longer span halts, stale-venue gaps or the dead zone before
   the close print** (API_FEATURES §6): a label is valid only with a
   tradable anchor, a tradable AND fresh forward mid, and no non-tradable
   sample inside (t, t+h]. 15-minute equity labels near the close no longer
   exist rather than being computed against a frozen mid, so every long-horizon
   number above rests on fewer, better rows.
5. **Feature ingestion.** The engine now consumes only events the order book
   APPLIED, so a gateway replay can no longer double-count trade flow, and a
   stale-venue recovery resets the rolling windows instead of producing a
   max-conviction signal from the gap move. OFI features on the bundled data
   are affected only marginally, but the mechanism the paper relies on is now
   measured on the post-validation stream.
6. **Execution model.** The research backtester now executes at
   `t + 1 s` of EVENT time (not "the next row", which is 3.3 s on equities
   and 15 s on FX), drops decisions older than 60 s and flattens at session
   boundaries, so no P&L figure credits an overnight gap to a 5-second alpha.
   Latency stress is reported on a time grid (100 ms / 500 ms / 1 s / 5 s).
   Every P&L number in §4.3-§4.5 above is superseded by the current
   `research/alpha_reports/REPORT.md`.
7. **Verdicts (current run)**: EQ02 ITERATE, EQ03
   ITERATE, EQ12 ITERATE. The paper's
   conclusion — statistically real OFI predictability that does not survive
   costs at 5-second turnover — is unchanged.

The multiple-testing ledger was **reset and re-derived** for this round: an
experiment is now identified by (alpha, kind, canonical configuration) and
re-running a script no longer increases the count, and one adaptive
deployment counts as ONE experiment instead of its 211 monitoring
evaluations. Every "1,224 experiments" / "19,347 experiments" figure in the
body above is superseded by **760 experiments
(65 distinct configurations)**: Bonferroni
per-test |t| **3.99**, expected max |t| under
the global null **3.64**. Reports now read the
ledger at render time, so a report and the ledger can never disagree again.

## Erratum / Update — 2026-09-20 (ledger denominator)

The multiple-testing ledger `research/experiments.json` moved on 2026-09-19
when the contract-driven `ExperimentRunner` (`python -m iap.research run`,
`research/experiments/<id>/`) registered five new experiments — EQ01 @ 1 s,
EQ03 @ 1 s and @ 5 s, EQ06 @ 1 s and @ 10 s, 21 looks each, kind
`experiment_runner`, deliberately **not** de-duplicated against the
`promotion_pipeline` entries even where the computation coincides, so the
denominator only grows. The 2026-09-06 erratum's **760 looks over 65 distinct
configurations (Bonferroni per-test |t| 3.99, expected max |t| under the global
null 3.64)** are superseded by **865 looks over 70 distinct configurations:
Bonferroni per-test |t| ≥ 4.02 (p ≤ 5.78e-05), expected max |t| ≈ 3.68**.
No verdict, IC, t-statistic or P&L figure in this paper changes; the five
new experiments are all ITERATE and net-negative at 1× costs, like every
alpha before them. The analysis above is left as written and the research
reports quote the ledger at their own render time (`ledger_n_at_report`).

## Erratum / Update — 2026-10-03 (v1.4.0: the equity flow now reaches the close)

**What changed in the data.** Up to v1.3.0 the generator stopped the equity
continuous flow 37.6-43.2 % of the way through each 6.5 h session and
emitted nothing more until the 20:00 close auction. v1.4.0 calibrates the
flow rate to the session (`equities.flow.calibration = "session"`, the
default; `"legacy_budget"` reproduces the v1.3.0 dataset byte for byte), so
every equity stream now trades to the close. The number of equity events is
about the same (105,282 / 104,468 on days 1 / 2), so the flow is spread over
the whole session and is sparser in time. Every figure in this paper is
measured on equities and therefore moved. This section supersedes the body
and the "current" figures quoted in the 2026-09-06 and 2026-09-20 errata
above; those are left as dated records.

**Data (§2), re-derived** from `data/normalized/qc_report.json` and
`data/features/features_summary.json`: 308,975 normalized events in total
(equities 105,282 + 104,468, FX 49,668 + 49,557, the FX files unchanged), 46
streams, 262 sequence gaps (396 missing events), 450 duplicates, 128
out-of-order arrivals, 173 invalid events. Instrument 1 has 14,609 feature
rows; its median inter-emission gap is 2.1 s and its mean row gap 3.2 s (the
body's ~0.9 s median was the spacing inside the compressed v1.3.0 window).
Equity rows are 3.1-3.3 s apart on average across the 11 instruments, and
0.1-0.7 % of them have a crossed consolidated book.

**What the 2026-09-06 erratum says about the session is no longer true of
the data.** Item 1 describes equity frames that "occupy 13:30-16:05 UTC of
each day plus a lone 20:00 close print", and item 4 speaks of "the dead zone
before the close print". Neither exists in v1.4.0. Folds are still quantiles
of the row index; all four are non-degenerate (EQ02: 31,980 / 31,735 /
31,891 / 31,887 test pairs). What thins the labels now is the freshness rule
`max(5 s, 2 x median quote gap)`: the equity median gap is about 2 s, so the
5 s floor binds, and a label whose forward mid is older than 5 s is invalid.
The valid fraction per equity instrument is 99.4-99.7 % at 5 s, 78.7-80.5 %
at 10 s, 75.3-77.2 % at 1 m and 44.7-63.4 % at 15 m. Item 6's "the next row,
which is 3.3 s on equities" was an average that included the dead zone;
3.1-3.3 s is now the real spacing.

**Headline statistics (§4.1), current** (`research/alpha_reports/REPORT.md`,
`EQ02.json`, `EQ03.json`, `EQ12.json`; the gates read the uncrossed IC and
its Newey-West t):

| alpha | horizon | IC | IC unc | NW t unc | Rank IC | hit | folds+ | leak | net P&L 1x | flips/h | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| EQ02 | 5s | 0.0253 | 0.0253 | 7.52 | 0.0337 | 0.524 | 4/4 | pass | -139,468 | 378 | ITERATE |
| EQ03 | 5s | 0.0190 | 0.0190 | 5.78 | 0.0214 | 0.513 | 4/4 | pass | -148,562 | 423 | ITERATE |
| EQ12 | 5s | 0.0263 | 0.0263 | 7.52 | 0.0339 | 0.525 | 4/4 | pass | -137,421 | 371 | ITERATE |

On v1.3.0 the uncrossed IC / t were 0.0312 / 9.58, 0.0298 / 10.57 and
0.0315 / 9.28. Fold ICs: EQ02 0.0323 / 0.0236 / 0.0244 / 0.0217; EQ03
0.0315 / 0.0132 / 0.0178 / 0.0148; EQ12 0.0320 / 0.0238 / 0.0271 / 0.0229.
Leakage (last fold, unshifted vs shifted by one row): EQ02 0.0217 vs 0.0092,
EQ03 0.0148 vs 0.0040, EQ12 0.0229 vs 0.0092. In the lifecycle registry
(`research/alpha_registry.json`) all three fail one gate only,
`net_pnl_after_costs`.

**Decay (§4.2), last fold:**

| alpha | 10ms | 50ms | 100ms | 500ms | 1s | 5s | 10s | 30s | 1m | 5m | 15m |
|---|---|---|---|---|---|---|---|---|---|---|---|
| EQ02 | +0.004 | -0.003 | +0.006 | +0.020 | +0.021 | +0.037 | +0.044 | +0.032 | +0.027 | +0.012 | +0.008 |
| EQ03 | +0.003 | -0.002 | +0.004 | +0.015 | +0.017 | +0.033 | +0.039 | +0.027 | +0.022 | +0.012 | +0.008 |
| EQ12 | +0.004 | -0.004 | +0.005 | +0.022 | +0.023 | +0.040 | +0.046 | +0.033 | +0.027 | +0.012 | +0.009 |

**Costs (§4.3).** Cost stress, last fold: EQ02 -69,109 / -139,468 / -280,186
at 0.5x / 1x / 2x over 6,437 trades; EQ03 -73,661 / -148,562 / -298,364 over
6,843; EQ12 -68,073 / -137,421 / -276,117 over 6,303. Held-out day 2: EQ02
gross +2,265, costs 368,947, net -366,682 over 16,789 trades; EQ03 +2,190 /
385,681 / -383,491 over 17,518; EQ12 +2,385 / 361,519 / -359,134 over
16,315. Costs are 152-176 times gross. The 1x losses are about twice the
v1.3.0 ones (EQ02 was -66,620 over 3,692 trades) because the alphas now
trade for the whole session.

**Latency (§4.4).** Event grid, last-fold IC at +0 / +1 / +5 rows: EQ02
0.0217 / 0.0092 / 0.0097; EQ03 0.0148 / 0.0040 / 0.0099. Time grid, net P&L
at 100 ms / 500 ms / 1 s / 5 s: EQ02 -149,377 / -144,931 / -139,468 /
-92,498 (6,892 / 6,688 / 6,437 / 4,361 trades); EQ03 -161,581 / -155,533 /
-148,562 / -98,064. Regime split: EQ02 +0.0300 high-vol / +0.0144 low-vol;
EQ03 +0.0190 / +0.0136; EQ12 +0.0316 / +0.0148.

**Capacity (§4.5).** Unchanged: $34.3-46.6M per single stock, $1.31B for the
index ETF.

**Multiple testing (§3.3).** `research/experiments.json` is now scoped by
dataset: the 1068 looks of the v1.3.0 dataset are kept and the regenerated
pipelines added 852, for 1920 looks over 139 entries at v1.4.0 (5156 over
216 since v1.5.0; see the 2026-10-04 update). At that count the expected
max |t| under the global null was 3.888 and the Bonferroni per-test
threshold 4.206
(REPORT.md was rendered mid-regeneration at 1740 looks and prints 3.86 /
4.18). EQ02, EQ03 and EQ12 clear both. On equities they are now the only
alphas that do: EQ01 (2.67), EQ05 (2.45) and EQ06 (3.68) cleared the v1.3.0
yardstick and are below the current one.

**Conclusions, re-checked.**

1. *OFI is a statistically real predictor at 5 s (positive OOS IC, sign
   consistency, leakage pass, selection-adjusted t).* **Holds, weaker.** IC
   0.019-0.026 at t 5.78-7.52, four positive folds of four, above both
   ledger thresholds. The ICs are 16-36 % lower and the t-statistics 19-45 %
   lower than on v1.3.0.
2. *Positive IC at every horizon from 500 ms to 15 m, nothing below
   100 ms, peak near 10 s.* **Holds**, with a caveat that is new. The peak
   is still at 10 s (0.039-0.046). But a 10 s label exists on only about
   80 % of rows and a 5 s label on over 99 %, so the 5 s and 10 s points are
   no longer measured on the same rows: the 10 s point is conditional on a
   quote arriving within 5 s of the horizon. The location of the peak
   should not be read more finely than "between 5 s and 30 s".
3. *Multi-level OFI (EQ03) earns the strongest and most stable statistics
   of the family.* **No longer holds.** EQ03 now has the lowest IC and the
   lowest t of the three (0.0190 / 5.78 against 0.0253 / 7.52 for L1 OFI).
   The body's statement that the three variants are indistinguishable in IC
   does not describe the current table either; no test of the difference
   was run.
4. *Liquidity conditioning (EQ12) adds nothing material.* **Holds.** EQ12
   and EQ02 have the same t (7.52) and ICs of 0.0263 and 0.0253.
5. *Cost-negative at every multiplier, including half costs; costs are
   about two orders of magnitude above gross.* **Holds.** See the figures
   above; all 24 alphas are net-negative at 1x.
6. *The size of the loss is a turnover problem (EQ08 contrast).* **Holds.**
   EQ08 trades 26 flips/h and loses -7,362, the smallest loss on the equity
   book; it is a REJECT (t -1.67, hypothesis sign not confirmed).
7. *One row of extra lag costs about a fifth of the IC, five rows about
   three quarters: the signal survives one emission but not five.* **No
   longer holds.** One row now costs 58 % (EQ02) and 73 % (EQ03) of the
   last-fold IC, and the +5 figure is not lower than the +1 figure. A row is
   now about 3 s, not about 1 s, so most of the loss falls inside the first
   row and the event grid no longer resolves a gradual decline. On the time
   grid the loss shrinks with latency only because fewer trades are made
   (EQ02 loses about 21.7 per trade at 100 ms and 21.2 at 5 s).
8. *The edge is present in both volatility regimes, slightly stronger in
   low-vol.* The first half **holds**; the second **no longer holds** (it
   is now about twice as large in high-vol for EQ02 and EQ12). Regime
   conditioning still offers no rescue from costs.
9. *Capacity is not the constraint.* **Holds**, unchanged.
10. *ITERATE, not PROMOTE; statistical significance is the cheapest gate
    and net-of-cost economics is the one that fails.* **Holds.** The cost
    gate is the only gate the three alphas fail.

One related result from the planted-signal study
(`research/power/POWER_REPORT.md`): on data generated with the v1.4.0 flow,
a planted informed-order-flow effect of the reference size is detected at
t >= 3 in 1 of 3 seeds, and one of half that size in none (on v1.3.0: 3 of 3
and 1 of 3). The validation chain has less power on the sparser flow, which
is consistent with the lower t-statistics above.

## Erratum / Update — 2026-10-04 (v1.5.0: the corrected research methods are the defaults)

**What changed.** Not the data: the dataset is the v1.4.0 one
(`data_version` 116b7787…). What changed is how it is scored. The eleven
corrected research methods that v1.3.0 added and v1.4.0 kept as selectable
alternatives are the defaults since v1.5.0 (`iap.validation.methods`, bundle
`"v2"`; PLATFORM_CONVENTIONS.md §13.6), and every old rule stays selectable
under a legacy name (bundle `"legacy_v1"`). The ones that bear on this
paper: the gate IC is the pooled uncrossed IC on rows with a valid label or
a label invalid for BLACKOUT alone, scored at its realised reopen return
(legacy: valid rows only); the gate t is the HAC t of the pooled slope
(legacy: the Newey-West t of within-bucket ICs); the PROMOTE t threshold is
the Bonferroni |t| at the run's recorded gate look count, never below 3.0
(legacy: 3.0 fixed); the backtest takes a position only when the expected
return exceeds the round-trip spread and fee, holds it for the label
horizon, caps fills at the displayed L1 size, trades only the rows the IC
scores and charges square-root impact (legacy: a position on every signal
sign, uncapped, linear impact); capacity is the edge breakeven (legacy: the
participation line); per-fold diagnostics and a bootstrap interval of the
net P&L are reported, and the recompute-from-raw-events leakage probe is in
the standard suite. `research/alpha_reports/run_all.py --methods legacy_v1
--out-dir <dir>` reproduces the v1.4.0 report, and
`python/tests/test_legacy_methods.py` compares that reproduction field by
field with the pinned v1.4.0 report of EQ03
(`tests/golden/alpha_report_EQ03_v1.4.0.json`). The figures of the
2026-10-03 update are therefore the `legacy_v1` figures of the current
dataset; this section supersedes them as the current ones.

**Headline statistics (§4.1), current** (`research/alpha_reports/REPORT.md`,
`EQ02.json`, `EQ03.json`, `EQ12.json`; keys `gate_ic`, `gate_tstat`,
`nw_tstat_uncrossed`, `trade_count_1x_cost`, `net_pnl_1x_cost`):

| alpha | horizon | IC | gate IC | gate t | t within-bucket | Rank IC | hit | folds+ | leak | trades 1x | net P&L 1x | flips/h | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EQ02 | 5s | 0.0251 | 0.0251 | 7.17 | 7.61 | 0.0331 | 0.524 | 4/4 | pass | 0 | 0 | 378 | ITERATE |
| EQ03 | 5s | 0.0189 | 0.0189 | 5.16 | 5.85 | 0.0211 | 0.513 | 4/4 | pass | 0 | 0 | 423 | ITERATE |
| EQ12 | 5s | 0.0261 | 0.0261 | 7.20 | 7.62 | 0.0335 | 0.524 | 4/4 | pass | 0 | 0 | 371 | ITERATE |

The PROMOTE t threshold is 4.365 (`gates.min_nw_tstat`), the Bonferroni |t|
at the run's gate look count of 3,936 (`ledger_looks`). The headline ICs
the gate does not read: vol-scaled 0.0336 / 0.0266 / 0.0351, per-instrument
mean 0.0335 / 0.0265 / 0.0350 (`oos_ic_vol_scaled_uncrossed`,
`oos_ic_instrument_mean_uncrossed`). The gate IC on valid rows only
(`gate_ic_valid_only`) is 0.0253 / 0.0190 / 0.0263, the v1.4.0 gate IC; the
default row policy adds 275 / 275 / 268 BLACKOUT rows scored at the reopen
return (`n_blackout_rows_scored`). Fold ICs: EQ02 0.0317 / 0.0228 / 0.0247 /
0.0218; EQ03 0.0308 / 0.0121 / 0.0181 / 0.0157; EQ12 0.0313 / 0.0230 /
0.0276 / 0.0231. Leakage (last fold, unshifted vs shifted by one row): EQ02
0.0218 vs 0.0091, EQ03 0.0157 vs 0.0042, EQ12 0.0231 vs 0.0090; the
recompute probe passes on its three anchors for all three. `promote_gates`
has one false entry for each of the three, `cost`. In the lifecycle
registry (`research/alpha_registry.json`) each now fails two gates,
`net_pnl_after_costs` (value 0, which is not above 0) and `capacity` (0
against a threshold of 1,000,000 USD); on v1.4.0 it was the cost gate
alone.

**Decay (§4.2), last fold** (`decay_ic_by_horizon`):

| alpha | 10ms | 50ms | 100ms | 500ms | 1s | 5s | 10s | 30s | 1m | 5m | 15m |
|---|---|---|---|---|---|---|---|---|---|---|---|
| EQ02 | +0.004 | -0.003 | +0.006 | +0.020 | +0.021 | +0.037 | +0.044 | +0.031 | +0.027 | +0.014 | +0.010 |
| EQ03 | +0.003 | -0.002 | +0.004 | +0.015 | +0.017 | +0.033 | +0.040 | +0.027 | +0.023 | +0.015 | +0.005 |
| EQ12 | +0.004 | -0.004 | +0.005 | +0.022 | +0.023 | +0.040 | +0.046 | +0.032 | +0.028 | +0.014 | +0.010 |

**Costs (§4.3).** Under the cost-aware policy none of the three alphas
makes a trade at 1x costs on any of the four folds, or at 2x on the last:
the forecast never exceeds the round-trip spread and fee. Net P&L at 1x is 0 on the last
fold and 0 pooled over the four test segments, with a bootstrap interval of
[0, 0] (`net_pnl_bootstrap`); 0 of 4 folds survive 1x costs
(`fold_diagnostics`). A net of 0 does not pass the cost gate, which needs
net P&L above zero. At 0.5x costs EQ02 and EQ12 make 16 trades each and
lose 197 USD; EQ03 makes none. Held-out day 2: no trade, 0 gross, 0 costs
for all three. The v1.4.0 figures (EQ02 -139,468 over 6,437 trades at 1x;
costs 152-176 times gross on day 2) are what the legacy `"sign"` policy
produces: it holds a position on every signal sign and pays the spread on
every flip.

**Latency (§4.4).** Event grid, last-fold IC at +0 / +1 / +5 rows: EQ02
0.0218 / 0.0091 / 0.0097; EQ03 0.0157 / 0.0042 / 0.0099; EQ12 0.0231 /
0.0090 / 0.0105. Time grid (stress grid version 2), net P&L at 100 ms /
500 ms / 1 s / 5 s: 0 at every point for all three, with no trade. Regime
split: EQ02 +0.0301 high-vol / +0.0147 low-vol; EQ03 +0.0204 / +0.0139;
EQ12 +0.0317 / +0.0151.

**Capacity (§4.5).** The default capacity is the edge breakeven: the size
at which the realised gross edge per round trip of the 1x backtest equals
its cost, capped at the participation line. It is 0 for all three, because
they do not trade. The participation line the body quoted is unchanged and
is still reported (`capacity_proxy_usd_by_instrument`: $34.3-46.6M per
single stock, $1.31B for the index ETF).

**Multiple testing (§3.3).** `research/experiments.json` holds 5156 looks
over 216 entries: the 1068 looks of the v1.3.0 dataset, the 852 recorded on
the v1.4.0 dataset under the legacy methods, and 3,236 recorded under the
default methods, 760 of them by the signal-combination report (a validation with its backtest debits 84 per alpha under
the defaults and 28 under the legacy methods).
Expected max |t| under the global null is 4.135 and the Bonferroni per-test
threshold 4.424. The alpha report was judged at the count recorded when it
ran, 3,936: threshold 4.365, selection yardstick 4.07. EQ02, EQ03 and EQ12
clear all four numbers. On equities they are the only alphas with a
positive gate t that clears the threshold; EQ06 is at 4.36, above the
yardstick and 0.005 below the threshold.

**Conclusions, re-checked under the default methods.**

1. *OFI is a statistically real predictor at 5 s.* **Holds, and against a
   stricter test.** Gate t 7.17 / 5.16 / 7.20 against a threshold of 4.365
   (v1.4.0: 7.52 / 5.78 / 7.52 against 3.0). The pooled-slope t is 6-12 %
   lower than the within-bucket t it replaces as the gate statistic. The
   vol-scaled and per-instrument ICs are larger than the pooled one and
   have its sign.
2. *Positive IC from 500 ms to 15 m, nothing below 100 ms, peak near 10 s.*
   **Unchanged**, with the caveat of the 2026-10-03 update (the 10 s point
   is measured on about 80 % of rows).
3. *Multi-level OFI (EQ03) earns the strongest statistics.* **Still does
   not hold**: EQ03 has the lowest gate IC and gate t of the three.
4. *Liquidity conditioning (EQ12) adds nothing material.* **Unchanged**:
   7.20 against 7.17.
5. *Cost-negative at every multiplier; costs are about two orders of
   magnitude above gross.* The economic conclusion **holds and is
   stronger**; the figures behind it **do not carry over**. The corrected
   backtest does not show a loss of that size, because it does not take the
   trades: the expected return of the signal never exceeds one round trip
   of spread and fee at 1x. The alphas are unharvestable because there is
   no trade worth making, and the six-figure losses measured what trading
   every sign flip costs. No fold and no cost multiplier shows a positive
   net P&L.
6. *The size of the loss is a turnover problem (EQ08 contrast).* **Not
   measurable under the defaults**: EQ02 and EQ08 both make no trade at 1x.
   It remains true of the legacy policy, and it is the reason that policy
   was replaced.
7. *One row of extra lag costs about a fifth of the IC.* **Still does not
   hold**, as on v1.4.0: one row costs 58 % (EQ02) and 73 % (EQ03) of the
   last-fold IC. The time grid has no P&L to read.
8. *The edge is present in both volatility regimes.* **Unchanged** from the
   2026-10-03 update (about twice as large in high-vol for EQ02 and EQ12).
9. *Capacity is not the constraint.* **No longer holds as stated.** By the
   participation line it is unchanged. By the default edge-breakeven
   measure the capacity is 0, and the registry's capacity gate fails.
10. *ITERATE, not PROMOTE; net-of-cost economics is the gate that fails.*
    **Holds.** `cost` is the only PROMOTE gate the three alphas fail.

The planted-signal study (`research/power/POWER_REPORT.md`, report version
2, scored under the default methods) gives the same rates as before for
informed order flow: detected at t >= 3 in 1 of 3 seeds at the reference
size, in none at half of it and in 3 of 3 at twice it, by the pooled-slope
t and by the within-bucket t alike. No cell has a bootstrap interval of the
net P&L above zero.
