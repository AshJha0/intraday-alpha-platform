# C++ one-pager: the hot path in questions and answers

A short read for a code review or a self-check. The design is in `docs/HOT_PATH.md`; the figures
it quotes are measurements of this code on stated hosts (AMD EPYC 7763 GitHub runner; an Intel
Core 5 120U laptop under WSL2), in the exploratory evidence class, with no latency edge claimed.
Where the repository has no C++ example of a topic, the answer says so and points at the nearest
real code.

## The system in five lines

- C++17, one thread per stream, no locks or queues on the path: decode -> order book -> 48 features
  -> alpha score (`cpp/src/`).
- Median in-process tick-to-trade 1,535 ns on a real Nasdaq ITCH slice (EPYC runner, CI run
  38805495578); the feature engine is about two thirds of it.
- No allocation per event after warm-up; a counting `operator new` in
  `cpp/bench/bench_book_features.cpp` shows zero for a reserved book.
- Every output equals the Python reference on committed golden inputs (integers exact, features at
  1e-9), checked by the `golden` CI job on every pull request.
- Not measured: network, NIC, kernel, wire, venue. Not pinned, not isolated, not colocated.

## Questions to check your understanding

**1. Why does cache behaviour matter more than instruction count here?**

A book update does little arithmetic; its cost is memory access. A miss to DRAM is on the order of
100 ns, which is the whole median book update. So the layout keeps what one event touches close
together: orders and levels in two contiguous pools addressed by 32-bit indices (an `OrderNode`
is 40 bytes, so several share a 64-byte line), the `order_id` index in one open-addressing array
(one probe walk, no per-node allocation), and each side's levels in a sorted index vector that a
binary search walks. The bench shows the effect indirectly: the reserved book, whose pools are
allocated large up front, has a higher p99.9 on two of three workloads than the book that grows
on demand, which fits a larger, colder working set (an inference from timing, not from cache
counters, which were not available).

**2. What is false sharing, and where would it bite?**

Two threads writing different variables that sit on the same cache line make the line bounce
between cores, though no data is shared. The C++ path is single-threaded, so it has none. The
repository's example is the Rust SPSC ring (`rust/eventbus/src/lib.rs`): the producer's `tail` and
the consumer's `head` are each wrapped in `#[repr(align(64))]`, so each index owns a cache line. In
C++ the same is `alignas(std::hardware_destructive_interference_size)` (or `alignas(64)` where the
constant is missing).

**3. How does a lock-free single-producer single-consumer queue work?**

A fixed array of `capacity + 1` slots and two indices. The producer writes only `tail`, the
consumer only `head`; the one always-empty slot tells full from empty without a shared counter.
Push: read own `tail` (relaxed), read `head` (acquire) to check for space, write the slot, publish
`tail + 1` (release). Pop mirrors it. Because each index has one writer, no compare-and-swap is
needed. The repository's implementation is the Rust `eventbus` crate (frozen, golden-pinned,
`docs/POLYGLOT.md`); the C++ path does not use a queue because decode, book and features run in one
thread.

**4. Which memory orders, and why not `seq_cst` everywhere?**

Release on the store that publishes a slot, acquire on the load that consumes it: that pair
guarantees the slot's contents are visible before the index that says it is ready. Relaxed is
enough for a thread reading its own index. `seq_cst` would also be correct but adds a full fence
on some architectures (a locked instruction or `mfence` on x86 for stores, more on ARM) and buys
nothing an SPSC needs: there is no third thread whose view must agree on a single order. The
`eventbus` code uses exactly relaxed / acquire / release.

**5. How do you keep allocation off the hot path?**

Reserve or grow geometrically, then reuse. The book's pools and the order index grow by doubling
and recycle slots through free lists; `OrderBook::reserve` moves all growth before the session.
Top-of-book is copied into a fixed `std::array` buffer. Rolling windows are power-of-two ring
buffers that stop growing once a session's peak window population has been seen. And measure it:
the bench's counting `operator new` reports 0.37 allocations per 1,000 events for the growing book
on the real slice and 0.00 when reserved; the feature engine's 3.8 per 1,000 are its windows
filling from fresh state each pass. Exceptions stay off the path too: a malformed event is a
counted drop, never a throw.

**6. Why golden vectors instead of only unit tests?**

Because the risk is two implementations drifting, not one being wrong in isolation. A golden file
is the reference implementation's output on a fixed input, committed; every other implementation
must reproduce it. That catches the bugs unit tests written from the same mental model miss: an
`int64` sum that wraps on a long session, a different tie-break when two orders share a timestamp,
a float summed in another order. The tolerance is part of the contract: integers exact, features
1e-9 absolute or relative. The cost is that a deliberate semantics change means regenerating
goldens from Python and reviewing the diff, which is the point.

**7. Where is the time actually going, and what would you change first?**

The feature engine: about 991 of 1,535 ns at the median on the EPYC run. It maintains its own
merged top-10 view per instrument (book work paid twice) and finds the instrument through a
`std::map`. First changes, in order: reuse the book's state instead of rebuilding the merged view
for a single-venue stream; replace the map with a flat array indexed by a dense instrument number;
then measure again before touching anything else. Each would need the golden suite to stay
byte-identical.

**8. How would you make these numbers trustworthy for a production decision?**

Pin the thread to an isolated core, fix the CPU frequency, use `rdtsc` with a calibrated
conversion (or hardware timestamps on the NIC), record branch-miss and cache-miss counters with
`perf stat`, run long enough to see rare tails, and report the host with every figure. None of that
is available on a shared runner, which is why the figures here are labelled as what they are.

## Where to look

| topic | file |
|---|---|
| book layout, free lists, sorted levels | `cpp/include/iap/orderbook/book.hpp` |
| open-addressing order index | `cpp/include/iap/orderbook/order_index.hpp` |
| ring-buffer windows, `__int128` sums | `cpp/include/iap/features/rolling.hpp` |
| book + features bench (allocations) | `cpp/bench/bench_book_features.cpp`, `benchmarks/results_book_features.md` |
| whole-path bench | `cpp/bench/bench_tick_to_trade.cpp`, `benchmarks/results_tick_to_trade.md` |
| SPSC ring, padding, memory orders | `rust/eventbus/src/lib.rs` |
| parity policy | `docs/POLYGLOT.md`, `tests/golden/` |
