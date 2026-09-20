# C++ benchmark results (bench_all)

- CPU: Intel(R) Xeon(R) Processor @ 2.10GHz (2-CPU container, no pinning)
- Compiler: g++ 13.3.0, flags: -O3 -DNDEBUG -Wall -Wextra -Werror (CMake Release), C++17
- Method: steady_clock wall loop, 3 warmup iterations, then >= 0.5 s and >= 10 iterations per benchmark; workload = pinned golden vectors (tests/golden/events_eq_mbo.jsonl 2000 events, events_fx_quote.jsonl 800 events); single-threaded
- Caveat: mean-only figures (no tail latency; container timers), cross-run variance a few percent — methodology per benchmarks/RESULTS.md
- **Cache-residency caveat (read before comparing).** Every row in the first table is a fully **cache-resident, hot** figure. `bench_all` loops the SAME 2,000-event vector (144 KB of IAP1 records — comfortably inside L2) at least 10 times after 3 warmups, and the order book never exceeds a handful of price levels, so branch predictors, the level map and the free lists are all warm and the working set never leaves cache. Those are honest hot-L1/L2 numbers and are the right measure of the CODE's cost, but a reader replaying a 300k-event trading day from disk — cold page cache, a deeper book, TLB and LLC misses on the level map — will NOT reproduce them. Treat them as a lower bound on per-event cost, and read them next to the cold table below.

| benchmark (hot, cache-resident) | ns/event | events/sec | events |
|---|---:|---:|---:|
| IAP1 decode (eq, 2000 ev) | 184.1 | 5430856 | 2716000 |
| IAP1 encode (eq, 2000 ev) | 183.1 | 5461135 | 2732000 |
| JSONL decode (eq, 2000 ev) | 183.2 | 5457336 | 2730000 |
| book update (eq MBO, 2000 ev) | 26.4 | 37868665 | 18936000 |
| book update (fx QUOTE, 800 ev) | 35.8 | 27950555 | 13976000 |
| replay engine (eq, 2000 ev) | 36.8 | 27207999 | 13604000 |
| feature engine (eq, 48 feats, cadence 0) | 514.1 | 1945142 | 974000 |
| feature engine (fx, 48 feats, cadence 0) | 404.4 | 2472534 | 1236800 |
| alpha scoring (EQ01+EQ03+EQ06, 2000 rows x 3) | 33.6 | 29802581 | 14904000 |
| execution sim replay (eq, 2 parents) | 63.7 | 15687932 | 7844000 |
| execution sim replay, traced (eq, 2 parents, 2 traces, data_version hashed per run) | 579.9 | 1724410 | 864000 |
| execution sim replay, traced (eq, 2 parents, 2 traces, data_version supplied) | 100.3 | 9968653 | 4986000 |

Trace path — cost per DecisionTrace of the canonical-JSON contract (`iap::contracts`), single-threaded, in memory (no file I/O), same 2-CPU container. A trace is serialised once per decision, after it and outside the book / feature / execution event loop, so this is NOT a per-event figure; the two traced replay rows above show the same cost amortised per event (2 traces per 2,000-event replay) — the `data_version hashed per run` row includes the one-off sha256 of the IAP1 encoding of the whole stream (a per-session cost that dominates a 2,000-event workload), the `data_version supplied` row isolates the tracing itself. The tree build (to_value) is included in the per-trace rows, the JSONL write is not.

| benchmark (trace path, in-memory) | ns/trace | traces/sec | traces |
|---|---:|---:|---:|
| canonical serialisation, golden DecisionTrace (all stages, 5627-byte line) | 31708.1 | 31538 | 15769 |
| serialise + sha256 digest update, golden DecisionTrace | 61966.1 | 16138 | 8069 |

Cold reference — ONE single pass over a full generated session (`data/normalized/eq_20260824.normalized.jsonl`), no warmup, no repeat, working set far larger than L2. Single-pass means higher run-to-run variance than the hot table; this is a reference point, not a target.

| benchmark (cold, single pass) | ns/event | events/sec | events |
|---|---:|---:|---:|
| IAP1 decode (cold, 105640 ev, single pass) | 212.3 | 4709491 | 105640 |
| book update (cold, 4822 ev, single pass) | 45.3 | 22076733 | 4822 |
