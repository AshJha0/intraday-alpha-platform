// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"  // GCC 12: initializer-list Array{...} false positive
#endif

#include "iap/execution/queue_depletion.hpp"

namespace iap {

std::string queue_bucket(std::int64_t entry_ahead_qty,
                         const std::vector<std::int64_t>& edges) {
    std::int64_t lo = 0;
    for (const std::int64_t e : edges) {
        if (entry_ahead_qty <= e) {
            return e == lo ? std::to_string(e)
                           : std::to_string(lo) + "-" + std::to_string(e);
        }
        lo = e + 1;
    }
    return ">" + std::to_string(edges.back());
}

QueueDepletionTable QueueDepletionTable::from_value(const contracts::Value& doc) {
    QueueDepletionTable t;
    if (doc.has("queue_edges") && !doc.at("queue_edges").is_null()) {
        t.edges_.clear();
        for (const auto& e : doc.at("queue_edges").as_array()) {
            t.edges_.push_back(e.as_int64());
        }
        if (t.edges_.empty()) t.edges_ = default_queue_edges();
    }
    if (!doc.has("queue_depletion") || doc.at("queue_depletion").is_null()) return t;
    const auto& qd = doc.at("queue_depletion").as_object();
    auto read_side = [&](const char* name, std::map<std::string, Cell>& out) {
        auto it = qd.find(name);
        if (it == qd.end() || it->second.is_null()) return;
        for (const auto& [bucket, cell] : it->second.as_object()) {
            Cell c;
            if (!cell.is_null()) {
                for (const auto& [k, v] : cell.as_object()) {
                    if (v.is_null()) continue;  // a null rate is "unknown"
                    if (v.is_integer() || v.is_double()) c[k] = v.as_number();
                }
            }
            out[bucket] = std::move(c);
        }
    };
    read_side("bid", t.bid_);
    read_side("ask", t.ask_);
    return t;
}

std::optional<double> QueueDepletionTable::rate(std::uint8_t side,
                                                std::int64_t queue_qty,
                                                const char* key) const {
    const auto& cells = side == 0 ? bid_ : ask_;
    auto it = cells.find(queue_bucket(queue_qty, edges_));
    // Python: `cells.get(bucket) or None` — an EMPTY cell is None too.
    if (it == cells.end() || it->second.empty()) return std::nullopt;
    auto r = it->second.find(key);
    if (r == it->second.end()) return std::nullopt;
    return r->second;
}

}  // namespace iap
