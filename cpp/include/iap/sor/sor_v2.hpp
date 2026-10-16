// Cost-aware venue router (v1.12 X5 / v1.16 P0-4; v1.17 F7 part 1 C++ port).
//
// Hand-written, line-for-line port of iap.execution.sor_v2 (CostAwareRouter,
// RouterOptions, SweepChild, hazard_fill_prob) and of the venue model it
// routes on (iap.execution.venues_model: VenueModel, FeeTier, FeeLedger).
// Python stays the reference; this port reproduces
//   tests/golden/expected_f7_sor_sweep.json    (plan_sweep, rule 1)
//   tests/golden/expected_f7_sor_passive.json  (passive routing with fill
//                                               probability, rule 2)
// byte for byte (python/tools/make_golden_f7.py). The pinned router
// (sor.hpp, the fills golden) is untouched: eligibility is ITS rule
// (SmartOrderRouter::eligible: book present, not stale, TRADING, latency
// within max_venue_latency_ns), iteration is in ascending venue_id and every
// tie goes to the lower venue_id.
//
// Expected all-in costs, in bps of the consolidated mid, positive = cost
// (s = +1 buy / -1 sell, fees per share from the ledger's current tier,
// mid = 0.5 * (best_bid_ticks + best_ask_ticks), fee_bps = 1e4 * fee /
// (mid * tick)):
//
//   aggressive at v:  1e4 * s * (touch_v - mid) / mid + taker_bps_v
//                     + latency_penalty_bps_per_ms * latency_v_ms
//   passive at v:     p_v * (-rebate_bps_v - 1e4 * s * (mid - touch_v) / mid
//                            + toxicity_v)  +  (1 - p_v) * miss_cost_bps
//
// miss_cost_bps (nullopt = default) is half the consolidated spread in bps
// plus the mean taker fee (bps) of the venues quoting our side. p_v is the
// model's p_fill_touch unless a FillProb callback is given (v1.16 P0-4:
// queue-position aware, e.g. hazard_fill_prob over the X-4 execution
// depletion rate); a callback returning nullopt skips the venue.
//
// plan_sweep: a marketable order across the displayed depth (sweep_levels
// per venue) of every eligible venue quoting the opposite side, up to qty
// and the limit. sweep_rank "price": displayed price first (ties: lower
// taker fee, then lower venue_id); "all_in": by all-in price per share
// (buy: px * tick + taker; sell: -(px * tick) + taker, i.e. minus proceeds;
// ties: lower venue_id, then better price), so a fee can outweigh a tick.
// stagger "sync_arrival": send offset = max_latency - latency_v over the
// venues used (all children arrive together); "none": every offset 0.
//
// allocate_passive: weights p_v / (1 + max(toxicity_v, 0) / tox_scale_bps),
// venues with toxicity above max_toxicity_bps excluded; integer split in
// `lot` units by largest remainder (ties to the lower venue_id); the
// sub-lot remainder goes to the top-weight venue (v1.15).
//
// Tiered fees (FeeLedger): the tier of a fill is the one reached by the
// venue's volume in that UTC calendar month BEFORE the fill; the volume is
// credited after pricing. month_key = UTC YYYYMM of the fill timestamp.
//
// All arithmetic is in the Python evaluation order (left to right, the
// same operations), so the doubles are bit-identical.

#pragma once

#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "iap/execution/execution.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/sor/sor.hpp"

namespace iap {

constexpr const char* kDefaultToxHorizon = "1s";

// One fee tier: applies once the month's volume reaches min_monthly_shares.
struct FeeTier {
    std::int64_t min_monthly_shares = 0;
    double taker_fee_per_share = 0.0;
    double maker_rebate_per_share = 0.0;
};

// Router-facing model of one venue (iap.execution.venues_model.VenueModel).
struct VenueModel {
    VenueSpec spec;
    double p_fill_touch = 0.5;
    std::map<std::string, double> toxicity_bps;  // by markout horizon
    std::optional<std::int64_t> latency_ns;      // overrides spec.latency_mean_ns
    std::vector<FeeTier> tiers;                  // sorted, first at 0; empty = flat

    std::uint16_t venue_id() const { return spec.venue_id; }
    std::int64_t one_way_latency_ns() const {
        return latency_ns.has_value() ? *latency_ns : spec.latency_mean_ns;
    }
    double toxicity(const std::string& horizon = kDefaultToxHorizon) const {
        auto it = toxicity_bps.find(horizon);
        return it == toxicity_bps.end() ? 0.0 : it->second;
    }
    std::size_t tier_index(std::int64_t monthly_shares) const;
    // (taker fee, maker rebate) per share at that month-to-date volume.
    std::pair<double, double> fees(std::int64_t monthly_shares = 0) const;
    // Throws std::invalid_argument like the Python __post_init__.
    void validate() const;
};

// UTC YYYYMM of a nanosecond epoch timestamp (venues_model.month_key).
std::int64_t month_key(std::int64_t ts_ns);

// Monthly volume accumulator -> tier -> per-share fee / rebate.
class FeeLedger {
public:
    explicit FeeLedger(const std::map<std::uint16_t, VenueModel>& models)
        : models_(&models) {}
    std::int64_t monthly_volume(std::uint16_t venue_id, std::int64_t ts_ns) const;
    std::size_t tier(std::uint16_t venue_id, std::int64_t ts_ns) const;
    // (taker fee, maker rebate) per share for the NEXT fill at ts_ns.
    std::pair<double, double> quote(std::uint16_t venue_id, std::int64_t ts_ns) const;
    // Price one fill at the current tier, then credit its volume. Returns
    // the fee (negative = rebate received). Throws on qty <= 0.
    double record(std::uint16_t venue_id, std::int64_t qty, std::int64_t ts_ns,
                  bool maker);
    double total_fees() const { return total_fees_; }

private:
    const std::map<std::uint16_t, VenueModel>* models_;
    std::map<std::pair<std::uint16_t, std::int64_t>, std::int64_t> volume_;
    double total_fees_ = 0.0;
};

enum class StaggerMode : std::uint8_t { SYNC_ARRIVAL = 0, NONE = 1 };
enum class SweepRank : std::uint8_t { PRICE = 0, ALL_IN = 1 };

// Knobs of the cost-aware router (defaults = the Python defaults).
struct RouterOptions {
    std::string tox_horizon = kDefaultToxHorizon;
    double latency_penalty_bps_per_ms = 0.0;
    std::optional<double> miss_cost_bps;  // nullopt: half spread + mean taker fee
    StaggerMode stagger = StaggerMode::SYNC_ARRIVAL;
    SweepRank sweep_rank = SweepRank::PRICE;
    int sweep_levels = 10;
    double max_toxicity_bps = std::numeric_limits<double>::infinity();
    double tox_scale_bps = 1.0;
    std::int64_t lot = 1;

    // Throws std::invalid_argument like the Python __post_init__.
    void validate() const;
};

// One child of a sweep: send at decision_ts + send_offset_ns.
struct SweepChild {
    std::uint16_t venue_id = 0;
    std::int64_t price_ticks = 0;
    std::int64_t qty = 0;
    std::int64_t send_offset_ns = 0;
    std::int64_t arrival_offset_ns = 0;
};

// (venue_id, side, queue_qty_ahead) -> fill probability, nullopt = unknown.
using FillProb =
    std::function<std::optional<double>(std::uint16_t, std::uint8_t, std::int64_t)>;
// (venue_id, side, queue_qty_ahead) -> depletions per second, nullopt = unknown.
using HazardRate =
    std::function<std::optional<double>(std::uint16_t, std::uint8_t, std::int64_t)>;

// p = 1 - exp(-max(hazard, 0) * horizon_s); a nullopt hazard stays nullopt.
// Throws std::invalid_argument on horizon_s <= 0.
FillProb hazard_fill_prob(HazardRate hazard_per_s, double horizon_s);

class CostAwareRouter {
public:
    // `models` and `ledger` must outlive the router (the Python object owns
    // copies; here the golden test holds them). A null ledger means a fresh
    // ledger over `models`.
    CostAwareRouter(const std::map<std::uint16_t, VenueModel>& models,
                    RouterOptions options = RouterOptions{},
                    SorOptions sor_options = SorOptions{},
                    FeeLedger* ledger = nullptr);

    // Costs per eligible venue quoting the needed side (ascending venue_id).
    std::map<std::uint16_t, double> aggressive_costs(
        const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
        double tick, const std::vector<std::uint16_t>* candidates = nullptr) const;
    std::map<std::uint16_t, double> passive_costs(
        const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
        double tick, const std::vector<std::uint16_t>* candidates = nullptr,
        const FillProb* fill_prob = nullptr) const;

    // argmin of the costs (ties: lower venue_id); 0 = NO_ROUTE.
    std::uint16_t route_aggressive(
        const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
        double tick, const std::vector<std::uint16_t>* candidates = nullptr) const;
    std::uint16_t route_passive(
        const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
        double tick, const std::vector<std::uint16_t>* candidates = nullptr,
        const FillProb* fill_prob = nullptr) const;

    // Children across displayed depth (header). Throws std::invalid_argument
    // on qty <= 0, or on sweep_rank ALL_IN without a tick.
    std::vector<SweepChild> plan_sweep(
        const ConsolidatedBook& book, std::uint8_t side, std::int64_t qty,
        std::int64_t ts, std::optional<std::int64_t> limit_ticks = std::nullopt,
        const std::vector<std::uint16_t>* candidates = nullptr,
        std::optional<double> tick = std::nullopt) const;

    // Integer split of qty across venues quoting our side (header); venue
    // ids ascending, only venues with a positive allocation. Throws on qty <= 0.
    std::map<std::uint16_t, std::int64_t> allocate_passive(
        const ConsolidatedBook& book, std::uint8_t side, std::int64_t qty,
        const std::vector<std::uint16_t>* candidates = nullptr) const;

    RouterOptions& options() { return options_; }
    const RouterOptions& options() const { return options_; }
    FeeLedger& ledger() { return *ledger_; }

private:
    struct Touch {
        std::uint16_t vid;
        const OrderBook* book;
        std::int64_t px;
        std::int64_t qty;
    };
    std::vector<std::uint16_t> candidate_ids(
        const std::vector<std::uint16_t>* candidates) const;
    // Eligible venues (pinned rule) quoting `side_of_book`, ascending id.
    std::vector<Touch> touches(const ConsolidatedBook& book, std::uint8_t side_of_book,
                               const std::vector<std::uint16_t>* candidates) const;
    static std::optional<double> mid(const ConsolidatedBook& book);
    std::pair<double, double> fee_bps(std::uint16_t vid, std::int64_t ts,
                                      double mid_ticks, double tick) const;
    static std::uint16_t argmin(const std::map<std::uint16_t, double>& costs);

    const std::map<std::uint16_t, VenueModel>* models_;
    RouterOptions options_;
    FeeLedger own_ledger_;
    FeeLedger* ledger_;
    SmartOrderRouter gate_;
};

}  // namespace iap
