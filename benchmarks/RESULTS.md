# Benchmark results — index

Methodology first (spec §22): every number in this directory states its
hardware (2-CPU Intel Xeon @ 2.10 GHz container, no pinning), toolchain,
workload and boundary caveats; all figures are mean-only (no tail latency —
container timers and shared CPUs make honest p99s impossible here) with
cross-run variance of a few percent.

| artifact | what it holds |
|---|---|
| [results_cpp.md](results_cpp.md) | the full C++ `bench_all` stage table (codec, book, replay, features, alpha, execution sim) + methodology header. Regenerate: `cpp/build/bench_all benchmarks/results_cpp.md` |
| Rust replay demo | `cd rust && cargo run --release --bin demo` — demo-scale replay throughput over the golden vectors, streamed from a decoder thread through the bounded SPSC event bus into the engine (round-3 slab/intrusive-list book, O(1) cancels; a recent container run: ≈ 6.5M events/s equity, ≈ 5.1M events/s FX including the cross-thread hand-off; single timed pass, high variance) |
| Java replay demo | `java/demo.sh` — 25 timed full replays after 5 warmups (a recent container run: ≈ 3.5M events/s), plus the golden IAP1 SHA-256 digests |

Reference points from the committed C++ run (see `results_cpp.md` for the
exact table and caveats): IAP1 decode 174.4 ns/event, book update 25.7 ns,
replay engine 28.1M events/s, feature engine ~530 ns/event (48 features,
cadence 0), alpha scoring 38.5 ns/row.

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
not comparable to the C++ table's isolated 25.7 ns book update; it is the
end-to-end per-event cost an operator actually sees.

`bench_all` now measures the C++ cold case directly rather than leaving it
to prose: it appends a **cold reference table** — one single pass, no warmup
and no repeat, over `data/normalized/eq_20260824.normalized.jsonl` (override
with `bench_all <out.md> <events.jsonl>`; the rows are omitted with a printed
note when `data/` has not been generated). From the committed run, cold
against hot **within the same process**:

| stage | hot (cache-resident) | cold (single pass, full day) | ratio |
|---|---:|---:|---:|
| IAP1 decode | 174.4 ns/ev | 195.8 ns/ev | 1.12× |
| book update | 25.7 ns/ev | 42.1 ns/ev | 1.64× |

Decode barely moves because it streams linearly and the prefetcher hides the
misses (and because the CRC, not memory, is its bottleneck). The book update
is 1.6× slower cold: a real day's depth pushes the level map and its free
lists out of L2 and costs TLB misses that the 2,000-event loop never pays.
The **ratio** is the transferable number; the absolutes carry single-pass
variance and should not be quoted on their own.

Boundary caveat: the three languages' replay figures are NOT
boundary-identical — the C++ bench times the replay loop over pre-decoded
events with warmup and ≥0.5 s loops, while the Rust and Java demos include
engine construction and validation passes (see
`docs/papers/06_cpp_vs_rust_vs_java_event_driven.md` §4.2). Only
order-of-magnitude comparisons are supported.
