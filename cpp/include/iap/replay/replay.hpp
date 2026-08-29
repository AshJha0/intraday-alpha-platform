// Deterministic event-time replay driving order books (API_CORE.md section 5).
//
// Mirrors python/src/iap/replay/replay.py: consumes normalized events in
// event-time order, routes each to its per-instrument ConsolidatedBook,
// emits book-state snapshots every `snapshot_every` events, keeps the latest
// `keep_checkpoints` checkpoints every `checkpoint_every` events, and can be
// restored from any checkpoint to bit-identical subsequent state. No wall
// clock; all serialized iteration is in sorted key order (std::map).

#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <vector>

#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

// instrument_id -> venue_id -> exact-integer state summary.
using BookStates =
    std::map<std::uint32_t, std::map<std::uint16_t, BookStateSummary>>;

struct ReplaySnapshot {
    std::uint64_t index = 0;  // events processed when the snapshot was taken
    BookStates states;
};

inline bool operator==(const ReplaySnapshot& a, const ReplaySnapshot& b) {
    return a.index == b.index && a.states == b.states;
}

struct ReplayCheckpoint {
    std::uint64_t events_processed = 0;
    std::int64_t last_exchange_ts = 0;
    std::uint64_t time_regressions = 0;
    std::uint64_t checkpoint_every = 0;
    std::uint64_t snapshot_every = 0;
    std::map<std::uint32_t, ConsolidatedCheckpoint> books;
};

inline bool operator==(const ReplayCheckpoint& a, const ReplayCheckpoint& b) {
    return a.events_processed == b.events_processed &&
           a.last_exchange_ts == b.last_exchange_ts &&
           a.time_regressions == b.time_regressions &&
           a.checkpoint_every == b.checkpoint_every &&
           a.snapshot_every == b.snapshot_every && a.books == b.books;
}

struct ReplayRunSummary {
    std::uint64_t events_processed = 0;
    std::size_t instruments = 0;
    std::uint64_t time_regressions = 0;
    std::size_t snapshots = 0;
};

class ReplayEngine {
public:
    using SnapshotCallback =
        std::function<void(std::uint64_t, const ReplaySnapshot&)>;

    explicit ReplayEngine(std::uint64_t checkpoint_every = 0,
                          std::uint64_t snapshot_every = 0,
                          std::size_t keep_checkpoints = 4);

    ConsolidatedBook& instrument_book(std::uint32_t instrument_id);

    // Apply one event; tracks event-time monotonicity.
    void apply(const MarketEvent& ev);

    // Replay an event stream (with periodic snapshots/checkpoints).
    ReplayRunSummary run(const std::vector<MarketEvent>& events,
                         const SnapshotCallback& on_snapshot = nullptr);

    // Exact-integer per-venue state summaries (deterministic key order).
    BookStates book_states() const;

    ReplayCheckpoint checkpoint() const;
    static ReplayEngine restore(const ReplayCheckpoint& cp);

    std::uint64_t events_processed() const { return events_processed_; }
    std::uint64_t time_regressions() const { return time_regressions_; }
    const std::map<std::uint32_t, ConsolidatedBook>& books() const {
        return books_;
    }
    const std::vector<ReplayCheckpoint>& checkpoints() const {
        return checkpoints_;
    }
    const std::vector<ReplaySnapshot>& snapshots() const { return snapshots_; }

private:
    std::map<std::uint32_t, ConsolidatedBook> books_;
    std::uint64_t events_processed_ = 0;
    std::uint64_t checkpoint_every_;
    std::uint64_t snapshot_every_;
    std::size_t keep_checkpoints_;
    std::vector<ReplayCheckpoint> checkpoints_;
    std::vector<ReplaySnapshot> snapshots_;
    std::uint64_t time_regressions_ = 0;
    std::int64_t last_exchange_ts_ = 0;
};

}  // namespace iap
