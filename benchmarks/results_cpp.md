# C++ benchmark results (bench_all)

- CPU: Intel(R) Xeon(R) Processor @ 2.10GHz (2-CPU container, no pinning)
- Compiler: g++ 13.3.0, flags: -O3 -DNDEBUG -Wall -Wextra -Werror (CMake Release), C++17
- Method: steady_clock wall loop, 3 warmup iterations, then >= 0.5 s and >= 10 iterations per benchmark; workload = pinned golden vectors (tests/golden/events_eq_mbo.jsonl 2000 events, events_fx_quote.jsonl 800 events); single-threaded
- Caveat: mean-only figures (no tail latency; container timers), cross-run variance a few percent — methodology per benchmarks/RESULTS.md
- **Cache-residency caveat (read before comparing).** Every row in the first table is a fully **cache-resident, hot** figure. `bench_all` loops the SAME 2,000-event vector (144 KB of IAP1 records — comfortably inside L2) at least 10 times after 3 warmups, and the order book never exceeds a handful of price levels, so branch predictors, the level map and the free lists are all warm and the working set never leaves cache. Those are honest hot-L1/L2 numbers and are the right measure of the CODE's cost, but a reader replaying a 300k-event trading day from disk — cold page cache, a deeper book, TLB and LLC misses on the level map — will NOT reproduce them. Treat them as a lower bound on per-event cost, and read them next to the cold table below.

| benchmark (hot, cache-resident) | ns/event | events/sec | events |
|---|---:|---:|---:|
| IAP1 decode (eq, 2000 ev) | 174.4 | 5734892 | 2868000 |
| IAP1 encode (eq, 2000 ev) | 170.3 | 5871054 | 2936000 |
| JSONL decode (eq, 2000 ev) | 225.7 | 4429873 | 2216000 |
| book update (eq MBO, 2000 ev) | 25.7 | 38887761 | 19444000 |
| book update (fx QUOTE, 800 ev) | 36.2 | 27598521 | 13800000 |
| replay engine (eq, 2000 ev) | 35.5 | 28139908 | 14072000 |
| feature engine (eq, 48 feats, cadence 0) | 530.4 | 1885422 | 944000 |
| feature engine (fx, 48 feats, cadence 0) | 404.7 | 2470907 | 1236000 |
| alpha scoring (EQ01+EQ03+EQ06, 2000 rows x 3) | 38.5 | 25950590 | 12978000 |
| execution sim replay (eq, 2 parents) | 66.2 | 15109408 | 7556000 |

Cold reference — ONE single pass over a full generated session (`data/normalized/eq_20260824.normalized.jsonl`), no warmup, no repeat, working set far larger than L2. Single-pass means higher run-to-run variance than the hot table; this is a reference point, not a target.

| benchmark (cold, single pass) | ns/event | events/sec | events |
|---|---:|---:|---:|
| IAP1 decode (cold, 105640 ev, single pass) | 195.8 | 5106233 | 105640 |
| book update (cold, 4822 ev, single pass) | 42.1 | 23780755 | 4822 |
