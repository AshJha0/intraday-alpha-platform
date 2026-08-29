// MarketEvent layout + validation rules (API_CORE.md section 1).

#include <gtest/gtest.h>

#include <cstdint>
#include <stdexcept>

#include "iap/marketdata/events.hpp"

using iap::EventType;
using iap::MarketEvent;
using iap::SessionStatus;
using iap::Side;
using iap::validate;
using iap::validation_error;

namespace {

MarketEvent base_add() {
    return MarketEvent::of(1, 1, 1, 1000, 1100, 1,
                           static_cast<std::uint8_t>(EventType::ADD),
                           static_cast<std::uint8_t>(Side::BID), 2450, 100,
                           42, 0);
}

}  // namespace

TEST(MarketEvent, PackedLayoutMatchesIap1Record) {
    EXPECT_EQ(sizeof(MarketEvent), 72u);
    // Offsets are compile-time asserted in the header; spot check field order
    // through the factory (canonical order in, record order stored).
    MarketEvent ev = MarketEvent::of(1, 2, 3, 4, 5, 6, 7, 1, 9, 10, 11, 12);
    EXPECT_EQ(ev.event_id, 1u);
    EXPECT_EQ(ev.instrument_id, 2u);
    EXPECT_EQ(ev.venue_id, 3u);
    EXPECT_EQ(ev.exchange_ts, 4);
    EXPECT_EQ(ev.receive_ts, 5);
    EXPECT_EQ(ev.sequence, 6u);
    EXPECT_EQ(ev.event_type, 7u);
    EXPECT_EQ(ev.side, 1u);
    EXPECT_EQ(ev.price_ticks, 9);
    EXPECT_EQ(ev.qty, 10);
    EXPECT_EQ(ev.order_id, 11u);
    EXPECT_EQ(ev.trade_id, 12u);
}

TEST(Validation, ValidAddPasses) {
    EXPECT_EQ(validation_error(base_add()), "");
    EXPECT_NO_THROW(validate(base_add()));
}

TEST(Validation, ReceiveBeforeExchangeFails) {
    MarketEvent ev = base_add();
    ev.receive_ts = ev.exchange_ts - 1;
    EXPECT_NE(validation_error(ev), "");
    EXPECT_THROW(validate(ev), std::invalid_argument);
}

TEST(Validation, UnknownEventTypeFails) {
    MarketEvent ev = base_add();
    ev.event_type = 0;
    EXPECT_NE(validation_error(ev), "");
    ev.event_type = 10;
    EXPECT_NE(validation_error(ev), "");
}

TEST(Validation, BadSideFails) {
    MarketEvent ev = base_add();
    ev.side = 2;
    EXPECT_NE(validation_error(ev), "");
}

TEST(Validation, BookTypesRequireOrderId) {
    for (auto et : {EventType::ADD, EventType::MODIFY, EventType::CANCEL,
                    EventType::EXECUTE}) {
        MarketEvent ev = base_add();
        ev.event_type = static_cast<std::uint8_t>(et);
        ev.order_id = 0;
        EXPECT_NE(validation_error(ev), "") << "event_type " << int(ev.event_type);
    }
}

TEST(Validation, AddNonPositiveQtyFailsButCancelAllowed) {
    MarketEvent ev = base_add();
    ev.qty = 0;
    EXPECT_NE(validation_error(ev), "");
    ev.event_type = static_cast<std::uint8_t>(EventType::CANCEL);
    ev.price_ticks = 0;  // CANCEL: qty/price constraints are waived
    EXPECT_EQ(validation_error(ev), "");
}

TEST(Validation, AddNonPositivePriceFails) {
    MarketEvent ev = base_add();
    ev.price_ticks = 0;
    EXPECT_NE(validation_error(ev), "");
    ev.price_ticks = -5;
    EXPECT_NE(validation_error(ev), "");
}

TEST(Validation, TradeRules) {
    MarketEvent ev = MarketEvent::of(2, 1, 1, 1000, 1100, 2,
                                     static_cast<std::uint8_t>(EventType::TRADE),
                                     0, 2450, 100, 0, 7);
    EXPECT_EQ(validation_error(ev), "");
    ev.trade_id = 0;
    EXPECT_NE(validation_error(ev), "");
    ev.trade_id = 7;
    ev.qty = 0;
    EXPECT_NE(validation_error(ev), "");
    ev.qty = 100;
    ev.price_ticks = 0;
    EXPECT_NE(validation_error(ev), "");
}

TEST(Validation, QuoteAndSnapshotRequirePositivePayload) {
    for (auto et : {EventType::QUOTE, EventType::SNAPSHOT}) {
        MarketEvent ev = base_add();
        ev.event_type = static_cast<std::uint8_t>(et);
        EXPECT_EQ(validation_error(ev), "");
        ev.qty = -1;
        EXPECT_NE(validation_error(ev), "");
        ev.qty = 10;
        ev.price_ticks = 0;
        EXPECT_NE(validation_error(ev), "");
    }
}

TEST(Validation, StatusCodeChecked) {
    MarketEvent ev = MarketEvent::of(
        3, 1, 1, 1000, 1100, 3, static_cast<std::uint8_t>(EventType::STATUS),
        0, 0, static_cast<std::int64_t>(SessionStatus::HALT), 0, 0);
    EXPECT_EQ(validation_error(ev), "");
    ev.qty = 0;
    EXPECT_NE(validation_error(ev), "");
    ev.qty = 5;
    EXPECT_NE(validation_error(ev), "");
}

TEST(Validation, HeartbeatWithZeroPayloadPasses) {
    MarketEvent ev = MarketEvent::of(
        4, 1, 1, 1000, 1100, 4,
        static_cast<std::uint8_t>(EventType::HEARTBEAT), 0, 0, 0, 0, 0);
    EXPECT_EQ(validation_error(ev), "");
}

TEST(Validation, ValidateThrowsWithEventIdInMessage) {
    MarketEvent ev = base_add();
    ev.side = 9;
    try {
        validate(ev);
        FAIL() << "expected std::invalid_argument";
    } catch (const std::invalid_argument& e) {
        EXPECT_NE(std::string(e.what()).find("event_id=1"), std::string::npos);
    }
}
