# Predictability of Order-Flow Imbalance in Liquid Equity Markets: Significant, Stable, and Still Not Worth Trading

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
  `configs/execution.json`) charges half-spread + $0.003/share taker fee +
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
| cost model | `python/src/iap/backtest/costs.py`, `configs/execution.json` |
| stress axes | `python/src/iap/validation/stress.py` |
| data volumes and QC | `data/normalized/qc_report.json` |
| generator design | `python/src/iap/marketdata/generator.py` |
