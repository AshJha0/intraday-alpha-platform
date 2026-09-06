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
// tests/golden/expected_replay_fills.json; the Java port mirrors them
// exactly; PLATFORM_CONVENTIONS.md section 11 is the contract):
//
// 1. Latency: a child order decided at `decision_ts` arrives at the venue at
//      arrival_ts = decision_ts + decision_ns + risk_ns + wire_ns
//                 + venue.latency_mean_ns + jitter,
//    jitter = SplitMix64(seed).below(venue.latency_jitter_ns + 1) — one draw
//    per submitted order OR cancel, in submission order (uniform on
//    [0, jitter_ns], mirroring the venue latency profile in
//    configs/venues.json).
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
//    remainders are cancelled (reason UNFILLED_REMAINDER); a FOK order
//    fills fully or not at all (checked against displayed depth within the
//    limit before any fill).
// 3b. Displayed-liquidity consumption (mutating within a decision, pinned):
//    the simulator keeps a per-(instrument, venue, side, price) overlay of
//    the displayed size OUR aggressive fills already consumed. Aggressive
//    walks see `displayed - consumed` at each level and debit the overlay;
//    whenever an applied market event changes the displayed size at a
//    level (compared before/after the event) its overlay entry becomes
//    min(consumed, new displayed size) — new liquidity added on top of a
//    level we took becomes available, liquidity we took is never
//    resurrected by a partial refresh, and an entry reaching 0 is removed.
//    Two children arriving on the same display therefore share one copy of
//    the liquidity (the second gets the thin remainder), never fresh copies.
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
//    notional = qty * qty_unit * price_ticks * tick_size (qty_unit =
//    lot_size for FX, 1 for EQUITY/ETF; conventions section 1).
// 6. Linear impact (aggressive fills only, configs/execution.json
//    cost_model, IDENTICAL to the research cost model iap.backtest.costs):
//    impact_bps = impact_coeff_bps_per_pct_adv *
//    (child_order_qty * qty_unit / adv * 100); each taker fill is charged
//    impact_bps * 1e-4 * its own notional. Passive fills carry zero impact.
// 7. Cancels have latency: cancel(order_id, cancel_ts) arrives at
//      cancel_ts + decision_ns + risk_ns + wire_ns + venue mean + jitter
//    (one jitter draw, rule 1) and takes effect at max(cancel arrival,
//    order arrival) — a cancel never overtakes its own order — while
//    processing the first event with exchange_ts >= that time, merged with
//    activations in timestamp order (activation first on ties). An order
//    that fills before its cancel arrives is filled (no lookahead).
//    cancel_all() is the end-of-stream sweep (reason END_OF_STREAM,
//    immediate — not a trading decision). Time-in-force: an order with
//    expire_ts != 0 is expired (reason EXPIRED) at the start of the first
//    event with exchange_ts >= expire_ts, pending or resting, before any
//    activation — venue-side expiry has no latency by construction.
// 8. Venue trading-state gate: while the target venue's book is missing,
//    `stale` (unrecovered sequence gap) or its status is not TRADING
//    (HALT / AUCTION / CLOSE), no fill of any kind is produced on that
//    venue: an aggressive arrival (MARKET / IOC / FOK, and the marketable
//    part of a LIMIT) does not execute — MARKET/IOC/FOK are CANCELLED with
//    reason VENUE_NOT_TRADING, a LIMIT rests without executing (ahead_qty
//    = displayed level qty); resting orders are not consumed by
//    EXECUTE/CANCEL/ADD events observed while gated and the post-apply
//    crossing check is skipped. On the first event after which the venue is
//    open again (TRADING and not stale — the auction uncross / snapshot
//    recovery), every resting order crossed by the post-event opposite best
//    fills in full at the TOUCH (uncross) price, not at its limit.
// 9. Event processing order (pinned): expiries, then activations and
//    cancel arrivals merged by time, then passive queue tracking on the raw
//    event, then the book update, then the overlay reset, then the
//    post-apply crossing check.

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <tuple>
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
enum class CancelReason : std::uint8_t {
    NONE = 0,                // not cancelled
    UNFILLED_REMAINDER = 1,  // MARKET/IOC remainder, FOK miss
    VENUE_NOT_TRADING = 2,   // rule 8: arrived while halted/auction/stale
    USER = 3,                // cancel() arrived (rule 7)
    EXPIRED = 4,             // time-in-force (expire_ts)
    END_OF_STREAM = 5,       // cancel_all()
};

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

// Instrument reference data the simulator needs. qty_unit = real base
// units per qty unit: lot_size for FX (1 qty unit = 1,000 base ccy), 1 for
// EQUITY/ETF whose qty is already in shares (conventions section 1).
struct InstrumentSpec {
    std::uint32_t instrument_id = 0;
    double tick_size = 0.0;
    double qty_unit = 1.0;
    double adv = 1.0;  // average daily volume, base units
    std::string quote_ccy = "USD";
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
    std::int64_t expire_ts = 0;  // 0 = good till cancelled (rule 7)
    // ---- simulator-owned runtime state ----
    std::int64_t arrival_ts = 0;
    OrderState state = OrderState::PENDING;
    std::int64_t remaining = 0;
    std::int64_t ahead_qty = 0;  // displayed qty ahead of us at our level
    bool resting = false;
    bool cross_exempt = false;   // see the crossing-rule exemption above
    CancelReason cancel_reason = CancelReason::NONE;
    std::int64_t cancel_arrival_ts = 0;  // 0 = no cancel in flight
};

// Named counters (conventions section 8: every drop is counted).
struct ExecCounters {
    std::uint64_t venue_not_trading_cancels = 0;  // rule 8 aggressive arrivals
    std::uint64_t expired_orders = 0;             // rule 7 time-in-force
    std::uint64_t user_cancels = 0;               // rule 7 cancel arrivals
    std::uint64_t reopen_touch_fills = 0;         // rule 8 uncross fills
    std::uint64_t overlay_thinned_fills = 0;      // rule 3b: a walk saw consumed liquidity
};

class ExecutionSimulator {
public:
    explicit ExecutionSimulator(const ExecConfig& config);

    // Submit a child order (decision time semantics per pinned rule 1).
    // Returns the assigned order_id.
    std::uint64_t submit(const ChildOrder& child);

    // Request a cancel at decision time `cancel_ts` (rule 7: latency path;
    // no-op for terminal states; throws on an unknown id).
    void cancel(std::uint64_t order_id, std::int64_t cancel_ts);

    // Process one market event (rule 9 order).
    void on_event(const MarketEvent& ev);

    // Cancel every non-terminal order (end of stream, immediate).
    void cancel_all();

    // True when the venue book exists, is not stale and is TRADING (rule 8).
    static bool venue_open(const OrderBook* book);

    const ExecCounters& counters() const { return counters_; }
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
    void terminate(ChildOrder& o, CancelReason reason);
    void apply_cancel_arrival(ChildOrder& o);
    void expire_due(std::int64_t t);
    void activate_and_cancel_due(std::int64_t t);
    void crossing_check(const MarketEvent& ev, const OrderBook& book,
                        bool reopened);
    std::int64_t consumed_at(std::uint32_t instrument_id,
                             std::uint16_t venue_id, std::uint8_t side,
                             std::int64_t price_ticks) const;
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
    std::vector<std::uint64_t> cancels_;  // sorted by (effective ts, order_id)
    // Rule 3b overlay: (instrument, venue, side, price) -> consumed qty.
    std::map<std::tuple<std::uint32_t, std::uint16_t, std::uint8_t,
                        std::int64_t>, std::int64_t> consumed_;
    ExecCounters counters_;
    std::vector<Fill> fills_;
    std::uint64_t next_order_id_ = 1;
    std::uint64_t next_fill_id_ = 1;
};

}  // namespace iap
