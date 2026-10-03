# Alpha Decay versus Latency and Infrastructure Investment: What One Event of Lag Costs

> Dated record. The figures below are those of the dataset in force when the paper was written; the 2026-10-03 update at the end restates them on the v1.4.0 dataset and re-checks each conclusion.

*Intraday Alpha Platform research series, paper 4 of 6 (spec §28). Generated 2026-08-29 from the repository's committed research artifacts.*

---

## Abstract

We quantify the economics of latency for the platform's 24 flagship alphas
by combining two committed measurement sets: the validation framework's
latency stress, which re-scores every alpha with its signal lagged by
{+0, +1, +5} market events (spec §13), and the performance benchmarks of the
C++/Rust/Java ports (spec §22). The stress results separate the alpha book
cleanly by decay speed: the fast equity flow alphas (OFI family, queue
dynamics, microprice) lose ~20-37% of last-fold IC after a single event of
lag and ~75% or more after five (EQ02: 0.0273 → 0.0215 → 0.0068; EQ01's
microprice IC flips sign, 0.0213 → −0.0131 at +5), while the slow equity
alphas are essentially latency-insensitive even at five events (EQ09:
0.0862 → 0.0870 → 0.0833) and the FX regime family retains ~80% at one
event but only ~25-30% at five (FX09: 0.1414 → 0.1138 → 0.0345). In money
terms the latency effect is invisible on this dataset: every alpha is
cost-negative at 1x, and the stressed net P&L moves by well under 1%
because modeled costs dwarf gross alpha. Against this we set the measured
cost of computation: the C++ hot path processes decode + book + features +
alpha in ~0.5 µs/event (3.5 + 17.4 + 450 + 32 ns/event,
`benchmarks/results_cpp.md`) — six orders of magnitude below the ~0.9 s
median inter-event gap on the synthetic equity feed. On this platform the
entire measured "latency" penalty is information staleness (reacting events
late), not compute time; software throughput (C++ 37.1M replay events/s;
Rust ≈ 6.9M; Java ≈ 3.5M) matters for replay, burst catch-up and research
iteration, not for keeping up with the feed. We state the methodology
(2-CPU container, no pinning, mean-only timings) with every number. All
market data is synthetic.

---

## 1. Introduction

"How much is latency worth?" decomposes into two different questions:

1. **Decay:** how much predictive power does a signal lose per unit of
   delay between decision and execution?
2. **Infrastructure:** how much delay does the trading stack itself add,
   and what does reducing it cost?

The platform measures both natively. The stress module
(`python/src/iap/validation/stress.py`) re-runs every alpha's backtest and
IC with the executed signal lagged by 0, 1 and 5 emission events
(`LATENCY_SHIFTS = (0, 1, 5)`; grid pinned in `configs/execution/execution.json` as
`latency_shift_events_stress`), on top of a backtester that already never
executes on the decision row (`latency_rows = 1` default,
`python/src/iap/backtest/engine.py`). The benchmark suite
(`cpp/bench/bench_all.cpp`, results in `benchmarks/results_cpp.md`) measures
what each pipeline stage costs in nanoseconds. This paper joins the two.

## 2. Data and methodology

**Market data is synthetic** (two seeded days, 11 equity instruments with
MBO on 2 venues and 8 FX pairs quoted on 3 venues;
`python/src/iap/marketdata/generator.py`, volumes in
`data/normalized/qc_report.json`). Alpha statistics come from the
walk-forward validation run recorded in `research/alpha_reports/REPORT.md`
and the per-alpha JSONs (4 expanding purged/embargoed folds; latency stress
reported on the last fold).

**Event-time units.** A "+1 event" shift means the executed signal is one
feature-engine emission stale. Median inter-emission gaps, computed from the
committed feature frames: ~0.9 s for equity instrument 1 (14,105 rows,
`data/features/features_1.parquet`) and ~14.9 s for FX instrument 101
(6,573 rows, `features_101.parquet`). Event-time shifts are the honest
unit here: wall-clock latency stress would be dominated by the synthetic
feed's arrival process rather than by anything a trading system controls.

**Benchmark methodology (spec §22, stated in full).** All timing numbers
come from a 2-CPU Intel Xeon @ 2.10 GHz container without CPU pinning.
C++: g++ 13.3.0, `-O3 -DNDEBUG`, C++17, steady-clock wall loops, 3 warmup
iterations then ≥ 0.5 s and ≥ 10 iterations per benchmark, single-threaded,
workload = the pinned golden vectors (2,000 equity MBO events, 800 FX QUOTE
events); mean-only figures, no tail latencies, cross-run variance a few
percent (`benchmarks/results_cpp.md`). Rust and Java figures are demo-scale
replay throughputs (rustc 1.95.0 release build, `rust/replay/src/bin/demo.rs`;
OpenJDK 21.0.10, `java/src/main/java/com/iap/replay/Demo.java`, 5 warmup +
25 timed full replays) and are not boundary-identical to the C++ bench —
see §4.3.

## 3. Results: alpha decay under event lag

### 3.1 The full grid

Last-fold IC at +0/+1/+5 events of lag, all 24 alphas
(`research/alpha_reports/REPORT.md`, cost/latency stress table; retention =
+1ev IC / +0ev IC, shown where +0ev IC > 0.01):

| alpha | +0ev IC | +1ev IC | +5ev IC | IC retained after 1 event |
|---|---|---|---|---|
| EQ01 | +0.0213 | +0.0134 | -0.0131 | 63% |
| EQ02 | +0.0273 | +0.0215 | +0.0068 | 79% |
| EQ03 | +0.0267 | +0.0214 | +0.0085 | 80% |
| EQ04 | +0.0002 | -0.0059 | -0.0131 | — |
| EQ05 | +0.0188 | +0.0139 | +0.0222 | 74% |
| EQ06 | +0.0456 | +0.0467 | +0.0471 | ~100% |
| EQ07 | +0.0169 | +0.0172 | +0.0139 | ~100% |
| EQ08 | +0.0600 | +0.0587 | +0.0502 | 98% |
| EQ09 | +0.0862 | +0.0870 | +0.0833 | ~100% |
| EQ10 | +0.0004 | -0.0049 | -0.0035 | — |
| EQ11 | +0.0271 | +0.0268 | +0.0262 | 99% |
| EQ12 | +0.0272 | +0.0207 | +0.0062 | 76% |
| FX01 | -0.0187 | -0.0075 | -0.0005 | — |
| FX02 | -0.0254 | -0.0218 | +0.0099 | — |
| FX03 | +0.0036 | +0.0015 | +0.0067 | — |
| FX04 | +0.0076 | -0.0095 | -0.0036 | — |
| FX05 | +0.0127 | +0.0015 | -0.0250 | 12% |
| FX06 | +0.0682 | +0.0491 | +0.0126 | 72% |
| FX07 | -0.0117 | -0.0354 | -0.0397 | — |
| FX08 | +0.1191 | +0.0999 | +0.0470 | 84% |
| FX09 | +0.1414 | +0.1138 | +0.0345 | 80% |
| FX10 | +0.0962 | +0.0748 | +0.0046 | 78% |
| FX11 | +0.0832 | +0.0653 | +0.0248 | 78% |
| FX12 | +0.0007 | -0.0029 | -0.0138 | — |

### 3.2 A three-class taxonomy

The two columns together (one-event retention *and* five-event survival)
sort the book into three latency classes:

- **Fast flow/microstructure alphas (63-80% at +1, ≤ ~35% at +5):**
  EQ01/EQ02/EQ03/EQ12 — microprice and OFI signals whose conditioning
  state is refreshed and mean-reverted event by event; EQ01 flips sign
  outright at +5. These are the signals for which reacting within a few
  events is existential; they are also, on this dataset, cost-negative
  regardless (paper 1), so latency spend cannot rescue them here.
- **Slow equity alphas (~100% even at +5):** EQ06-EQ09 and EQ11 — VWAP
  deviation, sector-residual reversion, time-of-day and multi-day-scale
  constructions whose IC is statistically unchanged five emissions late.
  Latency infrastructure is irrelevant to their statistics.
- **FX regime alphas (~72-84% at +1, ~5-40% at +5):** FX06, FX08-FX11 —
  minute-scale conditional reversion/momentum for which one event (~15 s
  in FX) is a minor perturbation but five (~75 s) removes most of the
  edge.

(EQ04's, EQ10's and FX12's entries are noise at |IC| ≤ 0.01; EQ05's rising
+5ev value is small-sample noise on the same scale; we do not interpret
them.)

### 3.3 Money terms — and why there mostly aren't any here

On the refit alpha book, *no* alpha is net-positive at 1x costs
(REPORT.md: 0 PROMOTE), and the latency term is invisible in net money.
Stressed net P&L for the three lowest-turnover equity alphas
(`research/alpha_reports/EQ06.json`, `EQ08.json`, `EQ09.json`,
`stress.latency`):

| alpha | +0ev P&L | +1ev P&L | +5ev P&L |
|---|---|---|---|
| EQ06 | -87,643 | -87,319 | -86,621 |
| EQ08 | -7,543 | -7,439 | -7,746 |
| EQ09 | -25,989 | -25,935 | -26,266 |

The P&L deltas across shifts are fractions of a percent — pure noise
against a cost base that exceeds gross alpha by orders of magnitude
(paper 1). This is itself the finding: **when costs dominate gross alpha,
latency has no money value at all** — the IC retention grid of §3.1 is the
only channel through which staleness matters, and it becomes economic only
for a strategy whose costs are first brought below gross. The honest
infrastructure claim for this repository is therefore conditional: latency
spend would matter for the fast flow class *if* their turnover/cost problem
were solved (maker-style execution, slower scheduling), and demonstrably
does not matter for the slow equity class whose IC ignores five events of
lag.

## 4. Results: what infrastructure actually costs

### 4.1 The measured C++ hot path

From `benchmarks/results_cpp.md` (methodology in §2):

| stage | ns/event | events/sec |
|---|---:|---:|
| IAP1 binary decode (eq) | 3.5 | 282.01M |
| book update (eq MBO) | 17.4 | 57.42M |
| book update (FX QUOTE) | 29.6 | 33.82M |
| feature engine (48 features, every event) | 450.5 | 2.22M |
| alpha scoring (EQ01+EQ03+EQ06) | 32.3 | 30.96M |
| replay engine (eq) | 27.0 | 37.08M |
| execution sim replay | 41.7 | 23.97M |
| JSONL decode (for contrast) | 214.0 | 4.67M |

End-to-end decode → book → features → alpha is ≈ 504 ns/event, dominated
(~90%) by the feature engine computing all 48 native features at
cadence 0. An event-to-signal budget of ~0.5 µs against a median
inter-event gap of ~0.9 s means compute latency is ~6 orders of magnitude
away from being the binding constraint on this feed.

### 4.2 The implication, stated carefully

On this dataset, the "+1 event" stress is *not* a model of slow software —
no plausible software regression turns 0.5 µs into 0.9 s. It models
decision staleness from any source: queueing during bursts, batched
decision cadence, network hops, or venue round-trip on order placement. The
benchmarks bound the software term of that budget to noise; the stress
shows the total budget is worth ~20-37% of *IC* per event for the fast
flow class (and, on this cost-dominated dataset, ~0% of net P&L — §3.3).
The rational infrastructure conclusion for this platform is therefore:
(a) throughput headroom already vastly exceeds feed rates; (b) any latency
dollar belongs on *reacting on the triggering event* — event-driven
wake-up, no polling/batching, fast order path — rather than on shaving
nanoseconds off feature code; and (c) for the slow equity and FX regime
books, latency spend is close to worthless and cost/turnover work
dominates everywhere.

### 4.3 Cross-language throughput, with boundary caveats

Same container, measured from this repository (see paper 6 for full
methodology and architecture): C++ replay engine 37.1M events/s
(`benchmarks/results_cpp.md`, warmup + ≥ 0.5 s loops); Rust replay demo
6.89M (eq) / 6.07M (fx) events/s single-pass
(`rust/replay/src/bin/demo.rs` output); Java replay demo 3.48M events/s
over 25 timed full replays after 5 warmups (`java/.../replay/Demo.java`
output). These are *not* boundary-identical measurements (the demos include
engine construction and validation passes the C++ bench excludes), so we
claim only the order-of-magnitude ranking. Even the slowest port exceeds
the synthetic feed's ~155k events/day by ~9 orders of magnitude per day of
wall time; throughput differences monetize in research iteration speed
(replaying years of data) and burst recovery, not steady-state trading.

## 5. Limitations

1. **Synthetic data.** Event-arrival statistics (and hence the wall-clock
   meaning of "+1 event") are properties of the generator; real feeds are
   3-6 orders of magnitude faster in bursts, which shrinks the gap between
   compute latency and event spacing and can make software latency a real
   term. The *method* — price latency in events, then convert — transfers;
   the numbers do not.
2. **Coarse shift grid.** {0, 1, 5} events with the backtester's base
   latency of one row; no sub-event (intra-spacing) resolution, no queueing
   model of burst arrival.
3. **Mean-only benchmarks on shared hardware.** 2-CPU container, no
   pinning, no p99/p99.9 (spec §22 asks for tails; `results_cpp.md`
   discloses their absence). Tail latency is precisely what matters in
   bursts, and it is unmeasured here.
4. **Two-day sample.** IC deltas in §3.1 carry sampling error that the
   platform's own multiple-testing note (1,224 ledger experiments,
   REPORT.md) says to respect.
5. **No order-path measurement.** Spec §22's decision-to-wire benchmark is
   not yet in `benchmarks/`; the execution simulator models venue latency
   (50/50/100 µs legs + ~150 µs venue mean, `tests/golden/expected_replay_fills.json`
   description) but that is a simulation parameter, not a measurement.

## 6. Conclusions

Latency stress in event time cleanly separates this platform's alpha book
into fast flow/microstructure signals that shed a fifth to a third of
their IC per event and most of it by five (EQ01's flips sign), slow equity
signals that are statistically indifferent to five events of lag, and FX
regime signals in between (fine at one event, mostly gone at five). The
measured software stack, at ~0.5 µs event-to-signal against ~1 s event
spacing, contributes effectively none of that staleness — and because every
alpha on this dataset is cost-negative at 1x, the latency effect never
reaches the P&L line at all. The economically defensible infrastructure
program that follows is unglamorous: keep the hot path allocation-free and
event-driven (it already is — paper 6), spend on reacting to the *next
event* rather than on nanosecond shaving, and accept that until the cost
problem is solved, the latency budget is not where the P&L is for any alpha
in this book. Every number above is reproducible from the pinned seed and
committed artifacts.

## Artifact provenance

| claim | artifact |
|---|---|
| latency stress grid (all 24 alphas) | `research/alpha_reports/REPORT.md` (cost/latency stress table) |
| per-alpha stressed P&L | `research/alpha_reports/EQ06.json`, `EQ08.json`, `EQ09.json` (+ peers), `stress.latency` |
| stress semantics, shift grid | `python/src/iap/validation/stress.py`, `configs/execution/execution.json` |
| backtester base latency | `python/src/iap/backtest/engine.py` (`latency_rows`) |
| C++ stage benchmarks + methodology | `benchmarks/results_cpp.md`, `cpp/bench/bench_all.cpp` |
| Rust / Java replay throughput | `rust/replay/src/bin/demo.rs`, `java/src/main/java/com/iap/replay/Demo.java` (run in this container) |
| inter-emission gaps | computed from `data/features/features_1.parquet`, `features_101.parquet` |
| feed volumes | `data/normalized/qc_report.json` |

## Erratum / Update — 2026-09-06 (round-3 trading fixes)

- The §3 decay table (OOS IC by lag, FX rows included) is currency-free and
  unchanged. Any FX P&L figure cross-referenced from `REPORT.md` before
  2026-09-06 (papers 2 and 3) was summed in quote currency as if USD; the
  re-derived USD figures are in those papers' errata and in
  `research/alpha_reports/REPORT.md`.
- The execution-side latency controls this paper argued for are now
  enforced rather than merely declared: `configs/execution/execution.json`
  `latency_budget_ns`, `max_participation` and `min_slice_interval_ns` are
  read by the Java `BacktestEngine`/`PaperTrading` and violations are
  counted (`PLATFORM_CONVENTIONS.md` §11.4; test
  `ExecutionScenarioTest.backtestEnforcesParticipationSliceIntervalAndLatencyBudget`).


## Erratum / Update — 2026-09-06 (round-3 research fixes: latency in time)

1. **The latency axis was measured in ROWS, not time.** The "+0ev / +1ev /
   +5ev" grid shifts execution by emission events, and one emission row is
   ~3.3 s on the equity book and ~15-22 s on the FX book in this dataset — so
   the same column meant two very different latencies, and neither was a
   latency budget anyone could act on. The backtester now supports TIME
   latency (`latency_ns`: the decision at t executes at the first row with
   `exchange_ts >= t + latency_ns`) and every report carries a pinned time
   grid of **100 ms / 500 ms / 1 s / 5 s**. The event grid is retained only
   for continuity with this paper's tables.
2. **Decision age and session flattening.** A decision used to fill at
   whatever row came next, however old: a 16:05 decision "filled" at the
   20:00 close print and a 20:00 decision at the next day's open, which
   credited the overnight gap to a 1-second alpha. The research backtester
   now drops decisions older than `max_decision_age_ns` (60 s) and flattens
   before every session boundary. The decay-vs-latency curves above are
   therefore optimistic in the tail: the P&L at large lags included fills
   that could not have happened.
3. **Labels** no longer span halts, stale-venue gaps or a frozen mid, so the
   long-horizon points of the decay curves rest on fewer, better rows
   (15 m equity label validity is 47 %, not ~100 %).
4. **Crossed-book conditioning** applies to every FX curve in this paper: see
   paper 02's erratum table.

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

### Benchmark erratum — compute-cost figures superseded (2026-09-06)

**Every row of the §4.1 hot-path table is superseded, not just the codec
rows.** The per-event compute figures quoted in §1, in the §4.1 table and
in §4.3 (3.5 + 17.4 + 450.5 + 32.3 ns/event ≈ 0.5 µs; C++ 37.1M replay
events/s) predate the mandatory CRC-32 IAP1 trailer added in round 3 and
the round-3 feature-engine changes. Re-measured on the same methodology
(`benchmarks/results_cpp.md`; codec explanation in paper 06's benchmark
erratum):

| stage (§4.1 row) | as published | current |
|---|---:|---:|
| IAP1 binary decode (eq) | 3.5 ns | **174.4 ns** |
| book update (eq MBO) | 17.4 ns | **25.7 ns** |
| book update (FX QUOTE) | 29.6 ns | **36.2 ns** |
| feature engine (48 features, every event) | 450.5 ns | **530.4 ns** |
| alpha scoring (EQ01+EQ03+EQ06) | 32.3 ns | **38.5 ns** |
| replay engine (eq) | 27.0 ns / 37.1M ev/s | **35.5 ns / 28.1M ev/s** |
| execution sim replay | 41.7 ns | **66.2 ns** |
| JSONL decode (contrast) | 214.0 ns | **225.7 ns** |

The **feature engine row is the one an earlier version of this erratum left
unflagged**, and it matters: it is ~69 % of the end-to-end path, so a
correction that re-derives only the decode and book terms understates the
total. End to end, decode + book + features + alpha is now
174.4 + 25.7 + 530.4 + 38.5 = **769 ns ≈ 0.77 µs/event**, not the ≈ 0.68 µs
this erratum previously stated (that figure summed the two re-measured
terms with the two stale ones) and not §4.1's ≈ 504 ns.

**The paper's argument is unchanged**: 0.77 µs is still five to six orders
of magnitude below the ~0.9 s median inter-event gap on the synthetic
equity feed, so the measured decay penalty remains information staleness
rather than compute time. Only the absolute compute numbers move — and
they moved *against* the platform, which is the direction that could have
threatened the argument had the gap been narrower.

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

## Erratum / Update — 2026-09-20 (benchmark table regenerated)

`benchmarks/results_cpp.md` was regenerated on 2026-09-19 (the C++ trace
contract added two traced-replay rows and a trace-path table). The hot rows
moved within the stated few-percent cross-run variance: IAP1 decode 174.4 →
**184.1 ns/event**, book update 25.7 → **26.4 ns**, replay engine 28.1M →
**27.2M events/s**, feature engine 530.4 → **514.1 ns/event**, alpha scoring
38.5 → **33.6 ns/row**; the measured hot path is now 184.1 + 26.4 + 514.1 +
33.6 = **758 ns ≈ 0.76 µs/event** (was 0.77). Nothing in the argument moves
— the gap to the ~0.9 s median inter-event gap is unchanged at six orders of
magnitude — and the platform's documents now quote the table rather than
this paper (`tests/harness/check_headline_numbers.py`, `cpp_benchmark_numbers`).
The new rows: serialising one 5.6 KB `DecisionTrace` costs 31.7 µs (+ 30.3 µs
to hash), paid once per decision off the event loop; ≈ 37 ns/event amortised
on the 2,000-event golden replay.

## Erratum / Update — 2026-10-03 (v1.4.0: the equity flow now reaches the close)

**What changed in the data.** Up to v1.3.0 the generator stopped the equity
continuous flow about 40 % of the way through each 6.5 h session; v1.4.0
calibrates the flow rate to the session, so equities trade to the close
(paper 01's update of the same date has the detail). About the same number
of equity events now covers the whole session, so equity rows are further
apart: instrument 1 has 14,609 feature rows with a median inter-emission gap
of 2.1 s and a mean row gap of 3.2 s (3.1-3.3 s across the 11 instruments),
where the body measured ~0.9 s inside the compressed window. The FX files
are byte-identical to v1.3.0 (instrument 101: 6,573 rows, median gap
14.9 s). The benchmark figures do not depend on the dataset. This section
supersedes the body's equity rows and the "current" figures in the earlier
errata, which stay as dated records.

**The event-lag grid (§3.1), current** (`research/alpha_reports/REPORT.md`,
cost and latency stress table; retention shown where the +0 IC exceeds
0.01). The FX rows are the v1.3.0 values: every field of the FX JSONs that
existed then is unchanged.

| alpha | +0ev IC | +1ev IC | +5ev IC | retained at +1 | retained at +5 |
|---|---|---|---|---|---|
| EQ01 | +0.0062 | -0.0049 | +0.0023 | — | — |
| EQ02 | +0.0217 | +0.0092 | +0.0097 | 42% | 45% |
| EQ03 | +0.0148 | +0.0040 | +0.0099 | 27% | 67% |
| EQ04 | +0.0182 | +0.0147 | +0.0001 | 81% | 1% |
| EQ05 | +0.0075 | +0.0040 | -0.0079 | — | — |
| EQ06 | +0.0194 | +0.0141 | +0.0334 | 73% | 172% |
| EQ07 | -0.0013 | -0.0096 | -0.0064 | — | — |
| EQ08 | -0.0407 | -0.0451 | -0.0450 | — | — |
| EQ09 | -0.0212 | -0.0273 | -0.0280 | — | — |
| EQ10 | +0.0031 | +0.0020 | -0.0111 | — | — |
| EQ11 | +0.0124 | +0.0097 | +0.0120 | 78% | 97% |
| EQ12 | +0.0229 | +0.0092 | +0.0105 | 40% | 46% |
| FX01 | -0.0189 | -0.0076 | -0.0005 | — | — |
| FX02 | +0.0300 | +0.0252 | -0.0149 | 84% | — |
| FX03 | -0.0035 | +0.0019 | -0.0106 | — | — |
| FX04 | +0.0071 | -0.0102 | -0.0047 | — | — |
| FX05 | +0.0121 | +0.0311 | +0.0752 | 257% | 621% |
| FX06 | +0.0801 | +0.0617 | +0.0188 | 77% | 23% |
| FX07 | +0.0091 | +0.0369 | +0.0433 | — | — |
| FX08 | +0.1406 | +0.1169 | +0.0581 | 83% | 41% |
| FX09 | -0.1617 | -0.1342 | -0.0481 | — | — |
| FX10 | +0.1184 | +0.0919 | +0.0079 | 78% | 7% |
| FX11 | +0.1095 | +0.0888 | +0.0444 | 81% | 41% |
| FX12 | -0.0013 | -0.0060 | -0.0162 | — | — |

FX09's IC is negative (it is a REJECT, paper 2); its magnitude falls to 83 %
at one row and 30 % at five.

**The time grid** (net P&L at 1x costs and trade count, last fold,
`stress.latency_time` in the per-alpha JSONs):

| alpha | 100 ms | 500 ms | 1 s | 5 s | trades at 100 ms / 5 s |
|---|---|---|---|---|---|
| EQ01 | -41,725 | -39,814 | -38,230 | -31,560 | 1,705 / 1,364 |
| EQ02 | -149,377 | -144,931 | -139,468 | -92,498 | 6,892 / 4,361 |
| EQ03 | -161,581 | -155,533 | -148,562 | -98,064 | 7,446 / 4,624 |
| EQ05 | -329,553 | -315,478 | -300,873 | -201,940 | 15,067 / 9,302 |
| EQ06 | -83,859 | -83,010 | -81,593 | -75,110 | 4,266 / 3,827 |
| EQ08 | -7,812 | -7,628 | -7,362 | -6,563 | 405 / 348 |
| EQ09 | -13,799 | -13,715 | -13,335 | -11,092 | 659 / 549 |
| EQ11 | -29,332 | -29,342 | -29,015 | -27,730 | 1,432 / 1,349 |
| EQ12 | -147,889 | -143,078 | -137,421 | -90,552 | 6,794 / 4,235 |

The §3.3 table on the event grid (+0 / +1 / +5 rows): EQ06 -83,833 /
-84,182 / -84,026; EQ08 -7,799 / -8,099 / -8,393; EQ09 -13,799 / -14,023 /
-13,704.

**Decay by horizon, equity alphas** (last-fold OOS IC,
`decay_ic_by_horizon`):

| alpha | 100ms | 500ms | 1s | 5s | 10s | 30s | 1m | 5m | 15m |
|---|---|---|---|---|---|---|---|---|---|
| EQ01 | +0.012 | +0.021 | +0.023 | +0.038 | +0.044 | +0.044 | +0.050 | +0.053 | +0.033 |
| EQ02 | +0.006 | +0.020 | +0.021 | +0.037 | +0.044 | +0.032 | +0.027 | +0.012 | +0.008 |
| EQ03 | +0.004 | +0.015 | +0.017 | +0.033 | +0.039 | +0.027 | +0.022 | +0.012 | +0.008 |
| EQ05 | -0.000 | +0.007 | +0.015 | +0.034 | +0.054 | +0.048 | +0.047 | +0.021 | -0.000 |
| EQ06 | +0.013 | +0.016 | +0.021 | +0.042 | +0.046 | +0.073 | +0.074 | +0.015 | +0.007 |
| EQ08 | -0.001 | -0.012 | -0.013 | -0.035 | -0.060 | -0.046 | -0.032 | +0.019 | +0.130 |
| EQ11 | +0.001 | -0.003 | -0.012 | -0.029 | -0.046 | -0.058 | -0.053 | -0.020 | +0.012 |
| EQ12 | +0.005 | +0.022 | +0.023 | +0.040 | +0.046 | +0.033 | +0.027 | +0.012 | +0.009 |

The OFI hump (EQ02, EQ03, EQ12: near zero at 100 ms, peak at 10 s, slow
decline) is still there. Three shapes changed. EQ01's curve peaked at 1-5 s
and fell to +0.006 at 1 m on v1.3.0; it now rises to +0.053 at 5 m. EQ06's
peak moved from 10-30 s to 30 s-1 m. EQ11's 15 m point fell from +0.179 to
+0.012, and EQ08's from +0.277 to +0.130. These curves are also less
comparable across horizons than before. The label freshness rule is
`max(5 s, 2 x median quote gap)`; the equity median gap is about 2 s, so the
5 s floor binds and a label whose forward mid is older than 5 s is invalid.
The valid fraction per equity instrument is 99.4-99.7 % at 5 s, 78.7-80.5 %
at 10 s, 75.3-77.2 % at 1 m and 44.7-63.4 % at 15 m. Every point at 10 s and
beyond is measured on the rows that were followed by a fresh quote at the
horizon, which is a different and more active subset than the 5 s point
uses.

**What the 2026-09-06 erratum says about the session.** Item 1's "one
emission row is ~3.3 s on the equity book" was an average that included the
dead zone; 3.1-3.3 s is now the real spacing, and FX rows are 21.7-22.1 s
apart on average. Item 2's example of a 16:05 decision filling at the 20:00
close print describes the v1.3.0 data; there is no such gap now, and the
60 s decision-age bound and session flattening remain in force. Item 3's
15 m equity label validity of 47 % is now 44.7-63.4 %.

**Ledger.** 1920 looks over 139 entries (the 1068 looks of the v1.3.0
dataset are kept); expected max |t| under the global null 3.888, Bonferroni
per-test threshold 4.206. Verdicts: 0 PROMOTE / 10 ITERATE / 14 REJECT
(EQ11 moved from ITERATE to REJECT); all 24 alphas are net-negative at 1x.

**Conclusions, re-checked.**

1. *Fast flow alphas (EQ01, EQ02, EQ03, EQ12) shed a fifth to a third of
   their IC per event and most of it by five.* **No longer holds as
   stated.** The OFI alphas now lose 58-73 % at the first row, and the +5
   value is not below the +1 value (EQ03: 27 % retained at +1, 67 % at +5).
   EQ01's IC is under 0.01 at every lag and changes sign twice. That the
   OFI family is the latency-sensitive part of the equity book still holds;
   the per-event rate does not, because one row is now about 3 s and most of
   the loss falls inside it. The event grid no longer resolves the decline;
   it would take a grid finer than one row.
2. *Slow equity alphas (EQ06-EQ09, EQ11) are indifferent to five events of
   lag.* **Holds, weaker.** EQ08, EQ09 and EQ11 are flat across the grid,
   but EQ08 and EQ09 are flat at a negative IC, EQ07 is noise, and EQ11's
   +0 IC is 0.012 (0.151 on v1.3.0). EQ06 moves from 0.019 to 0.014 to
   0.033, which is noise of the same size as its level. There is little
   positive IC left in this class for lag to remove.
3. *FX regime alphas keep about 80 % at one event and lose most by five.*
   **Holds**, unchanged (77-83 % at +1, 7-41 % at +5), with the crossed-book
   caveat of the round-3 erratum.
4. *Stressed net P&L moves by well under 1 %; latency has no money value
   when costs dominate gross.* The number **no longer holds**; the
   conclusion **holds**. On the event grid EQ06 moves 0.4 % and EQ09 1.6 %,
   but EQ08 moves 7.6 % by five rows. On the time grid the loss falls by
   10-39 % between 100 ms and 5 s for the alphas tabulated above except
   EQ11 (5 %), and it falls because fewer trades are made, not because any
   edge is recovered: EQ02 loses 21.7 per trade at 100 ms and 21.2 at 5 s.
   No alpha is positive at any latency, so latency still never reaches the
   P&L line as alpha. The time-grid pattern was already present on v1.3.0
   (EQ02: -73,990 at 100 ms, -53,411 at 5 s); the body predates that grid.
5. *The software stack contributes none of the staleness: about 0.76 µs
   per event against the inter-event gap.* **Holds.** The gap is now 2.1 s
   (median, instrument 1), still six orders of magnitude above the hot
   path. The feed is still about 155k events a day (308,975 over two days).
6. *Every alpha is cost-negative at 1x, so until the cost problem is
   solved the latency budget is not where the P&L is.* **Holds.**
7. *Latency spend belongs on reacting to the next event, not on shaving
   nanoseconds.* **Holds**, and the equity figures now make it sharper:
   being one row late costs the OFI alphas more than half their IC.
