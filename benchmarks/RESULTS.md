# Benchmark results — index

Methodology first (spec §22): every number in this directory states its
hardware (2-CPU Intel Xeon @ 2.10 GHz container, no pinning), toolchain,
workload and boundary caveats; all figures are mean-only (no tail latency —
container timers and shared CPUs make honest p99s impossible here) with
cross-run variance of a few percent.

| artifact | what it holds |
|---|---|
| [results_cpp.md](results_cpp.md) | the full C++ `bench_all` stage table (codec, book, replay, features, alpha, execution sim) + methodology header. Regenerate: `cpp/build/bench_all benchmarks/results_cpp.md` |
| Rust replay demo | `cd rust && cargo run --release --bin demo` — demo-scale replay throughput over the golden vectors (a recent container run: ≈ 6.9M events/s equity, ≈ 6.1M events/s FX; single timed pass, high variance) |
| Java replay demo | `java/demo.sh` — 25 timed full replays after 5 warmups (a recent container run: ≈ 3.5M events/s), plus the golden IAP1 SHA-256 digests |

Reference points from the committed C++ run (see `results_cpp.md` for the
exact table and caveats): IAP1 decode 3.5 ns/event, book update 17.4 ns,
replay engine 37.1M events/s, feature engine ~450 ns/event (48 features,
cadence 0), alpha scoring 32.3 ns/row.

Boundary caveat: the three languages' replay figures are NOT
boundary-identical — the C++ bench times the replay loop over pre-decoded
events with warmup and ≥0.5 s loops, while the Rust and Java demos include
engine construction and validation passes (see
`docs/papers/06_cpp_vs_rust_vs_java_event_driven.md` §4.2). Only
order-of-magnitude comparisons are supported.
