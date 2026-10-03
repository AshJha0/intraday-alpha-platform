# How it works — the quant, algo and AI sides of the platform

A guided explanation for a technically literate newcomer. It goes top-down:
the pipeline on one page, then one section per side of the platform — quant
research, execution algorithms, hard risk, machine learning, the AI / agent
boundary, determinism. Every section ends with **where to look** (file
paths) and **how to run it** (a command that works from a checkout).

Three things to know before reading:

1. **All market data is synthetic.** A seeded generator produces every
   event. Every number below is a statement about that generator and this
   pipeline, not about a market.
2. **The results are negative, and that is the headline.** 24 alphas were
   researched; none is promoted. The end-to-end loop loses 22.65 USD on its
   golden session. The ML gate fails. The platform is built so that those
   sentences are trustworthy, not so that they read well.
3. **Corrected methods are opt-in.** Where the v1.3.0 review found a better
   statistic or policy, it was added *beside* the pinned default, with the
   default unchanged, so that no committed number moves. Each one is named
   below with its default.

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
session (16,578 events, 355 decisions) it fills 55 times and loses 22.65
USD: about +0.04 bps of alpha against −0.41 bps of execution cost.

**Where to look**

- `python/src/iap/mvp/engine.py` — the per-event order of the whole loop.
- `docs/MVP.md` — the same loop explained module by module, with its results.
- `docs/DIAGRAMS.md` §1 (pipeline) and §10 (the MVP loop).
- `PLATFORM_CONVENTIONS.md` — every pinned rule, by section.

**How to run it**

```bash
cd python
PYTHONPATH=src python3 -m iap.mvp run                                   # ~7 s; prints events, decisions, fills, P&L, digest
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
  own story cannot be promoted. EQ09 is the standing example: a large
  t-statistic, the wrong sign, REJECT.

The second rule is what stops a researcher from fitting first and writing
the story afterwards.

### 2.3 Labels and horizons

A label is the thing a signal is supposed to predict: the forward return
from time `t` to `t + h`. They are computed in event time at 11 horizons
from 10 ms to 15 minutes, in two forms — mid-to-mid, and cost-adjusted
(paying half the spread at both ends). A label is *valid* only if the
stream was observed through `t + h`, the book was tradable at both ends,
and no halt, auction or stale-venue gap falls inside the horizon. A signal
just before a halt is therefore never credited with the reopening jump.

### 2.4 IC

The information coefficient is the correlation between score and label
over out-of-sample rows. It is the basic measure of "does the signal point
the right way". Two details matter here:

- The gate reads the IC on **uncrossed** rows only. A merged book whose bid
  is above its ask is two venues disagreeing, not a price anyone could
  trade; on FX it inflated one alpha's IC from 0.041 to 0.117.
- The pooled IC is computed on the standardised signal, not on the fitted
  expected return, so that a sign flip between folds cannot hide.

Opt-in, reported beside it and read by no gate: a per-instrument mean IC
and a volatility-scaled IC (so one volatile instrument cannot dominate the
pool), and an IC that scores halted rows at the realised reopen return
(`compute_labels(blackout_reopen=True)`, default off).

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

### 2.6 Newey–West t, and the pooled-slope variant

An IC needs a significance test, and the rows are not independent —
overlapping labels are autocorrelated. The pinned test computes one IC per
five-minute bucket, takes their mean weighted by the number of pairs in
each bucket, and divides by a Newey–West (Bartlett-weighted) long-run
standard error. The gate asks for t ≥ 3.0.

That statistic has a blind spot. A within-bucket correlation demeans score
and label inside each bucket, so signal that lives *between* buckets is
removed from the test — while the gate IC beside it is pooled and keeps it.
The **pooled-slope HAC t** tests the pooled slope directly with the same
buckets and weights. It is reported as `nw_tstat_pooled`; the gate still
reads the pinned statistic.

### 2.7 The multiple-testing ledger and looks

Try enough things and something will look significant. So every look at
the data is recorded in a ledger (`research/experiments.json`), identified
by alpha, kind and configuration, so that re-running a script does not
inflate the count. Today it holds 1,068 looks over 70 distinct
configurations. From that count come two yardsticks printed in every
report: the largest t expected from pure noise over that many trials
(about 3.73) and the Bonferroni threshold (about 4.07). An alpha with a t
of 3.2 passes the fixed gate and sits *below* both yardsticks, and the
report says so.

Two things changed in v1.3.0. A `--dry-run` used to compute everything and
record nothing; it now debits its looks. And the gate can be made to follow
the count: `tstat_threshold="ledger"` (default `"fixed"`, 3.0) uses the
larger of 3.0 and the ledger's Bonferroni threshold.

### 2.8 Cost model and capacity

The research cost model charges half the spread, venue fees, and an impact
that is linear in the order's share of daily volume. This is where every
alpha dies: EQ03 has an IC of 0.030 with a t above 10 and loses 70,651 at
1× costs, because it flips its position hundreds of times an hour and pays
the spread each time.

Opt-in alternatives, each with the pinned default unchanged:

| option | default | what the option does |
|---|---|---|
| `CostModel(impact_model="sqrt")` | `"linear"` | square-root impact in participation |
| `BacktestConfig(cap_fills_at_l1=True)` | off: any size fills at the touch | no fill larger than the displayed top-of-book size |
| `BacktestConfig(position_policy="cost_aware")` | `"sign"`: follow the sign of the signal on every row | enter only when the expected return exceeds the round-trip cost, hold for the label horizon |
| `capacity_breakeven` | capacity = participation cap × volume × price | the order size at which the edge equals its own cost |

The cost-aware policy does not rescue anything. In COOKBOOK recipe 28 —
EQ03 scored over both sessions with the default backtest configuration, so
not comparable with the report's 70,651 — it reduces 25,418 trades to 4 and
a loss of 458,645 to a loss of 70, because the signal almost never clears
its own costs. That is the same finding from the other side.

### 2.9 Stress

Each alpha is re-run under harsher assumptions: costs at 0.5×, 1× and 2×;
latency of 100 ms to 5 s; high- and low-volatility regimes. An alpha that
only works at zero latency is not an alpha. (`stress_version=2`, default 1,
is a corrected construction of one of the latency grids; the committed
reports use version 1.)

### 2.10 Leakage probes

Leakage is using information that was not available at decision time. It
produces beautiful backtests. Four detectors; the first three run on every
validation:

1. **Label guard.** Score once normally and once with every label column
   replaced by garbage. Any difference proves the scoring path reads labels.
2. **Shift-by-one.** Lag the scores by one row. A real signal degrades
   gracefully; a leak collapses.
3. **Truncation probe.** Re-score on a truncated feature frame. The score
   at the cut must be bit-identical.
4. **Recompute probe** (opt-in). Rebuild the *features* from truncated raw
   events. This catches look-ahead baked into a feature itself, which the
   first three cannot see.

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
one fails the same gate: net P&L after costs.

Since v1.3.0 there is one more condition, and it is about the evidence
rather than the alpha. A research result is **gate-eligible** only if it
was produced under the pinned protocol or something stricter (costs at 1×
or more, at least four folds, the standard latency and embargo) and on the
periods derived from the dataset. Any other configuration still runs and
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
| order flow | half the reference | 1 of 3 | 3 of 3 | 0 of 3 |
| order flow | reference | 3 of 3 | 3 of 3 | 0 of 3 |
| order flow | twice the reference | 3 of 3 | 3 of 3 | 0 of 3 |
| lead-lag | none (null) | 0 of 3 | 1 of 3 | 0 of 3 |
| lead-lag | reference | 0 of 3 | 1 of 3 | 0 of 3 |
| lead-lag | twice the reference | 1 of 3 | 2 of 3 | 0 of 3 |
| either, reversed mid-sample | any | 0 of 3 | 0 of 3 | 0 of 3 |

What it means:

- The chain **can detect** a planted order-flow effect, reliably at the
  reference size (mean IC 0.073, mean t 5.96).
- It has **almost no power** against the planted lead-lag at the reference
  size. A real effect like that would be reported as absent.
- It is **not fooled** by an effect that reverses half-way through.
- The null row is not perfectly clean: one seed of three produced ITERATE
  with nothing planted.
- It **promotes nothing, at any size** — not even an effect with a mean t
  above 13. No fold survives 1× costs in any cell.

The last point limits what the headline can claim. "0 PROMOTE" is not
proof that the chain is a strict judge of alpha; PROMOTE has not been shown
to be reachable in this generator's cost structure. With three seeds per
cell a rate moves in steps of a third: this calibrates the chain, it is
not a power curve.

**Where to look**

- `python/src/iap/features/` (registry, engine), `python/src/iap/alpha/`
  (base class and the 24 alphas), `python/src/iap/labels/`.
- `python/src/iap/validation/` — `splits.py`, `metrics.py`, `leakage.py`,
  `stress.py`, `ledger.py`, `validate.py`, `diagnostics.py`.
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
PYTHONPATH=src python3 -m iap.research show 217fa0cb1d89a9c8   # one committed experiment: spec, result table, VERDICT, eligibility
PYTHONPATH=src python3 -m iap.lifecycle status                 # 24 alphas, all CANDIDATE, with the gate each one fails
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

All four changes can only reduce simulated fills. The fills golden, the
MVP golden and the TCA golden are reproduced unchanged, because the golden
sessions contain none of these patterns.

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

**Result: the gate fails.** The best linear baseline (ridge) scores an IC
of −0.043 against the mid-to-mid label. XGBoost, LightGBM and the network
were never fitted on this data.

The same linear models score an IC of 0.94 against the *cost-adjusted*
label. That number is an artefact: the cost-adjusted target contains the
observable spread, and predicting the spread is easy. The gate once read
that label and passed trivially. The lesson is in LEARN.md §7.2 — a model
can ace its target and tell you nothing about alpha.

### 5.2 Meta-labelling

Meta-labelling separates direction from conviction. A primary model says
which way; a second model predicts whether acting on that signal will be
profitable after costs, and the trade is taken only above a probability
threshold.

**Result: the gate is degenerate on this data.** 3.7 % of test signals are
profitable after costs. The calibrated meta-model (AUC 0.753) puts every
test probability below the threshold, so it takes zero of 11,461 trades.
The report flags this `gate_degenerate: true` and does not present it as a
good decision: a gate that never fires is no evidence either way.

### 5.3 Drift monitoring and refit policies

Models decay. The adaptive layer measures it with three monitors: PSI
(population stability index — has the distribution of a feature or signal
shifted?), a KS statistic (diagnostic only), and the rolling realised IC
compared with the research baseline as a z-score. A refit can be
triggered on a schedule or by drift (PSI above 0.25, or the IC z-score
below −2). A live sub-machine moves a decaying alpha ACTIVE → WATCH →
RETIRED on its rolling IC.

Four refit policies were compared on ten alphas: static, weekly, daily and
drift-triggered.

**Result: no policy demonstrably beats static.** On two synthetic sessions
the weekly schedule cannot fire even once, the drift-triggered policy
refits 126 times in total, every policy is net-negative after costs, and
the differences between policies are within noise. The report opens with
that statement. What the study does establish is that the machinery is
deterministic and leak-free.

Opt-in since v1.3.0, in the Python reference only: a two-sample HAC form of
the IC z-score (`ic_z_method = "hac"`, default `"pinned"`) and a CUSUM
retirement rule that weights each reading by its new information
(`breach_rule = "cusum"`, default `"consecutive"`).

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
grep -A 4 "^## The gate" research/ml_reports/ML_REPORT.md       # the gate rule and its FAILED result
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
  synthetic sessions, about 206,000 rows. The linear baseline already
  fails the gate against the frictionless label, and the one high IC in
  the ML report is spread prediction. A high-capacity model would fit the
  generator's noise process. The gate of §5.1 exists to stop exactly this.
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
  sessions add no data. Evidence comes from sessions, and these have been
  looked at 1,068 times. Debate multiplies looks; it does not add a
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
| what changed in v1.3.0 | [../CHANGELOG.md](../CHANGELOG.md) |
