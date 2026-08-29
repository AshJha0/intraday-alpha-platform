#include "iap/marketdata/events.hpp"

#include <stdexcept>

namespace iap {

namespace {

bool is_book_type(std::uint8_t et) {
    return et == static_cast<std::uint8_t>(EventType::ADD) ||
           et == static_cast<std::uint8_t>(EventType::MODIFY) ||
           et == static_cast<std::uint8_t>(EventType::CANCEL) ||
           et == static_cast<std::uint8_t>(EventType::EXECUTE);
}

}  // namespace

std::string validation_error(const MarketEvent& ev) {
    // Integer domain checks (u64/u32/u16 ranges) are inherent to the
    // fixed-width struct fields; the semantic checks below mirror the
    // Python reference exactly.
    if (ev.receive_ts < ev.exchange_ts) {
        return "receive_ts " + std::to_string(ev.receive_ts) +
               " < exchange_ts " + std::to_string(ev.exchange_ts);
    }
    if (ev.event_type < 1 || ev.event_type > 9) {
        return "unknown event_type: " + std::to_string(ev.event_type);
    }
    if (ev.side != 0 && ev.side != 1) {
        return "side must be 0 (BID) or 1 (ASK): " + std::to_string(ev.side);
    }

    const std::uint8_t et = ev.event_type;
    if (is_book_type(et)) {
        if (ev.order_id == 0) {
            return "order_id required for event_type " + std::to_string(et);
        }
        if (ev.qty <= 0 && et != static_cast<std::uint8_t>(EventType::CANCEL)) {
            return "qty must be > 0 for event_type " + std::to_string(et) +
                   ": " + std::to_string(ev.qty);
        }
        if (ev.price_ticks <= 0 &&
            et != static_cast<std::uint8_t>(EventType::CANCEL)) {
            return "price_ticks must be > 0 for event_type " +
                   std::to_string(et) + ": " + std::to_string(ev.price_ticks);
        }
    } else if (et == static_cast<std::uint8_t>(EventType::TRADE) ||
               et == static_cast<std::uint8_t>(EventType::QUOTE) ||
               et == static_cast<std::uint8_t>(EventType::SNAPSHOT)) {
        if (ev.qty <= 0) {
            return "qty must be > 0 for event_type " + std::to_string(et) +
                   ": " + std::to_string(ev.qty);
        }
        if (ev.price_ticks <= 0) {
            return "price_ticks must be > 0 for event_type " +
                   std::to_string(et) + ": " + std::to_string(ev.price_ticks);
        }
        if (et == static_cast<std::uint8_t>(EventType::TRADE) &&
            ev.trade_id == 0) {
            return "trade_id required for TRADE";
        }
    } else if (et == static_cast<std::uint8_t>(EventType::STATUS)) {
        if (ev.qty < 1 || ev.qty > 4) {
            return "STATUS qty must be a SessionStatus code: " +
                   std::to_string(ev.qty);
        }
    }
    // HEARTBEAT: no payload constraints.
    return "";
}

void validate(const MarketEvent& ev) {
    std::string reason = validation_error(ev);
    if (!reason.empty()) {
        throw std::invalid_argument("invalid MarketEvent (event_id=" +
                                    std::to_string(ev.event_id) +
                                    "): " + reason);
    }
}

}  // namespace iap
