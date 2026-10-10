// bench_tick_to_trade: per-event tick-to-trade latency DISTRIBUTION of the
// canonical C++ hot path (v1.12, plan item X6).
//
// bench_all reports stage MEANS over hot loops. This program timestamps every
// event individually through the whole in-process decision path and reports
// percentiles (p50 / p90 / p99 / p99.9 / max) end to end and per stage:
//
//   raw IAP1 frame (one 104-byte v2 frame per event: header + record + CRC
//   trailer, so the CRC is verified per message, as a feed handler would)
//     -> decode_iap1          (stage "decode")
//     -> OrderBook::apply     (stage "book";   the book the decision reads)
//     -> FeatureEngine::apply (stage "features"; 48 native features,
//                              cadence 0 — NOTE the engine keeps its own
//                              merged book view, so book maintenance is paid
//                              again inside this stage)
//     -> score_row x3         (stage "alpha"; EQ01 + EQ03 + EQ06 linear_z_v1)
//     -> order decision       (stage "decision"; bench-local, see below)
//
// Boundary (stated, not hidden). There is NO canonical C++ risk engine — the
// pre-trade risk engines are Rust / Java / Python (docs/POLYGLOT.md). The
// "decision" stage is a BENCH-LOCAL stand-in: confidence-weighted blend of
// the three signals, threshold, side, limit price from the opposite best,
// plus a minimal pre-trade check (position cap, order-notional cap, price
// band vs mid, kill switch). It is representative in cost (a handful of
// compares) but it is not the platform's risk engine. Not included at all:
// network / NIC / kernel (no kernel bypass, no sockets), order encoding and
// wire send, venue round trip, logging, the decision trace.
//
// Timing: std::chrono::steady_clock (portable; no rdtsc). Six clock reads per
// event; their own cost is measured and printed ("timer overhead") and is
// INCLUDED in every figure. Warm-up: the first full pass over each workload
// is run and discarded. Each timed pass builds fresh book / feature state
// OUTSIDE the timed region (sequence numbers restart).
//
// Histogram: HDR-style log-linear buckets (16 linear sub-buckets per power of
// two -> <= 6.25% relative bucket width), fixed size, no allocation while
// recording, no new dependency. A percentile is reported as the UPPER bound
// of the bucket that holds it (conservative); max is exact.
//
// Workloads (both pinned): the golden tests/golden/events_eq_mbo.jsonl (2000
// events) and a generated equity day: 100,000 MBO events from SplitMix64 seed
// 20261012 (one instrument, one venue; adds / cancels / executes / trades
// around a random-walk mid), built in-process so CI needs no data/ files.
//
// Usage: bench_tick_to_trade [output.md]

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

#include "iap/alpha/alpha.hpp"
#include "iap/features/feature_engine.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/rng.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/util/data_paths.hpp"

namespace {

using Clock = std::chrono::steady_clock;

inline std::int64_t ns_between(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
}

volatile std::uint64_t g_sink = 0;

// ------------------------------------------------------------- histogram
class LogHistogram {
public:
    static constexpr int kSubBits = 4;
    static constexpr int kSub = 1 << kSubBits;
    static constexpr int kBuckets = (64 - kSubBits + 1) * kSub;

    void record(std::int64_t v) {
        if (v < 0) v = 0;
        const auto u = static_cast<std::uint64_t>(v);
        ++counts_[index(u)];
        ++n_;
        sum_ += static_cast<double>(u);
        if (u > max_) max_ = u;
    }
    std::uint64_t count() const { return n_; }
    std::uint64_t max() const { return max_; }
    double mean() const { return n_ ? sum_ / static_cast<double>(n_) : 0.0; }

    // Upper bound of the bucket holding quantile q (0 < q <= 1).
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

// ------------------------------------------------------- generated day
std::vector<iap::MarketEvent> generate_equity_day(std::uint64_t seed,
                                                  std::size_t n) {
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
    std::int64_t ts = 1787578200000000000LL;  // 2026-08-24 09:30 ET session
    std::uint64_t seq = 0, next_oid = 1, next_tid = 1;
    std::int64_t mid = 2450;  // ticks of 0.01
    auto push = [&](std::uint8_t type, std::uint8_t side, std::int64_t px,
                    std::int64_t qty, std::uint64_t oid, std::uint64_t tid) {
        ++seq;
        ts += 1'000'000 + static_cast<std::int64_t>(rng.below(4'000'000));
        out.push_back(iap::MarketEvent::of(
            seq, 1, 1, ts, ts + 150'000 + rng.below(50'000), seq, type, side,
            px, qty, oid, tid));
    };
    push(static_cast<std::uint8_t>(iap::EventType::STATUS), 0, 0, 1, 0, 0);
    while (out.size() < n) {
        const double u = rng.uniform();
        if (rng.uniform() < 0.02) mid += rng.uniform() < 0.5 ? -1 : 1;
        if (mid < 100) mid = 100;
        if (live.size() < 20 || u < 0.45) {
            const std::uint8_t side = rng.uniform() < 0.5 ? 0 : 1;
            const std::int64_t off = 1 + rng.below(10);
            const std::int64_t px = side == 0 ? mid - off : mid + off;
            const std::int64_t qty = 100 * (1 + rng.below(10));
            live.push_back({next_oid, side, px, qty});
            push(static_cast<std::uint8_t>(iap::EventType::ADD), side, px, qty,
                 next_oid++, 0);
        } else if (u < 0.80) {
            const auto k = static_cast<std::size_t>(
                rng.below(static_cast<std::int64_t>(live.size())));
            const Live o = live[k];
            live[k] = live.back();
            live.pop_back();
            push(static_cast<std::uint8_t>(iap::EventType::CANCEL), o.side,
                 o.px, o.qty, o.id, 0);
        } else if (u < 0.92) {
            const auto k = static_cast<std::size_t>(
                rng.below(static_cast<std::int64_t>(live.size())));
            Live& o = live[k];
            const std::int64_t q = std::min<std::int64_t>(o.qty, 100);
            push(static_cast<std::uint8_t>(iap::EventType::EXECUTE), o.side,
                 o.px, q, o.id, next_tid++);
            o.qty -= q;
            if (o.qty == 0) {
                live[k] = live.back();
                live.pop_back();
            }
        } else {
            const std::uint8_t side = rng.uniform() < 0.5 ? 0 : 1;
            push(static_cast<std::uint8_t>(iap::EventType::TRADE), side, mid,
                 100 * (1 + rng.below(5)), 0, next_tid++);
        }
    }
    return out;
}

// ------------------------------------------------------- the pipeline
constexpr int kStages = 5;
const char* const kStageNames[kStages] = {"decode (IAP1 frame + CRC)",
                                          "book update",
                                          "native features (48)",
                                          "alpha score (EQ01+EQ03+EQ06)",
                                          "order decision + pre-trade check"};

struct Hists {
    LogHistogram total;
    std::array<LogHistogram, kStages> stage;
    std::uint64_t vectors = 0, orders = 0, rejects = 0, book_dropped = 0;
};

struct Decider {
    std::int64_t position = 0;
    bool kill_switch = false;
    static constexpr std::int64_t kMaxPosition = 5'000;
    static constexpr double kMaxNotional = 250'000.0;  // per order, $
    static constexpr double kBandBps = 500.0;
    static constexpr double kThreshold = 0.05;  // bps of blended signal
    static constexpr std::int64_t kClipQty = 100;
};

struct Workload {
    std::string name;
    std::vector<iap::MarketEvent> events;
    int timed_passes;
};

void run_pass(const std::vector<std::uint8_t>& frames, std::size_t n_events,
              const std::map<std::uint32_t, double>& ticks,
              const std::array<const iap::LinearZParams*, 3>& alphas,
              Hists* h) {
    constexpr std::size_t kFrame =
        iap::IAP1_HEADER_SIZE + iap::IAP1_RECORD_SIZE + iap::IAP1_TRAILER_SIZE;
    iap::OrderBook book(1, 1);
    iap::FeatureEngine fe(ticks, 0);
    iap::FeatureVector vec;
    Decider d;
    for (std::size_t i = 0; i < n_events; ++i) {
        const std::uint8_t* frame = frames.data() + i * kFrame;
        const auto t0 = Clock::now();
        const auto decoded = iap::decode_iap1(frame, kFrame);
        const iap::MarketEvent& ev = decoded.front();
        const auto t1 = Clock::now();
        const auto st = book.apply(ev);
        const auto t2 = Clock::now();
        const bool emitted = fe.apply(ev, vec);
        const auto t3 = Clock::now();
        double blend = 0.0, wsum = 0.0;
        if (emitted) {
            for (const auto* p : alphas) {
                const auto sig = iap::score_row(*p, vec);
                blend += sig.expected_return * sig.confidence;
                wsum += sig.confidence;
            }
        }
        const auto t4 = Clock::now();
        int order = 0;  // 0 none, 1 sent, -1 rejected
        if (emitted && wsum > 0.0) {
            const double s = blend / wsum;
            if (s > Decider::kThreshold || s < -Decider::kThreshold) {
                const bool buy = s > 0.0;
                const auto bid = book.best_bid();
                const auto ask = book.best_ask();
                if (bid && ask) {
                    const std::int64_t px =
                        buy ? ask->first : bid->first;
                    const double mid = 0.5 * static_cast<double>(
                                                 bid->first + ask->first);
                    const std::int64_t qty = Decider::kClipQty;
                    const std::int64_t new_pos =
                        d.position + (buy ? qty : -qty);
                    const double notional =
                        static_cast<double>(px) * 0.01 *
                        static_cast<double>(qty);
                    const double band =
                        mid > 0 ? 1e4 * std::abs(px - mid) / mid : 1e9;
                    if (d.kill_switch || new_pos > Decider::kMaxPosition ||
                        new_pos < -Decider::kMaxPosition ||
                        notional > Decider::kMaxNotional ||
                        band > Decider::kBandBps) {
                        order = -1;
                    } else {
                        d.position = new_pos;
                        order = 1;
                    }
                }
            }
        }
        const auto t5 = Clock::now();
        if (h != nullptr) {
            h->total.record(ns_between(t0, t5));
            h->stage[0].record(ns_between(t0, t1));
            h->stage[1].record(ns_between(t1, t2));
            h->stage[2].record(ns_between(t2, t3));
            h->stage[3].record(ns_between(t3, t4));
            h->stage[4].record(ns_between(t4, t5));
            h->vectors += emitted ? 1 : 0;
            h->orders += order == 1 ? 1 : 0;
            h->rejects += order == -1 ? 1 : 0;
            h->book_dropped += st == iap::ApplyStatus::APPLIED ? 0 : 1;
        }
        g_sink += ev.sequence + static_cast<std::uint64_t>(order + 1);
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

unsigned online_cpus() {
    std::ifstream f("/proc/cpuinfo");
    std::string line;
    unsigned n = 0;
    while (std::getline(f, line)) {
        if (line.rfind("processor", 0) == 0) ++n;
    }
    return n;
}

}  // namespace

int main(int argc, char** argv) {
    const std::string dir = iap::golden_dir();
    const std::string configs = iap::configs_dir();
    const std::map<std::uint32_t, double> ticks = {{1u, 0.01}};
    const auto params =
        iap::load_alpha_params(configs + "/strategies/alpha_params.json");
    const std::array<const iap::LinearZParams*, 3> alphas = {
        &params.at("EQ01"), &params.at("EQ03"), &params.at("EQ06")};

    std::vector<Workload> workloads;
    workloads.push_back({"golden eq_mbo (2000 ev)",
                         iap::read_jsonl(dir + "/events_eq_mbo.jsonl"), 50});
    workloads.push_back({"generated equity day (seed 20261012, 100000 ev)",
                         generate_equity_day(20261012ULL, 100000), 1});
    const char* const short_names[2] = {"golden", "genday"};

    // Timer overhead: back-to-back clock reads.
    LogHistogram timer;
    for (int i = 0; i < 200000; ++i) {
        const auto a = Clock::now();
        const auto b = Clock::now();
        timer.record(ns_between(a, b));
    }

    std::ostringstream md;
    char buf[512];
    md << "# C++ tick-to-trade latency (bench_tick_to_trade)\n\n";
    md << "- CPU: " << cpu_model() << " (" << online_cpus()
       << " logical CPUs visible, no pinning, shared host)\n";
    md << "- Path per event: one 104-byte IAP1 v2 frame -> decode_iap1 (CRC "
          "verified) -> OrderBook::apply -> FeatureEngine::apply (48 "
          "features, cadence 0) -> score_row EQ01+EQ03+EQ06 -> bench-local "
          "order decision + minimal pre-trade check. NOT included: network, "
          "NIC, kernel (no kernel bypass), order encoding / wire send, venue "
          "round trip, the platform risk engine (Rust/Java/Python), logging, "
          "decision trace.\n";
    md << "- Timing: steady_clock, 6 reads per event, warm-up pass discarded, "
          "fresh state per pass built outside the timed region; HDR-style "
          "log-linear histogram (<= 6.25% bucket width), percentile = bucket "
          "upper bound, max exact.\n";
    std::snprintf(buf, sizeof(buf),
                  "- Timer overhead (back-to-back steady_clock reads, "
                  "included in every figure): p50 %llu ns, p99 %llu ns\n",
                  static_cast<unsigned long long>(timer.quantile(0.50)),
                  static_cast<unsigned long long>(timer.quantile(0.99)));
    md << buf;

    std::ostringstream wide, guard;
    wide << "\n| path, workload | p50 ns | p90 ns | p99 ns | p99.9 ns | "
            "max ns | mean ns | samples |\n|---|---:|---:|---:|---:|---:|---:|"
            "---:|\n";
    guard << "\nGuard rows (compared by tests/harness/"
             "check_bench_regression.py --rows '^t2t .* p(50|99)$'):\n\n"
             "| benchmark (tick-to-trade guard) | ns |\n|---|---:|\n";
    std::ostringstream counts;
    counts << "\n";

    for (std::size_t w = 0; w < workloads.size(); ++w) {
        const auto& wl = workloads[w];
        std::vector<std::uint8_t> frames;
        frames.reserve(wl.events.size() * 104);
        for (const auto& ev : wl.events) {
            const auto f = iap::encode_iap1({ev});
            frames.insert(frames.end(), f.begin(), f.end());
        }
        run_pass(frames, wl.events.size(), ticks, alphas, nullptr);  // warm-up
        Hists h;
        for (int p = 0; p < wl.timed_passes; ++p) {
            run_pass(frames, wl.events.size(), ticks, alphas, &h);
        }
        auto row = [&](const std::string& label, const LogHistogram& x) {
            std::snprintf(
                buf, sizeof(buf),
                "| %s, %s | %llu | %llu | %llu | %llu | %llu | %.1f | %llu |\n",
                label.c_str(), wl.name.c_str(),
                static_cast<unsigned long long>(x.quantile(0.50)),
                static_cast<unsigned long long>(x.quantile(0.90)),
                static_cast<unsigned long long>(x.quantile(0.99)),
                static_cast<unsigned long long>(x.quantile(0.999)),
                static_cast<unsigned long long>(x.max()), x.mean(),
                static_cast<unsigned long long>(x.count()));
            wide << buf;
        };
        row("**tick-to-trade (end to end)**", h.total);
        for (int s = 0; s < kStages; ++s) row(kStageNames[s], h.stage[s]);
        for (const char* q : {"p50", "p99"}) {
            const double qq = q[1] == '5' ? 0.50 : 0.99;
            std::snprintf(buf, sizeof(buf), "| t2t %s %s | %llu |\n",
                          short_names[w], q,
                          static_cast<unsigned long long>(
                              h.total.quantile(qq)));
            guard << buf;
        }
        std::snprintf(buf, sizeof(buf),
                      "- %s: %d timed pass(es); feature vectors %llu, orders "
                      "%llu, pre-trade rejects %llu, book drops %llu\n",
                      wl.name.c_str(), wl.timed_passes,
                      static_cast<unsigned long long>(h.vectors),
                      static_cast<unsigned long long>(h.orders),
                      static_cast<unsigned long long>(h.rejects),
                      static_cast<unsigned long long>(h.book_dropped));
        counts << buf;
    }
    md << wide.str() << counts.str() << guard.str();

    std::printf("%s", md.str().c_str());
    if (argc > 1) {
        std::ofstream out(argv[1], std::ios::binary);
        out << md.str();
        std::printf("\nwrote %s\n", argv[1]);
    }
    return g_sink == 0xFFFFFFFFFFFFFFFFull ? 1 : 0;
}
