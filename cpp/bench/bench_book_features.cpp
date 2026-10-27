// bench_book_features: per-event latency DISTRIBUTION of the order book and
// the native feature engine, on their own (v2.1 Track 3).
//
// bench_tick_to_trade already reports these two stages inside the whole
// decode -> book -> features -> alpha -> decision path
// (benchmarks/results_tick_to_trade.md). This program isolates them so the
// questions "what does a book update cost, how does it grow with depth, and
// does the hot path allocate?" have their own reproducible answer:
//
//   stage "book"      OrderBook::apply on the bench's own book
//   stage "features"  FeatureEngine::apply (48 native features, cadence 0);
//                     the engine keeps its OWN merged book view, so book
//                     maintenance is paid again inside this stage
//   "book + features" the two back to back, timed as one span
//   "book, reserved"  OrderBook::apply on a book whose pools were reserved
//                     (OrderBook::reserve) outside the timed region: the
//                     difference to "book" is the cost of pool growth
//
// Reproducibility. Every input is pinned: the synthetic streams come from
// SplitMix64 with the seeds printed in the output (kSeedShallow, kSeedDeep),
// the real stream is the committed ITCH slice
// benchmarks/slices/itch50_20190130_AAPL (skipped with a note when absent).
// One full untimed pass per workload runs first (warm-up: caches, branch
// predictors, page faults of the event arrays). Each timed pass builds fresh
// book / engine state OUTSIDE the timed region.
//
// Allocation count. Global operator new is replaced by a counting wrapper
// over malloc; the counter is read between clock reads, so the figure is the
// number of heap allocations made INSIDE each timed stage, divided by the
// events of that stage. It counts calls, not bytes.
//
// Timing: std::chrono::steady_clock (portable; no rdtsc), three clock reads
// per event for the main spans, two for the reserved-book span. Their cost is
// measured and printed ("timer overhead") and is INCLUDED in every figure.
// Histogram: the same HDR-style log-linear buckets as bench_tick_to_trade
// (16 linear sub-buckets per power of two, <= 6.25% relative bucket width,
// percentile = upper bound of its bucket, max exact).
//
// What this is not: no network, NIC or kernel; no core pinning, no isolated
// CPU, no huge pages; a shared CI runner. The figures describe this code on
// that machine, not a trading system, and claim no latency edge.
//
// Usage: bench_book_features [output.md]

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <map>
#include <new>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "iap/features/feature_engine.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/rng.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/util/data_paths.hpp"

// ------------------------------------------------------ allocation counter
namespace {
std::atomic<std::uint64_t> g_allocs{0};
}  // namespace

void* operator new(std::size_t n) {
    g_allocs.fetch_add(1, std::memory_order_relaxed);
    if (void* p = std::malloc(n == 0 ? 1 : n)) return p;
    throw std::bad_alloc();
}
void operator delete(void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }

namespace {

using Clock = std::chrono::steady_clock;

inline std::int64_t ns_between(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
}

inline std::uint64_t allocs_now() {
    return g_allocs.load(std::memory_order_relaxed);
}

volatile std::uint64_t g_sink = 0;

constexpr std::uint64_t kSeedShallow = 20261027ULL;
constexpr std::uint64_t kSeedDeep = 20261028ULL;

// ------------------------------------------------------------- histogram
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
        const std::uint64_t base = (static_cast<std::uint64_t>(kSub) + sub)
                                   << shift;
        return base + ((std::uint64_t{1} << shift) - 1);
    }

    std::array<std::uint64_t, kBuckets> counts_{};
    std::uint64_t n_ = 0;
    std::uint64_t max_ = 0;
    double sum_ = 0.0;
};

// ------------------------------------------------------- generated stream
// One instrument on one venue; adds / cancels / executes / trades around a
// random-walk mid. `max_offset` sets how far from the mid new orders rest
// (the number of price levels per side the book carries) and `min_live` the
// resting order count the generator keeps. Never crosses the book.
std::vector<iap::MarketEvent> generate_stream(std::uint64_t seed, std::size_t n,
                                              std::int64_t max_offset,
                                              std::size_t min_live) {
    iap::SplitMix64 rng(seed);
    std::vector<iap::MarketEvent> out;
    out.reserve(n);
    struct Live {
        std::uint64_t id;
        std::uint8_t side;
        std::int64_t px;
        std::int64_t qty;
    };
    std::vector<Live> live;
    std::int64_t ts = 1787578200000000000LL;
    std::uint64_t seq = 0, next_oid = 1, next_tid = 1;
    std::int64_t mid = 2450;
    std::int64_t best_bid = 0, best_ask = 0;  // 0 = side empty
    auto refresh_best = [&]() {
        best_bid = 0;
        best_ask = 0;
        for (const Live& o : live) {
            if (o.side == 0 && (best_bid == 0 || o.px > best_bid)) best_bid = o.px;
            if (o.side == 1 && (best_ask == 0 || o.px < best_ask)) best_ask = o.px;
        }
    };
    auto push = [&](std::uint8_t type, std::uint8_t side, std::int64_t px,
                    std::int64_t qty, std::uint64_t oid, std::uint64_t tid) {
        ++seq;
        ts += 1'000'000 + static_cast<std::int64_t>(rng.below(4'000'000));
        out.push_back(iap::MarketEvent::of(
            seq, 1, 1, ts, ts + 150'000 + rng.below(50'000), seq, type, side,
            px, qty, oid, tid));
    };
    auto remove_at = [&](std::size_t k) {
        const Live o = live[k];
        live[k] = live.back();
        live.pop_back();
        if (o.px == best_bid || o.px == best_ask) refresh_best();
    };
    push(static_cast<std::uint8_t>(iap::EventType::STATUS), 0, 0, 1, 0, 0);
    while (out.size() < n) {
        const double u = rng.uniform();
        if (rng.uniform() < 0.02) mid += rng.uniform() < 0.5 ? -1 : 1;
        if (mid < max_offset + 10) mid = max_offset + 10;
        if (live.size() < min_live || u < 0.45) {
            const std::uint8_t side = rng.uniform() < 0.5 ? 0 : 1;
            const std::int64_t off = 1 + rng.below(max_offset);
            std::int64_t px = side == 0 ? mid - off : mid + off;
            if (side == 0 && best_ask != 0 && px >= best_ask) px = best_ask - 1;
            if (side == 1 && best_bid != 0 && px <= best_bid) px = best_bid + 1;
            if (px < 1) px = 1;
            const std::int64_t qty = 100 * (1 + rng.below(10));
            live.push_back({next_oid, side, px, qty});
            if (side == 0 && px > best_bid) best_bid = px;
            if (side == 1 && (best_ask == 0 || px < best_ask)) best_ask = px;
            push(static_cast<std::uint8_t>(iap::EventType::ADD), side, px, qty,
                 next_oid++, 0);
        } else if (u < 0.80) {
            const auto k = static_cast<std::size_t>(
                rng.below(static_cast<std::int64_t>(live.size())));
            const Live o = live[k];
            push(static_cast<std::uint8_t>(iap::EventType::CANCEL), o.side,
                 o.px, o.qty, o.id, 0);
            remove_at(k);
        } else if (u < 0.92) {
            const auto k = static_cast<std::size_t>(
                rng.below(static_cast<std::int64_t>(live.size())));
            Live& o = live[k];
            const std::int64_t q = std::min<std::int64_t>(o.qty, 100);
            push(static_cast<std::uint8_t>(iap::EventType::EXECUTE), o.side,
                 o.px, q, o.id, next_tid++);
            o.qty -= q;
            if (o.qty == 0) remove_at(k);
        } else {
            const std::uint8_t side = rng.uniform() < 0.5 ? 0 : 1;
            push(static_cast<std::uint8_t>(iap::EventType::TRADE), side, mid,
                 100 * (1 + rng.below(5)), 0, next_tid++);
        }
    }
    return out;
}

std::vector<iap::MarketEvent> read_real_slice(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::vector<std::uint8_t> bytes((std::istreambuf_iterator<char>(f)),
                                    std::istreambuf_iterator<char>());
    return iap::decode_iap1(bytes);  // magic, size and CRC verified
}

// ------------------------------------------------------- the passes
struct Stage {
    LogHistogram h;
    std::uint64_t allocs = 0;
};

struct Result {
    Stage book, features, both, reserved;
    std::uint64_t vectors = 0;
    std::size_t peak_levels = 0;  // max (bid + ask) levels seen at depth()
};

void run_pass(const std::vector<iap::MarketEvent>& events,
              const std::map<std::uint32_t, double>& ticks, Result* r) {
    const std::uint32_t iid = events.empty() ? 1 : events.front().instrument_id;
    const std::uint16_t vid = events.empty() ? 1 : events.front().venue_id;
    iap::OrderBook book(iid, vid);
    iap::OrderBook reserved(iid, vid);
    reserved.reserve(1u << 17, 1u << 14);
    iap::FeatureEngine fe(ticks, 0);
    iap::FeatureVector vec;
    for (const auto& ev : events) {
        const std::uint64_t a0 = allocs_now();
        const auto t0 = Clock::now();
        const auto st = book.apply(ev);
        const auto t1 = Clock::now();
        const std::uint64_t a1 = allocs_now();
        const bool emitted = fe.apply(ev, vec);
        const auto t2 = Clock::now();
        const std::uint64_t a2 = allocs_now();
        const auto t3 = Clock::now();
        const auto st2 = reserved.apply(ev);
        const auto t4 = Clock::now();
        const std::uint64_t a3 = allocs_now();
        if (r != nullptr) {
            r->book.h.record(ns_between(t0, t1));
            r->book.allocs += a1 - a0;
            r->features.h.record(ns_between(t1, t2));
            r->features.allocs += a2 - a1;
            r->both.h.record(ns_between(t0, t2));
            r->both.allocs += a2 - a0;
            r->reserved.h.record(ns_between(t3, t4));
            r->reserved.allocs += a3 - a2;
            r->vectors += emitted ? 1 : 0;
        }
        g_sink += ev.sequence + static_cast<std::uint64_t>(st) +
                  static_cast<std::uint64_t>(st2);
    }
    if (r != nullptr) {
        const std::size_t lv = book.depth(iap::Side::BID, 1 << 20).size() +
                               book.depth(iap::Side::ASK, 1 << 20).size();
        r->peak_levels = std::max(r->peak_levels, lv);
    }
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

struct Workload {
    std::string name;
    std::vector<iap::MarketEvent> events;
    int timed_passes;
};

}  // namespace

int main(int argc, char** argv) {
    const std::map<std::uint32_t, double> ticks = {{1u, 0.01}};

    std::vector<Workload> workloads;
    workloads.push_back(
        {"generated shallow (seed 20261027, 100000 ev, adds <= 10 ticks from mid)",
         generate_stream(kSeedShallow, 100000, 10, 20), 5});
    workloads.push_back(
        {"generated deep (seed 20261028, 100000 ev, adds <= 500 ticks from mid)",
         generate_stream(kSeedDeep, 100000, 500, 4000), 5});
    const std::string slice_path =
        iap::repo_root() +
        "/benchmarks/slices/itch50_20190130_AAPL/normalized/"
        "eq_20190130.normalized.iap1";
    auto slice = read_real_slice(slice_path);
    const bool have_slice = !slice.empty();
    if (have_slice) {
        workloads.push_back({"real ITCH slice AAPL 2019-01-30 09:30 (65132 ev)",
                             std::move(slice), 10});
    }

    LogHistogram timer;
    for (int i = 0; i < 200000; ++i) {
        const auto a = Clock::now();
        const auto b = Clock::now();
        timer.record(ns_between(a, b));
    }

    std::ostringstream md;
    char buf[640];
    md << "# C++ order book and feature engine latency (bench_book_features)\n\n";
    md << "Generated by `bench_book_features "
          "benchmarks/results_book_features.md`; the CI job `cpp` re-runs it "
          "and uploads its copy as the artifact `book-features-bench`. Do not "
          "edit by hand.\n\n";
    md << "- CPU: " << cpu_model() << " ("
       << std::thread::hardware_concurrency()
       << " logical CPUs visible, no pinning, no isolated core)\n";
    const char* host = std::getenv("IAP_BENCH_HOST");
    md << "- Host: "
       << (host != nullptr && *host != '\0'
               ? host
               : (std::getenv("GITHUB_ACTIONS") != nullptr
                      ? "GitHub-hosted runner (shared VM)"
                      : "not stated (set IAP_BENCH_HOST)"))
       << "\n";
#if defined(__clang__)
    md << "- Compiler: clang " << __VERSION__;
#elif defined(__GNUC__)
    md << "- Compiler: GCC " << __VERSION__;
#else
    md << "- Compiler: unknown";
#endif
#if defined(NDEBUG)
    md << ", NDEBUG build (CMake Release adds -O3 -DNDEBUG), C++17\n";
#else
    md << ", assertions ON (not a Release build: figures not comparable)\n";
#endif
    md << "- Inputs: SplitMix64 seeds " << kSeedShallow << " (shallow) and "
       << kSeedDeep << " (deep); real slice "
       << (have_slice ? "benchmarks/slices/itch50_20190130_AAPL (provenance "
                        "in its SLICE.md)"
                      : "NOT FOUND (workload skipped)")
       << "\n";
    md << "- Method: one untimed warm-up pass per workload, then the timed "
          "passes (fresh book / engine state per pass, built outside the "
          "timed region); steady_clock; HDR-style log-linear histogram "
          "(<= 6.25% bucket width), percentile = bucket upper bound, max "
          "exact.\n";
    std::snprintf(buf, sizeof(buf),
                  "- Timer overhead (back-to-back steady_clock reads, "
                  "included in every figure): p50 %llu ns, p99 %llu ns\n",
                  static_cast<unsigned long long>(timer.quantile(0.50)),
                  static_cast<unsigned long long>(timer.quantile(0.99)));
    md << buf;
    md << "- `allocs / 1k ev`: heap allocations (calls to operator new) "
          "inside the stage per 1,000 events, from a counting operator "
          "new.\n";

    md << "\n| stage, workload | p50 ns | p90 ns | p99 ns | p99.9 ns | max ns "
          "| mean ns | allocs / 1k ev | samples |\n|---|---:|---:|---:|---:|"
          "---:|---:|---:|---:|\n";
    std::ostringstream notes;
    notes << "\n";
    for (const auto& wl : workloads) {
        run_pass(wl.events, ticks, nullptr);  // warm-up
        Result r;
        for (int p = 0; p < wl.timed_passes; ++p) run_pass(wl.events, ticks, &r);
        auto row = [&](const char* label, const Stage& s) {
            const double per_k =
                s.h.count() ? 1000.0 * static_cast<double>(s.allocs) /
                                  static_cast<double>(s.h.count())
                            : 0.0;
            std::snprintf(
                buf, sizeof(buf),
                "| %s, %s | %llu | %llu | %llu | %llu | %llu | %.1f | %.2f | "
                "%llu |\n",
                label, wl.name.c_str(),
                static_cast<unsigned long long>(s.h.quantile(0.50)),
                static_cast<unsigned long long>(s.h.quantile(0.90)),
                static_cast<unsigned long long>(s.h.quantile(0.99)),
                static_cast<unsigned long long>(s.h.quantile(0.999)),
                static_cast<unsigned long long>(s.h.max()), s.h.mean(), per_k,
                static_cast<unsigned long long>(s.h.count()));
            md << buf;
        };
        row("book", r.book);
        row("book, reserved", r.reserved);
        row("features (48)", r.features);
        row("**book + features**", r.both);
        std::snprintf(buf, sizeof(buf),
                      "- %s: %d timed pass(es); feature vectors %llu; book "
                      "levels at end of stream (bid + ask) %zu\n",
                      wl.name.c_str(), wl.timed_passes,
                      static_cast<unsigned long long>(r.vectors),
                      r.peak_levels);
        notes << buf;
    }
    md << notes.str();

    std::printf("%s", md.str().c_str());
    if (argc > 1) {
        std::ofstream out(argv[1], std::ios::binary);
        out << md.str();
        std::printf("\nwrote %s\n", argv[1]);
    }
    return g_sink == 0xFFFFFFFFFFFFFFFFull ? 1 : 0;
}
