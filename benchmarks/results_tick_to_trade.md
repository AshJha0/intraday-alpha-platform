# C++ tick-to-trade latency (bench_tick_to_trade)

- CPU: AMD EPYC 7763 64-Core Processor (4 logical CPUs visible, no pinning, shared host)
- Path per event: one 104-byte IAP1 v2 frame -> decode_iap1 (CRC verified) -> OrderBook::apply -> FeatureEngine::apply (48 features, cadence 0) -> score_row EQ01+EQ03+EQ06 -> bench-local order decision + minimal pre-trade check. NOT included: network, NIC, kernel (no kernel bypass), order encoding / wire send, venue round trip, the platform risk engine (Rust/Java/Python), logging, decision trace.
- Timing: steady_clock, 6 reads per event, warm-up pass discarded, fresh state per pass built outside the timed region; HDR-style log-linear histogram (<= 6.25% bucket width), percentile = bucket upper bound, max exact.
- Timer overhead (back-to-back steady_clock reads, included in every figure): p50 30 ns, p99 31 ns

| path, workload | p50 ns | p90 ns | p99 ns | p99.9 ns | max ns | mean ns | samples |
|---|---:|---:|---:|---:|---:|---:|---:|
| **tick-to-trade (end to end)**, golden eq_mbo (2000 ev) | 1279 | 2047 | 2559 | 11263 | 33112 | 1344.6 | 100000 |
| decode (IAP1 frame + CRC), golden eq_mbo (2000 ev) | 271 | 303 | 383 | 511 | 29626 | 273.9 | 100000 |
| book update, golden eq_mbo (2000 ev) | 83 | 135 | 223 | 287 | 27201 | 86.8 | 100000 |
| native features (48), golden eq_mbo (2000 ev) | 735 | 1343 | 1791 | 3455 | 32090 | 807.4 | 100000 |
| alpha score (EQ01+EQ03+EQ06), golden eq_mbo (2000 ev) | 135 | 215 | 271 | 303 | 29265 | 142.4 | 100000 |
| order decision + pre-trade check, golden eq_mbo (2000 ev) | 30 | 51 | 71 | 91 | 16100 | 34.2 | 100000 |
| **tick-to-trade (end to end)**, generated equity day (seed 20261012, 100000 ev) | 1407 | 1471 | 1727 | 12287 | 298570 | 1368.0 | 100000 |
| decode (IAP1 frame + CRC), generated equity day (seed 20261012, 100000 ev) | 271 | 271 | 303 | 383 | 24396 | 263.2 | 100000 |
| book update, generated equity day (seed 20261012, 100000 ev) | 103 | 143 | 223 | 799 | 156444 | 106.8 | 100000 |
| native features (48), generated equity day (seed 20261012, 100000 ev) | 895 | 959 | 1087 | 11263 | 201218 | 865.4 | 100000 |
| alpha score (EQ01+EQ03+EQ06), generated equity day (seed 20261012, 100000 ev) | 103 | 111 | 123 | 215 | 20468 | 102.8 | 100000 |
| order decision + pre-trade check, generated equity day (seed 20261012, 100000 ev) | 30 | 31 | 41 | 61 | 10510 | 29.8 | 100000 |

- golden eq_mbo (2000 ev): 50 timed pass(es); feature vectors 100000, orders 17650, pre-trade rejects 0, book drops 0
- generated equity day (seed 20261012, 100000 ev): 1 timed pass(es); feature vectors 100000, orders 150, pre-trade rejects 4574, book drops 0

Guard rows (compared by tests/harness/check_bench_regression.py --rows '^t2t .* p(50|99)$'):

| benchmark (tick-to-trade guard) | ns |
|---|---:|
| t2t golden p50 | 1279 |
| t2t golden p99 | 2559 |
| t2t genday p50 | 1407 |
| t2t genday p99 | 1727 |
