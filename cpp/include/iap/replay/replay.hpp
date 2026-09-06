// Deterministic event-time replay driving order books (API_CORE.md section 5).
//
// Mirrors python/src/iap/replay/replay.py: consumes normalized events in
// event-time order, validates ids against an optional universe
// (instrument -> venue ids; unknown ids are dropped + counted), routes each
// to its per-instrument ConsolidatedBook, emits book-state snapshots every
// `snapshot_every` events (retaining the latest `keep_snapshots`), keeps the
// latest `keep_checkpoints` checkpoints every `checkpoint_every` events, and
// can be restored from any checkpoint to bit-identical subsequent state.
// Checkpoints serialize to the cross-language JSON document of API_CORE §5
// (checkpoint_to_json / checkpoint_from_json). No wall clock; all serialized
// iteration is in sorted key order (std::map).

#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

// Engine checkpoint schema version (API_CORE section 5).
constexpr std::int64_t ENGINE_CHECKPOINT_VERSION = 2;

// instrument_id -> venue_id -> exact-integer state summary.
using BookStates =
    std::map<std::uint32_t, std::map<std::uint16_t, BookStateSummary>>;

// instrument_id -> allowed venue ids.
using Universe = std::map<std::uint32_t, std::set<std::uint16_t>>;

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
    std::uint64_t unknown_instrument_dropped = 0;
    std::uint64_t unknown_venue_dropped = 0;
    std::uint64_t checkpoint_every = 0;
    std::uint64_t snapshot_every = 0;
    std::uint64_t keep_checkpoints = 4;
    std::uint64_t keep_snapshots = 4;
    std::uint64_t snapshots_emitted = 0;
    std::uint64_t reorder_window = 0;
    std::optional<Universe> universe;
    std::map<std::uint32_t, ConsolidatedCheckpoint> books;
};

inline bool operator==(const ReplayCheckpoint& a, const ReplayCheckpoint& b) {
    return a.events_processed == b.events_processed &&
           a.last_exchange_ts == b.last_exchange_ts &&
           a.time_regressions == b.time_regressions &&
           a.unknown_instrument_dropped == b.unknown_instrument_dropped &&
           a.unknown_venue_dropped == b.unknown_venue_dropped &&
           a.checkpoint_every == b.checkpoint_every &&
           a.snapshot_every == b.snapshot_every &&
           a.keep_checkpoints == b.keep_checkpoints &&
           a.keep_snapshots == b.keep_snapshots &&
           a.snapshots_emitted == b.snapshots_emitted &&
           a.reorder_window == b.reorder_window &&
           a.universe == b.universe && a.books == b.books;
}

// Cross-language JSON codec for checkpoints (API_CORE section 5 shape;
// structurally equal to the Python reference's `ReplayEngine.checkpoint()`).
// Throws std::invalid_argument on malformed / wrong-version input.
std::string checkpoint_to_json(const ReplayCheckpoint& cp);
ReplayCheckpoint checkpoint_from_json(const std::string& text);
std::string book_checkpoint_to_json(const BookCheckpoint& cp);
BookCheckpoint book_checkpoint_from_json(const std::string& text);

struct ReplayRunSummary {
    std::uint64_t events_processed = 0;
    std::size_t instruments = 0;
    std::uint64_t time_regressions = 0;
    std::uint64_t snapshots = 0;
    std::uint64_t unknown_instrument_dropped = 0;
    std::uint64_t unknown_venue_dropped = 0;
};

class ReplayEngine {
public:
    using SnapshotCallback =
        std::function<void(std::uint64_t, const ReplaySnapshot&)>;

    explicit ReplayEngine(std::uint64_t checkpoint_every = 0,
                          std::uint64_t snapshot_every = 0,
                          std::size_t keep_checkpoints = 4,
                          std::size_t keep_snapshots = 4,
                          std::size_t reorder_window = 0,
                          std::optional<Universe> universe = std::nullopt);

    ConsolidatedBook& instrument_book(std::uint32_t instrument_id);

    // Apply one event; counts it, validates it against the universe, tracks
    // event-time monotonicity.
    void apply(const MarketEvent& ev);

    // Explicit sequence reset on every book (session roll).
    void reset_sequences();

    // Replay an event stream (with periodic snapshots/checkpoints).
    ReplayRunSummary run(const std::vector<MarketEvent>& events,
                         const SnapshotCallback& on_snapshot = nullptr);

    // Exact-integer per-venue state summaries (deterministic key order).
    BookStates book_states() const;

    ReplayCheckpoint checkpoint() const;
    static ReplayEngine restore(const ReplayCheckpoint& cp);

    std::uint64_t events_processed() const { return events_processed_; }
    std::uint64_t time_regressions() const { return time_regressions_; }
    std::uint64_t unknown_instrument_dropped() const {
        return unknown_instrument_dropped_;
    }
    std::uint64_t unknown_venue_dropped() const { return unknown_venue_dropped_; }
    std::uint64_t snapshots_emitted() const { return snapshots_emitted_; }
    std::size_t keep_checkpoints() const { return keep_checkpoints_; }
    std::size_t keep_snapshots() const { return keep_snapshots_; }
    std::size_t reorder_window() const { return reorder_window_; }
    const std::optional<Universe>& universe() const { return universe_; }
    const std::map<std::uint32_t, ConsolidatedBook>& books() const {
        return books_;
    }
    std::map<std::uint32_t, ConsolidatedBook>& books() { return books_; }
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
    std::size_t keep_snapshots_;
    std::size_t reorder_window_;
    std::optional<Universe> universe_;
    std::vector<ReplayCheckpoint> checkpoints_;
    std::vector<ReplaySnapshot> snapshots_;
    std::uint64_t snapshots_emitted_ = 0;
    std::uint64_t time_regressions_ = 0;
    std::uint64_t unknown_instrument_dropped_ = 0;
    std::uint64_t unknown_venue_dropped_ = 0;
    std::int64_t last_exchange_ts_ = 0;
};

}  // namespace iap
