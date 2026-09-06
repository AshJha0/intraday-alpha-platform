#include "iap/replay/replay.hpp"

#include <limits>
#include <stdexcept>

#include "iap/util/json.hpp"

namespace iap {

ReplayEngine::ReplayEngine(std::uint64_t checkpoint_every,
                           std::uint64_t snapshot_every,
                           std::size_t keep_checkpoints,
                           std::size_t keep_snapshots,
                           std::size_t reorder_window,
                           std::optional<Universe> universe)
    : checkpoint_every_(checkpoint_every),
      snapshot_every_(snapshot_every),
      keep_checkpoints_(keep_checkpoints),
      keep_snapshots_(keep_snapshots),
      reorder_window_(reorder_window),
      universe_(std::move(universe)) {
    if (reorder_window > MAX_REORDER_WINDOW) {
        throw std::invalid_argument("reorder_window out of range");
    }
}

ConsolidatedBook& ReplayEngine::instrument_book(std::uint32_t instrument_id) {
    auto it = books_.find(instrument_id);
    if (it == books_.end()) {
        it = books_
                 .emplace(instrument_id,
                          ConsolidatedBook(instrument_id, reorder_window_))
                 .first;
    }
    return it->second;
}

void ReplayEngine::apply(const MarketEvent& ev) {
    if (ev.exchange_ts < last_exchange_ts_) ++time_regressions_;
    last_exchange_ts_ = ev.exchange_ts;
    ++events_processed_;
    if (universe_) {
        auto it = universe_->find(ev.instrument_id);
        if (it == universe_->end()) {
            ++unknown_instrument_dropped_;
            return;
        }
        if (it->second.count(ev.venue_id) == 0) {
            ++unknown_venue_dropped_;
            return;
        }
    }
    instrument_book(ev.instrument_id).apply(ev);
}

void ReplayEngine::reset_sequences() {
    for (auto& [iid, cons] : books_) {
        (void)iid;
        cons.reset_sequences();
    }
}

ReplayRunSummary ReplayEngine::run(const std::vector<MarketEvent>& events,
                                   const SnapshotCallback& on_snapshot) {
    for (const auto& ev : events) {
        apply(ev);
        if (snapshot_every_ != 0 &&
            events_processed_ % snapshot_every_ == 0) {
            ReplaySnapshot snap;
            snap.index = events_processed_;
            snap.states = book_states();
            ++snapshots_emitted_;
            if (on_snapshot) on_snapshot(events_processed_, snap);
            snapshots_.push_back(std::move(snap));
            if (snapshots_.size() > keep_snapshots_) {
                snapshots_.erase(snapshots_.begin());
            }
        }
        if (checkpoint_every_ != 0 &&
            events_processed_ % checkpoint_every_ == 0) {
            checkpoints_.push_back(checkpoint());
            if (checkpoints_.size() > keep_checkpoints_) {
                checkpoints_.erase(checkpoints_.begin());
            }
        }
    }
    ReplayRunSummary summary;
    summary.events_processed = events_processed_;
    summary.instruments = books_.size();
    summary.time_regressions = time_regressions_;
    summary.snapshots = snapshots_emitted_;
    summary.unknown_instrument_dropped = unknown_instrument_dropped_;
    summary.unknown_venue_dropped = unknown_venue_dropped_;
    return summary;
}

BookStates ReplayEngine::book_states() const {
    BookStates out;
    for (const auto& [iid, cons] : books_) {
        auto& venues = out[iid];
        for (const auto& [vid, book] : cons.books()) {
            venues.emplace(vid, book.state_summary());
        }
    }
    return out;
}

ReplayCheckpoint ReplayEngine::checkpoint() const {
    ReplayCheckpoint cp;
    cp.events_processed = events_processed_;
    cp.last_exchange_ts = last_exchange_ts_;
    cp.time_regressions = time_regressions_;
    cp.unknown_instrument_dropped = unknown_instrument_dropped_;
    cp.unknown_venue_dropped = unknown_venue_dropped_;
    cp.checkpoint_every = checkpoint_every_;
    cp.snapshot_every = snapshot_every_;
    cp.keep_checkpoints = keep_checkpoints_;
    cp.keep_snapshots = keep_snapshots_;
    cp.snapshots_emitted = snapshots_emitted_;
    cp.reorder_window = reorder_window_;
    cp.universe = universe_;
    for (const auto& [iid, cons] : books_) {
        cp.books.emplace(iid, cons.checkpoint());
    }
    return cp;
}

ReplayEngine ReplayEngine::restore(const ReplayCheckpoint& cp) {
    if (cp.reorder_window > MAX_REORDER_WINDOW) {
        throw std::invalid_argument("checkpoint reorder_window out of range");
    }
    ReplayEngine engine(cp.checkpoint_every, cp.snapshot_every,
                        static_cast<std::size_t>(cp.keep_checkpoints),
                        static_cast<std::size_t>(cp.keep_snapshots),
                        static_cast<std::size_t>(cp.reorder_window),
                        cp.universe);
    engine.events_processed_ = cp.events_processed;
    engine.last_exchange_ts_ = cp.last_exchange_ts;
    engine.time_regressions_ = cp.time_regressions;
    engine.unknown_instrument_dropped_ = cp.unknown_instrument_dropped;
    engine.unknown_venue_dropped_ = cp.unknown_venue_dropped;
    engine.snapshots_emitted_ = cp.snapshots_emitted;
    for (const auto& [iid, bcp] : cp.books) {
        engine.books_.emplace(iid, ConsolidatedBook::restore(bcp));
    }
    return engine;
}

// ------------------------------------------------------------- JSON codec

namespace {

void put_u64(std::string& out, std::uint64_t v) {
    char buf[21];
    int n = 0;
    do {
        buf[n++] = static_cast<char>('0' + v % 10);
        v /= 10;
    } while (v != 0);
    while (n > 0) out.push_back(buf[--n]);
}

void put_i64(std::string& out, std::int64_t v) {
    if (v < 0) {
        out.push_back('-');
        put_u64(out, ~static_cast<std::uint64_t>(v) + 1);
    } else {
        put_u64(out, static_cast<std::uint64_t>(v));
    }
}

void put_key(std::string& out, const char* key) {
    out.push_back('"');
    out += key;
    out += "\":";
}

void put_bool(std::string& out, bool b) { out += b ? "true" : "false"; }

void put_event(std::string& out, const MarketEvent& ev) {
    out.push_back('[');
    put_u64(out, ev.event_id);
    out.push_back(',');
    put_u64(out, ev.instrument_id);
    out.push_back(',');
    put_u64(out, ev.venue_id);
    out.push_back(',');
    put_i64(out, ev.exchange_ts);
    out.push_back(',');
    put_i64(out, ev.receive_ts);
    out.push_back(',');
    put_u64(out, ev.sequence);
    out.push_back(',');
    put_u64(out, ev.event_type);
    out.push_back(',');
    put_u64(out, ev.side);
    out.push_back(',');
    put_i64(out, ev.price_ticks);
    out.push_back(',');
    put_i64(out, ev.qty);
    out.push_back(',');
    put_u64(out, ev.order_id);
    out.push_back(',');
    put_u64(out, ev.trade_id);
    out.push_back(']');
}

void put_book(std::string& out, const BookCheckpoint& cp) {
    out.push_back('{');
    put_key(out, "x-version");
    put_i64(out, CHECKPOINT_VERSION);
    out.push_back(',');
    put_key(out, "instrument_id");
    put_u64(out, cp.instrument_id);
    out.push_back(',');
    put_key(out, "venue_id");
    put_u64(out, cp.venue_id);
    out.push_back(',');
    put_key(out, "levels");
    out.push_back('[');
    for (std::size_t i = 0; i < cp.levels.size(); ++i) {
        if (i) out.push_back(',');
        const auto& lvl = cp.levels[i];
        out += "{\"side\":";
        put_u64(out, lvl.side);
        out += ",\"price_ticks\":";
        put_i64(out, lvl.price_ticks);
        out += ",\"orders\":[";
        for (std::size_t k = 0; k < lvl.orders.size(); ++k) {
            if (k) out.push_back(',');
            out.push_back('[');
            put_u64(out, lvl.orders[k].first);
            out.push_back(',');
            put_i64(out, lvl.orders[k].second);
            out.push_back(']');
        }
        out += "]}";
    }
    out += "],";
    put_key(out, "arrival_order");
    out.push_back('[');
    for (std::size_t i = 0; i < cp.arrival_order.size(); ++i) {
        if (i) out.push_back(',');
        put_u64(out, cp.arrival_order[i]);
    }
    out += "],";
    put_key(out, "last_sequence");
    put_u64(out, cp.last_sequence);
    out.push_back(',');
    put_key(out, "has_sequence");
    put_bool(out, cp.has_sequence);
    out.push_back(',');
    put_key(out, "sequence_epoch");
    put_u64(out, cp.sequence_epoch);
    out.push_back(',');
    put_key(out, "exchange_ts");
    put_i64(out, cp.exchange_ts);
    out.push_back(',');
    put_key(out, "receive_ts");
    put_i64(out, cp.receive_ts);
    out.push_back(',');
    put_key(out, "trade_flow");
    put_i64(out, cp.trade_flow);
    out.push_back(',');
    put_key(out, "status");
    put_i64(out, cp.status);
    out.push_back(',');
    put_key(out, "stale");
    put_bool(out, cp.stale);
    out.push_back(',');
    put_key(out, "snapshot_active");
    put_bool(out, cp.snapshot_active);
    out.push_back(',');
    put_key(out, "snapshot_broken");
    put_bool(out, cp.snapshot_broken);
    out.push_back(',');
    put_key(out, "snapshot_countdown");
    put_u64(out, cp.snapshot_countdown);
    out.push_back(',');
    put_key(out, "snapshot_synthetic_next");
    out.push_back('[');
    put_u64(out, cp.snapshot_synthetic_next[0]);
    out.push_back(',');
    put_u64(out, cp.snapshot_synthetic_next[1]);
    out += "],";
    put_key(out, "reorder_window");
    put_u64(out, cp.reorder_window);
    out.push_back(',');
    put_key(out, "reorder_pending");
    out.push_back('[');
    for (std::size_t i = 0; i < cp.reorder_pending.size(); ++i) {
        if (i) out.push_back(',');
        put_event(out, cp.reorder_pending[i]);
    }
    out += "],";
    put_key(out, "counters");
    const BookCounters& c = cp.counters;
    out += "{\"duplicates_dropped\":";
    put_u64(out, c.duplicates_dropped);
    out += ",\"gaps_detected\":";
    put_u64(out, c.gaps_detected);
    out += ",\"dropped_while_stale\":";
    put_u64(out, c.dropped_while_stale);
    out += ",\"unknown_order_events\":";
    put_u64(out, c.unknown_order_events);
    out += ",\"invalid_side_dropped\":";
    put_u64(out, c.invalid_side_dropped);
    out += ",\"invalid_payload_dropped\":";
    put_u64(out, c.invalid_payload_dropped);
    out += ",\"unknown_type_dropped\":";
    put_u64(out, c.unknown_type_dropped);
    out += ",\"modify_price_mismatch\":";
    put_u64(out, c.modify_price_mismatch);
    out += ",\"snapshot_restarts\":";
    put_u64(out, c.snapshot_restarts);
    out += ",\"sequence_resets\":";
    put_u64(out, c.sequence_resets);
    out += ",\"late_recovered\":";
    put_u64(out, c.late_recovered);
    out += ",\"events_applied\":";
    put_u64(out, c.events_applied);
    out += "}}";
}

[[noreturn]] void bad(const std::string& what) {
    throw std::invalid_argument("checkpoint JSON: " + what);
}

const Json& field(const Json& obj, const char* key) {
    if (!obj.has(key)) bad(std::string("missing key ") + key);
    return obj[key];
}

std::uint64_t get_u64(const Json& obj, const char* key) {
    const Json& v = field(obj, key);
    if (v.type != Json::Type::Num || !v.is_int || v.neg) {
        bad(std::string("field ") + key + " must be an unsigned integer");
    }
    return v.mag;
}

std::int64_t get_i64(const Json& obj, const char* key) {
    const Json& v = field(obj, key);
    if (v.type != Json::Type::Num || !v.is_int) {
        bad(std::string("field ") + key + " must be an integer");
    }
    constexpr std::uint64_t kI64Max = 0x7FFFFFFFFFFFFFFFULL;
    if (v.neg) {
        if (v.mag > kI64Max + 1) bad(std::string("field ") + key + " out of i64 range");
        return static_cast<std::int64_t>(~v.mag + 1);
    }
    if (v.mag > kI64Max) bad(std::string("field ") + key + " out of i64 range");
    return static_cast<std::int64_t>(v.mag);
}

bool get_bool(const Json& obj, const char* key) {
    const Json& v = field(obj, key);
    if (v.type != Json::Type::Bool) bad(std::string("field ") + key + " must be a bool");
    return v.boolean;
}

std::uint64_t as_u64(const Json& v, const char* what) {
    if (v.type != Json::Type::Num || !v.is_int || v.neg) {
        bad(std::string(what) + " must be an unsigned integer");
    }
    return v.mag;
}

std::int64_t as_i64(const Json& v, const char* what) {
    if (v.type != Json::Type::Num || !v.is_int) {
        bad(std::string(what) + " must be an integer");
    }
    constexpr std::uint64_t kI64Max = 0x7FFFFFFFFFFFFFFFULL;
    if (v.neg) {
        if (v.mag > kI64Max + 1) bad(std::string(what) + " out of i64 range");
        return static_cast<std::int64_t>(~v.mag + 1);
    }
    if (v.mag > kI64Max) bad(std::string(what) + " out of i64 range");
    return static_cast<std::int64_t>(v.mag);
}

template <typename T>
T narrow(std::uint64_t v, const char* what) {
    if (v > std::numeric_limits<T>::max()) bad(std::string(what) + " out of range");
    return static_cast<T>(v);
}

BookCheckpoint parse_book(const Json& j) {
    if (get_i64(j, "x-version") != CHECKPOINT_VERSION) {
        bad("unsupported book checkpoint x-version");
    }
    BookCheckpoint cp;
    cp.instrument_id = narrow<std::uint32_t>(get_u64(j, "instrument_id"), "instrument_id");
    cp.venue_id = narrow<std::uint16_t>(get_u64(j, "venue_id"), "venue_id");
    for (const Json& lj : field(j, "levels").a()) {
        LevelCheckpoint lc;
        lc.side = narrow<std::uint8_t>(get_u64(lj, "side"), "side");
        lc.price_ticks = get_i64(lj, "price_ticks");
        for (const Json& oj : field(lj, "orders").a()) {
            const auto& pair = oj.a();
            if (pair.size() != 2) bad("order entry must be [id, qty]");
            lc.orders.emplace_back(as_u64(pair[0], "order_id"),
                                   as_i64(pair[1], "qty"));
        }
        cp.levels.push_back(std::move(lc));
    }
    for (const Json& v : field(j, "arrival_order").a()) {
        cp.arrival_order.push_back(as_u64(v, "arrival_order"));
    }
    cp.last_sequence = get_u64(j, "last_sequence");
    cp.has_sequence = get_bool(j, "has_sequence");
    cp.sequence_epoch = get_u64(j, "sequence_epoch");
    cp.exchange_ts = get_i64(j, "exchange_ts");
    cp.receive_ts = get_i64(j, "receive_ts");
    cp.trade_flow = get_i64(j, "trade_flow");
    cp.status = get_i64(j, "status");
    cp.stale = get_bool(j, "stale");
    cp.snapshot_active = get_bool(j, "snapshot_active");
    cp.snapshot_broken = get_bool(j, "snapshot_broken");
    cp.snapshot_countdown = get_u64(j, "snapshot_countdown");
    const auto& nxt = field(j, "snapshot_synthetic_next").a();
    if (nxt.size() != 2) bad("snapshot_synthetic_next must have 2 entries");
    cp.snapshot_synthetic_next = {as_u64(nxt[0], "snapshot_synthetic_next"),
                                  as_u64(nxt[1], "snapshot_synthetic_next")};
    cp.reorder_window = get_u64(j, "reorder_window");
    for (const Json& row : field(j, "reorder_pending").a()) {
        const auto& f = row.a();
        if (f.size() != 12) bad("reorder_pending row must have 12 fields");
        cp.reorder_pending.push_back(MarketEvent::of(
            as_u64(f[0], "event_id"),
            narrow<std::uint32_t>(as_u64(f[1], "instrument_id"), "instrument_id"),
            narrow<std::uint16_t>(as_u64(f[2], "venue_id"), "venue_id"),
            as_i64(f[3], "exchange_ts"), as_i64(f[4], "receive_ts"),
            as_u64(f[5], "sequence"),
            narrow<std::uint8_t>(as_u64(f[6], "event_type"), "event_type"),
            narrow<std::uint8_t>(as_u64(f[7], "side"), "side"),
            as_i64(f[8], "price_ticks"), as_i64(f[9], "qty"),
            as_u64(f[10], "order_id"), as_u64(f[11], "trade_id")));
    }
    const Json& c = field(j, "counters");
    cp.counters.duplicates_dropped = get_u64(c, "duplicates_dropped");
    cp.counters.gaps_detected = get_u64(c, "gaps_detected");
    cp.counters.dropped_while_stale = get_u64(c, "dropped_while_stale");
    cp.counters.unknown_order_events = get_u64(c, "unknown_order_events");
    cp.counters.invalid_side_dropped = get_u64(c, "invalid_side_dropped");
    cp.counters.invalid_payload_dropped = get_u64(c, "invalid_payload_dropped");
    cp.counters.unknown_type_dropped = get_u64(c, "unknown_type_dropped");
    cp.counters.modify_price_mismatch = get_u64(c, "modify_price_mismatch");
    cp.counters.snapshot_restarts = get_u64(c, "snapshot_restarts");
    cp.counters.sequence_resets = get_u64(c, "sequence_resets");
    cp.counters.late_recovered = get_u64(c, "late_recovered");
    cp.counters.events_applied = get_u64(c, "events_applied");
    return cp;
}

std::uint64_t parse_key_u64(const std::string& key) {
    if (key.empty() || key.size() > 20) bad("bad numeric key " + key);
    std::uint64_t v = 0;
    for (char ch : key) {
        if (ch < '0' || ch > '9') bad("bad numeric key " + key);
        const std::uint64_t d = static_cast<std::uint64_t>(ch - '0');
        if (v > (std::numeric_limits<std::uint64_t>::max() - d) / 10) {
            bad("numeric key overflow " + key);
        }
        v = v * 10 + d;
    }
    return v;
}

}  // namespace

std::string book_checkpoint_to_json(const BookCheckpoint& cp) {
    std::string out;
    put_book(out, cp);
    return out;
}

BookCheckpoint book_checkpoint_from_json(const std::string& text) {
    Json j;
    try {
        j = JsonParser::parse(text);
    } catch (const std::runtime_error& e) {
        bad(e.what());
    }
    return parse_book(j);
}

std::string checkpoint_to_json(const ReplayCheckpoint& cp) {
    std::string out;
    out.reserve(4096);
    out += "{\"x-version\":";
    put_i64(out, ENGINE_CHECKPOINT_VERSION);
    out += ",\"events_processed\":";
    put_u64(out, cp.events_processed);
    out += ",\"last_exchange_ts\":";
    put_i64(out, cp.last_exchange_ts);
    out += ",\"time_regressions\":";
    put_u64(out, cp.time_regressions);
    out += ",\"unknown_instrument_dropped\":";
    put_u64(out, cp.unknown_instrument_dropped);
    out += ",\"unknown_venue_dropped\":";
    put_u64(out, cp.unknown_venue_dropped);
    out += ",\"checkpoint_every\":";
    put_u64(out, cp.checkpoint_every);
    out += ",\"snapshot_every\":";
    put_u64(out, cp.snapshot_every);
    out += ",\"keep_checkpoints\":";
    put_u64(out, cp.keep_checkpoints);
    out += ",\"keep_snapshots\":";
    put_u64(out, cp.keep_snapshots);
    out += ",\"snapshots_emitted\":";
    put_u64(out, cp.snapshots_emitted);
    out += ",\"reorder_window\":";
    put_u64(out, cp.reorder_window);
    out += ",\"universe\":";
    if (!cp.universe) {
        out += "null";
    } else {
        out.push_back('{');
        bool first = true;
        for (const auto& [iid, venues] : *cp.universe) {
            if (!first) out.push_back(',');
            first = false;
            out.push_back('"');
            put_u64(out, iid);
            out += "\":[";
            bool fv = true;
            for (std::uint16_t vid : venues) {
                if (!fv) out.push_back(',');
                fv = false;
                put_u64(out, vid);
            }
            out.push_back(']');
        }
        out.push_back('}');
    }
    out += ",\"books\":{";
    bool first = true;
    for (const auto& [iid, ccp] : cp.books) {
        if (!first) out.push_back(',');
        first = false;
        out.push_back('"');
        put_u64(out, iid);
        out += "\":{\"instrument_id\":";
        put_u64(out, ccp.instrument_id);
        out += ",\"reorder_window\":";
        put_u64(out, ccp.reorder_window);
        out += ",\"venues\":{";
        bool fv = true;
        for (const auto& [vid, bcp] : ccp.venues) {
            if (!fv) out.push_back(',');
            fv = false;
            out.push_back('"');
            put_u64(out, vid);
            out += "\":";
            put_book(out, bcp);
        }
        out += "}}";
    }
    out += "}}";
    return out;
}

ReplayCheckpoint checkpoint_from_json(const std::string& text) {
    Json j;
    try {
        j = JsonParser::parse(text);
    } catch (const std::runtime_error& e) {
        bad(e.what());
    }
    if (j.type != Json::Type::Obj) bad("top level must be an object");
    if (get_i64(j, "x-version") != ENGINE_CHECKPOINT_VERSION) {
        bad("unsupported engine checkpoint x-version");
    }
    ReplayCheckpoint cp;
    cp.events_processed = get_u64(j, "events_processed");
    cp.last_exchange_ts = get_i64(j, "last_exchange_ts");
    cp.time_regressions = get_u64(j, "time_regressions");
    cp.unknown_instrument_dropped = get_u64(j, "unknown_instrument_dropped");
    cp.unknown_venue_dropped = get_u64(j, "unknown_venue_dropped");
    cp.checkpoint_every = get_u64(j, "checkpoint_every");
    cp.snapshot_every = get_u64(j, "snapshot_every");
    cp.keep_checkpoints = get_u64(j, "keep_checkpoints");
    cp.keep_snapshots = get_u64(j, "keep_snapshots");
    cp.snapshots_emitted = get_u64(j, "snapshots_emitted");
    cp.reorder_window = get_u64(j, "reorder_window");
    const Json& uni = field(j, "universe");
    if (uni.type == Json::Type::Obj) {
        Universe u;
        for (const auto& [key, arr] : uni.obj) {
            auto& venues = u[narrow<std::uint32_t>(parse_key_u64(key), "universe key")];
            for (const Json& v : arr.a()) {
                venues.insert(narrow<std::uint16_t>(as_u64(v, "universe venue"),
                                                    "universe venue"));
            }
        }
        cp.universe = std::move(u);
    } else if (uni.type != Json::Type::Null) {
        bad("universe must be null or an object");
    }
    const Json& books = field(j, "books");
    if (books.type != Json::Type::Obj) bad("books must be an object");
    for (const auto& [key, cj] : books.obj) {
        ConsolidatedCheckpoint ccp;
        ccp.instrument_id = narrow<std::uint32_t>(get_u64(cj, "instrument_id"), "instrument_id");
        if (parse_key_u64(key) != ccp.instrument_id) bad("books key != instrument_id");
        ccp.reorder_window = get_u64(cj, "reorder_window");
        const Json& venues = field(cj, "venues");
        if (venues.type != Json::Type::Obj) bad("venues must be an object");
        for (const auto& [vkey, bj] : venues.obj) {
            BookCheckpoint bcp = parse_book(bj);
            if (parse_key_u64(vkey) != bcp.venue_id) bad("venues key != venue_id");
            ccp.venues.emplace(bcp.venue_id, std::move(bcp));
        }
        cp.books.emplace(ccp.instrument_id, std::move(ccp));
    }
    return cp;
}

}  // namespace iap
