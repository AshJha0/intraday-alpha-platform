# LEARN.md — A Guided Tour of the Intraday Alpha Platform

This document walks through the entire platform the way a textbook would:
concepts first, then how this repository implements them, always with the
repo's *real, verified numbers* as worked examples. Everything cited here can
be reproduced from the committed code and seeds — that reproducibility is
itself one of the lessons.

Reading order matters less than you'd think; each section stands alone but
cross-references the others. If you only have an hour, read §6 (honest alpha
research), §7 (ML and meta-labeling), §12 (golden parity) and §20 (the MVP
and its IC audit) — they carry the platform's central ideas. If you have a
second hour, read §21–§26: they are the v1.3.0 review written up as case
studies — what was wrong in the safety code, why the tests had not caught
it, and what each fix pins.

For a shorter, top-down explanation of how the quant, algo and AI sides fit
together — one page per subsystem, each ending with where to look and a
command that runs — start with [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)
and come back here for the depth.

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
15. [Contracts and Protocols: typing the loop](#15-contracts-and-protocols-typing-the-loop)
16. [The Python risk and execution reference ports](#16-the-python-risk-and-execution-reference-ports)
17. [The seven-state promotion lifecycle](#17-the-seven-state-promotion-lifecycle)
18. [The decision trace, and replaying an incident](#18-the-decision-trace-and-replaying-an-incident)
19. [The data model: an index, not a database](#19-the-data-model-an-index-not-a-database)
20. [The MVP walkthrough, with the honest numbers](#20-the-mvp-walkthrough-with-the-honest-numbers)
21. [Fail-closed risk engineering: four bugs, worked](#21-fail-closed-risk-engineering-four-bugs-worked)
22. [Simulator realism: the fills that never happened](#22-simulator-realism-the-fills-that-never-happened)
23. [Statistical power: what a null result is worth](#23-statistical-power-what-a-null-result-is-worth)
24. [Multiple testing, and three ways to game a gate](#24-multiple-testing-and-three-ways-to-game-a-gate)
25. [Crash consistency and commit points](#25-crash-consistency-and-commit-points)
26. [Supply-chain hygiene](#26-supply-chain-hygiene)
27. [Twelve pitfalls this platform is built to avoid](#27-twelve-pitfalls-this-platform-is-built-to-avoid)
28. [Twelve interview questions (with answers from this repo)](#28-twelve-interview-questions-with-answers-from-this-repo)
29. [Further reading](#29-further-reading)

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
statistically predictive here (uncrossed OOS IC 0.0312/0.0298 with
Newey-West t of 8.47/10.61, every non-degenerate walk-forward fold
positive — the gates read the UNCROSSED column, and on equities the
consolidated book is crossed on only ~1 % of rows, so pooled and uncrossed
barely differ: 0.0309/0.0299) and *still not tradable* — at 133/147 signal
flips per hour the strategies pay the spread so often that costs exceed
gross alpha by roughly two orders of magnitude (EQ02 day 2: gross +1,360
against 195,402 of costs).

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

From `configs/marketdata/generator.json` (seed 20260829, 2 sessions):

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

**Equity flow stops about 40% into each session.** Each equity stream gets
a budget of `slots_per_stream` flow slots per session, and the loop that
spends it draws inter-arrival times at `slots / duration × 1.30 ×
(1 + excitation)`. The 1.30 multiplies the rate, so the budget would run
out 77% of the way through even with no clustering (measured: 74.5–79.6%
with the excitation kick set to 0); the self-exciting multiplier then about
halves the mean inter-arrival time, and nothing in the calibration accounts
for it. Measured on the bundled dataset: the last continuous event of an
equity stream falls 37.6–43.2% into the 6.5-hour session (mean 40.5%,
about 2 h 38 min after the open), followed by nothing until the close
auction prints. FX has the same kind of budget with a 1.05 margin and no
excitation, and ends 91.7–99.8% of the way through (mean 95.2%). What this
means for everything downstream: an equity "session" is its first 2 h 40
min; two sessions are about 5.3 hours of continuous equity flow, not 13;
the walk-forward folds are cut by row mass, so all four folds partition
that window and none of them sees afternoon flow; a feature row exists at
the close, nearly four hours after the one before it. The
default stays as it is because every golden vector, report and headline
number derives from it. `equities.fill_session: true` removes the budget
(flow to the close, about 2.5 times the equity events); regenerating the
research on it is backlog issue M08.

The generator's mid is **strongly mean-reverting** around its regime
process. Consequences you will see all over the research reports:

1. **Reversion alphas look great, momentum-family hypotheses often fail.**
   Several alphas ship with `hypothesis_confirmed = false` — the fitted sign
   contradicts the stated economic rationale — and are therefore barred from
   PROMOTE no matter how large the IC (e.g. EQ09, statistically strong at
   NW t −5.11 on 1.00 fold sign consistency, but with the sign pointing the
   wrong way: **REJECT**). FX09 used to be the example here at "IC 0.113,
   t 14.3, capped at ITERATE"; since the gates started reading the
   crossed-book-conditioned IC it fails on the number as well as the sign
   (uncrossed IC −0.0472, NW t −3.81, **REJECT**) — see §6.5.
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

- **ADD** — join the FIFO tail of the (side, price) level. While the venue
  status is TRADING, a price that crosses the opposite best is *marketable*:
  it executes against the opposite FIFO from the head of the best level
  (partial fills reduce the head; emptied orders are removed), and any
  remainder posts. Note the subtle consequence: an internal match generates
  **no EXECUTE events** on the wire — the execution simulator has to
  reverse-engineer those consumptions for queue accounting (§10). During a
  HALT / AUCTION call phase / after CLOSE **nothing matches**: crossing ADDs
  rest, the book is legitimately crossed (`is_crossed()`), and the venue's
  own EXECUTE messages perform the uncross — exactly what LSE SETS, Xetra or
  a NYSE re-opening auction looks like on the wire.
- **MODIFY** — quantity change only. Decrease keeps queue position; increase
  moves the order to the level tail (you lose priority when you upsize —
  matching real exchange semantics). A non-zero price that differs from the
  resting price is an adapter bug (ITCH Replace / MDP3 price-modify must be
  CANCEL+ADD): dropped and counted (`modify_price_mismatch`).
- **CANCEL** — remove by order_id; unknown ids are dropped and counted, never
  fatal.
- **EXECUTE** — fills the **referenced** order, by `order_id` (valid feeds
  always reference the FIFO head of its level, but the book applies whatever
  order the event references); partial fills keep position, an order is
  removed at qty 0. Does *not* touch `trade_flow`.
- **TRADE** — updates cumulative signed `trade_flow` only (+qty for buy
  aggressor, −qty for sell), with checked arithmetic. Trade prints and book
  mutations are separate event types with separate meanings.
- **QUOTE** (FX) — replaces the venue's entire side at L1: the quote-driven
  world in one rule. Real LP streams carry no order ids, so `order_id = 0`
  is first-class: the book keys the level by a deterministic *synthetic id*
  in a reserved range (`0xFFFF…`), and an explicit id may not rest on the
  other side.
- **SNAPSHOT** — a burst of records (one per resting order, bids then asks,
  best→worst, FIFO within level, with a countdown in `trade_id`); the first
  record clears both sides, the last clears the `stale` flag. **Broken-burst
  rule** (conventions §4): a sequence gap arriving *inside* an active burst
  marks that burst BROKEN — it still ends at its `trade_id == 0` record but
  does **not** clear `stale`; only a later complete, gap-free burst does.
  The countdown itself is validated: a restart (countdown goes up) clears
  and starts over, a skip breaks the burst; id-less (L2) records get
  synthetic ids; a repeated id is malformed.
- **Malformed events** — one policy, one table (API_CORE §4): unknown event
  type, bad side, `qty <= 0` / `price <= 0` where a positive value is
  required, `order_id 0` on MBO events, reserved ids, bad STATUS codes, and
  any i64 overflow are **dropped and counted** in a named counter *after*
  the sequence number is consumed — never an exception mid-stream. Only
  routing an event to the wrong book raises. The accounting invariant
  `applied + drops + held == events fed` is asserted in every port.

**Sequencing** is checked before dispatch: the first event of an epoch is
accepted whatever its sequence (0 included); a duplicate (sequence ≤ last)
is dropped and counted; a gap marks the book `stale = true`, and while stale
only SNAPSHOT/STATUS/TRADE/HEARTBEAT apply. Two things real feeds do that a
naive book cannot survive are pinned too: a **venue sequence reset** (LSE,
Xetra, Euronext and Nasdaq restart channel sequences daily; T7 on fail-over)
is recognised when a SNAPSHOT burst starts below the last sequence — new
epoch, `sequence_resets`, stale until the burst completes — and a
**retransmission** (MoldUDP64 re-request, A/B arbitration) is reordered by
an optional bounded hold-back buffer (`reorder_window`, ≤ 4096 events,
`late_recovered`). Stale books poison nothing downstream because the
consolidated view and the feature engine exclude stale venues and mark
affected features invalid (§4.3) — the fail-closed idea (§9) appearing
already at the data layer. Silent disconnects (no gap, the line just goes
quiet) are a *time* question, answered by `is_fresh(now, max_age)`.

**Checkpoints** serialize the full book (levels in sorted order, FIFO lists,
`arrival_order`, burst state, the reorder buffer and all twelve counters) as
a cross-language JSON document (x-version 2) such that restore-and-continue
is bit-identical to never having stopped — verified in tests, verified
*across languages* by `expected_checkpoint_eq_1000.json`, and the
foundation of replayable backtests.

Derived state after *every* event: best bid/ask with sizes, top-10 depth and
order counts per side, signed trade flow, last sequence, timestamps. The
golden file `expected_book_states.json` pins this state after events
100/500/1000/1500/2000 of the equity vector — exact integers, no epsilons —
and `expected_anomaly_states.json` pins per-venue state, all twelve counters
and the consolidated view of two *anomaly* vectors (gaps, duplicates, late
arrivals, resets, id-less quotes, malformed payloads, a halt with a
re-opening auction) in every language, with and without a reorder window.
`docs/SCENARIOS.md` maps each real-world scenario to its pinned rule and
tests.

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
state updates at 514.1 ns/event on the equity vector — `benchmarks/
results_cpp.md`, hot and cache-resident).

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

Result on the bundled data: **0 PROMOTE / 11 ITERATE / 13 REJECT** — every
one of the 24 alphas is net-negative at 1× modeled costs, so nothing clears
the last gate. Worked examples, straight from the master table:

Since round 3 the gates read the **uncrossed** IC and its Newey–West t —
the same IC restricted to rows whose consolidated book was not crossed by
a stale venue quote (`IC unc` in the master table). On the equity book the
two are the same number to three decimals (≈1 % of rows are crossed); on
FX, where 29–33 % of cross-sections are crossed, they are different alphas
entirely. Worked examples:

- **EQ03 (multi-level OFI) — ITERATE, the platform's signature finding.**
  Uncrossed IC 0.0298, NW t 10.61, all four folds non-degenerate and
  sign-consistent, leakage-clean… and net **−70,638** at 1× costs, because
  147 flips/hour means paying the spread constantly. Statistically real,
  economically dead on this data (paper 1).
- **EQ08 (VWAP/mid deviation) — REJECT, instructively.** Only 6 signal
  flips/hour, so it loses the least money of any equity alpha (−3,714 at
  1×) — but its uncrossed IC is −0.0468 and its fitted sign contradicts the
  stated rationale (`hyp = no`). Cheap to trade is not the same as real.
- **FX09 (vol-regime reversion) — REJECT, and the clearest lesson in the
  report.** Round 2 called it "the best FX statistics in the study" at
  IC 0.113, t 14.3. Conditioning on book state dissolves most of that: its
  IC is **−0.2100 on crossed rows and −0.0472 on uncrossed ones**, i.e. the
  signal was largely measuring the mechanical reversion of a stale LP's
  quote, not a vol regime. With `hyp = no` on top, it is a REJECT twice
  over. A number that only exists on untradeable rows is not a number.
- **FX01 (microprice on the FX quote book) — ITERATE, but on 0/4 folds.**
  Pooled IC −0.015 flips to +0.018 uncrossed (t 2.65), which clears the
  lenient ITERATE gate, yet **no individual fold** is positive
  (`folds+ = 0.00`) — so it can never reach PROMOTE. Compare EQ01, the same
  formula on the MBO book: uncrossed IC 0.0273, t 4.95, all four folds
  positive. Market structure decides (paper 2).

### 6.6 Multiple testing: counting your looks

Every walk-forward evaluation, decay horizon, stress variant, and backtest is
recorded in a ledger (`research/experiments.json`). The round-3 audit found
the count was measuring the wrong thing: it grew every time somebody *reran*
a script, and it counted each of the adaptive study's 211 monitoring
evaluations as a separate "experiment", so the Bonferroni denominator was a
function of how often the same code had been executed. An experiment is now
identified by **(alpha, kind, canonical config)** and de-duplicated on that
key — rerunning `run_all.py` changes nothing, and one adaptive deployment is
one experiment.

The current ledger holds **70 distinct configurations / 1068 looks**: a
one-time design scan (216 experiments: 24 alphas × 9 horizons), 24 promotion
pipelines at 28 experiments each (672), 40 adaptive deployments (10 alphas ×
4 refit policies), and — since 2026-09-19 — five `ExperimentRunner` runs at
28 each (140; §6.8). That translates into a selection yardstick: Bonferroni
per-test threshold |t| ≥ **4.071**, and an expected **max |t| ≈ 3.735 under
the global null**. Meaning: FX08's uncrossed t = 2.02 — or FX11's 1.40 — is
*consistent with pure selection* over this many trials, and the report says
so in print. EQ03 (t 10.61) and EQ02 (8.47) clear it; EQ06 (3.25) and EQ11
(3.16) pass the fixed t ≥ 3.0 gate but sit *below* the selection-adjusted
yardstick, which the master table flags. Most quant shops track this
informally at best; here it is a serialized, deterministic artifact — and
the runner's five entries were deliberately *not* de-duplicated against the
pipeline entries whose computation they repeat (EQ03 @ 5 s is the report's
own walk-forward): the denominator may only grow.

### 6.7 Cost reality

The cost model (`configs/execution/execution.json`) charges half-spread + fees
(mirroring venue configs) + linear impact per trade, and the day-2
out-of-sample backtest uses day-1-fitted parameters — the exact parameters
serialized for the production ports. The equal-weight ensembles finish
negative (equity −93,359; FX −43,499 net). The report's Sharpe column is
labeled as an event-time research yardstick, not a production claim. Honesty
in the artifacts, not just the prose.

### 6.8 A typed experiment: spec in, result out

The walk-forward story now has a typed artefact. `python -m iap.research run
--alpha EQ03 --horizon 1s` builds an `ExperimentSpec` — alpha, horizon, the
dataset version (sha256 over the normalized IAP1 bytes), the feature version
(the registry hash), the model *definition* hash, the pinned protocol
configuration (`n_folds 4`, `embargo_ns 60e9`, `cost_multiplier 1.0`,
`latency_ns 1e9`, `max_decision_age_ns 60e9`, `flatten_at_session_end`),
three periods and a seed — whose id is the first 16 hex of its own content
hash: the id *is* the request. It runs `validate_alpha` (the same purged,
embargoed walk-forward the report runs) plus a holdout backtest, and writes
an `ExperimentResult` (IC, rank IC, NW t, hit rate, turnover, fold
consistency, leakage detail, hypothesis sign, the holdout's gross / cost /
net bps, drawdown, Sharpe, the verdict, the ledger count at run time, the
commit, and `created_ts` = the test period's end in event time) under
`research/experiments/<id>/`. Two honesty rules are built in: a metric the
framework could not compute is a `ResearchError`, never a number; and a
rerun that reproduces different evidence under the same id is *refused*,
not overwritten. The seed is recorded and hashed and consumed by nothing —
the chain has no random element — and the document says so.

The period rule is the worked example of purge + embargo: `test` is the last
session, `validation` is the tail `[test_start − horizon − embargo, test_start)`
— exactly the rows the fold's train mask refuses — and `train` is everything
before it. On the bundled two-day data that tail falls in the overnight gap
and holds zero rows; on contiguous data it holds precisely the rows a naive
split would leak.

The five committed experiments (`research/experiments/README.md`) say what
the report says: all ITERATE, all leakage-clean, all four folds sign-consistent,
every holdout net-negative at 1× costs (EQ01 @ 1 s −301 bps of the capital
line, EQ03 @ 5 s −1173 bps, EQ06 −531 bps). The pinned-horizon runs
reproduce `research/alpha_reports/{EQ01,EQ03,EQ06}.json` at 1e-9
(`test_eq03_report_reproduces_through_the_runner`).

---

## 7. ML with meta-labeling

`research/ml_reports/run_ml.py` implements spec §14; `ML_REPORT.md` is the
committed result: 206,190 valid rows, 51 curated predictors across all 10
families, target `label_cost_5s`, four expanding walk-forward folds with a
60 s embargo and a 5 s purge.

### 7.1 The gate: advanced models must earn the right to run

Rule: **trees and the MLP run only if the best linear baseline's pooled OOS
IC against the mid-to-mid label is positive**. Simple models establish
whether signal exists; capacity-rich models then refine it. In the
committed run the best linear baseline (ridge) scores **−0.0430** against
the mid-to-mid label → gate **FAILED** → XGBoost, LightGBM and the MLP were
never fitted. Nothing above the linear tier was trained, so there is no
tree or MLP behaviour on this dataset to read anything into.

The gate did not always read that label. It used to read the model's own
target, the cost-adjusted return, on which the same linear models score a
mean fold IC of 0.9352 — and passed trivially, for the reason §7.2
explains. An earlier committed run therefore trained the full zoo and
crowned LightGBM at an "IC" of 0.70. Moving the gate to the frictionless
label is what turned a pass into the honest fail.

### 7.2 The crossed-book artifact: found, fixed, and honestly residual

An IC of 0.70 at 5 seconds — or the 0.94 the linear models score on the
cost-adjusted target today — would be the greatest alpha ever recorded. It is
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
corr(spread, target) = −0.937, and the linear models' IC of 0.94 on the
cost-adjusted target is mostly spread prediction.

Crucially, none of this was ever **leakage** — the shift-by-one test passes
throughout, because the spread at decision time legitimately is in the
information set. It is a *target-construction artifact* interacting with a
*generator artifact*. The honest directional measure — IC against the
mid-to-mid label — is ~0 to slightly negative for every model:
**no exploitable 5 s directional signal exists in this dataset**, exactly
what a near-random-walk generator should yield. The economics agree: under
the conservative cost model (realized costs floored at zero — you are never
paid to cross a crossed synthetic book), every model sits slightly below
0 bps/signal (elastic net: −0.147), while "label-exact" economics still show
+0.434 — a gap of 0.581 bps/signal of residual book-artifact. The report
instructs the reader to trust only the conservative column.

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

The committed run gates the elastic net's pooled OOS predictions (the linear
tier is the only one that ran): chronological 50/25/25
train/calibration/test split (60 s embargo), probability calibration on the
middle segment, and an *economic* meta-label (realized net P&L > 0 under
the conservative cost model — not "was the sign right"). The calibration is
**Platt scaling**, a two-parameter sigmoid, because the calibration segment
holds only 324 positives — below the pinned minimum of 500 for the isotonic
path.
Results: test AUC 0.753, Brier 0.0344, test base rate of profitable signals
0.037 — and at both τ = 0.5 and the calibration-chosen best τ = 0.300 the
gate keeps **zero** of the 11,461 test signals. The report flags this
`gate_degenerate: true` and refuses to dress it up: a gate that never fires
produces no evidence either way about whether abstaining pays. It shows
only that the calibrated probabilities sit under the threshold at a 3.7 %
base rate. The gate-off row (−2,281.9 total net bps, −0.20 bps/trade) is
what the ungated primary would have done. The machinery details still
matter: thresholds must be chosen on a calibration segment in probability
space and evaluated economically — and a gate that took no trades must be
reported as untested, not as vindicated. (Since v1.3.0 the reported
`auc_test` is `null` rather than a fabricated 0.5 when a test segment holds
a single class, and `impute_nan=False` is an opt-in alternative to imputing
missing meta-features to zero.)

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
- **Infeasible is an answer, not a NaN**: when no iterate satisfies the
  constraints (a gross cap cut below the minimum positions, say), the
  result says so — `feasible = false`, weights = the previous holding,
  `max_violation` in the audit — and the platform holds its position.
  Non-finite inputs are rejected before the first iteration. A solver that
  returns NaN weights to a risk engine is a production incident waiting
  for its first bad covariance (round 3, API_PORTFOLIO_TCA.md §1.3).

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
orchestration and, since 2026-09-19, a Python port (`iap.risk`, §16.2) that
the MVP loop runs; `tests/golden/expected_risk_decisions.json` pins a full
decision-vector replay that all three reproduce exactly, and the audit JSONL
and the state snapshot are byte-identical across the three engines.

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
  cancel, gap/recover, venue_down/venue_up, kill/unkill, override_loss,
  roll_session, snapshot) is a tiny domain-specific test format — a pattern
  worth copying for any stateful engine.

Round 3 (2026-09-06) hardened the semantics that a first implementation
tends to get subtly wrong — each one is now a pinned rule in
`PLATFORM_CONVENTIONS.md` §11.1 with a scenario test in Rust and Java:

- **Loss limits are mark-to-market.** A realized-only daily loss lets a
  position sail through the limit as long as nothing trades. The engine
  now evaluates realized + unrealized on every fill *and* on every market
  update of a held instrument, converted to the reporting currency, and
  latches with zero fills (`risk_mtm_loss_latches_without_fill`).
- **Notional means money.** `qty × qty_unit × price × tick × fx_rate`: a
  lot of USD/JPY is 100,000 units quoted in yen, and a JPY notional summed
  as dollars is off by two orders of magnitude. Conversion pairs live in
  `configs/risk/risk.json`; a missing or stale rate fails closed
  (`FX_RATE_MISSING`) instead of guessing 1.0.
- **Every in-flight order counts.** Tracking only resting LIMITs means
  three MARKET orders in the wire are invisible to the position projection.
  Now every allowed order is open until its terminal report, and the OMS
  callback (`on_order_done`) is part of the contract.
- **Marks carry market-data time.** Stamping the reference price with the
  decision clock makes the stale gate a no-op during a feed stall; the
  Java paper wiring now hands the engine the book event time
  (`paperStaleFeedRejectsOrders`).
- **Re-arming has a precedence.** `clear_kill` clears only the switch —
  the loss checks still reject and the next mark re-latches; the audited
  `override_loss_limit` (or a `roll_session`) has to come first. The
  runbook procedure is the test.
- **Byte-identical audit means integer formatting.** `format!("{:.2}")`
  and `String.format("%.2f")` disagree on ties; money in reasons goes
  through one integer-scaled formatter in both languages, with a
  decimal-tie golden.
- **Restart is a state problem.** `snapshot()` / `restore()` and a
  `NOT_BOOTSTRAPPED` mode make a redeploy resume bit-identically rather
  than start flat with a latched loss forgotten.

The kill-switch incident runbook
(`docs/runbooks/RUNBOOK_incident_kill_switch.md`) closes the loop from
mechanism to operations.

---

## 10. Execution algorithms and queue-position modeling

### 10.1 The algorithms

`cpp/include/iap/execution/algos.hpp` (C++ is the reference; Java and, since
2026-09-19, Python `iap.execution` port it against the same fills golden — §16.3)
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
  seed. Cancels travel the same path (rule 7): a cancel never overtakes its
  order and never undoes a fill that landed first; a child carries an
  `expire_ts` (the parent's `end_ts`) so no child outlives its window.
- **Venue state gate** (rule 8): a halted, auction-call or gap-stale venue
  book produces no fill of any kind; on re-open, crossed resting orders
  fill at the uncross touch. The SOR applies the same eligibility and
  returns "no route" rather than falling back to a stale venue.
- **Aggressive fills** walk the *displayed* top-10 depth, best-first, one
  fill per level; simulated orders never mutate the replayed book (the
  market stream stays authoritative); impact is charged economically
  instead.
- **Passive queue position**: `ahead_qty` starts as the displayed size at
  your level plus your own earlier orders resting there; EXECUTEs the book
  applied at your level deplete it (leftover volume after it reaches zero
  fills *you*, and one observed trade is one pool shared in queue order); a
  CANCEL advances you only when the cancelled order is known to be ahead of
  you, by the size the book actually removed; a trade-through (an execution
  at a price worse than yours) fills you at your limit but only up to the
  volume that printed; marketable ADDs — which generate no EXECUTE events
  (§3) — are *expanded* into their per-level consumptions and run through
  the same rules; a book whose display crosses your price fills you up to
  the displayed size that has not already been consumed, with a carefully
  pinned exemption preventing double-counting of liquidity you already
  took. MODIFYs never change `ahead_qty` (a modified order's queue position
  is unknowable from public data — pinned as ignored rather than guessed).
  The earlier versions of three of these rules — full-amount cancels, free
  trade-throughs, a crossing pool rebuilt on every event — were each
  optimistic in the direction that flatters a backtest; §22 is the case
  study.
- **Liquidity is consumed, not copied** (rule 3b): two children hitting
  the same displayed level inside one decision share one copy of it — the
  second sees the thin remainder. Simulated fills still never mutate the
  replayed book (the tape stays authoritative); the overlay only stops us
  from taking the same shares twice.

### 10.3 What the golden shows

`expected_replay_fills.json` (v2) runs two parents against the golden
equity vector: a passive VWAP BUY 400 (4 LIMIT slices joining the bid) and
an aggressive IS SELL 600 (3 front-loaded MARKET slices). The economics are
the lesson: the passive parent fills 329 of 400 shares with **negative
explicit cost** (−$0.658 — maker rebates) and leaves 71 unfilled when its
window closes, while the aggressive parent completes 600 paying $1.80 in
taker fees plus impact — a ~2 bps explicit swing between patience and
urgency on the same tape, and the timing risk of patience made visible
(paper 5 + erratum: v1 let the second slice fill 290 s after the window).
Every fill's price/qty/timestamp is exact; fees and impact match to 1e-9,
in C++ and Java alike.

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

Three more edges are pinned since round 3 (API_PORTFOLIO_TCA.md §2.1,
§2.4, §2.5; Python and Java held equal by the v2 golden's timeline cases):
a markout whose horizon runs past the end of the data (or across a halt) is
**undefined**, reported as `null` with the count of defined fills, never
fabricated from the last state; a passive fill is measured against the
state *before* the event that hit it (measuring against the post-print
book books the spread you captured as a cost); and a crossed consolidated
state is skipped and counted, while a locked one is a legitimate
zero-spread state.

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

- `tests/golden/` holds four pinned event vectors (2,000-event equity MBO,
  800-event FX quote, and two anomaly vectors of 1,403 / 561 events, all
  byte-exact JSONL) and expected outputs for codec (SHA-256 of the IAP1
  binary encoding — *byte* parity, the strongest possible claim), book
  states, anomaly states + counters, a cross-language checkpoint, a JSONL
  reject/accept fixture, features, alphas, backtest, risk decisions, replay
  fills, portfolio, and TCA.
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
  suites and prints the table (the v1.3.0 counts from CI, 2026-10-03: python 1563,
  cpp 289, rust 323, java 510 tests passed; golden groups 166/68/64/104; all
  PASS, plus `integration` (17) and `replay` (4) rows for the repo-level
  pytest suites, a `deployment` row — 25 structural checks passed in CI,
  where promtool and kubeconform are installed — and a `numbers` row that re-derives every headline
  figure in the docs from its artefact). The Java golden group runs all
  thirteen `*GoldenTest` classes (it once ran two of them and reported 18),
  the Rust group nine golden targets. The 2026-09-19/20 release added a new
  kind of golden: not a number to reproduce within a tolerance but a
  **byte sequence** — canonical JSON lines, a stream digest, the registry
  file — that four languages must produce identically (§18.3).

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
fast equity flow alphas (EQ02/EQ03/EQ12) shed ~15-21% of IC after one event
and ~66-82% after five (EQ03: 0.0254 → 0.0215 → 0.0086); EQ01's microprice
signal flips sign entirely by +5 events (0.0050 → 0.0083 → −0.0335). The
slow equity alphas do not notice even five events (EQ09: 0.1006 → 0.1055 →
0.1153, which drifts *up*; EQ11 keeps 95%), and the FX regime family
retains ~78-83% at one event (~15-22 s of FX tape) but only ~7-41% at five
(FX09: 0.1617 → 0.1342 → 0.0481). Alpha decay against *events* is the
economically meaningful axis.

**Measurement 2 — the speed of the stack.** The measured C++ hot path
(decode + book + features + alpha = 184.1 + 26.4 + 514.1 + 33.6 ns, the
2026-09-19 `benchmarks/results_cpp.md`) sums to ≈ 0.76 µs/event, versus a
median inter-event gap of ≈ 0.9 s on this dataset — six orders of magnitude
of headroom. (The ≈ 0.5 µs this section used to quote predates the
mandatory CRC-32 IAP1 trailer; paper 04's benchmark errata carry the
re-derivations; the table moves a few percent per regeneration and the docs
follow it.) Serialising a decision trace costs 31.7 µs per 5.6 KB record —
paid once per decision, off the event loop, ≈ 37 ns/event amortised. Rust
and Java demo-scale replays (≈ 6.5M events/s through the SPSC bus and
≈ 3.5M events/s) are equally overprovisioned.

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
alphas the policies performed 10 (static) / 10 (weekly) / 20 (daily) /
**126 (drift-triggered)** total refits — and none of that activity changed the
economics: every alpha stays net-negative after costs, and the P&L spread
between policies is one to two orders of magnitude smaller than the cost
drag. Refitting neither rescues nor ruins any alpha here.

### 14.4 The lifecycle state machine: hysteresis against flapping

(The ACTIVE → WATCH → RETIRED machine below is the *live* sub-machine; the
full seven-state promotion lifecycle that leads up to ACTIVE is §17.)

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
under the drift-triggered policy it refits 40 times (vs 1 static) while
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
allocation, and each of the 40 deployments (10 alphas × 4 refit policies) is
counted once in the multiple-testing ledger — round 3 stopped charging the
denominator for all 211 monitoring evaluations inside a single deployment.
"We built the machinery and proved it behaves; we did not prove
it makes money" is the adaptability layer's version of the platform's
central discipline: research truth over backtest cosmetics.

---

## 15. Contracts and Protocols: typing the loop

### 15.1 Why a contract layer at all

Until 2026-09-19 the platform's stages agreed on *schemas* (seven JSON
Schemas) and on *wire bytes* (JSONL, IAP1) but the Python code passed
pandas frames, dicts and ad-hoc dataclasses between them. That is fine for
research and fatal for a loop: the risk engine, the router and the TCA each
had their own idea of an order. The contract layer (`iap.contracts`,
[API_CONTRACTS.md](API_CONTRACTS.md)) pins one type per schema — 17 schemas,
22 frozen dataclasses including the nested records — and one
`runtime_checkable` Protocol per interface (18: `MarketDataSource`,
`OrderBookLike`, `FeatureEngineLike`, `Alpha`, `PortfolioConstructor`,
`RiskEngineLike`, `ExecutionAlgorithm`, `SmartOrderRouterLike`,
`ExecutionSimulatorLike`, `TCAEngine`, `ExperimentRunner`, `LifecycleGate`,
`AlphaLifecycle`, `TraceSink`, …).

### 15.2 What "validated on construction" buys

Every type checks its own domain when built — `bool` is never an int, a u16
venue id is a u16, a float is finite, an enum is a member, a version is 64
hex, an id has no `|` — and then its invariants: `ALLOW ⇔ rule_index == -1`,
`confidence == 0 ⇒ expected_return == 0`, `IS = delay + trading +
opportunity`, `net = gross − cost`, `leakage_passed == False ⇒ verdict ==
REJECT`. `from_dict` rejects unknown *and* missing keys; `to_dict` is
JSON-ready; the round trip is exact. The consequence is the one that matters
for a trading system: **a document that no longer satisfies its contract is
an error at the boundary, not a value somewhere downstream.** The store
validates on write (`validate_typed`, jsonschema + `referencing`, offline)
and re-types on read; every trace sink validates before emitting.

### 15.3 Protocols, and the honest "satisfied by" column

A Protocol is a promise about behaviour, not a base class, so the existing
reference components did not change to satisfy them — adapters did. The MVP
wraps `iap.risk.RiskEngine` as `RiskEngineAdapter`, `iap.execution.algos` as
`AlgoScheduler`, the SOR as `SorAdapter`, the simulator as
`SimulatorAdapter`, `iap.tca.order_tca` as `TcaAdapter`, and
`test_components_satisfy_the_contract_protocols` asserts each with
`isinstance`. Two Protocols have no implementation (`Feature`, because the
feature families are module functions; `OrderBookLike` is satisfied by
`OrderBook` but not by `ConsolidatedBook`), and API_CONTRACTS.md §4 says so
rather than pretending the table is full. One Protocol carries a rule in
its docstring that the rest of the platform is built around:
`RiskEngineLike.evaluate` is a pure function of the order and the engine's
own state — no wall clock, no I/O, no model, no LLM (§13.7 of the
conventions; ARCHITECTURE.md §11).

### 15.4 Canonical JSON: the byte-level contract

Contract documents are serialised as canonical JSON — keys sorted by code
point, `,`/`:` separators, ASCII escapes, integers exact, floats as Python's
shortest round-trip repr with its exact exponent rule, NaN rejected — and
`content_hash` is the sha256 of that text. That single rule is what makes
`config_version`, `experiment_id`, `portfolio_version` and the trace digest
comparable across four languages. Getting it byte-identical was real work:
Java's `Double.toString` prints two significant digits where Python prints
one (`4.9E-324` vs `5e-324`); Rust's serde_json *parser* was 1 ulp off on
17-digit decimals until the `float_roundtrip` feature was enabled; C++ lays
`std::to_chars` output back out under Python's rule. The golden
`expected_canonical_json.json` pins 2663 float bit patterns (612 of them exact
decimal midpoints and 17-digit values, where round-half-even at the last digit
is what distinguishes a faithful port), 24 escapes and 9 documents, and all four languages reproduce every byte (§18.3).

---

## 16. The Python risk and execution reference ports

### 16.1 Why port a reference you already have

The principle says Python defines the semantics; the risk engine's rule text
lives in Rust and the fill model's in C++ (§9, §10). Before 2026-09-19 that
left Python unable to run the loop it was the reference for — the MVP would
have had to stub risk or execution, and a stub is precisely what a golden
cannot vouch for. `iap.risk` and `iap.execution`
([API_TRADING.md](API_TRADING.md)) close the gap the honest way: not by
moving the rule text, but by consuming the same golden files the Java ports
consume, and matching them to the byte.

### 16.2 `iap.risk`: statement for statement

The port keeps the Rust engine's structure — the 23-check order, the
average-cost lots per (strategy, instrument, quote currency), the event-time
token bucket, the latching loss limits, snapshot / restore — and its
arithmetic *order*: `float(bid + ask) * tick / 2.0`, `BTreeMap` iteration in
sorted key order so float sums accumulate identically, a fold from `-0.0`
because Rust's `Iterator::sum::<f64>()` does. Two things had to be
reproduced rather than approximated: **serde_json's bytes** (ryu float
layout `1e+16` / `1e-6` / `3.0`, sorted keys, `\u00xx` escapes, accessor
strictness where `50000.0` is not an integer) for the audit lines and the
snapshot, and **`fmt_fixed`**, the integer-scaled half-away money formatter
in every reason string. The result on the goldens: every decision, rule id
and severity of the 110-step script exact; the 77-line audit byte-identical;
the snapshot after step 73 byte-identical; restore from any step
bit-identical to the unbroken run. Beyond the goldens, `format_f64` was
cross-checked against the real Rust toolchain on 24,993 random doubles and
`fmt_fixed` against a verbatim copy of `event.rs` on 20,012 samples — zero
mismatches.

The port also pins the one thing a Python port can silently get wrong:
integers. Every i64/u64 operation is range-checked — never a Python bigint,
never a wrap. Until v1.3.0 "out of domain" meant an `OverflowError` at the
statement where Rust's overflow-checked test build panics, which sounds
equivalent and was not: an exception in the order path is a crash, the Rust
release build and Java wrapped instead, and none of the three *rejected*.
The arithmetic an order or a fill can drive out of range is now a decision
in all three engines (§21.3).

### 16.3 `iap.execution`: nine rules, seven fills, bit for bit

The simulator port implements the nine pinned rules of `execution.hpp`
(§10.2) — latency with one SplitMix64 jitter draw per submit *or cancel*,
activation, the aggressive walk of displayed depth that never mutates the
replayed book, the consumed-liquidity overlay, deterministic queue position,
fees, linear impact, cancels and expiry, the venue trading-state gate, the
processing order — plus TWAP/VWAP/POV/IS slicing, the SOR ladder and the
replay driver. `test_execution_golden.py` runs the exact C++ generator
scenario and matches `expected_replay_fills.json`: ids, ticks, quantities,
timestamps and liquidity flags exact; every money field within the 1e-9 the
C++ and Java tests use *and additionally bit-identical*, because the
`%.17g` doubles in the file round-trip to exactly the Python doubles when
the IEEE operations happen in the same left-to-right order. Sixty-four rule
tests mirror every scenario of the C++ and Java suites, and 15 property
cases add what a scenario cannot: the book after every event is identical
to a bare `OrderBook` replay (simulated fills never touch it), fills stay
inside `[arrival, expire]`, filled never exceeds submitted. PEG/MID are not
implemented — exactly as in C++ and Java — and the port says so instead of
inventing a fill rule.

### 16.4 What this does and does not change

Rust remains normative for the risk rule text and C++ for the execution rule
text; their golden generators stay where they are; a rule change is still a
Rust or C++ change first (GOVERNANCE.md §1). What changed is that the
platform's principle now holds for the whole loop: the loop the MVP runs is
the reference loop, and the two components that were "ports only" are
proven equivalent by the same files as before. PLATFORM_CONVENTIONS.md §11
states this in exactly those words.

---

## 17. The seven-state promotion lifecycle

### 17.1 From a verdict to a state

The research report ends in a verdict — PROMOTE / ITERATE / REJECT — and the
adaptive layer starts at ACTIVE (§14.4). Between them there was nothing: no
record of *where* an alpha stood, what evidence it had cleared, or who
decided. `iap.lifecycle` ([docs/LIFECYCLE.md](docs/LIFECYCLE.md)) fills that
gap with a table-driven machine — RESEARCH → CANDIDATE → VALIDATING → PAPER
→ ACTIVE ⇄ WATCH → RETIRED, seven states, seventeen edges, eighteen gates —
in which every SYSTEM edge names the gates it evaluates in order, every
gate reads one field of a typed evidence document, and every transition is
one `LifecycleTransition` line with the gate results, the policy and the
actor. The live edges are the unchanged `LifecycleTracker` of §14.4,
wrapped; nothing in `iap.adaptive` moved.

### 17.2 Run it

```bash
cd python
PYTHONPATH=src python3 -m iap.lifecycle bootstrap --dry-run   # 24 reports -> evidence -> advance until still
PYTHONPATH=src python3 -m iap.lifecycle status                # the registry table
```

The `status` table reads, for all 24 alphas, `CANDIDATE` — and in the
"failed gates" column, for every one of them, `net_pnl_after_costs`. That is
the promotion report's finding restated by a state machine that reads the
same numbers through a different gate table, which is the reason for
pinning both: two independent readings of one artefact agree.
`test_bootstrap_failed_gates_agree_with_report_verdicts` proves it
mechanically (it recomputes the failed-gate set from each report's raw
numbers and the thresholds). Read the table closely and the research story
of §6 reappears: EQ01/EQ02/EQ03/EQ06/EQ11/EQ12 and FX04 fail *only* the
cost gate (statistics real, economics not); EQ04/EQ07–EQ10 fail the IC and
significance gates and often `hypothesis_sign`; the FX regime family fails
`stability` — the Pearson/rank-IC gap, the one pair of numbers in a result
that measures the *shape* of the signal–label relation rather than its
strength.

Why is FX01 ITERATE with a negative pooled IC? Because the gate reads the
**uncrossed** IC (the bootstrap maps `ic ← gate_ic`, exactly what the
PROMOTE gate reads), and FX01's uncrossed IC is +0.018 (§6.5). The machine
inherits the report's conditioning rule instead of re-deriving a different
truth.

### 17.3 Silence is not evidence

The rule that took the most care is about *absent* evidence. If the block an
edge needs is missing — no research result at CANDIDATE, no validation
block at VALIDATING, no paper sessions at PAPER, an uninformative or null
rolling IC at ACTIVE — nothing is evaluated and nothing moves, not even the
failure counter; the evaluation is recorded as `NO_EVIDENCE`. §14.6 shows
what happens otherwise: a monitor re-reading one frozen window retired an
alpha on six copies of the same number. RESEARCH is the one deliberate
exception — `ledger_entry_exists` *is* the presence check, so an empty
document there is a `HOLD` with `value = null`, which is the right answer
to "has this idea been ledgered?".

### 17.4 One alpha's life: the LC01 golden

`tests/golden/expected_lifecycle.json` scripts three lives, compared
exactly, step by step, in Python, Java and Rust. LC01 is the whole ladder:
RESEARCH → CANDIDATE on a ledger entry and a clean leakage test; →
VALIDATING on the nine research gates; → PAPER on a reproducible replay and
parity; → ACTIVE on five paper sessions with a tracking IC; then a hold, a
null IC (nothing moves), an uninformative IC (nothing moves), a breach →
WATCH, a second breach, a neutral-zone reading that resets both counters,
three recoveries → ACTIVE, another breach → WATCH, six consecutive breaches
→ RETIRED, a SYSTEM advance that records `TERMINAL` and moves nothing, and
a HUMAN reset → RESEARCH. LC02 is the leaking re-run demoted to RESEARCH at
once; LC03 the three paper failures demoted to CANDIDATE and a HUMAN
retire. Everything a port could get subtly wrong — which counter resets
when, whether entering a breach counts, whether RETIRED can be left by the
system — is a pinned field of a pinned step.

### 17.5 The caveat that stays

In the live Java paper loop the lifecycle gauge is observational (§14.4,
README): a RETIRED alpha keeps trading and `AlphaLifecycleRetired` pages a
human. The seven-state machine decides *state*; making RETIRED an
allocation gate in the live loop is backlog issue L04, and every document
that mentions the lifecycle says so.

---

## 18. The decision trace, and replaying an incident

### 18.1 One record per decision

"Why did we trade?" has to be answerable from one artefact, after the fact,
on another machine. The `DecisionTrace` ([docs/DECISION_TRACE.md](docs/DECISION_TRACE.md))
is that artefact: a header (an id, the session, the instrument, the
triggering event's time and sequence, and the four version hashes in force
— data, features, model, configuration) and nine stage lists in loop order
— signals, the portfolio target, risk decisions, parent order, child orders,
routing with every venue scored, fills, TCA, attribution. A stage that did
not run is empty, never fabricated: a REJECT trace stops at risk; a C++
replay trace has execution stages and nothing before. `signal[0]` is the
acting signal (the ensemble the portfolio sized on) and the members follow,
each labelled with its own alpha id — a rule that was pinned after the first
MVP attributed EQ01's signal to every order because the view joined the
first signal of the trace.

### 18.2 Ids without clocks

The trace id is the first 32 hex of `sha256("session|instrument|event_ts|sequence")`:
two runs of the same session produce the same ids, and any language
reproduces them with string concatenation and one hash (pinned:
`8b9fed6896d01463e64c4de915b0614b` for the golden inputs). The stream
digest is sha256 over every canonical line + newline in emission order;
same seed ⇒ same digest; one changed field or one swapped line changes it.
Known answers are pinned for one trace, the same trace twice, and the empty
stream; the MVP golden pins a whole session's digest (`d938eeae…`, 355
traces).

### 18.3 Four languages, one line

The pinned `explain()` block — nine lines from `Order 12345` to
`Attribution: alpha = +6.2 bps …` — is reproduced byte for byte by Python,
Java, Rust and C++ from the same example trace, as is the 5,627-byte
canonical line and its sha256. Java's `PaperTrading` writes one trace per
pre-trade risk decision to `decision_traces.jsonl`, fsynced with the risk
audit, resumable, counted (`trace_records_total`); C++'s `ExecutionReplay`
writes one per parent order after the run (never inside the event loop —
31.7 µs per trace to serialise, which is why); Rust's `contracts` crate
provides the sink and digest the telemetry crate re-exports. The trace is
the first golden of a new kind in this repository: a byte sequence rather
than a number within a tolerance.

### 18.4 The incident flow

Capture → replay → reproduce → debug → fix → regression test
([docs/runbooks/RUNBOOK_incident_replay.md](docs/runbooks/RUNBOOK_incident_replay.md)).
Every MVP run keeps its input stream and its configuration; `python -m
iap.mvp replay --run <dir>` re-runs the loop from the capture — never from
the generator — and must reproduce the digest, the report and the stream
hash, refusing first if a reference document changed since the run
(`config_version`). `explain` and the `v_order_chain` view locate the
decision; the engine is a plain object you can step under a debugger; a
fix shows up as a digest diff; the captured stream and its expected digest
become a golden. A replay that does *not* reproduce is itself the finding:
wrong capture, wrong configuration, or a non-deterministic component with a
test to write.

---

## 19. The data model: an index, not a database

### 19.1 Files are the truth

The platform's records live in flat files — Parquet feature frames, JSON
goldens, research documents, JSONL audits and traces — that are
deterministic, checksummed and archived. `schemas/sql/iap_v1.sql`
([docs/DATA_MODEL.md](docs/DATA_MODEL.md)) is a relational *index* over
them: 24 tables and 3 views into which every contract maps, built by
`python -m iap.store build` in about a second, byte-identical on a rebuild
from unchanged files, and never committed. Dropping it loses nothing. That
framing decides the design: the DDL is portable (SQLite 3 and PostgreSQL ≥
13 unchanged — `BIGINT` / `DOUBLE PRECISION` / `TEXT` only, enforced by a
whitelist parser in CI because there is no PostgreSQL there), every write is
an idempotent upsert of a validated document, every read re-types, and
there is no `NOW()` anywhere.

### 19.2 What the views answer

`v_order_chain` is one row per parent order — signal → portfolio leg → last
risk decision → child/venue counts → fills/fees → TCA → attribution — the
same chain `explain()` prints as text. `v_alpha_scorecard` joins each
alpha's latest experiment result, verdict, lifecycle state and ledger count;
`v_experiment_ledger_summary` gives the multiple-testing denominator per
kind. The questions the spec's observability section asks — why did we
trade, why was X rejected, what is the denominator, is live IC drifting
from research — are one query each, and COOKBOOK recipes 20, 21 and 24 run
them.

### 19.3 Honest mappings

The alpha reports predate the contracts and do not record a Sharpe, a
drawdown, a seed or a commit. The importer says so — `max_drawdown_bps =
sharpe = 0.0` listed under `configuration.unrecorded`, `git_commit =
unversioned-workspace`, the P&L basis recorded — rather than inventing
values; the TCA research harness gets its own `tca_orders` table for the
same reason (no algo, no latency, prices in real units). The adaptive
study's `lifecycle_log.jsonl` is imported as a *policy comparison* and never
sets an alpha's state; only the lifecycle service's own artefacts do. An
index that lied about what its sources contain would be worse than none.

---

## 20. The MVP walkthrough, with the honest numbers

### 20.1 One command

```bash
cd python && PYTHONPATH=src python3 -m iap.mvp run
# mvp run 58a10f2194a3c81c: events=16578 decisions=355 parents=66 children=105 fills=55 pnl=-22.651183 USD digest=d938eeae68c85a6c...
```

Seven seconds later `data/mvp/58a10f2194a3c81c/` holds the captured stream,
every decision as a trace (JSONL and SQLite), the risk audit, the TCA of
every parent, a report and the paper evidence the lifecycle reads
([docs/MVP.md](docs/MVP.md)). Nothing in the loop is a stub: the seeded
generator and normaliser are the research ones, unmodified; the books, the
feature engine, the three fitted alphas, the PGD optimizer, the risk engine
(§16.2), the algos, the SOR and the simulator (§16.3), the TCA and the
attribution are the reference components the goldens already pin, composed
over the Protocols of §15. The per-event order mirrors the Java
`BacktestEngine` / `PaperTrading` wiring rule for rule (docs/MVP.md §4 is
the row-by-row review), and the §12.1 money identity — `pnl.total ==
(gross − spread) − fees − impact`, risk daily P&L == gross − spread — is
asserted after every fill (|diff| 3.3e-11 on the golden run).

### 20.2 The numbers, stated as they are

Seed 12345: 16,578 events, 355 decisions, 66 parent orders, 212 children
generated of which 105 submitted (107 blocked by the 500 ms slice-interval
control, exactly as the Java loop would), 55 fills, 20.2 % fill rate.
Risk: 105 ALLOW, 0 REJECT, 0 KILL, 9 sequence gaps each recovered by the
following SNAPSHOT burst, 72 mark regressions dropped. Routing: XV3 75.9 %,
XV1 12.1 %, XV2 11.9 % of filled quantity (aggressive routing picks the
cheapest taker fee on price ties; passive routing prefers XV1's rebate).
TCA: implementation shortfall +0.106 bps quantity-weighted; TWAP fills
3.0 % (passive limits at a 1 s horizon mostly expire), POV 13.9 %, IS
68.9 %. **P&L −22.65 USD** on 3,126 shares: +0.039 bps of alpha
contribution against −0.41 bps of execution cost. The report field is
`alpha.cost_negative: true`. This is the research finding of §6 — real
signal, no money after costs — reproduced by a full loop on a different
synthetic stream, and it is the headline number of the MVP, not a footnote.

### 20.3 The IC audit: a worked example of "disbelieve it, then decompose"

The first version of the loop reported realized ICs of 0.285 / 0.338 /
−0.102 for EQ01 / EQ03 / EQ06 against research ICs of 0.027 / 0.030 /
0.043 — an order of magnitude apart. The pitfalls of §27 say what to do
with a number like that, and docs/MVP.md §7.1 records doing it:

1. **Pin the definition.** The realized IC is now computed by
   `iap.labels.compute_labels` on the feature engine's book-refresh mid
   series — the research label, not a timeline-based approximation — at the
   MVP horizon and at each alpha's fitted horizon. A test rebuilds the frame
   independently and asserts the same anchors, the same valid set, the same
   labels to 1e-9 and the same IC to 1e-12. The old definition had let
   8 / 7 / 2 windows through that contained a stale-venue blackout.
2. **Prove it is not a leak.** The engine is single-pass; cutting the stream
   at 35 % and 70 % reproduces every earlier signal and target bit for bit
   (the truncation probe); and the *research* code path run on the same
   captured stream gives the same 0.283 / 0.336 / −0.106 at 1 s and
   0.275 / 0.276 / −0.111 at the 100 ms research cadence. The number is a
   property of the data.
3. **Read the shift-by-one honestly.** Lagging the signal by one decision
   collapses the IC to 0.031 / −0.034 / 0.021. At a 1 s cadence equal to
   the 1 s horizon that collapse cannot discriminate a leak from a genuine
   fast signal — the shifted window simply does not overlap the label —
   which is exactly why `iap.validation.leakage` scales its required
   survival ratio by `1 − row_gap / horizon` (zero here). At 100 ms the
   one-row shift keeps 0.219 of 0.275: the signature of a genuine,
   autocorrelated microstructure signal. Items 1–2 are the evidence; item 3
   is reported with its limitation.
4. **Explain the data.** The generator quotes every venue around one shared
   efficient price plus a bounded AR(1) venue noise (ρ 0.9 per ≈ 200 ms
   slot) and cancels resting orders the efficient price has moved through:
   the displayed book *leans* towards the efficient price and the mid
   converges to it within about a second. Microprice deviation and OFI
   measure precisely that lean. A real feed would not be this kind.
5. **Check that it still does not pay.** The cost-adjusted IC — buy the ask
   now, sell the bid at t + h — is +0.017 / +0.065 at 1 s. The predicted
   move is smaller than the spread; that is the −22.65 USD.
6. **Let the lifecycle see it.** `paper_evidence.json` carries the realized
   IC at the fitted horizon, so the `paper_ic_tracking` gate (max gap 0.01)
   fails EQ01 and EQ03 on this data — correctly: paper behaviour that
   differs this much from research is a finding, not a promotion.

### 20.4 Determinism, twice

`python -m iap.mvp verify` runs the session twice from scratch and compares
bytes; `replay` re-runs it from the captured stream and must reproduce the
digest. `tests/golden/expected_mvp.json` pins the golden run for any port
of the loop (docs/MVP.md §9 lists what a port must reproduce: the feed
hashes, the version hashes, the per-event order, the decision rule, the
children, the trace, and every key of the report). The audit of the module
for wall clocks, randomness and unordered iteration is written down in
docs/MVP.md §5; the known limits — one instrument, one session, an optimizer
t-cost set below the modelled cost so the loop trades at all, passive TWAP
children that almost never fill at a 1 s horizon, a single-instrument
time-series IC with no fold structure and no ledger entry — are stated
there rather than left for a reader to discover.

---

## 21. Fail-closed risk engineering: four bugs, worked

§9 states the principle: when anything is wrong or unknown, the answer is
REJECT. The v1.3.0 review found four places where the hard risk engine did
the opposite, in all three languages at once. None was exotic. Each is a
guard that was correct for every input anyone had fed it, and open for one
nobody had. They are worth studying one by one because the pattern repeats
in every safety system: *a check that cannot fail is not a check*.

All four fixes kept the existing rule ids, so the pinned check order of
PLATFORM_CONVENTIONS.md §11.1 did not move, and the goldens that existed
before are reproduced unchanged. The normative text is §11.1; API_TRADING.md
§1.4 has the table of conditions, rule ids and reason strings.

### 21.1 The mark from the future

The stale-price gate asked one question: is the reference price too old?

```
age = order.timestamp - mark.timestamp
if age > stale_feed_timeout_ns:  reject STALE_PRICE
```

Now give it a mark stamped one hour *ahead* of the order — a corrupt
timestamp, a venue clock that jumped, a unit mix-up. The age is minus one
hour. Minus one hour is not greater than five seconds, so the mark is
trusted. It gets worse: the engine drops market updates older than the one
it holds (they are counted as regressions, conventions §11.1), so every genuine update that
arrives afterwards is *behind* the corrupt stamp and is discarded. The
engine now prices every order off a frozen, wrong mark, and will do so
until event time catches up with the corrupt stamp.

The obvious fix — reject when `age < -timeout` — is wrong, and the golden
says why. Step 107 of the main risk golden is an order whose *own* clock has
regressed by nine seconds; the pinned behaviour is that it still reaches the
throttle and is rejected `RATE_THROTTLE`. An order with a stale clock next
to a healthy mark must not be confused with a healthy order next to a mark
from the future. So the engine measures the mark against its **event
clock** — the latest order event time it knows, this order's timestamp or
the newest throttle-bucket time, whichever is later:

```
if mark.timestamp > clock + stale_feed_timeout_ns:  reject STALE_PRICE
   "reference price timestamp <ts> is more than <timeout>ns ahead of the
    latest order event time <clock>"
```

The conversion rate gets the same rule (`FX_RATE_MISSING`). No config key
and no snapshot field was added. Notice what the fix does *not* do: it does
not recover the instrument. Genuine updates behind the corrupt stamp are
still dropped by the regression rule, so orders in that instrument keep
rejecting. That is the point. Closed, counted and visible in the audit log
is the safe side of a corrupt clock; a human decides what happens next.

### 21.2 NaN is not greater than anything

Every float limit was written the natural way:

```
if order_notional > max_order_notional:  reject FAT_FINGER_NOTIONAL
```

IEEE-754 says every ordered comparison with NaN is false. A NaN notional
is not greater than the limit, so the order passes — and it passes every
other float limit after it for the same reason. Where does a NaN come from?
A `tick_size` of NaN in the reference data turns every notional into NaN;
the Java record even accepted `+Infinity`, because `Infinity > 0` is true.

Two fixes, one for each end. At the source, reference data must be finite
and positive: Java and Python refuse to construct an invalid
`InstrumentRef`, and Rust — whose struct fields are public, so construction
cannot be the guard — re-validates in `RiskEngine::new` and lands the
whole engine on `CONFIG_MISSING`. At the comparison, every limit is written
so that NaN *fails* it:

```
if not (order_notional <= max_order_notional):  reject FAT_FINGER_NOTIONAL
```

The two forms agree on every real number and differ only on NaN, which is
exactly the case the first form got wrong. The same rewrite covers the
price band, the throttle (`not (tokens >= 1)`), the instrument, gross and
net notional limits and both loss limits (`not (pnl > -limit)`).

### 21.3 One overflow, three languages, three behaviours

A position projection is `pos + open_orders + qty`. Feed it quantities near
2^63 and the three engines did three different things:

| engine | what happened | what the caller saw |
|---|---|---|
| Python port | an explicit range check raised `OverflowError` | an exception in the order path: a crash, not a decision |
| Rust, test profile | overflow-checked arithmetic panicked | the same, and the goldens are proven under this profile |
| Rust, release build; Java | two's-complement wrap | nothing: the sum wrapped and the limit check ran on the wrapped number |

Three implementations "held byte-identical by golden tests" disagreed, and
the goldens could not see it because no golden step overflowed. Now all
three do the same thing, and it is a decision:

- Position accounting lives in the **symmetric** domain
  `[-i64::MAX, i64::MAX]`. Excluding `i64::MIN` matters: it has no negation,
  so `abs()` of it overflows again one line later.
- A projection that leaves the domain rejects `MALFORMED_ORDER`,
  `projected position overflows i64 (fail-closed)`.
- A *fill* that cannot be booked is harder: a fill is a fact, it cannot be
  rejected. The engine applies nothing and latches the GLOBAL kill, because
  an engine that cannot book a fill no longer knows its exposure.
- Timestamp differences (mark age, the duplicate window, the throttle's
  elapsed time) are checked the same way and reject `MALFORMED_ORDER`,
  `timestamp arithmetic overflows i64 (fail-closed)`.

As a backstop the Rust release profile now builds with
`overflow-checks = true`, so an unchecked add that slips through panics
instead of wrapping. The primary control is still the checked arithmetic:
a panic in a risk engine is an outage.

### 21.4 Venue 0

An order names its venue; venue 0 means "the smart order router will
choose". The venue kill check was written per venue:

```
if order.venue_id != 0 and venue_killed[order.venue_id]:  reject KILL_VENUE
```

Kill venue 2 during an incident and every order pinned to venue 2 is
rejected. Every order sent through the router sails through — and the
router is free to send it to venue 2. The paper platform made this the
common case: a SOR session passed venue 0 to the pre-trade check for every
child. The same hole existed for disconnected venues.

The fix is in two places, because the defect was. In the engine, a venue-0
order rejects `KILL_VENUE` while *any* venue kill is engaged (the reason
names the lowest killed venue id), and rejects `VENUE_DISCONNECTED` when
*every* venue the engine knows of is disconnected — while one is up the
router still has somewhere to go. In the Java wiring, the pre-trade request
now names the venue the child is actually routed to (a replica of the
engine's router, evaluated at the same instant), and a child that leaves
for a different venue than the one approved is counted and cancelled.

### 21.5 Why the tests had not caught any of it

The main risk golden drives **one engine through one script**. That is the
right design for pinning a rule order, and it structurally cannot reach a
configuration that fails closed, an engine awaiting bootstrap, a restore,
or a kill command that does not parse — each needs a different engine. The
new edge golden (`tests/golden/expected_risk_edge_decisions.json`) is eight
independent scenarios, each with its own engine, and all three languages
must reproduce its decisions and its audit log byte for byte.

It does not cover everything, and the gaps are stated: the NaN comparisons,
the invalid-reference-data landing and the future-stamped conversion rate
are pinned by per-language rule tests only. Differential fuzzing of the
three engines against each other is the tool that would have found the
overflow disagreement without a human reading the code; it is backlog
(EPICS E31).

**Check yourself.**

*Q. A mark is stamped two seconds ahead of the order. Is that rejected?*
No. The tolerance is the same window as staleness, `stale_feed_timeout_ns`
(5 s in `configs/risk/risk.json`); a small positive skew between a feed
clock and an order clock is normal. Only a stamp more than the timeout
beyond the event clock is treated as untrusted.

*Q. Why is "reject" the wrong answer for a fill that overflows the
position?* A fill has already happened at the venue. Rejecting it would
leave the engine's position different from reality with nothing to say so.
Not booking it and latching the kill switch says: stop, the books no longer
reconcile.

*Q. Three engines agreed on every golden step and still disagreed on
overflow. What does parity prove?* That the implementations agree on the
inputs in the golden. It proves nothing about inputs that are not there,
and it cannot tell agreement on the right answer from agreement on a wrong
one (§22 has the same lesson from the simulator).

**Exercise.** Run the regression tests for these four cases, then see the
NaN rule in two lines:

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_risk_rules.py \
  -k "future_stamped or nan_limit or overflow or sor_order or extreme_timestamps"
# 7 passed, 82 deselected
python3 -c "nan = float('nan'); limit = 1e6; print(nan > limit, not (nan <= limit))"
# False True
```

Then replay the edge golden (COOKBOOK recipe 34) and find, in its output,
the audit line each of §21.1, §21.3 and §21.4 produces.

---

## 22. Simulator realism: the fills that never happened

A backtest is only as honest as its fills. §10.2 lists the simulator's
rules; this chapter is about three ways it was generous, each found by
asking the same question of a rule: *where did that liquidity come from?*
PLATFORM_CONVENTIONS.md §14.1 states the governing principle — the
simulator never fills more than the market actually traded — and
API_TRADING.md §2.4 lists the v1.3.0 rule changes.

### 22.1 The over-fill: 50 shares, sold twenty times

Rest a buy for 1,000 at 100. An ask of 50 posts at 100: the book is now
crossed against our order, no trade prints, and the simulator's crossing
check fills us from the displayed size of that crossing level. Fifty
shares. Correct so far.

The check ran after every event, and it rebuilt its pool from the display
each time. The ask still showed 50 — simulated fills never mutate the
replayed book, by design — so the next event, any event, a heartbeat,
filled another 50. Twenty unrelated events later a 1,000-share order was
filled against 50 shares of liquidity.

The simulator already had the right tool. Rule 3b keeps an *overlay* of
displayed size our aggressive fills have consumed, precisely so that two
children cannot take the same shares. The crossing check simply did not
use it. Now its pool is `displayed − consumed` and it debits the overlay
with what it takes. The overlay's existing refresh rule does the rest:
when a level's displayed size changes, the consumed amount becomes
`min(consumed, new displayed)`, so only liquidity added *on top of* a
consumed level becomes available. The regression test walks exactly that
sequence: 50 fills once; three unrelated events fill nothing; 30 more
shares post and exactly the new 30 fill; the first ask cancels and nothing
is resurrected.

### 22.2 The duplicate execute: trusting the wire over the book

Queue tracking read the raw event stream: an EXECUTE at our level reduces
the quantity ahead of us and, once that reaches zero, fills us. It ran
*before* the book applied the event and never asked whether the book
accepted it.

The book rejects things. A retransmitted duplicate (same sequence number)
is dropped. An EXECUTE naming an order id the book does not hold is
dropped. An EXECUTE quoting 500 against an order with 300 remaining trades
300. In each case the simulator had tracked the event as quoted: the
duplicate advanced our queue a second time, the phantom order traded
volume that never existed, the over-quoted execute moved 200 shares too
many.

The rule is now one sentence — **queue tracking trusts the book, not the
event**. It runs after the book update and only when the book reports the
event `APPLIED`. An execute trades `min(event qty, the book order's
remaining)` at the *book* order's side and price, whatever the event says.
The pre-event depth is captured first, so the order in which fills are
produced did not change.

### 22.3 The cancel from behind

When someone cancels at our price level, do we move up the queue? Only if
they were ahead of us. The old rule moved us up for every cancel at the
level, by the quantity the event quoted — pinned and documented as a
"deterministic choice", and optimistic: a cancel at our level may just as
well be an order that joined after us, and crediting it moves us up a queue
we have not actually advanced in.

The simulator now remembers, per resting order of ours, which market
orders joined the level after it did — an ADD at our price, or a MODIFY
that increased an order's size and so sent it to the back. A cancel of one
of those does not advance us. A cancel of a real order that was there
before us does, by the displayed size the book actually removed. On a
level without order ids (QUOTE and SNAPSHOT records get synthetic ids) the
simulator cannot know, and assumes the cancel came from behind. Every one
of those choices can only reduce fills.

### 22.4 The lesson the goldens could not teach

All three defects were implemented identically in C++, Java and Python,
and the fills golden passed throughout — before and after. The golden
sessions simply contain no static crossed display, no dropped event and no
cancel from behind, so the fix reproduces `expected_replay_fills.json`,
`expected_mvp.json` and `expected_tca.json` unchanged. Cross-language
parity told us three implementations agreed. It could not tell us they
agreed on a rule that fabricated liquidity. That needs a different kind of
test: an invariant ("filled quantity never exceeds what the market
displayed or traded") checked over generated event sequences. The existing
property tests cover filled ≤ submitted, fills inside the order's window
and an untouched book; the liquidity-conservation invariant is backlog
(EPICS E31, FZ02).

And the simulator is still a model. None of this calibrates it to real
fills; that is a separate backlog epic (E27) and, without live or
paper-venue fills to calibrate against, cannot be done in this repository.

**Check yourself.**

*Q. Why not let simulated fills mutate the replayed book, so the 50 shares
are simply gone?* Because the tape is the record of what the market did
without us. Mutating it makes every later event inconsistent with the book
it is applied to. The overlay keeps the tape authoritative and still stops
us taking the same shares twice.

*Q. The cancel rule is conservative on QUOTE-driven levels. Is that a
bias?* Yes, a deliberate one, in the direction that understates passive
fills. A backtest that is wrong should be wrong against you.

*Q. Did these fixes change any reported result?* No committed number moved:
the golden sessions do not contain the patterns. Simulated fills of other
sessions can only shrink.

**Exercise.**

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_execution_rules.py \
  -k "crossed_display or dropped_by_the_book or as_the_book_saw_it or cancel_from_behind"
# 4 passed, 60 deselected
```

Read `test_queue_crossed_display_is_consumed_once_until_it_changes` and
predict the fill list after each event before reading the asserts.

---

## 23. Statistical power: what a null result is worth

The platform's headline is a null: of the flagship alphas on the bundled
data, none is promoted. Is that evidence that the validation chain is
rigorous, or that it cannot find anything?

A null result cannot answer that by itself. A test that never rejects and
a test that rejects exactly when it should produce the same output on data
with nothing in it. And for some hypotheses the bundled data has nothing
in it by construction: in the default generator the aggressor sign is a
fair coin and no instrument leads another (both planted strengths default
to zero; §2.3 notes the cross-venue case). Reporting "no effect found"
there is correct and uninformative — it does not distinguish a working
detector from a broken one.

What separates them is a **positive control**: put an effect of known size
into the data and see whether the chain finds it. That is the
planted-signal power study (`python -m iap.research power`,
`research/power/POWER_REPORT.md`).

### 23.1 What is planted

The generator gained an opt-in `planted` block. It is off by default, adds
no random draw when off, and the pinned dataset was regenerated and hashed
before and after: byte-identical. Two effects:

- **Informed order flow.** The generator precomputes the efficient price
  path, so it knows the future. With strength `s`, the aggressor of an
  execution is a buyer with probability
  `0.5 + 0.5 · s · tanh(g / scale)`, where `g` is a kernel-weighted
  efficient-price move over the coming seconds (20 one-second steps in the
  reference configuration). Trade sign leads price —
  the footprint of informed flow. Detector: EQ04.
- **Lead-lag.** Every other equity's efficient-price move at step `k`
  gains `beta` times the leader's move at step `k − lag`. Detector: EQ10.

A *level* scales both (0 is the null, 1 the reference effect of
`research/power/generator_planted.json`), and a *break* scenario reverses
both effects from the middle of the sample. Each cell is run on three
generator seeds through the real feature pipeline and `validate_alpha`.

### 23.2 What came back

From the committed report (rates are over the three seeds of a cell):

| effect | scenario | level | mean gate IC | mean t (within-bucket) | significant | verdict ITERATE or better | PROMOTE |
|---|---|---:|---:|---:|---:|---:|---:|
| order flow (EQ04) | stable | 0 | −0.0134 | −1.35 | 0.00 | 0.00 | 0.00 |
| order flow (EQ04) | stable | 0.5 | +0.0324 | +2.72 | 0.33 | 1.00 | 0.00 |
| order flow (EQ04) | stable | 1 | +0.0726 | +5.96 | 1.00 | 1.00 | 0.00 |
| order flow (EQ04) | stable | 2 | +0.1579 | +13.42 | 1.00 | 1.00 | 0.00 |
| lead-lag (EQ10) | stable | 0 | +0.0006 | +0.65 | 0.00 | 0.33 | 0.00 |
| lead-lag (EQ10) | stable | 1 | +0.0060 | +1.12 | 0.00 | 0.33 | 0.00 |
| lead-lag (EQ10) | stable | 2 | +0.0216 | +2.53 | 0.33 | 0.67 | 0.00 |
| both | break | 0.5 – 2 | within ±0.036 | all negative | 0.00 | 0.00 | 0.00 |

Read it row by row.

- **The chain has power, for one effect.** At the reference size the
  order-flow effect is flagged significant in three seeds of three; at half
  size, one of three.
- **For the other it has almost none.** The lead-lag effect at the
  reference size is flagged in zero of three seeds, and at twice the size
  in one of three by the statistic the gate reads. A real effect of that
  size would be reported as "not found".
- **The null row is not clean.** With nothing planted, one lead-lag seed
  of three still came out ITERATE. Its t was nowhere near significant; the
  ITERATE band is deliberately loose. With three seeds that is one event,
  not a rate — but it is a false "evidence" and the table shows it.
- **The break rows behave.** An effect that reverses mid-sample is
  flagged in no cell, and fold sign consistency falls to between 0.25 and
  0.50, against 0.83 to 1.00 in the stable cells at the reference size and
  above.
- **Nothing is promoted, at any size.** Not even the planted effect with
  a t of 13. Zero folds survive 1× costs in every cell, and no bootstrap
  interval for net P&L lies above zero.

That last row is the one that changes how to read the headline. PROMOTE
requires positive net P&L after costs under the research execution model,
and in this generator's cost structure even a large, real, stable, planted
effect does not clear it. So "0 PROMOTE on the bundled data" is not
evidence that the chain is a strict judge of alpha. It is at least partly
a statement about the generator's spreads relative to the size of any
signal in it, traded with a sign-following policy that pays the spread on
every flip. The significance half of the chain is informative. The
promotion half has not been shown to be reachable.

### 23.3 What the corrected statistics change

The report scores each run three ways: the within-bucket Newey–West t that
the gate reads; the **pooled-slope HAC t**, which tests the pooled IC
directly and keeps signal that lives between buckets; and the within-bucket
t against a **ledger-derived threshold** — the multiple-testing threshold
of the study's own 42 tests, t ≥ 3.24 rather than 3. In this grid they
agree almost everywhere. The one cell where they differ is lead-lag at
twice the reference size: the pooled t flags two seeds of three where the
within-bucket t flags one. That is weak evidence the pooled statistic has
more power for a between-bucket effect, and it is why both are reported
and neither replaced the pinned one (§24.4).

### 23.4 What this study is not

Three seeds per cell: a rate moves in steps of 0.33, and the report says
"this calibrates the chain, it is not a precise power curve". The planted
effects are the generator's own construction, detected by the two alphas
written for exactly those effects; power against an effect of a different
shape is unknown. And it is synthetic end to end. A power statement about
real markets needs real data (EPICS E25).

**Check yourself.**

*Q. The order-flow detector reports a mean IC of −0.013 with t −1.35 on the
null. Is that a problem?* It is a reminder that "nothing planted" is not
"nothing there": the generator's microstructure produces small correlations
of its own (§2.3). The row is not significant and produced no evidence; a
level-0 row that *was* significant would be the problem.

*Q. Why is a study with three seeds worth committing?* Because the
alternative was no positive control at all, and because its limits are
printed in the report. A wider grid is compute, not design.

*Q. What single result here most limits the claims the rest of the
repository can make?* PROMOTE is zero in every cell. The chain has been
shown to detect; it has not been shown to promote.

**Exercise.** Run the tiny grid of COOKBOOK recipe 27 and compare its
`stable` level-1 rows with the three-seed report: which cells agree, and
which could not possibly agree with one seed? Then run the tests that pin
the planted generator:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_planted_signals.py
# 29 passed (about a minute)
```

---

## 24. Multiple testing, and three ways to game a gate

§6.6 explains the ledger: every look is counted, and the count sets a
selection yardstick. A count only protects you if it cannot be dodged. The
v1.3.0 review asked how a researcher — or an automated agent with a goal —
could get a result past the promotion gate without the result deserving
it. It found three ways. None needed bad faith; each was a parameter the
tooling offered.

### 24.1 Cheaper costs

`cost_multiplier` is a legitimate research knob: stress an alpha at 2× and
see whether it survives. It also accepts 0.5. Every alpha in the registry
is held at CANDIDATE by the same gate, `net_pnl_after_costs`, so halving
the costs is the shortest path to a pass. The same goes for running with
fewer folds, a shorter embargo, a faster latency assumption, a longer
permitted decision age, or without flattening at the session end.

### 24.2 A chosen holdout

The runner derives its three periods from the dataset's session calendar:
train on the earlier sessions, purge and embargo, test on the last. The
spec also accepts explicit periods, which is necessary for reproducing a
run. It means a caller can choose the holdout — try several and keep the
one that looks best. The ledger counts each as a look, but the surviving
result is the maximum of several, and reads like a single test.

### 24.3 Free looks

`--dry-run` computed every statistic, printed it, and wrote nothing —
including nothing to the ledger. An unlimited supply of uncounted looks:
run dry until the numbers are good, then run once for the record. The
denominator the whole multiple-testing argument rests on was optional.

A fourth, accidental variant: two writers. The ledger was read, updated in
memory and written back. Two processes doing that at once both succeed and
one update is lost. Lost looks are uncounted looks.

### 24.4 How they are closed

The principle is: **anything can be run; not everything is evidence.**
Forbidding `cost_multiplier=0.5` would remove a useful experiment. Instead
the result carries a mark.

- **Gate eligibility.** A result is promotion evidence only if its
  configuration is at least as conservative as the pinned protocol
  (`cost_multiplier >= 1.0`, `latency_ns >= 1 s`, `embargo_ns >= 60 s`,
  `n_folds >= 4`, `max_decision_age_ns <= 60 s`, session flattening on)
  *and* its periods are the ones derived from the dataset. The runner
  writes the verdict and the reasons to `eligibility.json` beside the
  result. In the lifecycle, evidence flagged not eligible makes every gate
  that reads the research block fail with a null value — exactly as if the
  number were missing.
- **A dry run is a look.** `--dry-run` still writes no experiment
  directory, and now debits the ledger. A rerun of an identical
  configuration adds nothing, as before.
- **The ledger is safe for parallel writers.** A lock file created with
  `O_CREAT | O_EXCL`, the ledger re-read under the lock, this writer's
  pending records replayed onto the fresh state, a temporary file and an
  atomic replace. Two processes that both record now both land. A lock
  left by a killed process is never broken automatically — a human removes
  it, because the tool cannot know whether the other writer is dead.
- **The threshold can follow the count.** The ledger has always computed
  the Bonferroni threshold its count implies, and no gate read it. With
  `tstat_threshold="ledger"` the PROMOTE gate becomes the larger of 3.0
  and that threshold — on today's ledger about 4.07 instead of 3. It is
  opt-in, like every corrected statistic in this release, so that its
  effect can be measured before it is adopted and no committed verdict
  moves silently. On the bundled data it would change no verdict: nothing
  is promoted under either threshold.

Why opt-in rather than simply fixed? Because the committed reports, the
ledger and the goldens were produced under the pinned definitions, and a
platform whose numbers change when the code is upgraded has lost the
ability to say what changed. The pinned default stays reproducible; the
corrected method sits beside it with a name; docs/RESEARCH_VALIDITY.md
lists every pair.

What is *not* closed: the determination is made by the runner, in Python,
on the researcher's own machine. The reader re-derives the configuration
bounds from the spec, so a hand-edited `eligibility.json` cannot make a
half-cost run eligible
(`test_sidecar_cannot_overrule_the_configuration_bounds`). The period check
needs the dataset, so it is taken from the sidecar as written — and a
result document produced without the runner at all is not stopped by
anything in this repository. The stronger controls — pre-registration
before the data is read, a reserve session on a seed the researcher never
sees, an authenticated human approval for each promotion — are designed
and are backlog (EPICS E30).

**Check yourself.**

*Q. An experiment ran with `cost_multiplier=2.0` and `n_folds=6`. Is it
gate-eligible?* Yes: both are stricter than the protocol. Eligibility is a
one-sided bound, not an equality.

*Q. A colleague reproduces a published run by passing its three periods
explicitly. They equal the derived periods. Eligible?* Yes. The rule
compares the periods with the ones derived from the dataset, not how they
were supplied.

*Q. Why does the ledger refuse to break a stale lock automatically?* The
safe failure of a lock is to block. Breaking it on a timeout turns "one
writer died" into "two writers at once", which is the bug the lock exists
to prevent.

**Exercise.** COOKBOOK recipe 35 builds an eligible and a non-eligible
spec and prints the reasons; recipe 29 runs an experiment against the
ledger-derived threshold on a scratch ledger. Then:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q \
  tests/test_research_store_safety.py tests/test_research_validity.py
# 69 passed
```

`test_research_store_safety.py` includes the two-process ledger test; read
it and say what would be lost if the re-read under the lock were removed.

---

## 25. Crash consistency and commit points

A process can be killed between any two instructions. "What is on disk if
it dies *here*?" has to have a good answer at every line that writes
state. The Java paper platform's checkpoint did not, in three ways, and
the research store had the same disease in a milder form.

### 25.1 Two atomic writes are not one atomic checkpoint

A checkpoint is two files: `risk_snapshot.json` (positions, lots, open
orders, kill latches) and `session_state.json` (the event cursor, P&L, the
order-id sequence). Each was written correctly — temporary file, `fsync`,
atomic rename. A crash can still fall *between* the two renames, and then
the directory holds the risk state of one checkpoint beside the cursor of
another. Resume from that and the engine replays events its risk state has
already seen, or skips events it has not.

Atomicity of the parts does not give atomicity of the whole. The fix is
the oldest one in storage: a **single commit point**.

1. Write the new snapshot, fully and fsynced, under a side name
   (`risk_snapshot.json.next`). The previous checkpoint is untouched.
2. Replace `session_state.json` atomically. It now carries the sha256 of
   exactly those snapshot bytes. *This rename is the commit.*
3. Rename the snapshot into place.

Crash before step 2: the old state and the old snapshot, a consistent
pair. Crash between 2 and 3: the new state, and the new snapshot under its
`.next` name — resume sees that the hash of `risk_snapshot.json` does not
match, finds that the `.next` file's does, and rolls forward. Any other
mismatch — an edited, truncated or foreign snapshot — is refused with both
hashes in the message. There is no instant at which the directory holds a
cursor and a risk state from different moments.

### 25.2 State that was restored, and state that was not

`--resume` restored the risk engine. It did not restore the strategy's
*account*, which started flat. A flat account next to a risk engine
holding 500 shares makes the strategy buy its 500 shares again. And the
snapshot's open orders were restored faithfully — children of an execution
simulator that no longer existed, which would therefore never fill, never
cancel and never report. Each one stayed in every position projection for
the rest of the session.

Both come from one mistake: restoring one component and leaving the
components it must agree with at their defaults. Now the account is seeded
from the restored positions, and the orphaned open orders are released
through the engine's normal order-done path, with a counter saying how
many.

### 25.3 The helpful shutdown hook

On SIGTERM a JVM shutdown hook took a final checkpoint. Considerate — and
a data race. The trading loop holds no lock across an event, so the hook's
thread serialised the risk engine's maps while the trading thread was
still mutating them.

The rule: **one writer.** The hook now does nothing but raise a volatile
flag and wait, bounded, ten seconds. The trading thread reads the flag at
every event boundary — and between pacing slices when a realtime feed is
quiet — writes the checkpoint itself, and ends the session in a new state,
`STOPPED`. If the wait expires the last periodic checkpoint stands, and at
most one interval is replayed.

The admin kill switch had the mirror-image bug: commands were queued for
the trading thread, and a kill sent while the feed was quiet waited for
the next market event, timed out, and was *removed from the queue*. An
operator pressed the big red button and the platform kept trading. A kill now latches
the instant it is accepted — the order path sends nothing from that
moment — is never withdrawn, and answers `202` if the trading thread has
not recorded it within the wait.

### 25.4 The same pattern in the research store

Different language, same shape. An experiment was two files in a
directory; a reader could see one without the other. Now the directory is
staged under `.staging-<id>-<pid>` and moved into place in one rename, and
a listing skips and reports a directory it cannot read instead of failing.
A model run id was "the next free number"; two processes could pick the
same one. Now creating the directory *is* the claim: `mkdir` without
`exist_ok` either succeeds or tells you someone else got there first.

**Check yourself.**

*Q. Why hash the snapshot instead of putting a sequence number in both
files?* A sequence number says which checkpoint a file claims to belong
to. A content hash says the bytes are the ones that were committed. It
also catches an edited or truncated snapshot, which a counter cannot.

*Q. The audit log is appended and flushed before the state file is
renamed. After a hard kill it can be longer than the state file says. Is
that corruption?* No — it is the expected torn case, and the direction is
deliberate. The extra lines are decisions that happened after the last
commit. Resume refuses the mismatch rather than guessing, and the runbook
says what to do; the opposite order would lose decisions silently.

*Q. A `risk_snapshot.json.next` file is sitting in the state directory.
Delete it?* No. It may be the only copy of the snapshot the committed
state refers to.

**Exercise.** The Java tests run in CI (`PlatformSafetyTest`:
`checkpointHasOneCommitPointVerifiedOnResume`,
`stopRequestCheckpointsOnTheTradingThreadAndResumes`,
`killLatchesImmediatelyAndIsNeverDropped`); with a JDK,
`cd java && bash build.sh && bash test.sh`. The Python half runs anywhere:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_research_store_safety.py
```

Then write down, for `commitCheckpoint`'s three steps, the directory
contents after a crash following each one, and what `--resume` does with
it. `docs/DIAGRAMS.md` §13 is the answer key.

---

## 26. Supply-chain hygiene

The code in this repository is a small fraction of the code that runs when
its CI does. The rest arrives by name — an action, a package, a base image,
a compiler — and a name is a pointer someone else can move. Supply-chain
hygiene is replacing names with things that cannot move, and being exact
about which replacements have actually been made.

### 26.1 Tags move; hashes do not

`uses: actions/checkout@v4` runs whatever the tag `v4` points to when the
job starts. A tag is mutable: whoever controls that repository, or
compromises it, can repoint it. Every `uses:` in the three workflows is
therefore pinned to a 40-character commit SHA, with the human-readable
version in a trailing comment. A SHA names content. Dependabot is
configured to propose bumps and rewrites the SHA and the comment together,
so pinning does not mean never updating — it means updating by a reviewed
change.

The same idea applies one level down:

- **Base images** are pinned by digest (`FROM debian:bookworm-slim@sha256:…`).
- **The Rust toolchain** is pinned in `rust/rust-toolchain.toml`, and CI
  and the Rust Dockerfile name the same version; a harness check fails if
  they drift.
- **Tools downloaded in CI** (promtool, kubeconform) are verified against a
  recorded SHA-256 before they are installed.
- **Rust dependencies** are locked: every cargo invocation passes
  `--locked`, so a build fails rather than silently resolving something
  newer than `Cargo.lock`.

### 26.2 Version pins are weaker than byte pins — say so

Python is the honest exception. `python/requirements-ci.txt` lists the
exact version of every package CI installs, direct and transitive. Its own
header says what that is worth: *"No hashes: they were not generated, so
this pins versions, not bytes."* A version pin stops an unplanned upgrade;
it does not stop a re-uploaded artefact under the same version. Writing
the limitation into the file is better than letting a reader assume the
stronger property.

### 26.3 Least privilege, and provenance

Each workflow starts with `permissions: contents: read`, and a job that
needs more asks for exactly that (the release job that pushes images gets
`packages: write`; nothing else does). A compromised step in the test job
cannot push an image.

The release workflow builds the four images on a `v*` tag, refuses to run
unless the tagged commit has a green CI run, pushes to the registry,
attaches a build-provenance attestation to each image and writes a
manifest of image digests. Deployment manifests are then to be pinned to
those digests, and the provenance can be verified before deploying.

### 26.4 A control that is only a file

Here the honest part matters most. Several of these controls exist as
files and not yet as facts:

- The release workflow **has never run**. It can only be exercised by a
  tag, so its first run is its test.
- The deployment manifests reference the release **by tag**. They are to
  be pinned by digest after the first release run produces a manifest.
- Branch protection, required status checks, required reviews, Dependabot
  alerts and secret scanning are **repository settings that are not
  configured**. `docs/governance/REPO_SETTINGS.md` has the commands; until
  someone runs them, "pull requests only" is a convention one maintainer
  follows.
- Tag signing is a practice. Nothing verifies a signature.
- No image vulnerability scan is wired into a workflow.

A governance document that says "required" where the truth is "not yet
configured" is worse than no document, because it stops the reader from
checking. GOVERNANCE.md opens with a table of exactly this: each control,
whether it is enforced, and by what.

**Check yourself.**

*Q. `--locked` fails the build when `Cargo.lock` is out of date. Isn't
that a nuisance?* It is the feature. The alternative is a build that
quietly uses a dependency version nobody reviewed.

*Q. An action is pinned by SHA. What can still go wrong?* The pinned
commit can itself be malicious or vulnerable — pinning fixes *which* code
runs, not whether it is good. That is what review of the Dependabot bump,
and CodeQL over the workflows, are for.

*Q. Why does the import-policy test belong in a chapter about supply
chain?* It is the same idea turned inward: the trading path is not allowed
to acquire a dependency on a network or LLM client by accident. A test
that parses the imports makes that a property of the code, not of
reviewers' attention (and it, too, has a stated gap: it scans the Python
packages, not `iap.replay`, and not the other three languages).

**Exercise.**

```bash
python3 tests/harness/check_deployment.py --verbose | grep -E "image_pinning|workflow_supply_chain|rust_toolchain_pinned"
#   [ok  ] image_pinning
#   [ok  ] workflow_supply_chain: 3 workflows
#   [ok  ] rust_toolchain_pinned: 1.98.1
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_import_policy.py
# 25 passed
```

Then open `.github/workflows/ci.yml`, pick one `uses:` line, and find the
release that SHA belongs to. That lookup is what a reviewer of a
Dependabot bump is being asked to do.

---

## 27. Twelve pitfalls this platform is built to avoid

1. **Lookahead in labels or benchmarks.** One pinned at-or-before rule for
   labels, TCA and features; shift-by-one tests enforce it mechanically.
2. **Leakage via undeclared features.** Alphas declare their inputs; the
   harness masks everything else and demands identical output.
3. **Random splits on overlapping labels.** Walk-forward only, purge at the
   label horizon, 60 s embargo (§6.2).
4. **Uncounted multiple testing.** A ledger de-duplicated by (alpha, kind,
   config) — 70 distinct configurations, 1068 looks — with a printed
   expected-max-|t| yardstick of 3.735; FX08's t = 2.02 is called what it is.
   Round 3 also fixed the denominator itself: it used to grow every time a
   script was rerun, which made the correction a function of how busy the
   researcher had been.
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
    queue-position and latency model shared by golden tests in three
    languages (C++, Java, Python); passive vs aggressive economics measured, not assumed.
12. **Benchmark numbers without methodology.** Every published figure
    carries hardware, compiler, workload and boundary caveats (2-CPU
    container, mean-only, no pinning) — per spec §22's "never present an
    isolated latency number."

---

## 28. Twelve interview questions (with answers from this repo)

**Q1. Why can order-flow imbalance be a real predictor and still lose
money?**
Because significance and tradability are different tests. EQ03: OOS IC
0.030, t 10.6 on uncrossed rows, every fold positive — and −70,651 net at 1×
costs, since 671 signal flips per active hour pay the spread continuously.
IC measures correlation; P&L measures correlation × horizon × turnover −
costs.

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
profitable net of costs. With a profitable-trade base rate of 0.037,
calibrated probabilities rarely approach 0.5, so thresholds must be chosen
on a held-out calibration segment in probability space and evaluated
economically. On this dataset even the calibration-chosen τ = 0.300 declines
every test signal, and the gate-off alternative realized −0.20 net
bps/trade. That looks like a vindication and is not one: a gate that never
fires has shown neither skill nor the value of abstaining, and the report
flags it `gate_degenerate`. A conviction model must be allowed to say
"don't trade" — and a gate that said nothing else must be reported as
untested.

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
your level deplete it (overflow fills you), a CANCEL decrements it only when
the cancelled order is known to be ahead of you (a real order id that did not
join the level after you; by the size the book removed), trade-throughs and crossing
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
flow alphas shed ~15-21% of IC after one event of staleness and ~66-82% by
five (EQ01's microprice signal flips sign outright), the slow equity alphas
do not notice at all (EQ09 drifts up, EQ11 keeps 95%), and the FX regime
family holds ~78-83% at one event but loses most of its IC by five
(~7-41%) — and the compute path is six orders of magnitude faster than the
feed. So the marginal microsecond of
compute is worthless, but being events late is existential for flow alphas.
The budget goes wherever your reaction-to-next-event chain is actually
bottlenecked, weighted per alpha family.

**Q11. Your paper-trading loop reports a realized IC ten times the research
IC. What do you do before you celebrate?**
Pin the definition, then attack the number. Here the MVP's EQ01 read 0.283
against a research 0.027 (§20.3). First make the realized IC *be* the
research label (`iap.labels.compute_labels` on the same book-refresh series,
asserted to 1e-12 against an independent rebuild); then prove the engine is
single-pass (truncate the stream, reproduce every earlier signal bit for
bit) and that the research code path on the same captured stream gives the
same number; then read the shift-by-one test with its known blind spot (at
a cadence equal to the horizon it cannot discriminate); then look at the
data — a synthetic generator whose venues lean towards a shared efficient
price makes microprice and OFI look clairvoyant at 1 s; and finally check
the cost-adjusted IC (0.017) and the P&L (−22.65 USD): still no money. A
number that survives all of that is a property of the data, stated as such,
and the lifecycle's `paper_ic_tracking` gate still fails the alpha for
diverging from research — correctly.

**Q12. How do you make "why did we trade?" answerable a week later, on
another machine, in another language?**
One typed record per decision with every stage's output and the four
version hashes in force (§18), serialised as canonical JSON — sorted keys,
fixed separators, ASCII, Python float repr, no NaN — so four languages
produce the same bytes; an id derived from
`session|instrument|event_ts|sequence`, never from a clock; a stream digest
(sha256 over each line plus newline) that a replay from the captured input
must reproduce; a store that indexes the lines but never replaces them; and
a runbook whose first step is "replay, and refuse to proceed if the
configuration in force differs". The C++ port writes its traces *after* the
replay because a 5.6 KB record costs 31.7 µs to serialise; the Java loop
fsyncs them with the risk audit at every checkpoint. The honest part is what
a trace does not contain: a stage that did not run is empty, and a version
the path does not have is 64 zeros, not a made-up hash.

---

## 29. Further reading

Inside this repository, in suggested order:

1. `docs/SPECIFICATION.md` — the governing spec; §32 is one paragraph and
   worth memorizing.
2. `PLATFORM_CONVENTIONS.md` + `schemas/FORMAT.md` — how contracts get pinned.
3. `API_CORE.md` → `API_FEATURES.md` → `API_ALPHA.md` →
   `API_PORTFOLIO_TCA.md` → `API_ADAPTIVE.md` → `API_CONTRACTS.md` →
   `API_TRADING.md` — the seven port contracts, increasingly rich.
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
0. `docs/HOW_IT_WORKS.md` — the whole platform top-down in one sitting,
   before any of the below.
9. `docs/MVP.md` — the loop end to end, the wiring review, the IC audit and
   the success-criteria table; then `docs/LIFECYCLE.md`,
   `docs/DECISION_TRACE.md` and `docs/DATA_MODEL.md` for the three
   subsystems it exercises, and `docs/ROADMAP.md` for what is done with
   evidence and what is backlog.
10. `docs/RESEARCH_VALIDITY.md` and `research/power/POWER_REPORT.md` — the
    opt-in corrected methods and the planted-signal study behind §23–§24;
    `CHANGELOG.md` for what v1.3.0 fixed and what it leaves open.

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
