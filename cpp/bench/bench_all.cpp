// bench_all: C++ hot-path benchmarks on the golden vectors (spec section 22).
//
// Extends bench_core with the feature engine, alpha scoring and the
// execution-simulator replay. Methodology: steady_clock wall loops with
// warmup, at least 0.5 s / 10 iterations per benchmark, on a 2-CPU
// container (-j2 builds; no CPU pinning available). Workload: the pinned
// golden vectors (2000 EQ MBO events / 800 FX QUOTE events). Prints a
// markdown table plus machine info (CPU model from /proc/cpuinfo, compiler,
// flags) and, with an output path argument, writes the same markdown there
// (benchmarks/results_cpp.md).
//
// Round-3 PLATFORM SEV-3 ("benchmark methodology omits that the workload is
// fully cache-resident"): the cache-residency caveat is emitted BY THIS
// PROGRAM, not hand-added to the markdown afterwards — a hand-edit is wiped
// by the next regeneration, which is how the caveat went missing before. A
// cold, single-pass row over a full generated session (no warmup, no repeat,
// working set far larger than L2) is appended whenever such a vector is
// available, so the hot figures are always printed next to their opposite.
//
// Usage: bench_all [output.md [cold_events.jsonl]]
//   cold_events.jsonl defaults to data/normalized/eq_20260824.normalized.jsonl
//   relative to the golden dir's repo root; the cold rows are omitted (with a
//   printed note) when it does not exist, since data/ is generated, not
//   committed.

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "iap/alpha/alpha.hpp"
#include "iap/features/feature_engine.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/replay/exec_replay.hpp"
#include "iap/replay/replay.hpp"

namespace {

using Clock = std::chrono::steady_clock;

double seconds_since(Clock::time_point start) {
    return std::chrono::duration<double>(Clock::now() - start).count();
}

volatile std::uint64_t g_sink = 0;  // defeat dead-code elimination

struct Result {
    std::string name;
    double ns_per_event;
    double events_per_sec;
    std::uint64_t events;
};

template <typename F>
Result bench(const std::string& name, std::size_t events_per_iter, F&& body) {
    for (int i = 0; i < 3; ++i) body();  // warmup
    int iters = 0;
    auto start = Clock::now();
    double elapsed = 0.0;
    do {
        body();
        ++iters;
        elapsed = seconds_since(start);
    } while (elapsed < 0.5 || iters < 10);
    const double total_events =
        static_cast<double>(events_per_iter) * static_cast<double>(iters);
    return Result{name, elapsed * 1e9 / total_events, total_events / elapsed,
                  static_cast<std::uint64_t>(total_events)};
}

// One single pass over `events_per_iter` events with NO warmup and NO repeat:
// the cold counterpart of bench(). Everything the pass touches (page cache,
// branch predictors, the level map, the allocator's free lists) starts in
// whatever state the process is in, which for a full-day vector means a
// working set that does not fit in L2. Mean over one pass only, so it carries
// more run-to-run variance than the hot rows — it is a reference point, not a
// figure to optimise against.
template <typename F>
Result bench_cold(const std::string& name, std::size_t events_per_iter,
                  F&& body) {
    auto start = Clock::now();
    body();
    const double elapsed = seconds_since(start);
    const double total_events = static_cast<double>(events_per_iter);
    return Result{name, elapsed * 1e9 / total_events, total_events / elapsed,
                  static_cast<std::uint64_t>(total_events)};
}

// Repo-relative rendering of a path built from IAP_GOLDEN_DIR, so the
// generated markdown is identical whatever directory the repo is checked out
// in (REPRODUCIBILITY.md §4: a regenerated artefact must diff only where the
// measurement changed).
std::string repo_relative(const std::string& path) {
    for (const char* root : {"/data/", "/tests/", "/configs/"}) {
        const auto pos = path.rfind(root);
        if (pos != std::string::npos) {
            return path.substr(pos + 1);
        }
    }
    return path;
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

std::string compiler_version() {
#if defined(__clang__)
    return std::string("clang ") + __clang_version__;
#elif defined(__GNUC__)
    return "g++ " + std::to_string(__GNUC__) + "." +
           std::to_string(__GNUC_MINOR__) + "." +
           std::to_string(__GNUC_PATCHLEVEL__);
#else
    return "unknown";
#endif
}

iap::ExecConfig bench_exec_config(const std::string& configs_dir) {
    iap::ExecConfig cfg;
    cfg.seed = 20260829;
    cfg.venues = iap::load_venues(configs_dir + "/venues.json");
    iap::InstrumentSpec ins;
    ins.instrument_id = 1;
    ins.tick_size = 0.01;
    ins.qty_unit = 1.0;
    ins.adv = 38000000.0;
    cfg.instruments[1] = ins;
    return cfg;
}

std::vector<iap::ParentOrder> bench_parents(std::int64_t t0) {
    constexpr std::int64_t kSec = 1'000'000'000;
    iap::ParentOrder vwap;
    vwap.parent_id = 1;
    vwap.instrument_id = 1;
    vwap.venue_id = 1;
    vwap.side = 0;
    vwap.qty = 400;
    vwap.algo = iap::AlgoType::VWAP;
    vwap.start_ts = t0 + 60 * kSec;
    vwap.end_ts = t0 + 660 * kSec;
    vwap.slices = 4;
    iap::ParentOrder is;
    is.parent_id = 2;
    is.instrument_id = 1;
    is.venue_id = 1;
    is.side = 1;
    is.qty = 600;
    is.algo = iap::AlgoType::IS;
    is.start_ts = t0 + 120 * kSec;
    is.end_ts = t0 + 720 * kSec;
    is.slices = 3;
    return {vwap, is};
}

}  // namespace

int main(int argc, char** argv) {
    const std::string dir = IAP_GOLDEN_DIR;
    const std::string configs = dir + "/../../configs";
    const std::string cold_path =
        argc > 2 ? std::string(argv[2])
                 : dir + "/../../data/normalized/eq_20260824.normalized.jsonl";
    const auto eq = iap::read_jsonl(dir + "/events_eq_mbo.jsonl");
    const auto fx = iap::read_jsonl(dir + "/events_fx_quote.jsonl");
    const auto eq_bin = iap::encode_iap1(eq);
    const std::string eq_jsonl = iap::encode_jsonl(eq);
    const std::map<std::uint32_t, double> ticks = {{1u, 0.01}, {101u, 1e-05}};
    const auto params =
        iap::load_alpha_params(configs + "/strategies/alpha_params.json");
    const auto exec_cfg = bench_exec_config(configs);

    // Pre-computed feature rows for the alpha-scoring benchmark.
    std::vector<iap::FeatureVector> eq_rows;
    {
        iap::FeatureEngine engine(ticks, 0);
        engine.run(eq, &eq_rows);
    }

    std::vector<Result> results;

    results.push_back(bench("IAP1 decode (eq, 2000 ev)", eq.size(), [&] {
        auto v = iap::decode_iap1(eq_bin);
        g_sink += v.back().sequence;
    }));
    results.push_back(bench("IAP1 encode (eq, 2000 ev)", eq.size(), [&] {
        auto b = iap::encode_iap1(eq);
        g_sink += b.size();
    }));
    results.push_back(bench("JSONL decode (eq, 2000 ev)", eq.size(), [&] {
        auto v = iap::decode_jsonl(eq_jsonl);
        g_sink += v.size();
    }));
    results.push_back(bench("book update (eq MBO, 2000 ev)", eq.size(), [&] {
        iap::OrderBook book(1, 1);
        for (const auto& ev : eq) book.apply(ev);
        g_sink += book.last_sequence();
    }));
    results.push_back(bench("book update (fx QUOTE, 800 ev)", fx.size(), [&] {
        iap::ConsolidatedBook cons(101);
        for (const auto& ev : fx) cons.apply(ev);
        g_sink += static_cast<std::uint64_t>(cons.trade_flow());
    }));
    results.push_back(bench("replay engine (eq, 2000 ev)", eq.size(), [&] {
        iap::ReplayEngine engine;
        engine.run(eq);
        g_sink += engine.events_processed();
    }));
    results.push_back(
        bench("feature engine (eq, 48 feats, cadence 0)", eq.size(), [&] {
            iap::FeatureEngine engine(ticks, 0);
            iap::FeatureVector vec;
            for (const auto& ev : eq) {
                engine.apply(ev, vec);
                g_sink += static_cast<std::uint64_t>(vec.valid[0]);
            }
        }));
    results.push_back(
        bench("feature engine (fx, 48 feats, cadence 0)", fx.size(), [&] {
            iap::FeatureEngine engine(ticks, 0);
            iap::FeatureVector vec;
            for (const auto& ev : fx) {
                engine.apply(ev, vec);
                g_sink += static_cast<std::uint64_t>(vec.valid[0]);
            }
        }));
    results.push_back(bench("alpha scoring (EQ01+EQ03+EQ06, 2000 rows x 3)",
                            eq_rows.size() * 3, [&] {
        double acc = 0.0;
        for (const auto& row : eq_rows) {
            for (const char* aid : {"EQ01", "EQ03", "EQ06"}) {
                const auto sig = iap::score_row(params.at(aid), row);
                acc += sig.expected_return;
            }
        }
        g_sink += static_cast<std::uint64_t>(acc != 0.0);
    }));
    results.push_back(
        bench("execution sim replay (eq, 2 parents)", eq.size(), [&] {
            iap::ExecutionReplay replay(exec_cfg,
                                        bench_parents(eq.front().exchange_ts));
            const auto res = replay.run(eq);
            g_sink += res.fills.size();
        }));

    // ---- cold reference: one single pass over a full generated session ----
    // data/ is generated, not committed, so the rows are conditional and their
    // absence is reported rather than silently skipped (fail-loud, §8).
    std::vector<Result> cold;
    std::string cold_note;
    {
        std::ifstream probe(cold_path, std::ios::binary);
        if (!probe.good()) {
            cold_note =
                "- Cold rows OMITTED: " + repo_relative(cold_path) +
                " is absent (data/ is generated — run `python3 -m "
                "iap.marketdata` first, then re-run bench_all).\n";
        } else {
            probe.close();
            const auto cold_events = iap::read_jsonl(cold_path);
            if (cold_events.empty()) {
                cold_note = "- Cold rows OMITTED: " + repo_relative(cold_path) +
                            " decoded to 0 events.\n";
            } else {
                const auto cold_bin = iap::encode_iap1(cold_events);
                char nbuf[128];
                std::snprintf(nbuf, sizeof(nbuf),
                              "IAP1 decode (cold, %llu ev, single pass)",
                              static_cast<unsigned long long>(
                                  cold_events.size()));
                cold.push_back(
                    bench_cold(nbuf, cold_events.size(), [&] {
                        auto v = iap::decode_iap1(cold_bin);
                        g_sink += v.back().sequence;
                    }));
                // A book is per (instrument, venue); the full-day vector is
                // every instrument on every venue interleaved, so the cold
                // book row replays the (instrument, venue) stream of the
                // first event and reports ns per event OF THAT STREAM.
                const std::uint32_t cold_instrument =
                    cold_events.front().instrument_id;
                const std::uint32_t cold_venue = cold_events.front().venue_id;
                std::vector<iap::MarketEvent> cold_book_events;
                for (const auto& ev : cold_events) {
                    if (ev.instrument_id == cold_instrument &&
                        ev.venue_id == cold_venue) {
                        cold_book_events.push_back(ev);
                    }
                }
                std::snprintf(nbuf, sizeof(nbuf),
                              "book update (cold, %llu ev, single pass)",
                              static_cast<unsigned long long>(
                                  cold_book_events.size()));
                cold.push_back(
                    bench_cold(nbuf, cold_book_events.size(), [&] {
                        iap::OrderBook book(cold_instrument, cold_venue);
                        for (const auto& ev : cold_book_events) {
                            book.apply(ev);
                        }
                        g_sink += book.last_sequence();
                    }));
            }
        }
    }

    std::ostringstream md;
    md << "# C++ benchmark results (bench_all)\n\n";
    md << "- CPU: " << cpu_model() << " (2-CPU container, no pinning)\n";
    md << "- Compiler: " << compiler_version()
       << ", flags: -O3 -DNDEBUG -Wall -Wextra -Werror (CMake Release), "
          "C++17\n";
    md << "- Method: steady_clock wall loop, 3 warmup iterations, then >= "
          "0.5 s and >= 10 iterations per benchmark; workload = pinned "
          "golden vectors (tests/golden/events_eq_mbo.jsonl 2000 events, "
          "events_fx_quote.jsonl 800 events); single-threaded\n";
    md << "- Caveat: mean-only figures (no tail latency; container timers), "
          "cross-run variance a few percent — methodology per "
          "benchmarks/RESULTS.md\n";
    md << "- **Cache-residency caveat (read before comparing).** Every row in "
          "the first table is a fully **cache-resident, hot** figure. "
          "`bench_all` loops the SAME 2,000-event vector (144 KB of IAP1 "
          "records — comfortably inside L2) at least 10 times after 3 "
          "warmups, and the order book never exceeds a handful of price "
          "levels, so branch predictors, the level map and the free lists are "
          "all warm and the working set never leaves cache. Those are honest "
          "hot-L1/L2 numbers and are the right measure of the CODE's cost, "
          "but a reader replaying a 300k-event trading day from disk — cold "
          "page cache, a deeper book, TLB and LLC misses on the level map — "
          "will NOT reproduce them. Treat them as a lower bound on per-event "
          "cost, and read them next to the cold table below.\n";
    if (!cold_note.empty()) {
        md << cold_note;
    }
    md << "\n| benchmark (hot, cache-resident) | ns/event | events/sec | "
          "events |\n";
    md << "|---|---:|---:|---:|\n";
    char buf[256];
    for (const auto& r : results) {
        std::snprintf(buf, sizeof(buf), "| %s | %.1f | %.0f | %llu |\n",
                      r.name.c_str(), r.ns_per_event, r.events_per_sec,
                      static_cast<unsigned long long>(r.events));
        md << buf;
    }
    if (!cold.empty()) {
        md << "\nCold reference — ONE single pass over a full generated "
              "session (`" << repo_relative(cold_path)
           << "`), no warmup, no repeat, working set far larger than L2. "
              "Single-pass means higher run-to-run variance than the hot "
              "table; this is a reference point, not a target.\n\n";
        md << "| benchmark (cold, single pass) | ns/event | events/sec | "
              "events |\n";
        md << "|---|---:|---:|---:|\n";
        for (const auto& r : cold) {
            std::snprintf(buf, sizeof(buf), "| %s | %.1f | %.0f | %llu |\n",
                          r.name.c_str(), r.ns_per_event, r.events_per_sec,
                          static_cast<unsigned long long>(r.events));
            md << buf;
        }
    }
    std::printf("%s", md.str().c_str());
    if (argc > 1) {
        std::ofstream out(argv[1], std::ios::binary);
        out << md.str();
        std::printf("\nwrote %s\n", argv[1]);
    }
    return g_sink == 0xFFFFFFFFFFFFFFFFull ? 1 : 0;
}
