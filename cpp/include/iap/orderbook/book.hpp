// L1/L2/MBO order book (PLATFORM_CONVENTIONS.md section 4 — pinned).
//
// Semantics mirror python/src/iap/orderbook/book.py exactly (see API_CORE.md
// section 4). Hot-path design: order and level pools in contiguous vectors
// with free lists, intrusive FIFO linked lists per price level, sorted
// per-side level-index vectors (best first), and an open-addressing order_id
// index — no per-event allocation after warmup (reserve()).

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

// (price_ticks, total_size) or (price_ticks, order_count).
using LevelEntry = std::pair<std::int64_t, std::int64_t>;

struct BookCounters {
    std::uint64_t duplicates_dropped = 0;
    std::uint64_t gaps_detected = 0;
    std::uint64_t dropped_while_stale = 0;
    std::uint64_t unknown_order_events = 0;
    std::uint64_t invalid_side_dropped = 0;
    std::uint64_t events_applied = 0;
};

inline bool operator==(const BookCounters& a, const BookCounters& b) {
    return a.duplicates_dropped == b.duplicates_dropped &&
           a.gaps_detected == b.gaps_detected &&
           a.dropped_while_stale == b.dropped_while_stale &&
           a.unknown_order_events == b.unknown_order_events &&
           a.invalid_side_dropped == b.invalid_side_dropped &&
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
    std::int64_t exchange_ts = 0;
    std::int64_t receive_ts = 0;
    std::int64_t trade_flow = 0;
    std::int64_t status = static_cast<std::int64_t>(SessionStatus::TRADING);
    bool stale = false;
    bool snapshot_active = false;
    bool snapshot_broken = false;
    BookCounters counters;
};

inline bool operator==(const BookCheckpoint& a, const BookCheckpoint& b) {
    return a.instrument_id == b.instrument_id && a.venue_id == b.venue_id &&
           a.levels == b.levels && a.arrival_order == b.arrival_order &&
           a.last_sequence == b.last_sequence &&
           a.exchange_ts == b.exchange_ts && a.receive_ts == b.receive_ts &&
           a.trade_flow == b.trade_flow && a.status == b.status &&
           a.stale == b.stale && a.snapshot_active == b.snapshot_active &&
           a.snapshot_broken == b.snapshot_broken && a.counters == b.counters;
}

// MBO order book for one instrument on one venue (venue_id == 0: synthetic,
// accepts any venue).
class OrderBook {
public:
    OrderBook(std::uint32_t instrument_id, std::uint16_t venue_id);

    // Pre-size pools; safe to call any time (never shrinks).
    void reserve(std::size_t orders, std::size_t levels);

    // Apply one event (sequence-checked). Throws std::invalid_argument on
    // routing errors or an unknown event type.
    void apply(const MarketEvent& ev);

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

    std::uint32_t instrument_id() const { return instrument_id_; }
    std::uint16_t venue_id() const { return venue_id_; }
    std::uint64_t last_sequence() const { return last_sequence_; }
    std::int64_t exchange_ts() const { return exchange_ts_; }
    std::int64_t receive_ts() const { return receive_ts_; }
    std::int64_t trade_flow() const { return trade_flow_; }
    std::int64_t status() const { return status_; }
    bool stale() const { return stale_; }
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
    std::int64_t exchange_ts_ = 0;
    std::int64_t receive_ts_ = 0;
    std::int64_t trade_flow_ = 0;
    std::int64_t status_ = static_cast<std::int64_t>(SessionStatus::TRADING);
    bool stale_ = false;
    bool snapshot_active_ = false;
    bool snapshot_broken_ = false;
    BookCounters counters_;

    std::uint32_t alloc_order();
    std::uint32_t alloc_level();
    void arrival_append(std::uint32_t oi);
    void arrival_unlink(std::uint32_t oi);
    std::size_t level_pos(int side, std::int64_t price) const;
    std::uint32_t find_level(int side, std::int64_t price) const;
    void insert_order(int side, std::int64_t price, std::uint64_t order_id,
                      std::int64_t qty);
    void remove_order(std::uint32_t oi);
    std::int64_t match_marketable(int side, std::int64_t price,
                                  std::int64_t qty);
    void clear_book();

    void apply_add(const MarketEvent& ev);
    void apply_modify(const MarketEvent& ev);
    void apply_cancel(const MarketEvent& ev);
    void apply_execute(const MarketEvent& ev);
    void apply_quote(const MarketEvent& ev);
    void apply_snapshot(const MarketEvent& ev);
};

// Consolidated view over per-venue books of one instrument. Routes events by
// venue_id; merged depth sums sizes and order counts at equal prices; best is
// best across venues. Sequence/staleness remain per venue.
struct ConsolidatedCheckpoint {
    std::uint32_t instrument_id = 0;
    std::map<std::uint16_t, BookCheckpoint> venues;
};

inline bool operator==(const ConsolidatedCheckpoint& a,
                       const ConsolidatedCheckpoint& b) {
    return a.instrument_id == b.instrument_id && a.venues == b.venues;
}

class ConsolidatedBook {
public:
    explicit ConsolidatedBook(std::uint32_t instrument_id);

    OrderBook& venue_book(std::uint16_t venue_id);
    void apply(const MarketEvent& ev);

    std::optional<LevelEntry> best_bid() const;
    std::optional<LevelEntry> best_ask() const;
    std::vector<LevelEntry> depth(Side side, int levels = DEPTH_LEVELS) const;
    std::vector<LevelEntry> order_count(Side side,
                                        int levels = DEPTH_LEVELS) const;
    // Sum of per-venue cumulative signed trade flow.
    std::int64_t trade_flow() const;

    std::uint32_t instrument_id() const { return instrument_id_; }
    const std::map<std::uint16_t, OrderBook>& books() const { return books_; }

    ConsolidatedCheckpoint checkpoint() const;
    static ConsolidatedBook restore(const ConsolidatedCheckpoint& cp);

private:
    // (price, size, count) rows, best first, aggregated across venues.
    std::vector<std::array<std::int64_t, 3>> merged(Side side) const;

    std::uint32_t instrument_id_;
    std::map<std::uint16_t, OrderBook> books_;
};

}  // namespace iap
