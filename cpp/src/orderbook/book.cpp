#include "iap/orderbook/book.hpp"

#include <algorithm>
#include <string>

namespace iap {

namespace {
constexpr int kBid = 0;
}

OrderBook::OrderBook(std::uint32_t instrument_id, std::uint16_t venue_id)
    : instrument_id_(instrument_id), venue_id_(venue_id) {
    reserve(1024, 256);
}

void OrderBook::reserve(std::size_t orders, std::size_t levels) {
    orders_.reserve(orders);
    free_orders_.reserve(orders);
    levels_.reserve(levels);
    free_levels_.reserve(levels);
    side_levels_[0].reserve(levels);
    side_levels_[1].reserve(levels);
    index_.reserve(orders);
}

// ---------------------------------------------------------------- application

void OrderBook::apply(const MarketEvent& ev) {
    if (ev.instrument_id != instrument_id_ ||
        (venue_id_ != 0 && ev.venue_id != venue_id_)) {
        throw std::invalid_argument(
            "event routed to wrong book: event " +
            std::to_string(ev.instrument_id) + "@" +
            std::to_string(ev.venue_id) + ", book " +
            std::to_string(instrument_id_) + "@" + std::to_string(venue_id_));
    }
    // Sequence handling (duplicates dropped, gaps => stale).
    if (ev.sequence <= last_sequence_) {
        ++counters_.duplicates_dropped;
        return;
    }
    if (ev.sequence > last_sequence_ + 1 && last_sequence_ != 0) {
        ++counters_.gaps_detected;
        stale_ = true;
        if (snapshot_active_) {
            // Gap inside an active SNAPSHOT burst: the burst is broken —
            // its completion record must NOT clear `stale` (records are
            // missing). Only a later complete gap-free burst recovers.
            snapshot_broken_ = true;
        }
    }
    last_sequence_ = ev.sequence;
    exchange_ts_ = ev.exchange_ts;
    receive_ts_ = ev.receive_ts;

    const auto et = static_cast<EventType>(ev.event_type);
    // Side-domain validation for side-indexed event types: malformed side
    // => dropped + counted (never raised mid-stream), same path as other
    // malformed events; the sequence number above is consumed (pinned).
    if (ev.side > 1 &&
        (et == EventType::ADD || et == EventType::QUOTE ||
         et == EventType::SNAPSHOT || et == EventType::TRADE)) {
        ++counters_.invalid_side_dropped;
        return;
    }
    if (stale_ && et != EventType::SNAPSHOT && et != EventType::STATUS &&
        et != EventType::TRADE && et != EventType::HEARTBEAT) {
        ++counters_.dropped_while_stale;
        return;
    }

    switch (et) {
        case EventType::ADD:
            apply_add(ev);
            break;
        case EventType::MODIFY:
            apply_modify(ev);
            break;
        case EventType::CANCEL:
            apply_cancel(ev);
            break;
        case EventType::EXECUTE:
            apply_execute(ev);
            break;
        case EventType::TRADE:
            trade_flow_ += (ev.side == static_cast<std::uint8_t>(Side::BID))
                               ? ev.qty
                               : -ev.qty;
            break;
        case EventType::QUOTE:
            apply_quote(ev);
            break;
        case EventType::SNAPSHOT:
            apply_snapshot(ev);
            break;
        case EventType::STATUS:
            status_ = ev.qty;
            break;
        case EventType::HEARTBEAT:
            break;
        default:
            throw std::invalid_argument(
                "unknown event_type " + std::to_string(ev.event_type) +
                " (event_id=" + std::to_string(ev.event_id) + ")");
    }
    ++counters_.events_applied;
}

// ----------------------------------------------------------------- primitives

std::uint32_t OrderBook::alloc_order() {
    if (!free_orders_.empty()) {
        std::uint32_t oi = free_orders_.back();
        free_orders_.pop_back();
        return oi;
    }
    orders_.emplace_back();
    return static_cast<std::uint32_t>(orders_.size() - 1);
}

std::uint32_t OrderBook::alloc_level() {
    if (!free_levels_.empty()) {
        std::uint32_t li = free_levels_.back();
        free_levels_.pop_back();
        return li;
    }
    levels_.emplace_back();
    return static_cast<std::uint32_t>(levels_.size() - 1);
}

void OrderBook::arrival_append(std::uint32_t oi) {
    OrderNode& o = orders_[oi];
    o.arr_prev = arrival_tail_;
    o.arr_next = NIL;
    if (arrival_tail_ != NIL) {
        orders_[arrival_tail_].arr_next = oi;
    } else {
        arrival_head_ = oi;
    }
    arrival_tail_ = oi;
}

void OrderBook::arrival_unlink(std::uint32_t oi) {
    OrderNode& o = orders_[oi];
    if (o.arr_prev != NIL) {
        orders_[o.arr_prev].arr_next = o.arr_next;
    } else {
        arrival_head_ = o.arr_next;
    }
    if (o.arr_next != NIL) {
        orders_[o.arr_next].arr_prev = o.arr_prev;
    } else {
        arrival_tail_ = o.arr_prev;
    }
}

// Position of `price` in side_levels_[side] (insertion point if absent).
std::size_t OrderBook::level_pos(int side, std::int64_t price) const {
    const auto& v = side_levels_[side];
    if (side == kBid) {  // sorted by price descending (best first)
        auto it = std::lower_bound(
            v.begin(), v.end(), price,
            [this](std::uint32_t li, std::int64_t p) {
                return levels_[li].price > p;
            });
        return static_cast<std::size_t>(it - v.begin());
    }
    auto it = std::lower_bound(v.begin(), v.end(), price,
                               [this](std::uint32_t li, std::int64_t p) {
                                   return levels_[li].price < p;
                               });
    return static_cast<std::size_t>(it - v.begin());
}

std::uint32_t OrderBook::find_level(int side, std::int64_t price) const {
    const auto& v = side_levels_[side];
    std::size_t pos = level_pos(side, price);
    if (pos < v.size() && levels_[v[pos]].price == price) return v[pos];
    return NIL;
}

void OrderBook::insert_order(int side, std::int64_t price,
                             std::uint64_t order_id, std::int64_t qty) {
    std::uint32_t li = find_level(side, price);
    if (li == NIL) {
        li = alloc_level();
        levels_[li] =
            LevelNode{price, 0, NIL, NIL, 0, static_cast<std::uint8_t>(side)};
        auto& v = side_levels_[side];
        v.insert(v.begin() + static_cast<std::ptrdiff_t>(level_pos(side, price)),
                 li);
    }
    std::uint32_t oi = alloc_order();
    LevelNode& lvl = levels_[li];
    orders_[oi] = OrderNode{order_id, qty, lvl.tail, NIL, li};
    if (lvl.tail != NIL) {
        orders_[lvl.tail].next = oi;
    } else {
        lvl.head = oi;
    }
    lvl.tail = oi;
    lvl.total_qty += qty;
    ++lvl.count;
    index_.upsert(order_id, oi);
    arrival_append(oi);
}

void OrderBook::remove_order(std::uint32_t oi) {
    arrival_unlink(oi);
    OrderNode& o = orders_[oi];
    LevelNode& lvl = levels_[o.level];
    if (o.prev != NIL) {
        orders_[o.prev].next = o.next;
    } else {
        lvl.head = o.next;
    }
    if (o.next != NIL) {
        orders_[o.next].prev = o.prev;
    } else {
        lvl.tail = o.prev;
    }
    lvl.total_qty -= o.qty;
    --lvl.count;
    index_.erase(o.id);
    if (lvl.count == 0) {
        const int side = lvl.side;
        auto& v = side_levels_[side];
        v.erase(v.begin() +
                static_cast<std::ptrdiff_t>(level_pos(side, lvl.price)));
        free_levels_.push_back(o.level);
    }
    free_orders_.push_back(oi);
}

std::int64_t OrderBook::match_marketable(int side, std::int64_t price,
                                         std::int64_t qty) {
    const int opp = side == kBid ? 1 : 0;
    while (qty > 0) {
        if (side_levels_[opp].empty()) break;
        const std::uint32_t li = side_levels_[opp].front();
        const std::int64_t best_price = levels_[li].price;
        const bool crosses =
            side == kBid ? price >= best_price : price <= best_price;
        if (!crosses) break;
        // Fill from the FIFO head of the best opposite level.
        const std::uint32_t head = levels_[li].head;
        OrderNode& o = orders_[head];
        const std::int64_t fill = std::min(qty, o.qty);
        qty -= fill;
        if (fill == o.qty) {
            remove_order(head);
        } else {
            o.qty -= fill;
            levels_[li].total_qty -= fill;
        }
    }
    return qty;
}

void OrderBook::clear_book() {
    orders_.clear();
    free_orders_.clear();
    levels_.clear();
    free_levels_.clear();
    side_levels_[0].clear();
    side_levels_[1].clear();
    index_.clear();
    arrival_head_ = NIL;
    arrival_tail_ = NIL;
}

// ------------------------------------------------------------- event handlers

void OrderBook::apply_add(const MarketEvent& ev) {
    if (index_.contains(ev.order_id)) {
        ++counters_.unknown_order_events;  // duplicate order id: drop + count
        return;
    }
    std::int64_t remaining = match_marketable(ev.side, ev.price_ticks, ev.qty);
    if (remaining > 0) {
        insert_order(ev.side, ev.price_ticks, ev.order_id, remaining);
    }
}

void OrderBook::apply_modify(const MarketEvent& ev) {
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi == OrderIndex::NPOS) {
        ++counters_.unknown_order_events;
        return;
    }
    OrderNode& o = orders_[oi];
    LevelNode& lvl = levels_[o.level];
    const std::int64_t old_qty = o.qty;
    const std::int64_t new_qty = ev.qty;
    if (new_qty <= 0) {
        remove_order(oi);
        return;
    }
    if (new_qty <= old_qty) {
        // Decrease: keep queue position.
        o.qty = new_qty;
    } else {
        // Increase: move to the tail of the level.
        if (lvl.tail != oi) {
            if (o.prev != NIL) {
                orders_[o.prev].next = o.next;
            } else {
                lvl.head = o.next;
            }
            orders_[o.next].prev = o.prev;  // o.next != NIL since not tail
            o.prev = lvl.tail;
            o.next = NIL;
            orders_[lvl.tail].next = oi;
            lvl.tail = oi;
        }
        o.qty = new_qty;
    }
    lvl.total_qty += new_qty - old_qty;
}

void OrderBook::apply_cancel(const MarketEvent& ev) {
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi == OrderIndex::NPOS) {
        ++counters_.unknown_order_events;
        return;
    }
    remove_order(oi);
}

void OrderBook::apply_execute(const MarketEvent& ev) {
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi == OrderIndex::NPOS) {
        ++counters_.unknown_order_events;
        return;
    }
    OrderNode& o = orders_[oi];
    const std::int64_t old_qty = o.qty;
    const std::int64_t fill = std::min(ev.qty, old_qty);
    if (fill >= old_qty) {
        remove_order(oi);
    } else {
        o.qty = old_qty - fill;
        levels_[o.level].total_qty -= fill;
    }
}

void OrderBook::apply_quote(const MarketEvent& ev) {
    // FX QUOTE: replace this venue's whole side at L1.
    const int side = ev.side;
    for (std::uint32_t li : side_levels_[side]) {
        std::uint32_t oi = levels_[li].head;
        while (oi != NIL) {
            const std::uint32_t next = orders_[oi].next;
            arrival_unlink(oi);
            index_.erase(orders_[oi].id);
            free_orders_.push_back(oi);
            oi = next;
        }
        free_levels_.push_back(li);
    }
    side_levels_[side].clear();
    insert_order(side, ev.price_ticks, ev.order_id, ev.qty);
}

void OrderBook::apply_snapshot(const MarketEvent& ev) {
    if (!snapshot_active_) {
        // Burst start: clear the whole book state (levels + orders).
        clear_book();
        snapshot_active_ = true;
        snapshot_broken_ = false;
    }
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi != OrderIndex::NPOS) remove_order(oi);
    insert_order(ev.side, ev.price_ticks, ev.order_id, ev.qty);
    if (ev.trade_id == 0) {  // last record of the burst
        snapshot_active_ = false;
        if (!snapshot_broken_) stale_ = false;
        snapshot_broken_ = false;
    }
}

// -------------------------------------------------------------- derived state

std::optional<LevelEntry> OrderBook::best_bid() const {
    const auto& v = side_levels_[0];
    if (v.empty()) return std::nullopt;
    const LevelNode& lvl = levels_[v.front()];
    return LevelEntry{lvl.price, lvl.total_qty};
}

std::optional<LevelEntry> OrderBook::best_ask() const {
    const auto& v = side_levels_[1];
    if (v.empty()) return std::nullopt;
    const LevelNode& lvl = levels_[v.front()];
    return LevelEntry{lvl.price, lvl.total_qty};
}

std::vector<LevelEntry> OrderBook::depth(Side side, int levels) const {
    const auto& v = side_levels_[static_cast<int>(side)];
    std::vector<LevelEntry> out;
    const std::size_t n = std::min<std::size_t>(
        v.size(), levels < 0 ? 0 : static_cast<std::size_t>(levels));
    out.reserve(n);
    for (std::size_t i = 0; i < n; ++i) {
        out.emplace_back(levels_[v[i]].price, levels_[v[i]].total_qty);
    }
    return out;
}

void OrderBook::depth_into(Side side, int levels,
                           std::vector<LevelEntry>& out) const {
    const auto& v = side_levels_[static_cast<int>(side)];
    out.clear();
    const std::size_t n = std::min<std::size_t>(
        v.size(), levels < 0 ? 0 : static_cast<std::size_t>(levels));
    for (std::size_t i = 0; i < n; ++i) {
        out.emplace_back(levels_[v[i]].price, levels_[v[i]].total_qty);
    }
}

std::int64_t OrderBook::level_qty(Side side, std::int64_t price_ticks) const {
    const std::uint32_t li = find_level(static_cast<int>(side), price_ticks);
    return li == NIL ? 0 : levels_[li].total_qty;
}

std::vector<LevelEntry> OrderBook::order_count(Side side, int levels) const {
    const auto& v = side_levels_[static_cast<int>(side)];
    std::vector<LevelEntry> out;
    const std::size_t n = std::min<std::size_t>(
        v.size(), levels < 0 ? 0 : static_cast<std::size_t>(levels));
    out.reserve(n);
    for (std::size_t i = 0; i < n; ++i) {
        out.emplace_back(levels_[v[i]].price,
                         static_cast<std::int64_t>(levels_[v[i]].count));
    }
    return out;
}

std::vector<RestingOrder> OrderBook::resting_orders() const {
    std::vector<RestingOrder> out;
    out.reserve(index_.size());
    for (std::uint32_t oi = arrival_head_; oi != NIL;
         oi = orders_[oi].arr_next) {
        const OrderNode& o = orders_[oi];
        const LevelNode& lvl = levels_[o.level];
        out.push_back(RestingOrder{o.id, lvl.side, lvl.price, o.qty});
    }
    return out;
}

BookStateSummary OrderBook::state_summary() const {
    BookStateSummary s;
    if (auto bb = best_bid()) {
        s.best_bid_ticks = bb->first;
        s.best_bid_size = bb->second;
    }
    if (auto ba = best_ask()) {
        s.best_ask_ticks = ba->first;
        s.best_ask_size = ba->second;
    }
    s.depth_bid_top5 = depth(Side::BID, 5);
    s.depth_ask_top5 = depth(Side::ASK, 5);
    s.order_count_bid_top3 = order_count(Side::BID, 3);
    s.order_count_ask_top3 = order_count(Side::ASK, 3);
    s.trade_flow = trade_flow_;
    s.sequence = last_sequence_;
    return s;
}

// --------------------------------------------------------------- checkpoints

BookCheckpoint OrderBook::checkpoint() const {
    BookCheckpoint cp;
    cp.instrument_id = instrument_id_;
    cp.venue_id = venue_id_;
    // Levels in sorted (side, price) ascending order — bids ascending price
    // (reverse of best-first storage), then asks ascending.
    for (int side = 0; side < 2; ++side) {
        const auto& v = side_levels_[side];
        const std::size_t n = v.size();
        for (std::size_t k = 0; k < n; ++k) {
            const std::uint32_t li = side == 0 ? v[n - 1 - k] : v[k];
            const LevelNode& lvl = levels_[li];
            LevelCheckpoint lc;
            lc.side = static_cast<std::uint8_t>(side);
            lc.price_ticks = lvl.price;
            lc.orders.reserve(lvl.count);
            for (std::uint32_t oi = lvl.head; oi != NIL; oi = orders_[oi].next) {
                lc.orders.emplace_back(orders_[oi].id, orders_[oi].qty);
            }
            cp.levels.push_back(std::move(lc));
        }
    }
    cp.arrival_order.reserve(index_.size());
    for (std::uint32_t oi = arrival_head_; oi != NIL;
         oi = orders_[oi].arr_next) {
        cp.arrival_order.push_back(orders_[oi].id);
    }
    cp.last_sequence = last_sequence_;
    cp.exchange_ts = exchange_ts_;
    cp.receive_ts = receive_ts_;
    cp.trade_flow = trade_flow_;
    cp.status = status_;
    cp.stale = stale_;
    cp.snapshot_active = snapshot_active_;
    cp.snapshot_broken = snapshot_broken_;
    cp.counters = counters_;
    return cp;
}

OrderBook OrderBook::restore(const BookCheckpoint& cp) {
    OrderBook book(cp.instrument_id, cp.venue_id);
    for (const auto& lvl : cp.levels) {
        for (const auto& [oid, qty] : lvl.orders) {
            book.insert_order(lvl.side, lvl.price_ticks, oid, qty);
        }
    }
    // Rebuild the global arrival order (levels above fixed the per-level
    // FIFO order; the arrival list must iterate in original insertion order
    // so resting_orders() is checkpoint-round-trip exact).
    if (cp.arrival_order.size() != book.index_.size()) {
        throw std::invalid_argument(
            "checkpoint arrival_order inconsistent with levels");
    }
    book.arrival_head_ = NIL;
    book.arrival_tail_ = NIL;
    std::vector<bool> seen(book.orders_.size(), false);
    for (const std::uint64_t oid : cp.arrival_order) {
        const std::uint32_t oi = book.index_.find(oid);
        if (oi == OrderIndex::NPOS || seen[oi]) {
            throw std::invalid_argument(
                "checkpoint arrival_order inconsistent with levels");
        }
        seen[oi] = true;
        book.arrival_append(oi);
    }
    book.last_sequence_ = cp.last_sequence;
    book.exchange_ts_ = cp.exchange_ts;
    book.receive_ts_ = cp.receive_ts;
    book.trade_flow_ = cp.trade_flow;
    book.status_ = cp.status;
    book.stale_ = cp.stale;
    book.snapshot_active_ = cp.snapshot_active;
    book.snapshot_broken_ = cp.snapshot_broken;
    book.counters_ = cp.counters;
    return book;
}

// ----------------------------------------------------------- ConsolidatedBook

ConsolidatedBook::ConsolidatedBook(std::uint32_t instrument_id)
    : instrument_id_(instrument_id) {}

OrderBook& ConsolidatedBook::venue_book(std::uint16_t venue_id) {
    auto it = books_.find(venue_id);
    if (it == books_.end()) {
        it = books_.emplace(venue_id, OrderBook(instrument_id_, venue_id))
                 .first;
    }
    return it->second;
}

void ConsolidatedBook::apply(const MarketEvent& ev) {
    venue_book(ev.venue_id).apply(ev);
}

std::vector<std::array<std::int64_t, 3>> ConsolidatedBook::merged(
    Side side) const {
    // Aggregate (price -> size, count) across venues in sorted venue order,
    // then emit best-first.
    std::map<std::int64_t, std::pair<std::int64_t, std::int64_t>> agg;
    for (const auto& [vid, book] : books_) {
        (void)vid;
        const auto& v = book.side_levels_[static_cast<int>(side)];
        for (std::uint32_t li : v) {
            auto& slot = agg[book.levels_[li].price];
            slot.first += book.levels_[li].total_qty;
            slot.second += static_cast<std::int64_t>(book.levels_[li].count);
        }
    }
    std::vector<std::array<std::int64_t, 3>> out;
    out.reserve(agg.size());
    if (side == Side::BID) {
        for (auto it = agg.rbegin(); it != agg.rend(); ++it) {
            out.push_back({it->first, it->second.first, it->second.second});
        }
    } else {
        for (const auto& [price, sc] : agg) {
            out.push_back({price, sc.first, sc.second});
        }
    }
    return out;
}

std::optional<LevelEntry> ConsolidatedBook::best_bid() const {
    auto m = merged(Side::BID);
    if (m.empty()) return std::nullopt;
    return LevelEntry{m[0][0], m[0][1]};
}

std::optional<LevelEntry> ConsolidatedBook::best_ask() const {
    auto m = merged(Side::ASK);
    if (m.empty()) return std::nullopt;
    return LevelEntry{m[0][0], m[0][1]};
}

std::vector<LevelEntry> ConsolidatedBook::depth(Side side, int levels) const {
    auto m = merged(side);
    std::vector<LevelEntry> out;
    const std::size_t n = std::min<std::size_t>(
        m.size(), levels < 0 ? 0 : static_cast<std::size_t>(levels));
    out.reserve(n);
    for (std::size_t i = 0; i < n; ++i) out.emplace_back(m[i][0], m[i][1]);
    return out;
}

std::vector<LevelEntry> ConsolidatedBook::order_count(Side side,
                                                      int levels) const {
    auto m = merged(side);
    std::vector<LevelEntry> out;
    const std::size_t n = std::min<std::size_t>(
        m.size(), levels < 0 ? 0 : static_cast<std::size_t>(levels));
    out.reserve(n);
    for (std::size_t i = 0; i < n; ++i) out.emplace_back(m[i][0], m[i][2]);
    return out;
}

std::int64_t ConsolidatedBook::trade_flow() const {
    std::int64_t total = 0;
    for (const auto& [vid, book] : books_) {
        (void)vid;
        total += book.trade_flow();
    }
    return total;
}

ConsolidatedCheckpoint ConsolidatedBook::checkpoint() const {
    ConsolidatedCheckpoint cp;
    cp.instrument_id = instrument_id_;
    for (const auto& [vid, book] : books_) {
        cp.venues.emplace(vid, book.checkpoint());
    }
    return cp;
}

ConsolidatedBook ConsolidatedBook::restore(const ConsolidatedCheckpoint& cp) {
    ConsolidatedBook cons(cp.instrument_id);
    for (const auto& [vid, bcp] : cp.venues) {
        cons.books_.emplace(vid, OrderBook::restore(bcp));
    }
    return cons;
}

}  // namespace iap
