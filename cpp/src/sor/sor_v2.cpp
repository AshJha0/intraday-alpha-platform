// Cost-aware router: line-for-line port of iap.execution.sor_v2 (header).

#include "iap/sor/sor_v2.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <stdexcept>

namespace iap {

// ------------------------------------------------------------ venue model

std::size_t VenueModel::tier_index(std::int64_t monthly_shares) const {
    std::size_t idx = 0;
    for (std::size_t i = 0; i < tiers.size(); ++i) {
        if (monthly_shares >= tiers[i].min_monthly_shares) idx = i;
    }
    return idx;
}

std::pair<double, double> VenueModel::fees(std::int64_t monthly_shares) const {
    if (tiers.empty()) {
        return {spec.taker_fee_per_share, spec.maker_rebate_per_share};
    }
    const FeeTier& t = tiers[tier_index(monthly_shares)];
    return {t.taker_fee_per_share, t.maker_rebate_per_share};
}

void VenueModel::validate() const {
    if (!(p_fill_touch >= 0.0 && p_fill_touch <= 1.0)) {
        throw std::invalid_argument("venue " + std::to_string(spec.venue_id) +
                                    ": p_fill_touch must be in [0, 1]");
    }
    for (std::size_t i = 0; i < tiers.size(); ++i) {
        const bool bad = (i == 0 && tiers[i].min_monthly_shares != 0) ||
                         (i > 0 && tiers[i].min_monthly_shares <=
                                       tiers[i - 1].min_monthly_shares) ||
                         tiers[i].min_monthly_shares < 0;
        if (bad) {
            throw std::invalid_argument(
                "venue " + std::to_string(spec.venue_id) +
                ": tiers must start at 0 and strictly increase");
        }
    }
}

std::int64_t month_key(std::int64_t ts_ns) {
    // Python: datetime.fromtimestamp(ts_ns // 1e9, tz=UTC) -> year * 100 + month
    // (floor division; proleptic Gregorian, Howard Hinnant's civil_from_days).
    std::int64_t secs = ts_ns / 1'000'000'000;
    if (ts_ns % 1'000'000'000 < 0) --secs;
    std::int64_t days = secs / 86'400;
    if (secs % 86'400 < 0) --days;
    const std::int64_t z = days + 719'468;
    const std::int64_t era = (z >= 0 ? z : z - 146'096) / 146'097;
    const std::int64_t doe = z - era * 146'097;
    const std::int64_t yoe = (doe - doe / 1460 + doe / 36'524 - doe / 146'096) / 365;
    const std::int64_t y = yoe + era * 400;
    const std::int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const std::int64_t mp = (5 * doy + 2) / 153;
    const std::int64_t m = mp < 10 ? mp + 3 : mp - 9;
    const std::int64_t year = y + (m <= 2 ? 1 : 0);
    return year * 100 + m;
}

std::int64_t FeeLedger::monthly_volume(std::uint16_t venue_id,
                                       std::int64_t ts_ns) const {
    auto it = volume_.find({venue_id, month_key(ts_ns)});
    return it == volume_.end() ? 0 : it->second;
}

std::size_t FeeLedger::tier(std::uint16_t venue_id, std::int64_t ts_ns) const {
    return models_->at(venue_id).tier_index(monthly_volume(venue_id, ts_ns));
}

std::pair<double, double> FeeLedger::quote(std::uint16_t venue_id,
                                           std::int64_t ts_ns) const {
    return models_->at(venue_id).fees(monthly_volume(venue_id, ts_ns));
}

double FeeLedger::record(std::uint16_t venue_id, std::int64_t qty,
                         std::int64_t ts_ns, bool maker) {
    if (qty <= 0) throw std::invalid_argument("fill qty must be > 0");
    const auto [taker, rebate] = quote(venue_id, ts_ns);
    const double fee = (maker ? -rebate : taker) * static_cast<double>(qty);
    volume_[{venue_id, month_key(ts_ns)}] += qty;
    total_fees_ += fee;
    return fee;
}

// ----------------------------------------------------------------- options

void RouterOptions::validate() const {
    if (sweep_levels <= 0 || lot <= 0 || tox_scale_bps <= 0.0) {
        throw std::invalid_argument("sweep_levels, lot and tox_scale_bps must be > 0");
    }
}

FillProb hazard_fill_prob(HazardRate hazard_per_s, double horizon_s) {
    if (horizon_s <= 0.0) {
        throw std::invalid_argument("hazard_fill_prob: horizon_s must be > 0");
    }
    return [hazard_per_s = std::move(hazard_per_s), horizon_s](
               std::uint16_t vid, std::uint8_t side,
               std::int64_t q) -> std::optional<double> {
        const std::optional<double> lam = hazard_per_s(vid, side, q);
        if (!lam.has_value()) return std::nullopt;
        return 1.0 - std::exp(-std::max(*lam, 0.0) * horizon_s);
    };
}

// ------------------------------------------------------------------ router

namespace {

std::map<std::uint16_t, VenueSpec> specs_of(
    const std::map<std::uint16_t, VenueModel>& models) {
    std::map<std::uint16_t, VenueSpec> out;
    for (const auto& [vid, m] : models) out[vid] = m.spec;
    return out;
}

}  // namespace

CostAwareRouter::CostAwareRouter(const std::map<std::uint16_t, VenueModel>& models,
                                 RouterOptions options, SorOptions sor_options,
                                 FeeLedger* ledger)
    : models_(&models),
      options_(std::move(options)),
      own_ledger_(models),
      ledger_(ledger != nullptr ? ledger : &own_ledger_),
      gate_(specs_of(models), sor_options) {
    options_.validate();
    for (const auto& [vid, m] : models) {
        (void)vid;
        m.validate();
    }
}

std::vector<std::uint16_t> CostAwareRouter::candidate_ids(
    const std::vector<std::uint16_t>* candidates) const {
    std::vector<std::uint16_t> c;
    if (candidates == nullptr) {
        for (const auto& [vid, m] : *models_) {
            (void)m;
            c.push_back(vid);
        }
    } else {
        c = *candidates;
        std::sort(c.begin(), c.end());
    }
    if (c.empty()) throw std::invalid_argument("router: empty candidate venue list");
    return c;
}

std::vector<CostAwareRouter::Touch> CostAwareRouter::touches(
    const ConsolidatedBook& book, std::uint8_t side_of_book,
    const std::vector<std::uint16_t>* candidates) const {
    std::vector<Touch> out;
    for (const std::uint16_t vid : candidate_ids(candidates)) {
        if (models_->find(vid) == models_->end()) continue;
        const OrderBook* vb = gate_.eligible(book, vid);
        if (vb == nullptr) continue;
        const std::optional<LevelEntry> q =
            side_of_book == 0 ? vb->best_bid() : vb->best_ask();
        if (q.has_value()) out.push_back({vid, vb, q->first, q->second});
    }
    return out;
}

namespace {

// The consolidated best PRICE on `side`: the best of the per-venue bests
// over the non-stale venues — the price ConsolidatedBook::best_bid /
// best_ask report (their merged() aggregates every level of every venue
// into a map, O(levels) per call: on the real slice's ~2,700-level book
// that put the passive route at ~400 us; the router needs only the touch
// prices, which per-venue bests give in O(venues)). Same value, same rule
// (non-stale venues only, whatever their status).
std::optional<std::int64_t> consolidated_touch(const ConsolidatedBook& book, Side side) {
    std::optional<std::int64_t> best;
    for (const auto& [vid, vb] : book.books()) {
        (void)vid;
        if (vb.stale()) continue;
        const std::optional<LevelEntry> q = side == Side::BID ? vb.best_bid() : vb.best_ask();
        if (!q.has_value()) continue;
        if (!best.has_value() || (side == Side::BID ? q->first > *best : q->first < *best)) {
            best = q->first;
        }
    }
    return best;
}

}  // namespace

std::optional<double> CostAwareRouter::mid(const ConsolidatedBook& book) {
    const auto bb = consolidated_touch(book, Side::BID);
    const auto ba = consolidated_touch(book, Side::ASK);
    if (!bb.has_value() || !ba.has_value()) return std::nullopt;
    // Python: 0.5 * (bb[0] + ba[0]) — the integer sum, then one multiply.
    return 0.5 * static_cast<double>(*bb + *ba);
}

std::pair<double, double> CostAwareRouter::fee_bps(std::uint16_t vid,
                                                   std::int64_t ts,
                                                   double mid_ticks,
                                                   double tick) const {
    const auto [taker, rebate] = ledger_->quote(vid, ts);
    const double px = mid_ticks * tick;
    return {1e4 * taker / px, 1e4 * rebate / px};
}

std::map<std::uint16_t, double> CostAwareRouter::aggressive_costs(
    const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
    double tick, const std::vector<std::uint16_t>* candidates) const {
    std::map<std::uint16_t, double> out;
    const std::optional<double> m = mid(book);
    if (!m.has_value()) return out;
    const double s = side == 0 ? 1.0 : -1.0;
    const RouterOptions& o = options_;
    for (const Touch& t : touches(book, side == 0 ? 1 : 0, candidates)) {
        const double taker_bps = fee_bps(t.vid, ts, *m, tick).first;
        const double lat_ms =
            static_cast<double>(models_->at(t.vid).one_way_latency_ns()) / 1e6;
        out[t.vid] = 1e4 * s * (static_cast<double>(t.px) - *m) / *m + taker_bps +
                     o.latency_penalty_bps_per_ms * lat_ms;
    }
    return out;
}

std::map<std::uint16_t, double> CostAwareRouter::passive_costs(
    const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
    double tick, const std::vector<std::uint16_t>* candidates,
    const FillProb* fill_prob) const {
    std::map<std::uint16_t, double> out;
    const std::optional<double> m = mid(book);
    if (!m.has_value()) return out;
    const double s = side == 0 ? 1.0 : -1.0;
    const RouterOptions& o = options_;
    const std::vector<Touch> tch = touches(book, side, candidates);
    double miss = 0.0;
    if (!o.miss_cost_bps.has_value()) {
        double fees_sum = 0.0;  // Python sum(): 0 + f_1 + f_2 ... in order
        for (const Touch& t : tch) fees_sum += fee_bps(t.vid, ts, *m, tick).first;
        const double half =
            1e4 * 0.5 *
            static_cast<double>(*consolidated_touch(book, Side::ASK) -
                                *consolidated_touch(book, Side::BID)) / *m;
        miss = half + (tch.empty() ? 0.0 : fees_sum / static_cast<double>(tch.size()));
    } else {
        miss = *o.miss_cost_bps;
    }
    for (const Touch& t : tch) {
        const VenueModel& vm = models_->at(t.vid);
        std::optional<double> p;
        if (fill_prob == nullptr) {
            p = vm.p_fill_touch;
        } else {
            p = (*fill_prob)(t.vid, side, t.qty);
        }
        if (!p.has_value()) continue;
        const double rebate_bps = fee_bps(t.vid, ts, *m, tick).second;
        const double capture = 1e4 * s * (*m - static_cast<double>(t.px)) / *m;
        const double fill_cost = -rebate_bps - capture + vm.toxicity(o.tox_horizon);
        out[t.vid] = *p * fill_cost + (1.0 - *p) * miss;
    }
    return out;
}

std::uint16_t CostAwareRouter::argmin(const std::map<std::uint16_t, double>& costs) {
    // Python: min(sorted(costs), key=...) — the first (lowest id) minimum.
    std::uint16_t best = 0;
    double best_cost = 0.0;
    bool have = false;
    for (const auto& [vid, c] : costs) {
        if (!have || c < best_cost) {
            have = true;
            best = vid;
            best_cost = c;
        }
    }
    return best;
}

std::uint16_t CostAwareRouter::route_aggressive(
    const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
    double tick, const std::vector<std::uint16_t>* candidates) const {
    return argmin(aggressive_costs(book, side, ts, tick, candidates));
}

std::uint16_t CostAwareRouter::route_passive(
    const ConsolidatedBook& book, std::uint8_t side, std::int64_t ts,
    double tick, const std::vector<std::uint16_t>* candidates,
    const FillProb* fill_prob) const {
    return argmin(passive_costs(book, side, ts, tick, candidates, fill_prob));
}

std::vector<SweepChild> CostAwareRouter::plan_sweep(
    const ConsolidatedBook& book, std::uint8_t side, std::int64_t qty,
    std::int64_t ts, std::optional<std::int64_t> limit_ticks,
    const std::vector<std::uint16_t>* candidates,
    std::optional<double> tick) const {
    if (qty <= 0) throw std::invalid_argument("sweep qty must be > 0");
    const bool all_in = options_.sweep_rank == SweepRank::ALL_IN;
    if (all_in && !tick.has_value()) {
        throw std::invalid_argument("sweep_rank=\"all_in\" needs tick");
    }
    // One sort key per displayed level. Python builds a tuple
    //   all_in: (key_px * tick + taker, vid, key_px, vid, px, q)
    //   price:  (key_px, taker, vid, vid, px, q)
    // and sorts by the first three fields.
    struct Level {
        double k0;        // all_in key, or key_px as a double (exact: ticks)
        double k1;        // vid (all_in) or taker fee (price)
        std::int64_t k2;  // key_px (all_in) or vid (price)
        std::uint16_t vid;
        std::int64_t px;
        std::int64_t q;
    };
    std::vector<Level> levels;
    for (const Touch& t : touches(book, side == 0 ? 1 : 0, candidates)) {
        const double taker = ledger_->quote(t.vid, ts).first;
        const std::vector<LevelEntry> depth =
            t.book->depth(side == 0 ? Side::ASK : Side::BID, options_.sweep_levels);
        for (const auto& [px, q] : depth) {
            if (limit_ticks.has_value() &&
                (side == 0 ? px > *limit_ticks : px < *limit_ticks)) {
                break;
            }
            const std::int64_t key_px = side == 0 ? px : -px;
            if (all_in) {
                // cost per share for a buy, minus proceeds for a sell
                const double key = static_cast<double>(key_px) * *tick + taker;
                levels.push_back({key, static_cast<double>(t.vid), key_px, t.vid, px, q});
            } else {
                levels.push_back({static_cast<double>(key_px), taker,
                                  static_cast<std::int64_t>(t.vid), t.vid, px, q});
            }
        }
    }
    std::stable_sort(levels.begin(), levels.end(), [](const Level& a, const Level& b) {
        if (a.k0 != b.k0) return a.k0 < b.k0;
        if (a.k1 != b.k1) return a.k1 < b.k1;
        return a.k2 < b.k2;
    });
    std::map<std::pair<std::uint16_t, std::int64_t>, std::int64_t> take;
    std::int64_t left = qty;
    for (const Level& l : levels) {
        if (left <= 0) break;
        const std::int64_t n = std::min(l.q, left);
        take[{l.vid, l.px}] += n;
        left -= n;
    }
    std::int64_t max_lat = 0;
    std::map<std::uint16_t, std::int64_t> lat;
    for (const auto& [key, n] : take) {
        (void)n;
        const std::int64_t l = models_->at(key.first).one_way_latency_ns();
        lat[key.first] = l;
        max_lat = std::max(max_lat, l);
    }
    std::vector<SweepChild> children;
    for (const Level& l : levels) {
        auto it = take.find({l.vid, l.px});
        if (it == take.end()) continue;
        const std::int64_t n = it->second;
        take.erase(it);
        if (n == 0) continue;
        const std::int64_t send =
            options_.stagger == StaggerMode::SYNC_ARRIVAL ? max_lat - lat[l.vid] : 0;
        children.push_back({l.vid, l.px, n, send, send + lat[l.vid]});
    }
    return children;
}

std::map<std::uint16_t, std::int64_t> CostAwareRouter::allocate_passive(
    const ConsolidatedBook& book, std::uint8_t side, std::int64_t qty,
    const std::vector<std::uint16_t>* candidates) const {
    if (qty <= 0) throw std::invalid_argument("allocation qty must be > 0");
    const RouterOptions& o = options_;
    std::map<std::uint16_t, double> w;
    for (const Touch& t : touches(book, side, candidates)) {
        const VenueModel& m = models_->at(t.vid);
        const double tox = m.toxicity(o.tox_horizon);
        if (tox > o.max_toxicity_bps) continue;
        const double wt = m.p_fill_touch / (1.0 + std::max(tox, 0.0) / o.tox_scale_bps);
        if (wt > 0.0) w[t.vid] = wt;
    }
    std::map<std::uint16_t, std::int64_t> out;
    if (w.empty()) return out;
    const std::int64_t lots = qty / o.lot;
    double total = 0.0;  // Python sum() over insertion (= ascending id) order
    for (const auto& [vid, wt] : w) {
        (void)vid;
        total += wt;
    }
    std::map<std::uint16_t, double> raw;
    std::map<std::uint16_t, std::int64_t> alloc;
    std::int64_t assigned = 0;
    for (const auto& [vid, wt] : w) {
        raw[vid] = static_cast<double>(lots) * wt / total;
        alloc[vid] = static_cast<std::int64_t>(raw[vid]);  // int(): truncation
        assigned += alloc[vid];
    }
    const std::int64_t rem = lots - assigned;
    std::vector<std::uint16_t> order;
    for (const auto& [vid, wt] : w) {
        (void)wt;
        order.push_back(vid);
    }
    std::stable_sort(order.begin(), order.end(), [&](std::uint16_t a, std::uint16_t b) {
        const double fa = -(raw[a] - static_cast<double>(alloc[a]));
        const double fb = -(raw[b] - static_cast<double>(alloc[b]));
        if (fa != fb) return fa < fb;
        return a < b;
    });
    for (std::int64_t i = 0; i < rem && i < static_cast<std::int64_t>(order.size()); ++i) {
        alloc[order[static_cast<std::size_t>(i)]] += 1;
    }
    for (const auto& [vid, a] : alloc) out[vid] = a * o.lot;
    const std::int64_t odd = qty - lots * o.lot;
    if (odd != 0) {
        // the sub-lot remainder goes to the top-weight venue (lower id on a tie)
        std::uint16_t top = 0;
        bool have = false;
        for (const auto& [vid, wt] : w) {
            if (!have || wt > w[top]) {
                have = true;
                top = vid;
            }
        }
        out[top] += odd;
    }
    for (auto it = out.begin(); it != out.end();) {
        if (it->second > 0) {
            ++it;
        } else {
            it = out.erase(it);
        }
    }
    return out;
}

}  // namespace iap
