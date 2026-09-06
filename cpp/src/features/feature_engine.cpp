// Native feature engine implementation (see feature_engine.hpp for the
// pinned semantics; python/src/iap/features/engine.py is the reference).

#include "iap/features/feature_engine.hpp"

#include <cmath>
#include <stdexcept>

namespace iap {

namespace {

const char* const kFeatureNames[NUM_FEATURES] = {
    "ret_simple_1s_v1",
    "ret_log_1s_v1",
    "ret_log_10s_v1",
    "ret_log_1m_v1",
    "ret_vol_adj_10s_v1",
    "mid_price_v1",
    "microprice_v1",
    "micro_mid_dev_bps_v1",
    "spread_ticks_v1",
    "spread_bps_v1",
    "depth_bid_l1_v1",
    "depth_ask_l1_v1",
    "depth_bid_l5_v1",
    "depth_ask_l5_v1",
    "depth_bid_l10_v1",
    "depth_ask_l10_v1",
    "imbalance_l1_v1",
    "imbalance_l3_v1",
    "imbalance_l5_v1",
    "imbalance_l10_v1",
    "ofi_l1_w1s_v1",
    "ofi_l1_w5s_v1",
    "ofi_l1_w30s_v1",
    "ofi_l3_w1s_v1",
    "ofi_l3_w5s_v1",
    "ofi_l3_w30s_v1",
    "ofi_l5_w1s_v1",
    "ofi_l5_w5s_v1",
    "ofi_l5_w30s_v1",
    "ofi_l10_w1s_v1",
    "ofi_l10_w5s_v1",
    "ofi_l10_w30s_v1",
    "ofi_norm_l1_w1s_v1",
    "ofi_norm_l1_w5s_v1",
    "ofi_norm_l1_w30s_v1",
    "ofi_norm_l5_w1s_v1",
    "ofi_norm_l5_w5s_v1",
    "ofi_norm_l5_w30s_v1",
    "signed_volume_w1s_v1",
    "signed_volume_w10s_v1",
    "signed_volume_w1m_v1",
    "trade_imbalance_w1s_v1",
    "trade_imbalance_w10s_v1",
    "trade_imbalance_w1m_v1",
    "rvol_w10s_v1",
    "rvol_w1m_v1",
    "rvol_w5m_v1",
    "vol_regime_ratio_v1",
};

// History retention (mirrors the reference: 2x the longest lookback + margin).
constexpr std::int64_t kHistKeepNs = 660 * NS_PER_SEC;

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

}  // namespace

const char* feature_name(int slot) {
    if (slot < 0 || slot >= NUM_FEATURES) {
        throw std::invalid_argument("feature slot out of range");
    }
    return kFeatureNames[slot];
}

int feature_index(const std::string& name) {
    for (int i = 0; i < NUM_FEATURES; ++i) {
        if (name == kFeatureNames[i]) return i;
    }
    return -1;
}

FeatureEngine::InstState::InstState(std::uint32_t iid, double tick_size)
    : tick(tick_size), cons(iid) {
    depth_bid.reserve(DEPTH_LEVELS);
    depth_ask.reserve(DEPTH_LEVELS);
    prev_bid.reserve(DEPTH_LEVELS);
    prev_ask.reserve(DEPTH_LEVELS);
    merge_scratch.reserve(8 * DEPTH_LEVELS);
}

FeatureEngine::FeatureEngine(const std::map<std::uint32_t, double>& tick_sizes,
                             std::int64_t cadence_ns)
    : ticks_(tick_sizes), cadence_ns_(cadence_ns) {
    if (cadence_ns < 0) {
        throw std::invalid_argument("cadence_ns must be >= 0");
    }
}

FeatureEngine::InstState& FeatureEngine::state(std::uint32_t instrument_id) {
    auto it = states_.find(instrument_id);
    if (it != states_.end()) return it->second;
    auto tick_it = ticks_.find(instrument_id);
    if (tick_it == ticks_.end()) {
        throw std::invalid_argument("no tick size for instrument " +
                                    std::to_string(instrument_id));
    }
    auto res = states_.emplace(instrument_id,
                               InstState(instrument_id, tick_it->second));
    return res.first->second;
}

// Signed depth change within the best-k levels of one side (OFI building
// block): sum over the union of prev/curr top-k prices of (curr - prev)
// sizes, missing price => size 0. Mirrors engine._delta.
std::int64_t FeatureEngine::depth_delta(const std::vector<LevelEntry>& prev,
                                        const std::vector<LevelEntry>& curr,
                                        int k) {
    const std::size_t np = std::min<std::size_t>(prev.size(),
                                                 static_cast<std::size_t>(k));
    const std::size_t nc = std::min<std::size_t>(curr.size(),
                                                 static_cast<std::size_t>(k));
    std::int64_t d = 0;
    for (std::size_t i = 0; i < nc; ++i) {
        std::int64_t prev_q = 0;
        for (std::size_t j = 0; j < np; ++j) {
            if (prev[j].first == curr[i].first) {
                prev_q = prev[j].second;
                break;
            }
        }
        d += curr[i].second - prev_q;
    }
    for (std::size_t j = 0; j < np; ++j) {
        bool seen = false;
        for (std::size_t i = 0; i < nc; ++i) {
            if (curr[i].first == prev[j].first) {
                seen = true;
                break;
            }
        }
        if (!seen) d -= prev[j].second;
    }
    return d;
}

void FeatureEngine::InstState::reset_rolling(std::int64_t t) {
    warm_ts = t;
    ++recoveries;
    hist2 = TimeSeries<std::int64_t>();
    histlog = TimeSeries<double>();
    rv_10s = RollingSum<double, 1>(W_10S);
    rv_1m = RollingSum<double, 1>(W_1M);
    rv_5m = RollingSum<double, 1>(W_5M);
    ofi_1s = RollingSum<std::int64_t, 4>(W_1S);
    ofi_5s = RollingSum<std::int64_t, 4>(W_5S);
    ofi_30s = RollingSum<std::int64_t, 4>(W_30S);
    depthavg_10s = RollingSum<std::int64_t, 4>(W_10S);
    tr_1s = RollingSum<std::int64_t, 3>(W_1S);
    tr_10s = RollingSum<std::int64_t, 3>(W_10S);
    tr_1m = RollingSum<std::int64_t, 3>(W_1M);
    depth_bid.clear();
    depth_ask.clear();
    book_ok = false;
}

std::uint64_t FeatureEngine::recoveries(std::uint32_t instrument_id) const {
    auto it = states_.find(instrument_id);
    return it == states_.end() ? 0 : it->second.recoveries;
}

std::int64_t FeatureEngine::warm_ts(std::uint32_t instrument_id) const {
    auto it = states_.find(instrument_id);
    return it == states_.end() ? -1 : it->second.warm_ts;
}

bool FeatureEngine::book_ok(std::uint32_t instrument_id) const {
    auto it = states_.find(instrument_id);
    return it != states_.end() && it->second.book_ok;
}

// `samples == false` is a *staleness refresh*: the merged view and book_ok
// are recomputed because the stale-venue set changed, but no OFI / depth /
// mid sample is recorded.  Returns true when the merged depth was oversized.
bool FeatureEngine::refresh_book(InstState& st, std::uint16_t venue_id,
                                 std::int64_t t, bool just_recovered,
                                 bool samples) {
    if (just_recovered) st.reset_rolling(t);
    // Refresh this venue's cached top-10 depth.
    const auto& books = st.cons.books();
    auto vb = books.find(venue_id);
    if (vb != books.end()) {
        auto& cache = st.venue_cache[venue_id];
        cache.first.clear();
        cache.second.clear();
        vb->second.depth_into(Side::BID, DEPTH_LEVELS, cache.first);
        vb->second.depth_into(Side::ASK, DEPTH_LEVELS, cache.second);
    }

    // Save the previous merged view (swap: no allocation).
    st.prev_bid.swap(st.depth_bid);
    st.prev_ask.swap(st.depth_ask);

    // Merge non-stale venues (ascending venue_id; equal prices summed).
    auto merge_side = [&](bool is_bid, std::vector<LevelEntry>& out) {
        auto& scratch = st.merge_scratch;
        scratch.clear();
        for (const auto& [vid, cache] : st.venue_cache) {
            auto bit = books.find(vid);
            if (bit == books.end() || bit->second.stale()) continue;
            const auto& lvls = is_bid ? cache.first : cache.second;
            for (const auto& [p, q] : lvls) {
                bool found = false;
                for (auto& e : scratch) {
                    if (e.first == p) {
                        e.second += q;
                        found = true;
                        break;
                    }
                }
                if (!found) scratch.emplace_back(p, q);
            }
        }
        if (is_bid) {
            std::sort(scratch.begin(), scratch.end(),
                      [](const LevelEntry& a, const LevelEntry& b) {
                          return a.first > b.first;
                      });
        } else {
            std::sort(scratch.begin(), scratch.end(),
                      [](const LevelEntry& a, const LevelEntry& b) {
                          return a.first < b.first;
                      });
        }
        out.clear();
        const std::size_t n =
            std::min<std::size_t>(scratch.size(), DEPTH_LEVELS);
        for (std::size_t i = 0; i < n; ++i) out.push_back(scratch[i]);
    };
    merge_side(true, st.depth_bid);
    merge_side(false, st.depth_ask);

    // Oversized merged depth (pinned section 2.2): a level above
    // FEATURE_MAX_QTY makes the merged view unusable — clear it, record
    // nothing, and let the next clean refresh re-baseline.
    bool oversized = false;
    for (const auto& e : st.depth_bid) oversized |= e.second > FEATURE_MAX_QTY;
    for (const auto& e : st.depth_ask) oversized |= e.second > FEATURE_MAX_QTY;
    if (oversized) {
        st.depth_bid.clear();
        st.depth_ask.clear();
        st.book_ok = false;
        return true;
    }

    // OFI contributions per level count (defined book_ok or not); the sample
    // is skipped only when previous and current views are all empty, or on
    // the first refresh after a recovery (no previous depth).
    if (samples && !just_recovered &&
        (!st.prev_bid.empty() || !st.prev_ask.empty() ||
         !st.depth_bid.empty() || !st.depth_ask.empty())) {
        std::array<std::int64_t, 4> contribs{};
        const int ks[4] = {1, 3, 5, 10};
        for (int i = 0; i < 4; ++i) {
            contribs[static_cast<std::size_t>(i)] =
                depth_delta(st.prev_bid, st.depth_bid, ks[i]) -
                depth_delta(st.prev_ask, st.depth_ask, ks[i]);
        }
        st.ofi_1s.add(t, contribs);
        st.ofi_5s.add(t, contribs);
        st.ofi_30s.add(t, contribs);
    }

    st.book_ok = !st.depth_bid.empty() && !st.depth_ask.empty();
    if (!st.book_ok || !samples) return false;

    st.bid_p = st.depth_bid[0].first;
    st.bid_q = st.depth_bid[0].second;
    st.ask_p = st.depth_ask[0].first;
    st.ask_q = st.depth_ask[0].second;
    auto side_sums = [](const std::vector<LevelEntry>& d, std::int64_t& s1,
                        std::int64_t& s3, std::int64_t& s5, std::int64_t& s10) {
        s1 = s3 = s5 = s10 = 0;
        for (std::size_t i = 0; i < d.size(); ++i) {
            const std::int64_t q = d[i].second;
            if (i < 1) s1 += q;
            if (i < 3) s3 += q;
            if (i < 5) s5 += q;
            s10 += q;
        }
    };
    side_sums(st.depth_bid, st.db1, st.db3, st.db5, st.db10);
    side_sums(st.depth_ask, st.da1, st.da3, st.da5, st.da10);
    st.mid2 = st.bid_p + st.ask_p;
    st.mid = static_cast<double>(st.mid2) * st.tick / 2.0;
    st.logmid = std::log(static_cast<double>(st.mid2));
    st.spread_ticks = st.ask_p - st.bid_p;
    st.spread_bps =
        static_cast<double>(st.spread_ticks) * st.tick / st.mid * 1e4;

    // Depth sample at every two-sided refresh (ofi_norm denominator inputs).
    st.depthavg_10s.add(t, {st.db1, st.da1, st.db5, st.da5});

    // Mid-change samples (returns / realized-vol inputs), compared against
    // the last RECORDED sample (pinned): a one-sided flicker that moves the
    // mid still yields a vol sample; a flicker back to the same mid does not.
    const bool has_last = !st.hist2.empty();
    if (!has_last || st.hist2.last() != st.mid2) {
        if (has_last) {
            const double dlm = st.logmid - st.histlog.last();
            const std::array<double, 1> sq{dlm * dlm};
            st.rv_10s.add(t, sq);
            st.rv_1m.add(t, sq);
            st.rv_5m.add(t, sq);
        }
        st.hist2.append(t, st.mid2);
        st.histlog.append(t, st.logmid);
    }
    return false;
}

bool FeatureEngine::apply(const MarketEvent& ev, FeatureVector& out) {
    InstState& st = state(ev.instrument_id);
    const std::int64_t t = ev.exchange_ts;
    if (st.first_ts >= 0 && t < st.last_ts) {
        // Cross-venue exchange_ts regression: dropped + counted before the
        // book sees it (pinned). Never thrown, never re-ordered.
        ++ts_regressions_dropped_;
        ++events_dropped_;
        ++events_processed_;
        return false;
    }
    const ApplyStatus status = st.cons.apply(ev);
    if (st.first_ts < 0) {
        st.first_ts = t;
        st.warm_ts = t;
    }
    st.last_ts = t;
    ++events_processed_;

    // The merged view is a function of WHICH venues are stale, so the trigger
    // is a change of the stale SET, not of "any venue is stale".
    std::vector<std::uint16_t> stale_now;
    for (const auto& [vid, book] : st.cons.books()) {
        if (book.stale()) stale_now.push_back(vid);
    }
    const bool stale_changed = stale_now != st.stale_venues;
    const bool just_recovered = !st.stale_venues.empty() && stale_now.empty();
    st.stale_venues.swap(stale_now);

    if (status != ApplyStatus::APPLIED) {
        ++events_dropped_;
        if (stale_changed &&
            refresh_book(st, ev.venue_id, t, just_recovered, false)) {
            ++oversized_depth_skipped_;
        }
        return emit_if_due(st, ev.instrument_id, t, out);
    }

    const auto et = static_cast<EventType>(ev.event_type);
    if (ev.qty > FEATURE_MAX_QTY) {
        // Oversized quantity (pinned section 2.2): the book may hold it, but
        // no rolling window folds it in — an int64 window sum stays exact.
        ++oversized_qty_dropped_;
    } else if (et == EventType::TRADE) {
        // signed / buy / sell traded quantity (side BID = buy aggressor).
        const std::int64_t buy = ev.side == 0 ? ev.qty : 0;
        const std::int64_t sell = ev.qty - buy;
        const std::array<std::int64_t, 3> vals{buy - sell, buy, sell};
        st.tr_1s.add(t, vals);
        st.tr_10s.add(t, vals);
        st.tr_1m.add(t, vals);
    }

    const bool book_touch =
        et == EventType::ADD || et == EventType::MODIFY ||
        et == EventType::CANCEL || et == EventType::EXECUTE ||
        et == EventType::QUOTE ||
        (et == EventType::SNAPSHOT && ev.trade_id == 0);
    if (book_touch) {
        if (refresh_book(st, ev.venue_id, t, just_recovered, true)) {
            ++oversized_depth_skipped_;
        }
    } else if (stale_changed) {
        if (refresh_book(st, ev.venue_id, t, just_recovered, false)) {
            ++oversized_depth_skipped_;
        }
    }

    return emit_if_due(st, ev.instrument_id, t, out);
}

bool FeatureEngine::emit_if_due(InstState& st, std::uint32_t iid,
                                std::int64_t t, FeatureVector& out) {
    if (cadence_ns_ == 0 || st.last_emit < 0 ||
        t - st.last_emit >= cadence_ns_) {
        // Evict expired samples from every window at emission time.
        st.rv_10s.trim(t);
        st.rv_1m.trim(t);
        st.rv_5m.trim(t);
        st.ofi_1s.trim(t);
        st.ofi_5s.trim(t);
        st.ofi_30s.trim(t);
        st.depthavg_10s.trim(t);
        st.tr_1s.trim(t);
        st.tr_10s.trim(t);
        st.tr_1m.trim(t);
        st.hist2.trim(t - kHistKeepNs);
        st.histlog.trim(t - kHistKeepNs);
        emit(st, iid, t, out);
        st.last_emit = t;
        ++vectors_emitted_;
        return true;
    }
    return false;
}

void FeatureEngine::run(const std::vector<MarketEvent>& events,
                        std::vector<FeatureVector>* emitted) {
    FeatureVector vec;
    for (const auto& ev : events) {
        if (apply(ev, vec) && emitted != nullptr) emitted->push_back(vec);
    }
}

void FeatureEngine::emit(InstState& st, std::uint32_t iid, std::int64_t t,
                         FeatureVector& out) const {
    out.instrument_id = iid;
    out.timestamp = t;
    out.values.fill(kNaN);
    out.valid.fill(false);

    // Single value funnel (API_FEATURES.md section 1): a non-finite value can
    // never be emitted with valid == true.
    auto put = [&](int slot, double v, bool ok) {
        const bool good = ok && std::isfinite(v);
        out.values[static_cast<std::size_t>(slot)] = good ? v : kNaN;
        out.valid[static_cast<std::size_t>(slot)] = good;
    };

    const bool ok = st.book_ok;

    // ---- price family: returns from at-or-before mid-change samples -----
    std::int64_t past2 = 0;
    double pastlog = 0.0;
    const bool has_1s = ok && st.hist2.at_or_before(t - W_1S, past2);
    put(F_RET_SIMPLE_1S,
        has_1s ? static_cast<double>(st.mid2) / static_cast<double>(past2) - 1.0
               : 0.0,
        has_1s);
    double ret_log_10s = 0.0;
    bool has_log_10s = false;
    {
        const struct {
            int slot;
            std::int64_t h;
        } horizons[3] = {{F_RET_LOG_1S, W_1S},
                         {F_RET_LOG_10S, W_10S},
                         {F_RET_LOG_1M, W_1M}};
        for (const auto& hz : horizons) {
            const bool has = ok && st.histlog.at_or_before(t - hz.h, pastlog);
            const double v = has ? st.logmid - pastlog : 0.0;
            put(hz.slot, v, has);
            if (hz.slot == F_RET_LOG_10S) {
                ret_log_10s = v;
                has_log_10s = has;
            }
        }
    }

    // realized vol (per sqrt-second); valid once the window is warm.
    auto rvol = [&](const RollingSum<double, 1>& win, std::int64_t w,
                    double& v) {
        if (!st.warm(t, w)) return false;
        v = std::sqrt(std::max(win.sum(0), 0.0) /
                      (static_cast<double>(w) / 1e9));
        return true;
    };
    double rv10 = 0.0, rv1m = 0.0, rv5m = 0.0;
    const bool has_rv10 = rvol(st.rv_10s, W_10S, rv10);
    const bool has_rv1m = rvol(st.rv_1m, W_1M, rv1m);
    const bool has_rv5m = rvol(st.rv_5m, W_5M, rv5m);
    put(F_RVOL_W10S, rv10, has_rv10);
    put(F_RVOL_W1M, rv1m, has_rv1m);
    put(F_RVOL_W5M, rv5m, has_rv5m);

    // EPS guard (section 4): the denominator is undefined when the vol
    // window holds no mid-change SAMPLE (exact integer count, not
    // `rvol > 0`: a float sum drifts and the test would flip per language).
    const bool has_rva = has_log_10s && has_rv1m && st.rv_1m.count() > 0;
    put(F_RET_VOL_ADJ_10S, has_rva ? ret_log_10s / (rv1m + FEATURE_EPS) : 0.0,
        has_rva);
    const bool has_vrr = has_rv1m && has_rv5m && st.rv_5m.count() > 0;
    put(F_VOL_REGIME_RATIO, has_vrr ? rv1m / (rv5m + FEATURE_EPS) : 0.0,
        has_vrr);

    // ---- microstructure ---------------------------------------------------
    put(F_MID_PRICE, st.mid, ok);
    const bool has_micro = ok && (st.bid_q + st.ask_q) > 0;
    double micro = 0.0;
    if (has_micro) {
        micro = (static_cast<double>(st.bid_p) * static_cast<double>(st.ask_q) +
                 static_cast<double>(st.ask_p) * static_cast<double>(st.bid_q)) /
                static_cast<double>(st.bid_q + st.ask_q) * st.tick;
    }
    put(F_MICROPRICE, micro, has_micro);
    const bool has_dev = has_micro && st.mid != 0.0;
    put(F_MICRO_MID_DEV_BPS, has_dev ? (micro - st.mid) / st.mid * 1e4 : 0.0,
        has_dev);
    put(F_SPREAD_TICKS, static_cast<double>(st.spread_ticks), ok);
    put(F_SPREAD_BPS, st.spread_bps, ok);

    put(F_DEPTH_BID_L1, static_cast<double>(st.db1), ok);
    put(F_DEPTH_ASK_L1, static_cast<double>(st.da1), ok);
    put(F_DEPTH_BID_L5, static_cast<double>(st.db5), ok);
    put(F_DEPTH_ASK_L5, static_cast<double>(st.da5), ok);
    put(F_DEPTH_BID_L10, static_cast<double>(st.db10), ok);
    put(F_DEPTH_ASK_L10, static_cast<double>(st.da10), ok);

    const struct {
        int slot;
        std::int64_t b, a;
    } imbs[4] = {{F_IMBALANCE_L1, st.db1, st.da1},
                 {F_IMBALANCE_L3, st.db3, st.da3},
                 {F_IMBALANCE_L5, st.db5, st.da5},
                 {F_IMBALANCE_L10, st.db10, st.da10}};
    for (const auto& im : imbs) {
        const bool has = ok && (im.b + im.a) > 0;
        put(im.slot,
            has ? static_cast<double>(im.b - im.a) /
                      static_cast<double>(im.b + im.a)
                : 0.0,
            has);
    }

    // ---- order flow -------------------------------------------------------
    const RollingSum<std::int64_t, 4>* ofis[3] = {&st.ofi_1s, &st.ofi_5s,
                                                  &st.ofi_30s};
    const std::int64_t ofi_w[3] = {W_1S, W_5S, W_30S};
    for (int ki = 0; ki < 4; ++ki) {           // k in {1, 3, 5, 10}
        for (int wi = 0; wi < 3; ++wi) {       // w in {1s, 5s, 30s}
            const bool warm = st.warm(t, ofi_w[wi]);
            put(F_OFI_L1_W1S + ki * 3 + wi,
                warm ? static_cast<double>(
                           ofis[wi]->sum(static_cast<std::size_t>(ki)))
                     : 0.0,
                warm);
        }
    }
    // ofi_norm_l{1,5}: ofi / (mean two-sided depth over 10s + EPS).
    const bool davg_ok =
        st.warm(t, W_10S) && st.depthavg_10s.count() > 0;
    for (int ni = 0; ni < 2; ++ni) {           // k in {1, 5}
        const std::size_t ofi_idx = ni == 0 ? 0 : 2;  // l1 -> 0, l5 -> 2
        const double denom_sum =
            ni == 0 ? static_cast<double>(st.depthavg_10s.sum(0) +
                                          st.depthavg_10s.sum(1))
                    : static_cast<double>(st.depthavg_10s.sum(2) +
                                          st.depthavg_10s.sum(3));
        const double denom =
            davg_ok ? denom_sum / static_cast<double>(st.depthavg_10s.count())
                    : 0.0;
        const bool depth_ok =
            davg_ok && (ni == 0 ? st.depthavg_10s.sum(0) + st.depthavg_10s.sum(1)
                                : st.depthavg_10s.sum(2) + st.depthavg_10s.sum(3)) > 0;
        for (int wi = 0; wi < 3; ++wi) {
            // Exact INTEGER guard: a float `> 0` test would flip between
            // languages on accumulation drift.
            const bool has = st.warm(t, ofi_w[wi]) && depth_ok;
            const double v =
                has ? static_cast<double>(ofis[wi]->sum(ofi_idx)) /
                          (denom + FEATURE_EPS)
                    : 0.0;
            put(F_OFI_NORM_L1_W1S + ni * 3 + wi, v, has);
        }
    }

    const RollingSum<std::int64_t, 3>* trs[3] = {&st.tr_1s, &st.tr_10s,
                                                 &st.tr_1m};
    const std::int64_t tr_w[3] = {W_1S, W_10S, W_1M};
    for (int wi = 0; wi < 3; ++wi) {
        const bool warm = st.warm(t, tr_w[wi]);
        put(F_SIGNED_VOLUME_W1S + wi,
            warm ? static_cast<double>(trs[wi]->sum(0)) : 0.0, warm);
        const std::int64_t tot = trs[wi]->sum(1) + trs[wi]->sum(2);
        const bool has = warm && tot > 0;
        put(F_TRADE_IMBALANCE_W1S + wi,
            has ? static_cast<double>(trs[wi]->sum(1) - trs[wi]->sum(2)) /
                      static_cast<double>(tot)
                : 0.0,
            has);
    }
}

}  // namespace iap
