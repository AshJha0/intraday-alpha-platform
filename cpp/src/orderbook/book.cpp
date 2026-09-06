#include "iap/orderbook/book.hpp"

#include <algorithm>
#include <limits>
#include <string>

namespace iap {

namespace {
constexpr int kBid = 0;
}

OrderBook::OrderBook(std::uint32_t instrument_id, std::uint16_t venue_id,
                     std::size_t reorder_window)
    : instrument_id_(instrument_id),
      venue_id_(venue_id),
      reorder_window_(reorder_window) {
    if (reorder_window > MAX_REORDER_WINDOW) {
        throw std::invalid_argument(
            "reorder_window must be <= " + std::to_string(MAX_REORDER_WINDOW) +
            ": " + std::to_string(reorder_window));
    }
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

ApplyStatus OrderBook::apply(const MarketEvent& ev) {
    if (ev.instrument_id != instrument_id_ ||
        (venue_id_ != 0 && ev.venue_id != venue_id_)) {
        throw std::invalid_argument(
            "event routed to wrong book: event " +
            std::to_string(ev.instrument_id) + "@" +
            std::to_string(ev.venue_id) + ", book " +
            std::to_string(instrument_id_) + "@" + std::to_string(venue_id_));
    }
    if (reorder_window_ != 0 && has_sequence_ && ev.sequence > last_sequence_ &&
        ev.sequence - last_sequence_ > 1) {
        // Out-of-sequence event ahead of a hole: hold it back until the
        // missing sequences arrive (bounded by reorder_window).
        if (pending_.count(ev.sequence) != 0) {
            ++counters_.duplicates_dropped;
            return ApplyStatus::DROPPED;
        }
        if (pending_.size() < reorder_window_) {
            pending_.emplace(ev.sequence, ev);
            return ApplyStatus::HELD;
        }
        // Buffer full: give up on the hole, declare the gap and apply
        // everything held so far in sequence order.
        pending_.emplace(ev.sequence, ev);
        return flush_pending(ev.sequence, true);
    }
    const ApplyStatus status = apply_sequenced(ev, false);
    if (!pending_.empty()) drain_pending();
    return status;
}

ApplyStatus OrderBook::flush_pending(std::uint64_t target, bool has_target) {
    std::map<std::uint64_t, MarketEvent> pending;
    pending.swap(pending_);
    ApplyStatus status = ApplyStatus::APPLIED;
    for (const auto& [seq, pev] : pending) {
        const ApplyStatus st = apply_sequenced(pev, true);
        if (has_target && seq == target) status = st;
    }
    return status;
}

void OrderBook::drain_pending() {
    while (!pending_.empty()) {
        auto it = pending_.find(last_sequence_ + 1);
        if (it == pending_.end()) return;
        MarketEvent pev = it->second;
        pending_.erase(it);
        apply_sequenced(pev, true);
    }
}

void OrderBook::reset_sequence() {
    if (!pending_.empty()) flush_pending();
    has_sequence_ = false;
    ++sequence_epoch_;
    ++counters_.sequence_resets;
    stale_ = true;
    snapshot_active_ = false;
    snapshot_broken_ = false;
    snapshot_countdown_ = 0;
}

bool OrderBook::payload_ok(const MarketEvent& ev) {
    const auto et = static_cast<EventType>(ev.event_type);
    switch (et) {
        case EventType::ADD:
            return ev.order_id != 0 && ev.qty > 0 && ev.price_ticks > 0 &&
                   ev.order_id < SYNTHETIC_ID_BASE;
        case EventType::MODIFY:
        case EventType::CANCEL:
            return ev.order_id != 0;
        case EventType::EXECUTE:
            return ev.order_id != 0 && ev.qty > 0;
        case EventType::QUOTE:
        case EventType::SNAPSHOT:
            return ev.qty > 0 && ev.price_ticks > 0 &&
                   ev.order_id < SYNTHETIC_ID_BASE;
        case EventType::TRADE:
            return ev.qty > 0 && ev.price_ticks > 0;
        case EventType::STATUS:
            return ev.qty >= 1 && ev.qty <= 4;
        case EventType::HEARTBEAT:
            return true;
    }
    return true;
}

ApplyStatus OrderBook::apply_sequenced(const MarketEvent& ev, bool from_buffer) {
    const auto et = static_cast<EventType>(ev.event_type);
    if (has_sequence_) {
        if (ev.sequence <= last_sequence_) {
            if (et == EventType::SNAPSHOT && !snapshot_active_ &&
                ev.sequence < last_sequence_) {
                // Venue sequence reset (daily restart / fail-over): the
                // SNAPSHOT burst starting the new epoch recovers the book.
                if (!pending_.empty()) flush_pending();
                ++sequence_epoch_;
                ++counters_.sequence_resets;
                stale_ = true;
            } else {
                ++counters_.duplicates_dropped;
                return ApplyStatus::DROPPED;
            }
        } else if (ev.sequence - last_sequence_ > 1) {
            ++counters_.gaps_detected;
            stale_ = true;
            if (snapshot_active_) {
                // Gap inside an active SNAPSHOT burst: the burst is broken —
                // its completion record must NOT clear `stale`.
                snapshot_broken_ = true;
            }
        } else if (!pending_.empty() && !from_buffer) {
            ++counters_.late_recovered;  // a gap filler arrived late
        }
    }
    has_sequence_ = true;
    last_sequence_ = ev.sequence;
    exchange_ts_ = ev.exchange_ts;
    receive_ts_ = ev.receive_ts;

    // Malformed-event classes: dropped + counted (never thrown), after the
    // sequence number above is consumed (pinned).
    if (ev.event_type < 1 || ev.event_type > 9) {
        ++counters_.unknown_type_dropped;
        return ApplyStatus::DROPPED;
    }
    if (ev.side > 1 &&
        (et == EventType::ADD || et == EventType::QUOTE ||
         et == EventType::SNAPSHOT || et == EventType::TRADE)) {
        ++counters_.invalid_side_dropped;
        return ApplyStatus::DROPPED;
    }
    if (!payload_ok(ev)) {
        ++counters_.invalid_payload_dropped;
        return ApplyStatus::DROPPED;
    }
    if (stale_ && et != EventType::SNAPSHOT && et != EventType::STATUS &&
        et != EventType::TRADE && et != EventType::HEARTBEAT) {
        ++counters_.dropped_while_stale;
        return ApplyStatus::DROPPED;
    }

    bool applied = true;
    switch (et) {
        case EventType::ADD:
            applied = apply_add(ev);
            break;
        case EventType::MODIFY:
            applied = apply_modify(ev);
            break;
        case EventType::CANCEL:
            applied = apply_cancel(ev);
            break;
        case EventType::EXECUTE:
            applied = apply_execute(ev);
            break;
        case EventType::TRADE: {
            std::int64_t flow = 0;
            const bool overflow =
                ev.side == static_cast<std::uint8_t>(Side::BID)
                    ? __builtin_add_overflow(trade_flow_, ev.qty, &flow)
                    : __builtin_sub_overflow(trade_flow_, ev.qty, &flow);
            if (overflow) {
                ++counters_.invalid_payload_dropped;
                return ApplyStatus::DROPPED;
            }
            trade_flow_ = flow;
            break;
        }
        case EventType::QUOTE:
            applied = apply_quote(ev);
            break;
        case EventType::SNAPSHOT:
            applied = apply_snapshot(ev);
            break;
        case EventType::STATUS:
            status_ = ev.qty;
            break;
        case EventType::HEARTBEAT:
            break;
    }
    if (!applied) return ApplyStatus::DROPPED;
    ++counters_.events_applied;
    return ApplyStatus::APPLIED;
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

std::int64_t OrderBook::level_total(int side, std::int64_t price) const {
    const std::uint32_t li = find_level(side, price);
    return li == NIL ? 0 : levels_[li].total_qty;
}

void OrderBook::clear_side(int side) {
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
    snapshot_synthetic_next_ = {0, 0};
}

// ------------------------------------------------------------- event handlers

bool OrderBook::apply_add(const MarketEvent& ev) {
    if (index_.contains(ev.order_id)) {
        ++counters_.unknown_order_events;  // duplicate order id: drop + count
        return false;
    }
    std::int64_t total = 0;
    if (__builtin_add_overflow(level_total(ev.side, ev.price_ticks), ev.qty,
                               &total)) {
        ++counters_.invalid_payload_dropped;
        return false;
    }
    std::int64_t remaining = ev.qty;
    if (status_ == static_cast<std::int64_t>(SessionStatus::TRADING)) {
        remaining = match_marketable(ev.side, ev.price_ticks, ev.qty);
    }
    if (remaining > 0) {
        insert_order(ev.side, ev.price_ticks, ev.order_id, remaining);
    }
    return true;
}

bool OrderBook::apply_modify(const MarketEvent& ev) {
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi == OrderIndex::NPOS) {
        ++counters_.unknown_order_events;
        return false;
    }
    OrderNode& o = orders_[oi];
    LevelNode& lvl = levels_[o.level];
    if (ev.price_ticks != 0 && ev.price_ticks != lvl.price) {
        ++counters_.modify_price_mismatch;  // price change must be CANCEL+ADD
        return false;
    }
    const std::int64_t old_qty = o.qty;
    const std::int64_t new_qty = ev.qty;
    if (new_qty <= 0) {
        remove_order(oi);
        return true;
    }
    if (new_qty <= old_qty) {
        // Decrease: keep queue position.
        o.qty = new_qty;
    } else {
        std::int64_t total = 0;
        if (__builtin_add_overflow(lvl.total_qty, new_qty - old_qty, &total)) {
            ++counters_.invalid_payload_dropped;
            return false;
        }
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
    return true;
}

bool OrderBook::apply_cancel(const MarketEvent& ev) {
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi == OrderIndex::NPOS) {
        ++counters_.unknown_order_events;
        return false;
    }
    remove_order(oi);
    return true;
}

bool OrderBook::apply_execute(const MarketEvent& ev) {
    const std::uint32_t oi = index_.find(ev.order_id);
    if (oi == OrderIndex::NPOS) {
        ++counters_.unknown_order_events;
        return false;
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
    return true;
}

bool OrderBook::apply_quote(const MarketEvent& ev) {
    // FX QUOTE: replace this venue's whole side at L1.
    const int side = ev.side;
    const std::uint64_t oid =
        ev.order_id != 0 ? ev.order_id : synthetic_order_id(ev.side, 0);
    const std::uint32_t resting = index_.find(oid);
    if (resting != OrderIndex::NPOS &&
        levels_[orders_[resting].level].side != side) {
        ++counters_.unknown_order_events;  // id rests on the other side
        return false;
    }
    clear_side(side);
    insert_order(side, ev.price_ticks, oid, ev.qty);
    return true;
}

bool OrderBook::apply_snapshot(const MarketEvent& ev) {
    if (snapshot_active_) {
        if (ev.trade_id >= snapshot_countdown_) {
            // Countdown went up (or repeated): the previous burst was
            // interrupted and this record starts a new burst.
            snapshot_active_ = false;
            ++counters_.snapshot_restarts;
        } else if (ev.trade_id != snapshot_countdown_ - 1) {
            // Countdown skipped ahead: records missing — burst broken.
            snapshot_broken_ = true;
        }
    }
    if (!snapshot_active_) {
        // Burst start: clear the whole book state (levels + orders).
        clear_book();
        snapshot_active_ = true;
        snapshot_broken_ = false;
    }
    snapshot_countdown_ = ev.trade_id;
    std::uint64_t oid = ev.order_id;
    if (oid == 0) {
        const std::uint64_t ordinal = snapshot_synthetic_next_[ev.side]++;
        oid = synthetic_order_id(ev.side, ordinal);
    }
    bool ok = true;
    std::int64_t total = 0;
    if (index_.contains(oid)) {
        ++counters_.unknown_order_events;  // repeated id inside a burst
        ok = false;
    } else if (__builtin_add_overflow(level_total(ev.side, ev.price_ticks),
                                      ev.qty, &total)) {
        ++counters_.invalid_payload_dropped;
        ok = false;
    } else {
        insert_order(ev.side, ev.price_ticks, oid, ev.qty);
    }
    if (ev.trade_id == 0) {  // last record of the burst
        snapshot_active_ = false;
        if (!snapshot_broken_) stale_ = false;
        snapshot_broken_ = false;
    }
    return ok;
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

bool OrderBook::is_crossed() const {
    const auto bb = best_bid();
    const auto ba = best_ask();
    return bb && ba && bb->first > ba->first;
}

bool OrderBook::is_locked() const {
    const auto bb = best_bid();
    const auto ba = best_ask();
    return bb && ba && bb->first == ba->first;
}

bool OrderBook::is_fresh(std::int64_t now_ns, std::int64_t max_age_ns) const {
    return !stale_ && has_sequence_ && now_ns - receive_ts_ <= max_age_ns;
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
    cp.has_sequence = has_sequence_;
    cp.sequence_epoch = sequence_epoch_;
    cp.exchange_ts = exchange_ts_;
    cp.receive_ts = receive_ts_;
    cp.trade_flow = trade_flow_;
    cp.status = status_;
    cp.stale = stale_;
    cp.snapshot_active = snapshot_active_;
    cp.snapshot_broken = snapshot_broken_;
    cp.snapshot_countdown = snapshot_countdown_;
    cp.snapshot_synthetic_next = snapshot_synthetic_next_;
    cp.reorder_window = reorder_window_;
    cp.reorder_pending.reserve(pending_.size());
    for (const auto& [seq, pev] : pending_) {
        (void)seq;
        cp.reorder_pending.push_back(pev);
    }
    cp.counters = counters_;
    return cp;
}

OrderBook OrderBook::restore(const BookCheckpoint& cp) {
    if (cp.reorder_window > MAX_REORDER_WINDOW) {
        throw std::invalid_argument("checkpoint reorder_window out of range");
    }
    OrderBook book(cp.instrument_id, cp.venue_id,
                   static_cast<std::size_t>(cp.reorder_window));
    for (const auto& lvl : cp.levels) {
        if (lvl.side > 1) {
            throw std::invalid_argument("invalid side in checkpoint level");
        }
        for (const auto& [oid, qty] : lvl.orders) {
            if (book.index_.contains(oid)) {
                throw std::invalid_argument(
                    "duplicate order_id " + std::to_string(oid) +
                    " in checkpoint");
            }
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
    book.has_sequence_ = cp.has_sequence;
    book.sequence_epoch_ = cp.sequence_epoch;
    book.exchange_ts_ = cp.exchange_ts;
    book.receive_ts_ = cp.receive_ts;
    book.trade_flow_ = cp.trade_flow;
    book.status_ = cp.status;
    book.stale_ = cp.stale;
    book.snapshot_active_ = cp.snapshot_active;
    book.snapshot_broken_ = cp.snapshot_broken;
    book.snapshot_countdown_ = cp.snapshot_countdown;
    book.snapshot_synthetic_next_ = cp.snapshot_synthetic_next;
    if (cp.reorder_pending.size() > book.reorder_window_) {
        throw std::invalid_argument(
            "checkpoint reorder_pending exceeds reorder_window");
    }
    for (const auto& pev : cp.reorder_pending) {
        if (!book.pending_.emplace(pev.sequence, pev).second) {
            throw std::invalid_argument(
                "duplicate pending sequence in checkpoint");
        }
    }
    book.counters_ = cp.counters;
    return book;
}

// ----------------------------------------------------------- ConsolidatedBook

ConsolidatedBook::ConsolidatedBook(std::uint32_t instrument_id,
                                   std::size_t reorder_window)
    : instrument_id_(instrument_id), reorder_window_(reorder_window) {
    if (reorder_window > MAX_REORDER_WINDOW) {
        throw std::invalid_argument("reorder_window out of range");
    }
}

OrderBook& ConsolidatedBook::venue_book(std::uint16_t venue_id) {
    auto it = books_.find(venue_id);
    if (it == books_.end()) {
        it = books_
                 .emplace(venue_id,
                          OrderBook(instrument_id_, venue_id, reorder_window_))
                 .first;
    }
    return it->second;
}

ApplyStatus ConsolidatedBook::apply(const MarketEvent& ev) {
    return venue_book(ev.venue_id).apply(ev);
}

void ConsolidatedBook::reset_sequences() {
    for (auto& [vid, book] : books_) {
        (void)vid;
        book.reset_sequence();
    }
}

bool ConsolidatedBook::is_crossed() const {
    const auto bb = best_bid();
    const auto ba = best_ask();
    return bb && ba && bb->first > ba->first;
}

bool ConsolidatedBook::is_locked() const {
    const auto bb = best_bid();
    const auto ba = best_ask();
    return bb && ba && bb->first == ba->first;
}

std::vector<std::uint16_t> ConsolidatedBook::active_venues() const {
    std::vector<std::uint16_t> out;
    for (const auto& [vid, book] : books_) {
        if (!book.stale()) out.push_back(vid);
    }
    return out;
}

std::vector<std::uint16_t> ConsolidatedBook::stale_venues() const {
    std::vector<std::uint16_t> out;
    for (const auto& [vid, book] : books_) {
        if (book.stale()) out.push_back(vid);
    }
    return out;
}

std::optional<std::int64_t> ConsolidatedBook::venue_status(
    std::uint16_t venue_id) const {
    auto it = books_.find(venue_id);
    if (it == books_.end()) return std::nullopt;
    return it->second.status();
}

std::vector<std::array<std::int64_t, 3>> ConsolidatedBook::merged(
    Side side) const {
    // Aggregate (price -> size, count) across venues in sorted venue order,
    // then emit best-first.
    std::map<std::int64_t, std::pair<std::int64_t, std::int64_t>> agg;
    for (const auto& [vid, book] : books_) {
        (void)vid;
        if (book.stale()) continue;  // non-stale venues only (pinned)
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
    // Saturating sum (pinned): per-venue flows are i64, the aggregate may not be.
    __int128 total = 0;
    for (const auto& [vid, book] : books_) {
        (void)vid;
        total += book.trade_flow();
    }
    const __int128 lo = std::numeric_limits<std::int64_t>::min();
    const __int128 hi = std::numeric_limits<std::int64_t>::max();
    if (total < lo) return std::numeric_limits<std::int64_t>::min();
    if (total > hi) return std::numeric_limits<std::int64_t>::max();
    return static_cast<std::int64_t>(total);
}

ConsolidatedCheckpoint ConsolidatedBook::checkpoint() const {
    ConsolidatedCheckpoint cp;
    cp.instrument_id = instrument_id_;
    cp.reorder_window = reorder_window_;
    for (const auto& [vid, book] : books_) {
        cp.venues.emplace(vid, book.checkpoint());
    }
    return cp;
}

ConsolidatedBook ConsolidatedBook::restore(const ConsolidatedCheckpoint& cp) {
    if (cp.reorder_window > MAX_REORDER_WINDOW) {
        throw std::invalid_argument("checkpoint reorder_window out of range");
    }
    ConsolidatedBook cons(cp.instrument_id,
                          static_cast<std::size_t>(cp.reorder_window));
    for (const auto& [vid, bcp] : cp.venues) {
        cons.books_.emplace(vid, OrderBook::restore(bcp));
    }
    return cons;
}

}  // namespace iap
