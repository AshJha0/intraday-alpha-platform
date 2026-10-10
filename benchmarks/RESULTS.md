# Benchmark results — index

Methodology first (spec §22): every number in this directory states its
hardware (2-CPU Intel Xeon @ 2.10 GHz container, no pinning), toolchain,
workload and boundary caveats; all figures are mean-only (no tail latency —
container timers and shared CPUs make honest p99s impossible here) with
cross-run variance of a few percent. The one exception is the v1.12
[tick-to-trade table](#tick-to-trade-latency-percentiles-v112) below, which
reports per-event percentiles measured on a GitHub-hosted CI runner, with its
own caveats.

| artifact | what it holds |
|---|---|
| [results_cpp.md](results_cpp.md) | the full C++ `bench_all` stage table (codec, book, replay, features, alpha, execution sim, traced execution sim) + the decision-trace path table + methodology header. Regenerate: `cpp/build/bench_all benchmarks/results_cpp.md` |
| Rust replay demo | `cd rust && cargo run --release --bin demo` — demo-scale replay throughput over the golden vectors, streamed from a decoder thread through the bounded SPSC event bus into the engine (round-3 slab/intrusive-list book, O(1) cancels; a recent container run: ≈ 6.5M events/s equity, ≈ 5.1M events/s FX including the cross-thread hand-off; single timed pass, high variance) |
| [results_tick_to_trade.md](results_tick_to_trade.md) | v1.12: per-event tick-to-trade percentiles (p50/p90/p99/p99.9/max) end to end and per stage, from a CI runner; also the baseline of the p50/p99 CI guard. Regenerate: `cpp/build/bench_tick_to_trade benchmarks/results_tick_to_trade.md` (on a CI runner, or download the `bench-tick-to-trade` artifact of the cpp job) |
| Python tick-to-trade | `python3 tools/tick_to_trade_py.py` — the same path through the Python reference implementation (events/s and percentiles in µs; laptop-scale, not committed) |
| Java replay demo | `java/demo.sh` — 25 timed full replays after 5 warmups (a recent container run: ≈ 3.5M events/s), plus the golden IAP1 SHA-256 digests |

Reference points from the committed C++ run (regenerated 2026-09-19; see
`results_cpp.md` for the exact table and caveats): IAP1 decode
184.1 ns/event, book update 26.4 ns, replay engine 27.2M events/s, feature
engine 514.1 ns/event (48 features, cadence 0), alpha scoring 33.6 ns/row;
serialising one 5.6 KB `DecisionTrace` 31.7 µs/trace (+ 30.3 µs to hash),
paid once per decision off the event loop — ≈ 37 ns/event amortised on the
2,000-event replay. The previous table (2026-09-06) read 174.4 / 25.7 /
28.1M / 530.4 / 38.5: within the stated few-percent cross-run variance.
Every document that quotes these figures is checked against the table by
`tests/harness/check_headline_numbers.py`.

**Why the codec numbers moved 50× in round 3.** The pre-round-3 table read
3.5 ns/event decode and 3.3 ns encode. IAP1 v2 then added a mandatory
CRC-32 integrity trailer over the record body, and `decode_iap1` verifies
it on every call. The table-driven, byte-at-a-time CRC has a loop-carried
dependency of roughly 5 cycles/byte, so 2,000 records × 72 bytes ≈ 144 KB
costs ≈ 343 µs per pass at 2.1 GHz — which is essentially the whole delta.
The control is in the same table: JSONL decode, which computes no CRC,
moved only 214 → 225.7 ns. The check is worth its cost (a corrupted
capture is otherwise decoded into plausible-looking events); quoting the
pre-CRC figure after adding it was the defect. If the CRC ever needs to be
cheap, the fix is a slice-by-8 or hardware-CRC implementation, not removing
the check.

## CI regression guard

The `cpp` job of `.github/workflows/ci.yml` runs `bench_all` in Release and
`tests/harness/check_bench_regression.py` compares each hot row with
`results_cpp.md`: a row more than 5x slower than the committed figure fails
the job, and so does a run that matches fewer than 80% of the committed rows
(a renamed table must not become a silent pass). The factor is large because
the committed table is one 2-CPU Xeon container and CI runs on shared
hardware: the guard is there for algorithmic regressions (a lost heap, an
O(n^2), an allocation per event), not for drift of a few percent and not for
tail latency. The cold single-pass rows are excluded (one unwarmed run each).
To regenerate the baseline, run `bench_all results_cpp.md` on the baseline
machine and update the documents that quote it
(`tests/harness/check_headline_numbers.py` lists them).

## Cold-workload reference (hot vs full-day)

Every figure in `results_cpp.md` is **cache-resident**: `bench_all` loops one
2,000-event (144 KB) vector after warmup, so the working set never leaves L2
and the book stays a handful of levels deep. They measure the code, not a
trading day. The reference point below is the opposite end — a single pass
over a full generated session, cold, including JIT warm-up and a book that
grows to its real depth:

| workload | events | measurement | p50 | p99 | p999 |
|---|---:|---|---:|---:|---:|
| Java JSONL decode, one full day (`data/normalized/eq_20260824.normalized.jsonl`) | 105,640 | `decode_latency_ns` from a paper session | ≤ 1,023 ns | ≤ 8,191 ns | ≤ 32,767 ns |
| Java book+features+alpha+risk per event, instrument 11 of that day | 9,736 | `book_update_latency_ns` (the whole `onEvent`, not a bare book apply) | ≤ 65,535 ns | ≤ 524,287 ns | ≤ 8,388,607 ns |

**Dataset note (v1.4.0, 2026-10-03).** These cold rows, and the cold reference
table of `results_cpp.md`, were measured over the `eq_20260824.normalized.jsonl`
of the v1.3.0 dataset (105,640 events, flow packed into the first 40% of the
session). The v1.4.0 generator writes a different file under the same name
(105,282 events, flow to the close). The timings were **not re-measured**: they
need the baseline machine, and a run on other hardware would not be comparable
with the hot table. To reproduce the measured input, generate the dataset with
`equities.flow.calibration` set to `"legacy_budget"`; the commands below run on
either file, with event counts that differ accordingly.

Reproduce (from the repo root, after `python3 -m iap.marketdata`):

```bash
cd java && bash build.sh
java -cp out/main com.iap.platform.PaperTrading   --configs ../configs   --events ../data/normalized/eq_20260824.normalized.jsonl   --instrument 11 --state-dir /tmp/iap-cold --report /tmp/iap-cold/report.json
python3 -m json.tool /tmp/iap-cold/report.json | sed -n '/latency_ns/,/}/p'
```

Read those numbers with two caveats. (1) The platform's histograms are the
pinned **log2** buckets, so each figure is the bucket's inclusive upper bound —
within one power of two ABOVE the true order statistic (conservative). (2) The
`book_update` row times the whole `onEvent` — book apply *plus* the feature
engine, the alpha, the portfolio solve and the pre-trade risk check — so it is
not comparable to the C++ table's isolated 26.4 ns book update; it is the
end-to-end per-event cost an operator actually sees.

`bench_all` now measures the C++ cold case directly rather than leaving it
to prose: it appends a **cold reference table** — one single pass, no warmup
and no repeat, over `data/normalized/eq_20260824.normalized.jsonl` (override
with `bench_all <out.md> <events.jsonl>`; the rows are omitted with a printed
note when `data/` has not been generated). From the committed run, cold
against hot **within the same process**:

| stage | hot (cache-resident) | cold (single pass, full day) | ratio |
|---|---:|---:|---:|
| IAP1 decode | 184.1 ns/ev | 212.3 ns/ev | 1.15× |
| book update | 26.4 ns/ev | 45.3 ns/ev | 1.72× |

Decode barely moves because it streams linearly and the prefetcher hides the
misses (and because the CRC, not memory, is its bottleneck). The book update
is 1.7× slower cold: a real day's depth pushes the level map and its free
lists out of L2 and costs TLB misses that the 2,000-event loop never pays.
The **ratio** is the transferable number; the absolutes carry single-pass
variance and should not be quoted on their own.

Boundary caveat: the three languages' replay figures are NOT
boundary-identical — the C++ bench times the replay loop over pre-decoded
events with warmup and ≥0.5 s loops, while the Rust and Java demos include
engine construction and validation passes (see
`docs/papers/06_cpp_vs_rust_vs_java_event_driven.md` §4.2). Only
order-of-magnitude comparisons are supported.

## Tick-to-trade latency percentiles (v1.12)

`cpp/bench/bench_tick_to_trade.cpp` timestamps **every event individually**
through the in-process decision path and records the latencies in an
HDR-style log-linear histogram (16 sub-buckets per power of two, so each
percentile is a bucket upper bound at most 6.25% above the true order
statistic; max is exact). Per event:

1. **decode** — one 104-byte IAP1 v2 frame (header + one 72-byte record +
   CRC trailer) through `decode_iap1`, CRC verified, as a feed handler would
   per message;
2. **book** — `OrderBook::apply` (the book the decision reads its top of
   book from);
3. **features** — `FeatureEngine::apply`, 48 native features at cadence 0
   (the engine keeps its own merged book view, so book maintenance is paid a
   second time inside this stage);
4. **alpha** — `score_row` for EQ01, EQ03 and EQ06 (linear_z_v1, the fitted
   `configs/strategies/alpha_params.json`);
5. **decision** — a bench-local stand-in: confidence-weighted direction vote,
   threshold, side, limit price from the opposite best, and a minimal
   pre-trade check (position cap, order-notional cap, price band, kill
   switch).

**What is not included.** There is no canonical C++ risk engine (the
pre-trade risk engines are Rust, Java and Python; `docs/POLYGLOT.md`), so the
decision stage is a representative few compares, not the platform risk
check. Also excluded: network, NIC and kernel (no sockets, no kernel bypass),
order encoding and wire send, the venue round trip, logging and the decision
trace. "Tick-to-trade" here therefore means **decoded-frame-in to
order-intent-out inside one process** — the software part of a real
tick-to-trade budget, not a wire-to-wire figure.

**Method.** `std::chrono::steady_clock` (portable; no rdtsc), six reads per
event; their own cost (≈ 30 ns back to back on the runner) is *included* in
every figure. The first full pass over each workload is warm-up and is
discarded; each timed pass builds fresh book and feature state outside the
timed region. Workloads, both pinned: the golden
`tests/golden/events_eq_mbo.jsonl` (2,000 events × 50 timed passes) and a
generated equity day (100,000 MBO events from SplitMix64 seed 20261012, one
instrument, adds / cancels / executes / trades around a random-walk mid,
built in-process so CI needs no `data/` files; one timed pass, so it is the
less cache-resident of the two).

**Hardware: a GitHub-hosted `ubuntu-24.04` runner, not the 2-CPU Xeon
container of the tables above and nowhere near a colocated, isolated, pinned
box.** The runner is a shared VM (4 vCPUs visible, no pinning, noisy
neighbours) and its CPU model varies between runs: the two runs of the PR
that introduced the table landed on an AMD EPYC 9V74 and an AMD EPYC 7763,
and the end-to-end p50 / p99 moved from 927 / 1,215 ns to 1,279 / 2,559 ns
between them. Do not compare these numbers with the hot table's means (other
hardware, and per-event timing adds the clock reads); compare stages with
each other and runs of this table with each other.

Committed run (`results_tick_to_trade.md`; AMD EPYC 7763, CI, 2026-10-10),
nanoseconds:

| path, workload | p50 | p90 | p99 | p99.9 | max | mean |
|---|---:|---:|---:|---:|---:|---:|
| **tick-to-trade, golden eq_mbo** | 1,279 | 2,047 | 2,559 | 11,263 | 33,112 | 1,344.6 |
| decode (frame + CRC) | 271 | 303 | 383 | 511 | 29,626 | 273.9 |
| book update | 83 | 135 | 223 | 287 | 27,201 | 86.8 |
| native features (48) | 735 | 1,343 | 1,791 | 3,455 | 32,090 | 807.4 |
| alpha score (3 alphas) | 135 | 215 | 271 | 303 | 29,265 | 142.4 |
| decision + pre-trade check | 30 | 51 | 71 | 91 | 16,100 | 34.2 |
| **tick-to-trade, generated day (100,000 ev)** | 1,407 | 1,471 | 1,727 | 12,287 | 298,570 | 1,368.0 |
| decode (frame + CRC) | 271 | 271 | 303 | 383 | 24,396 | 263.2 |
| book update | 103 | 143 | 223 | 799 | 156,444 | 106.8 |
| native features (48) | 895 | 959 | 1,087 | 11,263 | 201,218 | 865.4 |
| alpha score (3 alphas) | 103 | 111 | 123 | 215 | 20,468 | 102.8 |
| decision + pre-trade check | 30 | 31 | 41 | 61 | 10,510 | 29.8 |

How to read it. The feature engine is about 60% of the median and most of
the tail: it is the only stage that does real work per event (48 features
over rolling windows plus its own merged book). Decode is flat (the per-frame
CRC is a fixed cost). The p99.9 and max columns are dominated by the shared
runner — a preempted time slice shows up as a 10-300 µs outlier in whichever
stage was running — and are reported for completeness, not as properties of
the code; that is why the guard below never looks at them.

**CI guard.** The `cpp` job runs `bench_tick_to_trade` after ctest (about
5 s), appends the table to the job summary, uploads it as the
`bench-tick-to-trade` artifact, and runs
`check_bench_regression.py --rows '^t2t .* p(50|99)$' --factor 8` against
`results_tick_to_trade.md`: only the end-to-end p50 and p99 of each workload
are compared, and a run fails only when one is more than 8× the committed
figure. The factor is large because the runner CPU and its load change from
run to run (the 2× p99 swing above was two consecutive runs of the same
commit); like the `bench_all` guard it exists for algorithmic regressions (a
per-event allocation storm, an O(n) scan in the book), not for drift.

**Python reference path.** `tools/tick_to_trade_py.py` runs the same five
stages through the Python reference implementation (the generated day uses
the same SplitMix64 draws and seed; alpha scoring is the pinned linear_z_v1
formula per row, since `iap.alpha` scores DataFrames in batch). On a Windows
laptop it measured ≈ 4,600 events/s on the golden vector, p50 176 µs / p99
556 µs end to end, about 90% of it in the feature engine — two orders of
magnitude above C++, as expected of the readable reference; it is not run in
CI and its numbers are not committed as a baseline.
