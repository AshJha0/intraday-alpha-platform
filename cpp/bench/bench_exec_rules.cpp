// bench_exec_rules — per-event latency of the v1.17 F7 C++ execution-rule
// ports on a replayed book (the hot-path rows the F7 plan row asks for).
//
// Workloads: the golden eq_mbo stream (2000 events, 50 passes) and the
// committed real slice benchmarks/slices/itch50_20190130_AAPL (65,132
// events, 5 passes; F4 provenance in its SLICE.md). Per event the book is
// updated (not timed) and each rule is timed on the post-event state:
//   sor_v2 plan_sweep       CostAwareRouter::plan_sweep, all-in rank, buy
//                           5000 across the displayed depth (rule 1)
//   sor_v2 route_passive    passive_costs + route_passive with the hazard
//                           fill probability over the X-4 read (rule 2)
//   rule 6b parent impact   the rule-6b adder of one 100-share taker fill
//                           (ExecutionSimulator::parent_impact arithmetic,
//                           reproduced on the bench's own state) (rule 3)
//   pov_exact target        pov_target(parent, V) on the running TRADE
//                           volume (rule 4)
//   B2 read_signal          causal reading + regime (rule 6)
//   B3 pov_pacing_step      the per-TRADE pacing accumulation (rule 7)
//   B4 fade_rule            book_pressure + fade_rule (rule 8)
//   B5 queue rule + hazard  features + p_fill_within + queue_cancel_rule
//                           (rule 9)
// The AC trajectory (rule 5) and the B6 decomposition (rule 10) are
// per-slice / per-report, not per-event; they are not on the tick-to-trade
// path and have no row. Timing: steady_clock around each call, HDR-style
// log-linear histogram (the bench_tick_to_trade one), warm-up pass
// discarded. Writes a markdown table when given an output path
// (benchmarks/results_exec_rules.md).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"  // GCC 12: initializer-list Array{...} false positive
#endif

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "iap/execution/dark.hpp"
#include "iap/execution/fading.hpp"
#include "iap/execution/fill_hazard.hpp"
#include "iap/execution/pacing.hpp"
#include "iap/execution/queue_depletion.hpp"
#include "iap/execution/signal.hpp"
#include "iap/execution/algos.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/sor/sor_v2.hpp"
#include "iap/util/data_paths.hpp"

namespace {

using Clock = std::chrono::steady_clock;

inline std::int64_t ns_between(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
}

volatile double g_sink = 0.0;

// Same histogram as bench_tick_to_trade (<= 6.25 % bucket width).
class LogHistogram {
public:
    static constexpr int kSubBits = 4;
    static constexpr int kSub = 1 << kSubBits;
    static constexpr int kBuckets = (64 - kSubBits + 1) * kSub;

    void record(std::int64_t v) {
        if (v < 0) v = 0;
        const auto u = static_cast<std::uint64_t>(v);
        ++counts_[static_cast<std::size_t>(index(u))];
        ++n_;
        sum_ += static_cast<double>(u);
        if (u > max_) max_ = u;
    }
    std::uint64_t count() const { return n_; }
    std::uint64_t max() const { return max_; }
    double mean() const { return n_ ? sum_ / static_cast<double>(n_) : 0.0; }
    std::uint64_t quantile(double q) const {
        if (n_ == 0) return 0;
        auto rank = static_cast<std::uint64_t>(q * static_cast<double>(n_));
        if (rank < 1) rank = 1;
        if (rank > n_) rank = n_;
        std::uint64_t seen = 0;
        for (int i = 0; i < kBuckets; ++i) {
            seen += counts_[static_cast<std::size_t>(i)];
            if (seen >= rank) return std::min(upper(i), max_);
        }
        return max_;
    }

private:
    static int index(std::uint64_t v) {
        if (v < static_cast<std::uint64_t>(kSub)) return static_cast<int>(v);
        const int msb = 63 - __builtin_clzll(v);
        const int shift = msb - kSubBits;
        const int sub = static_cast<int>((v >> shift) & (kSub - 1));
        return (shift + 1) * kSub + sub;
    }
    static std::uint64_t upper(int i) {
        if (i < kSub) return static_cast<std::uint64_t>(i);
        const int shift = i / kSub - 1;
        const std::uint64_t sub = static_cast<std::uint64_t>(i % kSub);
        const std::uint64_t base = (static_cast<std::uint64_t>(kSub) + sub) << shift;
        return base + ((std::uint64_t{1} << shift) - 1);
    }
    std::array<std::uint64_t, kBuckets> counts_{};
    std::uint64_t n_ = 0;
    std::uint64_t max_ = 0;
    double sum_ = 0.0;
};

constexpr int kRules = 9;
const char* const kRuleNames[kRules] = {
    "sor_v2 plan_sweep (all-in, buy 5000)",
    "sor_v2 route_passive (hazard fill prob)",
    "rule 6b parent impact (one taker fill)",
    "pov_exact target",
    "B2 read_signal + regime",
    "B3 pov_pacing_step",
    "B4 book_pressure + fade_rule",
    "B5 hazard p_fill + queue_cancel_rule",
    "B10 dark p_fill + midpoint + pretrade check",
};

struct Workload {
    std::string name;
    std::vector<iap::MarketEvent> events;
    std::uint32_t instrument;
    std::uint16_t venue;
    double tick;
    double adv;
    int passes;
};

std::vector<iap::MarketEvent> read_real_slice(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::vector<std::uint8_t> bytes((std::istreambuf_iterator<char>(f)),
                                    std::istreambuf_iterator<char>());
    return iap::decode_iap1(bytes);
}

std::string cpu_model() {
    std::ifstream f("/proc/cpuinfo");
    std::string line;
    while (std::getline(f, line)) {
        if (line.rfind("model name", 0) == 0) {
            const auto pos = line.find(':');
            if (pos != std::string::npos) {
                std::size_t s = pos + 1;
                while (s < line.size() && line[s] == ' ') ++s;
                return line.substr(s);
            }
        }
    }
    return "unknown";
}

// A fitted hazard document is research output; the bench uses a fixed,
// plausible one (the shape of python/tests/test_fill_hazard_x4 fits).
iap::FillHazardModel bench_hazard() {
    using iap::contracts::Array;
    using iap::contracts::Object;
    using iap::contracts::Value;
    Object d;
    d["schema"] = Value("iap.fill_hazard/1");
    d["link"] = Value("cloglog");
    d["edges_ns"] = Value(Array{Value(0LL), Value(100000000LL), Value(250000000LL),
                                Value(500000000LL), Value(1000000000LL), Value(2000000000LL),
                                Value(5000000000LL)});
    d["ridge"] = Value(1e-3);
    d["features"] = Value(Array{Value("queue_ahead"), Value("depletion_per_s"),
                                Value("spread_ticks"), Value("imbalance"), Value("tod")});
    d["columns"] = Value(Array{Value("log_queue_ahead"), Value("log_depletion"),
                               Value("spread_ticks"), Value("imbalance"), Value("tod_sin"),
                               Value("tod_cos")});
    d["alpha"] = Value(Array{Value(-2.6), Value(-2.2), Value(-1.7), Value(-1.1), Value(-0.6), Value(0.3)});
    d["beta"] = Value(Array{Value(-0.7), Value(0.1), Value(-0.05), Value(0.02), Value(0.0), Value(0.0)});
    d["mean"] = Value(Array{Value(5.0), Value(0.0), Value(1.0), Value(0.5), Value(0.0), Value(0.0)});
    d["std"] = Value(Array{Value(1.0), Value(1.0), Value(1.0), Value(0.3), Value(1.0), Value(1.0)});
    d["n_samples"] = Value(6000LL);
    d["n_filled"] = Value(3000LL);
    d["clock"] = Value("utc");
    d["capture_offset_s"] = Value(0LL);
    return iap::FillHazardModel::from_value(Value(std::move(d)));
}

iap::QueueDepletionTable bench_depletion() {
    using iap::contracts::Array;
    using iap::contracts::Object;
    using iap::contracts::Value;
    auto cell = [](double h, double e, double c) {
        Object o;
        o["hazard_per_s"] = Value(h);
        o["exec_hazard_per_s"] = Value(e);
        o["cancel_hazard_per_s"] = Value(c);
        return Value(std::move(o));
    };
    Object side;
    side["0"] = cell(4.0, 3.1, 0.9);
    side["1-300"] = cell(2.2, 1.4, 0.8);
    side["301-1000"] = cell(0.9, 0.55, 0.35);
    side["1001-5000"] = cell(0.3, 0.16, 0.14);
    side[">5000"] = cell(0.08, 0.03, 0.05);
    Object qd;
    qd["bid"] = Value(side);
    qd["ask"] = Value(side);
    Object doc;
    doc["queue_edges"] = Value(Array{Value(0LL), Value(300LL), Value(1000LL), Value(5000LL)});
    doc["queue_depletion"] = Value(std::move(qd));
    return iap::QueueDepletionTable::from_value(Value(std::move(doc)));
}

struct Result {
    std::array<LogHistogram, kRules> h;
    std::uint64_t events = 0;
};

void run_workload(const Workload& w, Result* out, bool timed) {
    std::map<std::uint16_t, iap::VenueModel> models;
    iap::VenueModel vm;
    vm.spec.venue_id = w.venue;
    vm.spec.name = "V";
    vm.spec.taker_fee_per_share = 0.003;
    vm.spec.maker_rebate_per_share = 0.002;
    vm.spec.latency_mean_ns = 0;
    vm.p_fill_touch = 0.45;
    vm.toxicity_bps["1s"] = 0.8;
    models[w.venue] = vm;
    iap::RouterOptions all_in;
    all_in.sweep_rank = iap::SweepRank::ALL_IN;
    iap::CostAwareRouter router(models, all_in);
    const iap::QueueDepletionTable dep = bench_depletion();
    const iap::FillProb fp = iap::hazard_fill_prob(
        [&dep](std::uint16_t, std::uint8_t side, std::int64_t q) { return dep.exec_hazard(side, q); },
        1.0);
    const iap::FillHazardModel hz = bench_hazard();
    // a planted signal: one reading per 50 events from the book imbalance
    iap::ArraySignalSource src;
    {
        iap::ConsolidatedBook b(w.instrument);
        std::vector<iap::ArraySignalSource::Row> rows;
        for (std::size_t k = 1; k <= w.events.size(); ++k) {
            b.apply(w.events[k - 1]);
            if (k % 50 != 0) continue;
            const auto bb = b.best_bid();
            const auto ba = b.best_ask();
            if (!bb || !ba) continue;
            const double er = static_cast<double>(bb->second - ba->second) /
                              static_cast<double>(bb->second + ba->second) * 1e-4;
            const std::int64_t ts = w.events[k - 1].exchange_ts;
            if (!rows.empty() && rows.back().ts >= ts) continue;
            rows.push_back({ts, er * 1e4, er});
        }
        src.add(w.instrument, rows);
    }
    iap::PacingParams pac;
    iap::ParentOrder pov;
    pov.algo = iap::AlgoType::POV;
    pov.participation = 0.1;
    pov.pov_exact = true;
    const double pct = 1.0 / w.adv * 100.0;
    const double coeff = 2.0;
    const double exponent = 0.5;
    std::int64_t parent_q = 0;
    std::int64_t volume = 0;
    const iap::DarkFillModel dark_model = iap::DarkFillModel::prior();
    iap::DarkControls dark_controls;
    dark_controls.max_participation = 0.1;
    dark_controls.max_dark_notional = 1e7;
    double target_f = 0.0;

    iap::ConsolidatedBook book(w.instrument);
    for (const auto& ev : w.events) {
        book.apply(ev);
        const std::int64_t t = ev.exchange_ts;
        if (static_cast<iap::EventType>(ev.event_type) == iap::EventType::TRADE) volume += ev.qty;
        const auto bb = book.best_bid();
        const auto ba = book.best_ask();
        if (!bb || !ba) continue;
        if (timed) ++out->events;
        auto time = [&](int rule, auto&& fn) {
            const auto a = Clock::now();
            fn();
            const auto b = Clock::now();
            if (timed) out->h[static_cast<std::size_t>(rule)].record(ns_between(a, b));
        };
        time(0, [&] {
            const auto ch = router.plan_sweep(book, 0, 5000, t, std::nullopt, nullptr, w.tick);
            g_sink = g_sink + static_cast<double>(ch.size());
        });
        time(1, [&] { g_sink = g_sink + router.route_passive(book, 0, t, w.tick, nullptr, &fp); });
        time(2, [&] {
            const double unit_price = static_cast<double>(ba->first) * w.tick;
            const std::int64_t q0 = parent_q;
            const std::int64_t q1 = q0 + 100;
            auto total = [&](std::int64_t q) {
                return coeff * std::pow(static_cast<double>(q) * pct, exponent) * static_cast<double>(q);
            };
            const double cost = (total(q1) - total(q0)) * 1e-4 * unit_price;
            parent_q = q1 % 1'000'000;
            g_sink = g_sink + cost;
        });
        time(3, [&] { g_sink = g_sink + static_cast<double>(iap::pov_target(pov, volume)); });
        iap::SignalDecision d;
        time(4, [&] {
            d = iap::read_signal(src, w.instrument, 0, t, 1.0, 10'000'000'000, 0);
            g_sink = g_sink + d.z;
        });
        time(5, [&] {
            const std::optional<double> z = d.status == iap::SignalStatus::OK ? std::optional<double>(d.z) : std::nullopt;
            const auto s = iap::pov_pacing_step(0, z, pac, 0.1, true, 100, target_f);
            target_f = s.target_f > 1e9 ? 0.0 : s.target_f;
            g_sink = g_sink + s.rate;
        });
        time(6, [&] {
            const double pr = iap::book_pressure(bb->second, ba->second);
            const std::optional<double> z = d.status == iap::SignalStatus::OK ? std::optional<double>(d.z) : std::nullopt;
            g_sink = g_sink + static_cast<double>(iap::fade_rule(0, z, 1.0, pr, 0.8));
        });
        time(7, [&] {
            const double spread = static_cast<double>(ba->first - bb->first);
            const double imb = 1.0 - iap::book_pressure(bb->second, ba->second);
            const auto x = hz.features(static_cast<double>(bb->second), spread, imb,
                                       dep.exec_hazard(0, bb->second), t);
            const double p = hz.p_fill_within(x, 1'000'000'000);
            g_sink = g_sink + static_cast<double>(iap::queue_cancel_rule(bb->second, 5000, p, 0.2));
        });
        time(8, [&] {
            // the per-event work of one open dark child (work_dark) plus the
            // stateless pre-trade check of a post (dark_try)
            const double pf = dark_model.p_fill(volume % 50'000, ba->first - bb->first, 300, 600);
            const std::int64_t mid = iap::midpoint_ticks(bb->first, ba->first, 0);
            const auto code = iap::dark_pretrade_check(dark_controls, iap::VenueType::DARK, 300,
                                                       volume % 50'000, 1e5, 300.0 * mid * w.tick);
            g_sink = g_sink + pf + static_cast<double>(mid) + static_cast<double>(code);
        });
    }
}

}  // namespace

int main(int argc, char** argv) {
    const std::string dir = iap::golden_dir();
    std::vector<Workload> workloads;
    workloads.push_back({"golden eq_mbo (2000 ev)", iap::read_jsonl(dir + "/events_eq_mbo.jsonl"), 1, 1,
                         0.01, 38'000'000.0, 50});
    auto slice = read_real_slice(iap::repo_root() +
                                 "/benchmarks/slices/itch50_20190130_AAPL/normalized/"
                                 "eq_20190130.normalized.iap1");
    const bool have_slice = !slice.empty();
    if (have_slice) {
        workloads.push_back({"real ITCH slice AAPL 2019-01-30 09:30 (65132 ev)", std::move(slice), 1,
                             101, 0.01, 14'451'444.0, 5});
    }
    std::ostringstream md;
    char buf[512];
    md << "# C++ execution-rule latency (bench_exec_rules, v1.17 F7)\n\n";
    md << "- CPU: " << cpu_model() << "\n";
    md << "- Per event the book is updated (not timed) and each ported rule is timed on the "
          "post-event state: steady_clock around the call, warm-up pass discarded, log-linear "
          "histogram (<= 6.25% bucket width), percentile = bucket upper bound, max exact. "
          "Rows are per-event decisions of ONE rule on the consolidated book, not a "
          "tick-to-trade path (see results_tick_to_trade.md for that).\n";
    md << "- Real slice: "
       << (have_slice ? "benchmarks/slices/itch50_20190130_AAPL (F4; 65,132 events, 5 passes)"
                      : "ABSENT (rows missing)")
       << "\n\n| rule, workload | p50 ns | p90 ns | p99 ns | p99.9 ns | max ns | mean ns | samples |\n"
       << "|---|---:|---:|---:|---:|---:|---:|---:|\n";
    for (const Workload& w : workloads) {
        Result r;
        run_workload(w, &r, false);  // warm-up
        for (int p = 0; p < w.passes; ++p) run_workload(w, &r, true);
        for (int i = 0; i < kRules; ++i) {
            const LogHistogram& h = r.h[static_cast<std::size_t>(i)];
            std::snprintf(buf, sizeof(buf), "| %s, %s | %llu | %llu | %llu | %llu | %llu | %.1f | %llu |\n",
                          kRuleNames[i], w.name.c_str(),
                          static_cast<unsigned long long>(h.quantile(0.50)),
                          static_cast<unsigned long long>(h.quantile(0.90)),
                          static_cast<unsigned long long>(h.quantile(0.99)),
                          static_cast<unsigned long long>(h.quantile(0.999)),
                          static_cast<unsigned long long>(h.max()), h.mean(),
                          static_cast<unsigned long long>(h.count()));
            md << buf;
        }
    }
    md << "\n(sink " << g_sink << ")\n";
    std::printf("%s", md.str().c_str());
    if (argc > 1) {
        std::ofstream out(argv[1]);
        out << md.str();
    }
    return 0;
}
