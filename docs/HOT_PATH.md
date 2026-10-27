# The C++ hot path: order book and features

What happens to one market-data event in the canonical C++ code, how its data is laid out, where
it allocates, how its branches behave, and why it is kept byte-for-byte equal to the Python
reference. Written for two readers: an engineer who wants the design in a few minutes, and a
reviewer who wants each figure traced to the file and the machine that produced it.

Every number below is a measurement of this code on a stated host. All of them are in the
**exploratory** evidence class (`docs/LEARN.md` section 6): they describe the code, they support no
trading claim, and none of them is a latency edge. Nothing here was measured on a pinned,
isolated or colocated machine.

## 1. The path, and what is not on it

The plain idea: one event goes through four steps, each a function call in one thread, with no
queue, lock or system call between them.

```
IAP1 frame (104 bytes)  ->  decode_iap1 (CRC checked)  ->  OrderBook::apply
                        ->  FeatureEngine::apply (48 features)  ->  score_row (3 alphas)
```

`cpp/bench/bench_tick_to_trade.cpp` times that whole path plus a bench-local order decision;
`cpp/bench/bench_book_features.cpp` (new in v2.1) times the book and the feature engine on their
own. Not on the path, and not measured anywhere in this repository: the network, the NIC, the
kernel (no kernel bypass, no sockets), order encoding and the wire send, the venue round trip, the
platform risk engine (it is Rust and Python, not C++; `docs/POLYGLOT.md`), logging and the decision
trace.

## 2. What it costs, with the hardware

Two recorded runs answer this. Percentiles are the upper bound of an HDR-style log-linear bucket
(at most 6.25 % wide), so they read slightly high; `max` is exact. Each figure includes the cost of
the clock reads around it (stated per run).

**Whole path, per stage** (`benchmarks/results_tick_to_trade.md`; AMD EPYC 7763, 4 logical CPUs
visible, GitHub-hosted runner, no pinning; real-slice rows from CI run 38805495578 on 2026-10-15;
timer overhead p50 30 ns):

| stage, real ITCH slice AAPL 2019-01-30 (651,320 samples) | p50 ns | p99 ns | p99.9 ns |
|---|---:|---:|---:|
| decode (frame + CRC) | 255 | 335 | 543 |
| book update | 103 | 383 | 1,983 |
| features (48) | 991 | 1,855 | 11,775 |
| alpha score (3 linear models) | 103 | 143 | 215 |
| tick-to-trade, end to end | 1,535 | 2,943 | 12,799 |

**Book and features alone, by depth, with allocations** (`benchmarks/results_book_features.md`;
Intel Core 5 120U laptop, 12 logical CPUs, WSL2 Ubuntu on Windows 11 with other applications
running, GCC 15.2.0 `-O3 -DNDEBUG`, 2026-10-27; timer overhead p50 71 ns). Read the p50 column;
the tails on this host are dominated by the desktop OS. The CI job `cpp` re-runs the same binary on
every pull request that touches C++ and uploads its table as the artifact `book-features-bench`.

| stage | workload | p50 ns | p99 ns | allocs per 1,000 events |
|---|---|---:|---:|---:|
| book | shallow (seed 20261027, orders within 10 ticks of the mid) | 183 | 767 | 0.07 |
| book | deep (seed 20261028, within 500 ticks; ~1,000 levels at the end) | 247 | 1,023 | 0.15 |
| book | real ITCH slice (5,353 levels at the end) | 239 | 1,535 | 0.37 |
| book, pools reserved first | real ITCH slice | 247 | 1,215 | 0.00 |
| features (48) | real ITCH slice | 1,087 | 5,631 | 3.82 |

Three things are worth reading out of these tables.

- **The feature engine is about two thirds of the median path** (991 of 1,535 ns on the EPYC run). It
  keeps its own merged top-10 book view per instrument, so book maintenance is paid a second time
  inside it, and it updates ten rolling sums and two mid-price histories per instrument.
- **Depth costs the book little at the median and more in the tail.** Going from tens of levels to
  thousands moves the median by tens of nanoseconds; the p99 roughly doubles. Section 3 says why.
- **The book allocates only while its pools grow.** With `OrderBook::reserve` called first, the
  count is zero in the timed region. The feature engine's 1.5 to 3.8 allocations per 1,000 events
  are its rolling windows growing while they fill: each timed pass starts from fresh state, so
  every pass pays the fill. A steady-state count on a long-running engine is not measured here.

The run-to-run spread on the laptop is large: two runs ten minutes apart gave a shallow-book p99 of
463 ns and 767 ns. Treat a difference smaller than about 2x between rows of different runs as
noise.

## 3. Data layout

**The order book** (`cpp/include/iap/orderbook/book.hpp`, `order_index.hpp`):

- Orders and price levels live in two pools, `std::vector<OrderNode>` and
  `std::vector<LevelNode>`, with free lists of indices. A node refers to another by a 32-bit index,
  not a pointer, so a node is small (an `OrderNode` is 40 bytes: id, quantity, five links) and the
  pool can be checkpointed and restored by value.
- Each level holds its orders as an intrusive doubly linked FIFO through those indices; a second
  intrusive list keeps global arrival order (the Python reference reports resting orders in that
  order, so the C++ must too).
- Each side keeps a sorted `std::vector<uint32_t>` of level indices, best first. Finding a price is a
  binary search; adding or removing a level shifts the tail of that vector. For a book of a few
  thousand levels the shift is a few kilobytes of `memmove`, which is cheap per call but grows with
  depth. That is the tail effect in section 2.
- `order_id -> slot` is an open-addressing hash table (`OrderIndex`): linear probing, backward-shift
  deletion so there are no tombstones, power-of-two capacity, load factor kept at or below 0.7.
  One contiguous array, one probe sequence, no node allocation per order.

**The feature engine** (`cpp/include/iap/features/feature_engine.hpp`, `rolling.hpp`): per
instrument, a `ConsolidatedBook` of per-venue books, the merged top-10 levels in preallocated
vectors (`merge_scratch` included), and rolling windows that are ring buffers over contiguous
storage with power-of-two capacity (index by `& (size - 1)`, no modulo). Integer windows accumulate
in `__int128`, so a long session cannot wrap the running sum; float windows add and evict in the
same order as the Python reference so their drift stays inside the golden tolerance.

**One known cost:** instruments are found through `std::map<uint32_t, InstState>`, a tree lookup
per event. With one instrument it is one comparison; with many it is a pointer chase. A flat array
indexed by a dense instrument number would remove it. Not changed in v2.1, where no behaviour
change was in scope.

## 4. Allocation on the hot path

The rule (`PLATFORM_CONVENTIONS.md` section 8) is: no allocation per event after warm-up. How the
code meets it:

- pools, the order index and the rolling buffers grow geometrically, so a session pays
  O(log n) growth steps, not one per event, and `OrderBook::reserve` can move all of the book's
  growth before the session;
- the top-of-book is copied into a fixed `DepthBuffer` (a `std::array`), not a fresh vector;
- the hold-back buffer for late retransmissions (`std::map`, `reorder_window`) is off by default.

How it is checked: `bench_book_features` replaces global `operator new` with a counting wrapper and
reads the counter between clock reads, so the allocation figures in section 2 are counts inside each
timed stage. A reserved book shows zero. The feature engine shows the window-fill growth described
above. The bench does not count bytes and does not see memory obtained without `operator new`.

## 5. Branch behaviour

- **The common case is straight-line.** An ADD at an existing level, a CANCEL or an EXECUTE is: hash
  probe, a few index updates, a quantity subtraction. New levels and level removal are the less
  common, more expensive branches.
- **The malformed-event policy never throws mid-stream.** Every bad input (duplicate sequence,
  unknown order, invalid side, overflowing arithmetic) is a counted drop: a compare and an
  increment, then return. Exceptions stay off the per-event path, and the drop branches are rare
  enough on real data to be predicted not-taken (the real-slice run reports 0 book drops).
- **Arithmetic is checked, not trusted.** Prices and quantities are `int64` ticks; sums that can
  overflow are widened (`__int128`) or checked. This costs a few instructions and buys the same
  answer as Python's unbounded integers.
- The bench's own decision step (a vote, a threshold, four risk compares) is about 30 ns at the
  median in the EPYC run, so branch cost outside the book and the features is small.

None of this was measured with hardware counters (`perf stat` branch-miss rates): the shared runner
does not expose them reliably, and the laptop runs under WSL2. The statements above come from
reading the code and from the timing distributions, not from counter data.

## 6. Why the C++ is kept equal to the Python

The plain idea: Python is where the semantics are decided and tested on research data; C++ is
where the same semantics run fast. If the two ever disagree, a research result would describe code
that does not run in the fast path.

The formal statement: for every committed golden input (`tests/golden/events_*.jsonl` and the F7
parity cases), the C++ output equals the committed expected file generated by the Python reference
(`python/tools/make_golden*.py`): book states, anomaly counters and checkpoints exactly as
integers, features at 1e-9 absolute / relative (`make_golden_features.py`, `TOL = 1e-9`). The `golden`
CI job builds C++ and runs these on every pull request; the policy for which copy of each component
is canonical is in `docs/POLYGLOT.md`.

A worked example: the C++ feature engine once used an `int64` accumulator for order-flow windows.
On a long session the sum wrapped, and every later `ofi_*` value was wrong while still marked
valid. The Python reference, with unbounded integers, gives the right answer, so a parity case
long enough to wrap the sum is the check that exposes the difference. The fix is the `__int128` accumulator described in
section 3 (the comment in `rolling.hpp` records it). Without the parity rule, the fast copy would
have been trusted because it was fast.

## 7. Honest limits

- **Shared CI runner.** The EPYC figures come from a GitHub-hosted VM shared with other tenants;
  four logical CPUs visible, no control over frequency scaling or neighbours.
- **No pinning, no isolated core, no huge pages, no `rdtsc`.** Timing uses `steady_clock`, whose
  own cost (22 to 71 ns at the median per pair of reads on these hosts) is inside every figure.
- **Not colocation.** No network path is in any figure. A real tick-to-trade number would add NIC,
  kernel or kernel-bypass stack, and the wire, which typically cost more than this whole in-process
  path.
- **No latency edge is claimed.** These are figures for a research platform's reference
  implementation, measured so that regressions are visible and the design can be discussed, not to
  compare with a production trading system.
- **The laptop table is one run.** It is committed with its host stated; the CI artifact is the
  repeatable source.

## 8. CI time (v2.1)

The per-pull-request CI was cut by risk, not by deleting checks (`.github/workflows/ci.yml`,
header). On main at 5fb22ad2 (run 40406094034) the longest job was `python` (3.11) at 24.4 min,
and 20.5 min of it was `pytest (not slow)` with coverage; the same suite without coverage on 3.12
took 8.8 min. So:

- pull requests collect no coverage; the `coverage` job and its 92 % floor run on push to main,
  nightly (02:23 UTC) and on dispatch;
- `python-slow` and `cpp` run their heavy steps on a pull request only when it touches what they
  test, and report green with a note otherwise; the `golden` job still builds C++ for parity on
  every pull request;
- every job still reports under its own name, so the seventeen required contexts in
  `docs/GOVERNANCE.md` are unchanged.

Expected effect, not yet measured: the PR critical path falls from about 25 min to about 10 to 12
min for a Python change, and further for a docs-only change. The first pull request that runs the
new workflow is the measurement.

## Sources

| figure | file | host and date |
|---|---|---|
| whole-path stages | `benchmarks/results_tick_to_trade.md` | AMD EPYC 7763, GitHub runner, CI run 38805495578 (2026-10-15) |
| book / features by depth, allocations | `benchmarks/results_book_features.md` | Intel Core 5 120U laptop, WSL2, 2026-10-27 |
| CI job and step times | GitHub Actions run 40406094034 (push to main, 5fb22ad2) | GitHub-hosted ubuntu-24.04 |
| golden tolerance | `python/tools/make_golden_features.py` | n/a |
