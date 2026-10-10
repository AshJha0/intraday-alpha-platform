# How it works — the quant, algo and AI sides of the platform

A guided explanation for a technically literate newcomer. It goes top-down:
the pipeline on one page, then one section per side of the platform — quant
research, execution algorithms, hard risk, machine learning, the AI / agent
boundary, determinism. Every section ends with **where to look** (file
paths) and **how to run it** (a command that works from a checkout).

Three things to know before reading:

1. **All market data is synthetic.** A seeded generator produces every
   event. Every number below is a statement about that generator and this
   pipeline, not about a market. Up to v1.3.0 the generator had a defect:
   equity flow stopped about 40% of the way through each session. v1.4.0
   fixes it (`equities.flow.calibration = "session"`; every equity stream
   now runs to the close) and regenerates the dataset and everything
   computed from it. About the
   same number of equity events is now spread over the whole 6.5-hour
   session, so the flow is about 2.5 times sparser in time than the old
   compressed flow (equity rows are a little over 3 s apart), and the
   equity signals are weaker on it. `"legacy_budget"` reproduces the
   v1.3.0 dataset byte for byte. v1.5.0 did not change the dataset.
2. **The results are negative, and that is the headline.** 24 alphas were
   researched; none is promoted. Eighteen of them never forecast a move
   larger than the cost of trading it, and make no trade; the six that
   trade lose. The end-to-end loop loses 81.53 USD on its
   golden session. The ML gate passes on this dataset — the linear baseline
   has a small positive IC, so the trees and the network are fitted — and
   no model earns its costs. The platform is built so that those sentences
   are trustworthy, not so that they read well.
3. **The corrected methods are the defaults.** Where the v1.3.0 review
   found a better statistic or policy, it was first added *beside* the old
   rule as an opt-in (v1.3.0 and v1.4.0). Since v1.5.0 the corrected rule
   is what runs when nothing is named, every research artefact was
   regenerated under it on the unchanged dataset, and every number below is
   a v1.5.0 number. Each old rule stays selectable under a legacy name —
   the bundle `legacy_v1` reproduces the v1.4.0 report — and is named
   below beside the default that replaced it.
4. **There is now real data too, and a maker side.** Since v1.6.0 the same
   pipeline has run on seven real Nasdaq ITCH sessions (2019-20; AAPL, MSFT,
   QQQ), and v1.8.0 confirmed four pre-registered signals on two unseen
   2026 days (docs/REAL_DATA.md §3). The signals are real and too small to
   pay a taker at 1-5 s. v1.9.0 and v1.10.0 add the opt-in maker path,
   auction research, optimal-execution schedules and stronger research
   governance; §3.6-§3.8 and §6.2 below explain them. Nothing in this
   document claims a profitable strategy. Sections 1-5 describe the bundled
   synthetic dataset unless they say otherwise.

The commands assume the seeded dataset exists. If `data/features/` is
empty, build it once (about two minutes):

```bash
cd python
PYTHONPATH=src python3 -m iap.marketdata     # raw -> normalized -> Parquet + QC report
PYTHONPATH=src python3 -m iap.features       # the feature / label store
```

Contents: [1 The pipeline](#1-the-pipeline-in-one-page) ·
[2 Quant](#2-quant-how-a-signal-is-researched-and-judged) ·
[3 Algo and execution](#3-algo-and-execution-how-an-order-becomes-fills) ·
[4 Risk](#4-risk-the-engine-that-says-no) ·
[5 ML](#5-ml-what-was-tried-and-what-it-showed) ·
[6 AI, LLMs and agents](#6-ai-llms-and-agents-the-boundary-what-exists-what-is-planned) ·
[7 Determinism and replay](#7-determinism-and-replay)

---

## 1. The pipeline in one page

```
events -> order book -> features -> alphas -> portfolio -> hard risk
       -> execution algos -> smart order router -> simulator -> TCA -> decision trace
```

| stage | what happens | the one rule that matters |
|---|---|---|
| **Events** | A seeded generator writes venue feeds; a normaliser validates sequences and writes a canonical stream (JSONL and the IAP1 binary). | Prices are integer ticks and timestamps integer nanoseconds. No float is ever on a contract. |
| **Order book** | Each venue's book is rebuilt event by event (L1 / L2 / market-by-order), then merged per instrument. | A sequence gap marks the book *stale*; a stale book contributes nothing until a snapshot recovers it. |
| **Features** | 205 registered features in 10 families are updated incrementally on each event. | Every value carries a validity bit. NaN is never a valid value. |
| **Alphas** | 24 small models turn features into an `AlphaSignal`: an expected return and a confidence. | Each alpha must state an economic rationale, and its fitted sign must agree with it. |
| **Portfolio** | A projected-gradient optimizer turns signals into target positions under position, gross, net, participation, turnover, volatility and currency constraints. | An infeasible solve never returns NaN weights. |
| **Hard risk** | Every order is checked against a pinned list of 23 rules. The first rule that fails decides. | Fail closed: anything missing, stale, malformed or out of range is a REJECT. |
| **Execution algos** | A parent order is sliced into children by TWAP, VWAP, POV or IS. | No child outlives its parent's window. |
| **Smart order router** | Each child is routed to a venue. | A stale or halted venue is never a fallback: no eligible venue means `NO_ROUTE`. |
| **Simulator** | Children are filled against the replayed tape under nine pinned rules. | It never fills more than the market displayed or traded. |
| **TCA** | Each parent's cost is decomposed. | Implementation shortfall = delay + trading + opportunity, as an exact identity. |
| **Decision trace** | One record per decision holds every stage above. | The stream of records has a digest that a replay must reproduce. |

Four languages implement parts of this. Python is the reference for
everything and runs the whole loop. C++ owns the latency-critical path and
the execution rule text. Rust owns the hard risk engine's rule text. Java
is the platform layer that runs paper sessions. They are held together by
golden files: one implementation writes the expected output, every other
one must reproduce it — exactly for integers and bytes, to 1e-9 for
floats.

The loop above is not a diagram of intent. One command runs it on one
synthetic instrument and writes a trace for every decision. On its golden
session (15,805 events, 800 decisions) it fills 169 times and loses 81.53
USD: about +0.02 bps of alpha against −0.41 bps of execution cost.

**Where to look**

- `python/src/iap/mvp/engine.py` — the per-event order of the whole loop.
- `docs/MVP.md` — the same loop explained module by module, with its results.
- `docs/DIAGRAMS.md` §1 (pipeline) and §10 (the MVP loop).
- `PLATFORM_CONVENTIONS.md` — every pinned rule, by section.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp run                                   # under a minute; prints events, decisions, fills, P&L, digest
PYTHONPATH=src python3 -m iap.mvp explain --run ../data/mvp/58a10f2194a3c81c 5   # one order: signal -> risk -> routing -> fills -> TCA
```

---

## 2. Quant: how a signal is researched and judged

The research side answers one question honestly: *is this signal real, and
does it survive costs?* Everything here exists to make a wrong "yes" hard.

### 2.1 The feature registry

A feature is not a column someone computed in a notebook. It is an entry
in a registry (`data/reference/feature_registry.json`): a name such as
`ofi_l5_w1s_v1`, a family, a version, parameters and declared
dependencies. The registry has 205 entries in 10 families — price,
microstructure, order flow, liquidity, volatility, time of day,
cross-asset, venue, regime, execution. The hash of the registry is the
`feature_version` stamped on every experiment, so a result always says
which definitions produced it. A 40-feature core set is also implemented
natively in C++, Rust and Java and matched to 1e-9.

### 2.2 The alpha contract and the economic-rationale rule

An alpha is a class with a fixed interface. It declares which features it
reads, it is fitted on training rows, and it scores a row into an
`AlphaSignal` — an expected return and a confidence. Two rules are enforced
in code:

- The class must carry a docstring section `Economic rationale:`. An alpha
  without a stated reason to work is rejected at construction.
- The fitted sign must agree with that rationale (`hypothesis_confirmed`).
  An alpha that is statistically strong in the *opposite* direction to its
  own story cannot be promoted. FX09 is the standing example: a gate
  t-statistic of −5.75, the wrong sign, REJECT; EQ08 is a second one, at
  −4.57. Both are beyond the significance threshold of §2.7 in the wrong
  direction. (On the v1.3.0 dataset EQ09 was the equity example, with a t
  of −5.65; on the regenerated dataset its t is −1.32 and it is simply not
  significant.)

The second rule is what stops a researcher from fitting first and writing
the story afterwards.

### 2.3 Labels and horizons

A label is the thing a signal is supposed to predict: the forward return
from time `t` to `t + h`. They are computed in event time at 11 horizons
from 10 ms to 15 minutes, in two forms — mid-to-mid, and cost-adjusted
(paying half the spread at both ends). A label is *valid* only if the
stream was observed through `t + h`, the book was tradable at both ends,
and no halt, auction or stale-venue gap falls inside the horizon. The
*valid* label of a signal just before a halt therefore does not exist.
Dropping such a row is not neutral, though: the position would have been
held through the halt. So each row whose label is invalid for a blackout
alone also carries the return to the first tradable price after it
(`label_reopen_<h>`), and §2.4 says which rows an IC scores.

One more condition matters on the v1.4.0 dataset. The price at `t + h` must
be fresh: no older than `max(5 s, 2 × the instrument's median quote gap)`.
Equity quotes are now about 2 s apart at the median, so the 5 s floor
binds, and on the sparser flow a material share of equity labels at
horizons of 10 s and more is invalid as `forward_stale`. The valid fraction
per equity is 0.99–1.00 at 5 s, 0.79–0.81 at 10 s, 0.75–0.77 at 1 minute
and 0.45–0.63 at 15 minutes (`data/features/features_summary.json`). Rows
with a stale forward price are excluded from every IC; they are not scored
as zero returns.

### 2.4 IC

The information coefficient is the correlation between score and label
over out-of-sample rows. It is the basic measure of "does the signal point
the right way". Three details matter here:

- The gate reads the pooled IC on **uncrossed** rows only. A merged book
  whose bid is above its ask is two venues disagreeing, not a price anyone
  could trade; on FX it inflated one alpha's IC from 0.041 to 0.117.
- The pooled IC is computed on the standardised signal, not on the fitted
  expected return, so that a sign flip between folds cannot hide.
- The rows it scores are the valid labels plus the rows invalid for a
  blackout alone, scored at their realised reopen return
  (`ic_rows="blackout_reopen"`, the default since v1.5.0; the legacy rule
  is `"valid_only"`). For 23 of the 24 alphas the two ICs agree to within
  0.003. For EQ11, at a 15-minute horizon, 23,413 such rows take the gate
  IC from 0.0261 to 0.0038: the valid-only IC had dropped exactly the rows
  on which the forecast was wrong. Both are in the report.

Reported beside the gate IC and read by no gate: a per-instrument mean IC
and a volatility-scaled IC (so one volatile instrument cannot dominate the
pool). The gate stays on the pooled IC because the gate t of §2.6 is the
significance of that slope and of no other.

### 2.5 Purged, embargoed walk-forward

The data is split in time, never at random. With four folds, each fold
trains on everything before its test segment — minus two exclusions:

- **Purge.** A training row whose label window reaches into the test
  segment is dropped. Its label was computed from prices the test set also
  sees.
- **Embargo.** A further 60 seconds before the test segment is dropped, to
  cover serial correlation that outlives the label horizon.

Folds are cut at quantiles of *row count*, not of wall-clock time, because
the data is not uniform in time: cutting by the clock once left two of four
folds empty and called them a pass. A fold with too few rows is reported as
degenerate and counted as a failure.

### 2.6 The gate t: pooled-slope HAC, and the within-bucket t it replaced

An IC needs a significance test, and the rows are not independent —
overlapping labels are autocorrelated. Up to v1.4.0 the gate computed one
IC per five-minute bucket, took their mean weighted by the number of pairs
in each bucket, and divided by a Newey–West (Bartlett-weighted) long-run
standard error.

That statistic has a blind spot. A within-bucket correlation demeans score
and label inside each bucket, so signal that lives *between* buckets is
removed from the test — while the gate IC beside it is pooled and keeps it.
The gate was testing one number and thresholding another. The
**pooled-slope HAC t** tests the pooled slope directly with the same
buckets and weights, and since v1.5.0 it is the statistic the gate reads
(`significance="pooled_slope"`, reported as `gate_tstat`). The
within-bucket t is the legacy rule (`"within_bucket"`) and is printed
beside it as `t other`.

The change cuts both ways. The equity alphas lose some t (EQ03 5.85 →
5.16, EQ01 2.72 → 1.59), and several FX alphas whose signal is between
buckets gain (FX08 2.18 → 3.84). The threshold the t is compared with is
§2.7.

### 2.7 The multiple-testing ledger and looks

Try enough things and something will look significant. So every look at
the data is recorded in a ledger (`research/experiments.json`), identified
by alpha, kind, configuration and — since v1.4.0 — dataset, so that
re-running a script does not inflate the count. Today it holds 5,156 looks
over 216 entries: 1,068 were taken on the v1.3.0 dataset, 852 on the
v1.4.0 dataset under the old rules, 2,476 were added when v1.5.0
re-ran everything under the new ones, and 760 by the signal-combination
experiments of the same release (§2.13). A statistic computed on a different
dataset, or under a different method bundle, is a different look, and the
denominator only grows: regenerating the data or changing the rules does
not reset it. From that count come two yardsticks: the largest t expected
from pure noise over that many trials (about 4.10) and the Bonferroni
threshold (about 4.39).

Up to v1.4.0 the PROMOTE gate asked for t ≥ 3.0 whatever the count, and
the report printed the yardsticks beside it. Since v1.5.0 the gate follows
the count (`tstat_threshold="ledger"`; the legacy rule is `"fixed"`): the
threshold is the larger of 3.0 and the Bonferroni |t| at the look count
the run is judged at — the ledger before the run plus what the run adds,
declared before the first alpha is evaluated and recorded with the result.
The promotion report was judged at a count of 3,936, which gives 4.365.
Three alphas clear it (EQ02 7.17, EQ03 5.16, EQ12 7.20) where six cleared
3.0; EQ06 misses at 4.36 and FX04 at 4.24, and the threshold was not moved
for them. A result is not re-judged when the ledger grows afterwards.

One validation costs more looks than it used to: 84 per alpha and run
under the default methods (83 for the validation at four folds, one for
the out-of-sample backtest), 28 under `legacy_v1`. The per-fold
diagnostics of §2.8 are looks too.

One earlier change stands (v1.3.0): a `--dry-run` used to compute
everything and record nothing; it debits its looks.

### 2.8 Cost model and capacity

The research cost model charges half the spread, venue fees, and an impact
that grows with the square root of the order's share of daily volume. This
is where every alpha dies, and since v1.5.0 the report shows how. EQ03 has
a gate IC of 0.019 with a t of 5.2 and makes **no trade**: its fitted
expected return never exceeds the round-trip spread and fee of the row it
is computed on. Up to v1.4.0 the backtest followed the sign of the signal
on every row instead, so the same alpha flipped its position hundreds of
times an hour, paid the spread each time and lost 148,562 at 1× costs.
That loss measured the policy. The finding underneath is the same in both:
the predicted move is smaller than the spread.

The rules, default first, each with the legacy rule that reproduces a
v1.4.0 number:

| choice | default since v1.5.0 | legacy rule |
|---|---|---|
| positions | `BacktestConfig(position_policy="cost_aware")`: enter only when the expected return exceeds the round-trip cost, hold for the label horizon | `"sign"`: follow the sign of the signal on every row (`BacktestConfig.legacy()`) |
| fills | `cap_fills_at_l1=True`: no fill larger than the displayed top-of-book size | any size fills at the touch |
| rows traded | `block_rows_column="auto"`: only the rows the IC scores | every row |
| impact | `CostModel(impact_model="sqrt")` | `"linear"` (`CostModel.with_linear_impact()`) |
| capacity | `capacity="breakeven"`: the order size at which the realised edge equals its own cost, capped at the participation line | `"participation"`: participation cap × volume × price |
| per-fold cost survival, bootstrap interval of the net P&L | reported; no gate reads them | not computed |

What the report shows under the defaults: at 1× costs on the last fold 18
of the 24 alphas make no trade, 6 trade and lose (EQ11 and five FX alphas,
between 22 and 733 USD), and none ends above zero. A net P&L of exactly 0
does not pass `net P&L > 0`, so the cost gate fails for all 24, as it did
before. The edge-breakeven capacity is above zero for three alphas (EQ11,
FX08, FX10) and zero for the other 21.

The cost-aware policy does not rescue anything, and a policy that never
trades is not a result: it is no evidence that abstaining pays. COOKBOOK
recipe 28 runs both policies on EQ03 over both sessions — no trade under
the default, 38,222 trades and a loss of 826,926 under the legacy rules.
What "0 PROMOTE" says is narrower than it sounds: on this synthetic book
the forecasts are smaller than the spread, for an alpha that crosses it.
The research backtester does not model passive execution, so it says
nothing about what resting orders would earn.

### 2.9 Stress

Each alpha is re-run under harsher assumptions: costs at 0.5×, 1× and 2×;
latency of 100 ms to 5 s; high- and low-volatility regimes. An alpha that
only works at zero latency is not an alpha. (`stress_version=2`, the
default since v1.5.0, carries the whole base backtest configuration into
the row-latency grid; version 1, the legacy rule, rebuilt it from four
fields. An alpha that makes no trade at 1× has nothing to stress: its
stressed P&L is 0 in every column, and only its IC columns move.)

### 2.10 Leakage probes

Leakage is using information that was not available at decision time. It
produces beautiful backtests. Four detectors, all in the standard suite
since v1.5.0:

1. **Label guard.** Score once normally and once with every label column
   replaced by garbage. Any difference proves the scoring path reads labels.
2. **Shift-by-one.** Lag the scores by one row. A real signal degrades
   gracefully; a leak collapses. The converse does not hold: on the v1.4.0
   MVP session the lagged IC does not collapse (EQ01: 0.217 unlagged, 0.149
   lagged by one decision), because the signals persist from one decision
   to the next. That is persistence, not a leak, and not proof of
   cleanliness either — the evidence against a leak is the pinned label
   definition and the truncation probe (docs/MVP.md).
3. **Truncation probe.** Re-score on a truncated feature frame. The score
   at the cut must be bit-identical.
4. **Recompute probe.** Rebuild the *features* from truncated raw
   events. This catches look-ahead baked into a feature itself, which the
   first three cannot see. It runs whenever the raw events are available
   (three anchors on the first 12,000 events of the first normalized file
   per asset class); all 24 alphas pass. A validation without events
   reports `recompute_ok: null`, and the runner then marks the result not
   gate-eligible. Up to v1.4.0 the probe was opt-in and no report ran it.

### 2.11 What the lifecycle gates check

A verdict (PROMOTE / ITERATE / REJECT) is one research result. The
lifecycle is the state machine an alpha moves through over time: RESEARCH
→ CANDIDATE → VALIDATING → PAPER → ACTIVE ⇄ WATCH → RETIRED. Each
promotion needs every gate of its edge to pass:

| edge | gates |
|---|---|
| RESEARCH → CANDIDATE | a ledger entry exists; leakage clean |
| CANDIDATE → VALIDATING | leakage clean; out-of-sample IC; statistical significance; fold consistency; fold count; hypothesis sign; **net P&L after costs**; **the bootstrap lower bound of net P&L**; capacity; stability; **correlation with alphas already through** |
| VALIDATING → PAPER | holdout IC tracks research; replay reproducible; cross-language parity |
| PAPER → ACTIVE | minimum paper sessions; paper IC tracking; paper net P&L; no kill events |
| ACTIVE ⇄ WATCH → RETIRED | rolling live IC |

On the bundled data all 24 alphas reach CANDIDATE and stop there. Every
one fails the same three gates: net P&L after costs, the bootstrap bound
on it, and capacity (an alpha
that does not trade, or trades at a loss, has an edge-breakeven capacity
below the 1,000,000 USD the gate asks for). For three of them (EQ02, EQ03,
EQ12) those are the only gates that fail.
The bootstrap gate asks for more than a positive number: the lower end of
a 95 % interval around the net P&L of all four folds has to be above zero,
and an alpha that never trades has no interval and fails (17 of the 24;
the 7 that trade lose). The correlation gate asks whether a candidate is
more than 0.7 correlated with an alpha that is already at VALIDATING or
beyond; nobody is, so it passes for all 24 without having been tested —
§2.13 shows what it would do. Statistical significance fails
for 21, stability for 14, the IC gate for 12. Under the v1.4.0 rules the
cost gate failed for all 24 and was the only failure for four (the three
above and FX04, whose t of 4.24 is now below the threshold).

The live edges at the bottom of the table have a corrected rule too
(§5.3): WATCH → RETIRED is decided by a CUSUM of the shortfall below the
gate, not by a count of consecutive breaches.

Since v1.3.0 there is one more condition, and it is about the evidence
rather than the alpha. A research result is **gate-eligible** only if it
was produced under the pinned protocol or something stricter (costs at 1×
or more, at least four folds, the standard latency and embargo), on the
periods derived from the dataset, and — since v1.5.0 — under the default
method bundle. Any other configuration still runs and
is still counted in the ledger, but every gate that reads it fails.
Halving the costs is a legitimate experiment and is not promotion evidence.

### 2.12 The power study, and what its result means

"Nothing was promoted" is only informative if the chain *could* promote
something real. So the generator has an opt-in mode that plants effects of
known size — informed order flow, and one instrument leading another — and
the unmodified pipeline is run on the result (`python -m iap.research
power`). Each run is generated once and scored on its first 1, 2, 4 (and
in the extended grid 8) sessions, 20 generator seeds per cell, so a rate
comes with a binomial interval. A detection is the gate statistic of §2.6,
the pooled-slope t, at or above the PROMOTE threshold in force (4.365), with
a positive IC. From `research/power/POWER_REPORT.md` (4 sessions) and
`research/power/extended/POWER_REPORT.md` (8 sessions), at the reference
size:

| planted effect | detector | 2 sessions | 4 sessions | 8 sessions |
|---|---|---:|---:|---:|
| order flow | EQ04 at its declared 5 s label | 2 of 20 | 10 of 20 | 20 of 20 |
| order flow | EQ04 at the kernel-matched 10 s label | 11 of 20 | 20 of 20 | 20 of 20 |
| lead-lag | EQ10 at its declared 1 s label | 0 of 20 | 0 of 20 | 0 of 20 |
| lead-lag | EQ10 at the lag-matched 5 s label | 1 of 20 | 6 of 20 | 16 of 20 |
| nothing planted (null) | each of the four | 0 of 20 | 0 of 20 | 0 of 20 |

The 95 % intervals: 20 of 20 is 0.84–1.00, 16 of 20 is 0.58–0.92, 10 of 20
is 0.30–0.70, 0 of 20 is 0.00–0.16. At the fixed threshold of 3.0 the 5 s
order-flow row reads 12, 19 and 20 of 20.

What it means:

- The chain **detects the planted order flow** at the reference size once
  it has enough sessions: every run on 8, half of them on 4, one in ten on
  the 2 sessions the bundled dataset has. The earlier report's "1 of 3" was
  3 seeds on 2 sessions at t ≥ 3; the same cell with 20 seeds is 12 of 20.
- It **detects the planted lead-lag only at a label long enough to contain
  it**. The lead is two one-second steps; EQ10's declared label is one
  second and ends before the follower has moved, so at that horizon the
  effect is not detected at any session count tested (the fitted model puts
  80 % power near 50 sessions). Against the 5 s label the same signal is
  detected in 16 of 20 runs on 8 sessions.
- **Where the power goes** (the report's diagnosis table): the observed mid
  moves on a tick grid, so 78 % of 5 s labels and 95 % of 1 s labels are
  exactly zero and the measured IC is 0.29 (order flow) and 0.11 (lead-lag)
  of the IC the mechanism has on the efficient price; 55 % of rows have no
  trade in the last 10 s and carry no order-flow signal; rows overlap, so
  1.4 to 3 rows make one independent observation; the walk-forward
  chain scores four fifths of the sample. The product of those reproduces
  the measured t (4.58 expected against 4.54 measured for EQ04 on 4
  sessions). Label validity is not the loss: 99.8 % of 5 s labels are
  scored.
- The **ledger threshold costs sessions, not detections**: 80 % power at
  4.365 needs about 5 sessions for the order-flow effect and at 3.0 about 3.
- It is **not fooled** by an effect that reverses half-way through (no
  evidence in any of 20 runs), and the break test — the difference between
  the slope before and after mid-sample — flags the reversal in 20 of 20
  runs for order flow and 15 of 20 for lead-lag on 8 sessions, with at most
  one false alarm in 20 on a stable run.
- It **promotes nothing, at any size or session count** — not even an
  effect with a mean t of 18. At the reference size the cost-aware backtest
  makes no trade on the 5 s forecast; where it trades (the 10 s label, or
  twice the reference size) no fold survives 1× costs and no bootstrap
  interval of the net P&L lies above zero.

The last point limits what the headline can claim. "0 PROMOTE" is not
proof that the chain is a strict judge of alpha; PROMOTE has not been shown
to be reachable in this generator's cost structure: the chain can see an
effect it cannot monetise. And the first point limits what a REJECT on the
bundled two sessions can claim: an effect of the reference size would be
missed there nine times in ten.

**Where to look**

- `python/src/iap/features/` (registry, engine), `python/src/iap/alpha/`
  (base class and the 24 alphas), `python/src/iap/labels/`.
- `python/src/iap/validation/` — `splits.py`, `metrics.py`, `leakage.py`,
  `stress.py`, `ledger.py`, `validate.py`, `diagnostics.py`, and
  `methods.py`, the one place the default (`v2`) and legacy (`legacy_v1`)
  rule sets are written down.
- `python/src/iap/research/` — the experiment runner, gate eligibility
  (`specs.py`), the power study (`power.py`).
- `python/src/iap/lifecycle/` and `docs/LIFECYCLE.md`.
- `research/alpha_reports/REPORT.md`, `research/power/POWER_REPORT.md`,
  `docs/RESEARCH_VALIDITY.md`.
- LEARN.md §5–§6 (labels, honest alpha research), §23–§24 (power, gate
  gaming); COOKBOOK recipes 4, 26–31 and 35.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m iap.research show 00ebeb2b537b5155   # one committed experiment (EQ03 @ 5 s, default methods): spec, result table, VERDICT, the t threshold it was judged at, eligibility
PYTHONPATH=src python3 -m iap.research show f0f6c49b553f6b59   # the same request under the v1.4.0 rules: recorded, and no longer gate-eligible
PYTHONPATH=src python3 -m iap.lifecycle status                 # 24 alphas, all CANDIDATE, with the gates each one fails
PYTHONPATH=src python3 -m pytest -q tests/test_validation_framework.py tests/test_research_validity.py
```

---

### 2.13 Combining weak signals, and breadth

One weak signal is a small edge on a noisy forecast. The standard answer
is to combine many: if the errors of the signals are not perfectly
correlated they average down faster than the common forecast does. For
`K` standardised signals with mean IC `ic` and mean pairwise correlation
`rho`, the equal-weight blend has

    IC_blend = ic × sqrt( K / (1 + (K − 1) × rho) )

The square-root term is the **breadth** you actually have. With `rho = 0`
it is `sqrt(K)`: twelve independent signals triple the IC (√12 ≈ 3.5).
With `rho = 1` it is 1: twelve copies of one signal are one signal. This is
the "fundamental law" — information ratio ≈ IC × √breadth — and its fine
print: breadth counts *independent* bets.

`iap.combine` builds the blend as an alpha whose inputs are alphas and
sends it through the same validation as any single alpha (§2.5–§2.11).
Three things are specific to it.

**The weights are fitted out of sample.** In each walk-forward fold the
weights are estimated inside the training window, on predictions the
members made for rows they were not fitted on (three inner folds, same
purge and embargo). A weight never sees a test row; a test corrupts every
row from the test start onwards and checks that not one fitted number
moves. Four methods are available — equal weights (the default, and the
baseline: it estimates nothing), IC weights, ridge regression with the
penalty chosen inside the training window, and a mean-variance blend on a
shrunk covariance matrix. The last two account for correlation between
members; the first two do not.

**Combining is a search, and is charged as one.** Each (member list,
method) pair costs 83 + K looks — its validation plus one per member — and
a report that tries four methods declares all four before it evaluates the
first. The committed report was charged 760 of them and was judged at a t threshold
of 4.42.

**The result, on this data.** `research/combination/REPORT.md`:

| | members | mean member IC | effective bets | expected blend IC | measured IC | gate t | net P&L (all folds) | verdict |
|---|---|---|---|---|---|---|---|---|
| equities, equal weight | 12 | 0.0038 | 6.5 of 12 | 0.0099 | 0.0105 | 1.98 | 0 (no trade) | ITERATE |
| equities, ridge | 12 | | | | 0.0429 | 7.06 | −73 USD | ITERATE |
| FX, equal weight | 12 | 0.0109 | 9.9 of 12 | 0.0348 | 0.0315 | 3.22 | −13 USD | ITERATE |
| FX, ridge | 12 | | | | 0.0482 | 4.73 | −1 627 USD | ITERATE |

The arithmetic works: the measured equal-weight IC is what mean IC × √breadth
predicts, to within a few parts in ten thousand. The fitted methods do
better statistically — the equity ridge blend has an IC of 0.043 and a t of
7. And none of the eight combinations is promotable, for the reason no
member is: the forecast, in return units, is smaller than the cost of
acting on it. The equity blends make between 0 and 6 trades in four folds;
the FX blends that trade lose. Breadth multiplies IC. It does not multiply
the size of the move being forecast, and that — not statistical
significance — is what this dataset lacks.

**Why a correlation gate follows.** Of the twelve equity alphas, three
(EQ02, EQ03, EQ12 — three ways of measuring order-flow imbalance) are
correlated 0.95 to 1.00. They look like three alphas that each pass the
significance gate; they are one bet counted three times, and the twelve
equity signals hold about 6.5 independent bets. Allocating to all three
would triple the position in one idea. The lifecycle's correlation gate
(§2.11) refuses that: once one of them is at VALIDATING, the other two are
held at CANDIDATE.

### 2.14 Real data, and the `v3` validity bundle (v1.9)

On real sessions the statistics above are strong (EQ01's gate IC +0.105, t
21 on seven 2019-20 days) and four pre-registered signals held on two unseen
2026 days. The v1.9 review then found three ways the real-data inference
was weaker than its t-statistics said: two of the seven days are FOMC days
and one is a thin holiday session; QQQ supplies about half the rows, so the
pooled IC mostly describes QQQ; and no statistic treated the day as the
unit of independence. The opt-in `v3` bundle answers each:

- folds cut at session starts (`split_mode="day_aligned"`), with
  leave-one-day-out as an option;
- the gate IC is the equal-weight mean of per-instrument ICs (the pooled IC
  is kept beside it);
- a `validity` block reports per-day ICs with event tags, the gate
  statistics without FOMC and holiday-thin days, a HAC t with no lag product
  across a day boundary, a day-clustered t and a day-block bootstrap;
- ITCH-only data is labelled as one Nasdaq book (`book_scope =
  single_venue`, `price_reference = nasdaq_bbo`), and label freshness can be
  judged causally (`--label-freshness trailing`).

`v3` is not the default, so no published number moved. The validity block
costs four more looks per validation (88 instead of 84 at four folds).

**Where to look:** `python/src/iap/validation/methods.py`, `sessions.py`,
`metrics.py`; docs/RESEARCH_VALIDITY.md §1a; LEARN.md §32.

**How to run it:** COOKBOOK recipe 44 (synthetic fixture), or on an ingested
dataset `python -m iap.research run --alpha EQ01 --methods v3 --dataset-dir <dataset>`.

## 3. Algo and execution: how an order becomes fills

### 3.1 The four algorithms

A *parent* order ("buy 3,000 shares between 10:00 and 10:05") is worked as
a series of *child* orders. The algorithm decides the schedule:

| algorithm | schedule | when it is used |
|---|---|---|
| **TWAP** | equal slices at equal time intervals | no view on volume or urgency |
| **VWAP** | slices follow an expected volume profile (U-shaped over the session) | to trade in proportion to the market |
| **POV** | event-driven: trade a fixed fraction of the volume as it prints | to cap participation |
| **IS** (implementation shortfall) | front-loaded, decaying exponentially with a risk-aversion parameter | urgent: timing risk outweighs impact |

Two rules apply to all four. A slice larger than `max_child_qty` is split
into several children. Every child carries `expire_ts` = the parent's end
time, so no child outlives its window. The IS schedule is a front-loaded
exponential, not the closed-form Almgren–Chriss trajectory, and the
documentation says so.

### 3.2 Smart order routing

For each child the router chooses a venue:

1. **Eligibility.** A venue qualifies only if its book for the instrument
   exists, is not stale, is in TRADING status, and its mean latency is
   within the configured maximum.
2. **Aggressive orders** go to the venue showing the most favourable
   opposite best price.
3. **Passive orders** go to the venue with the highest maker rebate (when
   `prefer_rebate` is set), otherwise the lowest venue id quoting our side.
4. **Ties** break by taker fee, then commission, then venue id.
5. **No eligible venue** returns `NO_ROUTE`. The caller rejects the child
   and counts it. The router never falls back to a stale venue.

### 3.3 The simulator's fill and queue rules

The simulator replays the real tape and decides which of our children
fill. Its normative text is the header comment of
`cpp/include/iap/execution/execution.hpp`. The governing principle: **it
never fills more than the market actually traded or displayed, and our own
orders queue behind each other.**

- **Latency.** A child arrives after decision + risk + wire + venue
  latency plus one seeded jitter draw. A cancel travels the same path.
- **Aggressive orders** walk the displayed depth, one fill per level. Our
  fills never mutate the replayed book; instead an *overlay* records the
  displayed size we have consumed, so two children cannot take the same
  shares.
- **Passive orders** join a queue. `ahead_qty` starts as the displayed size
  at the level plus our own earlier orders there. Trades at our level pay
  it down; what is left over after it reaches zero fills us.
- **One trade is one pool.** A print of 400 shares is consumed once, in
  queue order, across all our orders at that price.
- **A trade through our price** fills us at our limit, but only up to the
  volume that printed.
- **The venue gate.** While a venue's book is missing, stale or not
  trading, nothing fills there.

v1.3.0 corrected four of these rules, in C++, Java and Python together:

| rule | before | after |
|---|---|---|
| crossing pool | rebuilt from the displayed size on every event, so a static 50-share display filled a 1,000-share order | displayed size **minus what was already consumed**; the check debits the overlay |
| which events count | every raw event | only events the book reports as **applied** — a retransmitted duplicate trades nothing |
| execute size | the quantity the event quoted | **capped** at the book order's remaining size, at the book order's side and price |
| cancels | every cancel at our level moved us up by its quoted size | only a cancel of an order **known to be ahead of us**, by the size the book removed |

All four changes can only reduce simulated fills. At v1.3.0 the fills
golden, the MVP golden and the TCA golden were reproduced unchanged,
because the golden sessions contain none of these patterns. (The MVP golden
was regenerated in v1.4.0 for a different reason: its session comes from
the generator, whose flow calibration changed; in v1.5.0 its events,
decisions, fills and P&L are unchanged and only its `config_version` and
trace digest moved, because the hashed configuration documents did. The
fills and TCA goldens are built on the fixed golden vectors and are still
byte-identical.)

The simulator remains a model. It has no PEG or MID order types, and it is
not calibrated against real fills (backlog, EPICS E27).

### 3.4 TCA decomposition

Transaction-cost analysis explains where the money went. For each parent:

```
implementation shortfall = delay + trading + opportunity
trading                  = spread + impact + timing
```

- **Delay** — the price moved between the decision and the first child.
- **Trading** — what the fills cost against the arrival price.
- **Opportunity** — what was missed on the part that never filled.

The identities hold to 1e-9 and are enforced as contract invariants. TCA
also reports post-fill markouts at 100 ms, 1 s and 10 s (did the price
move against us after we traded?); a markout is `null`, not zero, when it
cannot be measured.

**Where to look**

- `cpp/include/iap/execution/execution.hpp` — the nine rules, as text.
- `python/src/iap/execution/` — `algos.py`, `sor.py`, `simulator.py`,
  `replay.py`; `python/src/iap/tca/`.
- `API_TRADING.md` §2 (and §2.4 for the v1.3.0 rule changes),
  `API_PORTFOLIO_TCA.md` §2.
- `docs/DIAGRAMS.md` §6 and §12; LEARN.md §10–§11 and §22.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_exec_algos.py tests/test_sor.py \
  tests/test_execution_rules.py tests/test_execution_golden.py tests/test_tca_golden.py
```

### 3.5 Posting instead of crossing, and measuring what it buys

Every alpha here loses after costs, and most of the cost is the half-spread
a MARKET child pays. The execution policy of a parent order
(`ParentOrder.policy`) decides whether its children cross or post:

- `NATIVE`, the default, is what §3.1 describes.
- `AGGRESSIVE` crosses with every child.
- `PASSIVE` posts each schedule step at the near touch — one tick inside
  when the spread is three ticks or more, never at or through the other
  side — and lets it rest. How long is the parent's patience:
  `30 s × (1 − urgency)`, times `e^(−risk_aversion)` for IS. When the rest
  time is up the child is repriced once (or simply left in the queue if the
  touch has not moved), and after that the remainder is cancelled and
  crossed. If the schedule falls more than 10 % of the order behind, the
  resting children are cancelled and crossed at once.

The simulator did not change for this. A posted child is an ordinary LIMIT
order under the queue rules of §3.3; a cancel takes the same latency path as
an order and can lose the race to a fill, in which case only what was
actually cancelled is sent again. C++, Java and Python produce the same
fills (`tests/golden/expected_replay_fills_passive.json`).

Whether posting helped is a measurement, not an assumption. The **markout**
of a fill is the signed move of the mid after it,
`s × (mid(t_fill + h) − fill price)`, at 100 ms, 1 s, 5 s, 30 s, 60 s and
5 min. A passive buy at the bid starts half a spread ahead; if the mid then
falls, the fill was *adversely selected* — it happened because someone
better informed wanted to sell. The decomposition is exact:
*effective half-spread = realised half-spread + price impact*. A markout
window that runs past the end of the data, or across a halt or a gap in the
quotes, is reported as `null`, never as zero. The same module reports the
fill rate of resting orders, their time to fill, and both against the queue
position they started from (`iap.tca.markout`, Java `com.iap.tca.Markout`,
golden `expected_markout.json`).

`research/execution/EXECUTION_REPORT.md` runs the same parent orders under
each policy on the bundled equities and prices the quantity a passive order
failed to fill — at the move of the mid over the window plus the cost of
crossing it at the end — so that not trading cannot pass for cheap trading.
It also says which part of the result is the simulator: our resting order
never changes the replayed book, so nobody reacts to it.

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_passive_policy.py tests/test_markout.py
```

### 3.6 The maker path: calibrate, post or quote, decompose (v1.9, v1.10)

§3.5 posts children of a parent order that has to be filled anyway. The
maker path asks a different question: can an alpha that is too small to pay
a taker's round trip (about 0.07 bp forecast against about 0.7 bp of cost at
1 s on the real sessions) be traded by posting instead? It has three steps,
all opt-in Python research code; the default taker backtest is unchanged.

1. **Calibrate** (`iap.execution.calibration`). From one session's normalized
   events, estimate touch fill rates by queue-ahead bucket, queue-depletion
   hazards per side, feed latency quantiles, maker adverse selection at
   100 ms / 1 s / 10 s, and aggressor impact. The result is a versioned JSON
   document. The simulator takes it as `ExecutionSimulator(config,
   calibration=...)` and draws venue latency from it; the gates below read
   their adverse selection from it. ITCH carries no receive timestamp, so
   on ITCH the latency table must be an explicit assumption
   (`parametric_latency`).
2. **Backtest.** Two engines replay a score series through the FIFO
   simulator:
   - `MakerBacktester` (`iap.backtest.maker`) posts one order at the touch
     on the alpha's side when
     `|er| + half_spread + rebate − exit_cost > adverse_selection + margin`.
     A fill comes from queue position, not from the price touching. The exit
     is a taker cross (default), a mark to mid, or a passive exit: post at
     the far touch, reprice up to `exit_reprices` times, then cross the rest.
     Tail conditions on spread, |z|, |er|, queue imbalance and depletion
     probability, and an `allow` mask from the meta-label model, restrict
     when it posts.
   - `QuotingBacktester` (`iap.backtest.quoting`) keeps a bid and an ask
     resting around an Avellaneda-Stoikov reservation price
     `r = mid + alpha_weight·er·mid − γ·σ²·q·τ`: the alpha shifts it, the
     inventory `q` pushes it back toward flat. It enforces a hard inventory
     limit, refreshes quotes through the latency path, floors the
     half-spread at the calibrated adverse selection and flattens before
     the close (cancelling through the latency path and counting fills that
     land during the cancel).
3. **Decompose.** A maker trip's gross is `spread earned − adverse selection
   − exit slippage`; net adds rebates and subtracts fees and impact. A
   quoting session's gross is `spread captured + markout + inventory P&L +
   flatten cost`. Both identities are tested in every mode, so every dollar
   is attributed to a named term.

Labels for the same decisions (`iap.labels.maker_labels`: filled or not,
queue ahead, markouts, "filled and not run over") train filters and tell
you which part of the gate failed.

**Results so far are synthetic.** On the golden vector the gate with a taker
exit admits nothing; with a passive exit it loses 6.7 bp per trip (against
11.4 bp for an ungated taker exit) because the trips that last are the ones
the market ran over. The quoter with a toy score nets less skewed than
unskewed (+86.50 against +120.20 USD). LEARN.md §31 and §34 explain why
these numbers say nothing about a real alpha. A pre-registered, in-sample
maker study on the seven real sessions is running; docs/ROADMAP.md §3.5
describes it.

**Where to look:** API_TRADING.md §2.6-§2.7; `python/src/iap/execution/calibration.py`,
`backtest/maker.py`, `backtest/quoting.py`, `labels/maker_labels.py`;
DIAGRAMS.md §20-§21.

**How to run it:** COOKBOOK recipes 39 and 40 (synthetic), 45 and 46 (one
real session).

### 3.7 Auctions: the closing cross (v1.10)

The continuous-trading signals act over seconds. The Nasdaq closing cross
is a different market: for the last minutes of the day the exchange
publishes the Net Order Imbalance Indicator (NOII, ITCH message `I`) with
the paired and unpaired shares and the price at which the cross would
currently happen. `iap.auction` reads those messages, and the cross result
(`Q`), in a separate opt-in pass (`Itch50Reader(noii=True)`), so the
normalized dataset and its version never change. It builds features
(imbalance ratio, reference-price drift, far / near spread, time to the
cross) and targets (the cross price against the mid at decision time), and
strategy `AUC01` takes the imbalance side at the touch a set time before
the cross and exits in it. The walk-forward fits the orientation and
threshold on purged, day-aligned training days only. The CLI refuses to
backtest without a pre-registration for `AUC01/<cross>-<seconds>s`.

No real-data result exists yet. On synthetic sessions with a planted
relation the walk-forward recovers the planted sign, which shows the
plumbing works and nothing else.

**Where to look:** `python/src/iap/auction/`; docs/REAL_DATA.md §3.3;
DIAGRAMS.md §22. **How to run it:** COOKBOOK recipe 42.

### 3.8 Optimal execution: Almgren-Chriss, urgency, volume curves (v1.10)

Three opt-in upgrades to the parent-order schedules of §3.1. The pinned
TWAP / VWAP / IS schedules and their goldens are unchanged.

- **Almgren-Chriss** (`iap.execution.optimal`). The closed-form trajectory
  that minimises expected cost plus `λ` times its variance, given volatility
  and a temporary impact slope (from a calibration document, or the config
  default). `efficient_frontier` returns expected cost and variance across
  `λ`; `ParentOrder(is_model=ISModel.ALMGREN_CHRISS, ac_params=...)` uses it.
  At `λ = 0` the trajectory is a straight line; higher risk aversion
  front-loads.
- **Alpha urgency** (`iap.execution.urgency`). If the alpha agrees with
  waiting (the price is expected to move in the order's favour), post; if
  it is adverse, cross and front-load.
- **Forecast volume curve** (`iap.execution.volume_curve`). An intraday
  volume profile per instrument from TRADE events, shrunk toward the
  U-shape by `days / (days + shrinkage)`, used by VWAP.

**Where to look:** API_TRADING.md §2.8. **How to run it:** COOKBOOK recipes
41 and 48.

---

## 4. Risk: the engine that says no

### 4.1 The rule order

Every order passes through one function with a pinned sequence of checks.
The first check that fails is the decision, and its id is recorded:

```
 0 CONFIG_MISSING / NOT_BOOTSTRAPPED     12 FX_RATE_MISSING
 1 KILL_GLOBAL                           13 FAT_FINGER_NOTIONAL
 2 KILL_STRATEGY                         14 PRICE_BAND
 3 KILL_INSTRUMENT                       15 RATE_THROTTLE
 4 KILL_VENUE                            16 SELF_MATCH
 5 MALFORMED_ORDER                       17 POSITION_LIMIT
 6 UNKNOWN_INSTRUMENT                    18 INSTRUMENT_NOTIONAL
 7 DUPLICATE_ORDER_ID                    19 GROSS_NOTIONAL
 8 VENUE_DISCONNECTED                    20 NET_NOTIONAL
 9 SEQUENCE_GAP                          21 DAILY_LOSS
10 STALE_PRICE                           22 STRATEGY_LOSS
11 FAT_FINGER_QTY                        otherwise ALLOW
```

The order is part of the contract. Two implementations that both reject
an order, for different reasons, are not equivalent.

### 4.2 Fail-closed

The engine's inputs are reference data, limits, market data and fills.
When any of them is missing or cannot be trusted, the answer is REJECT:

- no configuration, or a configuration that does not parse → every order
  rejects;
- a restart without restored state → every order rejects until positions
  are bootstrapped;
- a sequence gap, or a reference price older than the stale timeout →
  reject;
- a position that cannot be valued → the notional check rejects rather
  than skipping it.

The v1.3.0 review found four places where the engine failed *open*
instead, and closed them in all three languages with identical reason
text:

| input nobody had tried | what happened | now |
|---|---|---|
| a reference price stamped in the future | its age was negative, never "too old", so it was trusted | `STALE_PRICE` when it is stamped beyond the engine's event clock by more than the stale timeout |
| NaN in a limit comparison | `NaN > limit` is false, so the order passed | comparisons are written `not (x <= limit)`; invalid reference data fails closed |
| arithmetic overflowing a 64-bit integer | Python raised, Rust's test build panicked, Rust's release build and Java wrapped | a projection that overflows rejects; a fill that cannot be booked latches the global kill |
| an order sent to venue 0 ("let the router choose") | skipped the venue kill and disconnect checks | rejected while any venue kill is engaged, or when every known venue is down |

The engine never reads an alpha's confidence, a lifecycle state or a model
output, and nothing may weaken a risk decision on the strength of one.

### 4.3 Kill switches

A kill switch stops new orders at one of four scopes: **global**,
**strategy**, **instrument**, **venue**. It latches — it never clears
itself. Loss limits engage it automatically: daily P&L is realised plus
unrealised, marked on every market update, so a position can breach its
limit with no fill at all. Re-arming has a fixed order (override the
limit, then clear the switch), and a restart is not a way to clear one: a
latched kill is checkpointed and restored.

An operator can engage one over an authenticated HTTP call on the Java
platform. Since v1.3.0 that kill takes effect the moment it is accepted,
before the trading thread records it; if recording takes longer than the
wait, the caller is told `202` (latched) rather than that it failed, for as
long as the session is running.

### 4.4 Three languages, one byte stream

The engine exists three times: Rust (the normative text), Java (the
platform) and Python (the reference loop). They are not merely required to
agree on ALLOW or REJECT. For the same script of orders, fills and market
updates all three must produce the same decision, the same rule id, the
same severity, and an **audit log that is byte-identical**. Money in a
reason string goes through one integer-scaled formatter, because
`format!("{:.2}")` and `String.format("%.2f")` round ties differently.

Two golden fixtures pin this:

- the **main golden** — one engine, a 110-step script, 59 orders, a
  77-line audit, a snapshot taken mid-script and restored;
- the **edge golden** (new in v1.3.0) — eight independent scenarios, each
  with its own engine, covering what one script cannot reach: venue kills
  and orders through the router, kill commands that do not parse, a mark
  from the future, timestamp and position overflow, unvaluable exposure, a
  missing configuration key, bootstrap and restore.

What byte parity does not prove: that the shared behaviour is right. The
three engines agreed on every golden step while disagreeing on overflow,
because no golden step overflowed. Comparing the engines on generated
inputs (differential fuzzing) is backlog (EPICS E31).

**Where to look**

- `rust/risk/src/` — `limits_eval.rs` is the rule order; `engine.rs`,
  `killswitch.rs`, `audit.rs`.
- `python/src/iap/risk/engine.py`, `java/src/main/java/com/iap/risk/`.
- `tests/golden/expected_risk_*.json` and `expected_risk_edge_*`.
- `PLATFORM_CONVENTIONS.md` §11.1, `API_TRADING.md` §1 (and §1.4),
  `docs/runbooks/RUNBOOK_incident_kill_switch.md`.
- `docs/DIAGRAMS.md` §5 and §11; LEARN.md §9 and §21.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_risk_golden.py tests/test_risk_rules.py
# with a Rust toolchain: cd rust && cargo test -p risk
```

---

## 5. ML: what was tried, and what it showed

### 5.1 The model zoo and its gate

The ML layer asks whether a model over many features predicts returns
better than the hand-built alphas. It has a ladder: linear baselines
(OLS, ridge, elastic net), then gradient-boosted trees (XGBoost, LightGBM)
and a small neural network.

The ladder has a gate. **The advanced models run only if the best linear
baseline shows positive out-of-sample IC against the mid-to-mid label.**
Simple models establish that a signal exists; complex models may then
refine it. If the linear model finds nothing, a tree will find the noise.

**Result: the gate passes, and no model earns its costs.** The best linear
baseline (ridge) scores an IC of +0.008 against the mid-to-mid label, so
XGBoost, LightGBM and the network were fitted. None of them improves on
it: their mid-label ICs are +0.005, +0.008 and −0.002. After conservative
costs every one of the six models loses money per signal, from −0.11 bps
(OLS, ridge, LightGBM) to −2.61 bps (the network). An IC of 0.008 is
positive and is not a tradable edge; the gate says a model may be tried,
not that it works.

On the v1.3.0 dataset the same gate failed (ridge −0.043) and the advanced
models were never fitted. The sign of a number that small moved with the
dataset; the conclusion — nothing here should be promoted — did not.

The same linear models score an IC of 0.96 against the *cost-adjusted*
label. That number is an artefact: the cost-adjusted target contains the
observable spread, and predicting the spread is easy. The gate once read
that label and passed trivially. The lesson is in LEARN.md §7.2 — a model
can ace its target and tell you nothing about alpha.

### 5.2 Meta-labelling

Meta-labelling separates direction from conviction. A primary model says
which way; a second model predicts whether acting on that signal will be
profitable after costs, and the trade is taken only above a probability
threshold.

**Result: the gate is degenerate on this data.** 6.2 % of test signals are
profitable after costs. The calibrated meta-model (AUC 0.670) puts every
test probability below the threshold, so it takes zero of 4,100 trades.
The report flags this `gate_degenerate: true` and does not present it as a
good decision: a gate that never fires is no evidence either way. (Since
v1.5.0 a missing meta-feature stays missing for the tree model —
`impute_nan=False`, 275 of 131,880 values — where it used to be imputed to
zero; `impute_nan=True` is the legacy rule. The AUC moved from 0.655 and
the outcome did not.)

### 5.3 Drift monitoring and refit policies

Models decay. The adaptive layer measures it with three monitors: PSI
(population stability index — has the distribution of a feature or signal
shifted?), a KS statistic (diagnostic only), and the rolling realised IC
compared with the research baseline as a z-score. A refit can be
triggered on a schedule or by drift (PSI above 0.25, or the IC z-score
below −2). A live sub-machine moves a decaying alpha ACTIVE → WATCH →
RETIRED on its rolling IC.

Two of those rules were corrected in v1.3.0 as opt-ins and are the
defaults since v1.5.0:

- **The z-score** is a two-sample HAC z of the pair-count-weighted rolling
  IC (`ic_z_method = "hac"`): it treats the baseline mean as an estimate
  with its own variance and the live bucket ICs as autocorrelated. The
  legacy rule (`"legacy"`) treated the baseline as a known constant and
  the buckets as independent and equally informative.
- **Retirement** is a CUSUM (`breach_rule = "cusum"`, slack 0.0025,
  threshold 0.01): each reading adds its shortfall below the gate, weighted
  by the share of its window that is new, and the alpha retires on a
  breach once the sum reaches the threshold. The legacy rule
  (`"consecutive"`) retired on the sixth breach in a row — and six
  readings of a two-hour window that advances fifteen minutes at a time
  are mostly the same rows.

Both are ported to Java and Rust, with the legacy rule selectable by name.

Four refit policies were compared on ten alphas: static, weekly, daily and
drift-triggered.

**Result: no refit policy makes any alpha profitable.** On two synthetic
sessions the weekly schedule cannot fire even once, and the
drift-triggered policy fits 88 times in total (78 refits after the ten
initial fits; 122 fits under the v1.4.0 rules). Of the 40 deployments none
ends above zero: 21 trade and lose, 19 make no trade. The widest gap
between two policies on one alpha is 2,111 USD (FX11), a difference in
costs paid, not in edge found. The report opens with that statement. What
the study does establish is that the machinery is deterministic and
leak-free.

One caveat that stays: in the live Java loop the lifecycle state is
*observational*. A RETIRED alpha keeps trading and an alert pages a human;
nothing reduces the position automatically.

**Where to look**

- `python/src/iap/models/` (`zoo.py`, `metalabel.py`),
  `research/ml_reports/ML_REPORT.md`, `research/models/` (one manifest per
  fit).
- `python/src/iap/adaptive/` (`drift.py`, `lifecycle.py`),
  `research/adaptive_reports/ADAPTIVE_REPORT.md`, `API_ADAPTIVE.md`.
- LEARN.md §7 and §14; COOKBOOK recipes 6, 7, 18 and 19.

**How to run it**

```bash
grep -A 4 "^## The gate" research/ml_reports/ML_REPORT.md       # the gate rule and its PASSED result
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_model_gate.py tests/test_adaptive.py
```

`python3 research/ml_reports/run_ml.py` regenerates the report; it adds
tracked model runs under `research/models/`, so run it deliberately.

---

## 6. AI, LLMs and agents: the boundary, what exists, what is planned

### 6.1 The pinned boundary

One rule, stated in `docs/ARCHITECTURE.md` §11 and
`PLATFORM_CONVENTIONS.md` §13.7:

> LLM reasoning is never on the trading path. It is read-only. It cannot
> override a risk decision, move a lifecycle state, or send an order.

The reason is in §7 below: the trading path is a pure function of a
captured event stream and a configuration, and a replay must reproduce it
byte for byte. A model call is neither deterministic nor fast, and its
failure mode is a plausible sentence. Hard limits must be code.

### 6.2 What exists today

**No LLM is called anywhere in this repository.** What exists is the
machinery that an automated researcher, human or model, would have to go
through. It was built before any agent on purpose: an agent given the goal
"get an alpha promoted" will find every shortcut §2.7 and §2.11 describe
(cheaper costs, a chosen holdout, free looks) faster than a person.

| piece | since | what it does | where |
|---|---|---|---|
| research store safe for parallel writers | v1.3.0 | ledger updated under a lock; run ids and experiment directories claimed atomically | `iap.experiment.locking`, `validation/ledger.py`, `research/runner.py` |
| gate eligibility | v1.3.0 | a result from a flattering configuration is ledgered and is not promotion evidence | `iap.research.specs`, `eligibility.json` |
| import-policy test | v1.3.0 | a guarded trading-path module that imports a network client or an LLM SDK fails CI | `python/tests/test_import_policy.py` |
| write broker + blackboard | v1.7.0 | the only path for agent writes; append-only, hash-chained JSONL of tasks, claims, findings, pre-registrations | `iap.agents.broker`, `blackboard` |
| pre-registration gate | v1.7.0 | `research run` refuses before reading data unless the (alpha, horizon) hypothesis is on the verified board | `iap.agents.prereg_gate` |
| reserve on a hidden seed | v1.7.0 | final evaluation on a synthetic session the agent cannot see; pass/fail only; attempts capped | `iap.agents.reserve`, `reserve_runner` |
| signed human approvals | v1.7.0 | retire / reset lifecycle edges need an expiring, single-use, signed approval | `iap.agents.approvals` |
| read-only MCP server | v1.7.0 | six versioned read tools over stdio, no write path (AST-tested), no network listener | `iap.agents.mcp_server` (`iap-mcp --root .`) |
| agent evaluations, untrusted text | v1.7.0 | planted leak, seeded bug, shuffled-label null, fabricated citation; free text returned wrapped and flagged | `iap.agents.evals`, `untrusted` |
| governance G1-G4 | v1.10.0 | costed, code-bound, git-anchored, Ed25519-signed pre-registrations (below) | `iap.agents.fingerprint`, `anchor`, `signing` |

**The governance chain (v1.10).** One hypothesis, from registration to a
gated run:

1. **Register.** The agent sends `preregister(alpha, horizon, sign,
   hypothesis)` signed with its Ed25519 private key, which lives outside the
   repository. The request covers the agent id, the operation, a digest of
   the arguments and a single-use nonce.
2. **Verify and fingerprint.** The broker holds only public keys
   (`research/agents/agent_pubkeys.json`), so it can check the signature but
   cannot forge one. It refuses a reused nonce. It computes the alpha's
   fingerprint: a hash of the source files of the alpha's modules and every
   `iap.*` module they import, the declared features' registry entries, and
   the numpy / pandas / scipy versions.
3. **Debit a look.** The registration costs one look on the
   multiple-testing ledger, so registering many hypotheses raises everyone's
   threshold.
4. **Append.** The entry, with its signed request, goes onto the
   hash-chained blackboard.
5. **Anchor.** The board is committed and pushed before any data is read;
   `cli anchor` records the first commit that contains each entry.
6. **Gate.** `python -m iap.research run` checks the registration and
   recomputes the fingerprint; changed code, features or dependency
   versions are refused (re-registering with `--supersede` costs another
   look). A `--no-prereg` run is marked ineligible for promotion.
7. **Re-verify, offline, by anyone.** `cli verify-board` checks the chain,
   that every committed version of the board is a prefix of the current one
   (a rewritten, re-hashed board fails against its own history), the
   anchors, and every signature.

Pre-registrations made before v1.10 (the six 2026 holdout entries) have no
fingerprint and were not debited; they are anchored to commit `6723fd0`.
GOVERNANCE.md §2a lists what the fingerprint does not cover. LEARN.md §35
explains why the first drafts (HMAC keys, class-only code hashes) were
replaced.

**Where to look:** `python/src/iap/agents/`; docs/governance/GOVERNANCE.md
§2a; DIAGRAMS.md §23. **How to run it:** COOKBOOK recipe 47.

The import-policy test has stated gaps: it scans the guarded trading-path
packages and not `iap.replay` or `iap.trace`, and nothing scans the Rust,
C++ or Java trees; those are enforced in review.

### 6.3 What is still planned

The research agent itself (plan items AI1-AI4, v1.11): an LLM that drafts
hypotheses, registers them and runs experiments only through the signed
broker, judged by the v1.7 agent evaluations, plus a model registry and
drift checks for whatever it fits. The boundary in §6.1 does not move: the
agent would act on the research record, never on the trading path.

### 6.4 What would be theatre on this data

Four things that would look impressive and establish nothing, with the
reason for each:

- **Deep sequence models over the order book.** The dataset is two
  synthetic sessions, about 211,000 rows. The best linear IC against the
  frictionless label is 0.008, the one high IC in the ML report is spread
  prediction, and the models with more capacity that the gate of §5.1 let
  through did no better: the trees did not beat the linear baseline and
  the small network was the worst model fitted. A higher-capacity model
  would fit the generator's noise process.
- **Reinforcement-learning execution against the platform's own
  simulator.** The simulator's fills are a pinned rule set, its impact is
  a formula, the replayed book never reacts to our orders, and none of it
  is calibrated to real fills. An RL agent would learn the simulator. Had
  one been trained before v1.3.0 it would have found the crossing-pool
  defect and been rewarded for filling 1,000 shares against 50.
- **An LLM as an alpha.** The instruments are `SYN.EQ.001`. There is no
  news, no filings and no text of any kind for a language model to read.
  Any signal it produced would be un-auditable, every prompt variant would
  be a look for the ledger, and the boundary of §6.1 forbids it on the
  trading path in any case.
- **Agent debate.** Several agents arguing about 24 alphas on the same two
  sessions add no data. Evidence comes from sessions, and the ledger
  already holds 5,156 looks at them (1,068 on the v1.3.0 dataset, the rest
  on the regenerated one). Debate multiplies looks; it does not add a
  holdout. Without pre-registration and a hidden reserve seed — both
  backlog — there is also no way to score who was right.

What would not be theatre is the unglamorous list: real exchange data,
measured latency, a simulator calibrated to live fills, book-level risk
(EPICS E25–E28). The ingestion path for the first of these exists
([REAL_DATA.md](REAL_DATA.md)); no real file has been run through it yet.

**Where to look**

- `docs/ARCHITECTURE.md` §11, `PLATFORM_CONVENTIONS.md` §13.7.
- `python/tests/test_import_policy.py`, `docs/RESEARCH_VALIDITY.md` §3–§5.
- `docs/EPICS.md` — E24 and E30.
- `docs/DIAGRAMS.md` §19 — the planned layer, labelled as backlog.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m pytest -q tests/test_import_policy.py                         # the boundary, as a test
PYTHONPATH=src python3 -m iap.research --json-errors show 0000000000000000; echo "exit=$?"   # one JSON error object on stderr, exit 1
PYTHONPATH=src python3 -m iap.store build > /dev/null
PYTHONPATH=src python3 -m iap.store sql "SELECT pipeline_verdict, COUNT(*) AS n FROM v_alpha_scorecard_current GROUP BY pipeline_verdict ORDER BY pipeline_verdict"
```

---

## 7. Determinism and replay

Everything above rests on one property: **the same inputs produce the
same bytes.** Without it a golden test cannot exist, an incident cannot be
reproduced, and a research result cannot be re-derived.

The rules (`PLATFORM_CONVENTIONS.md` §3):

- **One random number generator**, SplitMix64, implemented identically in
  four languages, always seeded explicitly.
- **No wall clock** on any deterministic path. Time is event time, read
  from the data. Checkpoints happen every N events, not every N seconds.
- **No unordered iteration.** Maps are iterated in sorted key order, so
  floating-point sums accumulate identically everywhere.
- **Integers on contracts.** Prices in ticks, quantities in units,
  timestamps in nanoseconds.
- **Canonical JSON.** Sorted keys, compact separators, one pinned float
  format — reproduced byte for byte in Python, Java, Rust and C++.

What that buys:

- **Golden tests.** A file written by one implementation is reproduced by
  the others, exactly.
- **The decision trace.** Each decision is one canonical JSON line; the
  stream has a running SHA-256 digest. Run the MVP twice and the digests
  match. Replay it from its captured event stream and they match again.
- **Incident replay.** A captured stream plus the configuration in force
  reproduces every decision, so "why did we trade?" has an answer a week
  later.
- **Restart.** A risk snapshot restored at event *k* continues with
  bit-identical decisions. v1.3.0 made the Java platform's checkpoint
  itself consistent: the state file is the single commit point and names
  its risk snapshot by hash, so a crash cannot leave a cursor beside a
  risk state from a different moment.

One honest limit: determinism makes results *reproducible*, not *right*.
Three engines reproducing the same wrong answer pass every parity test.
That is what the v1.3.0 review found, twice.

**Where to look**

- `PLATFORM_CONVENTIONS.md` §3 and §13.1–§13.2; `docs/DECISION_TRACE.md`.
- `python/src/iap/core/rng.py`, `python/src/iap/contracts/versions.py`
  (`canonical_json`), `python/src/iap/trace/`.
- `tests/replay/`, `docs/runbooks/RUNBOOK_incident_replay.md`.
- `docs/governance/REPRODUCIBILITY.md`; LEARN.md §12, §18 and §25.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp verify                                         # runs the loop twice from scratch, compares bytes
PYTHONPATH=src python3 -m iap.mvp replay --run ../data/mvp/58a10f2194a3c81c      # same digest from the captured stream
```

---

## Where next

| if you want | read |
|---|---|
| the concepts taught properly, with worked numbers | [../LEARN.md](../LEARN.md) |
| commands for a specific task | [../COOKBOOK.md](../COOKBOOK.md) |
| the system design, failure modes and the gaps to production | [ARCHITECTURE.md](ARCHITECTURE.md) |
| the pictures | [DIAGRAMS.md](DIAGRAMS.md) |
| every pinned rule | [../PLATFORM_CONVENTIONS.md](../PLATFORM_CONVENTIONS.md) |
| what is done and what is backlog | [ROADMAP.md](ROADMAP.md), [EPICS.md](EPICS.md) |
| to run the pipeline on real historical files you obtained (ITCH 5.0, LOBSTER) | [REAL_DATA.md](REAL_DATA.md) |
| what changed in each release, v1.1.0 to v1.10.0 | [../CHANGELOG.md](../CHANGELOG.md) |
| the real-data studies: 7 sessions, power, the 2026 holdout, auctions | [REAL_DATA.md](REAL_DATA.md) §3 |
| the governance controls, one table | [governance/GOVERNANCE.md](governance/GOVERNANCE.md) §2a |
