# C++ vs Rust vs Java for Event-Driven Low-Latency Trading: Evidence from Four Parallel Ports of One Platform

*Intraday Alpha Platform research series, paper 6 of 6 (spec §28). Generated 2026-08-29 from this repository's code, tests and benchmark artifacts. This is an engineering case study of THIS codebase, not a language shoot-out with general claims.*

---

## Abstract

The platform maintains four implementations of the same pinned semantics —
a Python reference plus C++, Rust and Java ports — held equal by
cross-language golden tests (spec §21) that require bit-identical codec
output (shared SHA-256 digests) and exact-integer book/feature/fill state.
This gives an unusually controlled setting for language comparison: same
algorithms, same event vectors, same container. Measured results: the C++
hot path decodes the IAP1 binary format at 3.5 ns/event (282M events/s),
updates the MBO book at 17.4 ns/event and replays end-to-end at 37.1M
events/s (`benchmarks/results_cpp.md`); demo-scale replay throughput is
≈ 6.9M events/s for Rust and ≈ 3.5M events/s for Java (different
measurement boundaries, disclosed below). All four suites pass in the
same harness run: Python 443 tests (45 golden), C++ 175 (37), Rust 181
(36), Java 291 (13) — the counts of this paper's recorded 2026-08-29 run;
the platform has since grown (the adaptability layer landed 2026-08-30)
and the same harness now passes 489/175/181/315 (49/37/36/13 golden).
Architecturally, all three ports converge on the same
low-latency discipline — pooled objects, free lists, intrusive FIFO lists,
open-addressing indices, no steady-state allocation — but pay for it in
different currencies: C++ in manual invariant discipline, Rust in a
confined `unsafe` island (exactly one file, the SPSC ring buffer, 5
`unsafe` sites), Java in fighting the collector by hand-rolling primitive
data structures the standard library refuses to provide. We report line
counts, dependency footprints, build-system constraints (the Java port
deliberately has no Maven), and the observed engineering trade-offs, with
the spec §22 methodology statement: 2-CPU Intel Xeon @ 2.10 GHz container,
no pinning, g++ 13.3.0 / rustc 1.95.0 / OpenJDK 21.0.10, mean-only
timings.

---

## 1. Introduction

Language-comparison papers usually compare different programs. This
repository offers something stricter: four codebases *forced* to be the
same program by golden tests covering order-book state after every event,
feature vectors, alpha scores, risk decisions, replay fills and codec
bytes (spec §21; `tests/golden/`). Divergence fails CI
(`tests/harness/run_all.sh`). Under that constraint, the remaining
differences — throughput, code volume, unsafe surface, allocation strategy,
build friction — are attributable to the languages and their ecosystems
rather than to design drift.

## 2. Methodology (spec §22 statement)

**Hardware/OS:** 2-CPU Intel(R) Xeon(R) @ 2.10 GHz container, Linux kernel
6.18 (6.18.44-fc-v22), no CPU pinning or NUMA control available, shared
cloud host. **Toolchains:** g++ 13.3.0 (`-O3 -DNDEBUG -Wall -Wextra
-Werror`, C++17, CMake Release); rustc/cargo 1.95.0 (release profile;
workspace pins `opt-level = 2` even for dev/test to keep golden replays
fast); OpenJDK 21.0.10 (plain `javac -Xlint:all -Werror`); Python 3.11.15
as reference. **Workload:** the pinned golden vectors —
`tests/golden/events_eq_mbo.jsonl` (2,000 equity MBO events) and
`events_fx_quote.jsonl` (800 FX QUOTE/TRADE events). **Timing:** C++
numbers from `cpp/bench/bench_all.cpp` (steady-clock wall loops, 3 warmup
iterations, then ≥ 0.5 s and ≥ 10 iterations per benchmark,
single-threaded); Rust and Java numbers from each port's replay demo run in
this container on 2026-08-29. **Caveats, per spec §22's "never present an
isolated latency number without methodology":** mean-only (no p50/p99 —
container timers and shared CPUs make tails unreliable here), cross-run
variance a few percent, and the three languages' replay figures have
*different measurement boundaries* (§4.2). Line counts measured 2026-08-29
on a repository under active development.

## 3. The three architectures

All ports implement the pinned semantics of `PLATFORM_CONVENTIONS.md` §4
(order-book), §3 (determinism: SplitMix64 only, sorted iteration) and the
IAP1 binary format (`schemas/FORMAT.md`). Their hot-path designs:

**C++ (`cpp/`, 24 headers/sources + 12 test files).** The most aggressive
memory layout: order and level *pools in contiguous vectors with free
lists*, intrusive per-level FIFO linked lists, sorted per-side level-index
vectors, and a hand-written open-addressing `order_id → slot` hash map with
linear probing and backward-shift deletion — "no per-event allocation after
warmup (`reserve()`)" is a documented contract
(`cpp/include/iap/orderbook/book.hpp`, `order_index.hpp`). The feature
engine pre-reserves all scratch (`cpp/src/features/feature_engine.cpp`).
C++ also owns the reference execution simulator (paper 5) and the
benchmark harness.

**Rust (`rust/`, 9 workspace crates, 44 source files).** Chooses safety
over layout: the order book uses `BTreeMap<(side, price), Level>` (sorted
iteration = determinism for free) plus a `HashMap` order index
(`rust/orderbook/src/book.rs`) — allocating, but safe and simple. The
performance-critical concession is the event bus: a hand-rolled bounded
SPSC lock-free ring buffer with cache-line-padded (`#[repr(align(64))]`)
atomic head/tail and `UnsafeCell<MaybeUninit<T>>` slots. **All `unsafe` in
the entire workspace is confined to this one file** —
`rust/eventbus/src/lib.rs`, 5 `unsafe` occurrences (two Send/Sync impls,
three slot accesses), each with a SAFETY comment; `grep -rc unsafe
rust/*/src` confirms zero elsewhere. Dependency policy is austere: serde +
serde_json only (`rust/Cargo.toml`; the full `Cargo.lock` closure is 20
packages, 9 of which are the workspace's own crates).

**Java (`java/`, 74 main sources at time of writing).** The interesting
port, because the platform's low-latency conventions ("avoid allocation
and boxing on hot paths", spec §23) are *against the grain* of idiomatic
Java. The order book pools order nodes on a free list, uses intrusive
doubly-linked FIFO queues, and hand-rolls an open-addressing primitive-long
hash map to avoid `HashMap<Long, Order>` boxing; levels live in `TreeMap`s
for sorted determinism (`java/src/main/java/com/iap/orderbook/OrderBook.java`
class doc). The feature engine documents "primitive ring buffers and
preallocated merge scratch; no steady-state allocation in the rolling
machinery" (`java/.../features/FeatureEngine.java`). GC pressure is thus
addressed the only way it can be on a hot path: by not allocating.
Notably, the Java build is plain `javac` — **no Maven/Gradle, by
documented decision** (`docs/BUILD_NOTES.md`: Maven Central is unreachable
from the build environment; the pom-equivalent dependency list is exactly
JUnit 4 + Hamcrest, vendored from the system).

**Python (`python/src/iap`, 73 modules)** is the semantic reference and
research stack; it is not a performance contender and is excluded from
throughput claims.

## 4. Measured performance

### 4.1 C++ stage benchmarks

From `benchmarks/results_cpp.md` (methodology §2):

| benchmark | ns/event | events/sec |
|---|---:|---:|
| IAP1 decode (eq) | 3.5 | 282,006,214 |
| IAP1 encode (eq) | 3.3 | 307,002,149 |
| JSONL decode (eq) | 214.0 | 4,673,300 |
| book update (eq MBO) | 17.4 | 57,416,259 |
| book update (fx QUOTE) | 29.6 | 33,818,930 |
| replay engine (eq) | 27.0 | 37,079,293 |
| feature engine (48 feats, cadence 0) | 450.5 | 2,219,575 |
| alpha scoring (3 alphas) | 32.3 | 30,962,520 |
| execution sim replay | 41.7 | 23,969,113 |

Two engineering facts stand out: the binary codec is ~60x faster than JSONL
(3.5 vs 214 ns/event) — format choice dwarfs most micro-optimization — and
the pooled MBO book absorbs an event in ~37 cycles at 2.1 GHz.

### 4.2 Cross-language replay throughput (boundaries disclosed)

| language | figure | boundary |
|---|---|---|
| C++ | 37.1M events/s | replay engine loop only, pre-decoded events, warmup + ≥0.5 s loops (`bench_all`) |
| Rust | 6.89M (eq) / 6.07M (fx) events/s | single timed `engine.run()` pass per vector, release build (`rust/replay/src/bin/demo.rs` output, this container) |
| Java | 3.48M events/s | 25 timed full replays (fresh engine per rep, both vectors = 70,000 events) after 5 warmup reps (`java/.../replay/Demo.java` output, this container) |

These support an order-of-magnitude ranking (C++ ≈ 5x Rust ≈ 2x Java on
this path in this repo) and nothing finer: the Rust figure times a single
0.1-0.3 ms pass (high variance), and the Java figure includes per-rep
engine construction and runs under a JIT warmed for only 5 reps. An
apples-to-apples `bench_all` port for Rust/Java is the obvious next step
and is noted as absent rather than approximated. For context, even 4M
events/s is ~3 orders of magnitude above the synthetic feed's peak needs
(paper 4, §4.3); throughput here buys research replay speed and burst
headroom.

### 4.3 Parity evidence

Full harness run (`tests/harness/run_all.sh`, this container, 2026-08-29):

| language | tests passed | golden-group tests | status |
|---|---|---|---|
| python | 443 | 45 | PASS |
| cpp | 175 | 37 | PASS |
| rust | 181 | 36 | PASS |
| java | 291 | 13 | PASS |

*Note (2026-08-30): the table above is the paper's timestamped
measurement. After the adaptability layer landed (Python `iap.adaptive` +
Java `com.iap.adaptive`, golden-pinned in
`tests/golden/expected_adaptive.json`), the same harness passes
python 489 (49 golden) / cpp 175 (37) / rust 181 (36) / java 315 (13) —
the parity claim is unchanged; the suites grew.*

Hardest evidence of parity: all languages must encode the golden vectors to
byte-identical IAP1 with pinned SHA-256 digests
(`tests/golden/expected_codec_sha256.json`:
`c4768ce3…0bfd284` / `599e4f22…f334d05`), and the Java demo run printed
exactly those digests. Book state after every event, feature vectors, alpha
scores, risk decisions and execution fills are compared as exact integers
or with explicit tolerances (spec §21; e.g. TCA identity at 1e-9,
`tests/golden/expected_tca.json`).

## 5. Engineering trade-offs observed in this repository

**Code volume (lines, 2026-08-29, production / tests):** C++ 4,492 / 3,465;
Rust 6,482 / 3,345; Java 9,130 / 6,515; Python 10,480 / 5,154. Raw counts
mislead, because the polyglot responsibility matrix (spec §3) gives each
port a different scope — C++ owns execution/SOR/benchmarks, Rust owns the
event bus, telemetry, risk and venue crates, Java owns portfolio/TCA/risk
services and is growing as this is written, and Python owns the entire
research stack. The defensible observation is narrower: for the *shared*
core (codec, book, replay, features, alpha), all three compiled ports land
within the same ~2x band, and none was dramatically terser than the others
once the golden tests forced identical edge-case handling.

**Where the difficulty lives differs by language.** In C++ the pooled book
is fast by default but every invariant (free-list integrity, index/pool
coherence) is enforced only by tests. In Rust the same design pressure
surfaced as a boundary decision: keep the book safe and allocating
(BTreeMap — and still hit 57M+ single-venue updates/s territory only in
C++), and spend the `unsafe` budget on the one component where lock-free
matters, with the compiler enforcing SPSC discipline through ownership
(`Producer`/`Consumer` are not `Clone`, push/pop take `&mut self` —
`rust/eventbus/src/lib.rs`). In Java the difficulty was ecosystem-shaped:
the collections library is hostile to allocation-free hot paths, so the
port re-implements what C++ gets from `std::vector` + indices (primitive
open-addressing map, intrusive lists, pooled nodes), and the *build* had to
abandon Maven entirely for environmental reasons — a reminder that
"language choice" in practice includes toolchain reachability
(`docs/BUILD_NOTES.md`).

**Determinism costs differ too.** Sorted iteration is free in Rust
(BTreeMap) and Java (TreeMap), but a discipline in C++ (sorted level-index
vectors). Conversely, allocation-freedom is near-free in C++ and a
deliberate effort in both Rust (avoided by design in the book) and Java
(fought for feature-engine and book paths, unavoidable elsewhere).

## 6. Limitations

1. **One workload, tiny vectors.** 2,000 + 800 events fit entirely in L1/L2
   cache; none of these numbers measures memory-system behavior at
   realistic book sizes or feed bursts.
2. **No tail latency.** Spec §22 asks for p50/p99/p99.9; the shared 2-CPU
   container cannot produce honest tails, so `benchmarks/results_cpp.md`
   publishes means only and says so. GC pause behavior — Java's
   characteristic risk — is therefore *unmeasured*, and the strongest Java
   claim we can support is "no steady-state allocation on the measured
   paths", not "no pauses".
3. **Boundary mismatch in §4.2**, as disclosed; Rust and Java lack a
   `bench_all` equivalent in `benchmarks/` at time of writing.
4. **Scopes differ per port** (spec §3), so LOC and test counts are not
   normalized; Java's golden count (13) reflects fewer, broader test
   classes (`CodecGoldenTest`, `BookGoldenTest`), not weaker coverage of
   the shared vectors.
5. **Moving target.** The repository is under active development; counts
   are timestamped 2026-08-29.

## 7. Conclusions

Within this repository, the classic folk hierarchy reproduces — C++ fastest
by a clear margin on the identical algorithm, Rust within an order of
magnitude with exactly one unsafe file, Java within another factor of ~2
after warmup with substantial hand-rolled machinery to sidestep the
collector — but the more useful findings are structural. First, golden
tests with byte-exact codec digests are what make a polyglot platform
maintainable: four implementations of pinned semantics stayed convergent
(443/175/181/291 tests green in one harness run) through active
development — including a coordinated regeneration of the golden vectors
and book-semantics rules that all four ports absorbed without parity loss. Second, the low-latency playbook (pool, don't allocate;
intrusive lists; open addressing; sorted iteration for determinism) is
language-independent — what differs is who enforces it: tests in C++, the
type system in Rust, and sheer discipline in Java. Third, ecosystem
constraints (an unreachable Maven Central, a deliberately austere 20-package
Cargo closure vs CMake+GoogleTest+Eigen) shaped the ports as much as
language semantics did. For this platform's actual feed rates, all three
ports are throughput-overprovisioned by orders of magnitude (paper 4), so
the deciding factors in practice were correctness leverage and engineering
cost — which is, we suspect, the honest general case more often than
latency folklore admits.

## Artifact provenance

| claim | artifact |
|---|---|
| C++ benchmark table + methodology | `benchmarks/results_cpp.md`, `cpp/bench/bench_all.cpp` |
| Rust / Java replay throughput + SHA digests | `rust/replay/src/bin/demo.rs`, `java/src/main/java/com/iap/replay/Demo.java` (+ `java/demo.sh`), run 2026-08-29 in this container |
| parity table | `tests/harness/run_all.sh` full run, 2026-08-29 |
| codec digests | `tests/golden/expected_codec_sha256.json` |
| C++ pooling/free-list/open-addressing design | `cpp/include/iap/orderbook/book.hpp`, `order_index.hpp`, `cpp/src/orderbook/book.cpp` |
| Rust unsafe confinement, SPSC design | `rust/eventbus/src/lib.rs`; `rust/orderbook/src/book.rs` (safe BTreeMap book) |
| Rust dependency policy | `rust/Cargo.toml`, `rust/Cargo.lock` (20 packages) |
| Java allocation strategy | `java/src/main/java/com/iap/orderbook/OrderBook.java`, `java/.../features/FeatureEngine.java` (class docs) |
| Java no-Maven decision + pom-equivalent | `docs/BUILD_NOTES.md`, `java/build.sh` |
| pinned semantics and conventions | `PLATFORM_CONVENTIONS.md` §§3-5, `docs/SPECIFICATION.md` §21-23 |
| line counts | `wc -l` over `cpp/{src,include,tests}`, `rust/**/*.rs` (excl. target), `java/src`, `python/src` — 2026-08-29 |

## Erratum / Update — 2026-09-06 (round-3 market-data boundary fixes)

- The Rust order book (`rust/orderbook/src/book.rs`) is no longer the "safe
  BTreeMap book" with `Vec`-backed FIFO and arrival lists described above:
  it now uses order/level slabs with intrusive per-level FIFO and global
  arrival lists plus price-keyed `BTreeMap`s, so cancels/executes/quotes
  are O(1) per order (the earlier design was O(N) in resting orders —
  quadratic on real books; a 50k-order cancel storm is now a regression
  test). Still no `unsafe` outside the SPSC ring.
- The Rust replay demo now streams events from a decoder thread through the
  bounded SPSC event bus into the engine (blocking backpressure, never
  drops). Re-measured in this container on 2026-09-06 with that cross-thread
  hand-off included: ≈ 6.5M events/s equity, ≈ 5.1M events/s FX (single
  timed pass, high variance) versus the ≈ 6.9M / ≈ 6.1M in-thread figures
  recorded above — the measurement boundary moved, the conclusion ("orders
  of magnitude of headroom") does not.
- All four books gained the same pinned anomaly semantics (status-gated
  matching, synthetic ids, reorder window, sequence resets, checked i64
  arithmetic, CRC-32 IAP1 trailer) and are held equal by two new anomaly
  golden vectors; see `docs/SCENARIOS.md` and `schemas/MIGRATIONS.md`
  (2026-09-06 entry). The parity counts quoted above are those of the
  2026-08-29 / 08-30 runs; the current table is in `README.md`.

### Benchmark erratum — the codec numbers above are superseded

**The paper's headline engineering claim is wrong in direction and is
withdrawn.** Section 4.1 reads "the binary codec is ~60x faster than JSONL
(3.5 vs 214 ns/event) — format choice dwarfs most micro-optimization". That
comparison was measured before IAP1 carried an integrity check. Round-3
made the CRC-32 trailer mandatory on every record (`schemas/MIGRATIONS.md`,
2026-09-06; rationale: a silently truncated or bit-flipped record must not
be replayed as valid state), and a byte-serial table CRC over the 144 KB
body costs roughly 5 cycles/byte. Re-measured on an idle container
(loadavg 0.25) with the same `bench_all` methodology:

| figure | as published (2026-08-29) | current (2026-09-06) |
|---|---|---|
| IAP1 decode | 3.5 ns/event | **174.4 ns/event** |
| IAP1 encode | 3.3 ns/event | **170.3 ns/event** |
| JSONL decode (control, no CRC) | 214 ns/event | 225.7 ns/event |
| book update (eq MBO) | 17.4 ns/event | **25.7 ns/event** |
| book update (fx QUOTE) | 29.6 ns/event | **36.2 ns/event** |
| feature engine (48 feats, cadence 0) | 450.5 ns/event | **530.4 ns/event** |
| alpha scoring (3 alphas) | 32.3 ns/event | **38.5 ns/event** |
| execution sim replay | 41.7 ns/event | **66.2 ns/event** |
| replay engine | 37.1M events/s | **28.1M events/s** |

The JSONL control moved 214 → 225.7 ns (container noise), which isolates
the *codec* change to the CRC path. The non-codec rows moved too — the
feature engine and alpha scoring by ~18-19 %, the execution sim by ~59 % —
from round-3 engine changes plus the same container variance; they are
listed here so no row of §4.1 is left looking current, but they carry no
part of the codec argument. Binary decode is now **1.29x** JSONL, not 60x:
with integrity checking included, format choice does *not* dwarf
micro-optimization on this path — the integrity check dominates the decode,
and a vectorised CRC (CLMUL) or a per-block rather than per-record check is
now the highest-value optimisation available, which is the opposite of the
paper's advice. The *architectural* conclusions (event-driven structure,
port comparison, boundaries) are unaffected; the *format* conclusion is
not, and should not be cited.

Current numbers, the cold single-pass reference table and the full
methodology are generated into `benchmarks/results_cpp.md` by
`cpp/bench/bench_all.cpp`; the caveat text is emitted by the generator so
it can no longer be lost on regeneration.
