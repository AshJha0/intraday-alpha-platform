# Alpha Decay versus Latency and Infrastructure Investment: What One Event of Lag Costs

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
(`LATENCY_SHIFTS = (0, 1, 5)`; grid pinned in `configs/execution.json` as
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
| stress semantics, shift grid | `python/src/iap/validation/stress.py`, `configs/execution.json` |
| backtester base latency | `python/src/iap/backtest/engine.py` (`latency_rows`) |
| C++ stage benchmarks + methodology | `benchmarks/results_cpp.md`, `cpp/bench/bench_all.cpp` |
| Rust / Java replay throughput | `rust/replay/src/bin/demo.rs`, `java/src/main/java/com/iap/replay/Demo.java` (run in this container) |
| inter-emission gaps | computed from `data/features/features_1.parquet`, `features_101.parquet` |
| feed volumes | `data/normalized/qc_report.json` |
