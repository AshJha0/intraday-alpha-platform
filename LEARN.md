# LEARN.md — A Guided Tour of the Intraday Alpha Platform

This document walks through the entire platform the way a textbook would:
concepts first, then how this repository implements them, always with the
repo's *real, verified numbers* as worked examples. Everything cited here can
be reproduced from the committed code and seeds — that reproducibility is
itself one of the lessons.

Reading order matters less than you'd think; each section stands alone but
cross-references the others. If you only have an hour, read §6 (honest alpha
research), §7 (ML and meta-labeling), and §12 (golden parity) — they carry
the platform's central ideas.

Contents:

1. [Market microstructure primer](#1-market-microstructure-primer)
2. [The synthetic market-data generator](#2-the-synthetic-market-data-generator)
3. [Order-book reconstruction semantics](#3-order-book-reconstruction-semantics)
4. [The feature factory](#4-the-feature-factory)
5. [Labels: defining what "prediction" means](#5-labels-defining-what-prediction-means)
6. [Alpha research done honestly](#6-alpha-research-done-honestly)
7. [ML with meta-labeling](#7-ml-with-meta-labeling)
8. [Portfolio construction under constraints](#8-portfolio-construction-under-constraints)
9. [Fail-closed risk design](#9-fail-closed-risk-design)
10. [Execution algorithms and queue-position modeling](#10-execution-algorithms-and-queue-position-modeling)
11. [TCA: decomposing execution cost](#11-tca-decomposing-execution-cost)
12. [Cross-language golden parity as an engineering discipline](#12-cross-language-golden-parity-as-an-engineering-discipline)
13. [Latency economics](#13-latency-economics)
14. [Adaptability: evolving faster than you decay](#14-adaptability-evolving-faster-than-you-decay)
15. [Twelve pitfalls this platform is built to avoid](#15-twelve-pitfalls-this-platform-is-built-to-avoid)
16. [Ten interview questions (with answers from this repo)](#16-ten-interview-questions-with-answers-from-this-repo)
17. [Further reading](#17-further-reading)

---

## 1. Market microstructure primer

### 1.1 The limit order book

Modern electronic markets are driven by a **central limit order book**: a
two-sided priority queue of resting limit orders. Bids (side 0 in this repo)
wait to buy; asks (side 1) wait to sell. Within a price level, orders queue in
**FIFO (price-time) priority**: the first order to arrive at a price is the
first to be filled when an aggressive order trades against that level.

Three views of the book, in increasing fidelity:

- **L1** — best bid/ask price and size only (the "touch" or BBO);
- **L2** — aggregated depth per price level (this repo tracks the top 10);
- **MBO** ("market by order") — every individual resting order with its
  identity and queue position. Only MBO lets you reason about *your own*
  queue position, which is why serious passive execution research needs it.

The bundled equity feed is MBO; the bundled FX feed is quote-driven (L1
per venue) — a deliberate contrast, because §10 and paper 2 both show that
queue-dynamics research is *structurally impossible* on an L1 book.

### 1.2 Events, not bars

Everything in this platform is **event-driven**. The canonical `MarketEvent`
(API_CORE.md §1) carries 12 fields — ids, timestamps (`exchange_ts`,
`receive_ts`, both int64 nanoseconds), a per-stream `sequence`, an event type
(ADD/MODIFY/CANCEL/EXECUTE/TRADE/QUOTE/SNAPSHOT/STATUS/HEARTBEAT), integer
`price_ticks` and `qty`. Prices are **never floats on a contract**: a real
price is `price_ticks * tick_size`, with tick sizes coming from reference
data. This is conventions §1 and it is load-bearing — integer state is what
makes cross-language *exact* equality possible (§12).

Time-based bars (1-minute OHLC, etc.) appear only far downstream (covariance
estimation, §8). All alpha research runs in **event time**: labels, windows
and validation splits are defined on `exchange_ts`, and features update when
events arrive, not when a clock ticks.

### 1.3 Microprice

The mid price `(Pb + Pa) / 2` ignores a crucial signal: *size imbalance at
the touch*. If 900 shares are bid and 100 offered, the next mid move is more
likely up. The **microprice** weights each side by the *opposite* size:

```
microprice = (Pb·Qa + Pa·Qb) / (Qb + Qa)
```

so a heavy bid pulls the microprice toward the ask. The deviation
`(microprice − mid)/mid` (registry feature `micro_mid_dev_bps_v1`) is the raw
signal of alpha **EQ01** and **FX01**. On this repo's data, the same formula
is a real (if small) predictor on the equity MBO book — OOS IC 0.0219,
t = 3.81, sign-stable, hypothesis-confirmed, verdict ITERATE (like every
alpha here it still loses money net of costs) — and carries *no stable
signal* on the synthetic FX quote book (FX01 REJECT, IC −0.0150). Same math,
different market structure, opposite verdict: a good first lesson in why
microstructure context matters.

### 1.4 Order-flow imbalance (OFI)

Price moves when net demand at the touch changes. OFI (Cont, Kukanov &
Stoikov's classic construction) measures that: at each book update, compute
the change in displayed bid depth minus the change in displayed ask depth
over the best K levels, and sum over a window:

```
e(K) = Δbid_depth(K) − Δask_depth(K)      per book refresh
ofi_lK_wW = Σ e(K) over the window (t−W, t]
```

The repo computes this exactly, in integers, for K ∈ {1,3,5,10} and
W ∈ {1s,5s,30s} (API_FEATURES.md §3). OFI drives alphas EQ02 (L1) and EQ03
(multi-level). Paper 1's finding is the honest headline: OFI *is*
statistically predictive here (pooled OOS IC 0.0274/0.0256 with t of
5.89/7.24, every non-degenerate walk-forward fold positive) and *still not
tradable* — at 246–277 signal flips per hour the strategies pay the spread
so often that costs exceed gross alpha by roughly two orders of magnitude.

### 1.5 Queues, and what you can and cannot observe

For a passive order, the economic questions are: how much size is **ahead of
me** in the queue, how fast does it deplete (executions ahead of me) or
evaporate (cancels ahead of me), and — the dark side — *when I do get
filled, is it because the price is about to move against me?* That last
phenomenon is **adverse selection**: passive fills cluster exactly when
informed flow trades through the book.

MBO data lets you model queue position deterministically (§10 documents this
repo's pinned model). L1 quote data does not: you see total size at the best,
not composition or your position within it. Paper 2 demonstrates this limit
concretely — EQ05 (queue depletion/replenishment) is only definable on the
equity MBO feed.

---

## 2. The synthetic market-data generator

### 2.1 Why synthetic data

Real tick data is licensed, enormous, and unshippable in an educational
repository. The alternative chosen here (`python/src/iap/marketdata/generator.py`)
is a fully **seeded synthetic generator**: identical seed ⇒ bit-identical
output files, which turns the entire downstream stack — features, alphas, ML,
TCA — into a deterministic function you can regression-test.

All randomness flows through one pinned RNG, **SplitMix64** (conventions §3),
implemented identically in all four languages with known-answer tests
(`tests/golden/splitmix64.json`). This is why the C++ execution simulator can
promise bit-identical fills for a given seed and why golden vectors can be
regenerated at all.

### 2.2 What it generates

From `configs/generator.json` (seed 20260829, 2 sessions):

- **Equities** (11 instruments: 10 index constituents + 1 ETF): MBO streams
  with regime-switching volatility (two sigma states, switch probability
  0.004), self-exciting order flow (excitation kick 1.4, decay 0.82 — bursts
  cluster like real flow), a real internal FIFO book that ADD/MODIFY/CANCEL/
  EXECUTE events act against, opening/closing auction prints, one scripted
  halt (SYN.EQ.007, session 0, 300 s), and SNAPSHOT-recovered sequence gaps.
- **FX** (8 G10 pairs, EUR/USD … EUR/GBP): QUOTE+TRADE events across three
  venues (LP1/LP2/PRI) with per-venue spreads and latency profiles.
- **One shared efficient price per instrument.** All venues of an instrument
  quote around a single efficient-price process, so the consolidated
  multi-venue book stays coherent by construction. This is a deliberate
  redesign: an earlier generator gave venues independent price noise, which
  left the merged equity book crossed ~95% of the time and contaminated the
  ML research — the full story, and what remains of the artifact, is §7.2.
- **Injected anomalies** for QC testing: sequence gaps, duplicates,
  out-of-order events, invalid messages, timestamp violations — disabled for
  golden vectors, enabled for the bundled dataset so the normalizer has real
  work to do. The QC report (`data/normalized/qc_report.json`) shows the
  audit: 310,782 raw events in, 310,159 out, with 322 gaps, 450 duplicates,
  182 out-of-order, 173 invalid, 81 clamped timestamps — every one counted.

### 2.3 Its limits — read this before believing any result

The generator's mid is **strongly mean-reverting** around its regime
process. Consequences you will see all over the research reports:

1. **Reversion alphas look great, momentum-family hypotheses often fail.**
   Several alphas ship with `hypothesis_confirmed = false` — the fitted sign
   contradicts the stated economic rationale — and are therefore barred from
   PROMOTE no matter how large the IC (e.g. FX09, IC 0.113, t 14.3, still
   capped at ITERATE).
2. **The consolidated multi-venue book can still cross occasionally**
   (negative spread). Under the shared-efficient-price design the merged
   *equity* book is crossed at only 0.79% of ML decision rows — but the FX
   consolidated book is crossed 29.5% of the time, because aggregated LP
   quotes go stale between venue updates (a real phenomenon of FX
   aggregation, amplified here by the synthetic update cadence). This
   residual is disclosed, measured, and produces the ML artifact story of
   §7.2 — which is also the story of how the original ~95% equity version
   of this artifact was found and fixed.
3. **Cross-venue lead-lag truly is ≈ 0 by construction** (shared-mid design),
   so paper 3's null result is the *correct* answer, and the paper's value is
   the identification diagnostics, not the effect size.
4. **No adverse selection for resting counterparties**: post-fill markouts
   are flat from 100 ms to 10 s (§11) because the mid reverts. Flagged as an
   artifact in the TCA report, not presented as a market truth.

The platform treats these as features of the exercise: a pipeline that
truthfully reports "this dataset does not contain that effect" is exactly
what you want pointed at real data later (spec §32).

---

## 3. Order-book reconstruction semantics

Book reconstruction sounds trivial until you pin every edge case. This repo
pins them all in conventions §4 / API_CORE.md §4, because four languages must
agree *exactly*:

- **ADD** — join the FIFO tail of the (side, price) level. If the price
  crosses the opposite best, it is *marketable*: it executes against the
  opposite FIFO from the head of the best level (partial fills reduce the
  head; emptied orders are removed), and any remainder posts. Note the subtle
  consequence: an internal match generates **no EXECUTE events** on the wire
  — the execution simulator has to reverse-engineer those consumptions for
  queue accounting (§10).
- **MODIFY** — quantity change only. Decrease keeps queue position; increase
  moves the order to the level tail (you lose priority when you upsize —
  matching real exchange semantics). The event's price field is ignored.
- **CANCEL** — remove by order_id; unknown ids are dropped and counted, never
  fatal.
- **EXECUTE** — fills the **referenced** order, by `order_id` (valid feeds
  always reference the FIFO head of its level, but the book applies whatever
  order the event references); partial fills keep position, an order is
  removed at qty 0. Does *not* touch `trade_flow`.
- **TRADE** — updates cumulative signed `trade_flow` only (+qty for buy
  aggressor, −qty for sell). Trade prints and book mutations are separate
  event types with separate meanings.
- **QUOTE** (FX) — replaces the venue's entire side at L1: the quote-driven
  world in one rule.
- **SNAPSHOT** — a burst of records (one per resting order, bids then asks,
  best→worst, FIFO within level, with a countdown in `trade_id`); the first
  record clears both sides, the last clears the `stale` flag. **Broken-burst
  rule** (conventions §4): a sequence gap arriving *inside* an active burst
  marks that burst BROKEN — it still ends at its `trade_id == 0` record but
  does **not** clear `stale`; only a later complete, gap-free burst does.
- **Side domain** — for side-indexed event types (ADD/QUOTE/SNAPSHOT/TRADE),
  `side` must be BID=0 or ASK=1. An event with side > 1 is malformed:
  dropped and counted (`invalid_side_dropped`) through the normal drop path
  — never an exception mid-stream — *after* its sequence number is consumed.

**Sequencing** is checked before dispatch: a duplicate (sequence ≤ last) is
dropped and counted; a gap marks the book `stale = true`, and while stale
only SNAPSHOT/STATUS/TRADE/HEARTBEAT apply. Stale books poison nothing
downstream because the feature engine excludes stale venues from its merged
view and marks affected features invalid (§4.3) — the fail-closed idea (§9)
appearing already at the data layer.

**Checkpoints** serialize the full book (levels in sorted order, FIFO lists,
counters — including `arrival_order`, the `snapshot_broken` flag and the
`invalid_side_dropped` counter, so the global resting-order arrival order
round-trips exactly) such that restore-and-continue is bit-identical to
never having stopped — verified in tests, and the foundation of replayable
backtests.

Derived state after *every* event: best bid/ask with sizes, top-10 depth and
order counts per side, signed trade flow, last sequence, timestamps. The
golden file `expected_book_states.json` pins this state after events
100/500/1000/1500/2000 of the equity vector — exact integers, no epsilons.

---

## 4. The feature factory

### 4.1 Design: a registry, not a script

The classic failure mode of feature engineering is a thousand ad-hoc
transformations, each computed slightly differently in research and
production. This platform's answer (conventions §6, API_FEATURES.md) is a
**pinned registry**: `data/reference/feature_registry.json` lists all **205**
features with name, family, version, parameters, doc string and dependencies,
generated from pinned parameter grids in `python/src/iap/features/`:

| family | count | examples |
|---|---|---|
| price | 30 | `ret_log_1s_v1`, vol-adjusted returns, acceleration, residual returns |
| micro | 52 | `microprice_v1`, `micro_mid_dev_bps_v1`, spreads, imbalance grids |
| flow | 47 | `ofi_l5_w1s_v1`, signed volume, trade imbalance, cancel intensity |
| liquidity | 13 | depth aggregates, participation, resiliency |
| vol | 13 | `rvol_w1m_v1`, vol acceleration, range, jump indicators |
| tod | 11 | minute-of-day normalized volume/spread/vol/depth |
| xasset | 11 | index/ETF and EUR/USD lead-lag, beta residuals |
| venue | 10 | venue imbalance, staleness, toxicity proxies |
| regime | 10 | trend/reversion state, high/low vol, `vol_regime_ratio_v1` |
| exec | 8 | expected fill probability, impact, alpha decay, urgency |

Feature identity includes a **definition version** (`_v1` suffix). The
registry's canonical-JSON SHA-256 (`585dd7b9…`) is the
`FeatureVector.feature_version`: change any definition and every downstream
artifact visibly changes version. That hash appears in golden files, model
manifests and feature parquet output — the versioning story is end to end.

A **native core set of 40** features (OFI, imbalance, microprice/spread,
depth, signed volume, trade imbalance, realized vol, returns — exact formulas
in API_FEATURES.md §3) is implemented in C++, Rust and Java with identical
semantics, golden-tested at 1e-9. The remaining 165 are Python-owned research
features until their family is ported.

### 4.2 Event-driven computation, pinned to the nanosecond

The engine consumes the normalized stream and, per instrument, owns a
consolidated book plus rolling state. The rules that make four
implementations agree (API_FEATURES.md §2) are worth internalizing:

- **Book refresh only after book-touching events** — and *not* after
  interior SNAPSHOT records, so a half-built book never contaminates rolling
  statistics.
- **Windows are half-open event-time intervals** `(t − w, t]`: a sample at
  exactly `t − w` is out; one at `t` is in. Off-by-one window conventions are
  a classic source of silent cross-implementation drift.
- **Integer sums stay integer.** OFI and signed volume accumulate exactly;
  only genuinely real-valued inputs (log returns) live in floating point.
- **Mid-change sampling**: return/vol features sample only when the mid
  actually changed; depth/spread averages sample at every valid refresh; OFI
  samples at every refresh regardless. Which statistic samples when is
  pinned per feature.
- **History lookups are last-observation-carried-forward** ("latest sample
  at-or-before `t − h`"), never interpolated.

### 4.3 Validity is a first-class output

Every `FeatureVector` carries a **validity bitset** parallel to the values:
a feature is invalid during warmup (`t − first_event < w`), when the merged
book is not `book_ok` (a side missing, or all venues stale), or when an input
is undefined (zero denominator, no history sample, no trades in a trade
window). The contract is brutal and simple: **NaN never appears with
valid = true**. Downstream, alphas turn invalid inputs into
`confidence = 0, expected_return = 0.0` — invalid data yields no signal, not
a garbage signal. This is the fail-closed philosophy (§9) applied to
research data.

On the bundled dataset the pipeline emits 208,437 vectors (100 ms cadence,
310,159 events, ~56 s in the Python reference; the C++ port does the same
state updates at ~450 ns/event).

---

## 5. Labels: defining what "prediction" means

Before any alpha claim, pin what is being predicted. `iap.labels` defines
event-time forward returns at 11 horizons {10ms … 15m} over the
per-instrument mid series:

```
mid_label(t, h)  = m(t+h)/m(t) − 1                              # frictionless
cost_label(t, h) = ((m(t+h) − hs(t+h)) − (m(t) + hs(t))) / m(t) # buy at ask, exit at bid
```

where `m`/`hs` are the prevailing mid/half-spread *at-or-before* the query
time. Three details carry most of the integrity:

1. **At-or-before, never after**: the anchor uses only events with
   `ts ≤ t`. The same rule is reused verbatim by TCA benchmarks (§11) —
   one lookahead convention for the whole platform.
2. **Validity requires observation**: a label is valid only if the stream
   was actually observed through `t + h`; nothing is extrapolated past the
   session end.
3. **Both raw and cost-adjusted labels exist** (spec §13). The cost label
   embeds the round-trip spread — which is exactly what makes it dangerous
   as an ML target, as §7 shows.

The alignment is defended by tests: shifting either series by one event must
break the feature/label alignment — the "shift-by-one" leakage test that
every alpha and model run executes automatically.

---

## 6. Alpha research done honestly

This is the platform's core curriculum. The framework
(`python/src/iap/validation/`) runs every one of the 24 flagship alphas
through an identical gauntlet; `research/alpha_reports/REPORT.md` is the
result, and every number below is from that committed report.

### 6.1 The interface forces a hypothesis

An alpha here is a class extending `AlphaModel`
(`python/src/iap/alpha/base.py`) with a pinned `alpha_id`, declared
`features` (registry names it is allowed to read), a pinned label `horizon` —
and a class docstring that **must contain an `Economic rationale:` section,
enforced at class-definition time**. An alpha without a stated economic
hypothesis cannot exist in this codebase; `TypeError` at import. The declared
feature list is doubly load-bearing: the harness verifies `score()` output is
unchanged when every *other* column is masked, which simultaneously catches
undeclared dependencies and label-column leakage.

Fitting is deliberately humble: the workhorse `LinearAlpha` z-scores an
*oriented* raw signal and fits one OLS slope of the pinned-horizon mid label
on the clipped z. The fitted slope is free-signed; `hypothesis_confirmed`
records whether the data agreed with the stated direction. Ports never fit —
they load `configs/strategies/alpha_params.json` and treat every number as
opaque (API_ALPHA.md).

### 6.2 Walk-forward with purging and embargo

Random train/test splits are wrong for overlapping time-series labels:
a 5 s-horizon label computed at the end of a training fold *contains* future
returns that leak into the test fold. The framework uses **4 expanding
walk-forward folds**, and at every train/test boundary:

- **purging** removes training rows whose label window overlaps the test
  period (purge width = the alpha's own label horizon);
- an **embargo** of 60 s further separates the sets, absorbing serial
  correlation the purge does not cover.

A fresh model instance is fitted per fold — no state bleeds across folds.

### 6.3 Leakage tests are automatic, not aspirational

Two leakage tests run for every alpha, every time: the masked-column guard
(§6.1) and the **shift-by-one test** — shifting features one event forward
relative to labels must destroy the IC. An alpha that survives its own
shifted version is reading the future somewhere; verdict REJECT, always,
regardless of any other statistic.

### 6.4 What gets measured

Per alpha: OOS IC (Pearson, pooled over folds), Rank IC, a Newey–West
t-statistic (serial-correlation-robust), hit rate, fold sign consistency,
**decay curve** across all 11 horizons, turnover (signal flips/hour),
capacity proxies, cost stress at ×{0.5, 1, 2} modeled costs, latency stress
at +{0, 1, 5} events of staleness, and a high/low-vol regime split.

### 6.5 The gates, and the honest scoreboard

Pinned promotion gates (spec §20):

- **PROMOTE**: leakage pass ∧ OOS IC ≥ 0.01 ∧ NW t ≥ 3.0 ∧ fold
  consistency ≥ 0.7 ∧ hypothesis confirmed ∧ **net P&L > 0 at 1× costs**.
- **ITERATE**: leakage pass ∧ IC ≥ 0.005 ∧ t ≥ 1.5.
- **REJECT**: otherwise (always, on leakage failure).

Result on the bundled data: **0 PROMOTE / 12 ITERATE / 12 REJECT** — every
one of the 24 alphas is net-negative at 1× modeled costs, so nothing clears
the last gate. Worked examples, straight from the master table:

- **EQ03 (multi-level OFI) — ITERATE, the platform's signature finding.**
  IC 0.0256, t 7.24, every non-degenerate fold positive, leakage-clean… and
  net **−199,913** at 1× costs, because 277 flips/hour means paying the
  spread constantly. Statistically real, economically dead on this data
  (paper 1).
- **EQ08 (VWAP/mid deviation) — REJECT, instructively.** Only 11 signal
  flips/hour, so it loses the least money of any equity alpha (−7,543 at
  1×) — but its pooled NW t is −0.14 and its fitted sign contradicts the
  stated rationale (`hyp = no`). Cheap to trade is not the same as real.
- **FX09 (vol-regime reversion) — ITERATE despite the best FX statistics
  in the study.** IC 0.113, t 14.3, regime-split IC 0.176 in high-vol —
  but `hyp = no` (the fitted sign contradicts the stated rationale) and it
  loses 670k at 1× costs. The gate that blocks it (`hypothesis_confirmed`)
  is the codified version of "a fit you can't explain is a fit you can't
  trust."
- **FX01 (microprice on the FX quote book) — REJECT.** IC −0.015, 1/4
  positive folds. Compare EQ01, the same formula on the MBO book: ITERATE,
  sign-stable and hypothesis-confirmed. Market structure decides (paper 2).

### 6.6 Multiple testing: counting your looks

Every walk-forward evaluation, decay horizon, stress variant, and backtest is
recorded in an append-only ledger (`research/experiments.json`) — 21 looks
per alpha per run plus a one-time 216-look design scan gave **1,224
experiments** at the committed promotion run; the adaptive-deployment study
(§14) then added 12,082 counted looks of its own across its two committed runs — 6,041 per run (every drift evaluation,
rolling-IC reading, refit and final backtest), bringing the ledger to
**13,306**. The ledger translates that into a selection yardstick:
Bonferroni per-test threshold |t| ≥ 4.62, and an expected **max |t| ≈ 4.36
under the global null** across the ledger. Meaning: an alpha waving t = 1.7
(FX04) is *consistent with pure selection* over this many trials, and the
report says so in print. Most quant shops track this informally at best; here
it is a serialized artifact that only ever grows.

### 6.7 Cost reality

The cost model (`configs/execution.json`) charges half-spread + fees
(mirroring venue configs) + linear impact per trade, and the day-2
out-of-sample backtest uses day-1-fitted parameters — the exact parameters
serialized for the production ports. The equal-weight ensembles finish
negative (equity −93,359; FX −43,499 net). The report's Sharpe column is
labeled as an event-time research yardstick, not a production claim. Honesty
in the artifacts, not just the prose.

---

## 7. ML with meta-labeling

`research/ml_reports/run_ml.py` implements spec §14; `ML_REPORT.md` is the
committed result: 208,334 valid rows, 51 curated predictors across all 10
families, target `label_cost_5s`, walk-forward with 60 s embargo and 5 s
purge.

### 7.1 The gate: advanced models must earn the right to run

Rule: **trees and the MLP run only if the best linear baseline achieves
positive mean OOS IC**. Simple models establish whether signal exists;
capacity-rich models then refine it. In the committed run elastic net scored
mean OOS IC 0.6542 → gate PASSED → XGBoost, LightGBM and an MLP ran
(LightGBM won at 0.7037). Had the target been the mid-to-mid label, the gate
would have correctly blocked them.

### 7.2 The crossed-book artifact: found, fixed, and honestly residual

An IC of 0.70 at 5 seconds would be the greatest alpha ever recorded. It is
nothing of the sort — and this section is now a case study in *finding and
fixing a data-generation artifact*, told in two acts.

**Act one: the discovery.** An earlier version of the generator gave each
venue independent price noise around a shared mid. Merging venues then
produced a consolidated equity book that was **crossed (negative spread)
~95% of the time** — so the cost-adjusted ML target, which embeds the
round-trip spread, was dominated by a large, observable, trivially
predictable spread component (corr(spread, target) ≈ −0.995 at the time).
Models scored superbly by predicting the *cost* component while knowing
nothing about direction, inflating every headline IC in the ML report.

**Act two: the fix, and what honestly remains.** The generator was
redesigned so that all venues of an instrument quote around **one shared
efficient price** (§2.2), and the datasets, goldens and reports were
regenerated. The consolidated equity book is now crossed at only **0.79%**
of ML decision rows. The residual — disclosed, not hidden — is FX: the
consolidated FX book is still crossed **29.5%** of the time, because
aggregated LP quotes go stale between venue updates (a real phenomenon of
FX aggregation, amplified here by the synthetic update cadence). The
spread component therefore still drives the headline numbers:
corr(spread, target) = −0.938, and the winning LightGBM IC of 0.70 remains
mostly spread prediction.

Crucially, none of this was ever **leakage** — the shift-by-one test passes
throughout, because the spread at decision time legitimately is in the
information set. It is a *target-construction artifact* interacting with a
*generator artifact*. The honest directional measure — IC against the
mid-to-mid label — is ~0 to slightly negative for every model:
**no exploitable 5 s directional signal exists in this dataset**, exactly
what a near-random-walk generator should yield. The economics agree: under
the conservative cost model (realized costs floored at zero — you are never
paid to cross a crossed synthetic book), every model sits near 0 bps/signal
(LightGBM: −0.098), while "label-exact" economics still show +1.2 bps/signal
of residual book-artifact. The report instructs the reader to trust only
the conservative column.

Two lessons now. First, the old one: **a model can ace its target and tell
you nothing about alpha — always decompose what the target actually
contains.** Second, the new one: **when the decomposition points at the
data-generating process, fix the generator, regenerate everything, and
publish the before/after** — the artifact shrank from ~95% to 0.79% on
equities precisely because the honest report made it impossible to ignore.

### 7.3 Meta-labeling: predicting *whether to act* on a signal

Meta-labeling (López de Prado) separates *direction* (primary model) from
*conviction* (secondary model): the meta-model predicts whether acting on the
primary signal will be profitable net of costs, conditioned on alpha
strength, spread, volatility, depth, queue imbalance and expected cost.

The committed run gates LightGBM's pooled OOS predictions: chronological
50/25/25 train/calibration/test split (60 s embargo), **isotonic
calibration** on the middle segment, and an *economic* meta-label (realized
net P&L > 0 under the conservative cost model — not "was the sign right").
Results: test AUC 0.552, Brier 0.0683, test base rate of profitable signals
0.073 — and at both τ = 0.5 and the calibration-chosen best τ = 0.300 the
gate keeps **zero** of the 5,212 test signals. That is not a malfunction:
with so few signals profitable net of costs, abstaining can be the
economically correct output, and the gate-off row (−941.7 total net bps,
−0.18 bps/trade) shows exactly what was declined. The machinery details
still matter: thresholds must be chosen on a calibration segment in
probability space and evaluated economically — and a gate whose honest
answer is "don't trade" must be allowed to say so.

### 7.4 Manifests: every fit is an audited experiment

Every model fit lives under `research/models/run_NNNN_<name>/` with a
`manifest.json` recording experiment id, git commit, **data version** (hash
of the QC report), **feature version** (registry hash), model version,
hyperparameters, train/test windows, and hardware — plus metrics and the
pickled model. `ledger.json` holds the monotone experiment counter feeding
the multiple-testing accounting of §6.6. Reproducibility is not a README
promise; it is a directory you can diff.

---

## 8. Portfolio construction under constraints

Signals are not positions. The portfolio layer (spec §15; API_PORTFOLIO_TCA.md
§1; Python reference `iap.portfolio`, Java production `com.iap.portfolio`)
solves:

```
maximize   alpha·w − lambda·wᵀΣw − Σ tc_i·|w_i − w_prev_i|
```

subject to any subset of seven constraint families: position boxes,
per-name participation, net exposure, **currency exposure** (an
E-matrix maps FX pair positions to per-currency exposures — spec §12's
requirement that cross-pair books be risk-measured in currencies, not pairs),
gross exposure, turnover, and a volatility target.

Design choices worth studying:

- **The algorithm is pinned, not just the answer.** Projected gradient
  ascent with a proximal (soft-threshold) step for the L1 transaction cost,
  cyclic projections in a fixed order, a pinned number of passes, tracking
  the best *feasible* iterate. Even the vol-target step is pinned as a radial
  scaling retraction — explicitly *not* the exact ellipsoidal projection —
  because production must replicate the reference bit-for-bit, and "any
  correct optimizer" is not a contract.
- **Exact L1-ball projection** via the sort-based algorithm — a small
  classic every quant developer should implement once.
- **Deterministic covariance**: RiskMetrics EWMA (λ = 0.94) on 1-minute
  last-observation bars, ddof=0 initialization, symmetrization, ridge
  `1e-6·tr(S)/n` — every choice spelled out because every choice changes
  the weights.
- **Constraint audit in every response**: each active constraint reports
  value, bound, slack, and a binding flag. "Constraint-auditable" (spec §15)
  means the optimizer explains itself.

The golden problem (`tests/golden/expected_portfolio.json`) exercises all
seven families on the 8-pair FX book: expected weights, objective and
`best_iteration` (1500) match at 1e-9 in Python and Java, and the Python
suite additionally checks the answer against an independent SLSQP optimum
(gap < 1e-5) — the golden itself is validated, not just frozen.

---

## 9. Fail-closed risk design

Spec §16's one-line philosophy: **hard risk decisions must be fail-closed** —
when anything is wrong or unknown, the answer is REJECT. The Rust engine
(`rust/risk/`) is the reference (safety-critical infrastructure is Rust's
lane in the responsibility matrix), with a Java port for platform
orchestration; `tests/golden/expected_risk_decisions.json` pins a full
decision-vector replay.

The rule set covers spec §16 end to end: fat-finger/max-order-size, price
bands vs a reference price, stale-price and sequence-gap gates, position/
notional/gross/net limits, daily and per-strategy loss limits, order-rate
throttles, duplicate-order and self-match prevention, venue-disconnect
handling, and kill switches at global/strategy/instrument/venue scope.

Engineering properties to note:

- **Deterministic decision order**: the golden pins not just ALLOW/REJECT
  but *which rule* decides (the first failing rule in the pinned check
  order) and the event severity. Two implementations that reject for
  different reasons are not equivalent.
- **Fail-closed data dependencies**: a sequence gap or stale market data
  *gates orders off* until recovery — the same stale-flag machinery from §3
  resurfacing as a hard control.
- **Auditability and replayability**: decisions are pure functions of
  (config, state, request); replaying the golden step sequence produces a
  byte-identical audit log. Risk events (including kill-switch engage/clear
  notifications) are part of the pinned output, in order.
- **State mutation is explicit**: the golden's step language (market, fill,
  cancel, gap/recover, venue_down/venue_up, kill/unkill) is a tiny
  domain-specific test format — a pattern worth copying for any stateful
  engine.

The kill-switch incident runbook
(`docs/runbooks/RUNBOOK_incident_kill_switch.md`) closes the loop from
mechanism to operations.

---

## 10. Execution algorithms and queue-position modeling

### 10.1 The algorithms

`cpp/include/iap/execution/algos.hpp` (C++ is the reference; Java ports)
implements the spec §17 set: **VWAP** (slice to an expected volume profile),
**TWAP** (uniform time slices), **POV** (participate at a target rate), and
**implementation shortfall** (front-load according to risk aversion), over
child order types MARKET/LIMIT/IOC/FOK.

### 10.2 The simulator: deterministic fills or it didn't happen

The event-driven execution simulator replays the same normalized stream as
everything else and simulates child-order lifecycles with pinned rules
(execution.hpp's header comment is the normative text):

- **Latency**: arrival = decision + decision_ns + risk_ns + wire_ns + venue
  mean + a SplitMix64 jitter draw per submission — deterministic given the
  seed.
- **Aggressive fills** walk the *displayed* top-10 depth, best-first, one
  fill per level; simulated orders never mutate the replayed book (the
  market stream stays authoritative); impact is charged economically
  instead.
- **Passive queue position**: `ahead_qty` starts as the displayed size at
  your level; observed EXECUTEs at your level deplete it (leftover volume
  after it reaches zero fills *you*); CANCELs decrement it in full
  (deterministic choice, documented); trade-throughs (executions at prices
  worse than yours) fill you completely; marketable ADDs — which generate no
  EXECUTE events (§3) — are *expanded* into their per-level consumptions and
  run through the same rules; a book whose display crosses your price fills
  you, with a carefully pinned exemption preventing double-counting of
  liquidity you already took. MODIFYs deliberately do nothing (a modified
  order's queue position is unknowable from public data — pinned as ignored
  rather than guessed).

### 10.3 What the golden shows

`expected_replay_fills.json` runs two parents against the golden equity
vector: a passive VWAP BUY 400 (4 LIMIT slices joining the bid) and an
aggressive IS SELL 600 (3 front-loaded MARKET slices). The economics are the
lesson: the passive parent completes 400 shares with **negative explicit
cost** (−$0.80 — maker rebates), while the aggressive parent pays $1.80 in
taker fees plus impact — a ~2 bps explicit swing between patience and
urgency on the same tape (paper 5). Every fill's price/qty/timestamp is
exact; fees and impact match to 1e-9, in C++ and Java alike.

---

## 11. TCA: decomposing execution cost

Transaction-cost analysis answers "where did the money go between the
decision and the fills?" The platform pins Perold's implementation-shortfall
decomposition (API_PORTFOLIO_TCA.md §2; Python reference, Java service):

```
delay_cost       = s·Q_f·(m_a − m_d)        # decision → arrival drift
trading_cost     = s·Σ q_f·(p_f − m_a)      # arrival → fills
opportunity_cost = s·(Q − Q_f)·(m_e − m_d)  # the part you never traded
total_is         = delay + trading + opportunity   # exact identity, golden-tested
```

with the sign convention "positive = worse than benchmark," and trading cost
further split into **spread + impact + timing**. Benchmarks (arrival, market
VWAP, TWAP) reuse the label layer's at-or-before prevailing-state rule — one
lookahead convention platform-wide. Adverse selection is measured as
post-fill markouts at {100ms, 1s, 10s}; an OLS impact estimate regresses
per-fill cost on participation.

The identity is enforced to 1e-9 — decompositions that don't sum are
narratives, not accounting. The golden cases include the edge everyone gets
wrong: a fully unfilled buy must come out as *pure opportunity cost*.

On the bundled 36-parent simulation (`research/tca/TCA_REPORT.md`): mean IS
20.6 bps (equity) / 0.45 bps (FX), and equity markouts ≈ **−24 bps, flat
from 100 ms to 10 s** — aggressive fills paid purely temporary impact and
resting counterparties suffered no adverse selection. On real data that flatness
would be remarkable; here it is flagged for what it is, an expected artifact
of the mean-reverting synthetic mid (§2.3).

---

## 12. Cross-language golden parity as an engineering discipline

### 12.1 The problem it solves

Research (Python) and production (C++/Rust/Java) implementing "the same"
logic differently is one of the most expensive failure modes in quantitative
trading: the backtest ran on semantics that production doesn't have. The
spec's answer (§21) is mechanical: for each canonical calculation, a fixed
event vector plus expected outputs, and **every language must load and
match**.

### 12.2 How this repo does it

- `tests/golden/` holds two pinned event vectors (2,000-event equity MBO,
  800-event FX quote, both byte-exact JSONL) and expected outputs for codec
  (SHA-256 of the IAP1 binary encoding — *byte* parity, the strongest
  possible claim), book states, features, alphas, backtest, risk decisions,
  replay fills, portfolio, and TCA.
- **Tolerance policy is explicit**: integer state (ticks, sizes, counts,
  sequences) matches *exactly* — no epsilons; float outputs match at
  abs 1e-9 + rel 1e-9. Deciding which quantities are integers (§1.2) is what
  makes the exact tier possible.
- **The reference validates itself before pinning**: golden generators
  cross-check against independent brute-force implementations (a naive
  order book, brute-force feature recomputation) before writing files, and
  the portfolio golden is checked against an SLSQP optimum. Golden files are
  regenerated only deliberately, with a MIGRATIONS.md entry.
- **One command proves parity**: `tests/harness/run_all.sh` runs all four
  suites and prints the table (a full harness run: python 489, cpp 175,
  rust 181, java 315 tests passed; golden groups 49/37/36/13; all PASS).

### 12.3 Why it changes how you write code

Golden parity converts "port this" from an act of interpretation into an act
of verification. Paper 6's case study shows the consequences: all three
native ports converged on the same allocation discipline (pools, free lists,
intrusive FIFO lists, open addressing), enforced three different ways —
by tests in C++, by the type system in Rust (all `unsafe` confined to five
sites in one ring-buffer file), by hand-rolled primitive structures in Java.
Every pinned edge case in this document — MODIFY-increase loses priority,
interior snapshot records don't refresh, the crossing-exemption in the queue
model — exists because a golden test would catch any implementation that
chose differently.

---

## 13. Latency economics

Paper 4 asks the question every trading firm eventually budgets around: *what
is a microsecond worth?* — and answers it by joining two measurements this
repo can actually make.

**Measurement 1 — the cost of staleness.** The validation framework's latency
stress rescores every alpha with signals delayed by +1 and +5 events. The
fast equity flow alphas (EQ02/EQ03/EQ12) shed ~20-25% of IC after one event
and ~70-77% after five (EQ03: 0.0267 → 0.0214 → 0.0085); EQ01's microprice
signal flips sign entirely by +5 events (0.0213 → −0.0131). The slow equity
alphas barely notice even five events (EQ09: 0.0862 → 0.0870 → 0.0833), and
the FX regime family retains ~80% at one event (~15 s of FX tape) but only
~25-30% at five (FX09: 0.1414 → 0.1138 → 0.0345). Alpha decay against
*events* is the economically meaningful axis.

**Measurement 2 — the speed of the stack.** The measured C++ hot path
(decode + book + features + alpha) sums to ≈ 0.5 µs/event, versus a median
inter-event gap of ≈ 0.9 s on this dataset — six orders of magnitude of
headroom. Rust and Java demo-scale replays (≈ 6.9M and ≈ 3.5M events/s) are
equally overprovisioned.

**The synthesis**: on this platform, latency economics are entirely about
*reacting to the next event* rather than compute speed. Latency investment
is existential for the flow alphas (their IC decays event by event — though
on this dataset costs bury them regardless), and nearly worthless for the
slow equity and FX regime books, which keep their statistics through
multi-event lags. The general lesson transfers to real markets: price
latency in units of *missed events at your signal's decay horizon*, not
nanoseconds in the abstract — and expect the answer to differ per alpha
family.

---

## 14. Adaptability: evolving faster than you decay

### 14.1 Why static models rot

Every fitted alpha is a snapshot of a joint distribution — feature values,
their relationship to forward returns, the cost surface — taken over its
training window. Markets do not honor snapshots: volatility regimes shift,
liquidity migrates, competitors crowd the signal, and the microstructure
itself changes (tick regimes, fee schedules, venue mixes). The result is
the best-documented phenomenon in live quant trading: **realized IC decays
relative to the research backtest**, sometimes gradually, sometimes in a
break. A platform that only validates at promotion time is betting that
the snapshot stays true forever. Spec §20's last two steps (12-13 —
continuous monitoring, retirement criteria) exist precisely because it
never does. The adaptability layer (`python/src/iap/adaptive` — reference;
`iap.backtest.adaptive` — deployment semantics; `com.iap.adaptive` — the
live Java port; contract in `/API_ADAPTIVE.md`) is this platform's
implementation of those steps: *measure* decay, *refit* when the evidence
says the input distribution moved, and *retire* alphas whose realized IC
stops clearing the gate.

### 14.2 Detecting drift: PSI and KS

The primary monitor is the **Population Stability Index** of a live
distribution (the alpha's signal, and each feature it declares) against a
research-window baseline. The formula is pinned in API_ADAPTIVE.md §2:
bucket the baseline into deciles (9 interior edges, linear-interpolation
quantiles), assign each live value `v` to bucket `searchsorted(edges, v,
side='left')` — a value exactly on an edge falls in the *lower* bucket —
and with live fraction `a_i` and baseline fraction `E_i`, both clamped
below at `1e-6` (no renormalization):

```
PSI = Σ_{i=0..9} (a_i − E_i) · ln(a_i / E_i)
```

The conventional reading is ~0.1 modest shift, ~0.25 significant; the
pinned trigger is strict `PSI > 0.25`. Fewer than 200 finite live values
reports **null**, never 0 — "no data" and "no drift" are different facts.
A two-sample Kolmogorov–Smirnov test (D plus the Numerical Recipes
asymptotic p, 100 terms, pinned) is computed alongside as a diagnostic
but never triggers anything: KS p-values on thousands of correlated
microstructure observations reject constantly, while PSI's bucketed view
degrades gracefully. Both are golden-tested to 1e-10 from a
SplitMix64-seeded recipe (`tests/golden/expected_adaptive.json`), so the
Java monitor provably computes the same number as the Python reference.
Alongside distribution drift, a **rolling realized-vs-research IC**
z-score (`z = (mean live bucket ICs − ic_mean) / (ic_std / √n)`) watches
the thing you actually care about — is the alpha still predicting? — with
maturity handled correctly: a row only enters the rolling IC once its
label horizon has fully elapsed.

### 14.3 Refit policies: static, scheduled, drift-triggered

`iap.adaptive.refit` pins three policy families, all pure functions of
`(now, last_fit, PSI values, ic_z)` — no wall clock, no randomness:

- **static** — fit once, never again. Zero refit cost and zero churn, and
  the honest baseline every adaptive scheme must beat; its failure mode is
  unbounded staleness.
- **scheduled** — refit on an epoch-aligned calendar cadence (weekly and
  daily instances are pinned). Predictable and audit-friendly, but it
  refits whether or not anything changed *and* can sleep through a fast
  break between boundaries.
- **drift_triggered** — refit when any monitored PSI exceeds 0.25 or the
  rolling-IC z drops below −2.0, rate-limited by a 1-hour minimum gap.
  Reactive exactly when the evidence moves — but it inherits the quality
  of its monitors, and a misconfigured monitor turns it into a refit
  treadmill (see the FX10 lesson below).

In the study (`research/adaptive_reports/ADAPTIVE_REPORT.md`), across 10
alphas the policies performed 10 (static) / 20 (daily) / **113
(drift-triggered)** total refits — and none of that activity changed the
economics: every alpha stays net-negative after costs, and the P&L spread
between policies is one to two orders of magnitude smaller than the cost
drag. Refitting neither rescues nor ruins any alpha here.

### 14.4 The lifecycle state machine: hysteresis against flapping

Monitoring produces a noisy scalar (rolling IC) and the platform must
turn it into a discrete allocation decision. A naive threshold would flap
— allocate, deallocate, reallocate on every noise excursion. The pinned
state machine (`iap.adaptive.lifecycle`, API_ADAPTIVE.md §6) is built
around **hysteresis**:

- **ACTIVE** → WATCH on a rolling IC below 0.0 (the entering breach counts
  as breach #1). Still allocated — WATCH is probation, not punishment.
- **WATCH** → RETIRED only after **6 consecutive** breaches; back to
  ACTIVE only after **3 consecutive** readings at or above 0.005. A
  reading in the neutral zone `[0.0, 0.005)` resets *both* counters —
  ambiguous evidence restarts the clock in both directions.
- **RETIRED** — allocation verifiably halted (the adaptive backtest forces
  positions flat), but *shadow scoring continues*, so the pinned
  re-activation path stays reachable: 3 consecutive recoveries earn WATCH
  — probation again, never a straight jump back to ACTIVE.

A null rolling IC (too little matured data) causes no transition at all.
Every transition appends a JSON line to `research/lifecycle_log.jsonl`
with the alpha, policy, timestamp, states, reason and the IC that caused
it — 248 in the committed study — and the asymmetry of the design (fast
to suspicion, slow to trust) is the point. Six alphas hit RETIRED under
at least one policy; FX01 finishes RETIRED under all four, which is the
system doing its job: a REJECT-grade alpha (promotion IC −0.015) that
should never have been deployed gets caught and shut off by the live
gate.

### 14.5 The live monitoring loop

The research layer *serializes its expectations*: decile edges, expected
fractions and IC baselines are written to `research/baselines/*.json`
(API_ADAPTIVE.md §1), and the Java paper-trading platform consumes those
files unchanged — the same numbers the study monitored against are the
numbers production is monitored against. `com.iap.adaptive`
(BaselineLoader, Psi, DriftMonitor, RollingIc, LifecycleGauge) feeds
three live Prometheus gauges, no longer placeholders:

- `alpha_live_vs_backtest_drift{alpha=...}` — PSI of the live signal
  against its research baseline;
- `alpha_rolling_ic{alpha=...}` — realized IC over the trailing window,
  matured rows only;
- `alpha_lifecycle_state{alpha=...}` — 0 ACTIVE / 1 WATCH / 2 RETIRED.

The `LiveVsBacktestDrift` alert fires on PSI > 0.25 — the same pinned
threshold as the research trigger — and the Trading & Risk Grafana
dashboard plots all three. Monitoring is observational by construction:
it never feeds back into a trading decision mid-session, so determinism
and replayability are untouched. Recipe 19 in the COOKBOOK scrapes all
three gauges from a live paced session.

### 14.6 FX10, or: how to choose monitors badly

The study's deliberate teaching case. FX10 declares `minute_of_day_v1`
among its features, and its drift baselines were captured from a
morning warmup — so as the session simply *progresses*, the live
minute-of-day distribution walks away from the baseline **by
construction**. PSI dutifully exceeds 0.25 at almost every evaluation:
FX10 logs 156 drift events where no other alpha logs more than 23, and
under the drift-triggered policy it refits 41 times (vs 1 static) while
ending in a *worse* deployed IC and P&L than static. Nothing
malfunctioned — the machinery behaved exactly as pinned. The lesson is
about **monitor selection**: deterministic calendar features do not
belong in a drift trigger, because their "drift" carries no information
about model validity. In a production review this is exactly the class
of misconfiguration a drift-monitor checklist must catch, and the report
leaves the false positive visible on purpose rather than quietly
excluding the feature.

### 14.7 The honest limits

Two synthetic sessions (~2.6 dense hours each) cannot rank refit
policies, and the report leads with that instead of burying it:
SCHEDULED(weekly) crosses no week boundary, so it is *identical to
static by construction*; daily fires exactly once; the generator has no
real regime shifts, so most drift-triggered refits come from the noisy
rolling-IC z rather than genuine distribution breaks; and every P&L
difference between policies is within noise. What the study *does*
establish is the part you can establish at this sample size: the
machinery is deterministic and leak-free (refits train only on purged,
embargoed history — asserted at runtime and shift-tested), triggers fire
exactly when the pinned rules say, retirement verifiably halts
allocation, and all 12,082 study looks (6,041 per run, two runs recorded) are counted in the multiple-testing
ledger. "We built the machinery and proved it behaves; we did not prove
it makes money" is the adaptability layer's version of the platform's
central discipline: research truth over backtest cosmetics.

---

## 15. Twelve pitfalls this platform is built to avoid

1. **Lookahead in labels or benchmarks.** One pinned at-or-before rule for
   labels, TCA and features; shift-by-one tests enforce it mechanically.
2. **Leakage via undeclared features.** Alphas declare their inputs; the
   harness masks everything else and demands identical output.
3. **Random splits on overlapping labels.** Walk-forward only, purge at the
   label horizon, 60 s embargo (§6.2).
4. **Uncounted multiple testing.** An append-only ledger (13,306 looks,
   12,082 of them from the adaptive study alone) with a printed
   expected-max-|t| yardstick; t = 1.7 is called what it is.
5. **Ignoring costs until the end.** Cost-adjusted labels, cost-stressed
   backtests, and a promotion gate requiring net P&L > 0 at 1× costs — which
   is exactly what stopped OFI (§6.5).
6. **Trusting a target you didn't decompose.** The 0.70 "IC" of §7.2 is
   spread prediction; the conservative-economics column told the truth —
   and the decomposition is also what caught the generator artifact.
7. **Floats on contracts.** Integer ticks/quantities everywhere;
   cross-language parity would be impossible otherwise.
8. **Hidden nondeterminism.** One pinned RNG, explicit seeds, no wall clock
   or unordered-map iteration on deterministic paths; checkpoint/restore is
   bit-identical.
9. **"The port is probably right."** Golden vectors with explicit tolerances
   across four languages; byte parity for the codec.
10. **Fail-open risk and silently stale data.** Gaps mark books stale;
    stale gates features invalid and orders rejected; kill switches are
    replayable audit events.
11. **Backtest fills your production couldn't get.** A pinned, deterministic
    queue-position and latency model shared by golden tests in two
    languages; passive vs aggressive economics measured, not assumed.
12. **Benchmark numbers without methodology.** Every published figure
    carries hardware, compiler, workload and boundary caveats (2-CPU
    container, mean-only, no pinning) — per spec §22's "never present an
    isolated latency number."

---

## 16. Ten interview questions (with answers from this repo)

**Q1. Why can order-flow imbalance be a real predictor and still lose
money?**
Because significance and tradability are different tests. EQ03: OOS IC
0.026, t 7.24, every non-degenerate fold positive — and −199,913 net at 1×
costs, since 277 signal flips/hour pay the spread continuously. IC measures
correlation; P&L measures correlation × horizon × turnover − costs.

**Q2. What is the microprice and when does it beat the mid?**
`(Pb·Qa + Pa·Qb)/(Qb+Qa)` — the size-weighted touch price that leans toward
the heavy side's opposite quote. It adds information when displayed L1 sizes
are informative about the next move: real on this repo's MBO equity book
(EQ01 ITERATE — significant and hypothesis-confirmed, though still
cost-negative), absent on its quote-driven FX book (FX01 REJECT). Structure
decides.

**Q3. Why purge *and* embargo in walk-forward validation?**
Purging removes training rows whose label windows overlap the test period —
direct leakage. The embargo (60 s here) additionally covers serial
correlation just outside the overlap. Purge width follows the label horizon;
embargo handles what the purge can't see.

**Q4. Your ML model shows IC 0.70 at 5 seconds. What do you do?**
Disbelieve it, then decompose the target. Here the cost-adjusted target
embeds a spread component that is highly predictable (corr ≈ −0.94) because
the consolidated book is sometimes crossed — 29.5% of the time on FX from
stale aggregated LP quotes, and in an earlier generator version ~95% of the
time on equities, a bug the decomposition exposed and a redesign (shared
efficient price) fixed down to 0.79%. Directional IC vs the mid label is ~0.
Check what the target contains, check IC against a frictionless label, check
economics under a conservative cost floor — and if the answer implicates
the data-generating process, fix the data, not the story.

**Q5. What is meta-labeling, and what does it mean when the gate keeps zero
trades?**
A secondary model predicting whether *acting* on the primary signal is
profitable net of costs. With a profitable-trade base rate of 0.073,
calibrated probabilities rarely approach 0.5, so thresholds must be chosen
on a held-out calibration segment in probability space and evaluated
economically. On this dataset even the calibration-chosen τ = 0.300 declines
every test signal — the correct output, since the gate-off alternative
realized −0.18 net bps/trade. A conviction model must be allowed to say
"don't trade."

**Q6. How do you make a portfolio optimizer "production-grade"?**
Make it deterministic and auditable: pin the algorithm (PGD + prox step,
fixed projection order and passes), not just the problem; validate inputs
(PSD, symmetry tolerance); emit a constraint audit (value/bound/slack/
binding per constraint) with every solve; golden-test the exact weights
across languages and cross-check against an independent optimizer.

**Q7. What does fail-closed mean in a risk engine?**
Unknown or degraded state ⇒ REJECT. Sequence gap ⇒ orders gated until
snapshot recovery; stale price ⇒ reject; venue disconnect ⇒ scoped kill.
Also: deterministic rule order (the golden pins *which* rule rejects),
byte-identical audit-log replay, and kill switches as first-class events.

**Q8. How do you model queue position for a passive order from public MBO
data?**
Track `ahead_qty` = displayed size at your level when you rest; EXECUTEs at
your level deplete it (overflow fills you), CANCELs decrement it (this repo
pins the deterministic full-amount choice), trade-throughs and crossing
displays fill you entirely, and marketable ADDs — which match silently —
must be expanded into their per-level consumptions. What's unknowable
(a MODIFY's queue effect) gets pinned as ignored rather than guessed.

**Q9. Decompose implementation shortfall — and what must be true of the
decomposition?**
Delay (arrival − decision drift on filled qty) + trading (fills vs arrival
mid, itself spread + impact + timing) + opportunity (unfilled qty × decision-
to-end drift). It must sum *exactly* to the total (enforced here to 1e-9),
and an unfilled order must be pure opportunity cost.

**Q10. How would you decide whether to invest in lower latency?**
Measure alpha decay in *events*, not seconds, per strategy: here the fast
flow alphas shed ~20-25% of IC per event of staleness and ~75% by five
events (EQ01's microprice signal flips sign outright), while the slow
equity and FX regime alphas barely notice — and the compute path is six
orders of magnitude faster than the feed. So the marginal microsecond of
compute is worthless, but being events late is existential for flow alphas.
The budget goes wherever your reaction-to-next-event chain is actually
bottlenecked, weighted per alpha family.

---

## 17. Further reading

Inside this repository, in suggested order:

1. `docs/SPECIFICATION.md` — the governing spec; §32 is one paragraph and
   worth memorizing.
2. `PLATFORM_CONVENTIONS.md` + `schemas/FORMAT.md` — how contracts get pinned.
3. `API_CORE.md` → `API_FEATURES.md` → `API_ALPHA.md` →
   `API_PORTFOLIO_TCA.md` → `API_ADAPTIVE.md` — the five port contracts,
   increasingly rich.
4. `research/alpha_reports/REPORT.md` — read the master table cold, then
   re-read §6 above.
5. `research/ml_reports/ML_REPORT.md` — the crossed-book artifact, in the
   authors' own numbers.
6. `research/adaptive_reports/ADAPTIVE_REPORT.md` — the adaptive study;
   start with "READ THIS FIRST", then §14 above.
7. `docs/papers/INDEX.md` — all six papers; paper 4 (latency) and paper 6
   (C++/Rust/Java case study) especially.
8. `cpp/include/iap/execution/execution.hpp` — the header comment is the
   best short document on deterministic fill modeling in the repo.

Classic external literature these designs draw on (find current editions):

- Harris, *Trading and Exchanges* — the standard microstructure-institutions text.
- O'Hara, *Market Microstructure Theory*; Hasbrouck, *Empirical Market
  Microstructure* — the theory and econometrics foundations.
- Cont, Kukanov & Stoikov, "The Price Impact of Order Book Events" — the OFI
  construction used by EQ02/EQ03.
- Stoikov, "The Micro-Price" — the microprice estimator behind EQ01/FX01.
- Avellaneda & Stoikov, "High-Frequency Trading in a Limit Order Book" —
  inventory-aware quoting, background for execution thinking.
- Almgren & Chriss, "Optimal Execution of Portfolio Transactions" — the
  impact/urgency tradeoff behind the IS algorithm.
- Perold, "The Implementation Shortfall: Paper vs. Reality" — §11's
  decomposition, from the source.
- López de Prado, *Advances in Financial Machine Learning* — purging,
  embargo, meta-labeling, deflated Sharpe/multiple testing.
- Bailey & López de Prado, "The Deflated Sharpe Ratio" — the selection-
  under-multiple-testing yardstick behind §6.6.
- Grinold & Kahn, *Active Portfolio Management* — alpha, IC and the
  fundamental law, context for §8.

Everything else is in the code — which, in this repository, is the point:
every claim above is a test, a golden file, or a committed report you can
rerun.
