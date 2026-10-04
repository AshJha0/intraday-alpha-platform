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

A note on which numbers these are. v1.3.0 added eleven corrected research
methods as opt-ins — a cost-aware backtest, a significance test of the
gated IC itself, a threshold that follows the count of looks, and others —
and v1.4.0 kept them opt-in. Since v1.5.0 they are the defaults, every
research artefact was regenerated under them on the unchanged dataset, and
the numbers below are the v1.5.0 numbers. Each old rule keeps a legacy name
(the bundle `legacy_v1` of `iap.validation.methods` reproduces the v1.4.0
report), and where a v1.4.0 figure is still the clearer illustration it is
quoted as such, with its date.

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
signal of alpha **EQ01** and **FX01**. On this repo's data the same formula
gives two weak results that are weak in different ways. On the equity MBO
book it is a small positive predictor — uncrossed OOS IC 0.0105, gate
t = 1.59, three of four folds positive, hypothesis-confirmed, verdict
ITERATE; the t is far below the PROMOTE threshold (4.365, §6.5), and its
forecast is so small that the backtest never finds a row worth trading.
(On the v1.3.0 dataset, whose equity flow was
compressed into the first 40 % of the session, the same alpha read IC
0.0273, t 4.77 — see §2.3.) On the synthetic FX quote book the answer
depends on which rows are counted: FX01's pooled IC is −0.0153, its IC on
rows where the consolidated book is not crossed is +0.0183 (t 2.26), and no
single fold is positive — ITERATE by the letter of the gate, with no stable
signal behind it (§6.5). Same math, different market structure, different
failure: a good first lesson in why microstructure context matters.

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
statistically predictive here (uncrossed OOS IC 0.0251/0.0189 with a
gate t of 7.17/5.16, every walk-forward fold positive — the gates
read the UNCROSSED column, and on equities the consolidated book is
crossed on only ~0.3 % of rows, so pooled and uncrossed agree to four
decimals) and *still not tradable*. The two backtest policies say so in
two ways. Under the default cost-aware policy neither alpha makes a single
trade: the fitted expected return never exceeds the round-trip spread and
fee. Under the legacy sign policy, which trades every flip — 378/423
signal flips per hour — the strategies paid the spread so often that costs
exceeded gross alpha by roughly two orders of magnitude (EQ02 day 2 in the
v1.4.0 report: gross +2,265 against 368,947 of costs).

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
  audit: 309,598 raw events in, 308,975 out, with 262 gaps, 450 duplicates,
  128 out-of-order, 173 invalid, 89 clamped timestamps — every one counted.

### 2.3 Its limits — read this before believing any result

**Equity flow now runs to the close — and the bug it replaces is worth
knowing.** Up to v1.3.0 each equity stream had a hard *budget* of
`slots_per_stream` flow slots per session, and the loop that spent it drew
inter-arrival times at `slots / duration × 1.30 × (1 + excitation)`. The
1.30 was a rate margin, so the budget would have run out 77 % of the way
through even with no clustering; the self-exciting multiplier
`(1 + excitation)` then shortened every inter-arrival time further, and
nothing in the calibration accounted for it. Measured on the v1.3.0
dataset, the last continuous event of an equity stream fell 37.6–43.2 %
into the 6.5-hour session (mean 40.5 %), followed by nothing until the
close auction prints: an equity "session" was its morning, and every
result up to v1.3.0 was computed on that.

v1.4.0 calibrates the rate instead of budgeting the slots
(`equities.flow.calibration = "session"`, the default; generator config
`x-version` 2). The base rate is `slots_per_stream ×
excitation_time_factor / duration`, where `excitation_time_factor` is
E[1 / (1 + excitation)] over the slot chain — a pinned SplitMix64 estimate
that depends only on the flow parameters, 0.522 for the bundled
configuration (`test_excitation_time_factor_is_pinned_and_explains_the_legacy_stop`).
There is no budget: `slots_per_stream` is now the *expected* number of
slots, spread over the whole session, and the last continuous event of
every equity stream falls in the final 0.2 % of the session. The way back
is `equities.flow.calibration = "legacy_budget"`, which reproduces the
v1.3.0 dataset (`data_version 203c8f54…`) byte for byte;
`tests/replay/test_generator_determinism.py` pins the raw-file hashes of
both datasets. The old `equities.fill_session` key belongs to the legacy
rule only and is an error with the default. FX is untouched: its files are
byte-identical to v1.3.0, and its flow still ends 91.7–99.8 % of the way
through its session (a budget with a 1.05 margin and no excitation).

What the fix does downstream is not all good news, and none of it is
hidden:

- **About the same number of events, about 2.5 times sparser.** The fix
  moved events, it did not add them (308,975 normalized events against
  310,159). The same flow that used to occupy 2 h 38 min now occupies
  6.5 h: equity feature rows are a mean 3.1–3.3 s apart (median 2.0–2.1 s),
  where inside the old active window they were about 1.3 s apart.
- **Labels go stale.** A label is valid only if the forward mid is fresher
  than `max(5 s, 2 × median quote gap)` (§5). On equities the median gap is
  about 2 s, so the 5 s floor binds, and on the sparser flow it removes a
  material share of the labels: per instrument, 99.4–99.7 % of 5 s labels
  are valid, but only 78.7–80.5 % at 10 s, 75.3–77.2 % at 1 minute and
  44.7–63.4 % at 15 minutes. Roughly a fifth to a quarter of equity labels
  at horizons of 10 s to 1 minute are invalid as `forward_stale`.
- **The statistics got weaker.** The walk-forward folds are cut by row
  mass and now span the whole session rather than partitioning the
  morning; every equity alpha's IC and t moved, mostly down (EQ03: IC
  0.0298 → 0.0190, t 10.57 → 5.78), EQ11 dropped from ITERATE to REJECT,
  and the count went from 0 PROMOTE / 11 ITERATE / 13 REJECT to
  0 / 10 / 14 (the v1.4.0 report, under the rules of the time). These were
  the numbers of the corrected dataset; the
  earlier ones described a market that closed before lunch. v1.5.0 kept
  the dataset and changed the default methods, and the same data now reads
  0 / 11 / 13 with different members (EQ03: gate IC 0.0189, gate t 5.16;
  §6.5 has the scoreboard).

The generator's mid is **strongly mean-reverting** around its regime
process. Consequences you will see all over the research reports:

1. **Reversion alphas look great, momentum-family hypotheses often fail.**
   Several alphas ship with `hypothesis_confirmed = false` — the fitted sign
   contradicts the stated economic rationale — and are therefore barred from
   PROMOTE no matter how large the IC. The clearest case today is FX09:
   uncrossed IC −0.0471, gate t −5.75, fold sign consistency 0.00 — a |t|
   beyond even the ledger threshold of 4.365, pointing the wrong way:
   **REJECT**
   (it used to be quoted at "IC 0.113, t 14.3, capped at ITERATE" before
   the gates read the crossed-book-conditioned IC — see §6.5). EQ08 is the
   equity case (IC −0.0481, gate t −4.57). EQ09 was
   the equity example on the v1.3.0 dataset (NW t −5.65); on the v1.4.0
   dataset its gate t is −1.32 and there is nothing left to explain.
2. **The consolidated multi-venue book can still cross occasionally**
   (negative spread). Under the shared-efficient-price design the merged
   *equity* book is crossed at only 0.26% of ML decision rows — but the FX
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

On the bundled dataset the pipeline emits 213,021 vectors (100 ms cadence,
308,975 events, about a minute in the Python reference; the C++ port does the same
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
time. Four details carry most of the integrity:

1. **At-or-before, never after**: the anchor uses only events with
   `ts ≤ t`. The same rule is reused verbatim by TCA benchmarks (§11) —
   one lookahead convention for the whole platform.
2. **Validity requires observation**: a label is valid only if the stream
   was actually observed through `t + h`; nothing is extrapolated past the
   session end. It also requires a *fresh* forward sample: a forward mid
   older than `max(5 s, 2 × median quote gap)` makes the label invalid
   (`forward_stale`, API_FEATURES §6). On the bundled equities the median
   gap is about 2 s, so the bound is the 5 s floor, and since v1.4.0
   spread the flow over the whole session (§2.3) it bites: about 80 % of
   10 s labels and 75–77 % of 1-minute labels are valid per instrument,
   against more than 99 % at 5 s. On FX the bound is 33–37 s and 97 % of
   labels up to 30 s are valid.
3. **Both raw and cost-adjusted labels exist** (spec §13). The cost label
   embeds the round-trip spread — which is exactly what makes it dangerous
   as an ML target, as §7 shows.
4. **A halt does not make a row disappear.** A label whose window contains
   a blackout (a halt, an auction call, a stale-venue gap) is invalid, and
   up to v1.4.0 every IC simply dropped the row. But a position entered
   before the halt is held through it, and dropping the row removes
   exactly the outcomes a forecast is least likely to get right. So each
   row that is invalid for BLACKOUT alone also carries its realised return
   to the first tradable price after the blackout (`label_reopen_<h>`), and
   since v1.5.0 the default row policy scores it there
   (`ic_rows="blackout_reopen"`; `"valid_only"` is the legacy rule). For 23
   of the 24 alphas this moves the gate IC by less than 0.003. For EQ11,
   at 15 minutes, 23,413 such rows move it from 0.0261 to 0.0038 — a
   selection effect, now visible. A row whose forward price is merely
   stale is still dropped under both rules.

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
Fold boundaries are quantiles of the row index, not of the wall clock: the
equity session is 6.5 h of each 24 h day, so equal wall segments would
leave folds empty or badly unequal. A fold with fewer than 32 usable pairs
is degenerate and counts as a failed fold.

### 6.3 Leakage tests are automatic, not aspirational

Two leakage tests run for every alpha, every time: the masked-column guard
(§6.1) and the **shift-by-one test** — shifting features one event forward
relative to labels must destroy the IC. An alpha that survives its own
shifted version is reading the future somewhere; verdict REJECT, always,
regardless of any other statistic.

Both test the *model* on frames it is handed, and neither can see
look-ahead that is already inside a feature — a centred window, a
full-sample normalisation. Two further probes close that. The truncation
probe re-scores on a truncated frame and requires the score at the cut to
be bit-identical. The **recompute probe** truncates the *raw events*,
rebuilds the features from the prefix and requires the last row to equal
the same row of the full run. It was an opt-in in v1.3.0 and v1.4.0, when
no report ran it; since v1.5.0 it is in the standard suite whenever the
raw events are available (three anchors on the first 12,000 events of the
first normalized file per asset class), and all 24 alphas pass. A report
produced without events says `recompute_ok: null` rather than `true`, and
the runner marks such a result not gate-eligible (COOKBOOK recipe 30).

### 6.4 What gets measured

Per alpha: OOS IC (Pearson, pooled over folds), Rank IC, a
serial-correlation-robust t-statistic of that IC, hit rate, fold sign
consistency, **decay curve** across all 11 horizons, turnover (signal
flips/hour), capacity, cost stress at ×{0.5, 1, 2} modeled costs, latency
stress in event time (100 ms to 5 s) and at +{0, 1, 5} rows of staleness,
and a high/low-vol regime split. Since v1.5.0 every validation also
reports the cost survival of *every* fold and a stationary-bootstrap
interval for the pooled net P&L (report-only: no gate reads them), and
beside the pooled IC a per-instrument mean IC and a volatility-scaled IC,
so that one volatile instrument dominating the pool is visible.

Four of those measurements changed definition in v1.5.0, and each change
replaced a statistic that answered a slightly different question from the
one the gate was asking:

| measurement | default since v1.5.0 | legacy rule (to v1.4.0) | what was wrong with the old one |
|---|---|---|---|
| the t the gate reads | HAC t of the **pooled slope** (`significance="pooled_slope"`) | Newey–West t of within-bucket ICs (`"within_bucket"`) | it tested within-bucket correlation while the gate IC beside it was pooled: signal between buckets was in the IC and not in its t |
| the rows an IC scores | valid labels, plus BLACKOUT-only rows at their reopen return (`ic_rows="blackout_reopen"`) | valid labels only (`"valid_only"`) | it dropped the rows a held position would have lived through (§5) |
| net P&L | cost-aware positions, L1-capped fills, only the scored rows, square-root impact | sign of the signal on every row, any size at the touch, linear impact (`BacktestConfig.legacy()`, `CostModel.with_linear_impact()`) | the loss measured a policy that trades every flip, not the alpha |
| capacity | edge breakeven: the size at which the realised edge equals its cost (`capacity="breakeven"`) | participation cap × volume × price (`"participation"`) | a volume proxy says nothing about whether there is an edge to scale |

### 6.5 The gates, and the honest scoreboard

Pinned promotion gates (spec §20):

- **PROMOTE**: leakage pass ∧ gate IC ≥ 0.01 ∧ gate t ≥ the significance
  threshold ∧ fold consistency ≥ 0.7 ∧ at least 3 non-degenerate folds ∧
  hypothesis confirmed ∧ **net P&L > 0 at 1× costs**.
- **ITERATE**: leakage pass ∧ IC ≥ 0.005 ∧ t ≥ 1.5.
- **REJECT**: otherwise (always, on leakage failure).

The significance threshold is `max(3.0, the Bonferroni t at the run's look
count)` (`tstat_threshold="ledger"`, the default since v1.5.0; §6.6 and
§24.4). For the committed report that is **4.365**. Up to v1.4.0 it was a
fixed 3.0 (`"fixed"`, the legacy rule).

Result on the bundled data: **0 PROMOTE / 11 ITERATE / 13 REJECT**. The
cost gate fails for all 24, in two different ways: **18 alphas make no
trade at all** on the last fold — their forecast never exceeds the
round-trip spread and fee, so their net P&L is exactly 0, and 0 does not
pass `net P&L > 0` — and **6 trade and lose** (EQ11 −502, FX05 −64, FX08
−328, FX09 −255, FX10 −22, FX11 −733 USD). None ends above zero. The
ITERATE set is EQ01, EQ02, EQ03, EQ05, EQ06, EQ12, FX01, FX04, FX08, FX10
and FX11.

The history of that line is worth keeping straight, because it moved twice
for two unrelated reasons:

- v1.3.0 dataset, old rules: 0 / 11 / 13.
- v1.4.0 dataset, old rules: 0 / 10 / 14. Regenerating the data with
  equity flow to the close moved EQ11 from ITERATE to REJECT (its
  uncrossed t fell from 3.04 to 1.40) and left every FX row unchanged,
  because the FX files are byte-identical. Under those rules all 24 alphas
  *lost* money at 1× costs, up to −300,873 (EQ05), and six cleared the
  fixed t of 3.0 (EQ02, EQ03, EQ06, EQ12, FX01, FX04).
- v1.4.0 dataset, v1.5.0 rules: 0 / 11 / 13. Three verdicts moved, all on
  the pooled-slope t: FX10 and FX11 became ITERATE (gate t 1.55 and 2.22
  against the ITERATE bar of 1.5; their within-bucket t was 0.38 and
  1.27), and FX03 became REJECT (1.13, from 1.95). Three alphas now clear
  the significance threshold (EQ02, EQ03, EQ12); EQ06 misses it at 4.36
  and FX04 at 4.24, and the threshold was not moved for them. The largest
  loss at 1× costs is 733 USD.

So "24 of 24 lose money" was a statement about the sign policy. What the
artefacts show now is narrower and, for most alphas, more direct: the
predicted move is smaller than the cost of trading it. That is not a
softer result — a strategy that cannot find a trade worth making has not
passed anything — but it is a different one, and it says nothing about
what a passive execution policy would earn, which the research backtester
does not model.

Since round 3 the gates read the **uncrossed** IC and its t —
the same IC restricted to rows whose consolidated book was not crossed by
a stale venue quote (`gate IC` in the master table). On the equity book the
two are the same number to three decimals (≈0.3 % of rows are crossed); on
FX, where 29–33 % of cross-sections are crossed, they are different alphas
entirely. Worked examples, straight from the master table:

- **EQ03 (multi-level OFI) — ITERATE, the platform's signature finding.**
  Gate IC 0.0189, gate t 5.16 — above the 4.365 threshold — all four folds
  non-degenerate and sign-consistent, leakage-clean… and **no trade** at
  1× costs: 423 signal flips an hour, none of them forecasting more than
  the spread. Under the legacy sign policy the same alpha traded every
  flip and lost 148,562 (the v1.4.0 report). Statistically real,
  economically dead on this data (paper 1). The statistic is weaker than
  the t of 10.57 the compressed v1.3.0 dataset gave, and still clear of
  the selection yardstick of §6.6.
- **EQ08 (VWAP/mid deviation) — REJECT, instructively.** Its gate IC is
  −0.0481 with a gate t of −4.57: significant beyond the ledger threshold,
  in the direction its stated rationale forbids (`hyp = no`). The legacy
  within-bucket t was −1.53 and hid that; the effect lives between
  buckets. It flips only 26 times an hour and makes no trade. A strong
  statistic with the wrong sign is a finding about the hypothesis, not an
  alpha.
- **FX09 (vol-regime reversion) — REJECT, and the clearest lesson in the
  report.** Round 2 called it "the best FX statistics in the study" at
  IC 0.113, t 14.3. Conditioning on book state dissolves most of that: its
  IC is **−0.2099 on crossed rows and −0.0471 on uncrossed ones**, i.e. the
  signal was largely measuring the mechanical reversion of a stale LP's
  quote, not a vol regime. With `hyp = no` on top, it is a REJECT twice
  over. A number that only exists on untradeable rows is not a number.
- **FX01 (microprice on the FX quote book) — ITERATE, but on 0/4 folds.**
  Pooled IC −0.015 flips to +0.018 uncrossed (gate t 2.26), which clears
  the lenient ITERATE gate, yet **no individual fold** is positive
  (`folds+ = 0.00`) — so it can never reach PROMOTE. Compare EQ01, the same
  formula on the MBO book: gate IC 0.0105, gate t 1.59, three of four
  folds positive, and pooled and uncrossed agree because the equity book
  is almost never crossed. Neither is strong; they fail differently, and
  market structure decides how (paper 2).
- **EQ11 (15-minute horizon) — REJECT, and the case for the reopen rows.**
  Its valid-only IC is 0.0261; scored on the rows a 15-minute position
  would actually have held through, 0.0038 (gate t 0.20). It is one of the
  six alphas that trade under the cost-aware policy — 31 trades on the
  last fold, −502 USD — and its pooled net over all four folds is −3,174
  with a bootstrap interval of −5,142 to −1,497.

### 6.6 Multiple testing: counting your looks

Every walk-forward evaluation, decay horizon, stress variant, and backtest is
recorded in a ledger (`research/experiments.json`). The round-3 audit found
the count was measuring the wrong thing: it grew every time somebody *reran*
a script, and it counted each of the adaptive study's 211 monitoring
evaluations as a separate "experiment", so the Bonferroni denominator was a
function of how often the same code had been executed. An experiment is now
identified by **(alpha, kind, canonical config, dataset)** and
de-duplicated on that key — rerunning `run_all.py` on the same data changes
nothing, and one adaptive deployment is one experiment.

The fourth element of that key is new in v1.4.0 (ledger `x-version` 2), and
the reason is the dataset regeneration itself. Running the same 24
pipelines on a new dataset is a new set of looks at a new sample; a ledger
keyed without the dataset would have de-duplicated them away, and a ledger
that was simply reset would have forgotten the looks already taken. So the
looks of the v1.3.0 dataset are kept and the new ones are added. v1.5.0
did the same thing along a different axis: the method bundle is part of
the canonical config, so the same 24 pipelines run under the new default
rules are new looks at the same sample, added beside the v1.4.0 ones
(ledger `x-version` 3). The current ledger holds **208 distinct
configurations / 4396 looks**:

- on the v1.3.0 dataset (`203c8f54…`), 70 entries carrying 1,068 of the
  looks: a one-time design scan (216: 24 alphas × 9 horizons), 24
  promotion pipelines at 28 looks each (672), 40 adaptive deployments (10
  alphas × 4 refit policies) and five `ExperimentRunner` runs at 28 each
  (140; §6.8);
- on the v1.4.0 dataset (`116b7787…`), 138 entries carrying the other
  3,328. Under the legacy rules, as recorded at v1.4.0 (852): the 24
  pipelines at 28 looks (672), five runner experiments (140) and 40
  adaptive deployments (40). Under the default rules, added by v1.5.0
  (2,476): the 24 pipelines at **84** looks each (2,016), five runner
  experiments at 84 (420) and the 40 adaptive deployments again (40). The
  design scan was not repeated.

A validation costs 84 looks under the default methods where it cost 28
under the legacy ones, because the default validation computes more — 83
evaluations at four folds (itemised in
`iap.validation.validate.looks_per_validation`) plus the day-2 backtest —
and every one of them is a look.

That translates into a selection yardstick: Bonferroni per-test threshold
|t| ≥ **4.389**, and an expected **max |t| ≈ 4.096 under the global null**
(4.206 and 3.888 at the v1.4.0 count of 1920, 4.071 and 3.735 before the
dataset regeneration: looking again raised the bar each time). Up to
v1.4.0 this yardstick was printed beside a fixed gate of t ≥ 3.0 and read
by nothing. Since v1.5.0 the Bonferroni figure *is* the PROMOTE
significance gate (`tstat_threshold="ledger"`; `"fixed"` is the legacy
rule), and the count it is derived from is pinned so that a rerun
reproduces its verdict: the ledger total before the run plus the looks the
run itself adds, declared before the first alpha is evaluated and stored
on each entry as `gate_looks`. The committed report was judged at **3,936
looks** (the 1,920 already recorded plus its own 2,016), which gives the
threshold of **4.365** and a yardstick of 4.07; the runner and adaptive
runs that followed brought the ledger to 4,396. The threshold is not
retroactive — later looks tighten later runs and do not re-judge a
recorded one.

Meaning: FX08's gate t = 3.84 — or FX11's 2.22 — is *consistent with
pure selection* over this many trials, and the report says so in print:
17 of the 24 alphas sit below the yardstick. EQ12 (7.20), EQ02 (7.17) and
EQ03 (5.16) clear the threshold; EQ06 (4.36) and FX04 (4.24) are above the
yardstick and below the threshold, so they fail the significance gate
that, at a fixed 3.0, they used to pass; EQ08 (−4.57) and FX09 (−5.75)
are beyond it with the wrong sign. The lists are the same at 3,936 and at
4,396 looks. Most quant shops track this informally at best;
here it is a serialized, deterministic artifact — and the runner's entries
were deliberately *not* de-duplicated against the pipeline entries for the
same alpha and horizon: the denominator may only grow.

### 6.7 Cost reality

The cost model (`configs/execution/execution.json`) charges half-spread + fees
(mirroring venue configs) + impact per trade — square-root impact since
v1.5.0 (`impact_model="sqrt"`, 100 bps for one full ADV, a convention
pinned before any result was computed with it: impact is empirically
concave in size, and a linear rule understates the cost of small orders
relative to large ones); the legacy rule is linear (`"linear"`,
`CostModel.with_linear_impact()`). The day-2
out-of-sample backtest uses day-1-fitted parameters — the exact parameters
serialized for the production ports. Under the default cost-aware policy
18 of the 24 alphas make no trade on day 2 and the six that do all lose
(EQ06 −179, EQ11 −5,948, FX08 −940, FX09 −782, FX10 −252, FX11 −2,397
USD). EQ11's row shows what such a loss is made of: gross −313, costs
5,634. The equal-weight
ensembles of the non-REJECT alphas (equity, six alphas; FX, five) make no
trade either: averaging forecasts that are each smaller than the spread
does not produce one that is larger. Under the legacy sign policy the same
ensembles traded every flip and finished at −346,576 and −33,950 (the
v1.4.0 report, FX with four alphas). The report's Sharpe column is
labeled as an event-time research yardstick, not a production claim. Honesty
in the artifacts, not just the prose.

### 6.8 A typed experiment: spec in, result out

The walk-forward story now has a typed artefact. `python -m iap.research run
--alpha EQ03 --horizon 1s` builds an `ExperimentSpec` — alpha, horizon, the
dataset version (sha256 over the normalized IAP1 bytes), the feature version
(the registry hash), the model *definition* hash, the pinned protocol
configuration (`n_folds 4`, `embargo_ns 60e9`, `cost_multiplier 1.0`,
`latency_ns 1e9`, `max_decision_age_ns 60e9`, `flatten_at_session_end`,
and since v1.5.0 `methods`, the name of the method bundle: `v2` unless
`--methods legacy_v1` asks for the v1.4.0 rules), three periods and a seed — whose id is the first 16 hex of its own content
hash: the id *is* the request. It runs `validate_alpha` (the same purged,
embargoed walk-forward the report runs) plus a holdout backtest, and writes
an `ExperimentResult` (IC, rank IC, t, hit rate, turnover, fold
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

`research/experiments/` holds fifteen committed experiments: the same five
specifications run three times, under different ids because the dataset
version and the method bundle are both part of the spec. `python -m
iap.research list` prints them with a `dataset` column:

```
experiment        alpha   horizon  dataset           IC      NW t     net bps  verdict
00ebeb2b537b5155  EQ03    5s       116b7787   +0.026570    +4.213     +0.0000  ITERATE
20f1b9093e7d0d04  EQ06    10s      116b7787   +0.034528    +3.598  -1148.3084  ITERATE
217fa0cb1d89a9c8  EQ03    5s       203c8f54   +0.036305   +10.449  -1172.9449  ITERATE
4a2900e4a6705542  EQ06    1s       203c8f54   +0.019676    +3.509   -531.1169  ITERATE
695e7b1e2bd2253e  EQ01    1s       116b7787   -0.001674    +0.771   -531.3535  REJECT
6e4a3431a3acf8a5  EQ01    1s       116b7787   -0.001917    -0.221     +0.0000  REJECT
838e0c2d75de4db6  EQ03    1s       116b7787   +0.013243    +2.414     +0.0000  ITERATE
852863faa44b7b07  EQ06    1s       116b7787   +0.004702    +0.918  -1148.2478  REJECT
876b08e20c46e6fd  EQ03    1s       116b7787   +0.013610    +3.488  -2174.7262  ITERATE
9d895cf7148c4a8d  EQ06    1s       116b7787   +0.003345    +0.690     +0.0000  REJECT
c73bb6294d226163  EQ01    1s       203c8f54   +0.027590    +3.044   -301.4984  ITERATE
d0dd1ab0711d33a1  EQ06    10s      203c8f54   +0.050475    +6.755   -531.1169  ITERATE
d7b554d0a3fa3b26  EQ03    1s       203c8f54   +0.016471    +4.250  -1173.0925  ITERATE
d87e34a9c67c1891  EQ06    10s      116b7787   +0.034331    +3.711     -1.0134  ITERATE
f0f6c49b553f6b59  EQ03    5s       116b7787   +0.027114    +4.840  -2174.7262  ITERATE
```

The five `203c8f54` rows are the v1.3.0 dataset and are kept as history.
The ten `116b7787` rows are the current dataset, twice: five recorded at
v1.4.0 under the rules of the time (they name no method bundle and are
history, not gate evidence), and five recorded at v1.5.0 under `v2` —
`00ebeb2b…`, `6e4a3431…`, `838e0c2d…`, `9d895cf7…` and `d87e34a9…`, the
rows whose net is zero or nearly so. The table does not print the bundle;
each experiment's `spec.json` and `eligibility.json` do (§24.4). For a `v2`
row the `IC` and `NW t` columns hold the gate IC and the pooled-slope t.

The verdicts did not move between the two bundles: EQ03 at 1 s and 5 s and
EQ06 at 10 s are ITERATE, EQ01 at 1 s (a slightly negative IC, two of four
folds positive) and EQ06 at 1 s are REJECT; on the old dataset all five
were ITERATE. What moved is the t and the holdout. The pooled-slope t is
lower than the within-bucket t for EQ03 (1 s: 3.49 → 2.41; 5 s: 4.84 →
4.21) and slightly higher for EQ06 at 10 s (3.60 → 3.71); none reaches the
threshold its run was judged at (4.37 to 4.39, at look counts of 4,020 to
4,356).
And where every legacy holdout lost between 531 and 2,175 bps of the
capital line by trading every flip, four of the five `v2` holdouts make no
trade and the fifth, EQ06 at 10 s, loses 1.01 bps. All fifteen are
leakage-clean, and the five `v2` ones also pass the recompute probe (three
anchors each). No holdout ends above zero.

These numbers are not the report's, and are not meant to be. The runner
runs its walk-forward on the *train* period only (session 1) and reports
session 2 separately as a declared holdout, while `run_all.py` walks
forward over both sessions and declares no holdout — a weaker protocol,
disclosed in `REPORT.md`. That is why EQ01 at 1 s is ITERATE in the report
(gate IC 0.0105 over two sessions) and REJECT in the runner (gate IC
−0.0019 on session 1 alone): a result that thin does not survive halving
the sample. `test_eq03_report_reproduces_through_the_runner` pins the
relationship that does hold — the runner equals `validate_alpha` called
directly on the runner's own window — and asserts the divergence from the
committed report explicitly, so it can only change deliberately.

---

## 7. ML with meta-labeling

`research/ml_reports/run_ml.py` implements spec §14; `ML_REPORT.md` is the
committed result: 210,745 valid rows, 51 curated predictors across all 10
families, target `label_cost_5s`, four expanding walk-forward folds with a
60 s embargo and a 5 s purge.

### 7.1 The gate: advanced models must earn the right to run

Rule: **trees and the MLP run only if the best linear baseline's pooled OOS
IC against the mid-to-mid label is positive**. Simple models establish
whether signal exists; capacity-rich models then refine it. In the
committed run the best linear baseline (ridge) scores **+0.0081** against
the mid-to-mid label → gate **PASSED** → XGBoost, LightGBM and the MLP were
fitted on the same four folds:

| model | tier | IC on the cost-adjusted target | IC vs mid-to-mid label | net bps/signal (conservative) |
|---|---|---:|---:|---:|
| ols | 0 | 0.9580 | +0.0079 | −0.110 |
| ridge | 0 | 0.9581 | +0.0081 | −0.110 |
| elastic net | 0 | 0.9575 | +0.0075 | −0.155 |
| xgboost | 1 | 0.9539 | +0.0046 | −0.111 |
| lightgbm | 1 | 0.9552 | +0.0078 | −0.110 |
| mlp | 2 | 0.0188 | −0.0024 | −2.612 |

Read the pass for what it is. The rule is "greater than zero", and 0.0081
is greater than zero by a small margin; the gate decides whether the
advanced models are *worth fitting*, not whether anything is tradable. What
the advanced tier then showed is the useful part: neither tree model beats
the ridge on the honest column, the MLP is worse than every linear model
on both columns, and **no model earns its costs** — the conservative net is
negative for all six (−0.110 to −2.612 bps per signal). The winner by the
pinned criterion is the ridge. More capacity bought nothing here.

On the v1.3.0 dataset the same gate failed — ridge scored −0.0430 on the
honest column and nothing above the linear tier was fitted — and this
section used to end there. The regenerated dataset changed the outcome of
the gate; it did not change the conclusion that there is nothing to
promote.

The gate did not always read that label. It used to read the model's own
target, the cost-adjusted return, on which the same linear models score a
mean fold IC of 0.958 — and passed trivially, for the reason §7.2
explains. An earlier committed run therefore trained the full zoo and
crowned LightGBM at an "IC" of 0.70. Moving the gate to the frictionless
label is what made the gate mean something: it has since failed on one
dataset and passed, narrowly, on another.

### 7.2 The crossed-book artifact: found, fixed, and honestly residual

An IC of 0.70 at 5 seconds — or the 0.96 the linear and tree models score
on the cost-adjusted target today — would be the greatest alpha ever recorded. It is
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
regenerated. The consolidated equity book is now crossed at only **0.26%**
of ML decision rows. The residual — disclosed, not hidden — is FX: the
consolidated FX book is still crossed **29.5%** of the time, because
aggregated LP quotes go stale between venue updates (a real phenomenon of
FX aggregation, amplified here by the synthetic update cadence). The
spread component therefore still drives the headline numbers:
corr(spread, target) = −0.960, and the IC of 0.96 on the cost-adjusted
target is mostly spread prediction.

Crucially, none of this was ever **leakage** — the shift-by-one test passes
throughout, because the spread at decision time legitimately is in the
information set. It is a *target-construction artifact* interacting with a
*generator artifact*. The honest directional measure — IC against the
mid-to-mid label — is between −0.0024 and +0.0081 across the six models:
two orders of magnitude below the headline, the same size as the weaker
single-feature alphas of §6, and **not an exploitable 5 s directional
signal**. The economics agree: under the conservative cost model (realized
costs floored at zero — you are never paid to cross a crossed synthetic
book), every model sits below 0 bps/signal (ridge: −0.110), while
"label-exact" economics still show +0.170 for the same model — a gap of
0.280 bps/signal of residual book-artifact. The report instructs the
reader to trust only the conservative column.

Two lessons now. First, the old one: **a model can ace its target and tell
you nothing about alpha — always decompose what the target actually
contains.** Second, the new one: **when the decomposition points at the
data-generating process, fix the generator, regenerate everything, and
publish the before/after** — the artifact shrank from ~95% to well under
1% on equities precisely because the honest report made it impossible to
ignore. v1.4.0 applied the second lesson again, to a different generator
defect (§2.3).

### 7.3 Meta-labeling: predicting *whether to act* on a signal

Meta-labeling (López de Prado) separates *direction* (primary model) from
*conviction* (secondary model): the meta-model predicts whether acting on the
primary signal will be profitable net of costs, conditioned on alpha
strength, spread, volatility, depth, queue imbalance and expected cost.

The committed run gates the ridge's pooled OOS predictions (the winner of
§7.1): chronological 50/25/25
train/calibration/test split (60 s embargo), probability calibration on the
middle segment, and an *economic* meta-label (realized net P&L > 0 under
the conservative cost model — not "was the sign right"). The calibration is
**Platt scaling**, a two-parameter sigmoid, because the calibration segment
holds only 255 positives — below the pinned minimum of 500 for the isotonic
path.
Results: test AUC 0.670, Brier 0.0567 (0.655 and 0.0568 at v1.4.0, when
missing meta-features were imputed), test base rate of profitable signals
0.062 — and at both τ = 0.5 and the calibration-chosen best τ = 0.300 the
gate keeps **zero** of the 4,100 test signals. The report flags this
`gate_degenerate: true` and refuses to dress it up: a gate that never fires
produces no evidence either way about whether abstaining pays. It shows
only that the calibrated probabilities sit under the threshold at a 6.2 %
base rate. The gate-off row (−746.7 total net bps, −0.18 bps/trade) is
what the ungated primary would have done. The meta gate was degenerate on
the v1.3.0 dataset too; passing the model gate of §7.1 did not change that. The machinery details still
matter: thresholds must be chosen on a calibration segment in probability
space and evaluated economically — and a gate that took no trades must be
reported as untested, not as vindicated. (Since v1.3.0 the reported
`auc_test` is `null` rather than a fabricated 0.5 when a test segment holds
a single class. And since v1.5.0 a missing meta-feature stays missing:
`impute_nan=False` is the default — it was an opt-in in v1.3.0 and v1.4.0
— because 0 is a legitimate value of several meta-features (a balanced
queue, a flat alpha), so imputing zero makes "missing" and "balanced"
indistinguishable to the tree model, which handles NaN natively;
`impute_nan=True` is the legacy rule. Here 275 of the 131,880 meta-feature values are missing, and
leaving them so moved the AUC by 0.015 and the gate not at all.)

### 7.4 Manifests: every fit is an audited experiment

Every model fit lives under `research/models/run_NNNN_<name>/` with a
`manifest.json` recording experiment id, git commit, **data version** (hash
of the QC report), **feature version** (registry hash), model version,
hyperparameters, train/test windows, and hardware — plus metrics and the
pickled model. `ledger.json` holds the monotone model-run counter (47 fits
so far) — a different counter from the alpha multiple-testing ledger of
§6.6, and neither is a substitute for the other. Reproducibility is not a README
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
  suites and prints the table (the v1.5.0 counts from CI, 2026-10-04: python 1701,
  cpp 289, rust 333, java 522 tests passed; golden groups 179/68/69/109; all
  PASS, plus `integration` (35) and `replay` (6) rows for the repo-level
  pytest suites, a `deployment` row — 25 structural checks passed in CI,
  where promtool and kubeconform are installed — and a `numbers` row that re-derives every headline
  figure in the docs from its artefact). The Java golden group runs all
  thirteen `*GoldenTest` classes (it once ran two of them and reported 18),
  the Rust group ten golden targets. The 2026-09-19/20 release added a new
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
stress rescores every alpha with signals delayed by +1 and +5 events (last
fold; `+0ev / +1ev / +5ev IC` in the report). The fast equity flow alphas
(EQ02/EQ03/EQ12) shed 58–73 % of their IC after a single event (EQ02:
0.0218 → 0.0091; EQ03: 0.0157 → 0.0042; EQ12: 0.0231 → 0.0090), and
EQ01's microprice signal changes sign after one (0.0062 → −0.0049). What
is left after that is small and not monotone — the +5 column reads 0.0097,
0.0099, 0.0105 and 0.0022 for the same four — so the honest summary is
"most of the IC is gone after one event", not a decay curve. One equity
event is a bigger unit than it used to be: emitted rows are a mean 3.1–3.3 s
apart since v1.4.0 (about 1.3 s inside the old active window, where the
same alphas lost 15–21 % per event), so "+1 event" is now more than half
of a 5 s horizon. EQ06, at a 10 s horizon, is not ordered at all (0.0194 →
0.0146 → 0.0330), and EQ11 at 15 minutes barely moves (0.0166 → 0.0158 →
0.0198) on an IC that is not significant to begin with. The FX regime
family retains ~77-83% at one event (~22 s of FX tape) but only ~8-41% at
five (FX09: 0.1617 → 0.1337 → 0.0472). These are the v1.5.0 figures; they
differ from the v1.4.0 ones in the third or fourth decimal, because the
ICs now score the reopen rows of §5 — except EQ11, whose +0 IC moved from
0.0124 to 0.0166 for the reason §6.5 gives. Alpha
decay against *events* is the economically meaningful axis — provided the
size of an event is stated next to it.

(The same report carries a latency stress of the *P&L*, in clock time and
on the row grid. Since v1.5.0 the row grid carries the whole base backtest
configuration into every stressed run — `stress_version=2`; the legacy
grid, `stress_version=1`, rebuilt the configuration from four fields and
silently dropped the rest, so a stressed run was not the base run plus a
delay. Under the cost-aware policy most of those P&L cells are 0: an alpha
that does not trade on time does not trade late either.)

**Measurement 2 — the speed of the stack.** The measured C++ hot path
(decode + book + features + alpha = 184.1 + 26.4 + 514.1 + 33.6 ns, the
2026-09-19 `benchmarks/results_cpp.md`) sums to ≈ 0.76 µs/event, versus a
median inter-event gap of ≈ 1.3 s per equity instrument on this dataset —
six orders of magnitude of headroom. (The ≈ 0.5 µs this section used to quote predates the
mandatory CRC-32 IAP1 trailer; paper 04's benchmark errata carry the
re-derivations; the table moves a few percent per regeneration and the docs
follow it.) Serialising a decision trace costs 31.7 µs per 5.6 KB record —
paid once per decision, off the event loop, ≈ 37 ns/event amortised. Rust
and Java demo-scale replays (≈ 6.5M events/s through the SPSC bus and
≈ 3.5M events/s) are equally overprovisioned.

**The synthesis**: on this platform, latency economics are entirely about
*reacting to the next event* rather than compute speed. Latency investment
is existential for the flow alphas (most of their IC is gone one event
late — though on this dataset costs bury them regardless), and worth much
less for the FX regime book, which keeps about four fifths of its
statistics through a one-event lag. The slow equity alphas, which used to
be the other half of this contrast, no longer carry a significant IC on
the regenerated dataset (EQ09, EQ11), so they illustrate nothing either
way. The general lesson transfers to real markets: price
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
z-score watches the thing you actually care about — is the alpha still
predicting? — with maturity handled correctly: a row only enters the
rolling IC once its label horizon has fully elapsed. Since v1.5.0 the z is
a two-sample HAC statistic (`adaptive.ic_z_method = "hac"`):

```
z = (mean_w(live bucket ICs) − ic_mean) / √(var_live + var_base)
```

with `mean_w` the pair-count-weighted mean of the live bucket ICs,
`var_live` its Newey–West variance and `var_base` the variance of the
baseline mean. The legacy z (`"legacy"`, the default up to v1.4.0) was
`(mean live bucket ICs − ic_mean) / (ic_std / √n)`, which assumes three
things that do not hold: that the baseline mean is a known constant
rather than an estimate, that bucket ICs of overlapping labels are
independent, and that a bucket of 40 pairs is as informative as one of
400. Each assumption makes the z larger than the evidence, and a z that
is too large is a refit trigger that fires on the baseline's own sampling
error. The rolling IC the lifecycle reads is the same pair-weighted mean,
and the Java live gauge (`RollingIc`) computes it that way too; the z
itself is computed by the Python reference only, because no port
evaluates a refit trigger.

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
**88 (drift-triggered)** total refits — and none of that activity made
anything profitable: no deployment ends above zero after costs (0 of 40).
Under the default cost-aware backtest 19 of the 40 make no trade at all
(EQ01, EQ03 and FX01 under every policy, FX06 under every policy, FX05
under three of four) and 21 trade and lose. The policies are not
interchangeable, though: a refit changes how often an alpha's forecast
clears its costs, and so how much it trades. On 4 of the 10 alphas every
policy ends at the same net P&L; the widest gap between two policies on
one alpha is 2,111 USD (FX11: −19 static on 35 trades, −2,130
drift-triggered on 5,828), and it is a difference in costs paid, not in
edge found. Refitting rescues no alpha here. The three equity alphas
(EQ01, EQ03, EQ06) log no drift event and the drift-triggered policy
never refits them beyond the initial fit; 85 of the 88 are FX fits. Of
the 78 refits after the initial ones, 65 name a PSI breach and 14 the
rolling-IC z (a refit can name both).

The v1.4.0 report read 122 drift-triggered refits on the same data, under
the legacy z and the consecutive-breach rule; the HAC z fires less often
(FX05: 34 refits then, 20 now; FX08: 20 then, 6 now). Its P&L column is
not comparable with today's for a second reason besides the backtest
policy: the adaptive runner summed quote-currency FX P&L as if it were
USD until v1.5.0 fixed it, so the v1.4.0 policy totals (−8.69 million)
were wrong in scale. One older detail for anyone
diffing against v1.3.0: the FX rows moved by about one trade per
deployment between v1.3.0 and v1.4.0 although the FX data is
byte-identical. That was not a data
effect — the adaptive report committed at v1.3.0 had last been generated
before the 2026-09-20 backtester corrections, and re-running the v1.3.0
code on the v1.3.0 dataset reproduced the v1.4.0 FX numbers.

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
- **WATCH** → RETIRED on persistent breach; back to
  ACTIVE only after **3 consecutive** readings at or above 0.005. Since
  v1.5.0 "persistent" is a **CUSUM** (`breach_rule="cusum"`): each
  reading adds `new_fraction × (0.0 − rolling_ic − k)` to a running sum
  that is floored at zero, with slack `k = 0.0025`, and a WATCH reading
  that is itself a breach and leaves the sum at or above `h = 0.01`
  retires the alpha. `new_fraction` is the share of the reading's window
  that is new — 0.125 here, a 2-hour window advancing by a 15-minute
  block. That weighting is the reason for the change. The legacy rule
  (`"consecutive"`, the default up to v1.4.0) retired after **6
  consecutive** breaches, with a neutral-zone reading in `[0.0, 0.005)`
  resetting both counters; but six successive readings of a window that
  moves by an eighth share most of their rows, so six breaches can be one
  bad stretch counted six times. The CUSUM asks for a shortfall that adds
  up, in new information, to the IC an alpha needed to be promoted (0.01)
  — both parameters were fixed before any result was computed with them.
  It never retires on the reading that entered WATCH.
- **RETIRED** — allocation verifiably halted (the adaptive backtest forces
  positions flat), but *shadow scoring continues*, so the pinned
  re-activation path stays reachable: 3 consecutive recoveries earn WATCH
  — probation again, never a straight jump back to ACTIVE.

A null rolling IC (too little matured data) causes no transition at all.
Every transition appends a JSON line to `research/lifecycle_log.jsonl`
with the alpha, policy, timestamp, states, reason and the IC that caused
it — 300 in the committed study (260 under the legacy rule at v1.4.0) —
and the asymmetry of the design (fast
to suspicion, slow to trust) is the point. Four alphas hit RETIRED under
at least one policy (FX01, FX05, FX06, FX11); FX01 finishes RETIRED under
all four, which is the system doing its job: an alpha whose pooled
promotion IC is negative (−0.015; it is ITERATE only on the uncrossed
rows, with no positive fold — §6.5) gets caught and shut off by the live
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
FX10 logs 156 drift events where no other alpha logs more than 31, and
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

Two synthetic sessions (6.5 h of equity flow and about 21 h of FX quotes
each) cannot rank refit policies, and the report leads with that instead
of burying it:
SCHEDULED(weekly) crosses no week boundary, so it is *identical to
static by construction*; daily fires exactly once; the generator has no
real regime shifts, so a drift trigger that fires here is reacting to
sampling noise or to a monitor that drifts by construction — 65 of the 78
drift-triggered refits name a PSI breach, and 39 of those 78 are FX10's
calendar feature (§14.6); and the P&L differences between policies are
differences in how much each one trades, on one synthetic day of
out-of-warmup data, not evidence about any of them. What the study *does*
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
"failed gates" column, for every one of them, `net_pnl_after_costs` and
`capacity`. That is
the promotion report's finding restated by a state machine that reads the
same numbers through a different gate table, which is the reason for
pinning both: two independent readings of one artefact agree.
`test_bootstrap_failed_gates_agree_with_report_verdicts` proves it
mechanically (it recomputes the failed-gate set from each report's raw
numbers and the thresholds). Read the table closely and the research story
of §6 reappears: EQ02, EQ03 and EQ12 fail *only* the two economic gates
(statistics real, economics not); FX04 and FX06 add
`statistical_significance`, and EQ01, EQ06 and three of the FX regime
family (FX08, FX10, FX11) add that and `stability` — the
Pearson/rank-IC gap, the one pair of numbers in a result that measures the
*shape* of the signal–label relation rather than its strength; EQ05 and
EQ11 fail `oos_ic` on top of all of those, EQ10 `oos_ic` and
significance; EQ04 and EQ07–EQ09 fail the IC, significance and
fold-consistency gates and `hypothesis_sign`. Over the 24: cost 24,
capacity 24, significance 21, stability 14, IC 12, fold consistency 10,
hypothesis sign 9.

Two of those counts are the v1.5.0 defaults at work. The significance gate
is read against the ledger threshold (4.365 for this report) instead of a
fixed 3.0, which is why EQ06 at 4.36 and FX04 at 4.24 now fail it and 21
alphas do where 18 did. And `capacity` (at least 1,000,000 USD) fails for
every alpha because the capacity is now the edge-breakeven size — the
size at which the realised edge per round trip equals its cost — and an
alpha that makes no trade, or loses before costs, has a capacity of 0;
the three that have one at all read 38,248 (EQ11), 1,561 (FX08) and
61,135 USD (FX10). Under the legacy participation proxy the gate passed
for all 24, edge or no edge. The history of the "cost only" list: on the
v1.3.0 dataset eight alphas failed only the cost gate (EQ02, EQ03, EQ12,
FX04, EQ01, EQ05, EQ06 and EQ11); on the v1.4.0 dataset under the old
rules four did (EQ02, EQ03, EQ12, FX04); under the default rules three
fail only cost and capacity. The
transition log was rebuilt with `bootstrap --force` for the new rules and
the previous one is archived under `research/archive/`
(`lifecycle_transitions.dataset-116b7787.methods-legacy_v1.jsonl`).

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

`tests/golden/expected_lifecycle.json` (`x-version` 2 since v1.5.0) scripts
four lives under the default policy and one under the legacy policy,
compared exactly, step by step, in Python, Java and Rust. LC01 is the
whole ladder:
RESEARCH → CANDIDATE on a ledger entry and a clean leakage test; →
VALIDATING on the nine research gates (a t of 4.0 against a ledger
threshold of 3.5); → PAPER on a reproducible replay and
parity; → ACTIVE on five paper sessions with a tracking IC; then a hold, a
null IC (nothing moves), an uninformative IC (nothing moves), a breach →
WATCH, where the CUSUM starts accumulating, a second breach, a
neutral-zone reading that drains it, three recoveries → ACTIVE, a relapse
→ WATCH, and on the fifth breach since the relapse the CUSUM reaches its
threshold → RETIRED (five readings of −0.02, each adding 0.125 × 0.0175);
a SYSTEM advance that records `TERMINAL` and moves nothing, and
a HUMAN reset → RESEARCH. LC02 is the leaking re-run demoted to RESEARCH at
once; LC03 the three paper failures demoted to CANDIDATE and a HUMAN
retire. LC04 is what the CUSUM rule changes: eight breaches inside the
slack that never retire (the consecutive rule would have at the sixth), a
deep breach in a disjoint window that does, a breach deep enough to cross
the threshold at once that only enters WATCH, and a reading above the gate
that does not retire whatever the sum. LG01, under `legacy`, is the LC01
script with the rules up to v1.4.0 — a fixed significance threshold, six
consecutive breaches → RETIRED. Everything a port could get subtly wrong
— which counter resets
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
stream; the MVP golden pins a whole session's digest (`e534ac1f…`, 800
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
deterministic, checksummed and archived. `schemas/sql/iap_v2.sql`
([docs/DATA_MODEL.md](docs/DATA_MODEL.md)) is a relational *index* over
them: 26 tables and 6 views into which every contract maps, built by
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
same chain `explain()` prints as text. `v_alpha_scorecard` has one row per
alpha and scope — the dataset and the method bundle a number was computed
in — with the latest experiment result of that scope, its verdict, the look
count and threshold it was judged at and the scope's ledger count;
`v_alpha_scorecard_current` is the current dataset and bundle only, so a
legacy-rules result or another dataset's looks never leak into today's
numbers. `v_experiment_ledger_summary` gives the multiple-testing
denominator per scope and kind. The questions the spec's observability section asks — why did we
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
# mvp run 58a10f2194a3c81c: events=15805 decisions=800 parents=235 children=348 fills=169 pnl=-81.531396 USD digest=e534ac1f06c50537...
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
asserted after every fill (|diff| 6.8e-11 on the golden run).

The run id is the same as in v1.3.0 — `configs/mvp/mvp.json` and the seed
did not change — but everything behind it did: the MVP feed comes from the
same generator, so the v1.4.0 flow calibration (§2.3) changed the stream,
the fitted parameters changed with the dataset, and the golden was
regenerated.

v1.5.0 regenerated it once more and changed nothing a trader would see.
The loop does not use the research backtester, so the new default methods
do not touch it: the events, decisions, parents, fills and P&L are
identical to v1.4.0. What moved is the digest (`f51890da…` → `e534ac1f…`)
and the `config_version` it covers (`f293e7e7…` → `bf8cc608…`), because
`config_version` hashes `configs/execution/execution.json`, which now
names its impact model, and `alpha_params.json`, whose header names the
regeneration commit. A digest that changes when a hashed configuration
document changes, with every count and every dollar the same, is the
digest doing its job.

### 20.2 The numbers, stated as they are

Seed 12345: 15,805 events, 800 decisions, 235 parent orders, 507 children
generated of which 348 submitted (159 blocked by the 500 ms slice-interval
control, exactly as the Java loop would), 169 fills, 15.1 % fill rate.
Risk: 348 ALLOW, 0 REJECT, 0 KILL, 8 sequence gaps each recovered by the
following SNAPSHOT burst, 28 mark regressions dropped. Routing: XV3 70.9 %,
XV1 19.8 %, XV2 9.3 % of filled quantity (aggressive routing picks the
cheapest taker fee on price ties; passive routing prefers XV1's rebate).
TCA: implementation shortfall +0.063 bps quantity-weighted; TWAP fills
0.4 % (passive limits at a 1 s horizon mostly expire), POV 6.3 %, IS
72.9 %. **P&L −81.53 USD** on 8,229 shares: +0.022 bps of alpha
contribution against −0.41 bps of execution cost, net −0.39 bps of filled
notional. The report field is
`alpha.cost_negative: true`. This is the research finding of §6 — a small
signal, no money after costs — reproduced by a full loop on a different
synthetic stream, and it is the headline number of the MVP, not a footnote.

Why 800 decisions where v1.3.0 had 355, on slightly fewer events? Because
the 15-minute session now has flow in all of it. Under the old budget rule
the stream ran out part-way through the session and the loop had nothing
to decide on afterwards; now the decisions are spread evenly over the
fifteen minutes. More decisions made more parents, more fills and a loss
3.6 times as large, at the same cost per unit of notional (−0.41 bps both
times): the loop did more of the same unprofitable thing.

### 20.3 The IC audit: a worked example of "disbelieve it, then decompose"

The golden session reports realized ICs far from the research ICs:

| alpha | realized IC at 1 s | cost-adjusted | shift-by-one | n | fitted horizon: realized IC | research IC | gap |
|---|---:|---:|---:|---:|---:|---:|---:|
| EQ01 | +0.217 | −0.024 | +0.149 | 790 | 1 s: +0.217 | 0.011 | 0.206 |
| EQ03 | +0.110 | −0.029 | +0.116 | 707 | 5 s: +0.147 | 0.019 | 0.128 |
| EQ06 | −0.079 | +0.079 | −0.047 | 354 | 10 s: −0.130 | 0.028 | 0.158 |
| ensemble | +0.104 | −0.003 | +0.111 | 790 | — | — | — |

(The research IC is the gate IC of the v1.5.0 report — 0.0105, 0.0189 and
0.0277 — which is why the last two columns differ from the v1.4.0 table in
the third decimal; the realized columns are unchanged.) EQ01's realized IC
is twenty times its research IC; EQ06's has the wrong
sign. The pitfalls of §27 say what to do with numbers like that. The audit
was first done on 2026-09-20, on the v1.3.0 stream, when the loop reported
0.285 / 0.338 / −0.102 against research ICs of 0.027 / 0.030 / 0.043
(docs/MVP.md §7.1 records it); the steps are the same on the v1.4.0
golden, and one of them now reads differently.

1. **Pin the definition.** The realized IC is computed by
   `iap.labels.compute_labels` on the feature engine's book-refresh mid
   series — the research label, not a timeline-based approximation — at the
   MVP horizon and at each alpha's fitted horizon. A test rebuilds the frame
   independently and asserts the same anchors, the same valid set, the same
   labels to 1e-9 and the same IC to 1e-12. (The definition before the
   audit had let through windows that contained a stale-venue blackout.)
2. **Prove it is not a leak.** The engine is single-pass; cutting the stream
   at 35 % and 70 % reproduces every earlier signal and target bit for bit
   (the truncation probe, `test_shift_by_one_and_truncation_leakage_probes`).
   The *research* code path, run on the same captured stream, gives the
   same numbers as the loop at the 1 s cadence (0.217 / 0.110 / −0.079) and
   nearly the same at the 100 ms research cadence (0.207 / 0.119 / −0.087
   over 5,095 rows; repeated on the v1.4.0 stream, docs/MVP.md §7.1). The
   number is a property of the data. The pinned definition and the
   truncation probe are tests, and they run against the current generator
   on every CI run.
3. **Read the shift-by-one honestly.** This is the step that changed. On
   the v1.3.0 stream, lagging the signal by one decision collapsed the IC
   (0.031 / −0.034 / 0.021), and the write-up had to explain why a
   collapse at a cadence equal to the horizon cannot discriminate a leak
   from a genuine fast signal. On the v1.4.0 stream there is no collapse
   to explain: lagged by one decision, EQ01 keeps 0.149 of 0.217 and EQ03
   and the ensemble do not fall at all (0.116, 0.111). That is persistence:
   EQ01's signal has an autocorrelation of 0.78 from one decision to the
   next, and its IC decays smoothly with the lag — 0.217, 0.149, 0.109,
   0.085 at lags 0 to 3, and about zero by lag 5 — the pattern of a slowly
   changing book state that predicts the next few seconds. A surviving
   shifted IC does not prove a leak and a collapsing one did not disprove
   it; the leak evidence is items 1–2, on both streams. The research
   harness's own shift test (`iap.validation.leakage`) scales its required
   survival ratio by `1 − row_gap / horizon` for the same reason: what a
   one-row shift should do depends on how the row spacing compares with
   the horizon.
4. **Explain the data.** The generator quotes every venue around one shared
   efficient price plus a bounded AR(1) venue noise (ρ 0.9 per flow slot)
   and cancels resting orders the efficient price has moved through:
   the displayed book *leans* towards the efficient price and the mid
   converges to it. Microprice deviation and OFI measure that lean. Under
   the v1.4.0 calibration the flow slots are further apart in clock time
   (same expected count, whole session), so a noise that decays per slot
   decays more slowly per second, and a 1 s decision cadence can see the
   same lean more than once. That is a plausible reading of the
   persistence in item 3, by construction of the generator; it has not
   been isolated by an experiment. A real feed would not be this kind. Nothing here explains EQ06's negative
   realized IC on 354 samples of one 15-minute session, and this document
   does not try to.
5. **Check that it still does not pay.** The cost-adjusted IC — buy the ask
   now, sell the bid at t + h — is −0.024 for EQ01 and −0.029 for EQ03 at
   1 s, and −0.003 for the ensemble the loop trades on. The predicted move
   is smaller than the spread; that is the −81.53 USD.
6. **Let the lifecycle see it.** `paper_evidence.json` carries the realized
   IC at the fitted horizon, so the `paper_ic_tracking` gate (max gap 0.01)
   fails all three alphas on this data (gaps 0.206 / 0.128 / 0.158; on the
   v1.3.0 stream it failed EQ01 and EQ03) — correctly: paper behaviour
   that differs this much from research is a finding, not a promotion.

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
cancel from behind, so the fix reproduced `expected_replay_fills.json`,
`expected_mvp.json` and `expected_tca.json` unchanged (the v1.3.0 files;
the MVP golden has since been regenerated for the v1.4.0 dataset, §20). Cross-language
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

The generator gained an opt-in `planted` block (this one stays opt-in: it
is a test fixture, not a research method). It is off by default, adds
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

| effect | scenario | level | mean gate IC | mean gate t (pooled slope) | significant | verdict ITERATE or better | PROMOTE | mean trades at 1× |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| order flow (EQ04) | stable | 0 | −0.0054 | −0.51 | 0.00 | 0.00 | 0.00 | 0 |
| order flow (EQ04) | stable | 0.5 | +0.0032 | +0.22 | 0.00 | 0.00 | 0.00 | 0 |
| order flow (EQ04) | stable | 1 | +0.0327 | +2.93 | 0.33 | 1.00 | 0.00 | 0 |
| order flow (EQ04) | stable | 2 | +0.0823 | +6.20 | 1.00 | 1.00 | 0.00 | 14 |
| lead-lag (EQ10) | stable | 0 | −0.0018 | −0.23 | 0.00 | 0.00 | 0.00 | 0 |
| lead-lag (EQ10) | stable | 0.5 | +0.0047 | +0.68 | 0.00 | 0.33 | 0.00 | 0 |
| lead-lag (EQ10) | stable | 1 | +0.0117 | +1.23 | 0.00 | 0.33 | 0.00 | 0 |
| lead-lag (EQ10) | stable | 2 | +0.0137 | +1.75 | 0.00 | 0.67 | 0.00 | 0 |
| both | break | 0.5 – 2 | −0.036 to −0.0001 | −2.28 to +0.12 | 0.00 | 0.00 | 0.00 | 0 |

The study has been run three times. It was re-run for v1.4.0 because its
generator configuration then took the flow calibration of §2.3
(`research/power/generator_planted.json`,
`calibration: "session"`, the same `slots_per_stream`): the planted
sessions have flow to the close, and the table moved with them. It was
re-run for v1.5.0 under the default methods (report version 2), on the
same planted sessions: the t in the table is now the pooled-slope t the
gate reads, the ICs score the reopen rows, and the backtest behind the
PROMOTE column is the cost-aware one. Read it row by row.

- **The chain has power, for one effect, at a large enough size.** At
  twice the reference size the order-flow effect is flagged significant in
  three seeds of three. At the reference size it is flagged in one of
  three — all three come out ITERATE, but two have a t below 3. At half
  size it is not detected at all, not even as ITERATE. The new methods
  changed none of these rates.
- **That is less power than the same study showed on the compressed
  flow.** On the v1.3.0 generator the reference size was flagged in three
  of three and half size in one of three. The same planted strength
  produced about half the IC after the recalibration (+0.0313 against
  +0.0726 at the reference size, +0.0813 against +0.1579 at twice it, both
  runs under the rules of the time; the v1.5.0 run reads +0.0327 and
  +0.0823). The two configurations
  differ only in the flow calibration — the same expected number
  of slots spread over 2.5 times as much clock time; the study does not
  isolate which consequence of that (sparser rows, staler labels, fewer
  executions per kernel window) costs the power.
- **For the other effect it has almost none.** The lead-lag effect is
  flagged significant in no seed at any size, by any of the three
  statistics. Under the pooled-slope t it now reaches ITERATE in one, one
  and two seeds of three at half, one and two times the reference size —
  ITERATE needs a t of 1.5, and the mean pooled t at twice the size is
  1.75 where the within-bucket t was 0.87. In the v1.4.0 run it never
  reached ITERATE. The mean IC rises with the planted size (0.0047,
  0.0117, 0.0137) and so does fold consistency, so the effect is in the
  data; the chain sees a hint of it and does not establish it. A real
  effect of that size would be reported as "not found". (On the v1.3.0
  generator twice the reference size was flagged in one seed of three.)
- **The null rows are clean, for what three seeds are worth.** With
  nothing planted no seed is flagged and none comes out ITERATE. On the
  v1.3.0 generator one lead-lag seed of three did — a false "evidence".
  Zero of three now and one of three then are both compatible with the
  same small false-positive rate; neither measures it.
- **The break rows behave.** An effect that reverses mid-sample is
  flagged in no cell, and fold sign consistency falls to between 0.08 and
  0.50, against 0.75 to 1.00 in the stable cells at the reference size and
  above.
- **Nothing is promoted, at any size.** Not even the planted effect with
  a t of 6.20. Zero folds survive 1× costs in every cell, and no bootstrap
  interval for net P&L lies above zero. The last column says how: at the
  reference size the cost-aware backtest does not trade the planted
  effect at all — the forecast never exceeds the round-trip cost — and at
  twice that size it trades 14 times on the last fold, on average, and
  loses.

That last row is the one that changes how to read the headline. PROMOTE
requires positive net P&L after costs under the research execution model,
and in this generator's cost structure even a large, real, stable, planted
effect does not clear it. So "0 PROMOTE on the bundled data" is not
evidence that the chain is a strict judge of alpha. It is at least partly
a statement about the generator's spreads relative to the size of any
signal in it. Up to v1.4.0 this paragraph could add "traded with a
sign-following policy that pays the spread on every flip", and the policy
was a candidate explanation. It no longer is: a policy that trades only
when the forecast clears its costs finds nothing to trade at the
reference size and loses at twice it. The chain can see an effect it
cannot monetise. The significance half of the chain is informative. The
promotion half has not been shown to be reachable.

### 23.3 What the corrected statistics change

The report scores each run three ways: the **pooled-slope HAC t**, which
tests the pooled IC directly and keeps signal that lives between buckets —
the statistic the gate reads since v1.5.0; the within-bucket Newey–West t,
the legacy gate statistic; and the pooled t against a **ledger-derived
threshold** — the multiple-testing threshold
of the study's own 42 tests, t ≥ 3.24 rather than 3, the significance
gate as PROMOTE now applies it. In this grid the
three give the same detection rate in every cell. The v1.3.0 run had one
cell where they differed — lead-lag at twice the reference size, where the
pooled t flagged two seeds of three and the within-bucket t one — and this
section read that as weak evidence that the pooled statistic has more
power for a between-bucket effect. The re-runs do not repeat it: the mean
pooled t in that cell is still the larger of the two (+1.75 against
+0.87), but neither flags a seed; the difference shows only at the lower
ITERATE bar, where two seeds of three now pass.

So the pooled-slope t did not become the default because this study
showed it to be more powerful — the evidence for that is thin, and was
thin when the choice was made. It became the default because of what it
tests. The gate reads a pooled IC; the t beside it should be the
significance of that slope and not of a different statistic, and the
within-bucket t discards exactly the between-bucket component the pooled
IC contains. The cost is visible in §6.5: for the equity ITERATE alphas
the pooled t is the *lower* of the two (EQ03 5.16 against 5.85). The
within-bucket t stays selectable (`significance="within_bucket"`) and is
printed as `t other` in every report.

### 23.4 What this study is not

Three seeds per cell: a rate moves in steps of 0.33, and the report says
"this calibrates the chain, it is not a precise power curve". The planted
effects are the generator's own construction, detected by the two alphas
written for exactly those effects; power against an effect of a different
shape is unknown. And it is synthetic end to end. A power statement about
real markets needs real data (EPICS E25).

**Check yourself.**

*Q. The order-flow detector reports a mean IC of −0.005 with t −0.51 on the
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
the costs was the shortest path to a pass. (Under the cost-aware default
it happens not to work on this data — at 0.5× costs the alphas that start
trading lose, and the rest still make no trade — but the knob is the same
knob, and a control should not depend on the data being unhelpful.) The
same goes for running with
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
- **The threshold follows the count.** The ledger has always computed
  the Bonferroni threshold its count implies, and up to v1.4.0 no gate
  read it. With `tstat_threshold="ledger"` the PROMOTE gate is the larger
  of 3.0 and that threshold. It was an opt-in in v1.3.0 and v1.4.0 and is
  the default since v1.5.0 (`"fixed"`, a flat 3.0, is the legacy rule):
  the committed report was judged at 4.365, the threshold at its 3,936
  looks, and the five runner experiments at 4.37 to 4.39. A fixed 3.0 on a
  ledger of thousands of looks is a gate that selection alone is expected
  to pass — the expected largest |t| under the null is above 4. The count
  a run is judged at is recorded with it (`gate_looks`), a rerun is judged
  at the recorded count, and `eligibility.json` carries the threshold and
  the looks it came from. On the bundled data the change moved no verdict
  — nothing is promoted under either threshold — and it halved the list
  of alphas that pass the significance gate, from six to three (§6.5).
  The CLI flag `--tstat-threshold` is gone: the policy is part of the
  method bundle, and the bundle is part of the experiment id.

Why were these methods opt-in for two releases rather than simply fixed?
Because the committed reports, the
ledger and the goldens were produced under the pinned definitions, and a
platform whose numbers change when the code is upgraded has lost the
ability to say what changed. So v1.3.0 put each corrected method beside
the pinned one with a name and measured its effect, v1.4.0 changed the
dataset and nothing else, and v1.5.0 changed the defaults and nothing
else — on an unchanged dataset, with every artefact regenerated in one
pass and a results table (CHANGELOG.md) that attributes each moved number
to a rule. The old rules stay reproducible under their legacy names:
`python -m iap.research run --methods legacy_v1`, and
`research/alpha_reports/run_all.py --methods legacy_v1 --out-dir <dir>`,
which is tested field by field against two reports pinned from the v1.4.0
tag. docs/RESEARCH_VALIDITY.md lists every pair.

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
# 27 passed
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
   config, dataset) — 208 distinct configurations, 4396 looks — with a printed
   expected-max-|t| yardstick of 4.096; FX08's t = 3.84 is called what it is.
   Regenerating the dataset did not reset the count, and neither did
   changing the default methods: the looks already taken are kept and the
   new ones added (§6.6). Since v1.5.0 the count is not only printed: the
   PROMOTE significance gate is derived from it.
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
0.019, t 5.2 on uncrossed rows, every fold positive — and the move it
predicts is smaller than the round-trip spread and fee. A backtest that
trades only when the forecast clears its costs (the default since v1.5.0)
therefore makes no trade at all, and a net P&L of 0 does not pass
`net P&L > 0`; one that follows the sign of the signal (the legacy
policy) pays the spread on 423 flips an hour and lost 148,562 at 1× costs
in the v1.4.0 report. Two ways of measuring the same fact.
IC measures correlation; P&L measures correlation × horizon × turnover −
costs.

**Q2. What is the microprice and when does it beat the mid?**
`(Pb·Qa + Pa·Qb)/(Qb+Qa)` — the size-weighted touch price that leans toward
the heavy side's opposite quote. It adds information when displayed L1 sizes
are informative about the next move. On this repo's MBO equity book it is
a weak positive predictor (EQ01 ITERATE — uncrossed IC 0.0105, gate t
1.59, hypothesis-confirmed, three of four folds, no trade clears its
costs); on its
quote-driven FX book the sign depends on whether rows with a stale,
crossed consolidated quote are counted, and no fold is positive (FX01,
ITERATE on the uncrossed rows only). Structure decides what the number
means.

**Q3. Why purge *and* embargo in walk-forward validation?**
Purging removes training rows whose label windows overlap the test period —
direct leakage. The embargo (60 s here) additionally covers serial
correlation just outside the overlap. Purge width follows the label horizon;
embargo handles what the purge can't see.

**Q4. Your ML model shows IC 0.70 at 5 seconds. What do you do?**
Disbelieve it, then decompose the target. Here the cost-adjusted target
embeds a spread component that is highly predictable (corr ≈ −0.96) because
the consolidated book is sometimes crossed — 29.5% of the time on FX from
stale aggregated LP quotes, and in an earlier generator version ~95% of the
time on equities, a bug the decomposition exposed and a redesign (shared
efficient price) fixed down to 0.26%. Directional IC vs the mid label is at
most 0.008, and no model earns its costs.
Check what the target contains, check IC against a frictionless label, check
economics under a conservative cost floor — and if the answer implicates
the data-generating process, fix the data, not the story.

**Q5. What is meta-labeling, and what does it mean when the gate keeps zero
trades?**
A secondary model predicting whether *acting* on the primary signal is
profitable net of costs. With a profitable-trade base rate of 0.062,
calibrated probabilities rarely approach 0.5, so thresholds must be chosen
on a held-out calibration segment in probability space and evaluated
economically. On this dataset even the calibration-chosen τ = 0.300 declines
every test signal, and the gate-off alternative realized −0.18 net
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
Measure alpha decay in *events*, per strategy, and say how long an event
is: here the fast flow alphas shed 58-73% of IC after one event of
staleness (one equity row is about 3 s on this data; EQ01's microprice
signal changes sign outright), and the FX regime family holds ~77-83% at
one event (about 22 s) but loses most of its IC by five (~8-41%) — and the
compute path is six orders of magnitude faster than the feed. So the marginal microsecond of
compute is worthless, but being events late is existential for flow alphas.
The budget goes wherever your reaction-to-next-event chain is actually
bottlenecked, weighted per alpha family.

**Q11. Your paper-trading loop reports a realized IC ten times the research
IC. What do you do before you celebrate?**
Pin the definition, then attack the number. Here the MVP's EQ01 reads 0.217
against a research 0.011 (§20.3). First make the realized IC *be* the
research label (`iap.labels.compute_labels` on the same book-refresh series,
asserted to 1e-12 against an independent rebuild); then prove the engine is
single-pass (truncate the stream, reproduce every earlier signal bit for
bit) and that the research code path on the same captured stream gives the
same number; then read the shift-by-one number for what it can show (here the
signal lagged by one decision keeps 0.149 and decays smoothly with further
lags: persistence, which is neither proof of a leak nor proof against
one); then look at the
data — a synthetic generator whose venues lean towards a shared efficient
price makes microprice and OFI look clairvoyant at 1 s; and finally check
the cost-adjusted IC (−0.024) and the P&L (−81.53 USD): still no money. A
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
    corrected methods (opt-in in v1.3.0 and v1.4.0, the defaults since
    v1.5.0, each with its legacy name) and the planted-signal study behind
    §23–§24;
    `CHANGELOG.md` for what v1.3.0 fixed and what it leaves open, for
    the v1.4.0 dataset regeneration (§2.3), and for the v1.5.0 table of
    what each new default moved.

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
