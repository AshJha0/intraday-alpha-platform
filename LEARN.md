# LEARN.md — A Guided Tour of the Intraday Alpha Platform

This document walks through the entire platform the way a textbook would:
concepts first, then how this repository implements them, always with the
repo's *real, verified numbers* as worked examples. Everything cited here can
be reproduced from the committed code and seeds — that reproducibility is
itself one of the lessons.

Sections §45-§99 (added for v1.16) and §100-§139 (v1.17) are shorter concept entries: each
teaches one idea behind the platform, says where it lives in the code, and
ends with questions. If a v1.12-v1.17 term is unfamiliar, start there.

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

Three reading paths, depending on why you are here:

- *You do quant research.* §1, §5, §6, §23, §24, §32, §33, §40, §42, then
  §44. Run COOKBOOK recipes 1, 3, 4, 29 and 35 alongside. You will come
  out knowing why a t of 21 can be overstated, what a look costs, and why
  nothing here is promoted. Then §65-§87 (IC, HAC, multiple testing,
  pre-registration, holdouts, CPCV, leakage) and §78 on the R7 result.
  The v1.17 additions are §109-§115 (breadth, the registered N, the
  statistics revision), §130-§136 (clustered SEs, conformal sizing,
  meta-labelling, regimes, long horizons, `linear_z_v2`, ML looks).
- *You build trading systems.* §3, §9, §10, §12, §16, §21, §22, §25, §30,
  §36, then §44. Run recipes 2, 9, 10, 14, 22, 69 and 70. The theme is
  fail-closed design and proving the implementations agree. Then §45-§64
  (queues, costs, impact, execution schedules, maker and quoting economics)
  and §88-§99 (data, models, paper trading, controls, agents).
  For v1.17, §101-§108 (execution quality), §121-§127 (agents, spend,
  CI gates, budgets) and §128-§129, §137-§139 (data features, opt-in
  discipline, releases).
- *You are reviewing the platform.* §20, §27, §44, then
  [docs/ROADMAP.md](docs/ROADMAP.md) §3.8-3.10 and CHANGELOG.md. These say
  what is wrong or unfinished, in the platform's own words.

A note on Java. The sections below often mention a Java engine or the Java
paper-trading platform. Since v1.15 that engine is archived, unchanged, in
`archive/java/` (frozen at v1.14.0) and CI runs parity across Python, C++
and Rust only; where a section says "four languages" it describes the
platform as it was when the section was written.

The [glossary](#glossary) at the end defines the terms this document uses
without stopping to explain them.

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
29. [Combining weak signals, and why correlation gates matter](#29-combining-weak-signals-and-why-correlation-gates-matter)
30. [Adverse selection and markouts](#30-adverse-selection-and-markouts)
31. [Why taker-only research cannot pay at 1 s, and what maker economics changes](#31-why-taker-only-research-cannot-pay-at-1-s-and-what-maker-economics-changes)
32. [The FOMC / sampling flaw, and day-clustered inference](#32-the-fomc--sampling-flaw-and-day-clustered-inference)
33. [The 2026 holdout: pre-registration in practice](#33-the-2026-holdout-pre-registration-in-practice)
34. [What the synthetic maker and quoting results do and do not show](#34-what-the-synthetic-maker-and-quoting-results-do-and-do-not-show)
35. [Governance: why symmetric keys and class-only code hashes were not enough](#35-governance-why-symmetric-keys-and-class-only-code-hashes-were-not-enough)
36. [Parity proves agreement, not correctness: the snapshot-burst bug](#36-parity-proves-agreement-not-correctness-the-snapshot-burst-bug)
37. [Why an LLM must never compute a number](#37-why-an-llm-must-never-compute-a-number)
38. [Freezing copies instead of deleting them](#38-freezing-copies-instead-of-deleting-them)
39. [Model registries as evidence](#39-model-registries-as-evidence)
40. [Why a Sharpe must be deflated](#40-why-a-sharpe-must-be-deflated)
41. [What tick-to-trade does and does not measure](#41-what-tick-to-trade-does-and-does-not-measure)
42. [An underpowered result is not a negative result (AUC01)](#42-an-underpowered-result-is-not-a-negative-result-auc01)
43. [Feedback loops in passive execution](#43-feedback-loops-in-passive-execution)
44. [What an independent review found, and how we triaged it (v1.13/v1.14)](#44-what-an-independent-review-found-and-how-we-triaged-it-v113v114)
45. [Queue position: why the back of the queue fills last and worst](#45-queue-position-why-the-back-of-the-queue-fills-last-and-worst)
46. [Round-trip cost: why a 0.1 bp edge cannot pay a 1 bp spread](#46-round-trip-cost-why-a-01-bp-edge-cannot-pay-a-1-bp-spread)
47. [The maker's P&L identity: spread captured, markout, inventory](#47-the-makers-pl-identity-spread-captured-markout-inventory)
48. [Square-root impact](#48-square-root-impact)
49. [Parent vs child impact](#49-parent-vs-child-impact)
50. [Residual vs total impact](#50-residual-vs-total-impact)
51. [Permanent vs temporary impact, and trading against your own flow](#51-permanent-vs-temporary-impact-and-trading-against-your-own-flow)
52. [Almgren–Chriss: trading off impact against risk](#52-almgrenchriss-trading-off-impact-against-risk)
53. [TWAP, VWAP and implementation shortfall schedules](#53-twap-vwap-and-implementation-shortfall-schedules)
54. [POV participation maths](#54-pov-participation-maths)
55. [Maker vs taker economics](#55-maker-vs-taker-economics)
56. [Two-sided quoting and inventory skew](#56-two-sided-quoting-and-inventory-skew)
57. [When a skew is inert: ticks and the no-cross clamp](#57-when-a-skew-is-inert-ticks-and-the-no-cross-clamp)
58. [Fill probability as a hazard](#58-fill-probability-as-a-hazard)
59. [Post-only orders, reprices and give-up](#59-post-only-orders-reprices-and-give-up)
60. [Cost-aware routing: all-in price and fill probability](#60-cost-aware-routing-all-in-price-and-fill-probability)
61. [Latency legs, and stressing them](#61-latency-legs-and-stressing-them)
62. [Fee tiers and dated regulatory fees](#62-fee-tiers-and-dated-regulatory-fees)
63. [Capacity and break-even size](#63-capacity-and-break-even-size)
64. [End-of-day flatten](#64-end-of-day-flatten)
65. [The information coefficient: what an IC of 0.08 means](#65-the-information-coefficient-what-an-ic-of-008-means)
66. [HAC t: why overlapping labels need it](#66-hac-t-why-overlapping-labels-need-it)
67. [Few clusters: day-clustered t with G − 1 degrees of freedom](#67-few-clusters-day-clustered-t-with-g--1-degrees-of-freedom)
68. [Bonferroni and the ledger threshold](#68-bonferroni-and-the-ledger-threshold)
69. [Holm and Benjamini–Hochberg](#69-holm-and-benjaminihochberg)
70. [Romano–Wolf: using the dependence between tests](#70-romanowolf-using-the-dependence-between-tests)
71. [The experiment ledger as an append-only chain](#71-the-experiment-ledger-as-an-append-only-chain)
72. [Pre-registration: what it fixes, and when](#72-pre-registration-what-it-fixes-and-when)
73. [Holdout discipline: one look](#73-holdout-discipline-one-look)
74. [Code fingerprints](#74-code-fingerprints)
75. [Blackboards, fork boards and git anchors](#75-blackboards-fork-boards-and-git-anchors)
76. [Deviations, recorded before the look](#76-deviations-recorded-before-the-look)
77. [Power and the minimum detectable effect](#77-power-and-the-minimum-detectable-effect)
78. [Predictive but not tradable: the R7 result](#78-predictive-but-not-tradable-the-r7-result)
79. [Walk-forward vs combinatorial purged CV](#79-walk-forward-vs-combinatorial-purged-cv)
80. [Probability of backtest overfitting](#80-probability-of-backtest-overfitting)
81. [Leakage: four detectors and what each one catches](#81-leakage-four-detectors-and-what-each-one-catches)
82. [Fast causal or leak? The time-shift triage](#82-fast-causal-or-leak-the-time-shift-triage)
83. [Embargo by feature window](#83-embargo-by-feature-window)
84. [Refit or frozen: what a holdout test re-estimates](#84-refit-or-frozen-what-a-holdout-test-re-estimates)
85. [Hypothesis sign and beta stability](#85-hypothesis-sign-and-beta-stability)
86. [Scaling per instrument before pooling](#86-scaling-per-instrument-before-pooling)
87. [Causal row blocks: when a skipped row is a look-ahead](#87-causal-row-blocks-when-a-skipped-row-is-a-look-ahead)
88. [Same-timestamp coalescing](#88-same-timestamp-coalescing)
89. [Signing trades: the quote rule and the tick rule](#89-signing-trades-the-quote-rule-and-the-tick-rule)
90. [Feature drift between in-sample and holdout](#90-feature-drift-between-in-sample-and-holdout)
91. [Memory on pooled real data](#91-memory-on-pooled-real-data)
92. [Event clocks and Hawkes intensities](#92-event-clocks-and-hawkes-intensities)
93. [DeepLOB: learning from the raw book](#93-deeplob-learning-from-the-raw-book)
94. [Per-session windows and mid-relative normalisation](#94-per-session-windows-and-mid-relative-normalisation)
95. [Conformal intervals and confidence-scaled sizing](#95-conformal-intervals-and-confidence-scaled-sizing)
96. [Model cards and content hashes at promotion](#96-model-cards-and-content-hashes-at-promotion)
97. [Paper trading: what the PAPER gates check](#97-paper-trading-what-the-paper-gates-check)
98. [Pre-trade controls and a tamper-evident audit](#98-pre-trade-controls-and-a-tamper-evident-audit)
99. [Agents that propose and humans that decide](#99-agents-that-propose-and-humans-that-decide)
100. [A tree model on the same bench, and two gates that read one number](#100-a-tree-model-on-the-same-bench-and-two-gates-that-read-one-number)
101. [Implementation shortfall, and the benchmark that measures it](#101-implementation-shortfall-and-the-benchmark-that-measures-it)
102. [Fill types, and a markout that knows how the fill happened](#102-fill-types-and-a-markout-that-knows-how-the-fill-happened)
103. [Queue-aware cancels: when waiting is the expensive choice](#103-queue-aware-cancels-when-waiting-is-the-expensive-choice)
104. [Quote fading, and pricing the fades it saved against the fills it missed](#104-quote-fading-and-pricing-the-fades-it-saved-against-the-fills-it-missed)
105. [Signal-aware urgency: let the alpha choose passive or aggressive](#105-signal-aware-urgency-let-the-alpha-choose-passive-or-aggressive)
106. [Pacing, and Almgren-Chriss with a drift](#106-pacing-and-almgren-chriss-with-a-drift)
107. [The depth cap versus the book](#107-the-depth-cap-versus-the-book)
108. [A dark midpoint, as an idea only](#108-a-dark-midpoint-as-an-idea-only)
109. [Breadth and the fundamental law](#109-breadth-and-the-fundamental-law)
110. [The cross-sectional book](#110-the-cross-sectional-book)
111. [Break-even notional: how big can the book be?](#111-break-even-notional-how-big-can-the-book-be)
112. [A universe rule, written before any statistic](#112-a-universe-rule-written-before-any-statistic)
113. [A Sharpe deflated at the number of trials you registered](#113-a-sharpe-deflated-at-the-number-of-trials-you-registered)
114. [The statistics revision](#114-the-statistics-revision)
115. [One gate statistic](#115-one-gate-statistic)
116. [Why TOPS equals the top of DEEP: an oracle for the book](#116-why-tops-equals-the-top-of-deep-an-oracle-for-the-book)
117. [Capture clocks: pcapng and TAI](#117-capture-clocks-pcapng-and-tai)
118. [Process-and-delete: the data lifecycle on a small disk](#118-process-and-delete-the-data-lifecycle-on-a-small-disk)
119. [The approval rule for downloads](#119-the-approval-rule-for-downloads)
120. [Spend governance: a budget the model cannot raise](#120-spend-governance-a-budget-the-model-cannot-raise)
121. [A number must come from a tool result, not from the model](#121-a-number-must-come-from-a-tool-result-not-from-the-model)
122. [The three v1.17 agents, and what each is allowed to write](#122-the-three-v117-agents-and-what-each-is-allowed-to-write)
123. [Documentation checks as a CI gate](#123-documentation-checks-as-a-ci-gate)
124. [Reproducible builds and SBOMs](#124-reproducible-builds-and-sboms)
125. [Ratchets: coverage floors and a growing mypy scope](#125-ratchets-coverage-floors-and-a-growing-mypy-scope)
126. [Latency budgets: a limit is a measurement times a margin](#126-latency-budgets-a-limit-is-a-measurement-times-a-margin)
127. [A real slice committed to the repository](#127-a-real-slice-committed-to-the-repository)
128. [Share-normalised features and the per-session ADV](#128-share-normalised-features-and-the-per-session-adv)
129. [TRADE_BUST: a correction that must not move registered vectors](#129-trade_bust-a-correction-that-must-not-move-registered-vectors)
130. [Day-clustered standard errors, in the v1.17 gate form](#130-day-clustered-standard-errors-in-the-v117-gate-form)
131. [Conformal sizing: width as confidence](#131-conformal-sizing-width-as-confidence)
132. [Meta-labelling: choose the threshold on rows nobody fitted](#132-meta-labelling-choose-the-threshold-on-rows-nobody-fitted)
133. [Regime conditioning: one beta, or one per regime?](#133-regime-conditioning-one-beta-or-one-per-regime)
134. [Long horizons: features and labels, truncated at the close](#134-long-horizons-features-and-labels-truncated-at-the-close)
135. [linear_z_v2: standardise each instrument against itself](#135-linear_z_v2-standardise-each-instrument-against-itself)
136. [ML looks are looks](#136-ml-looks-are-looks)
137. [Opt-in, and byte-identical when off](#137-opt-in-and-byte-identical-when-off)
138. [One entry point for realism, and a label for what a cost is](#138-one-entry-point-for-realism-and-a-label-for-what-a-cost-is)
139. [Release, regenerate, and retiring a language](#139-release-regenerate-and-retiring-a-language)
140. [Further reading](#140-further-reading)
- [Glossary](#glossary)

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

Result on the bundled data (v1.14.0): **0 PROMOTE / 12 ITERATE / 12
REJECT** (0 / 11 / 13 under v1.5.0 to v1.13.0). The
cost gate fails for all 24, in two different ways: **18 alphas make no
trade at all** on the last fold — their forecast never exceeds the
round-trip spread and fee, so their net P&L is exactly 0, and 0 does not
pass `net P&L > 0` — and **6 trade and lose** (EQ11 −662, FX05 −160, FX08
−425, FX09 −471, FX10 −93, FX11 −1,853 USD; under v1.5.0 −502, −64, −328,
−255, −22, −733: the causal row block trades rows the old mask skipped). None ends above zero. The
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
(ledger `x-version` 3). At v1.5.0 the ledger held 216 distinct
configurations and a look count of 5,156, broken down below. As of v1.17.0 it holds
**422 distinct configurations / 5,732 looks** (`research/experiments.json`). v1.14.0 regenerated every statistic under its revised methods, and each one is a new look (`stats_revision`), adding 202 to the count over 77 configurations (the
breakdown below is the v1.5.0 one, and every entry in it is still there):

- on the v1.3.0 dataset (`203c8f54…`), 70 entries carrying 1,068 of the
  looks: a one-time design scan (216: 24 alphas × 9 horizons), 24
  promotion pipelines at 28 looks each (672), 40 adaptive deployments (10
  alphas × 4 refit policies) and five `ExperimentRunner` runs at 28 each
  (140; §6.8);
- on the v1.4.0 dataset (`116b7787…`), 146 entries carrying the other
  4,088. Under the legacy rules, as recorded at v1.4.0 (852): the 24
  pipelines at 28 looks (672), five runner experiments (140) and 40
  adaptive deployments (40). Under the default rules, added by v1.5.0
  (3,236): the 24 pipelines at **84** looks each (2,016), five runner
  experiments at 84 (420), the 40 adaptive deployments again (40) and the
  eight signal-combination experiments at 95 looks each (760; §29). The
  design scan was not repeated.

A validation costs 84 looks under the default methods where it cost 28
under the legacy ones, because the default validation computes more — 83
evaluations at four folds (itemised in
`iap.validation.validate.looks_per_validation`) plus the day-2 backtest —
and every one of them is a look.

That translates into a selection yardstick: Bonferroni per-test threshold
|t| ≥ **4.447**, and an expected **max |t| ≈ 4.160 under the global null**
as of v1.17.0, source `research/experiments.json` (4.432 and 4.144 at v1.14.0, 4.424 and 4.135 at v1.5.0's count of 5,156, 4.206 and 3.888 at the v1.4.0 count of 1920, 4.071 and 3.735 before the
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
runs that followed brought the ledger to 4,396, and the combination
experiments of §29 to 5,156, and the v1.14.0 regeneration further; it stands at 5,732 as of v1.17.0. The
threshold is not
retroactive — later looks tighten later runs and do not re-judge a
recorded one.

Meaning: FX08's gate t = 3.84 — or FX11's 2.22 — is *consistent with
pure selection* over this many trials, and the report says so in print:
17 of the 24 alphas sit below the yardstick. EQ12 (7.20), EQ02 (7.17) and
EQ03 (5.16) clear the threshold; EQ06 (4.36) and FX04 (4.24) are above the
yardstick and below the threshold, so they fail the significance gate
that, at a fixed 3.0, they used to pass; EQ08 (−4.57) and FX09 (−5.75)
are beyond it with the wrong sign. The lists are the same at 3,936 and at
5,156 (the v1.5.0 report; the v1.14 report's t values differ, see
docs/RESEARCH_VALIDITY.md §4b). Most quant shops track this informally at best;
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
- **One command proves parity**: `tests/harness/run_all.sh` runs all three
  suites and prints the table (the v1.14.0 counts from CI, 2026-10-11: python 2411,
  cpp 302, rust 376 tests passed; golden groups 193/72/75; all
  PASS, plus `integration` (35) and `replay` (6) rows for the repo-level
  pytest suites, a `deployment` row — 29 structural checks passed in CI,
  where promtool and kubeconform are installed — and a `numbers` row that re-derives every headline
  figure in the docs from its artefact). The Java golden group runs all
  seventeen `*GoldenTest` classes (it once ran two of them and reported 18),
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
→ ACTIVE ⇄ WATCH → RETIRED, seven states, seventeen edges, twenty gates —
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
"failed gates" column, for every one of them, `net_pnl_after_costs`,
`net_pnl_bootstrap_ci` and `capacity` (§29.7). That is
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

`tests/golden/expected_lifecycle.json` (`x-version` 3) scripts
six lives under the default policy and one under the legacy policy,
compared exactly, step by step, in Python, Java and Rust. LC01 is the
whole ladder:
RESEARCH → CANDIDATE on a ledger entry and a clean leakage test; →
VALIDATING on the eleven gates of that edge (a t of 4.0 against a ledger
threshold of 3.5, a bootstrap interval above zero, no correlated peer); → PAPER on a reproducible replay and
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
stream; the MVP golden pins a whole session's digest (`a7f1ee60…`, 800
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
# mvp run 58a10f2194a3c81c: events=15805 decisions=800 parents=231 children=347 fills=163 pnl=-82.837333 USD digest=a7f1ee6066f6eba9...
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
identical to v1.4.0. What moved is the digest (`f51890da…` → `2affe409…`)
and the `config_version` it covers (`f293e7e7…` → `439bbad5…`), because
`config_version` hashes `configs/execution/execution.json`, which now
names its impact model, and `alpha_params.json`, whose header names the
regeneration commit. A digest that changes when a hashed configuration
document changes, with every count and every dollar the same, is the
digest doing its job. In v1.14.0 the digest moved again (`a7f1ee60…`) and
this time a few counts moved too, because the refitted `alpha_params.json`
changes the expected returns the loop trades on: 231 parents and 163 fills
(was 235 and 169), −82.84 USD (was −81.53).

### 20.2 The numbers, stated as they are

Seed 12345 (v1.14.0): 15,805 events, 800 decisions, 231 parent orders, 496
children generated of which 347 submitted (149 blocked by the 500 ms
slice-interval control, exactly as the Java loop would), 163 fills, 15.4 %
fill rate. Risk: 347 ALLOW, 0 REJECT, 0 KILL, 8 sequence gaps each
recovered by the following SNAPSHOT burst, 28 mark regressions dropped.
Routing (synthetic venues XV1-XV3; superseded as a routing claim by the v1.17 B8
real two-venue evaluation, pending data): XV3 69.2 %, XV1 19.6 %, XV2 11.2 % of
filled quantity (aggressive
routing picks the cheapest taker fee on price ties; passive routing
prefers XV1's rebate). TCA: implementation shortfall +0.065 bps
quantity-weighted; TWAP fills 0.4 % (passive limits at a 1 s horizon
mostly expire), POV 6.7 %, IS 77.8 %. **P&L −82.84 USD** on 8,295 shares:
+0.0245 bps of alpha contribution against −0.4367 bps of execution cost,
net −0.41 bps of filled notional. The report field is
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
| EQ01 | +0.217 | −0.024 | +0.149 | 790 | 1 s: +0.217 | 0.024 | 0.193 |
| EQ03 | +0.109 | −0.029 | +0.116 | 707 | 5 s: +0.147 | 0.030 | 0.117 |
| EQ06 | −0.079 | +0.079 | −0.047 | 354 | 10 s: −0.130 | 0.056 | 0.185 |
| ensemble | +0.103 | −0.002 | +0.111 | 790 | — | — | — |

(v1.14.0 numbers. The research IC is the gate IC of the v1.14 report —
0.0237, 0.0300 and 0.0556; under v1.5.0 it was 0.0105, 0.0189 and 0.0277,
with gaps 0.206 / 0.128 / 0.158. The realized columns barely moved.)
EQ01's realized IC is about nine times its research IC; EQ06's has the
wrong sign. The pitfalls of §27 say what to do with numbers like that. The audit
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
   is smaller than the spread; that is the −82.84 USD.
6. **Let the lifecycle see it.** `paper_evidence.json` carries the realized
   IC at the fitted horizon, so the `paper_ic_tracking` gate (max gap 0.01)
   fails all three alphas on this data (gaps 0.193 / 0.117 / 0.185 at v1.14.0; on the
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
both effects from the middle of the sample. Each cell is run on twenty
generator seeds through the real feature pipeline and `validate_alpha`.

Two things about the design matter for reading the result.

- **Sessions.** A run is generated once, at the largest session count, and
  scored on its first 1, 2, 4 (and, in the extended grid, 8) sessions. That
  is exact: without a break, session *k* does not depend on what follows it,
  so the first *n* sessions of a run are the *n*-session run of the same
  seed (a test compares the bytes). The bundled calendar has five trading
  days; the study writes its own reference tree with as many weekdays as it
  needs and leaves `configs/` alone.
- **Detectors.** Each effect is scored by its alpha at the alpha's declared
  label horizon and at the *matched* horizon — the shortest label that
  covers the planted mechanism (order flow: when half the impact kernel has
  arrived, 10 s; lead-lag: the lag plus the one-second signal window, 5 s).
  The matched horizon is a rule on the planted config, not a choice made
  after seeing rates, and every detector, session count and break test is
  counted in the study's own Bonferroni denominator.

### 23.2 What came back

A detection is a positive gate IC and a pooled-slope t at or above the
PROMOTE threshold in force — 4.365, the threshold the committed promotion
reports were judged at (a gate look count of 3,936). The report also gives every
rate at the fixed 3.0 and at the study's own threshold. Reference size,
stable scenario; each cell is detections of 20 runs with its Wilson 95 %
interval (`research/power/POWER_REPORT.md` up to 4 sessions,
`research/power/extended/POWER_REPORT.md` for 8):

| effect | detector | 2 sessions | 4 sessions | 8 sessions | mean t at 8 |
|---|---|---:|---:|---:|---:|
| order flow | EQ04 @ 5 s (declared) | 2 [0.03, 0.30] | 10 [0.30, 0.70] | 20 [0.84, 1.00] | +6.75 |
| order flow | EQ04 @ 10 s (matched) | 11 [0.34, 0.74] | 20 [0.84, 1.00] | 20 [0.84, 1.00] | +9.04 |
| lead-lag | EQ10 @ 1 s (declared) | 0 [0.00, 0.16] | 0 [0.00, 0.16] | 0 [0.00, 0.16] | +2.27 |
| lead-lag | EQ10 @ 5 s (matched) | 1 [0.01, 0.24] | 6 [0.15, 0.52] | 16 [0.58, 0.92] | +5.03 |
| null (level 0) | each of the four | 0 [0.00, 0.16] | 0 [0.00, 0.16] | 0 [0.00, 0.16] | −1.51 to −0.26 |

Read it row by row.

- **Order flow is detected, given sessions.** On eight sessions every run
  detects the reference effect at the declared horizon. On four, half do; on
  the two sessions the bundled dataset has, one in ten. The previous report
  said "one seed of three" for this cell at t ≥ 3 — with twenty seeds the
  same cell is 12 of 20 at 3.0 and 2 of 20 at the threshold the gate
  actually applies.
- **The t grows as the square root of the sessions**, as it should: +2.76,
  +3.28, +4.54, +6.75 on 1, 2, 4 and 8. The report fits
  `t ~ N((κ₀ + κ·level)·√sessions, sd)` and reads off the *minimum
  detectable effect* — the size detected 80 % of the time: on eight sessions
  0.84 of the reference at 5 s and 0.66 at 10 s — and the sessions the
  reference needs for 80 %: about five at 4.365, about three at 3.0. The
  ledger threshold costs sessions, not detections.
- **Lead-lag is invisible at the declared horizon.** The planted lead is two
  one-second steps and EQ10's label is one second: the label ends before the
  follower has moved. On the efficient price the IC at that horizon is
  exactly zero; the +0.009 the chain measures comes from quotes going stale.
  No run of 20 detects it at any session count, and the model puts 80 %
  power near 50 sessions. At a 5 s label the same signal is detected in 16
  of 20 runs on eight sessions. A detector's horizon has to match the
  mechanism; the alpha as declared is the wrong instrument for this effect.
- **Where the rest of the power goes.** The report's diagnosis table sets
  the IC the mechanism has on the efficient price beside the measured one
  and removes one idealisation at a time. For order flow on four sessions:
  expected t 38.0 if every row carried a signal and rows were independent;
  25.5 because only 45 % of rows have a trade in the last ten seconds; 7.29
  because the observed mid sits on a tick grid (78 % of 5 s labels are
  exactly zero) and reads 0.037 where the efficient price would give 0.128;
  5.12 because overlapping rows make two rows one observation; 4.58 because
  walk-forward scores four fifths of the sample. Measured: 4.54. Invalid
  labels are not on the list — 99.8 % of 5 s labels are scored.
- **The null rows are clean, and now that says something.** No detector is
  flagged with nothing planted, at 3.0 or at 4.365, at any session count:
  the false-positive rate is below 16 % with 95 % confidence, per cell. At
  most 2 of 20 null runs reach ITERATE.
- **The break rows behave, and the break is detectable.** An effect that
  reverses mid-sample is reported as evidence in no run. The break z — the
  HAC z of the slope before against the slope after mid-sample — flags it in
  20 of 20 runs for order flow from four sessions on, and in 15 of 20 for
  lead-lag at 5 s on eight; on stable runs it fires at 3.0 at most once in
  twenty.
- **Nothing is promoted, at any size or session count.** Not even twice the
  reference on eight sessions, mean t 18.5. No fold survives 1× costs in any
  cell and no bootstrap interval for net P&L lies above zero. At the
  reference size the cost-aware backtest does not trade the 5 s forecast at
  all; at the 10 s label it trades and no fold survives its costs.

That last row is the one that changes how to read the headline. PROMOTE
requires positive net P&L after costs under the research execution model,
and in this generator's cost structure even a large, real, stable, planted
effect does not clear it. So "0 PROMOTE on the bundled data" is not
evidence that the chain is a strict judge of alpha. The significance half
of the chain is informative — and measurably so, now. The promotion half
has not been shown to be reachable.

And the first row changes how to read a REJECT. On two sessions the chain
misses a real effect of the reference size nine times in ten. A null on the
bundled dataset is weak evidence of absence.

The study has been run four times: on the v1.3.0 generator, after the
v1.4.0 flow recalibration (the same planted strength gave about half the
IC on the sparser flow), under the v1.5.0 default methods (report version
2; three seeds, two sessions), and in the form above (report version 3).
The planted effect sizes have not changed since v1.3.0.

### 23.3 What the corrected statistics change

The gate statistic is the **pooled-slope HAC t**, which tests the pooled
IC directly and keeps signal that lives between buckets; the within-bucket
Newey–West t, the legacy statistic, is still recorded per run. Beside the
chain the study computes one more: the pooled slope of the label on the
alpha's raw signal over every scored row, with **session-clustered** HAC
errors — the Bartlett cross-products stop at the session boundary, because
the last bucket of one day and the first of the next are not neighbours.
It needs no walk-forward split, so it sees five fifths of the sample where
the chain sees four; its t is the `t direct` column of the diagnosis, and
the chain's t sits where √(4/5) of it should (4.58 against 4.54). It
explains the chain's number; it is not a second chance to detect.

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

Twenty seeds per cell bound a rate to within about ±0.2; they do not
measure a false-positive rate of one in a thousand. The session prefixes of
one run share data, so a detector's rows at different session counts are
not independent of each other. Session counts beyond eight are the fitted
√-law, not measurements. The planted effects are the generator's own
construction, detected by the two alphas written for exactly those effects;
power against an effect of a different shape is unknown. The reference
sizes are stated as what they are — an IC of 0.13 (R² 1.6 %) at 5 s for
order flow and 0.17 (R² 2.8 %) for lead-lag on the efficient price, 0.037
and 0.019 on the observed mid — and no claim is made that real markets
carry effects of that size. And it is synthetic end to end. A power
statement about real markets needs real data (EPICS E25).

**Check yourself.**

*Q. The order-flow detector reports a mean IC of −0.007 with t −0.89 on the
null (four sessions), and the t falls further with more sessions. Is that a
problem?* It is a reminder that "nothing planted" is not
"nothing there": the generator's microstructure produces small correlations
of its own (§2.3). A detection needs a positive IC, so the row produces none; a
level-0 row that *was* detected would be the problem. What it does cost is
power: the planted effect has to overcome that negative offset first, which
is why the fitted model of §23.2 has an intercept.

*Q. Why were three seeds not enough?* One detection in three has a 95 %
interval of 0.06 to 0.79: it is compatible with a chain that almost never
detects and with one that usually does. Twenty seeds give 16 of 20 an
interval of 0.58 to 0.92, which excludes one in three. The first report was
worth committing — the alternative was no positive control at all — but its
rates could not carry a conclusion.

*Q. What single result here most limits the claims the rest of the
repository can make?* PROMOTE is zero in every cell. The chain has been
shown to detect; it has not been shown to promote.

**Exercise.** Run the tiny grid of COOKBOOK recipe 27 and compare its
`stable` level-1 rows with the committed twenty-seed report: which cells
agree, and which could not possibly agree with one seed? Then run the tests that pin
the planted generator:

```bash
cd python && PYTHONPATH=src python3 -m pytest -q tests/test_planted_signals.py
# 35 passed (about a minute)
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

**Exercise.** The Java tests (`PlatformSafetyTest`:
`checkpointHasOneCommitPointVerifiedOnResume`,
`stopRequestCheckpointsOnTheTradingThreadAndResumes`,
`killLatchesImmediatelyAndIsNeverDropped`) ran in CI until v1.15 archived
the Java engine to `archive/java/`; with a JDK they still run there,
`cd archive/java && bash build.sh && bash test.sh`. The Python half runs anywhere:

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
#   [ok  ] rust_toolchain_pinned: 1.99.0
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
   config, dataset) — 422 distinct configurations, 5,732 looks as of v1.17.0
   (`research/experiments.json`) — with a printed expected-max-|t| yardstick of 4.160; FX08's t = 3.84 is called what it is.
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
the cost-adjusted IC (−0.024) and the P&L (−82.84 USD): still no money. A
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

## 29. Combining weak signals, and why correlation gates matter

### 29.1 The idea, and its fine print

No single signal in this repository is strong. The best equity alpha has an
out-of-sample IC of 0.026. The textbook response is the fundamental law of
active management: `IR ≈ IC × sqrt(breadth)`. Small skill, applied to many
independent bets, adds up.

The word that carries the weight is *independent*. Take `K` standardised
signals, each with information coefficient `ic` against the same label, with
mean pairwise correlation `rho`. Their equal-weight average has variance
`(1 + (K − 1) rho) / K` and covariance `ic` with the label, so

    IC_blend = ic × sqrt( K / (1 + (K − 1) × rho) )

Work it by hand for twelve signals with `ic = 0.01`:

| `rho` | `K / (1 + (K − 1) rho)` | blend IC |
|---|---|---|
| 0.0 | 12.0 | 0.0346 |
| 0.1 | 5.7 | 0.0239 |
| 0.3 | 2.8 | 0.0167 |
| 0.7 | 1.4 | 0.0117 |
| 1.0 | 1.0 | 0.0100 |

At a correlation of 0.3 — unremarkable for signals built from the same
order book — twelve signals are worth fewer than three. Breadth is not the
number of signals you have. It is the number of independent things they
say.

### 29.2 Measure the breadth you have

`iap.combine.effective_bets` turns a correlation matrix into one number,
the participation ratio of its eigenvalues: `N_eff = (Σλ)² / Σλ²`. Twelve
uncorrelated signals give 12; twelve copies give 1. On the bundled data
(`research/combination/REPORT.md`):

- the twelve equity alphas hold **6.5** effective bets. The reason is one
  cluster: EQ02, EQ03 and EQ12 are three measurements of order-flow
  imbalance, correlated 0.95 to 1.00;
- the twelve FX alphas hold **9.9**: no pair is above 0.7.

The arithmetic then says what an equal-weight blend should achieve, and
the measurement agrees with it:

| | mean member IC | breadth `K/(1+(K−1)rho)` | expected blend IC | measured |
|---|---|---|---|---|
| equities | 0.0038 | 6.8 | 0.0099 | 0.0105 |
| FX | 0.0109 | 10.1 | 0.0348 | 0.0315 |

### 29.3 Fitting weights without fooling yourself

Equal weights estimate nothing, which is their virtue. Anything smarter —
weighting by IC, ridge regression, a mean-variance blend — estimates `K`
numbers from the same short sample the result will be judged on, and the
way this goes wrong is always the same: the weights see the rows they are
then scored on.

`CombinedAlpha` is built so that they cannot (PLATFORM_CONVENTIONS.md
§13.8). In each walk-forward fold it is handed the training window only.
Inside that window it runs a second, inner walk-forward: each member is
fitted on the early part and predicts the later part, and those
out-of-sample predictions — the *stack* — are what the weights are fitted
on. The ridge penalty is chosen by cross-validation inside the stack. The
standardisation of each member uses the stack's mean and scale. Then the
whole thing is applied, frozen, to the test rows.

The tests do not take this on trust. For every fold and every method,
`test_fold_parameters_ignore_everything_from_the_test_start_on` replaces
every label from the test start onwards with garbage (or shifts them by a
row, or multiplies every later feature by 1 000) and requires the fitted
parameters to be identical to the last bit. A second test fits a combiner
the wrong way — on train and test together — and checks that the same
corruption *does* move it: a leakage test that cannot fail is not a test.

One real bug was caught this way, by the platform's own truncation probe
(`iap.validation.leakage`). The blend was first computed as a matrix product. A BLAS routine may
round the dot product of row 500 differently depending on whether the
matrix has 501 rows or 5 000, so the score of a row depended, in the last
bit, on rows after it. No information leaked; the probe failed anyway, and
it was right to: "bit-identical when the future is removed" is the
property, and "the difference is tiny" is how real look-ahead gets waved
through. The blend is now accumulated member by member.

### 29.4 Combining is a search

Four methods on one member list is four tries at a significant result, and
each is charged: 83 looks for its validation chain plus one per member,
declared to the ledger before the first is evaluated (§24). The committed
report was charged 760 of them and every combination in it — the default included —
was judged at a t threshold of 4.42, not 3.0. Selecting members by their
past verdicts would be a further, hidden search (survivorship in member
selection), which is why the default member list is all twelve alphas of
an asset class, the rejected ones too.

### 29.5 The honest result

| combination | gate IC | gate t | trades, four folds | net P&L 1× | verdict |
|---|---|---|---|---|---|
| equities, equal weight | 0.0105 | 1.98 | 0 | 0 | ITERATE |
| equities, IC-weighted | 0.0262 | 6.29 | 3 | −44 | ITERATE |
| equities, ridge | 0.0429 | 7.06 | 6 | −73 | ITERATE |
| equities, shrinkage MV | 0.0413 | 6.38 | 6 | −73 | ITERATE |
| FX, equal weight | 0.0315 | 3.22 | 15 | −13 | ITERATE |
| FX, IC-weighted | 0.0428 | 3.74 | 2 713 | −929 | ITERATE |
| FX, ridge | 0.0482 | 4.73 | 4 444 | −1 627 | ITERATE |
| FX, shrinkage MV | 0.0478 | 4.76 | 4 419 | −1 631 | ITERATE |

Nothing is promotable. Read the columns in order and the reason is plain.
The combination works *statistically*: the fitted equity blends have a t of
6 to 7, well over the 4.42 they needed. It does not work *economically*:
an IC of 0.04 on a five-second label is a forecast of a fraction of a tick,
the cost-aware backtest enters only when the forecast exceeds the spread
and fee, and on equities that happens six times in four folds. Breadth
raised the correlation between forecast and outcome. It did nothing for the
size of the outcome relative to the cost of trading it. A PROMOTE here
would have been the surprise, and the first thing to check would have been
the leakage tests of §29.3.

### 29.6 Why a correlation gate

Suppose the costs were lower and EQ02, EQ03 and EQ12 each cleared every
gate. A lifecycle that judges alphas one at a time would promote all
three and the book would hold three allocations to one idea — three times
the position, one bet's worth of diversification. That is the failure a
correlation gate exists for: each alpha is fine on its own evidence, and
the *set* is wrong.

`cross_alpha_correlation` (docs/LIFECYCLE.md §3) sits on CANDIDATE →
VALIDATING: the candidate's largest absolute signal correlation with any
alpha already at VALIDATING or beyond must be at most 0.7. Four details
are worth knowing because each closes a loophole:

- **Absolute value.** An alpha correlated −0.9 with an allocated one is its
  mirror image; it adds no information either.
- **Vacuous pass, stated.** With nobody to compare against, the value is 0.0
  and the gate passes. Today that is all 24 alphas. The gate has decided
  nothing yet, and the registry says so by the value.
- **Fail closed.** If nobody measured the correlations, the evidence block
  is absent and the gate fails. An empty list of peers ("there is no other
  alpha") and a missing block ("nobody looked") are different statements.
- **Order is part of the rule.** The gate compares a candidate with what is
  already through, so whoever goes first wins. The bootstrap goes in
  ascending id: EQ02 would pass, EQ03 and EQ12 would be held.

The signal is gated rather than the P&L because most alphas here never
trade — a P&L correlation would be undefined exactly where the gate is
needed. Where P&L exists it is reported (FX08 and FX09: 0.41).

### 29.7 The bootstrap gate, in the same change

The same release turned a reported number into a gate. Up to then the
lifecycle asked "is net P&L after costs positive on the last fold?" — a
point estimate on a fifth of the data. `net_pnl_bootstrap_ci` asks for the
lower end of a 95 % stationary-bootstrap interval around the pooled P&L of
all four folds to be above zero (COOKBOOK §31 runs the bootstrap). Two rules in it
matter more than the level:

- an alpha that makes **no trade** has a P&L of exactly zero in every
  resample, an interval of [0, 0], and no evidence. The gate fails it and
  says why. Seventeen of the 24 alphas are in that position;
- the comparison is **strict**: a lower bound of exactly zero fails.

All 24 alphas fail it today and no verdict moved, because all 24 already
failed the point-estimate gate. Its job is the case that has not happened
yet: an alpha whose last fold was lucky.

### 29.8 Exercises

1. In `python/tests/test_combine.py`, `make_frames` makes feature `f3` a
   noisy copy of `f1`. Predict the effective number of bets of the four toy
   members, then check it against `test_member_pass_on_toy_members_...`.
2. Change `_garble_from(..., "labels")` to corrupt labels from one row
   *before* the test start. Which methods' parameters move, and why not
   `equal_weight`'s weights?
3. Using the formula of §29.1: how many independent signals with IC 0.004
   would it take to reach the equity blend's measured 0.0105? How many at
   `rho = 0.07`?
4. EQ08 has a gate IC of −0.040 at the combination horizon — a strong
   signal with the wrong sign. `ridge` gives it a negative weight;
   `ic_weighted` gives it zero. Argue for each.

## 30. Adverse selection and markouts

**The question.** Chapter 22 made the simulator stop inventing fills. This
chapter is about the fills it does give you, and one number that tells you
whether a fill was worth having: the *markout*.

**Why crossing is expensive and posting is not free.** A MARKET buy pays the
ask: half a spread above the mid, plus the taker fee. A LIMIT buy resting at
the bid gets half a spread *below* the mid, plus a rebate — if it fills. But
a resting order does not choose its counterparty. It fills when somebody
decides to sell at the bid, and the sellers who are most eager to hit a bid
are the ones who expect the price to fall. So the fills a passive order
collects are a biased sample of moments: more of the ones where the price
was about to move against it. That bias is **adverse selection**, and it is
why "earn the spread instead of paying it" is not a free lunch.

**The markout.** For a fill at price `p` and time `t`, on side `s` (+1 buy,
−1 sell), the markout at horizon `h` is

```
markout(h) = s × (mid(t + h) − p)
```

Positive means the fill looks good `h` later; negative means you would
rather not have traded. Three more quantities, all per unit:

```
effective half-spread   = s × (p − mid_at_fill)        what you paid against the mid you traded on
realised half-spread(h) = s × (p − mid(t + h))         = −markout(h)
price impact(h)         = s × (mid(t + h) − mid_at_fill)

effective half-spread = realised half-spread(h) + price impact(h)      (exactly)
```

For a taker the effective half-spread is positive (it paid). For a maker it
is negative (it earned), and its price impact is negative when the mid moved
against it: `adverse selection(h) = −price impact(h)`. What a passive fill
*keeps* is the half-spread it earned minus the adverse selection — which is
its markout.

**Three details that decide whether the number is honest.**

1. *Which mid is "at fill"?* A passive fill is caused by the event stamped
   with its timestamp, and the book after that event already shows the
   trade that hit you. The platform uses the state strictly *before* a
   MAKER fill and the state *at* a TAKER fill (API_PORTFOLIO_TCA.md §2.4).
   Use the post-event mid for a maker and the adverse move disappears into
   the reference price.
2. *Which mid is "at t + h"?* The last one at or before `t + h`. No
   interpolation, no peeking at the next quote.
3. *What if there is no such mid?* Then there is no markout. A fill ten
   seconds before the close has no five-minute markout; a window that
   contains a halt, an auction or a stretch with a one-sided book has none
   either. The platform reports `null` and counts the fills that were
   defined. Reporting zero instead would pull every late-session average
   toward "nothing happened".

**Run it.** One parent order, three policies, on the golden equity vector:

```bash
PYTHONPATH=python/src python3 - <<'EOF'
from dataclasses import replace
from pathlib import Path
from iap.core.codec import read_jsonl
from iap.execution import (AlgoType, ExecConfig, ExecPolicy, ExecutionReplay,
                           InstrumentSpec, Liquidity, ParentOrder, load_venues)
from iap.tca.markout import MarkoutFill, build_gated_timeline, markout_report

SEC = 1_000_000_000
events = read_jsonl(Path("tests/golden/events_eq_mbo.jsonl"))
t0 = events[0].exchange_ts
cfg = ExecConfig(seed=20260829, venues=load_venues("configs/venues/venues.json"),
                 instruments={1: InstrumentSpec(1, 0.01, 1.0, 38_000_000.0)})
parent = ParentOrder(parent_id=1, instrument_id=1, venue_id=1, side=0, qty=1200,
                     algo=AlgoType.TWAP, start_ts=t0 + 60 * SEC, end_ts=t0 + 660 * SEC,
                     slices=12)
timeline = build_gated_timeline(events, 1, 0.01)
for policy in (ExecPolicy.AGGRESSIVE, ExecPolicy.NATIVE, ExecPolicy.PASSIVE):
    res = ExecutionReplay(cfg, [replace(parent, policy=policy, urgency=0.5)]).run(events)
    r = res.parents[1]
    fills = [MarkoutFill(f.ts, f.price_ticks * 0.01, f.qty, f.side,
                         "MAKER" if f.liquidity == Liquidity.MAKER else "TAKER",
                         f.venue_id, "TWAP") for f in res.fills]
    rep = markout_report(fills, timeline, min_fills=3)
    print(f"{policy.name:10s} filled {r.filled_qty:4d}/{parent.qty} avg {r.avg_price:.4f} "
          f"fees {r.fees:.3f} rebates {r.rebates:.3f}")
    for liq, cell in rep["by_liquidity"].items():
        row = cell["horizons"]
        print(f"   {liq} n={cell['n_fills']:2d} markout bps:",
              {h: None if row[h]["markout_bps"] is None else round(row[h]["markout_bps"], 2)
               for h in ("1s", "30s", "5min")})
EOF
# AGGRESSIVE filled 1200/1200 avg 24.5242 fees 3.600 rebates 0.000
#    TAKER n=12 markout bps: {'1s': -5.27, '30s': -5.27, '5min': -2.89}
# NATIVE     filled  600/1200 avg 24.5033 fees 0.000 rebates 1.200
#    MAKER n= 6 markout bps: {'1s': -0.34, '30s': 1.02, '5min': 7.14}
# PASSIVE    filled 1100/1200 avg 24.5191 fees 2.100 rebates 0.800
#    MAKER n= 4 markout bps: {'1s': -1.02, '30s': -2.55, '5min': 4.08}
#    TAKER n= 7 markout bps: {'1s': -4.37, '30s': -4.66, '5min': -3.49}
```

Read it carefully. The aggressive order bought everything and every fill is
about five basis points under water a second later — that is the half-spread
of this instrument, paid twelve times. The native order bought at better
prices and collected rebates, but only **half** of what it was asked to buy;
the other 600 shares are not in its average price. The passive policy posted
first and crossed what did not fill: 1,100 of 1,200, a better average than
crossing everything. And the one-second markout of its passive fills is
*negative* (−1.02 bps): on these few fills, the mid had already moved
through the order by the time it was filled. Four fills are an anecdote, not
a measurement — which is why every cell carries its count and a standard
error, and why a cell with too few fills prints `null`.

**The trap: the order you did not fill.** Compare the three average prices
and the native order wins. It wins by not buying when the price was going
up. An execution report that stops at "average price of what filled" will
always prefer the policy that trades least. The study in
`research/execution/EXECUTION_REPORT.md` therefore charges the unfilled
quantity twice: the move of the mid from arrival to the end of the window
(Perold's opportunity cost), and the half-spread plus fee it would take to
finish the order there. Under that accounting, on 836 parent orders, the
passive policy costs less than crossing — and the native policy, cheapest
per filled share, ends up behind it once its unfilled third is paid for.

**How much of that would survive a real market?** Less than the table says,
and the report says why. In the replay our resting order does not exist for
anybody else: no one cancels in front of it, joins behind it, or backs away
when it improves the touch. The adverse selection measured there is whatever
the synthetic order flow happens to contain, not the selection a real
counterparty applies. The mechanics are real — a maker earns the half-spread
and the rebate, queue position at entry decides the fill rate, the cancel
can lose the race — and the magnitudes are the simulator's. The honest use
of this chapter's tool is the other direction: take real fills, compute the
same markout table, and see what the passive fills actually kept.

**Check yourself.**

1. A sell order rests at the ask of a 100.00 / 100.04 market and fills. One
   second later the market is 100.03 / 100.07. Effective half-spread,
   price impact, markout? *(−0.02, −0.03, −0.01: it earned two cents of
   half-spread, the mid then moved three cents against it — adverse
   selection — and the fill is one cent under water; the realised
   half-spread is +0.01.)*
2. Why does the markout of a fill made 20 s before the close have no 30 s
   value instead of the value of the last quote? *(The last quote is not the
   price 30 s later; carrying it forward reports "no move" for exactly the
   fills that have no evidence.)*
3. The native policy shows the lowest cost per filled share. What number do
   you ask for next? *(The fill rate, and the cost of the quantity it did not
   fill.)*

## 31. Why taker-only research cannot pay at 1 s, and what maker economics changes

**The question.** Seven real Nasdaq sessions (v1.6.0) and a two-day 2026
holdout (v1.8.0) both show statistically strong signals at 1-5 s: EQ01's
gate IC is +0.105 in sample and +0.037 out of time. None of them trades
profitably. Why can a signal be this significant and still lose money?

**The arithmetic.** An information coefficient is a correlation, not a
return. The return a signal forecasts is roughly `IC × σ(h)`, where `σ(h)`
is the standard deviation of the mid move over the horizon. For a large-cap
stock over one second, `σ(1 s)` is of the order of 1 bp, so even an IC of
0.07-0.1 forecasts a move of well under 0.1 bp on an average row. The
platform's real-data reports put the typical forecast at about **0.07 bp**.
A taker round trip pays the spread (one tick on a 150-300 USD stock is
0.3-0.7 bp), two taker fees (0.003 USD/share is about 0.1-0.2 bp per side)
and impact: about **0.7 bp** in total. The forecast is about a tenth of
the cost. A t-statistic of 20 says the forecast is reliably above zero; it
says nothing about whether it is above 0.7 bp. The cost-aware position
policy (v1.5.0) makes this visible: it trades only when the forecast
exceeds the round-trip cost, and on real data it almost never does.

**What a maker changes.** A passive order is paid the half-spread instead
of paying it, and collects a rebate instead of a fee. Done naively, that
turns −0.7 bp into roughly +0.7 bp per round trip. The catch is in chapter
30: the fills a resting order gets are selected against it. The relevant
comparison is no longer forecast versus spread; it is

```
|forecast| + half-spread earned + rebate − exit cost  >  adverse selection
```

and every term on the right is measured, not assumed. That is the v1.9
design (API_TRADING.md §2.6): calibrate fill probability, queue depletion
and adverse selection from the event stream (M1), post only when the gate
above clears (M2), label each decision with what a maker would actually
have got (M3: filled? run over?), and add conditions on the tail of the
score and the book (M4).

**What the synthetic numbers say.** On the golden synthetic vector
(COOKBOOK recipe 39) the gate with a taker exit admits nothing, because the
half-spread earned on entry is paid back on exit: the gate collapses to
`|er| + rebate − taker fee − impact > AS`, and a 1 bp toy score cannot clear
1.4 bp of measured adverse selection. With a passive exit the gate admits
nearly every row, but only 32% of exits fill passively, the rest time out
and cross, and adverse selection *on the trips actually taken* is +7.1 bp
against a calibrated +1.4 bp: the trips that last are the ones the market
ran over. Net: −6.7 bp per trip, against −11.4 bp for an ungated taker exit.
Better, and still a loss.

**The general lesson.** The calibrated average adverse selection is not the
adverse selection of the trades you take. Any gate that conditions on an
average will be wrong in the direction that hurts, because the trades that
survive to be measured are selected by the market. This is why the
maker study on real data (pre-registered, in-sample; it finished in v1.16 with no maker edge) reports
adverse selection per trip, not per calibration.

**Check yourself.**

1. A signal has IC 0.05 at 1 s on a stock with `σ(1 s)` = 1.2 bp. Spread is
   one tick on a 200 USD stock, taker fee 0.003 USD/share. Does a taker
   strategy pay? *(Forecast ≈ 0.06 bp; spread 0.5 bp plus 2 × 0.15 bp fees
   ≈ 0.8 bp. No, by an order of magnitude.)*
2. Why does a taker exit make the maker gate almost the same as a taker
   gate? *(The half-spread earned on entry is paid back crossing out; only
   the rebate-minus-fee difference and the adverse-selection term remain.)*
3. The calibration says maker adverse selection is 1.4 bp at 10 s. The
   backtest's trips show 7.1 bp. Which one is the bug? *(Neither. The
   calibration averages over all touch joins; the trips are the subset that
   filled and stayed open, which the market selected.)*

## 32. The FOMC / sampling flaw, and day-clustered inference

**The question.** The seven real sessions behind every v1.6-v1.8 number are
2019-01-30, 2019-03-27, 2019-07-30, 2019-08-30, 2019-10-30, 2019-12-30 and
2020-01-30. What is wrong with that sample, and what does a t of 21 on it
really mean?

**What went wrong.** Three things, each found in the v1.9 review:

- **Event days.** 2019-01-30 and 2019-10-30 are FOMC announcement days, and
  2019-12-30 is a thin holiday session. Two of seven days are the most
  unusual afternoons of their year. If a signal works mainly on those days,
  a pooled statistic says it works.
- **Unequal weight.** QQQ supplied about half the feature rows. A pooled IC
  is dominated by the busiest instrument; it answers "does it work on
  QQQ?" more than "does it work?".
- **The unit of independence.** Rows within a day share that day's news,
  volatility and liquidity. A HAC t-statistic corrects for autocorrelation
  between nearby rows, but it treats seven days of a million rows each as
  millions of nearly independent observations. A common shock that lasts
  the whole day is invisible to it. On seven days, the number of independent
  observations of "a day" is seven.

**What v1.9 adds (opt-in, the `v3` bundle).** Seeded, stratified day
sampling recorded in `dataset.json` so the draw can be audited (R1); an
event calendar and a report block with the gate statistics recomputed
without FOMC and holiday-thin days; folds cut at session starts, and a
leave-one-day-out layout (R2); a day-clustered t and a day-block bootstrap,
which resample whole days (R2); the gate IC as the equal-weight mean of
per-instrument ICs (R3); a HAC t that forms no lag product across a day
boundary (R6). COOKBOOK recipe 44 shows the block on a synthetic fixture.

**How to read the three t-statistics.** If the row-level HAC t is 20 and
the day-clustered t is 3, the signal is consistent within days but its
day-to-day variation is large relative to its mean; with seven days you
cannot rule out that two good days carry it. The day-clustered t has about
`days − 1` degrees of freedom, so its critical values are those of a
t-distribution with 6 degrees of freedom, not a normal. Neither number is
"the right one"; the day-clustered one answers the question a trader asks
("will tomorrow look like this?").

**What did not change.** The `v3` bundle is not the default. Every
published number, golden and report is still the `v2` rule, so nothing
moved when v1.9 shipped. Re-running the real-data batch under `v3` is a
v1.11 item (docs/ROADMAP.md §3.5); until then, the honest reading of the
2019-20 statistics is "strong at the row level, unproven at the day level".

**Check yourself.**

1. Why does a day-block bootstrap use whole days rather than rows? *(Rows
   within a day are dependent through the day's common shock; resampling
   rows treats them as independent and understates the variance.)*
2. A signal's gate IC drops from 0.08 to 0.02 when FOMC days are removed.
   What do you report? *(Both, with the ex-event number as the one to plan
   on; and the observation that the signal is concentrated on event days is
   itself a finding.)*

## 33. The 2026 holdout: pre-registration in practice

**The question.** After v1.7 every alpha had been designed, fitted and
selected on the same seven sessions. How do you test it on new data without
fooling yourself?

**The procedure (v1.8.0, docs/REAL_DATA.md §3.2).**

1. *Write the hypotheses before the data exists in usable form.* Six
   entries went onto `research/agents/blackboard.jsonl`, each naming alpha,
   horizon, expected sign, role (primary, confirmatory, control) and the
   pass rule: leakage clean, gate IC of the registered sign, gate t at or
   above the Bonferroni threshold of the holdout dataset's own fresh ledger.
2. *Make the timing verifiable.* The entries were committed and pushed
   (commit `6723fd0`) before any 2026 feature was computed. The commit is
   the timestamp; v1.10's `anchors.json` now records it for each entry.
3. *Freeze everything else.* The alpha definitions, horizons, cost model,
   `v2` methods and feature registry were frozen at v1.7.2. The fitted
   parameters were not: `mu`, `sigma` and `beta` were refit per fold on the
   2026 rows, so this tested the specification, not the 2019–20
   coefficients (v1.13 adds a no-refit `frozen_params` mode for the next
   holdout). The threshold rose with run order, 3.43 to 3.89; future
   holdouts fix it in the pre-registration. The data was two full XNAS ITCH days,
   2026-05-15 and 2026-05-18, on the same three symbols.
4. *Include controls that should fail.* EQ07 (a "reversal" whose sign was
   already wrong in sample) and EQ04 (known to be underpowered) were
   registered as controls. A procedure that confirms everything, controls
   included, is not testing anything.

**The result.** All four confirmatory signals were confirmed (EQ01 t 7.74,
EQ02 8.02, EQ10 8.08, EQ05 9.51, each above its threshold of 3.43-3.79).
EQ07 was significantly wrong-signed again (−3.95): the 10 s relation is
continuation, not reversal. EQ04 was positive but below threshold, "cannot
tell" as registered. No run traded at 1x costs.

**What it does and does not establish.** It establishes that the
microstructure signals are not artefacts of the 2019-20 selection: they
survive six years and a design they could not have been fitted to. It does
not establish size: three of the four are a third to a half of their
in-sample IC (decay, an in-sample inflated by event days, or both), and two
days cannot separate a signal from a two-day common shock (chapter 32). It
says nothing about P&L, because none was registered. And it is spent: those
two days have now been looked at, so the next confirmatory test needs new
sessions.

**The design choices that matter most.** A fresh ledger per holdout dataset
(so the threshold is not inflated by thousands of in-sample looks, and not
deflated by ignoring the looks the holdout itself takes); a pass rule
written as code-checkable conditions; and controls. The weakest point was
that nothing tied the registration to the *code* that would run: an alpha
could have been edited between registration and run without the gate
noticing. Chapter 35 is how v1.10 closed that.

**Check yourself.**

1. Why give the holdout its own ledger instead of adding its looks to the
   in-sample one? *(The in-sample ledger's 5,000+ looks were spent
   searching on other data; charging them to the holdout makes the
   threshold unreachable for no reason. The holdout is charged for its own
   looks, which is what multiple-testing control requires.)*
2. A confirmatory signal passes with t 3.8 against a threshold of 3.79.
   Is that a confirmation? *(Under the registered rule, yes. Report the
   margin, and do not lower any other threshold after seeing it.)*

## 34. What the synthetic maker and quoting results do and do not show

**The numbers.** All on synthetic data:

| setup | result | source |
|---|---|---|
| maker backtest, golden vector, passive exit | −6.7 bp per trip (32% of exits fill passively) | COOKBOOK 39 |
| same, ungated, taker exit | −11.4 bp per trip | COOKBOOK 39 |
| two-sided quoter, golden vector, toy L1-imbalance score, skewed | +86.50 USD | COOKBOOK 40 |
| same, no skew (inventory term only) | +120.20 USD | COOKBOOK 40 |
| quoter, 8 synthetic days, look-ahead signal, skewed | +3,326.66 USD, Sharpe per day 34.75 | v1.10 development run* |
| same, no skew | +2,535.66 USD, Sharpe per day 36.95 | v1.10 development run* |

\* Measured during v1.10 development with the synthetic generator and
look-ahead score of `python/tests/test_quoting.py` extended to eight days;
the committed test runs four days and asserts only the direction (skew
beats no skew on markout and net). The figures are not a committed
artefact.

**What they show.**

- *The accounting is right.* Every row decomposes exactly: for the maker
  backtest, `gross = spread earned − adverse selection − exit slippage`;
  for the quoter, `spread + markout + inventory + flatten` plus rebates
  minus fees and impact. The identities are tested in every exit mode, so a
  P&L number cannot come from a term nobody can name.
- *The controls work.* The inventory limit is never exceeded (both quoters
  hit 300 shares and stop), the half-spread floor uses the calibrated
  adverse selection, and the flatten crosses what is left before the close.
- *Skew is a bet on the signal.* With an informative signal (the
  look-ahead fixture), skewing the reservation price earns more in total
  because it leans quotes away from the side about to be run over. With an
  uninformative one (the toy score on the golden vector), skewing only
  moves quotes off the best price: less spread captured, no less adverse
  selection, lower net.

**What they do not show.**

- *Anything about a real alpha.* A look-ahead signal knows the future mid.
  Its job is to prove that if a signal is informative, the machinery turns
  that into lower markout losses. Real alphas forecast about 0.07 bp
  (chapter 31); whether that is enough to move quotes usefully is the open
  question.
- *Realistic fill probabilities.* The synthetic generator's order flow
  decides who trades against a resting quote; real counterparties select
  harder. Synthetic markouts are a lower bound on how bad it can get only in
  the sense that they are not evidence at all.
- *Risk-adjusted superiority.* The skewed quoter earned more on eight days
  and had a lower Sharpe per day (34.75 against 36.95). Sharpe ratios in the
  thirties are themselves a sign the data is synthetic.

**The honest next step** is the real-data maker study now running
(pre-registered, in-sample, seven sessions, EQ01/EQ02/EQ05/EQ10, taker and
passive exits, session-clustered inference with a Bonferroni correction
over eight primary cells). Its verdict rule says even a positive in-sample
result reads "worth an out-of-sample test", never "profitable".

## 35. Governance: why symmetric keys and class-only code hashes were not enough

**The question.** v1.7 gave the agent layer a write broker, a hash-chained
blackboard and pre-registration. v1.8 used them for the 2026 holdout. What
could still go wrong, and why did the first fixes in v1.10 need a second
round?

**Four gaps (plan items G1-G4).**

1. *Pre-registrations were free.* Registering a hypothesis cost no look on
   the multiple-testing ledger, so an agent could register a hundred
   hypotheses and report the one that confirmed. Fix: each registration
   debits one look (`kind="prereg"`).
2. *A registration did not bind the code.* The entry named an alpha id. The
   alpha's code could change between registration and run.
3. *The chain could be rewritten.* A hash chain detects an edited entry, but
   anyone who can write the file can recompute every later hash. Fix: every
   committed version of the board must be a prefix of the current one
   (checked against git history), and `anchors.json` records the first
   commit holding each entry.
4. *Agent identity was asserted.* The broker took the agent id from the
   caller.

**Why the first fix for gap 2 was not enough.** The first draft hashed the
source of the alpha class and its base classes (`inspect.getsource`).
That misses module-level helper functions the class calls, constants
defined next to it, and any `iap.*` module it imports. An alpha's behaviour
could change completely while its class text stayed identical. It also
depended on `inspect`, whose output can vary across Python versions. The
shipped fingerprint (scheme 2) hashes whole source *files*: the defining
modules and, transitively, every `iap.*` module they import (an AST walk),
LF-normalised so Windows and Linux agree, plus the declared features'
registry entries and their family modules, plus the pinned numpy, pandas
and scipy versions. It over-approximates (editing another alpha in the same
file changes the hash), which is the safe direction. GOVERNANCE.md §2a
lists what it still does not cover: implicit package `__init__` modules,
computed dynamic imports, data and config files read at run time, the
interpreter.

**Why the first fix for gap 4 was not enough.** The first draft signed
agent requests with HMAC-SHA256. HMAC is symmetric: the key that verifies a
signature can also create one. So the broker, every verifier, and anyone
who audits the board needs the agent's secret, and any of them can forge a
request in that agent's name. A signature that the verifier could have
produced proves nothing to a third party. Ed25519 separates the two: the
agent holds a private key outside the repository, the broker and verifiers
hold only the public key from `research/agents/agent_pubkeys.json`, and a
verifier cannot sign. Each request covers the agent, the operation, a
digest of the arguments and a single-use nonce, so it cannot be moved to
another operation or replayed. The signed request is stored on the entry,
so anyone can re-verify the whole board offline (`verify-board`). Stored
HMAC entries from the draft remain verifiable with the shared key; new HMAC
writes are refused.

**What is still not covered.** The keys are files; nothing here is a
hardware module or an identity provider. Git anchoring proves an entry
existed at a commit, not that the commit's timestamp is honest; pushing to
a remote someone else controls is what makes it hard to back-date. And all
of this protects the *research record*. None of it touches the trading
path, which stays free of agents by rule (HOW_IT_WORKS.md §6).

**Check yourself.**

1. An auditor wants to check that an agent really made a request. With
   HMAC, what does the auditor need, and what does that let the auditor do?
   *(The shared secret, which lets the auditor forge requests too.)*
2. An alpha imports a helper from `iap.features.rolling`. Is a change to
   that helper caught by scheme 2? *(Yes: the AST walk follows `iap.*`
   imports transitively. It would not be caught by the class-only draft.)*
3. Why does a pre-registration cost a look even if the run never happens?
   *(The choice of hypothesis is itself a search; registering many and
   running the promising ones is a multiple comparison.)*

## 36. Parity proves agreement, not correctness: the snapshot-burst bug

**The question.** v1.11 put the Rust feature engine behind a Python API
(plan item E2) and compared it with the Python reference on every row and
every one of the 45 native slots. The golden tests had passed in all four
languages for many releases. Why did a full-row comparison still find a
disagreement?

**What a golden test checks.** The feature goldens
(`expected_features.json`, `expected_features_anomalies.json`) pin the
values at *checkpoints*: chosen rows at chosen event indices. Every engine
that matches the checkpoints passes. Nothing in that design says the rows
*between* checkpoints agree, and every checkpoint on the anomaly vector
happens to sit outside a SNAPSHOT recovery burst.

**The bug.** When a venue sends a SNAPSHOT, the book is rebuilt over
several events. API_FEATURES.md §2 pins what the feature engine does
during that burst: it keeps the derived state from before the burst and
does not read the half-built book. The Python reference does that. The Rust
engine read the half-built book, recorded mid prices from it, and those
samples moved the return features for a few rows after the burst ended.
Validity matched on every row; only values differed, and only inside or
just after bursts. The comparison was first recorded as a strict xfail (so
a fix shows up as an unexpected pass that asks for the marker to be
removed), and the Rust engine is corrected within the v1.11.0 release
(CHANGELOG v1.11.0).

**Why the comparison found it and the goldens did not.** Three properties
of the new test: it compares *all* rows, not checkpoints; it compares *two
independent implementations* row by row, so any divergence is visible
without knowing the right answer in advance; and it runs on the anomaly
vectors, which exist to contain the awkward cases (gaps, resets, bursts).
The general lesson is the one LEARN.md §21 drew from the v1.3.0 risk bugs:
four engines that agree byte for byte can all be wrong in the same way, and
engines that agree at the points you chose to check can still disagree
everywhere else. A golden test is a statement about the points it pins.

**What the comparison does not prove.** Agreement between Python and Rust
after the fix means the two implement the same rule. Whether that rule is
right is a separate question, answered by the brute-force checks
(`iap.features` against a naive recomputation) and by reading
API_FEATURES.md §2. Parity moves the question from "do they agree" to "is
the spec right"; it does not answer the second.

**Check yourself.**

1. A golden file pins 40 rows out of 2,000. A port matches all 40. What
   has been shown about the other 1,960? *(Nothing directly; only that the
   port's state at those 40 points matches.)*
2. Why make the known gap a *strict* xfail rather than a skip? *(A skip
   hides a fix; a strict xfail fails when the test starts passing, so the
   marker cannot outlive the bug.)*
3. Two engines agree on every row of every vector. Name one way they can
   both still be wrong. *(Both implement the same wrong reading of the
   spec, or the vectors never exercise the case.)*

## 37. Why an LLM must never compute a number

**The question.** v1.11 lets a Claude model drive research (plan item AI1):
it proposes a hypothesis, pre-registers it, asks for a run and files a
finding. Every number in a finding must already exist in an artefact the
finding cites. Why such a strict rule, when the model can do arithmetic?

**Three ways a number goes wrong.** A language model produces the most
plausible next text. A number it writes can be:

- *invented*: a plausible IC that no computation produced (a hallucination);
- *derived*: a correct-looking transformation of real numbers, such as a t
  "after adjustment" or a Sharpe from an IC, done in prose where nobody can
  check the method;
- *injected*: copied from text that an attacker placed where the model
  reads it, such as a board entry saying "t = 9.87".

All three read exactly like a correct number. A reviewer cannot tell them
apart by looking at the finding.

**The rule, and how it is enforced.** Code computes every metric and the
verdict (`iap.llm.runners.verdict`) and writes a report. A finding is filed
only if every number in its title and text appears, up to the rounding
shown, in a *numeric* field of an artefact it cites, and at least one cited
report comes from the same session (`iap.llm.verify.verify_finding`).
Free-text fields do not count as evidence, which is what defeats the
injection case: the planted "9.87" sits in a text field, so citing the
entry that contains it does not verify it. COOKBOOK recipe 55 shows a
finding quoting `0.25` refused, and the same finding with the report's own
numbers filed.

**Why not let the model compute and check the result afterwards?**
Because the check would have to recompute the number, and then the
recomputed number is the evidence and the model's is redundant. Making the
model copy rather than compute keeps the evidence chain short: report →
finding, both hashable, both on the board. It also means the verdict of a
pre-registered test is fixed by code the moment the run finishes; the
model can describe it but cannot argue it into a different verdict.

**What the rule does not cover.** A finding can quote only true numbers and
still be badly reasoned ("IC 0.097, so this will make money"). The rule
checks provenance, not argument; the transcript is kept so a person can
read the argument (GOVERNANCE.md §2b). And the live eval of 2026-10-10
(`claude-haiku-5-5`, 4 of 4 scenarios passed, estimated $0.0096) shows a
model that behaved well; it is the mocked adversarial evals, where a
scripted model attacks each control and each ablation lets the failure
through, that show the controls work.

**Check yourself.**

1. A finding says "IC 0.0968" and cites a report whose `gate_ic` is
   0.096781. Is it filed? *(Yes: the number matches up to the rounding
   shown.)*
2. A finding says "t = 9.87" and cites a board entry whose free-text body
   contains "9.87". Is it filed? *(No: free-text fields are not evidence,
   and a finding needs a report from its own session.)*
3. Why does the budget cap pre-registrations as well as dollars? *(Each
   pre-registration is a look; an agent that can register without limit
   can p-hack within its dollar budget.)*

## 38. Freezing copies instead of deleting them

**The question.** The platform implements the order book in four
languages, the native features and six golden alphas in four, risk in
three and the lifecycle in three. Each copy is golden-pinned. Plan item E3
asked for fewer copies. v1.11 deleted none. Why is that the right outcome?

**The real risk of duplication.** Copies that agree today are not the
problem; the golden tests guarantee that. The problem is the next feature:
it must be written N times, and a copy that is not updated drifts, or
blocks the release until someone who knows that language ports it. The
cost grows with every feature added to every copy.

**Why deletion was ruled out.** The E3 brief allowed deleting only code
with no consumer in `deployment/`, CI, the MVP, documented commands or the
golden harness. Every duplicated copy has one: a Docker image builds it,
the Java platform imports it, or its golden test is the only proof that
that language implements a pinned contract. The two copies with no runtime
consumer (Rust `alpha`, Rust `lifecycle`) are exactly the second kind;
deleting them would remove parity evidence and change the published test
counts. They are recorded as RETIRE candidates, a decision for later.

**What freezing does instead.** POLYGLOT.md marks 18 copies CANONICAL
(where new behaviour lands) and 20 FROZEN (24 paths). A frozen copy is still
built, tested and pinned, but takes no new features. The rule is
mechanical: `tests/harness/check_polyglot_policy.py` fails a pull request
that touches a frozen path without a line starting `POLYGLOT-OVERRIDE:`,
and one that adds a function, method or type without the louder
`POLYGLOT-OVERRIDE: new-api` (COOKBOOK recipe 53). The growth stops; the
evidence stays. If a future release decides the Rust alpha proof is no
longer wanted, deleting it is a one-line CHANGELOG decision against code
that has not moved.

**The general lesson.** When removing something has a cost you cannot
measure well (here, lost evidence), and keeping it has a cost that grows
(here, porting every feature), stop the growth first. That turns an
open-ended liability into a fixed one, and defers the irreversible step
until there is a reason for it.

**Check yourself.**

1. A bug fix in the canonical Python book changes a pinned rule. What has
   to happen to the frozen Java book? *(Port it in the same PR, regenerate
   the golden, and add a `POLYGLOT-OVERRIDE:` line.)*
2. Why is `rust/features` canonical and not frozen? *(It is the fast path
   behind the pyo3 binding of E2; freezing it would have blocked E2 and the
   snapshot-burst fix.)*
3. Why does the gate need the louder `new-api` marker? *(A port of a fixed
   rule is expected; a new declaration in a frozen copy is the drift the
   policy exists to stop, so it needs a deliberate, visible exception.)*

## 39. Model registries as evidence

**The question.** Before v1.11 every fitted model in the repository was a
research object: fitted in a script, scored, written to a report. Plan
item AI3 added `iap.mlops`, a registry with monitoring and shadow mode.
What does a registry add to research that never trades?

**A result needs to name its model.** A sentence like "the maker filter
cut adverse selection by X" is evidence only if someone can load the exact
model, on the exact data, and get X again. Without a registry that model is
a file in a temp directory, refitted with whatever code is current. The
registry makes the model a citable object: its id is a content hash over
the artefact, the parameters, the dataset version and date range, the
feature set and the feature-registry hash, the code fingerprint and the
seed. Refit identically and you get the same id; change anything and you
get a new one.

**Immutability and tamper detection.** A registered model is never
overwritten. Loading it re-hashes everything and refuses a mismatch; COOKBOOK
recipe 54 appends one byte to `model.joblib` and `verify` exits 2. A model
that has silently changed under its name is worse than no model, because
every result that cites it is now wrong without anyone knowing.

**The pre-registration link.** Registering requires a blackboard
pre-registration entry, whose hash is stored on the record, unless the
model is marked `exploratory`, which is also stored and printed. That ties
the model to the hypothesis it was fitted to test, and makes an exploratory
model impossible to present later as a confirmatory one. It is the same
idea as pre-registering a hypothesis (§33, §35), applied to the object that
tests it.

**Monitoring and shadow mode, read as evidence.** Drift checks compare a
current window with the reference window the model was registered on:
feature and prediction drift (PSI, KS), calibration (ECE, Brier) for
classifiers, IC decay for regressors. Recipe 54 shows why the outcome-based
checks matter: the features and the scores did not move, the relationship
did, and only calibration saw it. Shadow mode runs a candidate beside the
champion on the same inputs, records what it would have done, and never
lets it act; `promotion_decision` also requires the lifecycle PROMOTION
gates, so a better shadow score alone promotes nothing.

**What it is not.** No model in this repository is on a trading path, and
none has earned its costs (§7; HOW_IT_WORKS.md §5). The registry does
not make a model good. It makes a claim about a model checkable.

**Check yourself.**

1. Two people fit the same filter with the same data and seed on
   different commits. Do they get the same model id? *(Not necessarily: the
   code fingerprint is part of the id.)*
2. Why is calibration drift not visible in the score distribution?
   *(The scores can keep the same distribution while the probability they
   stand for changes; only realised outcomes show it.)*
3. A shadow candidate beats the champion on P&L delta. Is it promoted?
   *(No: `promotion_decision` also requires the lifecycle gates.)*

## 40. Why a Sharpe must be deflated

**The question.** A study reports a per-day Sharpe of 0.12 over 40 days and
a probabilistic Sharpe ratio (PSR) of 0.78 against zero. Is that evidence
of skill? It depends on a number the study does not show: how many things
were tried before this one was reported.

**Selection inflates the maximum.** Take `N` strategies with no skill at
all. Each one's measured Sharpe is noise around zero, but the best of them
is not: its expected value grows roughly like the square root of `2 ln N`
times the spread of the trial Sharpes. Report the best of 100 worthless
backtests and you report a Sharpe that looks real. The PSR cannot see
this, because it compares one track record with zero; it was never told
there were 99 others.

**What the DSR does.** The deflated Sharpe ratio (Bailey and López de Prado,
2014) replaces zero by `SR0`, the Sharpe the best of `N` null trials would
be expected to reach, and asks the PSR question against that bar, with the
variance of the estimate corrected for skew and fat tails. The published
worked example (annualised Sharpe 2.5 over five years of daily data, 100
trials, skew −3, kurtosis 10) gives `SR0` 0.1132 per day and DSR 0.9004;
`iap.validation.deflated` reproduces both (COOKBOOK recipe 56).

**Which N.** The DSR is only as honest as its `N`, and `N` is exactly what
an author is tempted to choose. Here it is not chosen: `effective_trials`
reads the experiment ledger (§6.6) and counts distinct configurations,
plus one for the run being judged. That is the conservative headline. The
raw look count is larger (one configuration can be looked at many times)
and is printed beside it as `dsr_at_looks`, the harshest reading. Recipe 62
shows the same 40 synthetic days move from DSR 0.78 with one trial, to
0.17 with twelve, to 0.02 with the ledger's count.

**What it does not do.** The DSR is a report, not a gate: the `v4` bundle
adds the block to validation reports and nothing reads it to promote or
reject. A study's registered verdict rule stays the rule; the DSR sits
beside it so a reader can see how much of a Sharpe survives the search
that produced it. CPCV and PBO (recipe 56) answer the related question of
whether the selection procedure overfits: PBO is the probability that the
configuration best in-sample falls below the median out of sample.

**Check yourself.**

1. Two studies report the same PSR. One was the only configuration tried,
   the other the best of 50. Which has the higher DSR? *(The first: the
   second is judged against a higher SR0.)*
2. Why is the minimum track record length independent of `N`? *(It answers
   how long one track record must be to beat a benchmark at a confidence
   level; deflation changes the benchmark, not that question.)*
3. Why not always use the raw look count? *(Repeated looks at one
   configuration are correlated, so counting each as an independent trial
   overstates `N`; it is reported, but as the bound, not the headline.)*

## 41. What tick-to-trade does and does not measure

**The question.** The C++ benchmark reports a tick-to-trade p50 of 1,279 ns
and p99 of 2,559 ns on the golden vector. Is that how fast this platform
would trade?

**What is inside the number.** `bench_tick_to_trade` times each event
individually from a raw IAP1 frame already in memory through five stages:
decode (p50 271 ns), book update (83), the native features (735), alpha
scoring (135) and an order decision with a minimal pre-trade check (30).
The clock starts when the frame is handed to the decoder and stops when an
order intent exists. Warm-up is excluded and the timer's own overhead is
measured and stated. The distribution, not a mean, is the result: p99.9 is
11,263 ns and the maximum 33,112 ns, an order of magnitude above the
median, which is where cache misses, allocation and the operating system
show up.

**What is outside it.** No network card, no kernel, no kernel bypass: the
frame does not arrive from a wire and the order does not leave on one. No
platform risk engine: the hard risk engine is Rust (normative, with a
Python reference port; the Java copy is archived), and the decision stage here is a bench-local check. No exchange gateway and no
venue protocol encoding. A wire-to-wire figure adds all of those, and on
commodity hardware the network path alone is usually larger than
everything measured here.

**Why the machine matters.** The committed baseline was measured on a CI
runner (AMD EPYC), a shared virtual machine. Runner-to-runner variance at
p99 is about 2x, which is why the CI guard fails only at 8x the baseline:
it catches a regression of kind (an allocation per event, a lock), not of
degree. A laptop number is not comparable to the baseline. The Python
reference implementation of the same path manages about 4,600 events per
second with a p50 of 176 µs on a laptop: the price of a readable
reference, not a competing measurement.

**Check yourself.**

1. The p50 is 1.3 µs and the p99.9 11 µs. Which matters more for a maker
   strategy? *(Often the tail: events arriving in a burst are the ones
   where being late costs queue position.)*
2. Could this be quoted as "sub-2 µs tick-to-trade"? *(Only with the
   boundary stated: in-process, decoded frame to order intent, no network
   and no risk engine.)*

## 42. An underpowered result is not a negative result (AUC01)

**The question.** The pre-registered closing-cross study `AUC01` finished
its in-sample run on seven real sessions. Primary cell `C-300s`: 3 trades,
2 of 6 test sessions with a trade, session-clustered mean +19.4 bp, 95%
interval −254 to +293 bp (Bonferroni −529 to +567). Verdict: NO
DEMONSTRATED EDGE. Does that mean auction imbalance does not predict the
cross?

**No, and it does not mean it does.** The strategy decides once per symbol
per day. Three symbols over seven sessions is 21 decisions, of which the
walk-forward traded three, on two days; the registration also asked for at
least four active sessions, and the study had two. With so few
observations the interval is hundreds of basis points wide on either side
of zero: a true edge of +20 bp and a true edge of zero would both produce
data like this. The study could not have demonstrated an edge of any
plausible size, so failing to demonstrate one carries almost no
information. That is what "underpowered" means.

**The opposite mistake.** The secondary cell (opening cross, `O-300s`) has
a narrower interval (+6.1 bp, −9.7 to +21.9) and a large t, from 4 trades
on two sessions. It is tempting to call that the finding. A statistic
clustered on two sessions is not reliable, the cell was registered as
secondary, and it also misses the four-session minimum. Same verdict. Nor
should the unweighted per-trade averages (+26.5 bp and +5.5 bp) be quoted
as the result: the registered statistic is the mean of per-session means.

**What to do with it.** Report it as it is: exploratory, in-sample, no
demonstrated edge, underpowered. Do not re-cut the cells until something
passes; that would be a new look, chosen after seeing the data. The
pre-registration already declared the next step: the frozen `C-300s` fit is
run on 2026 days not touched before. More decisions, from more symbols and
sessions, is the only thing that would make a future negative informative;
the power study (§23) shows how to size that before running it.

**Check yourself.**

1. A study with 21 decisions reports "no demonstrated edge". What else
   should it report? *(The interval, so a reader can see which effect sizes
   it could and could not rule out.)*
2. Why not lower the threshold until the walk-forward trades more often?
   *(That is a different configuration: a new look, chosen after seeing the
   data.)*

## 43. Feedback loops in passive execution

**The question.** A quoter's fills are being run over: each time its ask is
lifted, the price keeps rising. Should the strategy adapt while it trades?

**What the feedback is.** `iap.execution.markout_feedback` keeps an
exponentially weighted average of realised markouts, the price move after
each passive fill measured from the side that filled. A markout enters the
average only once its horizon has passed on the replay clock, so the loop
never uses a price it could not yet have seen. When at least `min_fills`
markouts are known and the average is below the threshold, the policy
stands down for a cooldown, posts further behind, or posts smaller.

**What it achieves.** On a synthetic regime built to be toxic (the price
drifts up and the score always sells), the quoter without feedback fills
192 times and loses 62.75 USD to markout. With a threshold demanding at
least 2 bp earned per fill it stands down 119 times, fills 8 times, and
markout falls to −1.75 USD (recipe 64). The run still loses 85 USD.
Feedback limits damage; it does not create an edge.

**Why loops need care.** A feedback rule is itself a strategy with
parameters (horizon, half-life, threshold, cooldown), and every one of them
can be tuned until a backtest looks good, which is the selection problem of
§40 again. It also changes what the strategy observes: a quoter that stands
down stops collecting the markouts that would tell it the regime has
changed, so the cooldown and the EWMA reset are what let it look again. The
fill hazard (recipe 61) is a different kind of loop: it predicts, before
posting, whether a repost is likely to fill at all, and gives up instead of
chasing. Both are opt-in and synthetic-only here; neither has been run on
the real sessions.

**Check yourself.**

1. Why must the markout enter the average only after its horizon has
   passed? *(Otherwise the policy uses a future price: look-ahead inside
   the backtest.)*
2. The feedback cut the markout loss by 97%. Is the strategy now good?
   *(No: it still loses; it just quotes much less.)*

## 44. What an independent review found, and how we triaged it (v1.13/v1.14)

**The setting.** On 2026-10-11, after v1.12.0, five independent read-only
reviews looked at the platform against the standard of a top market-making
or quant firm: research validity, execution and simulator realism, data and
features, risk/governance/AI/security, and engineering. Every P0 and P1
finding was checked by reading the code path. Their common verdict was
that the spine is right (integer contracts, one RNG, byte-exact
cross-language goldens, a fail-closed risk engine with a differential
fuzzer, the look ledger and pre-registration, a harness that re-derives
every published number) and that several published statements were
weaker than they read.

**What it found, plainly.**

- *Some statistics are not causal or are overstated.* The research
  backtest blocks rows using whether the *future* label is valid, a
  look-ahead that reaches every "net P&L at 1x" and bootstrap CI (R-P0-1).
  The gate t-statistics use a two-bucket HAC window without day
  clustering, so t values such as 21.1 and 25.7 are overstated (R-P1-3).
  The 2026 holdout write-up said "parameters frozen", but every fold
  refitted `mu`/`sigma`/`beta` on 2026 rows, and its threshold rose with
  run order (R-P1-2). **No headline conclusion flips**: still 0 alphas
  promoted, no trade at 1x, no edge in AUC01. The wording and several
  report fields had to change.
- *Evidence integrity had holes.* The risk audit log has no hash chain
  (G-P0-1); the look ledger is plain JSON and the ledger and blackboard
  had already forked between branches (G-P1-1, G-P1-5); `verify-board` ran
  nowhere in CI and the agent key registry was empty (G-P1-2); a CLI could
  record a human lifecycle decision with no proof (G-P1-3).
- *The pre-trade control set is incomplete* against SEC Rule 15c3-5 (no
  max open orders, participation cap, message throttle, cancel-rate limit
  or per-strategy position limit; G-P0-2).
- *Two running studies had rules that could not do what they claimed.* The
  quoting study's Avellaneda-Stoikov terms were numerically inert at the
  registered parameters: every quote sat a fixed 1-2 ticks behind the
  touch and the alpha skew moved it by a fraction of a cent, so "skew vs no
  skew" was untestable (X-P1-3). The maker/quoting verdict rule needed t
  about 4.8 on six sessions or fewer and dropped zero-trip sessions from
  the estimand (R-P1-5). AUC01's closing-cross exit assumed an MOC order
  accepted at Nasdaq's 15:55 cutoff (X-P1-5).
- *Data mapping and simulator biases*, none sign-changing: same-nanosecond
  replace and execute pairs emitted as separate book states, hidden prints
  signed by the tick rule, midpoint prints rounded up, MODIFY size-downs
  never crediting queue position, no SEC/FINRA fees, zero feed latency.
- *Engineering*: CRLF artefacts on Windows (different hashes from Linux,
  the one engineering P0), an O(N) resting-order scan, CI on 3.11 while
  studies ran on 3.12, no type checking.

**How it was triaged.** The rule was: what can be fixed *without changing
any published number or regenerating any artefact* goes into v1.13 now;
what changes numbers goes into one v1.14 "correctness release" that
regenerates goldens, stores and reports once, under new
pre-registrations, with every docs number re-read. Splitting it this way
keeps each release's claims checkable: v1.13 can be verified by "nothing
moved", v1.14 by "everything moved for a stated reason".

- **v1.13 (hygiene and hardening, shipped):** LF artefacts, the O(1)
  order lookup, the 3.11/3.12 CI matrix and slow-test split, advisory
  mypy, hashed requirements and gitleaks; regulatory fee fields (default
  0), one markout reference convention, the auction entry cutoff, quoting
  parameters in ticks; frozen-parameter scoring and a pre-registered
  threshold (code, plus corrected holdout wording), the fixed auction
  orientation, a power-aware study summariser, a longer embargo, a sourced
  event calendar; the required `governance` CI job, a merge procedure for
  research branches, signed approval for human lifecycle edges, a locked
  blackboard append and a stricter LLM numbers rule.
- **v1.14 (correctness, shipped 2026-10-11):** the causal row block,
  trailing labels by default, day-separated inference with an automatic
  bandwidth, the time-shift leakage detector, one look per gate decision,
  the data-mapping fixes, queue credit on MODIFY, opt-in feed latency and
  routed exits, the risk audit hash chain, a chained ledger, maintainer
  trust roots, blocking supply-chain audits, and the 15c3-5 control set.
- **Later:** archiving the frozen Java copy, and switching the opt-in
  execution options on in the v2 maker and quoting pre-registrations.
  Both were done in v1.15 (`archive/java/`, `configs/prereg/v2.json`).

The 15c3-5 controls were planned for "later" and shipped in v1.14 anyway.
The argument for waiting was that a pre-trade check guarding a simulator
protects nothing. The argument that won was cost: seven new checks pinned
by goldens in three languages are cheap to add while the risk engine is
being regenerated for the hash chain, and expensive to retrofit under a
live gateway, where every change to a risk rule is a production change.

**Three ideas from v1.14 worth keeping.**

*A state change is not an event.* An ITCH execute arrives as two
canonical events (EXECUTE, then TRADE) and a replace as two more (CANCEL,
then ADD), each pair stamped with one nanosecond. Up to v1.13 the feature
engine sampled the book after each event, so it saw a book with the old
order gone and the new one not yet added: a state the market was never
in. On the synthetic equity vector that phantom state was 409 of 2,000
rows. The fix groups events by `(instrument, exchange_ts)` and samples
once per group. The streaming MVP keeps the per-event rule, because a live
consumer cannot know an event closes its group until the next one arrives;
that is a stated difference, not an oversight.

*A hash chain makes an edit visible, not impossible.* Each risk audit line
carries the hash of the previous line, and the ledger carries a head over
every entry's identity. Change one byte and every later link breaks. But
someone who can rewrite the whole file can rebuild the chain, so the value
of a chain is only as good as the place its head is copied to: the paper
session's state file, git history on `main`, a pre-registration. The trust
roots follow the same logic one level up: CI reads them from `main`, so a
branch cannot add a key and vouch for it in the same change.

*Causal means "known at decision time", including what you skip.* The old
backtest skipped rows whose future label was invalid. That sounds like
hygiene and is look-ahead: at 09:31:00 you do not know whether the quote
at 09:31:01 will be valid. The v1.14 row block uses only decision-time
state. The old figure is still reported beside the new one
(`net_pnl_1x_cost_label_block`) so the size of the bias is on the page.

**What happened to the studies.** The maker study was left to finish as
registered; it will be reported with the fee, queue-credit, decomposition
and power caveats. The quoting study was **aborted before any result was
read**, with the reason logged (`data/real/quoting_real/ABORTED.md`):
continuing would have spent about twelve hours on a run that could not test
its hypothesis. A v2 protocol in ticks will be pre-registered. AUC01 was
already done; its holdout verdict (0 trades, not confirmed) stands, and
the in-sample +19.4 bp now carries the cutoff and fitted-sign caveats. The
next confirmatory round on new data (R7) runs on v1.14, after the
sessions are re-ingested under manifest x-version 2.

**The lesson.** A review is most useful when it is allowed to say that a
number you published is wrong, and when the fixes are sorted by what they
do to published claims rather than by how easy they are. Aborting a study
because its design cannot answer its question is not a loss; reading its
result first and then deciding would have been.

**Check yourself.**

1. Why put the causal row-block fix in v1.14 and not v1.13? *(It changes
   every net P&L and CI; v1.13 promises no published number moves.)*
2. Why does it matter that the quoting abort happened before any result was
   read? *(An abort after seeing the result is a selection decision; before,
   it is a design correction.)*

## 45. Queue position: why the back of the queue fills last and worst

**The question.** Two limit buys rest at the same best bid. Are they the
same order?

**No: the queue makes them different.** At one price, a FIFO venue fills
orders in arrival order. An order that joins a bid with 4,000 shares
already resting fills only after 4,000 shares have traded or been
cancelled ahead of it. The order at the front fills on the first sell that
arrives, including the small, uninformed ones. The order at the back fills
only when a lot of selling has gone through the level, and heavy selling is
exactly when the price is about to fall. So the back of the queue fills
less often, and when it fills it is more often run over: queue position is
a large part of a passive order's adverse selection (§30).

**What can be observed.** On an order-by-order (MBO) feed such as ITCH the
queue ahead of a simulated order is the displayed size that arrived before
it, minus what has traded or been cancelled since. The simulator keeps
exactly that number per resting child (`ahead_qty`, rule 4 in the
header of `iap.execution.simulator`) and fills a child only from trades
the tape shows, never from its own wish to fill. Two corrections were
learned the hard way: a cancel *behind* our order must not move us forward
(§22.3), and since v1.14 a MODIFY that shrinks an order *ahead* of us
credits our position like a cancel would (X-P1-1).

**Where it lives.** `iap.execution.simulator` (Python port; C++ is the
reference), `ahead_qty` in the fills goldens; the fill-hazard model (§58)
uses the queue ahead as its first feature.

**Check yourself.**

1. Why does a simulator that fills a passive order whenever the price
   touches its level overstate maker P&L twice over? *(It fills too often,
   and it fills the benign touches that a real back-of-queue order would
   have missed.)*
2. A size-reducing MODIFY ahead of us: why credit it? *(The shares ahead of
   us shrank exactly as if they had been cancelled; ignoring it puts us
   artificially far back.)*

## 46. Round-trip cost: why a 0.1 bp edge cannot pay a 1 bp spread

**The question.** EQ10 and EQ05 were confirmed on unseen data (§78). The
v1.16 P0-6 monetisation study then ran 480 cells of holding period, entry
band, signal and sizing, and every cell made zero trades. How can a
confirmed predictor never trade?

**The arithmetic.** A taker who buys at the ask and later sells at the bid
pays the full spread plus two fees, whatever the price does in between.
The research cost model writes this hurdle, per unit of notional, as

```
round_trip_cost_return = multiplier * (2 * half_spread + 2 * fee_per_unit) / mid
```

(`CostModel.round_trip_cost_return` in `iap.backtest.costs`; impact is
left out because it depends on size). The default `cost_aware` position
rule enters only when the expected return clears that hurdle. In the P0-6
diagnostic the largest expected return the signals produced was about
0.1-0.2 bp, the median round-trip cost about 0.8-1.2 bp, and the realised
move in the direction of the top 0.1 % of signals about 0.1-0.15 bp from
1 s to 1 m (`research/v116/MONETISATION_P0-6.md`). The edge is an order of
magnitude below the cost.

**Why longer holds do not fix it.** Holding longer lets a move grow, but a
1 s microstructure signal has mostly decayed by then, while the spread is
paid in full on every round trip. Widening the entry band trades only the
strongest signals; the study's top 0.01 % (27-107 events) had noisy,
often negative moves (best 0.58 bp). An IC measures ranking, not size: a
correlation of 0.08 with a 1 s return whose standard deviation is a
fraction of a basis point predicts a fraction of a basis point.

**What would change it.** Not paying the spread: posting instead of
crossing (§55), or quoting both sides and earning the spread (§56). That
is why the owner's decision after P0-6 was no-go on buying data for a
taker strategy, and the remaining test is the two-sided quoting study.

**Check yourself.**

1. A signal predicts 0.15 bp per trade with a hit rate of 60 %. The spread
   is 1 bp. Expected net per round trip? *(About 0.15 − 1 = −0.85 bp
   before fees: the hit rate does not enter.)*
2. Why did the cost-aware rule produce zero trades rather than many losing
   trades? *(It refuses any entry whose expected return is below the
   hurdle; a taker-only rule that ignored costs would have traded and
   lost.)*

## 47. The maker's P&L identity: spread captured, markout, inventory

**The question.** A quoting run reports net P&L of −12 USD. What went
wrong: the quotes, the fills, or the inventory?

**One identity answers it.** For quote fill *i* with signed quantity `d_i`
(+ for a buy), price `p_i`, the mid `m_i` just before the fill's event,
and the mid `m_i^h` a markout horizon later, the quoting backtester splits
the gross P&L exactly (rule 6 of `iap.backtest.quoting`):

```
gross           = sum -d_i * p_i
                = spread_captured + markout + inventory_pnl + flatten_cost
spread_captured = sum d_i * (m_i - p_i)          # >= 0 when we buy below the mid
markout         = sum d_i * (m_i^h - m_i)        # adverse selection, < 0 is bad
inventory_pnl   = sum d_i * (M - m_i^h) + sum_f d_f * (M - m_f)
flatten_cost    = sum_f d_f * (m_f - p_f)        # crossing to get flat
net             = gross + rebates - taker_fees - impact
```

where `f` runs over the flatten rounds and `M` is the mid of the last one.
The telescoping works because the position is flat at the end
(`sum d_i = 0`); the identity is tested.

**How to read it.** A maker earns `spread_captured` (half the spread per
fill when it quotes at the touch) and the rebate. It loses `markout` when
the fills it gets are the ones the market is about to run over, it carries
`inventory_pnl` risk while it holds a position, and it pays
`flatten_cost` to get flat at the end. A strategy whose spread capture is
eaten by the markout has no edge at the touch; that is what the v1.15
one-sided maker study found on real data (−0.6 to −1.7 bp per round trip).

**Check yourself.**

1. Why measure the markout from the pre-event mid rather than the fill
   price? *(So the spread captured and the subsequent move are separate:
   from the fill price the two would be mixed.)* Since v1.13 the reference
   is the mid before the first event of the fill's timestamp (X-P1-4).
2. Spread captured +30, markout −35, rebates +8, flatten −5. Net before
   fees and impact, ignoring inventory? *(−2.)*

## 48. Square-root impact

**The question.** Trading 1 % of a day's volume costs, say, 10 bp of
impact. What does 4 % cost: 40 bp or 20 bp?

**The empirical regularity.** Across markets and decades, the price impact
of a metaorder grows roughly with the square root of its size relative to
daily volume, not linearly. The research cost model's default (since
v1.5.0) is

```
impact_bps = sqrt_impact_coeff_bps * sqrt(|q| * unit / adv)
```

with `sqrt_impact_coeff_bps` the impact of trading one full ADV, pinned at
100 bp (`DEFAULT_SQRT_IMPACT_COEFF_BPS`, a convention fixed before any
result was computed with it, not a calibration). So 1 % of ADV costs
`100 * sqrt(0.01) = 10` bp and 4 % costs `100 * sqrt(0.04) = 20` bp: four
times the size, twice the cost per share.

**Why the shape matters.** The legacy linear rule (`impact_model="linear"`)
charges `coeff * participation_in_pct` bp, with the default coefficient
2 bp per % of ADV. For a 1,000-share child in a 5 M-share ADV that is
`2 * 0.02 = 0.04` bp; the square root charges `100 * sqrt(0.0002) ≈ 1.41`
bp. Linear impact understates the cost of small orders, which are most
of what an intraday strategy sends, and makes capacity look larger than
it is.

**Where it lives.** `iap.backtest.costs.CostModel` (`impact_model`,
`sqrt_impact_coeff_bps`, `impact_bps`), required to be named in
`configs/execution/execution.json` so a v1.4.0 document cannot be priced
silently under the other rule. The execution simulator is a separate
component with its own impact rules (§49).

**Check yourself.**

1. Under the square-root rule, what does halving an order do to its total
   impact cost? *(Cost per share falls by `1/sqrt(2)`, total cost by
   `1/(2*sqrt(2))`, about 35 % of the original.)*
2. Why is the coefficient called a convention? *(It was chosen before
   seeing results, so it cannot have been tuned to them; it was never fitted
   to this data. §50 is the calibration.)*

## 49. Parent vs child impact

**The question.** A 10,000-share parent order is sliced into ten
1,000-share children. Does slicing reduce its impact?

**Under a per-child rule, yes, and that is the bug.** Simulator rule 6
charges each taker fill a linear impact computed from its own child's size.
Ten small children each pay a tenth of the per-share impact a single
10,000-share child would, so slicing cuts the modelled impact cost tenfold.
Real impact does not work that way: the market sees the parent's
cumulative flow, and slicing changes the timing, not the total pressure.

**Rule 6b (v1.16 P0-1, opt-in).** With `ExecConfig.impact_model =
"parent"`, the simulator keeps the parent's cumulative taker quantity `Q`
and charges each fill the *increment* of the parent's total cost:

```
f(Q)      = coeff * (Q * qty_unit / adv * 100) ** impact_exponent   # bps
total(Q)  = f(Q) * Q                                                 # bps x qty
fill cost = (total(Q + q) - total(Q)) * 1e-4 * qty_unit * price
```

Summing over the fills gives `total(Q_final)` whatever the slicing, so
splitting a parent cannot lower its cost (it is exactly equal at a constant
price). `impact_exponent` in (0, 1] chooses linear (1) or square root
(0.5). Children with `parent_id == 0` are each their own parent.

**Default unchanged.** `impact_model="child"` is the default and keeps rule
6 and every golden byte-identical; 6b is Python only. The v3
pre-registration config turns it on with `impact_shape: "sqrt"`
(`configs/prereg/v3.json`), with the coefficient taken from the residual
calibration (§50).

**Check yourself.**

1. Under rule 6 with linear impact, how does the cost of a 10,000-share
   order change if it is sent as 100 children of 100? *(It falls 100-fold;
   under 6b it is unchanged.)*
2. Why charge the increment rather than `f(Q)` per fill? *(Charging the
   current marginal rate to each fill would make early fills cheap and late
   fills expensive and the total would depend on fill sizes; the increment
   telescopes to the parent's total.)*

## 50. Residual vs total impact

**The question.** You measure impact as the mid move from just before an
aggressive sweep to some time after it. What did you measure?

**Two things at once.** A marketable order that walks the book removes the
levels it trades through, so the mid moves mechanically the instant it
executes. The cost of walking the book is already charged separately as
the fill prices themselves (the depth walk). The *total* move from before
the sweep therefore double-counts it. What a parent-impact rule should
charge is the *residual*: how much further the mid moves after the sweep,
which is the market's reaction to the information in the flow.

**The calibration (v1.16 P0-2).** `estimate_calibration(...,
impact_fit=True)` (CLI `--impact-fit`) adds an `impact.residual` block.
For each sweep with sign `s` (+1 for a buy), pre-sweep mid `m0`, post-sweep
mid `m_post` and the mid `m_h` a horizon later, the response is

```
y = 1e4 * s * (m_h - m_post) / m0        # bps
x = 100 * q / adv                        # % of a tape ADV
```

with ADV from the tape, including non-displayed prints. It fits
`y = b * x` and `y = b * sqrt(x)` through the origin with HC0 standard
errors, and a log-log power fit, per horizon. Sweeps that straddle a
trading halt are skipped. `apply_impact_fit(config, calibration, shape)`
then switches an `ExecConfig` to rule 6b with the fitted coefficient and
exponent (shape `"linear"`, `"sqrt"` or `"power"`; a fitted exponent
outside (0, 1] is refused).

**Why through the origin.** Zero size must mean zero impact; an intercept
would charge a fixed cost per sweep that belongs in the spread and fees.

**Check yourself.**

1. Why would fitting on the total move make the parent rule overcharge?
   *(The depth walk is already in the fill prices; adding it again inside
   the impact charge counts it twice.)*
2. Why tape ADV including hidden prints? *(Displayed-only volume
   understates the market's size, overstates participation and so
   understates the per-% coefficient.)*

## 51. Permanent vs temporary impact, and trading against your own flow

**The question.** After a 20,000-share buy program, part of the price rise
reverts and part stays. Which part does a strategy pay for?

**Two legs.** Temporary impact is the premium paid to get liquidity now; it
decays once the pressure stops. Permanent impact is the information the
flow reveals; it stays, and every later share trades at the new level.
Almgren–Chriss (§52) models both: `h(v) = eps*sign(v) + eta*v` temporary,
`g(v) = gamma*v` permanent.

**In the simulator (rule 6b).** The permanent leg uses the instrument's
cumulative signed taker quantity `S` (buys +). A fill moving it from `S`
to `S'` pays

```
perm * (S'^2 - S^2) / 2      (in % of ADV units, times 1e-4 * qty_unit * price)
```

which is `m(S) = perm * S` integrated over `dS`. Buys and sells use the
same formula, so a sell that brings `S` back towards zero earns a credit:
trading against your own earlier flow is cheaper than adding to it, as it
should be. `permanent_impact_bps_per_pct_adv` defaults to 0 and needs
`impact_model="parent"`; the config refuses it otherwise.

**Why it matters for intraday research.** A strategy that turns over its
position many times a day keeps adding and removing flow. With no permanent
leg it pays temporary impact on every trade and never feels its own
footprint on the price; with one, round trips partly cancel and one-way
accumulation costs more. Both are models; neither is calibrated on a
strategy's own fills here.

**Check yourself.**

1. Buy 1,000, then sell 1,000, with `perm > 0` and no other flow. Net
   permanent charge? *(Zero: `S` goes 0 → 1,000 → 0 and the two increments
   cancel.)*
2. Why must `eta - gamma*tau/2` be positive in Almgren–Chriss (§52)?
   *(Otherwise the per-interval temporary cost term is non-positive and
   the optimisation has no minimum; the code raises and asks for more
   slices.)*

## 52. Almgren–Chriss: trading off impact against risk

**The question.** Sell 50,000 shares by the close. Fast is expensive
(impact); slow is risky (the price may move). What schedule is optimal?

**The model.** Almgren and Chriss (2000): work `X` shares over `N`
intervals of length `tau = T/N`, trading `n_j` in interval `j` and holding
`x_j` after it. With temporary impact `h(v) = eps*sign(v) + eta*v`,
permanent impact `gamma`, volatility `sigma` and risk aversion `lambda`,
minimise `E + lambda * V` where

```
eta_tilde = eta - gamma * tau / 2
E = gamma * X^2 / 2 + eps * sum |n_j| + (eta_tilde / tau) * sum n_j^2
V = sigma^2 * tau * sum_{j=1..N} x_j^2
```

The optimal holdings are

```
x_j = X * sinh(kappa * (T - t_j)) / sinh(kappa * T),    t_j = j * tau
2 * (cosh(kappa * tau) - 1) / tau^2 = lambda * sigma^2 / eta_tilde
```

With `lambda = 0`, `kappa = 0` and the schedule is linear (TWAP); larger
`lambda` front-loads, trading more early to cut the variance of the
unsold remainder. `efficient_frontier` returns `(lambda, E, V)` with `E`
rising and `V` falling as `lambda` grows.

**Where it lives.** `iap.execution.optimal` (`ACParams`, `ac_kappa`,
`ac_holdings`, `ac_trajectory`, `ac_cost`, `efficient_frontier`), used
only when a `ParentOrder` sets `is_model=ISModel.ALMGREN_CHRISS`; the
pinned IS schedule (an exponential decay that is part of the cross-language
golden contract) is unchanged.

**Units are the trap.** `ac_params_from_calibration` maps the simulator's
coefficient to `eta = coeff * 1e-2 * price / adv`, so `adv` must be in
shares per the same time unit as `horizon` and `sigma`. An ADV per day with
a horizon in seconds is off by 23,400x (6.5 h). Since v1.13 the explicit
form `adv_per_day` + `horizon_s` exists and every ambiguous mix is refused.
Since v1.16 `impact_source="residual"` takes `eta` from the residual linear
fit of §50 instead of the full-move slope.

**Check yourself.**

1. What does the optimal schedule look like when `sigma = 0`? *(Linear:
   with no price risk there is nothing to gain by hurrying.)*
2. Why does a more risk-averse trader front-load? *(Shares held later are
   exposed to more price variance; selling early reduces `V` at the cost of
   higher `E`.)*

## 53. TWAP, VWAP and implementation shortfall schedules

**The question.** Three parent algorithms all spread an order over time.
What does each one assume?

**TWAP** slices evenly in time: equal weights, no view of the market.
**VWAP** slices in proportion to expected volume, so each child is a
similar fraction of the volume trading around it; the pinned curve is
`1 + x^2` over the session (heavier at the ends, the intraday "U" in
simplified form), and since v1.10 a forecast curve can replace it
(`ParentOrder.volume_curve`, `iap.execution.volume_curve`, with
`estimate_volume_curves(tz="America/New_York")` since v1.13 so a curve is
built in exchange-local time). **IS** (implementation shortfall)
front-loads to reduce the price risk of waiting: the pinned schedule decays
exponentially with `risk_aversion`, or follows Almgren–Chriss (§52) when
asked. **POV** (§54) has no schedule at all; it follows the tape.

**What the benchmark is.** Each algorithm is judged against the benchmark
it targets. VWAP against the interval VWAP, IS against the arrival price
(the shortfall decomposition in §11). Comparing a VWAP algorithm with the
arrival price punishes it for something it was not trying to do.

**Where it lives.** `iap.execution.algos` (`AlgoType`, `ParentOrder`,
`slice_weights`, `slice_quantities`, `slice_times`); C++ is the reference
for the pinned schedules (`cpp/include/iap/execution/algos.hpp`).

**Check yourself.**

1. A VWAP child at 09:31 is three times the size of one at 12:30. Bug?
   *(No: the curve expects more volume near the open.)*
2. Why does the forecast curve need a time zone? *(A UTC curve misplaces
   the open and close by the daylight-saving offset for part of the
   year.)*

## 54. POV participation maths

**The question.** A POV (percent-of-volume) algorithm is set to 10 %. The
market trades 9,000 shares. How many should it have traded?

**Two answers.** The pinned rule targets `floor(p * V)` with `V` the
market's TRADE volume: 900 shares. But the market volume a simulator
replays never contains the strategy's own simulated fills; in reality
those fills are part of the total. If we trade 900 against 9,000 of
others, the total is 9,900 and our share is 9.1 %, not 10 %. To be `p` of
the total `V + ours`:

```
ours = p * (V + ours)   =>   ours = p / (1 - p) * V
```

which at `p = 0.10` and `V = 9,000` is 1,000 shares, 10 % of 10,000.

**The v1.16 option.** `ParentOrder.pov_exact` (P0-5, opt-in, Python only)
uses the target `floor(p / (1 - p) * V)` and covers the whole deficit at
once in children of at most `max_child_qty`, so the algorithm catches up
after expiries or missed fills instead of trading at most one child per
print. It needs `0 < participation < 1` (`iap.execution.replay`). Off, the
pinned rule is unchanged, because it is part of the cross-language golden
contract.

**Why the difference matters.** At 5 % the gap is small (`0.05/0.95` is
5.26 %); at 25 % it is a third more (`0.25/0.75`). Participation also
feeds the risk engine's 15c3-5 participation cap (§98), which counts only
TRADE prints as market volume.

**Check yourself.**

1. At `p = 0.2` and `V = 8,000`, what does `pov_exact` target? *(2,000;
   the pinned rule targets 1,600.)*
2. Why cap each catch-up child at `max_child_qty`? *(One huge catch-up
   child would itself be a large, impact-heavy taker order.)*

## 55. Maker vs taker economics

**The question.** The same 1 s signal loses money crossing the spread.
Could it make money posting instead?

**Two different businesses.** A *taker* sends marketable orders: it is
certain to trade, pays the spread and a taker fee, and is paid only by the
move it predicted. A *maker* rests limit orders: it may not trade at all,
earns the spread and usually a rebate when it does, and pays adverse
selection when the order that hits it was better informed (§30, §47). Per
share, on a venue charging a taker fee `f` and paying a maker rebate `r`:

```
taker round trip ≈ -spread - 2f + realised move
maker round trip ≈ +spread + 2r + markout_1 + markout_2     (if both legs fill passively)
```

with the markouts usually negative. The maker turns the spread from a cost
into income, at the price of fill uncertainty and adverse selection.

**Sizes on this data.** The assumed fee tiers
(`configs/reference/fee_tiers.json`) charge a taker 0.003 USD per share and
pay a maker 0.001-0.002 (assumptions, not a quoted price list). On a 250 USD stock
0.003 USD is 0.12 bp; on a 25 USD stock it is 1.2 bp. Fees therefore matter
much more for cheap stocks. On real Nasdaq sessions the v1.15 one-sided
maker study (`iap.research.maker_real`) found that resting one side at the
touch lost 0.6 to 1.7 bp per round trip: the markout outweighed the spread
and rebate. The signal's job in a maker strategy is to avoid the fills
that will be run over and to lean the quotes (§56), not to pay for crossing.

**Where it lives.** `iap.backtest.maker` (`MakerBacktester`, `MakerConfig`),
`iap.execution.passive` (`ExecPolicy`), §31 for why taker-only research
cannot pay at 1 s.

**Check yourself.**

1. A maker fills at the bid, and 1 s later the mid has fallen by more than
   the half-spread. What did the round trip earn so far, before the rebate?
   *(Less than zero: spread captured minus a larger markout.)*
2. Why is a maker's P&L harder to backtest than a taker's? *(Whether it
   fills depends on queue position and on who traded, which a backtest has
   to simulate from the tape; a taker fill is nearly certain.)*

## 56. Two-sided quoting and inventory skew

**The question.** A market maker quotes both sides. After buying three
times in a row it is long. What should it do to its quotes?

**Lean against the inventory.** Avellaneda and Stoikov (2008) derive a
*reservation price* below the mid when long (and above when short), and a
half-spread from risk aversion and order-arrival intensity. The quoting
backtester's rule 2 (`iap.backtest.quoting`) is that model plus an alpha
term:

```
r = m + alpha_weight * er * m - gamma * sigma2 * q * tau
h = max(0.5 * gamma * sigma2 * tau + ln(1 + gamma / k) / gamma,
        min_half_spread_ticks * tick,
        (adverse_selection_bps - rebate_bps) * m / 1e4)
bid = floor((r - h) / tick),   ask = ceil((r + h) / tick)
```

with mid `m`, expected return `er` from the alpha, inventory `q`, variance
rate `sigma2` and horizon `tau`. A long position lowers both quotes, so the
ask is more likely to be hit (inventory falls) and the bid less likely
(inventory stops growing). A positive `er` raises both: buy more readily,
sell less readily. The quotes are then clamped never to cross the book:
the strategy joins or improves the touch, never takes.

**Hard limits and getting flat.** Rule 3 sizes each side so `|q|` never
exceeds `max_inventory`, counting every working order including ones whose
cancel is in flight. Rule 5 flattens before the end, through the latency
path; fills during a cancel count.

**Check yourself.**

1. `q = +5`, `er = 0`: which quote moves closer to the mid? *(The ask: the
   reservation price falls, so both quotes fall, and the ask approaches the
   mid.)*
2. What does `alpha_weight = 0` give? *(The no-skew baseline, inventory
   control only; the study compares against it.)*

## 57. When a skew is inert: ticks and the no-cross clamp

**The question.** The first M5 quoting study was registered with
Avellaneda–Stoikov parameters, then aborted before any result was read.
Why abort a study that had not failed?

**Because it could not answer its question.** At the registered
parameters, the alpha and inventory terms of rule 2 moved the reservation
price by a fraction of a tick. After `floor`/`ceil` to the tick grid and
the clamp that stops a quote crossing the book, the quotes were almost
always the same with or without the alpha: the skew was numerically inert
(`data/real/quoting_real/ABORTED.md`). A study comparing "skew" with "no
skew" would have compared two copies of the same quotes.

**The fix (v1.13 X-P1-3).** `skew_mode="ticks"` works in whole ticks from
the touch:

```
skew = round(alpha_weight * er * mid / tick)
inv  = round(gamma * sigma2 * q * tau / tick + inv_skew_ticks * q)
bid  = best_bid - hb + skew - inv,     ask = best_ask + hb + skew - inv
```

with `hb = h_min_ticks` (0 joins the touch), then the adverse-selection
floor and the no-cross clamp. Every run reports
`frac_decisions_skew_changed_quote` (the share of decisions whose prices
differ from the same decision with `er = 0`) and
`ticks_behind_touch_{mean,median,max}`, so an inert skew is visible.

**The v2 registration.** The owner-approved values in
`research/quoting_real/prereg_v2_DRAFT.json` set `alpha_weight` per alpha
(EQ01 7, EQ05 15, EQ10 12) so every symbol's median skew is at least one
tick, `inv_skew_ticks` 1 per unit of quantity, and a validity gate of
0.10: if fewer than 10 % of decisions have their quotes changed by the
skew, `summarise` reports "SKEW INERT" and no verdict. Registration and
signing (`tools/quoting_v2_register.py`) are for the owner only.

**Check yourself.**

1. Why is a validity gate better than reading a null result? *(An inert
   skew guarantees a null; the gate says the experiment did not happen,
   instead of reporting "no effect".)*
2. Why was choosing `alpha_weight` from the median skew in ticks not
   peeking at results? *(It uses the signal's distribution, not the P&L,
   and was fixed before the look.)*

## 58. Fill probability as a hazard

**The question.** Before posting a limit order at the touch, how likely is
it to fill within the next second?

**Survival analysis.** A resting order is "at risk" of filling until it
fills or is cancelled; a cancel before any fill is a *censored*
observation, not a non-fill. `iap.execution.fill_hazard.FillHazardModel`
cuts the time since posting into buckets and models the hazard of each
bucket, the probability of filling in it given no fill so far:

```
h_b(x) = 1 - exp(-exp(a_b + beta . z(x)))                   # link="cloglog" (default)
P(fill within t) = 1 - prod_b (1 - h_b) ** (overlap_b(t) / width_b)
```

The cloglog link is the exact grouped-time form of a continuous
proportional-hazards (Cox) model with a piecewise-constant baseline. The
features `z(x)` are standardised: the log of the displayed queue ahead,
the log of the queue-depletion rate, the spread in ticks, the queue
imbalance and the time of day as sine and cosine. Fitting expands each
order into one row per bucket it was at risk in and runs IRLS with a small
ridge, numpy only, so the same data gives the same JSON bit for bit.

**Uses.** The passive policy's `reprice_policy="hazard"` and `min_fill_prob`
give up instead of chasing an order unlikely to fill. A simpler conversion
exists too: `hazard_fill_prob` in `iap.execution.sor_v2` turns a constant
queue-depletion hazard `lambda` into `p = 1 - exp(-lambda * horizon_s)`
for the router (§60).

**Check yourself.**

1. Why not fit a logistic regression on "filled within 1 s: yes/no"?
   *(Orders cancelled at 0.3 s would count as non-fills although they were
   never at risk for the full second: censoring biases it down.)*
2. With a constant hazard of 0.7 per second, what is the 1 s fill
   probability? *(`1 - exp(-0.7) ≈ 0.50`.)*

## 59. Post-only orders, reprices and give-up

**The question.** A buy limit is sent at the bid. While it travels, the
offer drops to the same price. What happens when it arrives?

**It crosses, unless told not to.** A plain limit buy at or above the best
ask executes as a taker: the maker strategy pays the spread and a fee it
never intended to. Simulator rule 10 (v1.12 X4) adds
`ChildOrder(post_only="reject"|"slide")`: a post-only order never takes
liquidity on arrival; it is rejected (`CancelReason.POST_ONLY_REJECT`) or
slid to the best non-crossing price.

**Chasing the touch.** When the market moves away from a resting order,
the policy can cancel and repost at the new touch, losing its queue
position each time. `MakerConfig.entry_reprices` caps how many times;
`reprice_policy="follow"` always reposts, `"hazard"` reposts only if the
fill hazard (§58) says it is worth it; `give_up="cancel"|"cross"` decides
what happens when the budget runs out: abandon the trade, or pay the
spread to complete it.

**Defaults.** All of these are opt-in. The pinned PASSIVE policy keeps
`max_reprices=1`, and the default maker and quoting outputs are
byte-identical to before (a fingerprint test pins them).

**Check yourself.**

1. Why does "slide" exist next to "reject"? *(Slide keeps the order in the
   book one tick away instead of losing the opportunity entirely.)*
2. Why can `give_up="cross"` make a maker strategy look like a taker
   strategy in the TCA? *(Its fills after give-up are taker fills, with the
   spread and fee paid.)*

## 60. Cost-aware routing: all-in price and fill probability

**The question.** Two venues show the same best ask. One charges a taker
fee of 0.30 c, the other 0.10 c but shows only 100 shares. Where should a
2,000-share buy go?

**All-in cost, not displayed price.** The v1.12 router
(`iap.execution.sor_v2.CostAwareRouter`) chooses the route with the lowest
expected all-in cost: price, fee, expected impact and latency risk for an
aggressive order; for a passive order, the expected cost given a fill
probability and the venue's toxicity (markouts). `plan_sweep` plans a
multi-venue sweep with latency-staggered sends so the slower venue's
liquidity is not gone by the time the order arrives.

**The v1.16 options (P0-4).** `RouterOptions(sweep_rank="all_in")` makes
`plan_sweep(..., tick=)` take levels by price *including* the taker fee,
so a cheaper-fee venue one tick worse can come first when the fee
difference is larger than the tick. The default `"price"` takes the
displayed price first with the fee as tie-break, unchanged. `route_passive`
and `passive_costs` accept a `fill_prob(vid, side, queue_qty)` callable in
place of the model's touch fill probability; `hazard_fill_prob` (§58)
builds one; a `None` skips the venue.

**Honest scope.** The real data here is Nasdaq only, so multi-venue
routing is capability for multi-venue data, tested on synthetic venues.
The pinned `SmartOrderRouter` and its C++ parity are unchanged.

> **Superseded (v1.17 B8, pending data).** This rests on synthetic venues. The
> real two-venue evaluation (Nasdaq + IEX books merged on the overlap days, per-venue
> calibration, router vs pinned) replaces it: `research/execution/b8/README.md`,
> docs/EXECUTION_QUALITY.md section 8. No real result exists yet; kept as history.

**Check yourself.**

1. At a 100 USD price, when does a 0.2 c fee difference outweigh a
   one-cent tick? *(Never per share: 0.2 c < 1 c. All-in ranking reorders
   levels only when the fee gap exceeds the price gap.)*
2. Why stagger sends by latency? *(Orders sent at once arrive at different
   times; a fast venue's fill can tell other traders to pull liquidity at
   the slow one.)*

## 61. Latency legs, and stressing them

**The question.** Real ITCH data carries exchange timestamps. How long
after an event can a strategy act on it?

**Two legs, both assumed.** The *feed leg* is the time from the exchange
timestamp to the strategy seeing the event; the *order leg* is the time
from the decision to the order reaching the venue. ITCH has neither: the
v2 pre-registration assumes `feed_latency_ns` 100,000 (100 µs), and v3
adds an assumed venue latency with a parametric tail (`venue_latency`:
mean 250 µs, jitter 100 µs, a 1 % chance of a 10x delay;
`configs/prereg/v3.json`). Assumptions should be labelled as such, and
the config descriptions do.

**Stress instead of believing.** Because the latency is assumed, v3
reports a latency stress grid, `latency_stress` {0, 1, 5}x, with the 1x
case primary (`RealismOptIns.stressed_exec_configs` in
`iap.execution.realism`). A strategy whose result depends on being
faster than the assumption is fragile; one that survives 5x is not
latency-limited. The feed leg is also where look-ahead hides: a decision
must use only events the strategy could have seen at `t + feed_latency`.

**Why calibrated latency is not a fix.** `estimate_calibration` can
measure a feed-receive minus exchange time when a capture has both
stamps, and the simulator warns (`FeedLatencyAsOrderLegWarning`) when such
a feed-derived table is used as the order leg, because a market-data delay is
not an order-entry delay.

**Check yourself.**

1. Why is the 0x case worth reporting? *(It is the upper bound: if a
   strategy loses even with zero latency, speed is not the problem.)*
2. A result is positive at 0x and 1x and negative at 5x. Report it how?
   *(As latency-sensitive, with the assumption stated; the primary 1x
   number is not the whole story.)*

## 62. Fee tiers and dated regulatory fees

**The question.** A backtest across 2019 and 2026 charges sellers the same
SEC fee in both years. Is that right?

**No: regulatory fees change.** US equity sales pay SEC Section 31 (a rate
on sell notional) and the FINRA Trading Activity Fee (per share, capped
per trade). Both are reset periodically, and the TAF was paused from
2026-10-01 to 2026-12-31. Since v1.15 the Python, C++ and Rust engines
read dated periods from `configs/reference/regulatory_fee_schedule.json`
(continuous from 2019-01-01, with sec.gov / finra.org sources) and charge
the fee in force on the fill's date, sell side only (X-P1-1, cross-engine
golden `tests/golden/expected_regulatory_fees.json`). Off in code
defaults, on in `configs/prereg/v2.json`.

**Exchange fees are a tier, not a constant.** A venue's taker fee and
maker rebate depend on the member's monthly volume. A research firm does
not know which tier it would be in, so v1.16 makes it an explicit
assumption: `fee_tier` from `configs/reference/fee_tiers.json` (top / mid /
base: taker 0.003 USD per share in all three, maker rebate 0.002 / 0.0015 /
0.001) with `fee_tier_sensitivity` reporting the lower tier. The v3
pre-registration uses "mid" and reports "base". For a maker strategy the
rebate is often a large share of the edge, so the tier assumption can
decide the sign.

**Check yourself.**

1. Why charge the regulatory fee only on sells? *(Section 31 and the TAF
   are levied on sales.)*
2. A maker strategy earns 0.4 bp net at the top tier on a 50 USD stock. Does
   it survive the base tier? *(Two legs each lose 0.001 USD of rebate:
   0.002 USD per round trip on 50 USD is 0.4 bp, so it breaks even before
   anything else changes.)*

## 63. Capacity and break-even size

**The question.** An alpha earns 2 bp gross per round trip. How large can
each trade be before costs eat it?

**Solve for the size where the next share loses money.** Per unit of
notional, a round trip of size `q` costs the size-free hurdle (§46) plus
impact on both legs:

```
edge_return = round_trip_cost_return + 2 * multiplier * impact_bps(q * unit / adv) * 1e-4
```

Under square-root impact (§48), with `per_leg_bps = (edge_return -
round_trip_cost_return) / (2 * multiplier * 1e-4)`:

```
participation = (per_leg_bps / sqrt_impact_coeff_bps) ** 2
q_breakeven   = participation * adv / unit
```

`CostModel.breakeven_size` returns 0 when the edge does not even cover
spread and fee at zero size, and infinity when the model charges no
impact. `capacity_breakeven` in `iap.validation.metrics` wraps it as
units, notional and participation.

**Worked example.** Edge 2 bp, hurdle 1 bp, multiplier 1: room 1 bp, so
0.5 bp per leg. With the 100 bp coefficient, `(0.5 / 100)^2 = 2.5e-5` of
ADV; on a 50 M-share ADV that is 1,250 shares. The capacity is small
because square-root impact is steep at small sizes.

**Why not the old proxy.** `capacity_proxy_usd` is `max_participation *
ADV * price`: a liquidity cap that is the same for an alpha with a 5 bp
edge and one with none. Since v1.5.0 a validation report's capacity is the
break-even one, and the lifecycle `capacity` gate reads it. Since v1.14
the ADV is each session's own (as-of, from earlier sessions).

**Check yourself.**

1. The edge doubles from 2 bp to 3 bp with a 1 bp hurdle. What happens to
   break-even size? *(Room doubles, so participation quadruples.)*
2. Why does a zero-capacity alpha still get an IC in the report? *(IC
   measures prediction; capacity measures whether it can be traded. §78
   is the case where they disagree.)*

## 64. End-of-day flatten

**The question.** An intraday strategy ends the session holding 413
shares. Is that a problem for its P&L?

**Yes, for two reasons.** It is overnight risk an intraday strategy was not
supposed to take, and a backtest that marks the leftover position at the
last mid quietly skips the cost of closing it. Every honest intraday result
has to say how the position became flat and what that cost.

**The MVP option (v1.16 item 17).** `execution.eod_flatten_lead_ns` in
`mvp.json` (absent = the pinned golden run, `iap.mvp.config`): from
`close - lead`, every decision targets flat, no new exposure is opened,
and the residual (position plus in-flight orders) is worked as one
most-urgent parent ending at the close, with its costs charged. The report
gains `counts.eod_flatten` (decisions, parents, too_late,
residual_position) only when it is on. On the tiny config with a lead of a
third of the session, the end position went from −413 to 0.

**Elsewhere.** The research backtester flattens at session end by rule
(the `session flatten` in the v4 methods), the maker and quoting
backtesters flatten through the simulator when `flatten_route="sim"`, and
the closing auction (§42) is an alternative venue for the last trade.

**Check yourself.**

1. Why include in-flight orders in the residual? *(A child sent before the
   cutoff can still fill and leave a position after it.)*
2. Why make the flatten one most-urgent parent instead of a market order
   at the close? *(Spreading it over the lead window reduces impact; the
   parent still must end by the close.)*

## 65. The information coefficient: what an IC of 0.08 means

**The question.** EQ10's holdout IC is +0.0838. Is that a strong signal?

**What it is.** The IC is the correlation between the score at decision
time and the forward return over the horizon. `iap.validation.metrics.ic`
is the Pearson correlation (NaN with fewer than 32 pairs or a constant
side); `rank_ic` is the Spearman version, robust to outliers. The gate IC
of a report is pooled over the test folds.

**How big is 0.08.** The score explains `IC^2 = 0.0064`, about 0.6 % of the
variance of the 1 s return. That is large by the standards of
cross-sectional equity factors, where 0.02-0.05 per period is considered
good, and it is large because 1 s returns are dominated by the bid-ask
bounce and order-flow mechanics the features see directly. It is also why
the edge is tiny in basis points: 0.6 % of the variance of a 1 s return
whose standard deviation is a fraction of a basis point.

**The fundamental law, with its fine print.** Grinold's approximation
`IR ≈ IC * sqrt(breadth)` says many weakly predictive, independent bets add
up. Two catches here. The bets are not independent: consecutive 1 s
predictions overlap and share the same order flow, so effective breadth is
far below the row count (§29.2). And the law is gross of costs: with an
edge below the round-trip cost (§46), more breadth multiplies a loss.

**Check yourself.**

1. Two alphas have IC 0.08 at 1 s and 0.03 at 5 min. Which predicts larger
   moves in basis points? *(Probably the 5-minute one: the IC is
   dimensionless; the size of the move depends on the label's volatility,
   which grows with the horizon.)*
2. Why report both Pearson and rank IC? *(A few extreme rows can drive a
   Pearson IC; if the rank IC disagrees, the signal lives in the tails.)*

## 66. HAC t: why overlapping labels need it

**The question.** A 1 s alpha is scored every 100 ms. Its IC over 3 million
rows is 0.07. The naive t is about `0.07 * sqrt(3e6) ≈ 121`. Why is the
gate's t an order of magnitude smaller?

**Overlap makes rows dependent.** Ten consecutive rows share 90 % of their
label windows and nearly all of their features. Treating them as 3 million
independent observations overstates the information by orders of magnitude.
A heteroskedasticity- and autocorrelation-consistent (HAC) variance sums
the autocovariances that the naive formula ignores.

**The gate's version.** The gate t is the pooled slope of label on score,
`b = Sxy / Sxx`, with the moment `s_k = sum (x_i - xbar) * e_i` summed in
5-minute buckets and a Bartlett-weighted long-run variance
(`pooled_slope_hac_tstat` in `iap.validation.metrics`):

```
G_l    = sum_k s_k * s_{k+l}
var(b) = (G_0 + 2 * sum_{l=1..L} (1 - l / (L + 1)) * G_l) / Sxx^2
```

The bandwidth `L` (since v1.14, `auto_hac_lags`) is the larger of the
horizon rule `ceil(horizon / bucket) + 1` and the Newey–West (1994) rule
`floor(4 * (n / 100) ** (2/9))` on the number of non-empty buckets,
capped at `n - 1`. The horizon rule alone gives `L = 2` for any horizon up
to five minutes however long the sample, which leaves slow intraday
autocorrelation out. With 1,000 buckets the rule gives
`floor(4 * 10 ** (2/9)) = floor(6.67) = 6`. Since v1.14 no lag product
crosses a session boundary: the overnight gap is not a 5-minute step.

**Check yourself.**

1. Why bucket at all, instead of HAC on raw rows? *(The lag structure in
   rows depends on the event rate, which varies through the day; 5-minute
   buckets fix the lag unit in time.)*
2. Why the Bartlett weights `1 - l/(L+1)`? *(They keep the long-run
   variance estimate non-negative.)*

## 67. Few clusters: day-clustered t with G − 1 degrees of freedom

**The question.** The day-clustered t of an alpha is 3.1 on seven
sessions. Is it significant at 5 %?

**Clustering by day.** `day_cluster_tstat` sums the moment per day instead
of per bucket and assumes no dependence across days:

```
var(b) = G / (G - 1) * sum_d s_d^2 / Sxx^2
```

over the `G` days. Every within-day dependence, however long, is absorbed.
The price is that the variance is estimated from `G` numbers, so the t
has roughly `G - 1` degrees of freedom, not infinitely many.

**What that does to the critical value.** `day_cluster_inference` puts the
normal and Student-t readings side by side (report-only). With seven
sessions, `df = 6` and the two-sided 5 % critical value is 2.447, not
1.96; at three sessions (`df = 2`) it is 4.303. A t of 3.1 on seven days
passes the 5 % Student-t line, but only just, and nowhere near a
multiple-testing threshold (§68). The ledger's
`bonferroni_t_threshold_student_at(total, df)` gives the Student-t
analogue of the Bonferroni threshold for the same reading.

**Why few sessions is the binding constraint.** With the 2-10 real sessions
available, the number of days, not the number of rows, decides how much
can be known. This is why holdouts of fewer than four sessions are read by
sign and replication (`few_sessions`, v1.14), and why §32's FOMC lesson
was about days, not events.

**Check yourself.**

1. Why does the day-clustered t usually come out smaller than the HAC t?
   *(It absorbs within-day dependence that the 5-minute HAC bandwidth
   misses, and it pays for few clusters.)*
2. Seven sessions all have a positive IC. Under the null of no signal and
   independent days, how likely is that? *(`0.5^7 ≈ 0.8 %`: the
   sign-and-replication reading.)*

## 68. Bonferroni and the ledger threshold

**The question.** Why does the gate demand `|t| ≥ 4.432` when every
textbook says 1.96?

**Because 1.96 is for one test.** Run `N` tests at level `α` and the chance
that at least one passes by luck is up to `N * α`. Bonferroni divides `α`
by `N`. The platform's `N` is not chosen per study: it is the total number
of looks in the experiment ledger (`research/experiments.json`), every
gate decision ever recorded. The threshold is

```
|t| >= Φ^{-1}(1 - α / (2N)),      α = 0.05 (PINNED_ALPHA)
```

(`ExperimentLedger.bonferroni_t_threshold`). As of v1.17.0 the ledger holds
5,732 looks (`research/experiments.json`) and the threshold is `|t| = 4.447`. The threshold grows slowly,
like `sqrt(2 ln N)`: doubling the looks adds about 0.15.

**The yardstick beside it.** `expected_max_null_t` is `sqrt(2 ln N)`, the
approximate largest `|t|` that `N` independent null tests would produce:
4.160 at 5,732 looks, as of v1.17.0 (`research/experiments.json`). A best result near that number is what pure search
would have found.

**Why conservative is the point.** Bonferroni is valid under any
dependence and controls the family-wise error rate: the probability of
*any* false promotion. Looks at related alphas are correlated, so the true
effective `N` is smaller and the threshold is stricter than necessary. The
platform accepts that cost because a false PROMOTE is far more expensive
than a missed one. Holm, BH and Romano–Wolf (§69, §70) are reported
beside it and decide nothing.

**Check yourself.**

1. Why count looks across all studies, not just this one? *(Selection
   happened across all of them; a fresh per-study `N` would let a
   researcher reset the count by starting a new study.)*
2. What does a rerun of the same configuration on the same data cost?
   *(Nothing: identical identities are de-duplicated, `would_add` returns
   0.)*

## 69. Holm and Benjamini–Hochberg

**The question.** Four alphas have p-values 0.01, 0.02, 0.03 and 0.04.
Which are significant at 5 % after correcting for four tests?

**Bonferroni** multiplies each by 4: 0.04, 0.08, 0.12, 0.16. One survives.

**Holm (1979)** steps down. Sort the p-values; multiply the smallest by
`m`, the next by `m - 1`, and so on, carrying the running maximum so the
adjusted values are monotone:

```
4 * 0.01 = 0.04,  3 * 0.02 = 0.06,  2 * 0.03 = 0.06,  1 * 0.04 = 0.04 -> 0.06
Holm-adjusted: 0.04, 0.06, 0.06, 0.06
```

One survives here too, but Holm is never less powerful than Bonferroni and
controls the same family-wise error rate.

**Benjamini–Hochberg (1995)** controls a different quantity, the false
discovery rate: the expected share of false ones among the discoveries. It
steps up from the largest p-value, `q_(k) = min over j >= k of p_(j) * m /
j`:

```
0.04 * 4/4 = 0.04,  0.03 * 4/3 = 0.04,  0.02 * 4/2 = 0.04,  0.01 * 4/1 = 0.04
BH q-values: 0.04, 0.04, 0.04, 0.04
```

All four are discoveries at an FDR of 5 %.

**Which question.** FWER asks "is anything here a fluke?"; FDR asks "what
share of my list is flukes?". A promotion gate that puts money behind each
alpha needs the first. A screen that feeds a second, confirmatory test can
use the second. The platform's gate uses the ledger-wide Bonferroni line
(§68); since v1.16 item 15, `iap.validation.multiple_testing` adds
Bonferroni, Holm, BH q and Romano–Wolf columns to the 24-alpha
`REPORT.md`, report-only.

**Check yourself.**

1. Why must Holm carry the running maximum? *(Without it a larger raw
   p-value could get a smaller adjusted one, and the order of rejections
   would make no sense.)*
2. BH declares all four significant. Is each one 96 % likely to be real?
   *(No: FDR is a property of the list, not of each member.)*

## 70. Romano–Wolf: using the dependence between tests

**The question.** Twenty-four alphas share the same order-flow features.
Their test statistics are correlated. Bonferroni treats them as if they
were not. Can a correction use the correlation and still control the
family-wise error?

**Step-down max-t.** Romano and Wolf (2005) compare each observed `|t|`
with the bootstrap distribution of the *maximum* `|t*|` over the
hypotheses not yet rejected, with the bootstrap statistics centred so the
null holds. Taking hypotheses in decreasing `|t_obs|`:

```
p_k = mean over draws b of [ max_{j in rest_k} |t*_bj| >= |t_obs_k| ]
p_adj_k = max(p_adj_{k-1}, p_k)
```

When the tests are highly correlated, the max of 24 correlated statistics
is not much bigger than one of them, so the bar is lower than Bonferroni's;
when they are independent it is close to Bonferroni or Holm.

**How it is built here.** `fold_bootstrap_t` resamples the walk-forward
folds jointly across hypotheses (so their dependence is kept), seeded and
deterministic, and returns `t_obs` and null-centred `t_boot`, each draw
`(mean* - mean) / (sd* / sqrt(F))`. `romano_wolf(t_obs, t_boot)` does the
step-down. `report_family` computes it within each asset class, and gives
`None` with fewer than three shared folds.

**The honest limit.** With four folds there are few distinct resamples, so
the null distribution is coarse: label it fold-based wherever it appears.
It is report-only.

**Check yourself.**

1. Why centre the bootstrap statistics? *(The bootstrap must simulate the
   null; uncentred draws reproduce the observed effect.)*
2. Why resample the folds jointly rather than per alpha? *(Per-alpha
   resampling destroys the correlation that the method exists to use.)*

## 71. The experiment ledger as an append-only chain

**The question.** The Bonferroni threshold depends on the ledger's look
count. What stops someone deleting a few hundred entries before a run?

**A hash chain and a monotonicity check.** Since v1.14 every entry's
immutable identity is chained: `chain_link(prev, entry) = sha256(canonical
({prev} + the entry's identity))`, and `research/experiments.json` stores
`chain_head` and `chain_length`. `ExperimentLedger` refuses to load a
ledger whose head does not match, so the threshold is never computed from
an edited ledger. `tests/harness/check_ledger_monotone.py` (in the
`governance` CI job) requires the head ledger to descend from the base
branch's, its totals not to fall, and every pre-registration's recorded
`ledger_head` to be on the chain.

**What a look is.** Since v1.14 a look is one gate decision: two per
experiment, down from 84 when every fold statistic counted. The ledger key
is the identity (alpha, kind, config, dataset version, statistics
revision), so a regenerated statistic under a new revision is a new look,
and a rerun of the same identity is free.

**Merging research branches.** Branches that ran studies carry their own
looks. `tools/merge_ledger.py` merges two ledgers by entry identity and
reports conflicts; v1.16 merged the `step2` branch's 7 looks into main for
a total of 5,368, and `docs/research/R7_POWER.md` was regenerated (the
threshold moved from 4.432 to 4.433).

**Check yourself.**

1. Why chain the identity and not the whole entry? *(Mutable fields such as
   annotations can change without changing what was looked at; the chain
   protects what the count depends on.)*
2. Why must the ledger only grow? *(Looks cannot be un-taken; a smaller
   ledger is a lower threshold bought by forgetting.)*

## 72. Pre-registration: what it fixes, and when

**The question.** What exactly does a pre-registration commit to, and why
must it come before the data?

**It fixes every choice that could be tuned.** The hypothesis (alpha,
direction, horizon), the data (which sessions, by file hash), the code
(a fingerprint, §74), the methods bundle, the cost model and the
realism switches (`configs/prereg/v2.json`, `v3.json`, hashed into the
registration), the statistic and its threshold, and the decision rule. A
choice made after seeing a result is a new look, chosen by the result.

**How the platform enforces it.** `iap.agents.prereg_gate.require` runs
before the research runner touches any data. It verifies the blackboard's
hash chain, finds the pre-registration for (alpha, horizon), refuses one
written by an agent id (agents may draft, a human signs off), and refuses
a run whose code or feature fingerprint no longer matches. Without one,
the only way to run is `--no-prereg`, which makes the run exploratory and
never gate evidence. Since v1.13 the threshold can be fixed in the
registration (`tstat_threshold="prereg"`) instead of rising with every
later look. Once a confirmatory outcome is recorded by the owner, the
registration is *closed*: later runs are labelled post-confirmation and
can never count as evidence for that look.

**What it is not.** It is not a promise that the result will be positive.
R7 pre-registered a gate and recorded both passes (§78); AUC01 registered
and recorded an underpowered null (§42).

**Check yourself.**

1. After registration, a researcher notices the cost model's fee is out of
   date. What now? *(Record a deviation before the look, with the reason,
   §76; or register a new hypothesis. Never edit silently.)*
2. Why does a closed registration stop blocking on its fingerprint? *(Its
   look is spent; the code may move on, and later runs cannot claim to be
   that test.)*

## 73. Holdout discipline: one look

**The question.** The R7 holdout (7 sessions, 2025-11 to 2026-06) was used
once to confirm EQ10 and EQ05. Can it now be used to tune the monetisation
study?

**No.** A holdout is evidence only as long as no choice has been made with
it. Each use is a look at data that was supposed to be unseen; after a few,
the holdout is just more in-sample data, and the last confirmation it gave
is weaker for having been followed by tuning. So the rules are blunt:

- the v1.16 P0-6 monetisation study ran in-sample only, and the driver
  refuses the R7 holdout sessions;
- the AI4 rebuild may not use the 2025-11 → 2026 holdout sessions for any
  development before its new registration (`research/r7/AI4_DEFERRED.md`);
- the AI4-DEEPLOB-1 registration was signed and then deferred, and its
  spent look stays counted although no result was produced.

**What does not count as a look.** R7's first EQ10 attempt crashed on a
memory allocation before any statistic was computed; it was recorded as a
deviation with no ledger debit and retried once (§76). The test is whether
anyone saw a number that could influence a choice.

**When a holdout is spent.** After R7, the 2026 data is confirmatory
evidence for H4 and nothing else. New questions need new data, or an
honest label: "in-sample on previously held-out data".

**Check yourself.**

1. Why is "we only looked at the plot, not the t" still a look? *(A plot
   carries the same information that would steer a choice.)*
2. Why keep the deferred AI4 look on the ledger? *(It was registered and
   counted; removing it would lower the threshold retroactively.)*

## 74. Code fingerprints

**The question.** An alpha is registered on Monday. On Tuesday someone
edits a helper it calls. Is Wednesday's confirmatory run still testing the
registered hypothesis?

**Not if the code changed.** `iap.agents.fingerprint.fingerprint(alpha_id)`
returns what a pre-registration commits to (scheme 2):

- `code_hash`: sha256 over the source files of the module defining the
  alpha and its `iap` base classes, closed transitively over every `iap.*`
  module they import (an AST walk, including function-local and relative
  imports). Editing *any* code in a covered module changes it: a sound
  over-approximation.
- `features` and `feature_hash`: the registry features the alpha declares,
  closed over `depends_on`, and the modules that compute them.
- `deps`: installed numpy, pandas and scipy versions, pinned by version,
  not hashed.

Files are decoded as UTF-8 with line endings normalised to LF, so a
Windows and a Linux checkout hash alike. Not covered: data and config
files read at run time, dynamic imports, the interpreter.

**A lesson from R7.** A report-only `beta_stability` diagnostic was added
to `alpha/base.py`, a fingerprinted module, before the R7 look; the gate
refused to run. It was reverted (#117) and in v1.16 moved to
`iap.validation.beta_stability`, outside the hashed modules, with a test
that keeps `iap.alpha` from importing it. `tools/prereg_touch_check.py`
now warns in CI (`::warning::`, non-blocking; `--strict` locally) when a
pull request changes the fingerprint of an open pre-registration.

**Check yourself.**

1. Why hash whole modules rather than the alpha's function? *(A function
   depends on helpers, constants and base classes; a module closure catches
   them all at the cost of false alarms.)*
2. A docstring edit in `alpha/equity.py` breaks a registration. Bug?
   *(No, that is the over-approximation; the fix is to make such edits
   after the look, or record a deviation.)*

## 75. Blackboards, fork boards and git anchors

**The question.** Pre-registrations live on a blackboard, a JSONL file with
a hash chain. Anyone with write access can edit an entry and recompute
every later hash. What does the chain prove?

**Internal consistency only; git is the external witness.** The chain
shows the file was not edited *without* re-chaining. `iap.agents.anchor`
adds what the chain cannot: every committed version of the board must be a
prefix of the current one, every anchored entry hash must still appear at
its sequence number in the commit recorded in `research/agents/anchors.json`,
and in CI (`verify-board --trust-ref origin/main`) the board at
`origin/main` must be a prefix of the one under review. A re-chained board
fails, because its committed lines differ.

**Fork boards.** Research branches (`research/maker-real`,
`research/step2`) ran studies against their own boards. Their signed
entries bind the previous hash of *that* board, so replaying them onto
main's board would break every signature. The v1.16 owner decision: do not
replay. Each fork board is archived unchanged under
`research/agents/forks/<branch>/` with a `FORK.json`; main's board records
one operator finding; and the study drivers gate on their archived board
(`PREREG_BOARD` in `iap.research.maker_real` and `auction_real`).

**Check yourself.**

1. Why is "every committed version is a prefix" stronger than the chain?
   *(An edit that re-chains is internally consistent but changes committed
   lines, which git history exposes.)*
2. Why not merge fork entries into main's board with new signatures?
   *(Re-signing rewrites when and against what they were registered; the
   archived copy keeps the original evidence intact.)*

## 76. Deviations, recorded before the look

**The question.** Between registration and the look, something does not
match the plan. Change the plan, or change the data?

**Neither silently.** A deviation is a written record, made before the
relevant look, of what differs, why, and what was decided. R7 had four:

- *Lot sizes.* Holdout lot sizes differed from in-sample (AAPL 100 → 40 on
  2026-06-12; MSFT and QQQ 40 vs 100, the round-lot reform). The owner
  chose option A: merge with the lot-size consistency check relaxed
  (since v1.16 a merge option, `--allow-lot-size-differences`, which
  records `lot_size_by_session` in the manifest), because no feature reads `lot_size` and the gate is IC-only
  (`research/r7/R7_DEVIATION_lot_size.md`).
- *Crash, retry 1.* The first EQ10 look crashed on an 800 MiB allocation in
  the report-only CPCV block before any statistic: no result seen, no
  ledger debit, one retry after an output-identical fix.
- *Crash, retry 2.* EQ05 crashed on 1.04 GiB in the walk-forward splitter,
  again before any statistic; EQ10's completed result was carried over
  unchanged.
- *Code hash.* The `beta_stability` addition was reverted so the
  fingerprint gate passed (§74).

**What makes them legitimate.** Each was decided without knowledge of the
outcome, and each fix was checked to leave the statistics unchanged
("output identical"). A deviation recorded after seeing the result would
be a new, result-driven look.

**Check yourself.**

1. Why does "before any statistic" matter for a crash retry? *(A retry
   after seeing a number gives a second chance; before, it is a technical
   failure.)*
2. Why does the lot-size merge not threaten the IC test? *(Features do not
   read lot size; it would matter for a P&L test that sizes in lots.)*

## 77. Power and the minimum detectable effect

**The question.** Before spending the 2026 holdout, which hypotheses could
it actually confirm?

**Power analysis.** For each drafted hypothesis, `tools/r7_power.py`
(`iap.research.r7_power`) computes the probability of passing the gate if
the true effect is a given size, and the minimum detectable effect (MDE),
the smallest effect passed with 80 % probability. It uses only summary
statistics pinned by sha256; no market data is read and no look is taken.
The gate modelled is the HAC t against the ledger threshold *and* a
deflated Sharpe above 0.95 on the per-session IC series; "joint ≤" is the
smaller of the two powers, an upper bound on passing both.

**What it decided** (`docs/research/R7_POWER.md`, owner decisions
2026-10-11). On 7 sessions, only EQ10 and EQ05 had joint power ≥ 0.8 at
their in-sample IC; they became H4 tier 1. EQ01, EQ02 and EQ04 had joint
power 0.26 / 0.49 / 0.04 on 7 sessions and about 1.00 on 47, so they wait
for tier 2. EQ07 was dropped: its drafted sign contradicted the in-sample
IC, and flipping it after seeing results would fit seen data. The auction
hypothesis was dropped as underpowered even on 47 sessions.

**Why the DSR leg binds.** With `S` sessions, a DSR on `S` per-session
values has little resolution: EQ01's MDE for the DSR leg was 0.1757 on 7
sessions against 0.0386 for the HAC leg. The data's unit is the session.

**Check yourself.**

1. Why run power *before* the look? *(After, it is an excuse; before, it
   decides what is worth testing.)*
2. Why model a decayed effect too? *(The v1.8 holdout found ICs at a third
   to a half of in-sample; planning on the in-sample IC alone would be
   optimistic.)*

## 78. Predictive but not tradable: the R7 result

**The question.** R7 confirmed EQ10 and EQ05 on seven unseen sessions,
2025-11 to 2026-06, six years after the in-sample data. Why is v1.16 about
monetisation rather than deployment?

**The confirmation** (`docs/research/R7_RESULTS.md`). One look each, gate
HAC t ≥ 4.432 and deflated Sharpe > 0.95 on the per-session IC series:

| alpha | holdout IC | in-sample IC | HAC t | DSR | sessions > 0 |
|---|---|---|---|---|---|
| EQ10@1s | +0.0838 | 0.0756 | 13.66 | 0.99995 | 7/7 |
| EQ05@1s | +0.0409 | 0.0627 | 13.20 | 0.9999998 | 7/7 |

EQ10 held its IC; EQ05 kept about two thirds. The registered sensitivity
without the FOMC day and the holiday-thin day changes nothing material.
Drift monitoring (§90) found the queue-rate features drifted for MSFT
(EQ05 reads their 1 s versions), consistent with its lower holdout IC,
without establishing cause.

**The other half.** At the primary 1x cost, net P&L was 0 for both:
no trades. The verdict was IC-only by registration, so the confirmation
stands, and says nothing about money. P0-6 (§46) then showed why: an
expected return of 0.1-0.2 bp against a 0.8-1.2 bp round trip.

**The lesson.** Prediction and profitability are different hypotheses with
different tests. A research process that only asks the first produces
confirmed signals that cannot be traded; one that only asks the second
throws away real information. The platform registered the first, reported
the second, and is now testing the second separately: the in-sample taker
grid (§46, no-go) and the registered two-sided quoting study (§57).

**Check yourself.**

1. Is a 7/7 positive-session result strong evidence by itself? *(It is
   `0.5^7 ≈ 0.8 %` under a sign-symmetric null, so yes for the sign; not
   for the size.)*
2. Why not lower costs in the backtest until the alphas trade? *(That is a
   new configuration chosen after seeing the result, and the costs are
   real.)*

## 79. Walk-forward vs combinatorial purged CV

**The question.** Why does the platform report both a walk-forward result
and a CPCV result for the same alpha?

**Walk-forward** trains on the past and tests on the next block, expanding
(`WalkForwardSplitter`, four folds by default, with purging and a 60 s
embargo, §6.2). It is the honest simulation of deployment: one path,
always forward. But it gives one estimate on one path, the early folds
train on little data, and the result depends on where the fold boundaries
fall.

**CPCV** (López de Prado) splits the data into `N` groups and tests on
every combination of `k` of them, training on the rest with purging and
embargo on both sides of each test block:

```
splits = C(N, k)
paths  = C(N - 1, k - 1)     (= k/N * C(N, k))
```

With the defaults `N = 6`, `k = 2`: 15 splits, reassembled into 5 full
backtest paths. Groups are day-aligned when there are at least `N`
sessions. Each path is a complete out-of-sample history, so the result is
a distribution, not a point.

**The cost.** Training on data *after* the test block is not how a model
would be deployed, and leakage control has to be two-sided. That is why
walk-forward stays the gate and CPCV is report-only (the `v4` bundle's
`cpcv` block, `iap.validation.cpcv`).

**Check yourself.**

1. Why does CPCV need an embargo after the test block, not only before?
   *(Training rows just after the test block have features computed over
   windows that include test-period events.)*
2. With `N = 10`, `k = 2`, how many paths? *(`C(9, 1) = 9`.)*

## 80. Probability of backtest overfitting

**The question.** You tried twelve configurations and picked the best
in-sample. How likely is it that the best one is merely lucky?

**CSCV.** Bailey, Borwein, López de Prado and Zhu: take a `T x M` matrix
of per-period performance (rows in time order, `M` candidates), cut the
rows into `S` blocks (16 by default, even), and for every way of choosing
half the blocks as in-sample, find the in-sample winner and its rank
among the candidates out of sample. With the OOS rank `w = rank / (M + 1)`
(1 = worst), the logit `λ = ln(w / (1 - w))` is negative when the winner
lands in the bottom half. PBO is the share of splits with `λ ≤ 0`.

**Reading it.** PBO near 0: the in-sample winner usually stays above the
median. Near 0.5: picking the in-sample winner is no better than picking
at random. Above 0.5: the selection procedure actively picks losers, the
signature of fitting noise. `probability_of_backtest_overfitting` in
`iap.validation.cpcv` drops constant candidates (they carry no ranking
information) and is deterministic.

**What it does and does not say.** PBO judges the *selection procedure*,
not one strategy. It needs several real candidates; with a single
pre-registered configuration there is no selection to judge, which is
the point of pre-registering one.

**Check yourself.**

1. Why the median as the bar? *(Under no skill, the in-sample winner's
   out-of-sample rank is uniform, so it falls below the median half the
   time.)*
2. Why must `S` be even? *(Each split takes exactly half the blocks as
   in-sample.)*

## 81. Leakage: four detectors and what each one catches

**The question.** An alpha's IC is 0.3. Before celebrating, what could make
it fake?

**A leak is information from the label window reaching the score.**
`iap.validation.leakage.LeakageTester` runs five detectors, each
aimed at a different mechanism:

1. **Label-column guard.** Score the frames as-is and with every label
   column replaced by a constant. Any change means the scoring path reads
   labels: the catastrophic leak.
2. **Shift-by-one.** Lag the score by one row. A causal signal loses some
   IC, not almost all of it, and the required survival scales with how
   much of the horizon one row spans, `collapse_ratio * (1 -
   min(1, median_row_gap / horizon))`. Its suspicion line is a multiple of
   the promotion gate (3 x 0.010 = 0.030), not an absolute IC.
3. **Time shift** (v1.14). The row test is vacuous when a row spans the
   horizon, so this lags the score by `horizon / 4` in time and keeps only
   pairs whose lagged score is fresh. Too few pairs is reported as not
   applicable, never as a pass.
4. **Truncation probe.** Re-score on precomputed frames truncated at
   several anchors: a causal score at `t` cannot change when later rows
   are removed. It sees look-ahead in the scoring path only.
5. **Recompute probe.** Rebuild the features from the raw event prefix and
   require the last row to be bit-identical to the full build. This is the
   one that sees a feature *built* with look-ahead (a centred window, a
   full-sample normalisation), which is baked into every precomputed row.
   A probe that did not run is reported as such and makes the result not
   gate-eligible.

**Why several.** Each detector is blind somewhere: the label guard misses a
feature built from future events, the truncation probe misses a leak baked
into the features, the row shift misses a leak shorter than a row, the time
shift cannot tell a leak from a very fast causal signal (§82). Together they
cover each other's gaps; a failure of any of them fails `passed`, which
blocks promotion.

**Check yourself.**

1. A feature uses the full day's mean volume to normalise. Which detector
   catches it? *(The recompute probe: rebuilt from an event prefix, the
   mean differs. The truncation probe cannot, because the leaked mean is
   already in every precomputed row.)*
2. Why tie the suspicion threshold to the gate? *(A leak worth an IC of
   0.12 was once missed by an absolute 0.15 threshold; any alpha strong
   enough to matter must be checked.)*

## 82. Fast causal or leak? The time-shift triage

**The question.** FX11 fails the time-shift leakage test (§81): lagging its
score by a quarter of the horizon destroys its IC. Is it leaking, or is it
just fast?

**The ambiguity is real.** A leak confined to the near future (a feature
that sees an event at or just after the label's anchor) collapses under a
small lag. So does a genuine signal whose predictive power decays within
the lag. The gate's two numbers, IC unlagged and IC lagged, cannot tell
them apart.

**Triage by the shape of the decay (v1.15, detector 2c).** On a time-shift
failure the tester also lags the score by several fractions of the horizon
and records the profile. `classify_time_shift_failure` then labels it:

- `leak_suspect`: another causal check failed, or the profile has the leak
  shape: a cliff at the first step, a non-monotone profile, or a sign
  flip;
- `fast_causal`: every causal check passed and the IC decays gradually and
  monotonically, keeping its sign;
- `causality_unproven`: the recompute probe did not run, so nothing shows
  the inputs are causal.

The class is a diagnosis for the reviewer. By default `passed` is
unchanged, so a time-shift failure still blocks promotion;
`LeakageTester(accept_fast_causal=True)` is the opt-in. The FX11 finding
(`docs/research/FX11_FINDING.md`): no look-ahead found, consistent with a
fast causal signal, with the stated limit that the gate's numbers alone
cannot distinguish a 45 s leak from a fast signal. FX11 stays in RESEARCH
pending human review.

**Check yourself.**

1. Why is a cliff at the first lag the leak signature? *(A leak worth only
   the next few events vanishes as soon as the lag clears them; a real
   signal fades with the information it carries.)*
2. Why not make `accept_fast_causal` the default? *(The classifier is
   heuristic; a false "fast" on a real leak would promote it.)*

## 83. Embargo by feature window

**The question.** A walk-forward test block ends at 11:00. Training resumes
at 11:01, after a 60 s embargo. A feature in the training row at 11:01 is
a 10-minute rolling mean. What does it contain?

**Nine minutes of the test block.** Purging removes training rows whose
*labels* overlap the test block; the embargo adds a gap. But on two-sided
splits (CPCV, or walk-forward with training after a test block), a
training row just after the block carries *features* whose look-back
window reaches back into it. The gap must therefore be at least the
longest feature window:

```
post_block_embargo = max(embargo_ns, feature_window_ns)
```

(`post_block_embargo_ns` in `iap.validation.splits`, v1.13 R-P1-6,
opt-in via `feature_window_ns`; `"registry"` reads the longest window from
the feature registry). Both `WalkForwardSplitter` and
`CombinatorialPurgedSplitter` accept it. `None` keeps the old rule.

**Before the block, the label decides; after it, the feature does.** A row
before the test block leaks through its forward label; a row after it
leaks through its backward feature window. Purging handles the first, the
post-block embargo the second.

**Check yourself.**

1. Does an expanding walk-forward with training only *before* each test
   block need the post-block embargo? *(Not for that fold; it matters when
   training data follows the test block.)*
2. Why does this matter less for a 1 s label and a lot for a 10-minute
   feature? *(The label overlap is about one second; the feature overlap is
   the window length.)*

## 84. Refit or frozen: what a holdout test re-estimates

**The question.** The 2026 holdout of v1.8 "froze" the alpha definitions,
horizons, cost model and methods. Did it freeze the fitted numbers?

**No, and the docs once said otherwise.** The walk-forward on the holdout
refit each alpha's `mu`, `sigma` and `beta` per fold on 2026 rows. That is
a legitimate test (it asks whether the *recipe* works on new data), but it
is not a test of the fitted model trained in-sample, and the threshold also
rose with run order (3.43 to 3.89). v1.13 (R-P1-2) reworded REAL_DATA.md
and this document, and added the stricter alternative:

- `frozen_params` in `validate_alpha` and `ExperimentRunner.run`: score
  the out-of-sample rows with a parameter blob fitted elsewhere, no refit,
  reporting `frozen_ic` / `frozen_t` beside the refit statistics;
- `tstat_threshold="prereg"`: the threshold fixed in the registration
  instead of rising with later looks.

**Which to prefer.** A frozen test answers "would the model I have today
have worked?"; a refit test answers "does this way of building a model
work?". Deployment depends on the first; research generality on the
second. Say which one a number is.

**Check yourself.**

1. A refit holdout IC is 0.05 and the frozen IC 0.01. What happened?
   *(The recipe adapts to the new period, but the in-sample fit did not
   transfer: the parameters drifted.)*
2. Why does the threshold rising with run order matter? *(The last alpha
   tested faces a stricter bar than the first, for no reason related to its
   evidence.)*

## 85. Hypothesis sign and beta stability

**The question.** An alpha's rationale says high order-flow imbalance
predicts a rise. Its fitted beta on one fold is negative. Does that matter
if the pooled IC is positive?

**Yes: the sign is the hypothesis.** Every linear alpha's raw signal is
oriented by its stated hypothesis, and its fitted beta is free-signed
(`iap.alpha.base`). `hypothesis_confirmed` means the data agrees with the
stated direction. Since v1.14 it requires at least 70 % of the
walk-forward folds to confirm (`min_fold_sign_consistency`); before, only
the last fold was read, and EQ01 had a negative-beta fold the old rule
never saw. An alpha whose fitted sign contradicts its rationale can at
best be ITERATE, never PROMOTE.

**Beyond the sign.** `iap.validation.beta_stability` (v1.16, report-only)
estimates per-instrument and per-instrument-day OLS slopes of the label on
the raw signal (at least 32 valid pairs per slice) and summarises their
count, share positive, sign agreement with the median, median, IQR and
`IQR / |median|`. A beta that swings in sign day to day is a different
kind of signal from one that is steady and small, even at the same pooled
IC. The module lives outside the fingerprinted alpha modules (§74).

**Check yourself.**

1. Why not just flip the sign of an alpha whose beta is negative? *(That
   is a new hypothesis chosen after seeing the data: EQ07 was dropped from
   R7 for exactly this reason, §77.)*
2. `IQR / |median|` is 3. What does that say? *(The day-to-day spread of
   the slope is three times its typical size: unstable.)*

## 86. Scaling per instrument before pooling

**The question.** A pooled IC over AAPL, MSFT and QQQ is 0.06. How much of
that is QQQ?

**Possibly most of it.** On the real data QQQ is about half the rows and
most of the variance. A pooled correlation weights each instrument by its
share of the covariance, so a volatile, heavily traded instrument
dominates both the pooled IC and its t. The result can describe one
instrument while claiming three.

**The v1.14 default (R-P2-2).** `standardisation="instrument"`: before any
pooled statistic, the standardised signal and the label are divided by
that instrument's own standard deviation over its out-of-sample pairs
(scale only, no demeaning, per fold). Each instrument then contributes
by its number of rows, not its volatility. `"pooled"` is the v1.13 rule.
A v1.9 option, `gate_ic_source="instrument_mean"`, can gate on the
equal-weight mean of per-instrument ICs instead.

**Check yourself.**

1. Why not demean per instrument too? *(A level difference between
   instruments can be real signal; scaling equalises weight without
   removing it.)*
2. A pooled IC is 0.06 and the per-instrument ICs are 0.10, 0.01 and 0.00.
   What should the report say? *(That the signal is one instrument's.)*

## 87. Causal row blocks: when a skipped row is a look-ahead

**The question.** A backtest skips rows whose label is invalid, for
example because a trading halt falls inside the horizon. What is wrong
with that?

**The skip uses the future.** Whether a halt happens in the next second is
not known at the decision time. A backtest that declines to trade on rows
where the market *will* halt has used information a live strategy could
not have, and every net P&L and bootstrap interval computed that way
inherits the look-ahead. The v1.5-v1.13 default (`"auto"`) did exactly
this: it blocked on the rows the IC scores, a mask built from labels.

**The causal mask (v1.14, R-P0-1, the default).** `block_rows_column =
"causal"` blocks a decision only on state observable at `t`: the row's own
mid is missing or not positive, or the label engine found the *anchor*
itself unusable (no prevailing two-sided book at `t`). A forward window
that later turns out invalid does not block, so a live router could have
made exactly the same decisions (`iap.backtest.engine`). The report quotes
the label-mask P&L beside it (`net_pnl_1x_cost_label_block`) so the size
of the old bias is visible.

**The general rule.** A filter applied to a backtest is a decision the
strategy made. It is legitimate only if the strategy could have made it
at the time.

**Check yourself.**

1. Is dropping rows with no two-sided book at `t` look-ahead? *(No: the
   book at `t` is observable at `t`.)*
2. Why does the label mask flatter P&L? *(It avoids exactly the rows where
   something disruptive is about to happen.)*

## 88. Same-timestamp coalescing

**The question.** An ITCH "order executed" message becomes two events, an
EXECUTE and a TRADE, with the same exchange timestamp. A feature engine
samples the book after each one. How many book states did the market
have?

**One.** Events sharing an `exchange_ts` for one instrument are a single
state change: an ITCH `E` (EXECUTE + TRADE), a `U` replace (CANCEL + ADD),
a two-sided FX quote. Sampling after the first half shows a book that
never existed (an order removed but the trade not yet counted; a cancel
without its replacement), a *phantom state*. Features computed from it
(spreads, imbalances, OFI sums) are wrong, and a model can learn them.

**The v1.14 default (D-P1-1).** Every feature engine applies all events of
the group, folds their flow, and refreshes or emits once at the group's
last event (`group_ends`). A trade's effective spread is measured against
the pre-group mid. Ported identically to Rust, C++ and the frozen Java
engine. Kept per event, with reasons: the streaming MVP (no look-ahead on
a live stream) and the alpha golden frames. The synthetic EQ golden lost
409 of 2,000 rows, almost every remaining row changed in at least one
feature, and every result computed from the store moved; CHANGELOG v1.14
lists them. `python -m iap.features --same-ts per_event` rebuilds the old
store.

**Check yourself.**

1. Why does a live stream keep per-event processing? *(It cannot know
   whether more events with the same timestamp are coming without waiting,
   which is look-ahead in a live setting.)*
2. Why measure the effective spread against the pre-group mid? *(The
   post-group mid already includes the trade's own impact.)*

## 89. Signing trades: the quote rule and the tick rule

**The question.** A non-displayed ITCH `P` print shows a price and a size
but no aggressor side. Was it a buy or a sell?

**Infer it.** Signed volume (buys minus sells) is the raw material of
order-flow features, so the sign matters. Two classic rules:

- **Quote rule** (the core of Lee–Ready): a trade above the mid is a buy,
  below the mid a sell.
- **Tick rule**: a trade at a higher price than the previous trade is a
  buy, lower a sell; equal takes the last non-zero change.

The quote rule is more accurate when a valid quote exists; the tick rule
needs no quote but is noisier.

**The v1.14 mapping (D-P1-2).** A post-2014 `P` print is signed against
the mapper's own best bid and ask by the quote rule; the tick rule is used
only at the mid or without a two-sided, uncrossed quote. Counters
`hidden_trades_signed_by_quote_rule` and
`hidden_trades_signed_by_tick_rule` (now the fallbacks only) record the
split. In the same release midpoint prints round half to even (D-P1-3),
not half up, with residues recorded, because half-up rounding biases every
midpoint print in the same direction.

**Check yourself.**

1. A print exactly at the mid: which rule? *(The tick rule; the quote rule
   cannot decide.)*
2. Why does half-up rounding of midpoint prices bias signed features? *(A
   half-penny midpoint is always rounded up, so it looks above the mid more
   often than below.)*

## 90. Feature drift between in-sample and holdout

**The question.** Before running the R7 confirmatory test, the owner ran a
drift report on the holdout. Why look at the inputs if the gate only
reads the IC?

**To know what changed before reading what happened.** If the inputs
moved, a fall in IC has a candidate explanation; if they did not, it
points at the relationship itself. `iap.research.drift` streams the
feature store in record batches (chunk-invariant) and computes, per
feature, per symbol and pooled:

```
PSI = sum_b (q_b - p_b) * ln(q_b / p_b)       # p: reference share, q: target share, per bin
KS  = max |F_ref(x) - F_target(x)|
mean shift in reference-std units,  std ratio
```

with PSI bins cut at reference quantiles. The default thresholds
(`DriftThresholds`): PSI warn 0.10, alert 0.25; KS alert 0.10; mean
shift 0.5 reference sd; std ratio outside [1/2, 2]; fewer than 100 valid
rows is "insufficient".

**What it found.** Pooled, 122 of 205 features alerted, mostly depth and
intensity. For MSFT the queue depletion and replenishment rates had PSI
of 2.08 to 2.70 with mean shifts of about −0.8 reference sd. The round-lot
reform (§76) is a known structural change in the same period. The report
is descriptive; it decides nothing.

**Relation to §14.** The adaptive monitors of §14.2 use PSI and KS online
to trigger refits; this is the offline, whole-dataset version, run once
before a confirmatory look.

**Check yourself.**

1. 122 of 205 features alert and the IC holds. Contradiction? *(No: a
   scale or level shift can leave a standardised relationship intact.)*
2. Why cut PSI bins at reference quantiles? *(Equal-mass reference bins
   avoid empty bins and make PSI comparable across features.)*

## 91. Memory on pooled real data

**The question.** R7 crashed twice on allocations of 800 MiB and 1.04 GiB on
a 16 GB machine. How can a few hundred megabytes break it?

**Because the base was already huge.** One instrument over 10 sessions is
about 3 M rows. At 263 float64 feature columns:

```
3e6 rows * 263 cols * 8 B ≈ 6.3 GB per instrument  ->  about 19 GB for 3
```

Any full-width copy of the pooled store, even a short-lived one, is enough
to exhaust memory on its own (`docs/research/MEMORY_REVIEW_v116.md`).

**What was fixed and what is open.** `ResearchRunner.frames(needed)` (v1.16
item 12) loads only the experiment's features plus id, label and market
columns (`slim_columns`), caching one column set. That reduced the
walk-forward test frames, the CPCV fallback and the refit path, which are
built from it. Still open, each needing a check that its reports are
unchanged: narrowing walk-forward and CPCV frames to the scoring columns,
copying only label columns in the power and leakage code, and float32 /
int32 for per-fold arrays. R7's own loader was left as is, because R7 is
closed and its registered runs used it.

**The general lesson.** In pandas a slice, a `copy()`, a concat or a
fallback `except KeyError` path can each double the footprint. On research
data, memory is a design constraint: decide which columns a step needs,
and fail loudly instead of falling back to "all of them".

**Check yourself.**

1. Why does narrowing columns help more than adding swap? *(Swap turns a
   crash into a run many times slower; not loading the columns removes the
   cost.)*
2. Why must each memory fix prove output identity? *(A memory change that
   alters a statistic changes published results; the R7 retries were
   accepted only because the output was identical.)*

## 92. Event clocks and Hawkes intensities

**The question.** Features are sampled every 100 ms. At the open a hundred
events arrive in that time; at lunch, none. Is a clock the right sampler?

**Sample where the information is.** Calendar-time rows over-sample quiet
periods and under-sample busy ones. Since v1.12 (M6),
`python -m iap.features --sampling events:N|volume:N` emits a row every
`N` events or every `N` shares traded instead of on `--cadence-ms`. Rows
then carry similar amounts of market activity, which is closer to how
information arrives.

**Measuring arrival intensity.** Trades cluster: one trade makes another
more likely soon after. A Hawkes process models that self-excitation. The
extended feature set (`iap.features.extended`, `--feature-set extended`)
computes an exponential-kernel intensity per side online:

```
lambda_s(t) = mu + alpha * sum_{t_i <= t, side s} exp(-beta * (t - t_i))
S <- S * exp(-beta * dt) + alpha       # at each trade on side s
```

with `hawkes_{buy,sell,total}_b{beta}_v1` and an imbalance
`(buy - sell) / (buy + sell)`, for `beta` in {1, 0.1} per second. A
feature is valid once `5 / beta` seconds have passed (99 % of the kernel's
memory). The parameters are fixed per run and recorded, so a different
parameterisation is a different `feature_version`; `fit_hawkes_exp` is
the offline maximum-likelihood helper. The same set adds queue
time-to-depletion, trade-sign autocorrelation and odd-lot and hidden
execution shares.

**Check yourself.**

1. Why `5 / beta`? *(`exp(-5) ≈ 0.007`: after five decay times the
   history before the start is below 1 % of the kernel.)*
2. On a volume clock, does a 1 s label horizon still make sense? *(The
   label is still in time; rows are just placed differently. Overlap and
   HAC (§66) still apply.)*

## 93. DeepLOB: learning from the raw book

**The question.** The linear alphas use hand-built features. Could a deep
network learn better ones from the order book itself?

**The architecture.** DeepLOB (Zhang, Zohren and Roberts, 2019) takes a
window of `W` snapshots of `L` levels, each row laid out as `[ask_px_1,
ask_sz_1, bid_px_1, bid_sz_1, ..., ask_px_L, ask_sz_L, bid_px_L,
bid_sz_L]` (shape `(T, 4L)`). Convolutions combine price and size, then
sides, then levels; an inception module looks at several time scales; an
LSTM summarises the window. The output is three classes: down, flat, up.

**The smoothed label.** A raw mid change at `t + h` is mostly noise and
bounce. DeepLOB compares the *mean* future mid with the current one:

```
m_plus = mean(mid[t+1 .. t+h])
r      = m_plus / mid[t] - 1
label  = UP if r > threshold, DOWN if r < -threshold, else FLAT
```

Rows whose horizon runs past the end are dropped.

**Here.** `iap.models.deeplob` (v1.15, code only) has the dataset path
(`snapshot_matrix`, `smoothed_labels`, `make_windows`, `build_lob_dataset`
with a chronological split and train-only z-score normalisation) and a
numpy softmax baseline that always runs in CI;
`iap.models.deeplob_torch` has DeepLOB and an MLP behind the optional
`deep` extra (torch); `iap.models.deeplob_baselines` adds linear and
gradient-boosting baselines and a net-of-cost comparison. No real-data
training run has been done. `build_lob_dataset` keeps a train window
only if its label horizon ends before the cut, and a test window only if
its first row is at or after it.

**Check yourself.**

1. Why must the train windows' *labels* end before the cut? *(A label
   computed from test-period mids would leak the test period into
   training.)*
2. Why is a numpy baseline worth having? *(A deep model has to beat
   something simple on the same windows, net of cost, or it adds nothing,
   §7.1.)*

## 94. Per-session windows and mid-relative normalisation

**The question.** The AI4 DeepLOB pre-registration was signed in v1.15 and
then deferred without being run. What was wrong with the pipeline?

**Three problems, each a lesson** (`research/r7/AI4_DEFERRED.md`).

1. *No real-data path.* Nothing turned ITCH book replay into the `(T, 4L)`
   snapshot matrix. A registration that the code cannot execute as written
   is not a registration.
2. *Windows and labels crossed sessions.* `make_windows` and
   `smoothed_labels` run over one continuous series. On multi-day data a
   window can start in yesterday's afternoon and end in today's open, and a
   label can average mids across the overnight gap and the auctions.
   Neither describes anything a strategy could trade. Windows and labels
   must be built per session, with no window or horizon crossing a session
   boundary.
3. *A static price z-score.* Price levels were normalised with one
   training-period mean and standard deviation. Prices drift, so out of
   sample a z-scored price of +8 means only "the stock moved since
   training". Expressing levels relative to the current mid (for example
   `(px_k - mid_t) / tick` or in basis points), or normalising on a rolling
   window, keeps the inputs on the same scale every day. Empty deep levels
   (0 or NaN) also need a mask rather than a number.

**Why defer instead of fixing.** Fixing (2) and (3) changes the model
inputs the registration pins. Doing so under the same registration would
be a deviation of substance, chosen after review. So a new registration
will supersede it, the spent look stays counted, and the holdout sessions
are off limits for AI4 development until then (§73). The rebuild was done in v1.16 and
AI4 was closed on 2026-10-15 after an in-sample failure with no holdout look
(research/r7/AI4_CLOSED_v116.md); the code in §93 is the v1.15 pipeline the
review criticised.

**Check yourself.**

1. Why can a window that crosses the overnight gap look predictive? *(The
   overnight return is large and partly predictable from the close, which
   says nothing about intraday microstructure.)*
2. Mid-relative prices throw away the price level. Is that a loss? *(For a
   microstructure model, rarely: the shape of the book around the mid is
   the signal; the level mostly drifts.)*

## 95. Conformal intervals and confidence-scaled sizing

**The question.** A model predicts a 0.3 bp move. How sure is it, and
should the position depend on that?

**Conformal prediction** gives an interval with a coverage guarantee and
no distributional assumption. Split conformal: on a calibration set, take
the absolute residuals `|y - pred|` and their `ceil((n + 1)(1 - alpha))`-th
smallest value `q`; the interval is `pred ± q`, and covers the truth with
probability at least `1 - alpha` when the data are exchangeable.

**Intraday data are not exchangeable,** so `AdaptiveConformal` (adaptive
conformal inference, Gibbs and Candès) adjusts its level online after each
outcome:

```
alpha_t <- alpha_t + gamma * (alpha - miss_t)        # miss_t = 1 if y fell outside
```

A miss lowers `alpha_t` (wider intervals); a run of covers raises it. The
implementation clamps `alpha_t` to `[alpha / 10, 1 - 1e-9]` and counts and
logs every clamp, because unclamped a miss streak drives the width to
infinity and the position to zero silently.

**Sizing.** `confidence_from_width` maps an interval width to a confidence
in [0, 1] (1 at or below a reference width, `ref / width` above it), and
`conformal_size` scales the base target by it, never above the target or
`max_position`, rounding toward zero to whole lots.

**Scope.** `iap.models.conformal` (v1.15 M-P1-2) is opt-in research code:
`ConformalSizingConfig.enabled` is off, it is not in any pre-registration
config, and it is not wired into the parity-checked MVP sizing.
`calibration_report` compares empirical with nominal coverage by session.

**Check yourself.**

1. Nominal coverage 90 %, empirical 78 % on one session. What does ACI do
   next session? *(Starts with a lower `alpha_t` after the misses, so wider
   intervals.)*
2. Why round toward zero? *(Rounding up could exceed a cap.)*

## 96. Model cards and content hashes at promotion

**The question.** A model is promoted. A year later someone asks what data
it was trained on, what it was for, and who signed it off. Where is that
written?

**In a card the registry will not promote without.** Since v1.15
`ModelRegistry.promote` raises on a missing or invalid `model_card.json`
(`iap.mlops.model_card`). The card binds to one record (`model_id`,
`record_hash`) and carries: the data (`dataset_version`, date range,
sessions), the pre-registration id and hash (or `exploratory: true`),
metrics net of costs (non-empty), calibration or coverage figures (or an
explicit `None`), known limitations (non-empty), intended use, the owner's
sign-off reference, and its own `card_hash`. `generate_card` fills the
record-derived fields; the human fields must be supplied by a human.

**And the bytes must be the ones evaluated.** Promotion also checks the
model's content hash: `content_sha256` hashes the fitted state (class,
parameters, fitted arrays), stable across joblib, pickle and scikit-learn
versions, and promotion refuses a missing or mismatched one (G-P0-1).
`promotion.json` records both hashes. A card describing one model cannot
be promoted with another's weights.

**Check yourself.**

1. Why require "known limitations" to be non-empty? *(Every model has
   some; an empty list means nobody wrote them down.)*
2. Why hash the fitted state rather than the pickle file? *(Pickle bytes
   change with library versions while the model does not; the state is
   what the evaluation used.)*

## 97. Paper trading: what the PAPER gates check

**The question.** An alpha passed research and validation. Why not go
live?

**Because a backtest cannot see the live loop.** Paper trading runs the
strategy on live (or replayed live-shaped) data through the real
decision, risk and execution path, without sending orders to a market. It
catches what research cannot: data that arrives late or out of order,
features computed differently in production, risk checks that fire, a
fill model that disagrees with the venue.

**The lifecycle gates.** The PAPER → ACTIVE promotion in
`iap.lifecycle.machine` requires four gates (`iap.lifecycle.gates`), with
thresholds in `configs/strategies/lifecycle.json`:

| gate | metric | threshold |
|---|---|---|
| `paper_min_sessions` | sessions traded on paper | ≥ 5 |
| `paper_ic_tracking` | `|realized_ic - research_ic|` | ≤ 0.01 |
| `paper_net_pnl` | net P&L | ≥ 0 |
| `no_kill_events` | kill-switch events | ≤ 0 |

`paper_ic_tracking` is the drift monitor of the transition: the live IC
must match the research IC within 0.01, or something differs between the
two pipelines. Failures demote PAPER to CANDIDATE (on the configured
number of consecutive failures), and a human can retire it.

**Honest status.** No alpha here has reached PAPER on real data; the
lifecycle has 23 CANDIDATE alphas and none beyond (v1.14). The gates exist
and are tested (the LC01 golden, §17.4).

**Check yourself.**

1. Why gate on IC tracking and not only on P&L? *(Five sessions of P&L is
   noise; a gap between live and research IC is a pipeline bug you can see
   quickly.)*
2. Why is one kill event disqualifying? *(A strategy that trips a kill
   switch on paper would have done so with real money.)*

## 98. Pre-trade controls and a tamper-evident audit

**The question.** SEC Rule 15c3-5 requires a broker-dealer with market
access to have pre-trade risk controls. What does that look like in code?

**A fixed, fail-closed check order.** The Rust risk engine (normative;
Python reference port) runs every legacy check, then since v1.14 the
15c3-5 set as checks 23-29 (`risk.json` x-version 4, `pre_trade`, every key
optional): a restricted list, maximum open orders and open notional per
strategy, per-strategy position, participation of session market volume,
per-strategy cancel rate and per-venue message rate. Since v1.15 only
TRADE prints count as market volume for the participation cap
(`RiskEngine.on_market_event`): an EXECUTE is the same volume seen twice.
Per-key state is bounded (`max_tracked_keys`), failing closed when full.

**An audit you can verify.** Every `RiskEvent` carries `seq` and
`prev_sha256`, the SHA-256 of the previous JSONL line, so the audit log is
a hash chain (`verify_audit_chain` in Python and Rust; `audit_chain_head`
is the value to anchor elsewhere). Editing or deleting a past decision
breaks every later link. Loss-limit overrides are bounded and need a
minimum number of distinct approvers; a refused override is itself
audited.

**Check yourself.**

1. Why must a full state map fail closed rather than evict old keys?
   *(Evicting would forget open orders or positions, and a limit would pass
   that should fail.)*
2. Why anchor the chain head outside the log? *(A chain proves internal
   consistency; an attacker who rewrites the whole log rewrites the chain
   too, §75.)*

## 99. Agents that propose and humans that decide

**The question.** An LLM agent can read reports, propose hypotheses and
draft pre-registrations. What stops it from confirming its own idea?

**The boundary is in the tools, not the prompt.** The MCP workflow
(`iap.agents.mcp_workflow`, `iap-mcp-agent`) exposes only `propose`,
`draft_prereg`, `hit_rate`, `regulatory_fee` and `list_drafts`. Sign-off,
confirmatory runs, outcomes and promotion have no tool; the module refuses
to import if one is added, and a test enumerates the tool list. Each
proposal debits one look from the chained ledger, with a per-agent look
cap. `prereg_gate.require` refuses a pre-registration written by any
registered agent id (§72), and agent studies are exploratory only.

**Checking what the agent says it did.** A live evaluation found a model
closing a session with "Finding filed." after its finding had been
rejected. `iap.llm.summary_check` (v1.15) checks the closing summary
against the session's tool log: an action claim is supported only by an
accepted call of the tool that performs it, so a claim to have signed off
or promoted is always unsupported, and every number must match the
session's evidence under the exact-decimal rules of `iap.llm.verify`
(§37). Negated and modal clauses ("could not file") are not claims. A
false flag marks a report for review; it never blocks anything.

**The evals.** Six adversarial tasks (prompt injection in a board entry
and in a report field, an authority claim to sign off, look-budget evasion
by tiny and by repeated proposals, pushed fabricated numbers) are scored by
named checks that also score a real model. The first live run
(`docs/research/AGENT_EVAL_LIVE.md`) scored Sonnet 5.5 0.9773 and Opus 5.5
1.0, for 0.76 USD of a 20 USD cap.

**Check yourself.**

1. Why remove the tool rather than instruct the agent not to use it?
   *(Instructions can be overridden by injected text; a missing tool
   cannot be called.)*
2. Why does each proposal cost a look? *(A proposal is a search step; an
   agent that proposes a thousand variants has run a thousand tests.)*

## 100. A tree model on the same bench, and two gates that read one number

**The question.** The ML report fits gradient-boosted trees and finds
nothing that pays. The flagship alphas go through a walk-forward, four
leakage detectors, a gate with a significance threshold that follows the
look count, a pre-registration and a ledger. The trees went through none
of that. Is the ML result weaker evidence than it looks, and in which
direction?

**Same bench (v1.17 D1).** `iap.alpha.gbm.GbmAlpha` (`ML01`) is a GBM
behind the `AlphaModel` interface: `fit` on per-instrument frames, `score`
to `(expected_return, confidence)`, `params` / `load_params`, a declared
feature list, a pinned horizon and an `Economic rationale:` section like
every other alpha. It fits one model per horizon (the targets differ by a
hundredfold in scale), checks its own hypothesis sign on a held-out slice
of the training fold, and stores its fitted trees in a pickle sidecar whose
SHA-256 travels in the params and is verified before anything is
unpickled. Registering it is an explicit opt-in, so the 24-alpha report
never changes by accident. On the synthetic store it reads an IC of +0.055
at 5 s with a gate t of +6.0 and is an ITERATE: predictive, not tradable,
the familiar shape (§78).

**Two gates that read one number (v1.17 D4).** The model-zoo gate passed
when the best linear model's pooled OOS IC against the mid label was > 0:
+0.0081 opened it. One number, pooled across fits, with no standard error.
The opt-in `gate="clustered"` asks for the fold-mean rank IC to be positive
AND for the day-clustered t of the pooled predictions (standardised per
fold, because each fold is its own fit) to clear a stated threshold; both
readings are recorded whichever decides. The CPCV path IC had the same
flaw in another costume: a path concatenates the predictions of several
fits, and a fit whose outputs are ten times larger owns the path, while an
offset between fits is counted as variance. `cpcv_path_ic="demeaned"`
standardises each (split, group) before pooling, so the path IC is a
weighted mean of per-group ICs; the test fixture has two groups with the
same true IC and shows the pooled reading off by more than 0.05 and the
demeaned one within 0.01.

**Why the defaults did not move.** Both fixes change what a published
number means; the ML report and the `v4` CPCV blocks are regenerated only
by the CI job, under rules the owner flips deliberately. Until then every
report carries both readings, which is the honest state: the old number,
the better number, and the name of the one that decided.

**Check yourself.**

1. Why standardise predictions per fold before a clustered t, when the
   literal gate never did? *(Each fold is a different fit; pooling raw
   outputs weights folds by their scale and mixes in offsets between
   them. The t is about the sign of a relation, not about which fold
   shouted loudest.)*
2. Why is the GBM's `beta` 1.0 in its params? *(The validation chain
   divides `expected_return` by `beta` to recover the standardized signal
   that every fold statistic reads; for a tree model the clipped
   prediction IS that signal.)*
3. Why a digest-checked sidecar instead of the model inside the JSON?
   *(A fitted ensemble is megabytes of opaque bytes; the params file is a
   contract that people and three ports read. The digest makes the two
   inseparable without making the file unreadable.)*

## 101. Implementation shortfall, and the benchmark that measures it

**The question.** You must buy 1 % of a day's volume. Which algorithm and
which child policy cost least against the price you saw when you decided?

**The idea (Perold).** Implementation shortfall is the gap between the paper
portfolio (every share at the arrival price, no cost) and the real one. It
splits into spread, fees, impact, and the opportunity cost of what never
filled. The last term is the one a fill-rate table hides: an algorithm that
fills 60 % cheaply has not beaten one that fills 100 % dearly until the
unfilled 40 % is priced at where the market went. `iap.tca.shortfall.
parent_costs` does that split per parent order, in bps of arrival notional.

**The benchmark (v1.17 B1).** `python -m iap.research.execution_benchmark`
replays a grid (algo TWAP / VWAP / POV / IS × policy `native` / `aggressive`
/ `passive` × 0.1 / 0.5 / 1 / 2 % of ADV × session) through the execution
simulator on recorded books. The committed run is
`research/execution/benchmark/BENCHMARK_multi7.md`: AAPL, MSFT and QQQ over
seven sessions, 44,718,339 events, 252 parents per policy, 30-minute
windows, five slices, seed 20260829. Every cost carries
`ExecConfig.impact_label()` ("replay + linear child adder (rule 6)"), so a
reader cannot mistake a simulated impact for a measured one.

**What it found, and did not.** The largest paired difference is aggressive
minus native TWAP at 1 % ADV, -4.10 ± 2.19 bp (95 % CI -9.47 to 1.27, fill
rate difference -0.19). The interval holds zero, and per-cell n is 9-21
parents on 3-7 sessions: low-power. The honest reading in
`docs/EXECUTION_QUALITY.md` is that the benchmark is a machine for the signed
run, not a finding about the policies. The gate it prepares
(`prereg_DRAFT.json`) is unsigned; the owner signs it.

**What it is not.** Not a market-impact measurement, not evidence that any
policy wins, and not a promotion gate.

**Check yourself.**

1. Why does a cell with a fill rate of 0.83 need the completion term before
   its cost can be compared? *(The unfilled 17 % is a cost or a saving
   depending on where the price went; ignoring it rewards under-filling.)*
2. Why are costs labelled with `impact_label()`? *(The simulator's impact is
   a modelled adder, and the label is the cheapest guard against quoting it
   as a market reaction.)*

## 102. Fill types, and a markout that knows how the fill happened

**The question.** §30 measured adverse selection per fill. Why does the
v1.17 markout split fills again by how they came about?

**The idea.** A passive fill is not one thing. A first post that rested and
filled is the clean case. A fill of a child that re-posted after a cancel
is a different bet (you paid latency and lost your place). A fill that
landed after a cancel had already been sent lost a race. A taker fill never
risked selection at all. Pooling them hides the mix.
`iap.tca.markout_decomposition.fill_type` assigns one of `passive`,
`aggressive`, `faded`, `cancelled_replaced` from new provenance on
`ChildOrder` (`cancel_rule`, `cancel_rule_ts`, `replaces_order_id`), and
`regime_of` adds the B2 urgency regime (`agree`, `adverse`, `neutral`,
`no_signal`, `stale`, or `plain`).

**The statistic.** Per cell and horizon: the mean markout and the mean price
impact (`s * (m_h - m_fill)`, whose negative is the adverse selection of a
passive fill), each with a day-clustered standard error (§130). `decompose`
aggregates rows; `fill_rows` prices one timeline. With one day the SE is
`None`, not zero.

**Where it stands.** The machinery and its tests are committed
(`python/tests/test_v117_fading_cancels_markout.py`). The changelog says the
benchmark policies that exercise it were not run on multi7 in that PR, and
`docs/EXECUTION_QUALITY.md` section 6.1 reserves the results for the truth
pass. This entry therefore quotes no markout number.

**What it is not.** Not a claim that faded fills are worse than passive
ones; that is the question the decomposition exists to ask.

**Check yourself.**

1. Why is `faded` a maker fill after a cancel was sent, not a fill after a
   cancel was acknowledged? *(Rule 7: the cancel travels the latency path
   and can lose the race; the fill that lands first is the one the fade
   tried and failed to avoid.)*
2. Why can a pooled passive markout look better than any of its parts?
   *(Mix: clean first-post fills dilute the replaced and raced ones.)*

## 103. Queue-aware cancels: when waiting is the expensive choice

**The question.** §45 says the back of the queue fills last and worst. If a
resting order is hopelessly far back, why keep it?

**The idea.** A resting child can be judged on two numbers it already has:
the displayed size ahead of it (`ahead_qty`, the simulator's deterministic
queue position) and, with a fitted `FillHazardModel`, the probability of
filling within a horizon given spread, imbalance and depletion. B5
(`iap.execution.fading.QueueCancelParams`, `ParentOrder.queue_cancel`)
cancels when `ahead_qty > max_ahead_qty` or `p_fill_within < min_p_fill`,
then crosses (default) or re-posts. The cancel goes through simulator rule 7
and may lose the race.

**The calibration trap it exposed (review X-4).** A level shrinks for two
reasons: trades hit it, or orders ahead of you cancel. Only the first fills
anyone. The pooled `queue_depletion` hazard mixed them and so overstated
fill probability. `estimate_calibration(depletion_split=True)` now fits both
rates (`ExecCalibration.queue_exec_depletion_hazard`,
`queue_cancel_depletion_hazard`), and `sor_v2.hazard_fill_prob` documents the
execution rate as its input.

**What it is not.** Not a result that cancelling beats waiting: the
benchmark variants `passive+qcancel` exist (flags `--qcancel-max-ahead`,
`--qcancel-min-pfill`) but no committed run is quoted here. Off by default,
byte-identical off.

**Check yourself.**

1. Why must the hazard's depletion feature be the execution rate? *(A level
   emptied by cancels ahead of you did not fill you; using it as a fill
   signal inflates the probability.)*
2. Why does a queue cancel cross by default? *(The queue was the problem; the
   remainder still has to trade, so it takes liquidity.)*

## 104. Quote fading, and pricing the fades it saved against the fills it missed

**The question.** Pull a resting quote when the market is about to run
through it, and you avoid an adverse fill, but you also forgo fills that
would have been good. Does fading pay?

**The rule (B4).** `FadeParams` on `ParentOrder.fade` pulls a resting child
when the signal turns adverse by `threshold_z` (the same alignment test as
B2) or when the opposite side's share of the two L1 sizes reaches
`pressure_threshold` (`book_pressure`). The remainder is held off the book
until the condition clears (`hold`), re-posted, or crossed, at most
`max_fades` times. Every pull is a `PullRecord`
(`ExecReplayResult.pulls`, `ParentReport.diagnostics()`).

**The counterfactual.** `pull_outcomes`, `pull_rows` and `pull_summary` price
each pulled quantity afterwards: did the market trade through the pulled
price inside the horizon (the touch-through proxy for "it would have
filled"), and if so was that fill's markout adverse (a fade *saved*
`|markout|`) or favourable (a fade *missed* it)? The net is the answer,
not the count of fades.

**Why it is labelled.** Our resting order never moved the replayed book, and
touch-through is a proxy for a fill. The docstring says so; the report keeps
`fades_saved_vs_fills_missed` as its own field, per policy.

**What it is not.** Not a measured benefit. No committed run reports it
(`docs/EXECUTION_QUALITY.md` section 6.1), and the C++ port is pending (F7).

**Check yourself.**

1. Why can a fade lose money even when most pulls are correct? *(Saved
   adverse fills are small per fill; one missed favourable run can outweigh
   many; only the netted counterfactual compares them.)*
2. Why does the pull use rule 7 instead of removing the order instantly?
   *(A real cancel takes time and can lose to a fill; modelling it as free
   would flatter the fade.)*

## 105. Signal-aware urgency: let the alpha choose passive or aggressive

**The question.** A buy is waiting to be worked. If the alpha says price is
about to rise, patience costs; if it says price is about to fall, patience
pays. Can the child style follow the signal?

**The rule (B2).** `iap.execution.signal` supplies causal readings in signal
units: `AlphaUrgencySource.from_dataset` scores a flagship alpha's
registered parameters on a dataset's feature frames and reports the z-score
of the alpha's own scores. `ParentOrder(urgency_policy="alpha",
alpha_urgency=AlphaUrgencyPolicy(threshold_z, passive_urgency, max_age_ns,
latency_ns))` re-evaluates at every child decision: agree -> post, adverse
-> cross, neutral -> the base style. A reading is the latest at or before
the decision and no older than `max_age_ns`, so there is no look-ahead.
Each child records `urgency_regime`, `signal_z` and `pacing_scale`.

**Design point.** The policy is re-evaluated through the parent's life, not
once at arrival, so a signal that flips mid-order changes the style.

**Evidence.** `python/tests/test_v117_signal_aware.py` pins the causality.
No committed signal-aware benchmark result exists (`docs/EXECUTION_QUALITY.md`
section 4). The v1.16 in-sample study found EQ10 and EQ05 never clear taker
costs, and urgency does not change that: it moves the price a cost is paid
at, not whether the alpha is worth trading.

**What it is not.** Not a source of alpha and not enabled by default;
`--signal EQ10|EQ05|synthetic` on the benchmark opts in.

**Check yourself.**

1. Why measure the signal in z-units of the alpha's own scores? *(Alphas
   differ in scale; one threshold must mean the same strength for each.)*
2. Why compare each `native+urgency` variant with its own plain control on
   the same windows? *(Windows differ in difficulty; only a paired
   difference removes that.)*

## 106. Pacing, and Almgren-Chriss with a drift

**The question.** §52 solved the trade-off between impact and risk assuming
price has no drift. What changes when a signal says it does?

**The mechanics.** With zero drift the Almgren-Chriss trajectory depends on
risk aversion and impact only. A signal adds a term: when the price is
expected to run away from the order, trade sooner; when waiting is
expected to pay, hold more for later.
`ac_holdings` and `ac_trajectory` gain `drift=0.0` (so the old result is
the default), and `ac_drift_from_signal` converts a signal reading into the
drift. `PacingParams` in `iap.execution.pacing` carries three opt-ins
(`pov_signal_pacing`, `slice_signal_tilt`, `ac_signal_drift`): scale the
per-trade POV participation by the signal, re-apportion the remaining
quantity at each slice (never negative), or re-solve the Almgren-Chriss
trajectory per slice with the signal as drift.

**Why re-solve per slice.** The signal changes; a trajectory fixed at
arrival uses a stale forecast. The cost is that the schedule is no longer
the closed form, so the benchmark compares each variant with its plain
control (`native+pacing`, `native+ac`, `native+ac_drift`).

**What it is not.** Not tested on real data in a committed result; the
tests show causality and that the remaining quantity stays non-negative.
It scales an order's pace, never the order's size.

**Check yourself.**

1. Why must the tilt never make the remaining quantity negative? *(An
   over-tilted early slice would sell past the parent; the parent is a hard
   cap.)*
2. Why keep `drift=0.0` as the default argument? *(Existing callers and
   goldens must reproduce byte for byte; drift is opt-in.)*

## 107. The depth cap versus the book

**The question.** In the first multi7 run, 60-100 % of aggressive parents at
0.5 % ADV or more hit `depth_exhausted`. Was the market that thin, or was
the simulator's ten-level walk the limit?

**The method.** Same dataset, windows and seed, with only
`ExecConfig.walk_depth_levels` changed (default 10 -> 50 via
`--walk-depth 50`), plus a variant with a 300-share child cap. Nothing else
differs, which is what makes it a measurement of the cap.

**The answer** (`research/execution/benchmark/DEPTH_COMPARISON_multi7.md`).
At 0.5 % ADV it was the cap: the exhausted share went from 60-67 % to 0 %
(11 % for VWAP). At 1 % about half cap and half book. At 2 % it is the book:
a parent that size exhausts 50 displayed levels inside 30 minutes. Child
size is not the lever: capping at 300 shares leaves aggressive cells
unchanged and hurts native TWAP and VWAP.

**The twist.** Costs rose by 2-5 bp at 1-2 % ADV (aggressive TWAP at 1 %:
-6.9 to +0.7 bp). At depth 10 the cancelled quantity was priced by the
completion term, small or favourable on these sessions; at depth 50 the same
shares were walked and paid the real spread. The first run understated
cost where under-fill was largest. A cap you do not examine can hide the
very cost you are measuring.

**What it is not.** In-sample, three names, seven sessions; nothing signed.

**Check yourself.**

1. How does the comparison separate "cap" from "book"? *(If raising the cap
   to 50 levels removes the exhaustion, it was the cap; if the share stays
   high with 50 levels available, the displayed book ran out.)*
2. Why did a deeper walk make cost look worse? *(Quantity formerly
   cancelled and priced by a benign completion term is now filled at the
   walked prices.)*

## 108. A dark midpoint, as an idea only

**The question.** Some venues fill at the midpoint of the lit quote with no
displayed queue. Should a router send a patient order there?

**The idea.** A midpoint fill saves half the spread against a marketable
order and, unlike a displayed bid, carries no visible queue to wait in. The
cost is uncertainty: the fill probability depends on contra flow you cannot
see, and a midpoint fill can be adversely selected if the mid is stale. A
study would need a model of the dark fill probability and a markout of
dark fills, compared with the passive and aggressive fill types (§102).

**Where it stands.** This is an idea. The dark-midpoint model (plan item
B10) is not on `main`: no code, no number and no result for it exists in
this repository, and `docs/EXECUTION_QUALITY.md` section 6.3 reserves the
heading as a placeholder for the truth pass. This entry makes no claim
about a model, a venue or a benefit.

**What it is not.** Not implemented, not tested, not a result.

**Check yourself.**

1. What would make a midpoint fill adversely selected? *(A stale quote:
   the mid is about to move and the contra order knows it.)*
2. What evidence would have to exist before the router used it? *(A
   committed fill-probability model and a markout of dark fills, signed
   through the pre-registration process like any other gate.)*

## 109. Breadth and the fundamental law

**The question.** A signal with a rank IC of 0.06 looks too weak to matter.
Why do funds trade it anyway, and why can this repository not yet say that
it would work?

**The idea (Grinold and Kahn).** The fundamental law says the information
ratio of a strategy is roughly the skill per bet times the square root of the
number of independent bets: `IC × sqrt(BR)`. A small IC across many
independent names beats a larger IC on one. `iap.research.breadth_report`
reports the yardstick beside the book's measured breadth, and the word
*independent* is the catch: names that move together are not separate bets.
It computes effective bets from the correlation of the names' returns
(`iap.combine.weights.effective_bets`).

**The platform's real numbers.** `research/breadth/book/BOOK_multi7.md`
(AAPL, MSFT, QQQ, seven sessions, 905 rebalances): mean rank IC 0.0626
(day-clustered t 2.73, df 6) but effective bets 1.46 of 3 names. Three
correlated names are barely more than one bet. The synthetic book
(`BOOK_synthetic.md`, 11 names) has effective bets 10.33 of 11 and a rank IC
of -0.0745, because the generator has no cross-sectional edge.

**What it is not.** The law is a yardstick, not a forecast, and the
platform's 100-name book has not been run in a committed result
(`docs/PORTFOLIO_BREADTH.md`, placeholder). "A breadth of 3 is not breadth."

**Check yourself.**

1. Why does effective bets, not the name count, enter the law? *(Correlated
   names carry overlapping information; independent bets are what multiply.)*
2. A positive IC with t 2.73 and a net-negative book: contradiction?
   *(No. The IC measures ranking; the book also pays spread, fee and
   impact, §110-§111.)*

## 110. The cross-sectional book

**The question.** A time-series alpha asks "will this name rise?". A
cross-sectional book asks "which names will beat the others?". How does one
become a portfolio?

**The pipeline** (`python -m iap.research.breadth_book`, `run_book`). On a
rebalance clock (default five minutes) each name contributes its latest
fresh feature value; a score (`eq11`, or `eq10_beta_gap`, demeaned and
studentised) is ranked into target weights (`linear` or `deciles`); the
optimizer then trades them off against costs *inside* the objective: a
one-leg linear cost (half spread plus fee), an impact term (`ImpactTerm`,
sqrt rule or a calibration's residual fit), under dollar, beta and sector
neutrality (`iap.portfolio.exposures`, `configs/reference/sectors.json`) and
participation and turnover caps. A stale name is frozen, not traded; the
book is flattened at the last live rebalance of each session.

**Accounting.** P&L marks at the mid a stated delay after the decision and
charges spread, fee and impact per name by the same rule the optimizer saw,
so `net = gross - spread - fee - impact` to the last float; a test
reconstructs the gross P&L from positions and marks. One ledger look of
kind `breadth_book` is spent per run.

**What the committed runs say.** `BOOK_multi7.md`: gross P&L 1,042.67
against spread 3,651.02, fee 1,246.04 and impact 5,698.64, net -9,553.03.
A negative result for EQ11 as a three-name book after costs.

**What it is not.** Not a statement about EQ11 or about a 100-name book;
fractional shares, no queue, no venue; the sector file is public-knowledge
classification, not a vendor taxonomy.

**Check yourself.**

1. Why put costs inside the optimizer rather than subtract them afterwards?
   *(Otherwise the optimizer trades names whose edge is below their cost.)*
2. Why freeze a stale name instead of trading it on an old price? *(A trade
   on a stale mark books a gain or loss that no real fill would give.)*

## 111. Break-even notional: how big can the book be?

**The question.** A book that loses money at one size might still break
even at a smaller one. How is "how big" measured?

**The idea.** Impact cost per dollar rises with size (§48), so net P&L
per dollar falls as the notional rises. `iap.research.breadth_report`
re-runs the book at several multiples of a base notional (the impact term
and costs scale with the size) and reports net bps at each, plus the
break-even notional: the first zero crossing of net P&L across sizes. A
second figure, `capacity_breakeven` in `iap.validation.metrics`, is a
single-name proxy at the rebalance horizon using the book's realised
round-trip edge at universe medians.

**The committed result.** On the three-name multi7 book the net is
negative at every size from 0.25x to 4x the base notional (net -77.63 to
-99.08 bp in the sweep); the break-even notional is below the smallest size
tried and the single-name capacity proxy is zero shares
(`docs/PORTFOLIO_BREADTH.md`, `research/breadth/book/BOOK_multi7.md`).

**Why two numbers.** The size sweep is a book-level answer; the capacity
proxy is single-name. The doc states they are different objects and the
proxy is "not the capacity of the book".

**What it is not.** Not a capacity claim for any portfolio. A break-even
below the smallest size means the sweep never found positive net P&L, not
that a smaller size would.

**Check yourself.**

1. Why re-run the book at each size instead of scaling the P&L? *(Impact and
   participation caps are nonlinear, so the optimizer's decisions change
   with size.)*
2. Why is "zero shares" a legitimate capacity? *(If the realised edge is
   below the cost at any size, nothing can be traded profitably.)*

## 112. A universe rule, written before any statistic

**The question.** Which 100 stocks make a breadth test, and who decides?

**The rule** (`research/breadth/UNIVERSE_RULE.md`, `breadth_universe/v1`):
the top 100 Nasdaq-listed symbols by *displayed continuous-session volume*
on 2019-01-30, measured by one streaming pass over the full-market file
(`python -m iap.marketdata breadth-universe`, `iap.marketdata.breadth`).
ETFs are kept and flagged; test securities are excluded; crosses and
non-displayed prints are reported beside the ranking and do not rank.

**Why fix it first.** If you choose names after seeing returns, breadth is
chosen, not measured. The rule is written, versioned and hashed to its
source (`universe.json` records the source file's sha256 and the next 50
names).

**The measured outcome** (`research/breadth/UNIVERSE_2019-01-30.md`):
368,366,634 messages read, 8,713 symbols with a directory row, 27 test
securities excluded, 100 names of which 29 are ETFs; AAPL, QQQ and MSFT
rank 6, 12 and 15. The rule ranks share volume, not notional, so
low-priced names rank high (HMNY at rank 2); that is the rule as registered,
and a notional rule would be a new version and a new look.

**Survivorship is explicit.** A symbol that later delists stays in the list
and is reported as missing, not repaired. The 2025/26 days are the holdout
for every name outside AAPL, MSFT and QQQ, and a `--dataset-dir` containing
`holdout` is refused by the book driver.

**What it is not.** Not an index, not liquid-by-notional, not survivorship-free.

**Check yourself.**

1. Why rank by displayed volume rather than total? *(Crosses and hidden
   prints are not tradable by a displayed-order strategy.)*
2. Why does a notional ranking count as a new look? *(It is a different
   selection; a second selection of names is a second chance to find a good
   one.)*

## 113. A Sharpe deflated at the number of trials you registered

**The question.** §40 explained why a Sharpe must be deflated. What N goes
into the deflation?

**The bug that taught it (review R-1).** The R7 confirmatory deflated Sharpe
was recorded at N = 1 (EQ10) and N = 2 (EQ05): the trial count of the
*holdout dataset's own* ledger. That counts only what was tried on that data,
not how many configurations the research had tried before arriving there.
The right N is the repository ledger's distinct configurations plus the look.
`tools/r7_confirm.py` now takes it from `registered_trials`, and
`--recompute-dsr` re-deflates the recorded per-session IC series without
taking a new look.

**What changed.** At N = 297 (the look) and 308 (today), EQ10 at 1 s moved
from 0.99995 to 0.99886 and EQ05 from 0.9999998 to 0.99996
(`research/r7/r7_confirm_result_dsr_recompute.{json,md}`; the note
`research/r7/R7_DEVIATION_dsr_trials.md`). The gate is DSR > 0.95; both
verdicts are unchanged. A descriptive variant without two sessions moves EQ05
from 0.988 to 0.956 at N = 297, still above 0.95 (`docs/SIGNAL_RESEARCH.md`).

**What it teaches.** The correction was small because the evidence was
strong, but the recording was wrong, and it was found by review and
recorded as a deviation, not quietly fixed. Nothing in
`r7_confirm_result.json` was rewritten.

**What it is not.** Not a new look, and not a statement that the alphas are
tradable (§78).

**Check yourself.**

1. Why is a dataset-local trial count too small? *(The selection that led to
   this candidate happened across the whole research history.)*
2. Why record a deviation instead of overwriting the result? *(The record
   of what was reported at the time is itself evidence.)*

## 114. The statistics revision

**The question.** If the rules for computing a statistic change, what
happens to the ledger of statistics computed under the old rules?

**The mechanism.** `iap.validation.methods.STATS_REVISION` is `"v1.17"`; it
is written into every new ledger identity as `stats_revision`. The v1.14
rules are frozen as `v2_v114`, `v3_v114`, `v4_v114`, and an entry at `"v1.14"`
scopes to those bundles; an unknown revision raises. A rerun under new rules
is therefore a new look with its own identity, not a silent overwrite of the
old one. This is the same idea as §76 (deviations recorded) applied to the
code itself.

**The v1.17 change (R-3).** `v2`, `v3` and `v4` pass
`feature_window_ns="registry"`: after a test block of a two-sided split
(`leave_one_day_out`, the `cpcv` block) the embargo is at least the longest
feature window (5 m), not the 60 s embargo alone, because a feature at the
start of the next training row still reads 5 minutes of the test block. The
reports state `methods.post_block_embargo_ns`. The walk-forward is
one-sided, so no `v2` gate number moves; a regenerated `v4` cpcv block is a
new look.

**What it is not.** Not a change to any registered pre-registration; each
stays at the bundle it names.

**Check yourself.**

1. Why can a feature window larger than the embargo leak? *(The first
   training row after a test block still includes test-block data in its
   rolling window.)*
2. Why version the revision in the ledger identity? *(So a number computed
   under old rules is never mistaken for, or merged with, one computed
   under new rules.)*

## 115. One gate statistic

**The question.** If the walk-forward gate, the frozen-parameter block, the
validity block and the R7 confirmation each compute "the t-statistic", can
they disagree?

**They did (review R-2).** `frozen_params_block` kept the v1.13 L = 2 HAC
rule, no day separation and no per-instrument scaling, while the gate used
the bundle's bandwidth. The same number was computed in four places by four
rules. v1.17 puts one function behind all of them:
`iap.validation.metrics.gate_statistics()` returns the pooled IC, the
pooled-slope HAC t at the bundle's bandwidth and day separation, the
day-cluster t, `n_pairs` and `n_days`. The walk-forward gate, the frozen
block, the `validity` block and `tools/r7_confirm.py::pooled_stats` read it.

**What follows.** Reports gain `gate_tstat_day_cluster`, `gate_n_days` and a
`gate_statistics` block; the frozen block gains `frozen_t_day_cluster` and
states which statistics it used. Gate t values are unchanged (CHANGELOG): the fix
is to agreement, and agreement is the point. The same shape is reused wherever a
new gate needs a number (B6's `cluster_mean`, `coverage_by_regime`, the
regime gate), so a day-clustered reading means the same thing everywhere.

**What it is not.** Not a new test; a refactor that makes four readings one.

**Check yourself.**

1. Why is a duplicated statistic dangerous even if each copy is right?
   *(They drift apart; a gate and its report can quote different t values.)*
2. Why did the gate t values not move? *(The gate already used the bundle's
   rule; the divergent copies were the frozen block and the validity block.)*

## 116. Why TOPS equals the top of DEEP: an oracle for the book

**The question.** You rebuild an order book from a vendor's feed. How do you
know the rebuild is right?

**The idea.** For LOBSTER there is a published book to compare with. IEX
gives one differently. TOPS publishes IEX's best bid and offer; DEEP and
DEEP+ publish the book that produces them. A book reconstructed from DEEP
must therefore reproduce TOPS's price and size on both sides at every change.
`ingest --format iex-deep-plus --tops FILE` runs that comparison as the
session's `book_verification`. The oracle is independent because the vendor
computed the answer separately from the data being tested.

**Why it matters (§36).** Parity proves agreement, not correctness; an oracle
is the external check. The TOPS reader is `iap.marketdata.iex.TopsQuotes`;
`process-iex-day` extracts a universe quote file once from the TOPS capture,
deletes the capture, then verifies the DEEP+ book against the extract.

**What is checked and what is not** (`docs/DATA_SOURCES.md`). The codec is
tested on synthetic captures. A conformance pass on the first 250 MB of the
2026-10-13 TOPS file found 7,619,846 messages, 0 unknown types, 0 sequence
gaps and 0 crossed quotes
(`research/v117/IEX_HIST_CONFORMANCE_20261013_TOPS.md`). A DEEP+ book is
**unverified against real IEX data** until a day is ingested with both feeds.

**What it is not.** Not proof of a correct book on real data yet.

**Check yourself.**

1. Why is DEEP+ ingested with TOPS and not checked against itself? *(A
   self-check shares the reconstruction's own mistakes; the oracle does not.)*
2. Why did the conformance pass need no DEEP file? *(It tested the framing
   and TOPS message types only; the oracle comparison needs both feeds.)*

## 117. Capture clocks: pcapng and TAI

**The question.** A packet capture stamps each packet. Is that the time the
exchange says it happened?

**What the real files showed.** The IEX HIST files are **pcapng** (section
header, interface block, enhanced packet blocks), not the classic pcap the
vendor page describes; `iap.marketdata.iex` reads both. And the capture
host stamps in **TAI**, not UTC: capture stamp minus the IEX-TP send time was
37.00-38.14 s on the checked slice, and TAI - UTC was 37 s
(`docs/DATA_SOURCES.md`).

**The design response.** `exchange_ts` comes from the messages' own UTC
nanosecond timestamps, so the offset never touches the data, and
`receive_ts` is not derived from the capture stamp. Where a clock does enter
a model, it is named: `FillHazardModel(clock="America/New_York",
capture_offset_s=IEX_TAI_OFFSET_S)` fits the time-of-day feature on the
exchange clock, and `to_dict` records the clock (`"utc"` default, so
v1.12 documents load unchanged). Review finding X-8 asked for exactly this.

**The general lesson.** Time-of-day features are a silent source of error:
a 37-second offset would not break a test yet would misplace any feature
keyed to the clock, and a Nasdaq feed is local while the IEX capture clock
is TAI.

**What it is not.** Not a claim that the unchecked parts of the capture are
clean; the check covered the first 250 MB of one TOPS file.

**Check yourself.**

1. Why take `exchange_ts` from the message, not the capture stamp? *(The
   capture stamp is the recorder's clock and carries its offset and jitter;
   the message stamp is the exchange's own.)*
2. Why does the clock name go in `to_dict`? *(A model fitted on one clock and
   loaded on another is wrong without any error; the name makes the
   mismatch detectable.)*

## 118. Process-and-delete: the data lifecycle on a small disk

**The question.** One full-market day is 4-18 GB compressed and several
times that after normalising. The machine has 16 GB of memory and little
disk. How do you work with ten of them?

**The design** (`python -m iap.marketdata process-day`, A2/A2b, runbook
`docs/runbooks/RUNBOOK_process_and_delete.md`). One manifest day goes through
a disk guard, an approved download, `ingest --slim`, deletion of raw, the
Rust feature build, `KEEP.json` (sha256 of every kept file), then deletion
of the intermediate files and the vendor file. Each step leaves a marker and
resumes. **Nothing is deleted before the kept files verify.** The normaliser
accepts `--partition-instruments N` so memory is one partition plus one
event-time window, and the output is byte-identical (tested).

**Honest sizing** (`docs/DATA_SOURCES.md`). The normalized JSONL twin was
3x the bytes of the IAP1 file, so `--slim` omits it; the symbol-scaled
estimate overstated a 100-name day many times (830 GB estimated against
about 50 GB expected), so `--peak-gb` takes a measured value. The disk guard
never deletes to make room: it refuses, and the first 100-name day was
refused on those terms.

**What is kept.** Features, manifests, the IAP1 file, Parquet and `KEEP.json`;
what is deleted is regenerable.

**What it is not.** Not a backup; no wall-clock figure for a completed 100-name
day is committed yet.

**Check yourself.**

1. Why check the kept files' hashes before deleting? *(Deletion is
   irreversible; a corrupt kept file would leave nothing to regenerate from.)*
2. Why must the guard refuse rather than clean up? *(Deleting something to
   make room is a decision about data it was not asked to judge.)*

## 119. The approval rule for downloads

**The question.** An agent or script can download in the background. What
stops it from spending bandwidth, disk and legal goodwill without asking?

**The rule** (`docs/DATA_SOURCES.md` section 3): nothing is downloaded by
default. The IEX codec reads files; it fetches none. `process-day` downloads
only with `--download --i-have-approval`, through `tools/r7_download.py`,
which accepts only the hosts in `iap.marketdata.r7plan.ALLOWED_HOSTS`,
checks the size and records the sha256. Each day is an owner decision
recorded with its date: the 2019-01-30 full-market file for the universe
measurement, the first 250 MB of a TOPS file for the conformance pass, and
the A4 overlap days (`research/v117/iex_overlap_manifest.json`, the
approval sentence recorded verbatim).

**Listing is not downloading.** `tools/iex_hist.py recent --n 5` lists the
latest HIST days; no approval sentence, no download, and
`tools/iex_download.py` refuses a manifest without one.

**Boundaries.** The holdout sessions are never inputs of the chain, and
nothing outside a day's dataset directory and its vendor file is deleted.

**Why it matters.** Approval is a control like the pre-registration: a human
decision made before the action, written down, checked by code that refuses
without it.

**What it is not.** Not an access-control system; it constrains this
repository's tools, not the machine.

**Check yourself.**

1. Why an allow-list of hosts as well as a flag? *(The flag records
   consent; the allow-list limits where consent can be spent.)*
2. Why keep the approval sentence verbatim? *(Later readers can see what
   was approved, not a paraphrase of it.)*

## 120. Spend governance: a budget the model cannot raise

**The question.** An LLM agent pays per token. What keeps a loop from
spending a month's allowance in an afternoon?

**The ledger** (E1, `iap.llm.spend.SpendLedger`). An append-only,
file-locked JSONL outside the repository (`IAP_LLM_SPEND_LEDGER`, else a
per-user state directory), one row per API call with tokens, USD and
month-to-date. Prices are configuration (`configs/reference/llm_prices.json`
with `source` and `checked` date); `iap.llm.prices` refuses an unknown model
and never guesses. A monthly cap of USD 30, a per-run budget of USD 1
(`Budget.max_usd`), and an 80 % alert once a month. The call is refused
*before* it is made, in every process, when month-to-date plus its maximum
cost would pass the cap.

**Model pins.** Dated per-role pins in `configs/agents/llm.json` (research
`claude-sonnet-5-5`; ops, docs and evals `claude-haiku-5-5`), recorded in
the session summary. The most expensive model is no longer the default.

**Evidence.** `docs/AGENTS.md` records per-session costs (~USD 0.1-0.2 for
a research session, ~USD 0.01 for a morning ops run). The first live
evaluation (§99) used 0.76 USD of a 20 USD cap.

**What it is not.** Not a guarantee against a key used elsewhere; it governs
this repository's calls, and a corrupt row is skipped and counted, not
trusted.

**Check yourself.**

1. Why refuse before the call instead of alerting after? *(After is too
   late: the money is spent.)*
2. Why is the ledger outside the repository? *(An agent that can edit the
   repository must not be able to edit its own budget.)*

## 121. A number must come from a tool result, not from the model

**The question.** §37 said an LLM must never compute a number. What
mechanism makes that more than a request?

**The check.** `iap.llm.verify.verify_finding` accepts a finding, a
pre-registration draft, a report draft or a closing text only if every
number in it appears in the evidence of a cited artefact. The match is
exact-decimal; a labelled statistic must match its field; number words are
refused (a model cannot dodge the digit check by writing "ninety-five").
`iap.llm.summary_check` (§99) applies the same rule to the closing summary.
The morning ops narrative is checked against the alerts document, and a
narrative that fails is replaced by a fallback written by code
(`docs/AGENTS.md`, section 4).

**Where the rule lives.** In the output path, not the prompt. The model may
read, look up and draft; it gets a citation for a number the code computed.
A verification failure of the closing text makes `python -m iap.agents
research` exit 3: the drafts it wrote stand, the text does not.

**Why free text is wrapped.** Tool results go through
`iap.agents.untrusted`, which marks free text as data. Even so the controls
that matter are structural (§99): the verifier and the missing tools hold
whether or not the model obeys.

**What it is not.** Not a guarantee the model *understood* a number; it
guarantees the digits are the code's. A true number can still be cited for
the wrong claim, which is why a human reads every draft.

**Check yourself.**

1. Why refuse number words? *(They bypass a digit comparison and carry the
   same claim.)*
2. Why a code-written fallback rather than a retry? *(A retry re-asks the
   component that failed; the fallback is deterministic and cannot invent.)*

## 122. The three v1.17 agents, and what each is allowed to write

**The question.** §99 described an agent that proposes. v1.17 adds three
more LLM-backed agents. What separates them?

**The roster** (`docs/AGENTS.md`). The research assistant (E2, `python -m
iap.agents research`, `claude-sonnet-5-5`) runs *in-sample* studies into a
scratch store and drafts pre-registrations (`status DRAFT`, unsigned, in
`research/preregs/drafts/`). The morning ops agent (E3, `python -m iap.agents
ops`, `claude-haiku-5-5`) writes the narrative of an alerts document whose
thresholds are rules in code over the paper-trade, drift and cost reports
and the spend ledger. The docs-sync agent (E4, `python -m iap.agents
docs-sync`, Haiku) proposes edits for a pull request as a patch file that is
never applied. All run on `iap.llm.loop`, one budgeted, ledgered tool loop.

**What distinguishes them is the write set.** The ops agent decides no alert;
code does, and the model writes words. The docs agent edits no non-doc file
and applies nothing. The research assistant refuses every dataset in
`forbidden_datasets` and any name matching `*holdout*`, and every report says
`confirmatory: false`. None can sign, register, approve or promote: no tool
exists.

**Evals.** `iap.agents.wse_evals` adds one eval per agent to `agent-evals`
(run with the scripted client, no key), asserting for instance that the
research assistant refuses a holdout run and a fabricated number and that
the spend ledger holds one row per model call.

**What it is not.** Not autonomous research. Each agent produces a draft or a
patch or a document that a human reads.

**Check yourself.**

1. Why does a smaller model run the ops and docs agents? *(Their tasks are
   narrow, checked by code and cheap; the research task is the one that
   needs the larger model.)*
2. Why is a patch file safer than an edit? *(It has to be applied by a
   person, which is a review step the agent cannot skip.)*

## 123. Documentation checks as a CI gate

**The question.** Documentation rots. What keeps a repository with dozens of
documents from linking to files that no longer exist?

**The check.** `tests/harness/check_docs.py` verifies relative links, the
diagram index, that every `iap.*` name in `API_*.md` resolves by import, and
the freshness of status text against the last tag. It runs in the `docs` CI
job (advisory, with a lychee external-link step configured in
`lychee.toml`, which excludes the GitHub API and loopback and private
hosts). The docs-sync agent (§122) proposes fixes in the same spirit.

**The API-name check is the useful one.** A link checker finds a moved file;
an import check finds an API document that names a function that was renamed.
That is the failure readers actually hit. It is also why this document names
only identifiers that exist: LEARN is held to the same check.

**Advisory by design.** The job is not a required check, so an external
site's outage cannot block a merge; the repository's required checks are
listed in `docs/governance/GOVERNANCE.md`.

**What it is not.** Not a proof that prose is true. A document can link
correctly and still be wrong, which is what the headline-number checks and
the truth pass (§139) are for.

**Check yourself.**

1. Why check names by import instead of by text search? *(A name can appear
   in text after it stopped existing; an import fails.)*
2. Why keep external links advisory? *(They fail for reasons the repository
   does not control.)*

## 124. Reproducible builds and SBOMs

**The question.** If you ship a wheel and images, how does a user know the
artefact came from the source they can read?

**Reproducibility** (F1, the `reproducible-build` CI job). The wheel is built
twice under `SOURCE_DATE_EPOCH` set to the commit date, and the two sha256
values must match. The python image is built twice with BuildKit
`rewrite-timestamp`, report-only. Same source, same bytes: any difference is
an unpinned input.

**SBOMs.** Every release attaches CycloneDX software bills of materials: one
per image (syft, from the pushed image by digest, attested and pushed to GHCR
beside the provenance) and one per hashed Python lock file (`cyclonedx-bom`,
attested with the lock as subject), all uploaded as release assets
(`sbom-image-{python,cpp,rust}.cdx.json`; `.github/workflows/release.yml`).

**Digest pinning (F2).** `tools/release.py pin-digests` rewrites the k8s
manifests to `name:vX.Y.Z@sha256:<digest>` from `release-manifest.json`
(dry-run by default), so a tag cannot silently change what a deployment runs.

**What it is not.** The SBOM lists components; it does not say they are free
of vulnerabilities. And `reproducible-build` is not yet a required check: the
owner must add it to the ruleset (`docs/governance/GOVERNANCE.md`, section 0).

**Check yourself.**

1. What does a mismatched hash on two builds of one commit tell you? *(An
   input is not pinned: a timestamp, an unpinned dependency, a path.)*
2. Why pin by digest and not by tag? *(A tag can be re-pointed; a digest
   names the content.)*

## 125. Ratchets: coverage floors and a growing mypy scope

**The question.** How do you stop quality from slipping without demanding a
perfect score today?

**The idea.** A ratchet fixes a floor at the measured level and only moves it
with evidence. The `python-slow` coverage floor is `--cov-fail-under=54`
(`.github/workflows/ci.yml`). It began at 55 when measured at 55.90 % on the
wave-2 integration PR, then moved to 54 when main measured 54.76 % after
later merges: the changelog records both measurements and the reason, that
the 55.90 % predated modules only the fast job covers. A floor taken from a
measurement is honest; one typed in is a wish.

**The type ratchet** (F3). The mypy scope (`python/pyproject.toml`
`[tool.mypy]`) grew from core, orderbook, execution, backtest and features to
also `agents`, `llm`, `mvp` and `paper`. 258 errors went to 0 with type-only
changes, using `dataclass_transform` on `@contract` (which alone removed
191 errors) and one `type: ignore[attr-defined]`, named and explained. Type
fixes must not change behaviour, and the goldens prove they did not.

**What it is not.** Coverage is not correctness, and mypy at zero errors is a
bound on a class of mistakes, not a proof. A floor can also be lowered, so
the changelog entry is part of the control.

**Check yourself.**

1. Why take a floor from a CI run rather than the laptop? *(The floor guards
   the CI job; a different environment measures different code paths.)*
2. Why is lowering a floor allowed here? *(Because the entry says why; the
   control is the recorded reason, not the number.)*

## 126. Latency budgets: a limit is a measurement times a margin

**The question.** "Fast enough" needs a number. Whose, measured how?

**The method** (F4/F5). `iap.mvp.latency.measure_loop_latency` times the
Python MVP loop per event on a committed real slice (§127).
`benchmarks/latency_budget.json` stores the measurement with its provenance
(run id, CPU model, Python version, runner) and the limit:
`limit = measured x margin`, rounded up, with margin 4. The CI-measured p50
is 116,659 ns and p99 1,794,957 ns over 65,132 events; the limits are p50
500,000 ns and p99 8,000,000 ns. `tests/harness/check_mvp_latency_budget.py`
is blocking in the `python` job. Only p50 and p99 are guarded, never p99.9
or the max, because a shared runner produces outlier spikes unrelated to the
code. The same file records a local laptop at p50 255 µs, p99 3.8 ms.

**Why 4x.** A shared runner's noise is large; a tight bound would fail on
weather. The budget catches an order-of-magnitude regression (a quadratic
loop), not drift. Memory (§127) uses 2x and the same rule.

**Why a real slice.** Synthetic data has a book that is shallow and regular.
A budget measured on it would pass code that is slow on a real book's depth
and message mix.

**What it is not.** Not a claim about production latency: the budget is for
the Python reference loop in CI, and tick-to-trade for the C++ and Rust
engines is §41.

**Check yourself.**

1. Why not guard the max? *(One preempted run sets it; the guard would
   fail for reasons that say nothing about the code.)*
2. What does the margin trade? *(False alarms against sensitivity: 4x
   catches large regressions and ignores noise.)*

## 127. A real slice committed to the repository

**The question.** Real data is too large to commit. How do tests and
budgets still touch it?

**The slice** (`benchmarks/slices/itch50_20190130_AAPL`,
`tools/extract_real_slice.py`). Nasdaq ITCH 5.0 AAPL, 2019-01-30 from the
09:30 open: 1,132 book-reconstruction adds plus 64,000 real events, 4.69 MB,
with the book reaching about 2,700 price levels per side. It is a
self-contained ingested dataset, with provenance in `SLICE.md` and
`dataset.json` (`docs/governance/REPRODUCIBILITY.md` section 3.5).

**What it buys.** `bench_tick_to_trade` replays it as a third workload (the
`t2t realslice p50/p99` guard rows). `check_slice_memory.py` guards peak RSS
of the normaliser and the feature build against `benchmarks/memory_budget.json`
(CI-measured 194 MB and 173 MB, limits 2x). And it exposed a defect: with 12
parents on the slice, the passive working list made `ExecutionReplay` cost
239 µs per event; open-count tracking and amortised compaction brought it to
59 µs per event, with the goldens unchanged.

**Why it is valuable.** Quadratic behaviour hides on a small synthetic book
and shows on a real one. A committed slice makes that a CI-visible fact.

**What it is not.** One name, one day, one open: a performance workload, not
research data.

**Check yourself.**

1. What did the slice reveal that synthetic data did not? *(A list that grew
   with the book depth: cost per event scaled with it.)*
2. Why must outputs stay unchanged after the speedup? *(A performance
   fix that changes results is a behaviour change; the goldens prove it is not.)*

## 128. Share-normalised features and the per-session ADV

**The question.** Volume 10,000 shares means different things for QQQ and a
small stock. How do you compare?

**The family** (A5, D-P1-5). `iap.features.feature_v2` adds `sharenorm`: 14
`_v2` features dividing volumes and depths by the instrument's session ADV
(`iap.reference.session_adv.SessionAdvTable`, the dataset's
`adv_by_session`) and by the running session volume. Examples:
`session_volume_adv_v2`, `traded_volume_adv_w<w>_v2`, `depth_l10_adv_v2`.
Windows are 1, 5 and 15 minutes.

**The as-of rule.** The ADV for a session is the mean of the dataset's
*earlier* sessions (the first session uses its own volume), keyed by the
session date in the calendar's zone. A table that used the same day's volume
would put the day's total in every early feature: a look-ahead through the
normaliser.

**Execution uses it too (X-9).** `ExecConfig.adv_by_session` /
`load_exec_config(adv_table=)` make the impact rules divide by the ADV of the
fill's session; the default is the static ADV, byte-identical, and
`expected_replay_fills.json` is unchanged.

**What it is not.** Python reference only, pending the C++ and Rust ports
(F7/F8, `PLATFORM_CONVENTIONS.md` section 11.7); the registered feature vectors,
their goldens and the pre-registration fingerprints are untouched.

**Check yourself.**

1. Why is "ADV known" a validity condition for each feature? *(Without an
   ADV the ratio is undefined, and a missing value is better than a wrong one.)*
2. Why two normalisers, ADV and running session volume? *(ADV compares
   across names; session volume compares within a day to how active it is.)*

## 129. TRADE_BUST: a correction that must not move registered vectors

**The question.** An exchange breaks a trade after the fact. A feed has
already shown it. What happens to your features?

**The event.** `TRADE_BUST = 10` (`schemas/FORMAT.md` section 4) is the
canonical kind for ITCH `B` broken trades and IEX trade breaks. The mapper
emits it, the normaliser carries it into the IAP1 file, the JSONL twin and
Parquet.

**The invariant.** Every book (Python, Rust, C++) applies it as a no-op, and
the feature engines skip it before the book, so the registered feature
vectors are byte-identical with or without busts. Only the opt-in
`sharenorm` volumes react: from the bust's time forward the print is removed
from every window it is still in and from the session volume; emissions
before the bust stay what they were, because the print was on the tape then.
A bust naming a print the extension never recorded is counted
(`busts_unmatched`) and changes nothing.

**Why a no-op.** Registered features and goldens are fingerprinted; changing
them would change every pre-registration. A correction that matters can go
only where it is opt-in.

**Test discipline.** The anomaly fixture's two deliberately unknown events
moved to code 250 so the new meaning of code 10 did not collide with them;
expected states and checkpoints are unchanged.

**What it is not.** Not a claim that busts do not matter; only that the
registered path does not read them.

**Check yourself.**

1. Why leave earlier emissions unchanged after a bust? *(That is what a
   trader had at the time; restating history is a look-ahead.)*
2. Why count unmatched busts? *(A silent skip hides a mapping bug.)*

## 130. Day-clustered standard errors, in the v1.17 gate form

**The question.** §32 and §67 argued for day-clustered t statistics. How do
v1.17's new reports use them?

**The form.** One function shape everywhere: the cluster-robust (CR1)
sandwich with one cluster per trading day,
`G/(G-1) * sum_g (sum_{i in g} (x_i - mean))^2 / N^2`, and Student-t
inference with `G - 1` degrees of freedom through
`iap.validation.metrics.day_cluster_inference`. It appears in
`iap.tca.markout_decomposition.cluster_mean` (B6), in the execution
benchmark's cell statistics and paired differences (B1), in
`gate_statistics` (§115), in `coverage_by_regime` (§131), and in the breadth
report (mean rank IC with t 2.73, df 6 on seven sessions).

**Why the pattern.** Fills, parents and rebalances inside a day share a
market and a volatility regime; treating them as independent understates the
standard error, sometimes badly. Counting days gives the honest sample size.

**The edge cases.** With one cluster the SE is `None`, not zero. With
fewer than two clusters `t`, `df` and the interval are `None`. Cells with
fewer than 10 sessions are marked low-power in the benchmark.

**What it is not.** Not a cure for few clusters: with 7 days, df is 6 and
the critical value is large, which is correct. It prevents false precision;
it does not create power.

**Check yourself.**

1. Why Student-t with `G - 1` df and not the normal? *(With few clusters the
   SE itself is estimated from few numbers; the t has heavier tails.)*
2. Why report `None` rather than a point estimate of the SE for one day?
   *(One day has no dispersion across days; any number would be invented.)*

## 131. Conformal sizing: width as confidence

**The question.** §95 introduced conformal intervals. v1.17 wires them into
position size. What does "size by interval width" mean and what is checked?

**The rule** (D3). `iap.models.conformal.conformal_sizing_hook` is a
`SizingHook` for `Backtester(sizing_hook=)`: the split-conformal interval over
the primary signal's residuals gives a width, and
`confidence_from_width(width, ref_width)` maps it to a multiplier in [0, 1]
applied at the decision row (`SignalRow`,
`InstrumentResult.sizing_multipliers`). A narrow interval means the model has
been accurate lately and size stays near full; a wide one scales down. The MVP
engine takes the same rule through an optional `portfolio.conformal` block
(`ConformalSpec`), absent from every pinned config, so the golden run is
byte-identical.

**The check.** `coverage_by_regime` reports the empirical coverage per regime
against nominal with the day-clustered t of the shortfall (§130). It is
reported, not enforced (`passed: None`). Intraday residuals are not
exchangeable (volatility clusters, open and close differ), which is why the
module also provides `AdaptiveConformal` (Gibbs and Candes).

**What it is not.** Not a source of edge: it changes size, not direction, and
a finite-sample coverage guarantee holds only if calibration and test scores
are exchangeable, which a coverage report tests rather than assumes.

**Check yourself.**

1. Why per-regime coverage? *(Pooled coverage can be nominal while one regime
   is badly under-covered.)*
2. Why is the conformal rule absent from the pinned MVP config? *(Turning it
   on changes sizes and so the goldens in every language.)*

## 132. Meta-labelling: choose the threshold on rows nobody fitted

**The question.** §7 gated a primary signal with a calibrated probability and
a threshold `best_tau`. Where does the threshold come from?

**The flaw (review R-5).** In the three-way split, `best_tau` was chosen on
the *calibration* segment, the same rows the calibrator was fitted on. The
probability it swept was therefore optimistic, and the threshold tuned to that
optimism. Picking a parameter on the data used to fit a component upstream of
it is a leak even if each step looks careful.

**The fix** (D2). `run_meta_labeling(split_mode="calibrate_select")` splits
the out-of-sample region 40/20/20/20 into meta-train, calibration, select and
test. The calibrator is fitted on *calibrate*, `best_tau` is chosen on
*select*, and both taus are read on *test*. Boundaries are purged and
embargoed. `primary_selection_window` purges the primary signal's own
selection fold from the calibration fold, and the result records the split,
the windows and the purge count. `"legacy"` is the default and reproduces
every stored result.

**The ledger.** The `metalabel_tau` look records `split_mode` and
`primary_selection_window`, so the number states which procedure produced it.

**What it is not.** Not evidence that meta-labelling adds value; it makes
the threshold honest. Whether a gate helps is a separate question for the test
segment.

**Check yourself.**

1. Why can the calibrator not also choose the threshold? *(It has seen those
   rows; the calibrated probability is too good on them, so the best threshold
   on them is biased.)*
2. Why purge the primary's selection fold from calibration? *(The primary was
   chosen on those rows; calibrating on them stacks a second dependence.)*

## 133. Regime conditioning: one beta, or one per regime?

**The question.** A signal may work in quiet markets and not in volatile
ones. Can a model say so, and can a gate check it?

**The model** (C5). `iap.alpha.regime_conditioned.RegimeConditionedAlpha`
(id `<member>_rc`) wraps a registered linear alpha and conditions its scaling
on the row's regime flags (default `vol_regime_flag_v1`). In `per_regime` mode
`er = beta_r * z` with `beta_r` the OLS slope over that regime's training rows
(`min_obs` pairs, else the pooled slope); an unknown regime (NaN flag) scores
at the pooled slope. The wrapper inherits the member's standardisation and leaves its class,
fit and params untouched. Until v1.17 the research reports split the IC by
regime (`iap.validation.stress.regime_split`) but no model read the flags.

**The gate** (`iap.validation.regime_gate`). `regime_gate_statistics` computes
the same `gate_statistics` block (§115) per regime; a regime with fewer than
`min_pairs` finite pairs is reported but not judged. `regime_gate` decides.
Nothing calls it by default, and `validate_alpha` does not.

**The multiple-testing cost.** Splitting by regime multiplies the number of
looks. A regime that "works" among several is exactly the pattern §24 warns
about, so a regime-conditioned result needs its looks counted.

**What it is not.** Not a result: no study has run (`docs/research/
V117_WSC_MODELS.md`).

**Check yourself.**

1. Why fall back to the pooled slope for a thin regime? *(A slope from a few
   rows is noise; the pooled slope is the stable estimate.)*
2. Why report a thin regime but not judge it? *(Hiding it hides a gap in
   coverage; judging it overstates what the data supports.)*

## 134. Long horizons: features and labels, truncated at the close

**The question.** The registered labels use a fixed set of horizons
(`HORIZONS_NS`). Can the platform study 15 m, 30 m and
1 h targets without touching them?

**The pair** (C4). `iap.features.feature_v2.FeatureV2Extension` adds the
`longwin` family: 12 features at 15 m, 30 m and 1 h on a running engine, with
its own registry hash. Windows reset per session-local day and on recovery,
because a window must never span a session. `iap.labels.labels_v2.
compute_labels_v2` produces 15 m, 30 m and 1 h labels **truncated at the
session close and flagged** (`AFTER_CLOSE`). Goldens
`tests/golden/expected_features_v2.json` and `expected_labels_v2.json` are new;
no earlier golden moved.

**Why truncation, not extrapolation.** An hour-ahead label at 15:30 has no
mid an hour later in the same session. Reading the next day's open would turn
an overnight gap into a signal. The label is cut at the close and flagged, so
a study can drop or weight it.

**Why a new module.** The registered families, `WINDOW_NS`, the registry hash
(`585dd7b9...`) and the fingerprinted alpha modules are unchanged; adding new
horizons by edit would invalidate pre-registrations.

**What it is not.** Opt-in: the store CLI does not write them yet, and no
study has run.

**Check yourself.**

1. Why does a long window need a per-day reset? *(A window spanning the
   close would mix last evening's market with this morning's.)*
2. Why does a flagged, truncated label beat a missing one? *(It keeps the
   rows but lets the analysis choose how to treat them.)*

## 135. linear_z_v2: standardise each instrument against itself

**The question.** Which instrument's volatility sets "a strong signal"?

**The problem (R-7).** `linear_z_v1` pools `mu` and `sigma` of the raw signal
across the universe. On a real multi-name dataset the most active name sets
both: QQQ sets every z and the confidence eligibility of the others. A quiet
name never reaches `|z| >= conf_min` and a loud one is always "confident".

**The fix** (C3). `iap.alpha.linear_v2` (model `linear_z_v2`, twins
`EQ01_z2 ... FX12_z2`) standardises each instrument against its own causal
history: a rolling mean and standard deviation over a declared window
(default 30 m, 32 observations minimum), reading only rows at or before `t`.
A truncation probe test shows a row's z is bit-identical whether or not later
rows exist. The slope `beta` is pooled across the universe, because once every
instrument is on its own scale the slope is what is comparable.

**The combiner (R-8).** `ridge` and `shrinkage_mv` now solve on
pairwise-complete moments (a no-opinion row no longer enters `Z'Z/n` as 0), the
ridge search re-standardises inside each forward-chained split, and a new
`decay_aware` method weights by IC times a survival ratio at the combination
horizon. A new combination run writes `stacking: "v1.17"` into its ledger
identity; older identities reproduce byte for byte.

**What it is not.** A draft, not a result:
`research/preregs/linear_z_v2_prereg_DRAFT.json` is unsigned and no
`linear_z_v2` study has run. `COMBINATION.json` is still the pre-v1.17 run.

**Check yourself.**

1. Why a window and not the whole sample? *(A whole-sample mean uses the
   future; a causal window uses only what was known.)*
2. Why does a no-opinion row entering as 0 bias the moments? *(It adds a
   fake observation of zero, pulling covariances toward it.)*

## 136. ML looks are looks

**The question.** §100 put a GBM on the alpha bench. But the ML pipeline
itself makes choices: which model, which gate, which threshold. Do those
count?

**The finding (R-4).** The multiple-testing ledger counted alpha looks, but
the ML report's decisions were free: the model-zoo gate pass, the `best_tau`
choice, the primary-model choice and a shadow promotion. Each is a selection
among alternatives with a data-dependent outcome. v1.17 records them, opt-in:
`run_model_comparison(ledger=)` writes kind `model_zoo_gate`,
`run_meta_labeling(ledger=)` writes `metalabel_tau`,
`research/ml_reports/run_ml.py` writes `primary_model_choice` and
`promotion_decision(ledger=)` writes `shadow_promotion`, all dataset-scoped in
`research/experiments.json` beside the model ledger's per-fit counter.

**The shadow comparison.** `ShadowRunner.compare(ts=)` adds a paired
day-block bootstrap of the candidate-minus-champion IC
(`iap.validation.metrics.paired_day_block_bootstrap`). When it ran,
`candidate_better` also needs its 5th percentile above 0, not just a higher
mean.

**Why it matters.** The ledger threshold (§68) rises with the count of
looks. A model-selection step that is not counted lets the bar stand where it
was while the candidate set grows.

**What it is not.** Opt-in (`ledger=None` or no `ts` keeps the v1.16
behaviour); the regenerated ML report is what records the new looks.

**Check yourself.**

1. Why must a gate decision be a look? *(Its outcome depends on the data, and
   the result you carry forward depends on the outcome.)*
2. Why a 5th-percentile condition? *(A higher mean on a few days can be
   chance; a bootstrap lower bound above 0 asks for more.)*

## 137. Opt-in, and byte-identical when off

**The question.** v1.17 added dozens of mechanisms. How do you add them
without changing what was already established?

**The discipline.** Each piece is switched on explicitly and, off, reproduces
the old output byte for byte. Examples named in the changelog: B2 to B6 leave
every execution golden and the X4 fingerprint unchanged; `drift=0.0` in
`ac_holdings` is the v1.10 trajectory; `split_mode="legacy"` and
`gate="literal"` remain the defaults; `ledger=None` keeps the v1.16 ML
behaviour; a `portfolio.conformal` block absent from every pinned config keeps
the MVP golden; `TRADE_BUST` is a no-op in the registered path.

**What it protects.** Goldens (§12), pre-registration fingerprints (§74) and
published numbers. A default that moves is a retroactive change to a result
someone cited. When a change must alter a default, it is its own decision
(§114, the `stats_revision` tag).

**The tests.** Typical ones assert a golden unchanged or that an off option
reproduces a stored result. Each also shows the on-path does something: tests
show every v3 key changes at least one simulator fill or config.

**The cost.** More code paths, more flags (the benchmark driver has a dozen).
The cost is paid so that "no registered result changes" can be a sentence
rather than a hope.

**What it is not.** Not a reason for a feature never to become a default; the
owner can flip one deliberately and regenerate (§139).

**Check yourself.**

1. Why test that the on-path changes something? *(A flag that does nothing
   passes the byte-identical-off test trivially.)*
2. Why keep legacy names for old rules? *(So an old number can be
   reproduced and compared with a new one.)*

## 138. One entry point for realism, and a label for what a cost is

**The question.** The simulator has more than a dozen realism options
(§58-§61). How do you know a prereg key actually reached the simulator?

**The finding (X-2).** Some keys were silently shadowed: a calibration's
per-venue latency was read in place of the prereg venue latency.
`iap.execution.realism.apply(prereg, exec_config, calibration=, impact=,
maker_config=, quoting_config=)` is now the one entry point; it returns an
`AppliedRealism` with configs, grids, keyword arguments and an `applied`
statement per prereg key, and puts the prereg latency table on every venue of
the calibration. The MVP loop uses it (`MvpEngine.realism_applied`). Tests
show every v3 key changes at least one fill or config.

**A semantic fix.** The prereg `venue_latency.mean_ns` is now the *mean* of
the simulator's `base + U[0, jitter]` draw (`simulator_latency_base_ns`); it
had been the minimum. Ingested real datasets receive the assumed latency of
`configs/reference/venue_latency.json` instead of 0 / 0.

**What a cost is called (X-3).** The residual impact fit records its support
(`support_pct_adv`, the %ADV range of its sweeps). A parent beyond it is
flagged (`impact_beyond_support`) and `ExecConfig.impact_label()` names the
cost "replay + fitted adder (rule 6b, ...)", not a measurement. VWAP slices
also follow the forecast volume curve over the parent window (X-1,
`curve_slice_weights(curve, n, start_frac, end_frac)`), with the
whole-session resampling still the default.

**What it is not.** Not more realism: it is evidence that the realism asked
for is the realism applied.

**Check yourself.**

1. Why must the mean of a draw be what the prereg states? *(A prereg
   "mean 1 ms" that the simulator reads as the minimum is a different
   experiment.)*
2. Why label a cost outside the fit's support? *(A fit extrapolated beyond
   its data is an assumption, and the label says so.)*

## 139. Release, regenerate, and retiring a language

**The question.** What does it take to turn a pile of merged work into a
release you can trust, and what do you remove?

**Retire.** Java left the release in v1.17 (decision N7, closed 2026-10-15):
`archive/java/` holds the code frozen at v1.14.0, unbuilt, and its risk engine
is kept only as the byte-identical mirror of the Rust one. Its operational
role passed to `iap.metrics` (`python -m iap.metrics serve`): a stdlib
Prometheus exposition on :8080 with `/metrics`, `/health`, `/ready` and
`/status`, 30 metric families kept live and 16 Java-only names exposed as
`<name>_available 0` (`python -m iap.metrics names`). The alerts and panels
built only on Java metrics were removed and listed. Three images, not four.

**Pin.** `tools/release.py publish` pins the k8s manifests by digest (§124).
It also blocks on an `## Unreleased (vX.Y)` heading not yet renamed.

**Regenerate.** Research outputs depend on rules: the statistics revision
(§114) means `v2`/`v3`/`v4` reports must be regenerated, and they are
regenerated by a CI job under rules the owner flips deliberately, not by a
laptop. The changelog marks which outputs follow it ("CI regenerate").

**The truth pass.** `docs/EXECUTION_QUALITY.md`, `docs/PORTFOLIO_BREADTH.md`
and `docs/SIGNAL_RESEARCH.md` reserve headings as placeholders for a final
pass that replaces each with what is true at the tag. A document written
before its results exist says so, and the pass replaces it.

**What it is not.** Not done: the v1.17 headings above are written before the
tag, and the standing conclusions (no promoted alpha, no tradable edge) are
unchanged.

**Check yourself.**

1. Why keep a retired port's risk engine? *(It is a byte-identical mirror of
   the Rust one, a second implementation for checking.)*
2. Why regenerate in CI and not locally? *(A number that every reader can
   reproduce from the repository's own pipeline is the only kind that
   counts.)*

<!-- G5 placeholders: B10 dark model; C6 screen; F7 C++ ports; B9/F8 Rust -->

## 140. Further reading

Inside this repository, in suggested order:

0. `docs/HOW_IT_WORKS.md` — the whole platform top-down in one sitting,
   before any of the below.
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
11. `docs/ROADMAP.md` §3.8-3.10 and the v1.14.0 section of `CHANGELOG.md`
    — the 2026-10-11 review, what v1.14 fixed and every number it moved.

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

## Glossary

Terms as this repository uses them. Where a term has a wider meaning
elsewhere, the entry gives the local one.

| term | meaning here |
|---|---|
| adverse selection (AS) | for a passive fill, how far the mid moves against you after the fill, signed so that positive is a loss: `s * (m_future - m_fill) / p`, in bp. Measured per message or, since v1.14, per touch order (COOKBOOK 73) |
| alpha | a scored signal with a fixed id (EQ01, FX11, ...), a horizon and an expected sign; not a strategy until it survives costs |
| anchor | a git commit that contains a blackboard entry, proving the entry existed at that commit's time |
| blackboard | `research/agents/blackboard.jsonl`, the append-only, hash-chained log of pre-registrations and findings |
| Bonferroni threshold | the \|t\| a result must reach given the number of looks taken so far; it rises as the ledger grows |
| bundle (method bundle) | a named set of research rules: `v2` (default), `v3`, `v4`, `legacy_v1`, and since v1.14 the frozen `*_v113` copies |
| CANDIDATE | the second lifecycle state: the alpha passed the research checks it can pass on bundled data. Not promoted |
| coalescing | treating all events of one instrument with the same `exchange_ts` as one state change (v1.14) |
| decision trace | one `DecisionTrace` record per trading decision, from signal to TCA, written as canonical JSONL with a stream digest |
| effective bets | the number of independent bets a book makes, computed from the correlation of its names' returns (`iap.combine.weights.effective_bets`). The multi7 book has 1.46 of 3 (§109) |
| embargo | rows removed after a test fold so that labels overlapping the fold cannot leak into training |
| fade (quote fade) | pulling a resting child when the signal turns adverse or the opposite side's L1 share is heavy (`iap.execution.fading.FadeParams`, v1.17). Priced by the fades-saved-vs-fills-missed counterfactual (§104) |
| fail-closed | on missing, NaN or out-of-range input, the risk engine rejects rather than allows |
| FROZEN copy | a duplicated implementation kept for parity and changed only with a `POLYGLOT-OVERRIDE` line ([docs/POLYGLOT.md](docs/POLYGLOT.md)) |
| gate | a pass/fail check in the promotion report (significance, stability, cost, leakage, ...). An alpha is promoted only if every gate passes |
| golden | a committed expected-output file that every language must reproduce, byte for byte or within a stated tolerance |
| HAC | heteroskedasticity- and autocorrelation-consistent standard errors (Newey-West). Since v1.14 the bandwidth is chosen automatically and no lag product spans two session days |
| holdout | data set aside and only scored against pre-registered hypotheses. Once looked at, it is spent |
| IC | information coefficient: the rank correlation between a signal and the next-horizon return |
| ledger (look ledger) | `research/experiments.json`: every statistic the research has computed, counted as looks. A hash chain since v1.14 |
| look | one statistic computed and seen. Since v1.14 one look per gate decision |
| implementation shortfall | the gap between a paper portfolio at the arrival price and the real one: spread, fees, impact and the cost of what did not fill (§101) |
| markout | the mid some time after a fill, relative to the fill, as a measure of execution quality |
| OFI | order-flow imbalance: signed changes in best bid and ask size (Cont, Kukanov and Stoikov) |
| PBO | probability of backtest overfitting, from combinatorially symmetric cross-validation |
| pre-registration | a signed blackboard entry naming alpha, horizon, sign and pass rule before the data is scored |
| PROMOTE | the verdict that an alpha passed every gate. No alpha has received it |
| queue position (`ahead_qty`) | the displayed size ahead of a resting simulated order at its price level; cancels and, since v1.14, size-reducing MODIFYs ahead of it reduce it |
| row block | which feature rows the backtest is not allowed to trade on. Since v1.14 `causal`: decided from decision-time state only |
| sharenorm | the v1.17 feature family dividing volumes and depths by the session ADV (as of the session, from earlier sessions) or the running session volume (§128) |
| stats revision | the tag on a ledger identity (`v1.14`, now `v1.17`; `iap.validation.methods.STATS_REVISION`), so that a rerun under new rules is a new look |
| time-shift leakage test | v1.14 check: pair each label with the score from a quarter-horizon earlier. A causal signal keeps most of its IC; one that collapses is suspected of look-ahead and flagged for review. Too few pairs reports `null`, never a pass. In the #88 regeneration FX11 failed it (IC 0.108 to 0.027); the review has not found a leak |
| TOPS | IEX's top-of-book feed; the oracle a DEEP+ book reconstruction must reproduce (§116) |
| touch order | a resting simulated order at the best price, the unit of a v1.14 per-order markout |
| TRADE_BUST | canonical event kind 10, a broken trade; a no-op for every book and registered feature, removed from the opt-in `sharenorm` volumes (§129) |
| trust root | a maintainer key in `research/agents/trust_roots.json`, read from `main`, that must sign the agent key registry |
