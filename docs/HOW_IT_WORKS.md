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
re-running a script does not inflate the count. Today it holds 4,396 looks
over 208 entries: 1,068 were taken on the v1.3.0 dataset, 852 on the
v1.4.0 dataset under the old rules, and 2,476 were added when v1.5.0
re-ran everything under the new ones. A statistic computed on a different
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
| CANDIDATE → VALIDATING | leakage clean; out-of-sample IC; statistical significance; fold consistency; fold count; hypothesis sign; **net P&L after costs**; capacity; stability |
| VALIDATING → PAPER | holdout IC tracks research; replay reproducible; cross-language parity |
| PAPER → ACTIVE | minimum paper sessions; paper IC tracking; paper net P&L; no kill events |
| ACTIVE ⇄ WATCH → RETIRED | rolling live IC |

On the bundled data all 24 alphas reach CANDIDATE and stop there. Every
one fails the same two gates: net P&L after costs, and capacity (an alpha
that does not trade, or trades at a loss, has an edge-breakeven capacity
below the 1,000,000 USD the gate asks for). For three of them (EQ02, EQ03,
EQ12) those are the only gates that fail. Statistical significance fails
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
power`). From `research/power/POWER_REPORT.md`, three seeds per cell:

| planted effect | size | flagged significant | verdict ITERATE or better | PROMOTE |
|---|---|---:|---:|---:|
| order flow | none (null) | 0 of 3 | 0 of 3 | 0 of 3 |
| order flow | half the reference | 0 of 3 | 0 of 3 | 0 of 3 |
| order flow | reference | 1 of 3 | 3 of 3 | 0 of 3 |
| order flow | twice the reference | 3 of 3 | 3 of 3 | 0 of 3 |
| lead-lag | none (null) | 0 of 3 | 0 of 3 | 0 of 3 |
| lead-lag | half the reference | 0 of 3 | 1 of 3 | 0 of 3 |
| lead-lag | reference | 0 of 3 | 1 of 3 | 0 of 3 |
| lead-lag | twice the reference | 0 of 3 | 2 of 3 | 0 of 3 |
| either, reversed mid-sample | any | 0 of 3 | 0 of 3 | 0 of 3 |

"Flagged significant" is the gate statistic of §2.6, the pooled-slope t,
at 3 or more. The report also scores the within-bucket t and the pooled t
against the study's own multiple-testing threshold (42 tests, t ≥ 3.24);
the three give the same rate in every cell.

What it means:

- The chain **can detect** a planted order-flow effect, reliably only at
  twice the reference size (mean gate IC 0.082, mean t 6.20). At the
  reference size it reports evidence in every seed and significance in one
  of three (mean IC 0.033, mean t 2.93). At half the reference it sees
  nothing.
- It has **no measured power to call the planted lead-lag significant** at
  any size tested (mean t 1.75 at twice the reference). Under the pooled t
  it now reaches ITERATE in one or two seeds of three — under the v1.4.0
  rules it never did — which is evidence to keep looking, not a detection.
- It is **not fooled** by an effect that reverses half-way through.
- The null rows are clean: no seed produced a detection with nothing
  planted. Three seeds per cell cannot bound a false-positive rate.
- It **promotes nothing, at any size** — not even an effect with a mean t
  above 6. At the reference size the cost-aware backtest makes no trade at
  all on the planted effect; at twice that size it makes 14 on average, and
  no fold survives 1× costs in any cell.

The last point limits what the headline can claim. "0 PROMOTE" is not
proof that the chain is a strict judge of alpha; PROMOTE has not been shown
to be reachable in this generator's cost structure: the chain can see an
effect it cannot monetise. With three seeds per
cell a rate moves in steps of a third: this calibrates the chain, it is
not a power curve.

Two comparisons with earlier runs. The detection rates of the order-flow
effect are the same under the v1.5.0 rules as under the v1.4.0 rules on
the same generator. And the chain is less sensitive on the v1.4.0
generator than it was on the v1.3.0 one, where the order-flow effect was
significant in 3 of 3 seeds at the reference size and in 1 of 3 at half of
it; the planted effects and the seeds are the same, and the flow they are
planted in is about 2.5 times sparser in time.

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

**There is no LLM, agent or MCP code in this repository.** No model is
called anywhere. What exists is the foundation an agent layer would need —
each piece useful to a human or a script without one:

| piece | what it does | where |
|---|---|---|
| a research store safe for parallel automated writers | the ledger is updated under a lock by re-read, replay and atomic replace; run ids and experiment directories are claimed atomically | `python/src/iap/experiment/locking.py`, `validation/ledger.py`, `research/runner.py` |
| gate eligibility | a result produced under a flattering configuration is recorded and counted, and is not promotion evidence | `python/src/iap/research/specs.py`, `eligibility.json` |
| an import-policy test | fails if a module of the guarded Python packages imports a network client or an LLM SDK | `python/tests/test_import_policy.py` |
| machine-readable tooling | `list --json`, `show --json`, `--json-errors` with stable error codes; a read-only, one-statement SQL command over the store | `python -m iap.research`, `python -m iap.store sql` |

The import-policy test has stated gaps: it scans nine Python packages and
not `iap.replay` or `iap.trace`, and nothing scans the Rust, C++ or Java
trees — those are enforced in review.

### 6.3 The planned agent layer — backlog

Everything in this subsection is **planned and not built**. It is the
content of two backlog epics in [EPICS.md](EPICS.md): E24 (a read-only
research layer) and E30 (the controls that would let several agents work
without being able to fool the platform or each other).

| planned control | issue | what it is for |
|---|---|---|
| read-only MCP server | AG01, AL05 | agents read the ledger, reports, lifecycle log and decision traces through resources with no side effects |
| write broker and blackboard | AL01 | the only path by which an agent changes repository, ledger or lifecycle state; an append-only log of tasks, claims and findings |
| pre-registration | AL02 | a hypothesis is committed before any data is read, so it cannot be written to fit the result |
| reserve sessions on a hidden seed | AL03 | final evaluation on data generated from a seed the agents never see |
| authenticated human approval | AL04 | every lifecycle edge whose actor is HUMAN needs an authenticated approval — today the actor is asserted, not verified |
| agent evaluations | AL06 | planted leak, seeded bug, shuffled-label null, citation resolution — each must fail when its control is removed |
| untrusted free-text handling | AL07 | text from ledgers, reports and tool output is data, not instructions |

The order matters. The controls come before the agents because an agent
with a goal ("get an alpha promoted") will find every shortcut §2.7 and
§2.11 describe — cheaper costs, a chosen holdout, free looks — faster than
a person would. v1.3.0 closed those three in the tooling. It did not build
the agents.

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
  already holds 4,396 looks at them (1,068 on the v1.3.0 dataset, the rest
  on the regenerated one). Debate multiplies looks; it does not add a
  holdout. Without pre-registration and a hidden reserve seed — both
  backlog — there is also no way to score who was right.

What would not be theatre is the unglamorous list: real exchange data,
measured latency, a simulator calibrated to live fills, book-level risk
(EPICS E25–E28).

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
PYTHONPATH=src python3 -m iap.store sql "SELECT verdict, COUNT(*) AS n FROM v_alpha_scorecard GROUP BY verdict ORDER BY verdict"
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
| what changed in v1.3.0, v1.4.0 and v1.5.0 | [../CHANGELOG.md](../CHANGELOG.md) |
