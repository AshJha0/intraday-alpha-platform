# Queue-Aware Execution and Adverse Selection: Fill Economics from a Deterministic Simulator

*Intraday Alpha Platform research series, paper 5 of 6 (spec §28). Generated 2026-08-29 from the repository's committed research artifacts.*

---

## Abstract

We document the platform's queue-aware execution model and what it measures
about passive versus aggressive fill economics. The production-grade
simulator (C++ reference, ported to Java; pinned rules in
`cpp/include/iap/execution/execution.hpp`) tracks a resting order's queue
position deterministically from public events — depletion by observed
executions, full-amount cancel decrementation, trade-through and
crossed-book completion, expansion of marketable adds — on top of
latency-delayed order arrival and displayed-depth walking for aggressive
orders. On the pinned golden vector, a passive VWAP parent (4 maker fills,
400 shares) completes with *negative* explicit cost (-$0.80 in rebates,
zero impact), while an aggressive IS parent (3 taker fills, 600 shares)
pays $1.80 in fees plus impact — a 0.5 cents/share (~2 bps) explicit-cost
swing between the two styles before spread capture is counted
(`tests/golden/expected_replay_fills.json`). The research TCA harness on
the same golden streams (36 parent orders) measures mean implementation
shortfall of 20.6 bps on the equity instrument against 0.45 bps on FX, and
post-fill markouts of ≈ -24 bps (equities) that are *flat from 100 ms to
10 s* — marketable fills pay temporary impact that fully reverts,
implying the passive counterparty suffered no adverse selection and
captured the entire effective spread (`research/tca/TCA_REPORT.md`). A
participation regression yields 1.5 bps of additional cost per unit
child-participation, though with little explanatory power on this run
(R² 0.10). The Perold IS decomposition is enforced as an exact identity to
1e-9 (`tests/golden/expected_tca.json`). Data is synthetic; the
mean-reverting generator makes reversion-dominated markouts the expected
regime, and we flag where that biases conclusions pro-passive.

---

## 1. Introduction

Execution research needs two things the backtest layer cannot provide: a
defensible model of *when a passive order actually fills* (queue position),
and measurement of *what fills cost after the fact* (TCA, spec §19). The
platform implements both deterministically — same seed, bit-identical fills
— so that every number in this paper is reproducible and cross-language
tested.

## 2. Data and systems under study

**All market data is synthetic.** Two pinned golden event vectors drive
everything here: `tests/golden/events_eq_mbo.jsonl` (2,000 MBO events,
instrument 1, tick 0.01) and `events_fx_quote.jsonl` (800 QUOTE/TRADE
events, instrument 101, tick 1e-5). Two distinct simulators consume them:

1. **The event-driven execution simulator** (spec §§17-18; C++ reference
   `cpp/src/execution/execution.cpp` with pinned rules documented in
   `cpp/include/iap/execution/execution.hpp`; Java port under
   `java/src/main/java/com/iap/execution/`). Its output on the golden
   vector is itself a golden artifact
   (`tests/golden/expected_replay_fills.json`).
2. **The research TCA harness** (`python/src/iap/tca/simulator.py`), a
   deliberately simple marketable-slice simulator (4 children 15 s apart,
   depth-dependent impact ticks, 15% per-child unfill probability,
   SplitMix64 seed 20260829) whose purpose is to generate a realistic
   parent-order population for the TCA metrics in
   `research/tca/TCA_REPORT.md` (24 equity + 12 FX parents).

We keep the two rigorously separate below, as the artifacts themselves do
("This is NOT the production backtester" — `simulator.py` docstring).

## 3. The queue model

The pinned passive-fill rules (quoted from the normative comment block in
`cpp/include/iap/execution/execution.hpp`) are the core intellectual
content of the simulator:

- **Rest:** when a LIMIT remainder rests at price P, `ahead_qty` :=
  displayed quantity at (side, P) on that venue at rest time.
- **Depletion:** an observed EXECUTE at (side, P) reduces `ahead_qty` by
  its full quantity; leftover execute volume after `ahead_qty` hits zero
  fills our order at P (partials supported).
- **Cancels:** an observed CANCEL at (side, P) reduces `ahead_qty` by its
  full amount (deterministic — no probabilistic split), floored at zero.
  This is optimistic for us (real cancels may come from behind us); the
  choice is pinned and disclosed rather than tuned.
- **Trade-through:** an EXECUTE on our side at a price worse than P fills
  us completely at P — the market traded through our level.
- **Marketable ADD expansion:** an incoming ADD whose limit crosses the
  pre-event opposite best is expanded into the per-level volumes it
  consumes, applying the two EXECUTE rules level by level (the replayed
  book matches such adds internally without emitting EXECUTEs).
- **Crossed-display completion:** after an event, if the venue's opposite
  best crosses P, the order fills at P — with a pinned *exemption* for an
  order that rested while the display was already crossed by its own
  aggressive consumption, so displayed liquidity is never double-counted.
- **MODIFY:** ignored for queue position (a modified order's priority is
  unknowable from the public stream — pinned, disclosed).

Aggressive orders walk displayed top-10 depth of the target venue best
price first (one fill per level), never mutate the replayed book, and are
charged linear impact economically instead. Order arrival is
latency-delayed: decision + risk + wire legs (50/50/100 µs) plus per-venue
mean latency and a seeded jitter draw per submission
(`expected_replay_fills.json` description; `configs/venues.json`).

## 4. Results

### 4.1 Passive vs aggressive economics on the golden vector

`tests/golden/expected_replay_fills.json` pins the full fill-by-fill output
of two parents worked over the same 2,000-event equity stream (seed
20260829): parent 1, a VWAP BUY of 400 in 4 passive LIMIT slices joining
the best bid; parent 2, an IS SELL of 600 in 3 front-loaded MARKET slices.

| parent | style | fills | qty | avg price | fees/rebates | impact | total explicit cost |
|---|---|---|---|---|---|---|---|
| 1 (VWAP buy) | 100% MAKER | 4 | 400 | 24.4950 | -0.800 (rebate) | 0 | **-0.800** |
| 2 (IS sell) | 100% TAKER | 3 | 600 | 24.5081 | +1.800 | +0.0018 | **+1.802** |

Per share, the passive parent *earns* 0.2 cents (maker rebate,
`maker_rebate_per_share`, `configs/venues.json`) while the aggressive
parent pays 0.3 cents (taker fee) plus impact — a 0.5 cents/share explicit
swing, ≈ 2.0 bps at the ~$24.50 price level, before counting the half-spread
each style pays or captures. The passive parent nonetheless completed all
400 shares within its horizon — but only because the queue model's
depletion/trade-through rules fired; over the 2,000-event window its 4
child slices filled at prices spanning 24.48-24.51, i.e. queue-aware
passivity bought cost savings at the price of timing uncertainty.

### 4.2 TCA on the simulated parent-order population

From `research/tca/TCA_REPORT.md` (36 parents over the golden streams;
research harness of §2, item 2):

| metric | instrument 1 (equity) | instrument 101 (FX) |
|---|---|---|
| parent orders | 24 | 12 |
| mean fill rate | 0.854 | 0.833 |
| mean total IS | 20.63 bps | 0.446 bps |
| mean delay cost | 0.000 bps | 0.000 bps |
| mean trading cost | 19.84 bps | 0.445 bps |
| mean opportunity cost | +0.791 bps | +0.001 bps |
| mean arrival slippage | 23.40 bps | 0.533 bps |
| mean exec alpha vs VWAP | -23.70 bps | n/a |

The Perold decomposition (delay + trading + opportunity = total IS) is not
approximate: it is enforced as an identity to 1e-9 by golden test
(`tests/golden/expected_tca.json`, `tolerance: 1e-09`), and the attribution
note in TCA_REPORT.md further splits trading cost into spread + impact +
timing. The all-marketable schedule underperforms interval VWAP by 23.7 bps
on the equity instrument — the tape's passive participants did better —
which is the aggregate mirror of §4.1.

### 4.3 Adverse selection: the markout table

Post-fill markout, mean bps, marketable fills
(`research/tca/TCA_REPORT.md`; markout = side × (mid(t_fill+δ) − fill px) /
fill px):

| δ | instrument 1 | instrument 101 |
|---|---|---|
| 100 ms | -24.10 | -0.54 |
| 1 s | -24.12 | -0.54 |
| 10 s | -23.93 | -0.54 |

Two findings. First, markouts are *negative* at every window: after our
aggressive fills the price reverts — we paid temporary impact and spread;
prices did not continue against the passive side. In adverse-selection
terms, the resting counterparties on this dataset experienced none: they
sold at our bid-lifting prices and watched the mid come back, keeping the
effective spread. Queue position on this generator is an asset, not a
liability. Second, the markout is already fully formed at 100 ms and flat
to 10 s — the "temporary" impact component has no measurable decay
horizon at this sampling rate, and its magnitude (~24 bps) approximately
equals the mean arrival slippage (23.40 bps): what aggressive execution
paid is what reverted.

### 4.4 Impact versus participation

Signed fill cost regressed on child participation
(`research/tca/TCA_REPORT.md`):

| instrument | slope (bps per unit participation) | intercept (bps) | R² | n fills |
|---|---|---|---|---|
| 1 (equity) | 1.531 | 22.17 | 0.099 | 82 |
| 101 (FX) | 0.000 | 0.54 | 0.003 | 40 |

On the equity book the recovered slope is positive (+1.5 bps per unit
participation over a ~22 bps base) but explains little of the per-fill
variance on this run (R² ≈ 0.10): with the shared-efficient-price
generator, spread and timing dominate per-fill cost dispersion and the
depth-dependent impact-tick rule contributes only a small recoverable
signal. On the FX quote book, displayed sizes are large relative to child
sizes and the effect vanishes entirely.

### 4.5 Dispersion, not just means

The per-order tables in `research/tca/TCA_REPORT.md` show wide dispersion
around the aggregates that any per-order TCA consumer should expect. On
the equity instrument, total IS ranges from 4.6 to 44.0 bps across the 24
parents; fill rates range 0.50-1.00 (mean 0.854); opportunity cost is
*signed* — partially-filled orders show both positive values (order 21:
+9.20 bps, the unfilled remainder would have cost more) and negative ones
(order 19: -3.08 bps, not filling was favorable), netting to a mean of
+0.79 bps. Mean delay cost is exactly 0.000 bps on both instruments:
with a 200-1,500 ms decision-to-arrival delay against a slow synthetic
mid, arrival and decision mids coincide at this resolution, so
implementation shortfall here is trading cost almost by identity. VWAP
slippage is computable for only the subset of orders whose horizon
overlaps tape trades (marked `n/a` otherwise — 51 market trades on the FX
tape make interval VWAP mostly undefined there), an honest small-sample
artifact the report leaves visible.

### 4.6 Cross-language reproducibility of the fill set

The golden fill set of §4.1 is not a one-language artifact: the C++
simulator is the normative reference (`cpp/include/iap/execution/execution.hpp`
header comment), a Java port exists under
`java/src/main/java/com/iap/execution/` (`ExecutionSimulator`,
`ExecutionReplay`, `Algos`), and the harness's golden groups
(`tests/harness/run_all.sh`; C++ `ReplayFillsGolden` among the ctest
`-R Golden` suites) hold both to `expected_replay_fills.json` — ticks,
quantities and timestamps exact, fee/impact at 1e-9. The queue rules of
§3 are therefore executable documentation, not prose.

## 5. Limitations

1. **Synthetic, mean-reverting data — pro-passive bias.** The generator's
   mid reverts strongly (REPORT.md honesty note), so markouts *must* show
   reversion, understating real-world adverse selection where informed flow
   trades ahead of drift. The finding in §4.3 validates the measurement
   machinery, not a trading recommendation. TCA_REPORT.md also flags that
   the synthetic consolidated book can occasionally be crossed (< 2% of
   equity event states under the shared-efficient-price generator), which
   can produce isolated negative spread-cost lines.
2. **Two toy parents / 36 simulated parents.** The golden-fills comparison
   is one seed on one instrument; the TCA population is generated by a
   pinned toy scheduler (4 slices, 15% skip), not by real order flow.
3. **Optimistic cancel rule.** Full-amount `ahead_qty` decrement on
   observed cancels systematically flatters passive fill probability; a
   probabilistic or pro-rata alternative is future work and would need its
   own golden.
4. **No queue-aware *strategy* results.** This paper measures fill
   economics; it does not yet close the loop of feeding queue-position
   state into the alphas (the feature registry has queue features on
   equities — paper 1/2 — but the promoted alphas do not consume simulator
   state).
5. **Simulated latency is a parameter, not a measurement** (50/50/100 µs
   legs + venue profile) — see paper 4, limitation 5.

## 6. Conclusions

The platform's execution stack demonstrates that a fully deterministic,
public-events-only queue model is enough to make passive/aggressive
economics quantitatively comparable and regression-testable: on the pinned
golden vector, joining the queue converts a +$1.80-plus-impact explicit
cost into a -$0.80 rebate on comparable size, and the TCA layer prices the
aggressive alternative at ~24 bps of immediately-reverting impact — with
every decomposition enforced as an exact identity and every fill
reproducible bit-for-bit across C++ and Java from seed 20260829. On this
mean-reverting synthetic dataset, adverse selection against resting orders
is absent by construction; the deliverable is therefore the *measurement
harness* — markout windows, Perold identity, participation regression,
queue-position rules pinned in a normative header — ready to be pointed at
data where the answer is not known in advance.

## Artifact provenance

| claim | artifact |
|---|---|
| queue-position rules, latency model, fees, impact | `cpp/include/iap/execution/execution.hpp` (normative comment), `cpp/src/execution/execution.cpp`, Java port `java/src/main/java/com/iap/execution/` |
| golden fill-by-fill passive vs aggressive economics | `tests/golden/expected_replay_fills.json` |
| TCA population, IS/markout/impact tables | `research/tca/TCA_REPORT.md`, `research/tca/tca_orders.json` |
| research harness rules (slices, skip prob, impact ticks) | `python/src/iap/tca/simulator.py` |
| Perold identity to 1e-9 | `tests/golden/expected_tca.json`, `python/src/iap/tca/tca.py` |
| venue fees/rebates and latency profiles | `configs/venues.json` |
| golden event vectors | `tests/golden/events_eq_mbo.jsonl`, `events_fx_quote.jsonl` |
