# C++ benchmark results (bench_all)

- CPU: Intel(R) Xeon(R) Processor @ 2.10GHz (2-CPU container, no pinning)
- Compiler: g++ 13.3.0, flags: -O3 -DNDEBUG -Wall -Wextra -Werror (CMake Release), C++17
- Method: steady_clock wall loop, 3 warmup iterations, then >= 0.5 s and >= 10 iterations per benchmark; workload = pinned golden vectors (tests/golden/events_eq_mbo.jsonl 2000 events, events_fx_quote.jsonl 800 events); single-threaded
- Caveat: mean-only figures (no tail latency; container timers), cross-run variance a few percent — methodology per benchmarks/RESULTS.md

| benchmark | ns/event | events/sec | events |
|---|---:|---:|---:|
| IAP1 decode (eq, 2000 ev) | 3.5 | 282006214 | 141004000 |
| IAP1 encode (eq, 2000 ev) | 3.3 | 307002149 | 153502000 |
| JSONL decode (eq, 2000 ev) | 214.0 | 4673300 | 2338000 |
| book update (eq MBO, 2000 ev) | 17.4 | 57416259 | 28710000 |
| book update (fx QUOTE, 800 ev) | 29.6 | 33818930 | 16909600 |
| replay engine (eq, 2000 ev) | 27.0 | 37079293 | 18540000 |
| feature engine (eq, 48 feats, cadence 0) | 450.5 | 2219575 | 1110000 |
| feature engine (fx, 48 feats, cadence 0) | 336.8 | 2968913 | 1484800 |
| alpha scoring (EQ01+EQ03+EQ06, 2000 rows x 3) | 32.3 | 30962520 | 15486000 |
| execution sim replay (eq, 2 parents) | 41.7 | 23969113 | 11986000 |
