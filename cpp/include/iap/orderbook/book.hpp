// L1/L2/MBO order book (PLATFORM_CONVENTIONS.md section 4 — pinned).
//
// Semantics mirror python/src/iap/orderbook/book.py exactly (see API_CORE.md
// section 4): status-gated matching, synthetic ids for id-less QUOTE/SNAPSHOT
// records, SNAPSHOT countdown validation, the full malformed-event policy
// (drop + count, checked i64 arithmetic, never throw mid-stream), sequence
// bootstrap at any value, in-stream sequence resets, and an optional bounded
// hold-back buffer (`reorder_window`) for late retransmissions.
// Hot-path design: order and level pools in contiguous vectors with free
// lists, intrusive FIFO linked lists per price level, sorted per-side
// level-index vectors (best first), and an open-addressing order_id index —
// no per-event allocation after warmup (reserve()).

#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

#include "iap/marketdata/events.hpp"
#include "iap/orderbook/order_index.hpp"

namespace iap {

constexpr int DEPTH_LEVELS = 10;

// Checkpoint schema version (API_CORE section 4/5).
constexpr std::int64_t CHECKPOINT_VERSION = 2;

// Largest accepted reorder_window (hold-back buffer, events) — pinned.
constexpr std::size_t MAX_REORDER_WINDOW = 4096;

// Reserved order-id range (top 16 bits set): synthetic ids for id-less
// QUOTE/SNAPSHOT records = SYNTHETIC_ID_BASE | side << 40 | ordinal.
constexpr std::uint64_t SYNTHETIC_ID_BASE = 0xFFFF000000000000ULL;

inline std::uint64_t synthetic_order_id(std::uint8_t side, std::uint64_t ordinal) {
    return SYNTHETIC_ID_BASE | (static_cast<std::uint64_t>(side) << 40) |
           (ordinal & ((std::uint64_t{1} << 40) - 1));
}

// (price_ticks, total_size) or (price_ticks, order_count).
using LevelEntry = std::pair<std::int64_t, std::int64_t>;

// QC counters — exactly the Python reference's 12 counters, pinned order.
struct BookCounters {
    std::uint64_t duplicates_dropped = 0;
    std::uint64_t gaps_detected = 0;
    std::uint64_t dropped_while_stale = 0;
    std::uint64_t unknown_order_events = 0;
    std::uint64_t invalid_side_dropped = 0;
    std::uint64_t invalid_payload_dropped = 0;
    std::uint64_t unknown_type_dropped = 0;
    std::uint64_t modify_price_mismatch = 0;
    std::uint64_t snapshot_restarts = 0;
    std::uint64_t sequence_resets = 0;
    std::uint64_t late_recovered = 0;
    std::uint64_t events_applied = 0;

    // Sum of the drop counters (accounting invariant: applied + drops +
    // pending == events fed).
    std::uint64_t drops() const {
        return duplicates_dropped + dropped_while_stale + unknown_order_events +
               invalid_side_dropped + invalid_payload_dropped +
               unknown_type_dropped + modify_price_mismatch;
    }
};

inline bool operator==(const BookCounters& a, const BookCounters& b) {
    return a.duplicates_dropped == b.duplicates_dropped &&
           a.gaps_detected == b.gaps_detected &&
           a.dropped_while_stale == b.dropped_while_stale &&
           a.unknown_order_events == b.unknown_order_events &&
           a.invalid_side_dropped == b.invalid_side_dropped &&
           a.invalid_payload_dropped == b.invalid_payload_dropped &&
           a.unknown_type_dropped == b.unknown_type_dropped &&
           a.modify_price_mismatch == b.modify_price_mismatch &&
           a.snapshot_restarts == b.snapshot_restarts &&
           a.sequence_resets == b.sequence_resets &&
           a.late_recovered == b.late_recovered &&
           a.events_applied == b.events_applied;
}

// One resting order as reported by OrderBook::resting_orders().
struct RestingOrder {
    std::uint64_t order_id = 0;
    std::uint8_t side = 0;
    std::int64_t price_ticks = 0;
    std::int64_t qty = 0;
};

inline bool operator==(const RestingOrder& a, const RestingOrder& b) {
    return a.order_id == b.order_id && a.side == b.side &&
           a.price_ticks == b.price_ticks && a.qty == b.qty;
}

// Golden-comparable exact-integer state (shape of expected_book_states.json).
struct BookStateSummary {
    std::int64_t best_bid_ticks = 0;
    std::int64_t best_bid_size = 0;
    std::int64_t best_ask_ticks = 0;
    std::int64_t best_ask_size = 0;
    std::vector<LevelEntry> depth_bid_top5;
    std::vector<LevelEntry> depth_ask_top5;
    std::vector<LevelEntry> order_count_bid_top3;
    std::vector<LevelEntry> order_count_ask_top3;
    std::int64_t trade_flow = 0;
    std::uint64_t sequence = 0;
};

inline bool operator==(const BookStateSummary& a, const BookStateSummary& b) {
    return a.best_bid_ticks == b.best_bid_ticks &&
           a.best_bid_size == b.best_bid_size &&
           a.best_ask_ticks == b.best_ask_ticks &&
           a.best_ask_size == b.best_ask_size &&
           a.depth_bid_top5 == b.depth_bid_top5 &&
           a.depth_ask_top5 == b.depth_ask_top5 &&
           a.order_count_bid_top3 == b.order_count_bid_top3 &&
           a.order_count_ask_top3 == b.order_count_ask_top3 &&
           a.trade_flow == b.trade_flow && a.sequence == b.sequence;
}

// Checkpoint types: full deterministic serialization; levels sorted by
// (side, price) ascending, orders FIFO within each level.
struct LevelCheckpoint {
    std::uint8_t side = 0;
    std::int64_t price_ticks = 0;
    std::vector<std::pair<std::uint64_t, std::int64_t>> orders;  // (id, qty)
};

inline bool operator==(const LevelCheckpoint& a, const LevelCheckpoint& b) {
    return a.side == b.side && a.price_ticks == b.price_ticks &&
           a.orders == b.orders;
}

struct BookCheckpoint {
    std::uint32_t instrument_id = 0;
    std::uint16_t venue_id = 0;
    std::vector<LevelCheckpoint> levels;
    // Global arrival order of resting order ids — restore() rebuilds it so
    // resting_orders() round-trips checkpoints exactly (levels alone only
    // pin per-level FIFO). Mirrors the Python checkpoint schema (pinned).
    std::vector<std::uint64_t> arrival_order;
    std::uint64_t last_sequence = 0;
    bool has_sequence = false;
    std::uint64_t sequence_epoch = 0;
    std::int64_t exchange_ts = 0;
    std::int64_t receive_ts = 0;
    std::int64_t trade_flow = 0;
    std::int64_t status = static_cast<std::int64_t>(SessionStatus::TRADING);
    bool stale = false;
    bool snapshot_active = false;
    bool snapshot_broken = false;
    std::uint64_t snapshot_countdown = 0;
    std::array<std::uint64_t, 2> snapshot_synthetic_next{0, 0};
    std::uint64_t reorder_window = 0;
    std::vector<MarketEvent> reorder_pending;  // sequence order
    BookCounters counters;
};

inline bool operator==(const BookCheckpoint& a, const BookCheckpoint& b) {
    return a.instrument_id == b.instrument_id && a.venue_id == b.venue_id &&
           a.levels == b.levels && a.arrival_order == b.arrival_order &&
           a.last_sequence == b.last_sequence &&
           a.has_sequence == b.has_sequence &&
           a.sequence_epoch == b.sequence_epoch &&
           a.exchange_ts == b.exchange_ts && a.receive_ts == b.receive_ts &&
           a.trade_flow == b.trade_flow && a.status == b.status &&
           a.stale == b.stale && a.snapshot_active == b.snapshot_active &&
           a.snapshot_broken == b.snapshot_broken &&
           a.snapshot_countdown == b.snapshot_countdown &&
           a.snapshot_synthetic_next == b.snapshot_synthetic_next &&
           a.reorder_window == b.reorder_window &&
           a.reorder_pending == b.reorder_pending && a.counters == b.counters;
}

// Per-event verdict returned by OrderBook::apply (pinned).
//
// applied + dropped + held == events fed, for every book; downstream
// consumers (feature engine, API_FEATURES.md section 2) MUST ignore every
// event that is not APPLIED.
enum class ApplyStatus : std::uint8_t {
    APPLIED = 0,  // the event changed book state (or a valid no-op)
    DROPPED = 1,  // rejected, counted in exactly one drop counter
    HELD = 2,     // buffered behind a sequence hole (reorder_window > 0)
};

// MBO order book for one instrument on one venue (venue_id == 0: synthetic,
// accepts any venue).
class OrderBook {
public:
    // reorder_window: hold-back buffer size for late retransmissions
    // (0 = off, max MAX_REORDER_WINDOW; larger throws std::invalid_argument).
    OrderBook(std::uint32_t instrument_id, std::uint16_t venue_id,
              std::size_t reorder_window = 0);

    // Pre-size pools; safe to call any time (never shrinks).
    void reserve(std::size_t orders, std::size_t levels);

    // Apply one event (sequence-checked). Throws std::invalid_argument ONLY
    // on routing errors; every malformed event is dropped + counted.
    // Returns the pinned per-event verdict: downstream consumers (the
    // feature engine, API_FEATURES.md section 2) fold ONLY APPLIED events
    // into rolling state.
    ApplyStatus apply(const MarketEvent& ev);

    // Explicit venue sequence reset (session roll known out of band): held
    // events are flushed, then a new epoch starts (next event accepted
    // whatever its sequence) with the book stale until a complete burst.
    void reset_sequence();

    // ------------------------------------------------------- derived state
    std::optional<LevelEntry> best_bid() const;
    std::optional<LevelEntry> best_ask() const;
    // Top-N (price, total_size), best first.
    std::vector<LevelEntry> depth(Side side, int levels = DEPTH_LEVELS) const;
    // Allocation-free variant: clears `out` and fills it (capacity reused —
    // feature/execution hot paths, conventions section 8).
    void depth_into(Side side, int levels, std::vector<LevelEntry>& out) const;
    // Total resting size at an exact price level (0 when the level is absent).
    std::int64_t level_qty(Side side, std::int64_t price_ticks) const;
    // Top-N (price, order_count), best first.
    std::vector<LevelEntry> order_count(Side side,
                                        int levels = DEPTH_LEVELS) const;
    std::size_t order_count_total() const { return index_.size(); }
    // Every resting order in global arrival order (mirrors the Python
    // reference's resting_orders()).
    std::vector<RestingOrder> resting_orders() const;
    BookStateSummary state_summary() const;

    // True when best bid > best ask (call phase / crossed feed); locked: ==.
    bool is_crossed() const;
    bool is_locked() const;
    // Not stale and the last event was received within max_age_ns of now_ns.
    bool is_fresh(std::int64_t now_ns, std::int64_t max_age_ns) const;

    std::uint32_t instrument_id() const { return instrument_id_; }
    std::uint16_t venue_id() const { return venue_id_; }
    std::size_t reorder_window() const { return reorder_window_; }
    std::uint64_t last_sequence() const { return last_sequence_; }
    bool has_sequence() const { return has_sequence_; }
    std::uint64_t sequence_epoch() const { return sequence_epoch_; }
    std::int64_t exchange_ts() const { return exchange_ts_; }
    std::int64_t receive_ts() const { return receive_ts_; }
    std::int64_t trade_flow() const { return trade_flow_; }
    std::int64_t status() const { return status_; }
    bool stale() const { return stale_; }
    std::size_t pending_count() const { return pending_.size(); }
    const BookCounters& counters() const { return counters_; }

    // -------------------------------------------------------- checkpoints
    BookCheckpoint checkpoint() const;
    static OrderBook restore(const BookCheckpoint& cp);

private:
    friend class ConsolidatedBook;

    static constexpr std::uint32_t NIL = 0xFFFFFFFFu;

    struct OrderNode {
        std::uint64_t id = 0;
        std::int64_t qty = 0;
        std::uint32_t prev = NIL;       // level FIFO links
        std::uint32_t next = NIL;
        std::uint32_t level = NIL;
        std::uint32_t arr_prev = NIL;   // global arrival-order links
        std::uint32_t arr_next = NIL;
    };

    struct LevelNode {
        std::int64_t price = 0;
        std::int64_t total_qty = 0;
        std::uint32_t head = NIL;
        std::uint32_t tail = NIL;
        std::uint32_t count = 0;
        std::uint8_t side = 0;
    };

    std::uint32_t instrument_id_;
    std::uint16_t venue_id_;
    std::size_t reorder_window_;

    std::vector<OrderNode> orders_;       // order pool
    std::vector<std::uint32_t> free_orders_;
    std::vector<LevelNode> levels_;       // level pool
    std::vector<std::uint32_t> free_levels_;
    // Sorted level-pool indices, best first (bids: price desc, asks: asc).
    std::vector<std::uint32_t> side_levels_[2];
    OrderIndex index_;                    // order_id -> order pool slot
    // Intrusive doubly-linked list of resting orders in arrival order.
    std::uint32_t arrival_head_ = NIL;
    std::uint32_t arrival_tail_ = NIL;

    std::uint64_t last_sequence_ = 0;
    bool has_sequence_ = false;
    std::uint64_t sequence_epoch_ = 0;
    std::int64_t exchange_ts_ = 0;
    std::int64_t receive_ts_ = 0;
    std::int64_t trade_flow_ = 0;
    std::int64_t status_ = static_cast<std::int64_t>(SessionStatus::TRADING);
    bool stale_ = false;
    bool snapshot_active_ = false;
    bool snapshot_broken_ = false;
    std::uint64_t snapshot_countdown_ = 0;
    std::array<std::uint64_t, 2> snapshot_synthetic_next_{0, 0};
    std::map<std::uint64_t, MarketEvent> pending_;  // hold-back buffer
    BookCounters counters_;

    std::uint32_t alloc_order();
    std::uint32_t alloc_level();
    void arrival_append(std::uint32_t oi);
    void arrival_unlink(std::uint32_t oi);
    std::size_t level_pos(int side, std::int64_t price) const;
    std::uint32_t find_level(int side, std::int64_t price) const;
    std::int64_t level_total(int side, std::int64_t price) const;
    void insert_order(int side, std::int64_t price, std::uint64_t order_id,
                      std::int64_t qty);
    void remove_order(std::uint32_t oi);
    void clear_side(int side);
    std::int64_t match_marketable(int side, std::int64_t price,
                                  std::int64_t qty);
    void clear_book();

    ApplyStatus apply_sequenced(const MarketEvent& ev, bool from_buffer);
    // Applies every held-back event in sequence order (gap declared);
    // returns the verdict of `target` when the caller tracks one.
    ApplyStatus flush_pending(std::uint64_t target = 0, bool has_target = false);
    void drain_pending();
    static bool payload_ok(const MarketEvent& ev);

    bool apply_add(const MarketEvent& ev);
    bool apply_modify(const MarketEvent& ev);
    bool apply_cancel(const MarketEvent& ev);
    bool apply_execute(const MarketEvent& ev);
    bool apply_quote(const MarketEvent& ev);
    bool apply_snapshot(const MarketEvent& ev);
};

// Consolidated view over per-venue books of one instrument. Routes events by
// venue_id; merged depth (NON-STALE venues only, pinned) sums sizes and order
// counts at equal prices; best is best across venues. Sequence/staleness/
// status remain per venue.
struct ConsolidatedCheckpoint {
    std::uint32_t instrument_id = 0;
    std::uint64_t reorder_window = 0;
    std::map<std::uint16_t, BookCheckpoint> venues;
};

inline bool operator==(const ConsolidatedCheckpoint& a,
                       const ConsolidatedCheckpoint& b) {
    return a.instrument_id == b.instrument_id &&
           a.reorder_window == b.reorder_window && a.venues == b.venues;
}

class ConsolidatedBook {
public:
    explicit ConsolidatedBook(std::uint32_t instrument_id,
                              std::size_t reorder_window = 0);

    OrderBook& venue_book(std::uint16_t venue_id);
    // Route one event to its venue book; returns the pinned verdict.
    ApplyStatus apply(const MarketEvent& ev);
    // Explicit sequence reset on every venue book (session roll).
    void reset_sequences();

    std::optional<LevelEntry> best_bid() const;
    std::optional<LevelEntry> best_ask() const;
    std::vector<LevelEntry> depth(Side side, int levels = DEPTH_LEVELS) const;
    std::vector<LevelEntry> order_count(Side side,
                                        int levels = DEPTH_LEVELS) const;
    // Sum of per-venue cumulative signed trade flow (all venues), saturated
    // to int64.
    std::int64_t trade_flow() const;
    bool is_crossed() const;
    bool is_locked() const;
    // Sorted venue ids merged into the view (not stale) / excluded (stale).
    std::vector<std::uint16_t> active_venues() const;
    std::vector<std::uint16_t> stale_venues() const;
    // Session status of one venue's book (nullopt when unknown).
    std::optional<std::int64_t> venue_status(std::uint16_t venue_id) const;

    std::uint32_t instrument_id() const { return instrument_id_; }
    std::size_t reorder_window() const { return reorder_window_; }
    const std::map<std::uint16_t, OrderBook>& books() const { return books_; }

    ConsolidatedCheckpoint checkpoint() const;
    static ConsolidatedBook restore(const ConsolidatedCheckpoint& cp);

private:
    // (price, size, count) rows, best first, aggregated across venues.
    std::vector<std::array<std::int64_t, 3>> merged(Side side) const;

    std::uint32_t instrument_id_;
    std::size_t reorder_window_;
    std::map<std::uint16_t, OrderBook> books_;
};

}  // namespace iap
