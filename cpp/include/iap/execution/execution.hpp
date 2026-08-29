// Production-grade event-driven execution simulator (spec sections 17-18).
//
// The simulator replays the normalized market-event stream (the same stream
// the replay layer consumes), maintains per-venue books, and simulates the
// lifecycle of child orders: latency-delayed arrival, aggressive execution
// against displayed depth, passive queue-position tracking, partial fills,
// fees/rebates and linear impact. Deterministic: same config + seed =>
// identical fills, bit for bit (SplitMix64 only, no wall clock).
//
// PINNED RULES (documented here because this port is the reference for
// tests/golden/expected_replay_fills.json):
//
// 1. Latency: a child order decided at `decision_ts` arrives at the venue at
//      arrival_ts = decision_ts + decision_ns + risk_ns + wire_ns
//                 + venue.latency_mean_ns + jitter,
//    jitter = SplitMix64(seed).below(venue.latency_jitter_ns + 1) — one draw
//    per submitted order, in submission order (uniform on [0, jitter_ns],
//    mirroring the venue latency profile in configs/venues.json).
// 2. Activation: a pending order becomes active while processing the first
//    market event with exchange_ts >= arrival_ts, BEFORE that event is
//    applied to the books; orders activate in (arrival_ts, order_id) order.
//    Aggressive fills are stamped with arrival_ts.
// 3. Aggressive execution (MARKET, and the marketable part of LIMIT/IOC/FOK)
//    walks the DISPLAYED top-10 depth of the target venue's opposite side,
//    best price first, up to the limit price (MARKET: unconstrained). One
//    Fill per price level. Simulated orders never mutate the replayed book
//    (the market-data stream stays authoritative); the linear impact cost
//    accounts for the self-impact economically. Unfilled MARKET/IOC
//    remainders are cancelled; a FOK order fills fully or not at all (checked
//    against displayed depth within the limit before any fill).
// 4. Passive queue position (pinned deterministic rule): when a LIMIT
//    remainder rests at price P, ahead_qty := displayed qty at (side, P) on
//    that venue at rest time. Subsequently, on that venue:
//      - an observed EXECUTE at (side, P) reduces ahead_qty by its full qty;
//        any leftover EXECUTE volume after ahead_qty reaches 0 fills our
//        order (partial fills supported) at P;
//      - an observed CANCEL at (side, P) reduces ahead_qty by its full qty
//        (deterministic full amount — no probabilistic split), floored at 0;
//      - an observed EXECUTE on our side at a price WORSE than P (below our
//        bid / above our ask) means the market traded through our level: the
//        order fills in full at P;
//      - a MARKETABLE incoming ADD (its limit crosses the pre-event opposite
//        best; the replayed book matches it internally with NO EXECUTE
//        events, per conventions section 4) is expanded into the per-level
//        volumes it consumes: the walk over the pre-event displayed depth,
//        best first up to the ADD's limit, applies the two EXECUTE rules
//        above level by level (consumption at P depletes-then-fills;
//        consumption strictly worse than P fills in full);
//      - after the event is applied, if the venue's opposite best crosses P
//        (ask <= our bid / bid >= our ask, e.g. after an FX QUOTE replaced
//        L1), the order fills in full at P (an incoming marketable order
//        would have hit us first). EXEMPTION (pinned): a LIMIT remainder
//        that rests while the displayed opposite best already crosses its
//        price (possible only because the aggressive part just consumed
//        that very display — simulated fills never mutate the book) is
//        crossing-exempt until the display first shows an uncrossed
//        opposite best; without this the same displayed liquidity would be
//        double-counted (taken aggressively AND again via the crossing
//        rule);
//      - MODIFY events do not change ahead_qty (a modified order's queue
//        position is unknowable from the public stream — pinned: ignored).
//    Passive fills are stamped with the triggering event's exchange_ts.
// 5. Fees (configs/venues.json): equity venues charge
//    taker_fee_per_share * qty on aggressive fills and rebate
//    maker_rebate_per_share * qty on passive fills (fee < 0 = rebate).
//    FX venues charge commission_per_million * notional / 1e6 on every fill,
//    notional = qty * lot_size * price_ticks * tick_size.
// 6. Linear impact (aggressive fills only, configs/execution.json
//    cost_model): impact_bps = impact_coeff_bps_per_pct_adv *
//    (child_order_qty / adv * 100); each taker fill is charged
//    impact_bps * 1e-4 * its own notional. Passive fills carry zero impact.

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "iap/marketdata/events.hpp"
#include "iap/marketdata/rng.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

enum class OrderType : std::uint8_t { MARKET = 0, LIMIT = 1, IOC = 2, FOK = 3 };
enum class OrderState : std::uint8_t {
    PENDING = 0,    // submitted, in flight to the venue
    ACTIVE = 1,     // resting passively at the venue
    FILLED = 2,
    CANCELLED = 3,  // includes IOC/FOK/MARKET unfilled remainders
};
enum class Liquidity : std::uint8_t { TAKER = 0, MAKER = 1 };

// One venue's execution profile (configs/venues.json).
struct VenueSpec {
    std::uint16_t venue_id = 0;
    std::string name;
    bool is_fx = false;
    double taker_fee_per_share = 0.0;
    double maker_rebate_per_share = 0.0;
    double commission_per_million = 0.0;
    std::int64_t latency_mean_ns = 0;
    std::int64_t latency_jitter_ns = 0;
};

// Load every venue from configs/venues.json.
std::map<std::uint16_t, VenueSpec> load_venues(const std::string& path);

// Instrument reference data the simulator needs.
struct InstrumentSpec {
    std::uint32_t instrument_id = 0;
    double tick_size = 0.0;
    double lot_size = 1.0;
    double adv = 1.0;  // average daily volume, base units
};

// Internal (decision -> wire) latency legs; venue leg comes from VenueSpec.
struct LatencyConfig {
    std::int64_t decision_ns = 50'000;
    std::int64_t risk_ns = 50'000;
    std::int64_t wire_ns = 100'000;
};

struct ExecConfig {
    LatencyConfig latency;
    std::uint64_t seed = 20260829;
    double impact_coeff_bps_per_pct_adv = 2.0;
    std::map<std::uint32_t, InstrumentSpec> instruments;
    std::map<std::uint16_t, VenueSpec> venues;
};

struct Fill {
    std::uint64_t fill_id = 0;
    std::uint64_t order_id = 0;
    std::uint64_t parent_id = 0;
    std::uint32_t instrument_id = 0;
    std::uint16_t venue_id = 0;
    std::uint8_t side = 0;  // side of OUR order (0 buy / 1 sell)
    std::int64_t price_ticks = 0;
    std::int64_t qty = 0;
    std::int64_t ts = 0;  // exchange_ts of the fill (pinned rules 2/4)
    Liquidity liquidity = Liquidity::TAKER;
    double fee = 0.0;          // > 0 cost, < 0 rebate
    double impact_cost = 0.0;  // linear impact charge (taker fills only)
};

struct ChildOrder {
    std::uint64_t order_id = 0;  // assigned by submit()
    std::uint64_t parent_id = 0;
    std::uint32_t instrument_id = 0;
    std::uint16_t venue_id = 0;
    std::uint8_t side = 0;  // 0 = buy, 1 = sell
    OrderType type = OrderType::LIMIT;
    std::int64_t limit_ticks = 0;  // ignored for MARKET
    std::int64_t qty = 0;
    std::int64_t decision_ts = 0;
    // ---- simulator-owned runtime state ----
    std::int64_t arrival_ts = 0;
    OrderState state = OrderState::PENDING;
    std::int64_t remaining = 0;
    std::int64_t ahead_qty = 0;  // displayed qty ahead of us at our level
    bool resting = false;
    bool cross_exempt = false;   // see the crossing-rule exemption above
};

class ExecutionSimulator {
public:
    explicit ExecutionSimulator(const ExecConfig& config);

    // Submit a child order (decision time semantics per pinned rule 1).
    // Returns the assigned order_id.
    std::uint64_t submit(const ChildOrder& child);

    // Cancel an order (pending or resting); no-op for terminal states.
    void cancel(std::uint64_t order_id);

    // Process one market event (activation -> queue tracking -> book apply
    // -> crossing check, per the pinned rules above).
    void on_event(const MarketEvent& ev);

    // Cancel every non-terminal order (end of session).
    void cancel_all();

    const std::vector<Fill>& fills() const { return fills_; }
    const std::map<std::uint64_t, ChildOrder>& orders() const {
        return orders_;
    }
    const ExecConfig& config() const { return config_; }
    // Venue book for (instrument, venue); nullptr before any event touched it.
    const OrderBook* venue_book(std::uint32_t instrument_id,
                                std::uint16_t venue_id) const;
    ConsolidatedBook& instrument_book(std::uint32_t instrument_id);

private:
    const VenueSpec& venue(std::uint16_t venue_id) const;
    const InstrumentSpec& instrument(std::uint32_t instrument_id) const;
    // Queue tracking for observed consumption of displayed liquidity at one
    // price level (EXECUTE events and marketable-ADD expansion).
    void track_consumption(std::uint32_t instrument_id,
                           std::uint16_t venue_id, std::uint8_t side,
                           std::int64_t price_ticks, std::int64_t qty,
                           std::int64_t ts);
    void activate(ChildOrder& o);
    void aggressive_fill(ChildOrder& o, const OrderBook& book);
    void emit_fill(ChildOrder& o, std::int64_t price_ticks, std::int64_t qty,
                   std::int64_t ts, Liquidity liq);
    double fill_fee(const ChildOrder& o, std::int64_t price_ticks,
                    std::int64_t qty, Liquidity liq) const;

    ExecConfig config_;
    SplitMix64 rng_;
    std::map<std::uint32_t, ConsolidatedBook> books_;
    std::map<std::uint64_t, ChildOrder> orders_;
    std::vector<std::uint64_t> pending_;  // sorted by (arrival_ts, order_id)
    std::vector<std::uint64_t> resting_;  // ACTIVE order ids, ascending
    std::vector<Fill> fills_;
    std::uint64_t next_order_id_ = 1;
    std::uint64_t next_fill_id_ = 1;
};

}  // namespace iap
