// Event-driven native feature engine (API_FEATURES.md; conventions section 6).
//
// Implements the pinned native core set of 40 features plus the 8 extra
// registry features the production alphas read (ofi_norm_l{1,5}_w{1s,5s,30s},
// ret_vol_adj_10s_v1, vol_regime_ratio_v1) with semantics identical to the
// Python reference engine (python/src/iap/features/) — the golden checkpoints
// in tests/golden/expected_features.json must match at abs/rel 1e-9.
//
// This port exposes the documented 48-slot sub-vector indexed by registry
// names (API_FEATURES.md section 1: "golden comparisons are by feature
// name"); slots not implemented natively are simply absent from the
// sub-vector. Order below is pinned by the Feature enum.
//
// State-update semantics (pinned, section 2 of API_FEATURES.md):
// - an exchange_ts below the instrument's last seen exchange_ts (cross-venue
//   clock skew) is dropped + counted before the book sees it;
// - events are applied to the per-venue books of a ConsolidatedBook; ONLY
//   events the book reports APPLIED feed rolling state (duplicates, invalid
//   sides, malformed payloads and events dropped while stale contribute
//   nothing);
// - the merged top-10 view is refreshed after APPLIED book-touching events
//   (ADD/MODIFY/CANCEL/EXECUTE/QUOTE and the final record of a SNAPSHOT
//   burst, trade_id == 0) and after any event that changed the set of stale
//   venues (staleness refresh: view only, no samples), merging non-stale
//   venues in ascending venue_id;
// - a stale -> fresh recovery CLEARS every rolling window and history and
//   re-anchors warmup at the recovery timestamp (warmup_after_recovery);
// - windows are half-open (t - w, t] on exchange_ts;
// - mid-derived samples are recorded whenever the merged mid differs from the
//   last RECORDED sample; depth samples at every two-sided refresh; OFI
//   deltas at every refresh;
// - a windowed feature is invalid until t - warm_ts >= w (warmup);
// - every x / (y + EPS) ratio is invalid when y <= 0;
// - quantities above FEATURE_MAX_QTY are not folded into any window;
// - NaN never appears with valid == true (single value funnel).
//
// Hot path: preallocated rolling buffers and merge scratch; no per-event
// allocation after warmup (conventions section 8).

#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "iap/features/rolling.hpp"
#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

// Pinned windows (mirrors python iap.features.spec.WINDOW_NS).
constexpr std::int64_t NS_PER_SEC = 1'000'000'000;
constexpr std::int64_t W_1S = 1 * NS_PER_SEC;
constexpr std::int64_t W_5S = 5 * NS_PER_SEC;
constexpr std::int64_t W_10S = 10 * NS_PER_SEC;
constexpr std::int64_t W_30S = 30 * NS_PER_SEC;
constexpr std::int64_t W_1M = 60 * NS_PER_SEC;
constexpr std::int64_t W_5M = 300 * NS_PER_SEC;

// Pinned EPS used in every guarded division (spec EPS = 1e-12).
constexpr double FEATURE_EPS = 1e-12;

// Largest quantity folded into a rolling window (API_FEATURES.md section 2.2):
// beyond this a feed is malformed, not a market, and an int64 window sum could
// no longer be exact in every port.
constexpr std::int64_t FEATURE_MAX_QTY = std::int64_t{1} << 40;

// The native feature slots, pinned order. Names (registry names, _v1) come
// from feature_name().
enum Feature : int {
    F_RET_SIMPLE_1S = 0,
    F_RET_LOG_1S,
    F_RET_LOG_10S,
    F_RET_LOG_1M,
    F_RET_VOL_ADJ_10S,       // extra (EQ06/FX09 input)
    F_MID_PRICE,
    F_MICROPRICE,
    F_MICRO_MID_DEV_BPS,
    F_SPREAD_TICKS,
    F_SPREAD_BPS,
    F_DEPTH_BID_L1,
    F_DEPTH_ASK_L1,
    F_DEPTH_BID_L5,
    F_DEPTH_ASK_L5,
    F_DEPTH_BID_L10,
    F_DEPTH_ASK_L10,
    F_IMBALANCE_L1,
    F_IMBALANCE_L3,
    F_IMBALANCE_L5,
    F_IMBALANCE_L10,
    F_OFI_L1_W1S,
    F_OFI_L1_W5S,
    F_OFI_L1_W30S,
    F_OFI_L3_W1S,
    F_OFI_L3_W5S,
    F_OFI_L3_W30S,
    F_OFI_L5_W1S,
    F_OFI_L5_W5S,
    F_OFI_L5_W30S,
    F_OFI_L10_W1S,
    F_OFI_L10_W5S,
    F_OFI_L10_W30S,
    F_OFI_NORM_L1_W1S,       // extra (EQ03 input)
    F_OFI_NORM_L1_W5S,       // extra
    F_OFI_NORM_L1_W30S,      // extra
    F_OFI_NORM_L5_W1S,       // extra (EQ03 input)
    F_OFI_NORM_L5_W5S,       // extra (EQ03 input)
    F_OFI_NORM_L5_W30S,      // extra
    F_SIGNED_VOLUME_W1S,
    F_SIGNED_VOLUME_W10S,
    F_SIGNED_VOLUME_W1M,
    F_TRADE_IMBALANCE_W1S,
    F_TRADE_IMBALANCE_W10S,
    F_TRADE_IMBALANCE_W1M,
    F_RVOL_W10S,
    F_RVOL_W1M,
    F_RVOL_W5M,
    F_VOL_REGIME_RATIO,      // extra (FX09 input)
    NUM_FEATURES,
};

// Registry name of a feature slot (e.g. "ofi_l5_w5s_v1").
const char* feature_name(int slot);

// slot for a registry name, or -1 when the name is not implemented natively.
int feature_index(const std::string& name);

// FeatureVector contract (48-slot documented sub-vector; API_FEATURES.md
// section 1). values[i] is finite whenever valid[i]; invalid slots carry NaN.
struct FeatureVector {
    std::uint32_t instrument_id = 0;
    std::int64_t timestamp = 0;
    std::array<double, NUM_FEATURES> values{};
    std::array<bool, NUM_FEATURES> valid{};
};

class FeatureEngine {
public:
    // instrument_id -> tick_size (real price per tick, reference data).
    // cadence_ns = 0 emits one vector after every event of the instrument;
    // otherwise at most one vector per instrument per cadence interval
    // (event time only — no wall clock).
    explicit FeatureEngine(const std::map<std::uint32_t, double>& tick_sizes,
                           std::int64_t cadence_ns = 0);

    // Apply one event. Returns true when a vector was emitted; the emitted
    // vector is written into `out` (owned by the caller; no allocation).
    bool apply(const MarketEvent& ev, FeatureVector& out);

    // Convenience: apply and copy the vector (allocation-friendly callers).
    void run(const std::vector<MarketEvent>& events,
             std::vector<FeatureVector>* emitted = nullptr);

    std::uint64_t events_processed() const { return events_processed_; }
    std::uint64_t vectors_emitted() const { return vectors_emitted_; }
    // Events the book dropped/held — never folded into rolling state.
    std::uint64_t events_dropped() const { return events_dropped_; }
    // Events dropped for an exchange_ts regression (fail closed).
    std::uint64_t ts_regressions_dropped() const {
        return ts_regressions_dropped_;
    }
    // Applied events whose qty exceeded FEATURE_MAX_QTY (not folded).
    std::uint64_t oversized_qty_dropped() const {
        return oversized_qty_dropped_;
    }
    // Refreshes whose merged depth exceeded FEATURE_MAX_QTY.
    std::uint64_t oversized_depth_skipped() const {
        return oversized_depth_skipped_;
    }
    // Stale->fresh recoveries of one instrument (rolling-state resets).
    std::uint64_t recoveries(std::uint32_t instrument_id) const;
    // Warmup anchor of one instrument (-1 when it has no events yet).
    std::int64_t warm_ts(std::uint32_t instrument_id) const;
    // True when the instrument's merged book is currently two-sided.
    bool book_ok(std::uint32_t instrument_id) const;

private:
    struct InstState {
        double tick = 0.0;
        ConsolidatedBook cons;
        std::int64_t first_ts = -1;
        // warmup anchor: first event, or the last stale->fresh recovery
        std::int64_t warm_ts = -1;
        std::int64_t last_ts = 0;
        std::uint64_t recoveries = 0;
        // sorted ids of this instrument's venues whose book is stale
        std::vector<std::uint16_t> stale_venues;
        std::int64_t last_emit = -1;
        // merged top-10 view (refreshed on book-touching events)
        bool book_ok = false;
        std::vector<LevelEntry> depth_bid, depth_ask;       // current
        std::vector<LevelEntry> prev_bid, prev_ask;         // scratch (prev)
        std::map<std::uint16_t,
                 std::pair<std::vector<LevelEntry>, std::vector<LevelEntry>>>
            venue_cache;
        std::vector<LevelEntry> merge_scratch;              // preallocated
        std::int64_t bid_p = 0, bid_q = 0, ask_p = 0, ask_q = 0;
        std::int64_t db1 = 0, db3 = 0, db5 = 0, db10 = 0;
        std::int64_t da1 = 0, da3 = 0, da5 = 0, da10 = 0;
        std::int64_t mid2 = 0;
        double mid = 0.0, logmid = 0.0;
        std::int64_t spread_ticks = 0;
        double spread_bps = 0.0;
        // rolling state
        TimeSeries<std::int64_t> hist2;    // mid2 at mid changes
        TimeSeries<double> histlog;        // ln(mid2) at mid changes
        RollingSum<double, 1> rv_10s{W_10S}, rv_1m{W_1M}, rv_5m{W_5M};
        RollingSum<std::int64_t, 4> ofi_1s{W_1S}, ofi_5s{W_5S}, ofi_30s{W_30S};
        RollingSum<std::int64_t, 4> depthavg_10s{W_10S};  // db1,da1,db5,da5
        RollingSum<std::int64_t, 3> tr_1s{W_1S}, tr_10s{W_10S}, tr_1m{W_1M};

        explicit InstState(std::uint32_t iid, double tick_size);
        bool warm(std::int64_t t, std::int64_t w) const {
            return warm_ts >= 0 && t - warm_ts >= w;
        }
        // Clear every rolling window / history; re-anchor warmup at t.
        void reset_rolling(std::int64_t t);
    };

    InstState& state(std::uint32_t instrument_id);
    // Returns true when the merged depth was oversized (view unusable).
    bool refresh_book(InstState& st, std::uint16_t venue_id, std::int64_t t,
                      bool just_recovered, bool samples);
    bool emit_if_due(InstState& st, std::uint32_t iid, std::int64_t t,
                     FeatureVector& out);
    void emit(InstState& st, std::uint32_t iid, std::int64_t t,
              FeatureVector& out) const;
    static std::int64_t depth_delta(const std::vector<LevelEntry>& prev,
                                    const std::vector<LevelEntry>& curr,
                                    int k);

    std::map<std::uint32_t, double> ticks_;
    std::int64_t cadence_ns_;
    std::map<std::uint32_t, InstState> states_;
    std::uint64_t events_processed_ = 0;
    std::uint64_t events_dropped_ = 0;
    std::uint64_t ts_regressions_dropped_ = 0;
    std::uint64_t oversized_qty_dropped_ = 0;
    std::uint64_t oversized_depth_skipped_ = 0;
    std::uint64_t vectors_emitted_ = 0;
};

}  // namespace iap
