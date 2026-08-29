// Canonical MarketEvent contract and enums (PLATFORM_CONVENTIONS.md sections 1-2).
//
// All prices are int64 ticks, quantities int64 base units, timestamps int64 ns
// since the Unix epoch. The struct layout below mirrors the IAP1 binary record
// (schemas/FORMAT.md section 2) byte for byte on a little-endian target; the
// canonical JSONL key order is the logical field order used by MarketEvent::of.

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace iap {

enum class Side : std::uint8_t {
    BID = 0,
    ASK = 1,
};

enum class EventType : std::uint8_t {
    ADD = 1,
    MODIFY = 2,
    CANCEL = 3,
    EXECUTE = 4,
    TRADE = 5,
    QUOTE = 6,
    SNAPSHOT = 7,
    STATUS = 8,
    HEARTBEAT = 9,
};

enum class SessionStatus : std::uint8_t {
    TRADING = 1,
    HALT = 2,
    AUCTION = 3,
    CLOSE = 4,
};

// One canonical market event. Field order below is the IAP1 *record* order so
// the struct is layout-compatible with the wire format (72 bytes, no padding).
struct MarketEvent {
    std::uint64_t event_id;
    std::uint32_t instrument_id;
    std::uint16_t venue_id;
    std::uint8_t event_type;
    std::uint8_t side;
    std::int64_t exchange_ts;
    std::int64_t receive_ts;
    std::uint64_t sequence;
    std::int64_t price_ticks;
    std::int64_t qty;
    std::uint64_t order_id;
    std::uint64_t trade_id;

    // Factory taking arguments in the canonical (JSONL) field order.
    static MarketEvent of(std::uint64_t event_id, std::uint32_t instrument_id,
                          std::uint16_t venue_id, std::int64_t exchange_ts,
                          std::int64_t receive_ts, std::uint64_t sequence,
                          std::uint8_t event_type, std::uint8_t side,
                          std::int64_t price_ticks, std::int64_t qty,
                          std::uint64_t order_id, std::uint64_t trade_id) {
        return MarketEvent{event_id,   instrument_id, venue_id, event_type,
                           side,       exchange_ts,   receive_ts, sequence,
                           price_ticks, qty,          order_id,  trade_id};
    }
};

static_assert(sizeof(MarketEvent) == 72, "IAP1 record must be 72 bytes");
static_assert(offsetof(MarketEvent, event_id) == 0, "IAP1 layout");
static_assert(offsetof(MarketEvent, instrument_id) == 8, "IAP1 layout");
static_assert(offsetof(MarketEvent, venue_id) == 12, "IAP1 layout");
static_assert(offsetof(MarketEvent, event_type) == 14, "IAP1 layout");
static_assert(offsetof(MarketEvent, side) == 15, "IAP1 layout");
static_assert(offsetof(MarketEvent, exchange_ts) == 16, "IAP1 layout");
static_assert(offsetof(MarketEvent, receive_ts) == 24, "IAP1 layout");
static_assert(offsetof(MarketEvent, sequence) == 32, "IAP1 layout");
static_assert(offsetof(MarketEvent, price_ticks) == 40, "IAP1 layout");
static_assert(offsetof(MarketEvent, qty) == 48, "IAP1 layout");
static_assert(offsetof(MarketEvent, order_id) == 56, "IAP1 layout");
static_assert(offsetof(MarketEvent, trade_id) == 64, "IAP1 layout");

inline bool operator==(const MarketEvent& a, const MarketEvent& b) {
    return a.event_id == b.event_id && a.instrument_id == b.instrument_id &&
           a.venue_id == b.venue_id && a.event_type == b.event_type &&
           a.side == b.side && a.exchange_ts == b.exchange_ts &&
           a.receive_ts == b.receive_ts && a.sequence == b.sequence &&
           a.price_ticks == b.price_ticks && a.qty == b.qty &&
           a.order_id == b.order_id && a.trade_id == b.trade_id;
}

inline bool operator!=(const MarketEvent& a, const MarketEvent& b) {
    return !(a == b);
}

// Return a reason string if `ev` violates the contract, else "" (valid).
// Mirrors python/src/iap/core/events.py::validation_error (integer domain
// checks that are impossible with fixed-width fields are inherent here).
std::string validation_error(const MarketEvent& ev);

// Throw std::invalid_argument if `ev` violates the canonical contract.
void validate(const MarketEvent& ev);

}  // namespace iap
